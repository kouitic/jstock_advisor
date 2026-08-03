"""運用ハードニング5節・第2弾2節・第3弾: finalize処理の再実行安全性の結合テスト。
batch_tracker(moto DynamoDB)と watchlist_batch_finalizer を実際に組み合わせ、
以下を確認する。

- finalize処理の途中で例外が発生しても、それより前に成功したウォッチリスト追加・
  LINE通知自体は取り消されないこと。
- 例外後にFINALIZE_FAILEDへ遷移し、retry_finalize()で再実行しても、既に追加済みの
  銘柄が重複追加されず、LINE通知も再送されないこと。
- 主要スコア項目の欠損率が高い場合、データ提供元障害疑い率が低くてもABORTEDとなり
  部分結果をウォッチリストへ追加しないこと。
- 運用ハードニング第3弾: LINE送信失敗はNOTIFICATION_SENTとして記録されず、
  Reconciler/CLIが通知のみ再試行できること。add_if_new成功後・repository_results
  永続化前の障害から復元できること。batch auditが重複記録されないこと。
  content hashが再試行時刻に依存しないこと。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.entities.enums import WatchlistRegistrationSource
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.domain.signals.watchlist_screening import RankingEntry
from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.infrastructure.aws.batch_tracker import (
    WatchlistBatchStatus,
    WatchlistProgressStatus,
)
from jstock_advisor.infrastructure.local_repository.audit_log_repository import (
    AuditLogRepository,
)
from jstock_advisor.services import audit_service as audit_service_module
from jstock_advisor.services import watchlist_batch_finalizer as finalizer_module
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.watchlist_batch_finalizer import (
    maybe_finalize,
    retry_finalize,
    retry_notification,
)

_REGION = "ap-northeast-1"
_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)
_BATCH_TABLE = "jstock-batch_runs"
_PROGRESS_TABLE = "jstock-watchlist_candidate_progress"


@pytest.fixture(autouse=True)
def _stub_display_name_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """テストではJPX/override/既存Watchlistの実I/Oを避ける
    (このリポジトリの既存テストの慣例に合わせる)。"""
    monkeypatch.setattr(
        finalizer_module,
        "build_stock_display_name_resolver",
        lambda *_a, **_kw: _FakeStockDisplayNameResolver(),
    )


@pytest.fixture
def dynamo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_BATCH_TABLE,
            KeySchema=[{"AttributeName": "batch_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "batch_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName=_PROGRESS_TABLE,
            KeySchema=[
                {"AttributeName": "batch_id", "KeyType": "HASH"},
                {"AttributeName": "stock_code", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "batch_id", "AttributeType": "S"},
                {"AttributeName": "stock_code", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def _fake_scoring_config() -> SimpleNamespace:
    return SimpleNamespace(
        minimum_total_score=60.0,
        dividend_yield=SimpleNamespace(weight=30.0, zero_at_pct=3.5, full_at_pct=6.0),
        equity_ratio=SimpleNamespace(weight=25.0, zero_at_pct=40.0, full_at_pct=70.0),
        payout_ratio=SimpleNamespace(weight=15.0, healthy_min_pct=20.0, healthy_max_pct=60.0),
        dividend_growth=SimpleNamespace(weight=15.0, zero_at_years=0, full_at_years=10),
        shareholder_benefit=SimpleNamespace(
            weight=15.0, yield_full_at_pct=2.0, presence_only_score_ratio=0.5
        ),
    )


def _fake_thresholds_config() -> SimpleNamespace:
    return SimpleNamespace(
        minimum_market_cap_yen=50_000_000_000,
        require_positive_operating_cash_flow=True,
        exclude_dividend_cut_announced=True,
        exclude_debt_excess=True,
        exclude_deficit=True,
        exclude_going_concern_doubt=True,
        exclude_etf=True,
        exclude_reit=True,
    )


def _fake_config(
    *,
    high_throttle_rate_threshold_pct: float = 20.0,
    max_scoring_field_missing_rate_pct: float = 30.0,
    max_data_error_rate_pct: float = 100.0,
    max_not_found_rate_pct: float = 100.0,
    max_terminal_failure_rate_pct: float = 100.0,
    max_required_field_missing_rate_pct: float = 100.0,
    max_notification_retry_attempts: int = 3,
    notification_enabled: bool = True,
) -> SimpleNamespace:
    watchlist_screening = SimpleNamespace(
        candidate_universe=SimpleNamespace(provider="jpx"),
        screening_policy="high_dividend_financial_health",
        max_watchlist_additions_per_run=20,
        notification_enabled=notification_enabled,
        high_throttle_rate_threshold_pct=high_throttle_rate_threshold_pct,
        max_scoring_field_missing_rate_pct=max_scoring_field_missing_rate_pct,
        max_data_error_rate_pct=max_data_error_rate_pct,
        max_not_found_rate_pct=max_not_found_rate_pct,
        max_terminal_failure_rate_pct=max_terminal_failure_rate_pct,
        max_required_field_missing_rate_pct=max_required_field_missing_rate_pct,
        max_notification_retry_attempts=max_notification_retry_attempts,
        scoring=_fake_scoring_config(),
        thresholds=_fake_thresholds_config(),
        stock_display_name=SimpleNamespace(jpx_name_negative_cache_ttl_seconds=60),
    )
    return SimpleNamespace(watchlist_screening=watchlist_screening)


class _FakeStockDisplayNameResolver:
    """テストではJPX/override/既存Watchlistの実I/Oを避け、fallbackのみで
    解決する(このリポジトリの既存テストの慣例に合わせる)。"""

    def resolve(self, stock_code, fallback_name=None, fallback_name_provider=None):  # noqa: ANN001, ANN201
        if fallback_name:
            return fallback_name
        if fallback_name_provider is not None:
            provided = fallback_name_provider()
            if provided:
                return provided
        return stock_code


class _FakeWatchlistRepository:
    def __init__(self, preexisting: list[WatchlistItem] | None = None) -> None:
        self.added: list[Any] = list(preexisting or [])

    def add_if_new(self, item: Any) -> bool:
        if any(existing.stock_code == item.stock_code for existing in self.added):
            return False
        self.added.append(item)
        return True

    def get(self, stock_code: str) -> Any | None:
        return next((item for item in self.added if item.stock_code == stock_code), None)


class _FakeNotificationService:
    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    def notify_watchlist_additions(self, summary, content_hash):  # noqa: ANN001, ANN201
        self.calls.append(list(summary.items))
        return True


def _make_ranking_entry(stock_code: str) -> str:
    return RankingEntry(
        stock_code=stock_code,
        total_score=80.0,
        policy_scores={"high_dividend_financial_health": 80.0},
        matched_criteria=[],
        main_metrics={},
    ).model_dump_json()


def _drive_batch_with_two_passed_candidates(now: dt.datetime, batch_id: str = "batch-1") -> None:
    batch_tracker.try_acquire_dispatch_lease(batch_id, "dispatcher", now, 360, 72)
    batch_tracker.set_watchlist_batch_total(batch_id, 2, 72, now)
    batch_tracker.create_missing_candidate_progress_rows(batch_id, ["1111", "2222"], now, 72)
    batch_tracker.mark_dispatch_completed(batch_id, now)
    for stock_code in ("1111", "2222"):
        batch_tracker.claim_candidate_lease(batch_id, stock_code, "owner-a", now, 240)
        batch_tracker.complete_candidate(
            batch_id,
            stock_code,
            "owner-a",
            terminal_status=WatchlistProgressStatus.COMPLETED,
            evaluation_result="PASSED",
            ranking_entry=_make_ranking_entry(stock_code),
            is_provider_failure_suspected=False,
            missing_field_names=[],
            processing_duration_ms=100,
            now=now,
            total_score=80.0,
        )


def _providers() -> SimpleNamespace:
    return SimpleNamespace(financial_data=SimpleNamespace(get_financial_summary=lambda code: None))


def test_exception_after_watchlist_additions_does_not_lose_or_duplicate_on_retry(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """finalize処理中(LINE通知成功後・完了記録前)に例外が発生した場合の挙動。
    1回目: add_if_newで2件とも追加・LINE通知成功、その直後のrecord_batch_auditで
    例外(監査ログ書き込み失敗を想定)→ FINALIZE_FAILEDへ。
    2回目(retry_finalize): record_batch_auditは正常に動くよう差し替え、
    add_if_newは同じ銘柄コードに対して重複追加せず、LINE通知も再送されないこと。
    """
    _drive_batch_with_two_passed_candidates(_NOW)

    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()

    call_count = {"n": 0}
    original_record_batch_audit = finalizer_module.record_batch_audit

    def _flaky_record_batch_audit(**kwargs: Any) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("audit log write failed (simulated)")
        original_record_batch_audit(**kwargs)

    monkeypatch.setattr(finalizer_module, "record_batch_audit", _flaky_record_batch_audit)

    config = _fake_config()
    providers = _providers()

    with pytest.raises(RuntimeError, match="audit log write failed"):
        maybe_finalize("batch-1", _NOW, providers, config, fake_notification)

    # 監査ログ書き込みより前に成功していたウォッチリスト追加・LINE通知は取り消されない。
    assert {item.stock_code for item in fake_repo.added} == {"1111", "2222"}
    assert len(fake_notification.calls) == 1
    assert {item.stock_code for item in fake_notification.calls[0]} == {"1111", "2222"}

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.FINALIZE_FAILED.value

    later = _NOW + dt.timedelta(minutes=1)
    retried = retry_finalize("batch-1", later, providers, config, fake_notification)
    assert retried is True

    # 再実行しても重複追加は発生しない。
    assert {item.stock_code for item in fake_repo.added} == {"1111", "2222"}
    assert len(fake_repo.added) == 2
    # 既に通知済みのため、再実行では通知が再送されない。
    assert len(fake_notification.calls) == 1

    batch_after_retry = batch_tracker.get_watchlist_batch("batch-1")
    assert batch_after_retry is not None
    assert batch_after_retry["status"] == WatchlistBatchStatus.COMPLETED.value


def test_add_if_new_exception_for_one_candidate_does_not_block_others(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1銘柄のRepository書き込み失敗(add_if_new例外)が、他銘柄の追加や
    finalize全体の完了を妨げないこと。"""
    _drive_batch_with_two_passed_candidates(_NOW)

    class _PartiallyFailingRepository:
        def __init__(self) -> None:
            self.added: list[Any] = []

        def add_if_new(self, item: Any) -> bool:
            if item.stock_code == "2222":
                raise RuntimeError("dynamodb write failed (simulated)")
            self.added.append(item)
            return True

        def get(self, stock_code: str) -> Any | None:
            return next((item for item in self.added if item.stock_code == stock_code), None)

    fake_repo = _PartiallyFailingRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = _providers()

    finalized = maybe_finalize("batch-1", _NOW, providers, config, fake_notification)
    assert finalized is True
    assert {item.stock_code for item in fake_repo.added} == {"1111"}

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.COMPLETED.value


def test_finalize_aborted_by_field_missing_rate_even_when_provider_failure_rate_is_low(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """運用ハードニング3節: provider_failure_suspected_rateが閾値(20%)未満でも、
    主要スコア項目の欠損率が閾値(既定30%)を超えていればABORTEDとし、
    部分結果をウォッチリストへ追加しないこと。"""
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 4, 72, _NOW)
    batch_tracker.create_missing_candidate_progress_rows(
        "batch-1", ["1111", "2222", "3333", "4444"], _NOW, 72
    )
    batch_tracker.mark_dispatch_completed("batch-1", _NOW)
    for stock_code in ("1111", "2222", "3333", "4444"):
        batch_tracker.claim_candidate_lease("batch-1", stock_code, "owner-a", _NOW, 240)
        missing = ["dividend_yield_pct"] if stock_code != "4444" else []
        batch_tracker.complete_candidate(
            "batch-1",
            stock_code,
            "owner-a",
            terminal_status=WatchlistProgressStatus.COMPLETED,
            evaluation_result="PASSED",
            ranking_entry=_make_ranking_entry(stock_code),
            is_provider_failure_suspected=False,
            missing_field_names=missing,
            processing_duration_ms=100,
            now=_NOW,
        )

    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config(
        high_throttle_rate_threshold_pct=20.0, max_scoring_field_missing_rate_pct=30.0
    )
    providers = _providers()

    finalized = maybe_finalize("batch-1", _NOW, providers, config, fake_notification)
    assert finalized is True

    assert fake_repo.added == []  # 部分結果は採用されない
    assert fake_notification.calls == []

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.ABORTED.value
    assert batch["execution_result"] == (
        batch_tracker.EXECUTION_RESULT_SCORING_DATA_QUALITY_DEGRADED
    )


# --- 運用ハードニング第2弾2節: finalizeの段階的・再開可能化 -----------------------


class _CountingWatchlistRepository:
    """add_if_newの呼び出し回数を銘柄コード単位で記録するフェイク(再開時に
    既に永続化済みの銘柄へadd_if_newが再度呼ばれないことを確認するため)。"""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.calls: list[str] = []

    def add_if_new(self, item: Any) -> bool:
        self.calls.append(item.stock_code)
        if any(existing.stock_code == item.stock_code for existing in self.added):
            return False
        self.added.append(item)
        return True

    def get(self, stock_code: str) -> Any | None:
        return next((item for item in self.added if item.stock_code == stock_code), None)


class _FakeLineClient:
    def __init__(self) -> None:
        self.push_calls: list[str] = []

    def push_message(self, text: str) -> None:
        self.push_calls.append(text)


def _build_real_notification_service(tmp_path: Any, line_client: _FakeLineClient) -> Any:
    from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
        NotificationLogRepository,
    )
    from jstock_advisor.services.line_notification_service import LineNotificationService

    return LineNotificationService(
        line_client=line_client,
        notification_log_repository=NotificationLogRepository(store_dir=tmp_path),
        recommendation_repository=SimpleNamespace(),
        config=SimpleNamespace(),
        audit_service=SimpleNamespace(),
    )


def test_crash_before_finalize_target_persisted_retries_cleanly(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1件目の追加前(finalize_target_stock_codes永続化前)に失敗するケース。
    再試行時、対象銘柄・ランキングを再計算して正しく追加まで完了すること。"""
    _drive_batch_with_two_passed_candidates(_NOW)
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = _providers()

    call_count = {"n": 0}
    original_record_finalize_target = finalizer_module.record_finalize_target

    def _flaky_record_finalize_target(
        batch_id: str, now: dt.datetime, target_codes: list[str], ranking_json: str
    ) -> bool:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("lambda terminated before any write (simulated)")
        return original_record_finalize_target(batch_id, now, target_codes, ranking_json)

    monkeypatch.setattr(finalizer_module, "record_finalize_target", _flaky_record_finalize_target)

    with pytest.raises(RuntimeError, match="before any write"):
        maybe_finalize("batch-1", _NOW, providers, config, fake_notification)

    assert fake_repo.added == []
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.FINALIZE_FAILED.value
    assert "finalize_target_stock_codes" not in batch

    later = _NOW + dt.timedelta(minutes=1)
    retried = retry_finalize("batch-1", later, providers, config, fake_notification)
    assert retried is True

    assert {item.stock_code for item in fake_repo.added} == {"1111", "2222"}
    batch_after = batch_tracker.get_watchlist_batch("batch-1")
    assert batch_after is not None
    assert batch_after["status"] == WatchlistBatchStatus.COMPLETED.value


def test_crash_between_candidates_resumes_only_unprocessed_ones(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一部銘柄追加後に失敗するケース: 1111の処理(add_if_new+repository_results
    への永続化)が完了した直後、2222の処理を始める前にLambdaが異常終了した場合、
    再試行時に1111へはadd_if_newを再度呼ばず、2222のみを処理すること。"""
    _drive_batch_with_two_passed_candidates(_NOW)

    fake_repo = _CountingWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = _providers()

    original_fetch_stock_name = finalizer_module._fetch_stock_name

    def _flaky_fetch_stock_name(providers_arg: Any, stock_code: str) -> str | None:
        if stock_code == "2222":
            raise RuntimeError("lambda terminated mid-loop (simulated)")
        return original_fetch_stock_name(providers_arg, stock_code)

    monkeypatch.setattr(finalizer_module, "_fetch_stock_name", _flaky_fetch_stock_name)

    with pytest.raises(RuntimeError, match="mid-loop"):
        maybe_finalize("batch-1", _NOW, providers, config, fake_notification)

    assert {item.stock_code for item in fake_repo.added} == {"1111"}
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.FINALIZE_FAILED.value
    assert batch["repository_results"] == {"1111": finalizer_module.REPOSITORY_RESULT_ADDED}

    monkeypatch.setattr(finalizer_module, "_fetch_stock_name", original_fetch_stock_name)
    later = _NOW + dt.timedelta(minutes=1)
    retried = retry_finalize("batch-1", later, providers, config, fake_notification)
    assert retried is True

    # 1111へはadd_if_newが1回しか呼ばれない(再試行で再処理されない=重複追加なし)。
    assert fake_repo.calls == ["1111", "2222"]
    assert {item.stock_code for item in fake_repo.added} == {"1111", "2222"}
    assert len(fake_notification.calls) == 1
    assert {item.stock_code for item in fake_notification.calls[0]} == {"1111", "2222"}


def test_crash_after_write_before_notification_does_not_reprocess_watchlist(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全銘柄追加後・通知前に失敗するケース: 再試行時にWatchlistRepository.
    add_if_newが一切呼ばれず、通知欠落なく通知のみが実行されること。"""
    _drive_batch_with_two_passed_candidates(_NOW)

    fake_repo = _CountingWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = _providers()

    call_count = {"n": 0}
    original_hash = finalizer_module.compute_watchlist_addition_content_hash

    def _flaky_hash(
        batch_id: str, stock_codes: list[str], screening_policy: str, evaluation_date: dt.date
    ) -> str:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("lambda terminated before notification (simulated)")
        return original_hash(batch_id, stock_codes, screening_policy, evaluation_date)

    monkeypatch.setattr(finalizer_module, "compute_watchlist_addition_content_hash", _flaky_hash)

    with pytest.raises(RuntimeError, match="before notification"):
        maybe_finalize("batch-1", _NOW, providers, config, fake_notification)

    assert {item.stock_code for item in fake_repo.added} == {"1111", "2222"}
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.FINALIZE_FAILED.value

    later = _NOW + dt.timedelta(minutes=1)
    retried = retry_finalize("batch-1", later, providers, config, fake_notification)
    assert retried is True

    # 再試行でadd_if_newが再度呼ばれていない(書き込み済みのため、ウォッチリスト
    # 重複追加なし)。
    assert fake_repo.calls == ["1111", "2222"]
    assert len(fake_notification.calls) == 1
    assert {item.stock_code for item in fake_notification.calls[0]} == {"1111", "2222"}


def test_retry_finalize_after_completion_is_a_no_op(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一batch_idに対しretry_finalizeを複数回呼んでも、既にCOMPLETEDへ遷移
    済みの場合は2回目以降もtry_retry_finalizeの条件不成立でFalseを返し、
    重複して追加・通知が行われないこと。"""
    _drive_batch_with_two_passed_candidates(_NOW)
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = _providers()

    finalized = maybe_finalize("batch-1", _NOW, providers, config, fake_notification)
    assert finalized is True

    later = _NOW + dt.timedelta(minutes=1)
    assert retry_finalize("batch-1", later, providers, config, fake_notification) is False
    assert retry_finalize("batch-1", later, providers, config, fake_notification) is False
    assert len(fake_notification.calls) == 1
    assert len(fake_repo.added) == 2


def test_concurrent_retry_finalize_has_only_one_winner(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """複数のReconcilerが同一batch_id(FINALIZE_FAILED)に対して同時に
    retry_finalizeを呼んでも、条件付き更新(try_retry_finalize)により
    片方のみが実処理を獲得できること。"""
    _drive_batch_with_two_passed_candidates(_NOW)
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = _providers()

    call_count = {"n": 0}
    original_record_batch_audit = finalizer_module.record_batch_audit

    def _flaky_record_batch_audit(**kwargs: Any) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated failure")
        original_record_batch_audit(**kwargs)

    monkeypatch.setattr(finalizer_module, "record_batch_audit", _flaky_record_batch_audit)

    with pytest.raises(RuntimeError):
        maybe_finalize("batch-1", _NOW, providers, config, fake_notification)

    # 2つのReconcilerが同時にretry_finalizeを呼んだことを模擬(moto上の
    # 条件付き更新により片方のみ成功すること)。
    first = batch_tracker.try_retry_finalize("batch-1")
    second = batch_tracker.try_retry_finalize("batch-1")
    assert first is True
    assert second is False


# --- 運用ハードニング第3弾1節: LINE送信失敗の状態機械分離 -------------------------


def test_notification_exception_marks_notification_failed_not_sent(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LINE送信例外時にNOTIFICATION_SENTとして記録されず、NOTIFICATION_FAILEDへ
    遷移すること。finalize全体はFINALIZE_FAILEDにならず、ウォッチリスト追加結果は
    維持されること。"""
    _drive_batch_with_two_passed_candidates(_NOW)
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)

    class _FailingNotificationService:
        def __init__(self) -> None:
            self.calls = 0

        def notify_watchlist_additions(self, *args: Any, **kwargs: Any) -> bool:
            self.calls += 1
            raise RuntimeError("LINE push failed (simulated)")

    fake_notification = _FailingNotificationService()
    config = _fake_config(max_notification_retry_attempts=3)
    providers = _providers()

    finalized = maybe_finalize("batch-1", _NOW, providers, config, fake_notification)
    assert finalized is True
    assert fake_notification.calls == 1

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.NOTIFICATION_FAILED.value
    assert "finalize_notified_stock_codes" not in batch
    assert int(batch["notification_failure_count"]) == 1
    # ウォッチリスト追加自体はPhase2で既に確定・保持されている。
    assert {item.stock_code for item in fake_repo.added} == {"1111", "2222"}


def test_retry_notification_recovers_without_rewriting_watchlist(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LINE送信例外後、retry_notification()(Reconciler/CLI相当)が通知のみを
    再試行し、WatchlistRepository.add_if_newが再実行されないこと。"""
    _drive_batch_with_two_passed_candidates(_NOW)
    fake_repo = _CountingWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)

    class _FlakyOnceNotificationService:
        def __init__(self) -> None:
            self.calls = 0

        def notify_watchlist_additions(self, *args: Any, **kwargs: Any) -> bool:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("LINE push failed (simulated)")
            return True

    fake_notification = _FlakyOnceNotificationService()
    config = _fake_config(max_notification_retry_attempts=3)
    providers = _providers()

    finalized = maybe_finalize("batch-1", _NOW, providers, config, fake_notification)
    assert finalized is True
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.NOTIFICATION_FAILED.value

    later = _NOW + dt.timedelta(minutes=1)
    retried = retry_notification("batch-1", later, providers, config, fake_notification)
    assert retried is True
    assert fake_notification.calls == 2

    # ウォッチリスト書込みは再実行されない(add_if_newは最初の2回のみ)。
    assert fake_repo.calls == ["1111", "2222"]

    batch_after = batch_tracker.get_watchlist_batch("batch-1")
    assert batch_after is not None
    assert batch_after["status"] == WatchlistBatchStatus.COMPLETED.value
    assert set(batch_after["finalize_notified_stock_codes"]) == {"1111", "2222"}


def test_notification_exceeding_max_retries_becomes_completed_with_notification_failure(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """通知再試行回数がmax_notification_retry_attemptsを超過した場合のみ
    COMPLETED_WITH_NOTIFICATION_FAILUREへ遷移すること。ウォッチリスト追加結果は
    維持されること。"""
    _drive_batch_with_two_passed_candidates(_NOW)
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)

    class _AlwaysFailingNotificationService:
        def __init__(self) -> None:
            self.calls = 0

        def notify_watchlist_additions(self, *args: Any, **kwargs: Any) -> bool:
            self.calls += 1
            raise RuntimeError("LINE push failed (simulated)")

    fake_notification = _AlwaysFailingNotificationService()
    config = _fake_config(max_notification_retry_attempts=2)
    providers = _providers()

    finalized = maybe_finalize("batch-1", _NOW, providers, config, fake_notification)
    assert finalized is True
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.NOTIFICATION_FAILED.value
    assert int(batch["notification_failure_count"]) == 1

    later = _NOW + dt.timedelta(minutes=1)
    retried = retry_notification("batch-1", later, providers, config, fake_notification)
    assert retried is True

    batch_after = batch_tracker.get_watchlist_batch("batch-1")
    assert batch_after is not None
    assert batch_after["status"] == WatchlistBatchStatus.COMPLETED_WITH_NOTIFICATION_FAILURE.value
    assert int(batch_after["notification_failure_count"]) == 2
    assert {item.stock_code for item in fake_repo.added} == {"1111", "2222"}


def test_concurrent_retry_notification_has_only_one_winner(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """複数のReconcilerが同一batch_id(NOTIFICATION_FAILED)に対して同時に
    通知のみの再試行を呼んでも、条件付き更新(try_retry_notification)により
    片方のみが実処理を獲得できること。"""
    _drive_batch_with_two_passed_candidates(_NOW)
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)

    class _FailingNotificationService:
        def notify_watchlist_additions(self, *args: Any, **kwargs: Any) -> bool:
            raise RuntimeError("LINE push failed (simulated)")

    fake_notification = _FailingNotificationService()
    config = _fake_config(max_notification_retry_attempts=3)
    providers = _providers()

    finalized = maybe_finalize("batch-1", _NOW, providers, config, fake_notification)
    assert finalized is True
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.NOTIFICATION_FAILED.value

    first = batch_tracker.try_retry_notification("batch-1", _NOW)
    second = batch_tracker.try_retry_notification("batch-1", _NOW)
    assert first is True
    assert second is False


def test_notification_disabled_marks_skipped_without_calling_line(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """notification_enabled=falseの場合、LINE送信を試みずSKIPPEDとして明示的に
    記録し、正常にCOMPLETEDへ到達すること。"""
    _drive_batch_with_two_passed_candidates(_NOW)
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config(notification_enabled=False)
    providers = _providers()

    finalized = maybe_finalize("batch-1", _NOW, providers, config, fake_notification)
    assert finalized is True
    assert fake_notification.calls == []

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.COMPLETED.value
    assert batch["finalize_notification_outcome"] == batch_tracker.NOTIFICATION_OUTCOME_SKIPPED


def test_zero_additions_marks_not_required(dynamo, monkeypatch: pytest.MonkeyPatch) -> None:
    """追加0件(合格銘柄なし)の場合、NOT_REQUIREDとして明示的に記録され、LINE通知を
    試みずCOMPLETEDへ到達すること。"""
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 1, 72, _NOW)
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111"], _NOW, 72)
    batch_tracker.mark_dispatch_completed("batch-1", _NOW)
    batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-a", _NOW, 240)
    batch_tracker.complete_candidate(
        "batch-1",
        "1111",
        "owner-a",
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result="REQUIRED_CONDITION_FAILED",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=_NOW,
    )

    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = _providers()

    finalized = maybe_finalize("batch-1", _NOW, providers, config, fake_notification)
    assert finalized is True
    assert fake_notification.calls == []

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.COMPLETED.value
    assert batch["finalize_notification_outcome"] == batch_tracker.NOTIFICATION_OUTCOME_NOT_REQUIRED


def test_notification_dedup_survives_date_crossing_retry(
    dynamo, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """運用ハードニング第3弾4節: LINE送信成功後・状態永続化前に障害が発生し、
    翌日(日付をまたいで)再試行しても、content hashがバッチ開始日(started_at)
    基準のため変わらず、実際の再送(push_message)は1回に抑止されること。"""
    _drive_batch_with_two_passed_candidates(_NOW)
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)

    fake_line_client = _FakeLineClient()
    real_notification = _build_real_notification_service(tmp_path, fake_line_client)
    config = _fake_config()
    providers = _providers()

    call_count = {"n": 0}
    original_resolved = finalizer_module.record_notification_resolved

    def _flaky_resolved(
        batch_id: str, now: dt.datetime, notified_stock_codes: list[str], outcome: str
    ) -> bool:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("lambda terminated after LINE send (simulated)")
        return original_resolved(batch_id, now, notified_stock_codes, outcome)

    monkeypatch.setattr(finalizer_module, "record_notification_resolved", _flaky_resolved)

    with pytest.raises(RuntimeError, match="after LINE send"):
        maybe_finalize("batch-1", _NOW, providers, config, real_notification)

    assert len(fake_line_client.push_calls) == 1
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.FINALIZE_FAILED.value

    next_day = _NOW + dt.timedelta(days=1, minutes=1)
    retried = retry_finalize("batch-1", next_day, providers, config, real_notification)
    assert retried is True

    # 日付をまたいでもcontent hashは変わらないため、実際のLINE push_messageは
    # 1回のみ(既存の重複抑止機構との統合)。
    assert len(fake_line_client.push_calls) == 1
    batch_after = batch_tracker.get_watchlist_batch("batch-1")
    assert batch_after is not None
    assert batch_after["status"] == WatchlistBatchStatus.COMPLETED.value


# --- 運用ハードニング第3弾2節: add_if_new成功後・repository_results保存前の復元 ----


def test_add_if_new_success_recovered_as_added_when_same_batch(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """add_if_new()==True後・record_repository_result_item()永続化前に障害が
    起きたケースを、既にWatchlistRepositoryへ存在する状態を事前投入して再現する。
    再試行時、既存項目のregistration_batch_idが今回のbatch_idと一致すれば
    ADDEDとして復元され、通知対象からも漏れないこと。"""
    _drive_batch_with_two_passed_candidates(_NOW)

    preexisting = WatchlistItem(
        stock_code="1111",
        stock_name="既存銘柄",
        reason="前回試行での追加",
        registration_source=WatchlistRegistrationSource.AUTO_SCREENING,
        registration_policy="high_dividend_financial_health",
        registration_batch_id="batch-1",
        created_at=_NOW,
        updated_at=_NOW,
    )
    fake_repo = _FakeWatchlistRepository(preexisting=[preexisting])
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = _providers()

    finalized = maybe_finalize("batch-1", _NOW, providers, config, fake_notification)
    assert finalized is True

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.COMPLETED.value
    assert batch["repository_results"]["1111"] == finalizer_module.REPOSITORY_RESULT_ADDED
    assert batch["repository_results"]["2222"] == finalizer_module.REPOSITORY_RESULT_ADDED

    # 通知対象からも漏れない(1111・2222の両方が通知される)。
    assert len(fake_notification.calls) == 1
    assert {item.stock_code for item in fake_notification.calls[0]} == {"1111", "2222"}
    # 1111はもともと存在していたためadd_if_newで新規に追加されてはいない
    # (fake_repo.addedには最初から1件だけ含まれていた状態から2222が加わり2件)。
    assert len(fake_repo.added) == 2


def test_add_if_new_existing_from_different_batch_is_skipped_existing(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既存項目が別のbatch_idによるAUTO_SCREENING追加だった場合、復元せず
    SKIPPED_EXISTINGのままとし、通知対象にも含めないこと。"""
    _drive_batch_with_two_passed_candidates(_NOW)

    preexisting = WatchlistItem(
        stock_code="1111",
        stock_name="既存銘柄",
        reason="別バッチでの追加",
        registration_source=WatchlistRegistrationSource.AUTO_SCREENING,
        registration_policy="high_dividend_financial_health",
        registration_batch_id="other-batch-id",
        created_at=_NOW,
        updated_at=_NOW,
    )
    fake_repo = _FakeWatchlistRepository(preexisting=[preexisting])
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = _providers()

    finalized = maybe_finalize("batch-1", _NOW, providers, config, fake_notification)
    assert finalized is True

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.COMPLETED.value
    assert (
        batch["repository_results"]["1111"] == finalizer_module.REPOSITORY_RESULT_SKIPPED_EXISTING
    )
    assert batch["repository_results"]["2222"] == finalizer_module.REPOSITORY_RESULT_ADDED

    assert len(fake_notification.calls) == 1
    assert {item.stock_code for item in fake_notification.calls[0]} == {"2222"}


def test_add_if_new_existing_manual_registration_is_skipped_existing(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既存項目が手動登録(registration_source=MANUAL)だった場合、復元せず
    SKIPPED_EXISTINGのままとし、通知対象にも含めないこと。"""
    _drive_batch_with_two_passed_candidates(_NOW)

    preexisting = WatchlistItem(
        stock_code="1111",
        stock_name="手動登録銘柄",
        reason="利用者による手動登録",
        created_at=_NOW,
        updated_at=_NOW,
    )
    assert preexisting.registration_source == WatchlistRegistrationSource.MANUAL
    fake_repo = _FakeWatchlistRepository(preexisting=[preexisting])
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = _providers()

    finalized = maybe_finalize("batch-1", _NOW, providers, config, fake_notification)
    assert finalized is True

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.COMPLETED.value
    assert (
        batch["repository_results"]["1111"] == finalizer_module.REPOSITORY_RESULT_SKIPPED_EXISTING
    )
    assert batch["repository_results"]["2222"] == finalizer_module.REPOSITORY_RESULT_ADDED

    assert len(fake_notification.calls) == 1
    assert {item.stock_code for item in fake_notification.calls[0]} == {"2222"}


# --- 運用ハードニング第3弾3節: batch auditのbatch_id単位の冪等化 ------------------


def test_batch_audit_is_not_duplicated_when_flag_update_fails(
    dynamo, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """record_batch_audit()成功後・mark_batch_audit_recorded()前に障害が
    起きても、再試行時にrecord_batch_audit()が再度呼ばれるだけで、決定的な
    audit_idによるinsert_if_absentのおかげで実際に新規保存されるのは1回だけに
    なること(フラグだけに重複防止を依存していないことの直接的な確認)。

    record_batch_audit()はAuditService()(引数なし)経由でデフォルトの
    (プロジェクト実データ用)AuditLogRepositoryを使うため、このテストでは
    AuditServiceが使うAuditLogRepositoryクラス自体をtmp_path束縛のものへ
    差し替え、実データディレクトリを汚染しないようにする。
    """
    monkeypatch.setattr(
        audit_service_module,
        "AuditLogRepository",
        lambda store_dir=None: AuditLogRepository(store_dir=tmp_path),
    )
    unique_batch_id = "batch-audit-idempotency-test"
    _drive_batch_with_two_passed_candidates(_NOW, batch_id=unique_batch_id)
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = _providers()

    call_count = {"n": 0}
    original_mark_recorded = finalizer_module.mark_batch_audit_recorded

    def _flaky_mark_batch_audit_recorded(batch_id: str, now: dt.datetime) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("lambda terminated after audit write (simulated)")
        original_mark_recorded(batch_id, now)

    monkeypatch.setattr(
        finalizer_module, "mark_batch_audit_recorded", _flaky_mark_batch_audit_recorded
    )

    insert_results: list[bool] = []
    original_record_if_absent = AuditService.record_if_absent

    def _counting_record_if_absent(self: AuditService, *args: Any, **kwargs: Any) -> Any:
        result = original_record_if_absent(self, *args, **kwargs)
        insert_results.append(result is not None)
        return result

    monkeypatch.setattr(AuditService, "record_if_absent", _counting_record_if_absent)

    with pytest.raises(RuntimeError, match="after audit write"):
        maybe_finalize(unique_batch_id, _NOW, providers, config, fake_notification)

    batch = batch_tracker.get_watchlist_batch(unique_batch_id)
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.FINALIZE_FAILED.value
    assert not batch.get("finalize_batch_audit_recorded")

    later = _NOW + dt.timedelta(minutes=1)
    retried = retry_finalize(unique_batch_id, later, providers, config, fake_notification)
    assert retried is True

    # record_batch_audit自体は2回呼ばれる(フラグが立っていないため)が、
    # insert_if_absentが実際に新規保存したのは1回目のみ(2回目はNoneが返りスキップ)。
    assert insert_results == [True, False]

    batch_after = batch_tracker.get_watchlist_batch(unique_batch_id)
    assert batch_after is not None
    assert batch_after["status"] == WatchlistBatchStatus.COMPLETED.value


def test_crash_after_audit_recorded_before_completed_does_not_rerecord_audit(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """通知完了後・COMPLETED記録前にLambdaが異常終了した場合、再試行時に
    notify_watchlist_additionsもrecord_batch_auditも再度呼ばれず(finalize_batch_
    audit_recorded済みのため)、mark_watchlist_batch_completedのみ実行されること
    (batch audit重複なし・通知重複なし)。"""
    _drive_batch_with_two_passed_candidates(_NOW)
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = _providers()

    audit_calls = {"n": 0}
    original_record_batch_audit = finalizer_module.record_batch_audit

    def _counting_record_batch_audit(**kwargs: Any) -> None:
        audit_calls["n"] += 1
        original_record_batch_audit(**kwargs)

    monkeypatch.setattr(finalizer_module, "record_batch_audit", _counting_record_batch_audit)

    call_count = {"n": 0}
    original_mark_completed = finalizer_module.mark_watchlist_batch_completed

    def _flaky_mark_completed(
        batch_id: str,
        execution_result: str,
        now: dt.datetime,
        notification_permanently_failed: bool = False,
    ) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("lambda terminated after audit (simulated)")
        original_mark_completed(batch_id, execution_result, now, notification_permanently_failed)

    monkeypatch.setattr(finalizer_module, "mark_watchlist_batch_completed", _flaky_mark_completed)

    with pytest.raises(RuntimeError, match="after audit"):
        maybe_finalize("batch-1", _NOW, providers, config, fake_notification)

    assert audit_calls["n"] == 1
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.FINALIZE_FAILED.value
    assert batch.get("finalize_batch_audit_recorded") is True

    later = _NOW + dt.timedelta(minutes=1)
    retried = retry_finalize("batch-1", later, providers, config, fake_notification)
    assert retried is True
    assert audit_calls["n"] == 1  # 再試行でrecord_batch_auditは再度呼ばれない
    assert len(fake_notification.calls) == 1  # 通知も再送されない

    batch_after = batch_tracker.get_watchlist_batch("batch-1")
    assert batch_after is not None
    assert batch_after["status"] == WatchlistBatchStatus.COMPLETED.value
