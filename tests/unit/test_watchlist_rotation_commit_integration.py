"""永続ラウンドロビン方式(計画Part A-5/A-6/A-9)の「rotation commit」条件の
結合テスト(テストF〜K)。

rotation commitは「銘柄評価が完了したか」ではなく「その選択windowに対する
業務処理(ランキング・ウォッチリスト追加・通知)が`_finish_batch()`へ到達し
`mark_watchlist_batch_completed()`が呼ばれたか」で判定する
(watchlist_batch_finalizer.py::_maybe_commit_rotation参照)。本ファイルは
その境界を、実際に`maybe_finalize`/`maybe_finalize_maintenance`を駆動して確認する。

test_watchlist_finalize_integration.pyと同じmoto DynamoDBフィクスチャ・
フェイクRepository/NotificationServiceパターンを踏襲する。rotation状態
(WatchlistScreeningRotationState)はDynamoDBではなくローカルJSON実装
(`_commit_local`、非Lambda環境の既定)を使うため、`get_rotation_state`/
`try_commit_rotation_advance`をtmp_path束縛のラッパーへ差し替えて、実データ
ディレクトリ(data/local_store/)を汚染しないようにする。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.signals.watchlist_screening import RankingEntry
from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.infrastructure.aws.batch_tracker import (
    WatchlistBatchStatus,
    WatchlistProgressStatus,
)
from jstock_advisor.infrastructure.aws.watchlist_rotation_state import (
    create_rotation_state_if_absent,
    get_rotation_state,
    try_commit_rotation_advance,
)
from jstock_advisor.infrastructure.local_repository.audit_log_repository import (
    AuditLogRepository,
)
from jstock_advisor.services import audit_service as audit_service_module
from jstock_advisor.services import watchlist_batch_finalizer as finalizer_module
from jstock_advisor.services.watchlist_batch_finalizer import (
    maybe_finalize,
    maybe_finalize_maintenance,
)

_REGION = "ap-northeast-1"
_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)
_BATCH_TABLE = "jstock-batch_runs"
_PROGRESS_TABLE = "jstock-watchlist_candidate_progress"


@pytest.fixture(autouse=True)
def _stub_display_name_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        finalizer_module,
        "build_stock_display_name_resolver",
        lambda *_a, **_kw: _FakeStockDisplayNameResolver(),
    )


@pytest.fixture(autouse=True)
def _isolate_audit_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """record_batch_audit/record_rotation_commit_auditが実データディレクトリ
    (data/local_store/audit_log.json)を汚染しないようにする。"""
    audit_dir = tmp_path / "audit"
    monkeypatch.setattr(
        audit_service_module,
        "AuditLogRepository",
        lambda store_dir=None: AuditLogRepository(store_dir=audit_dir),
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


@pytest.fixture
def rotation_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """rotation stateの読み書きをtmp_path束縛のローカルJSONストアへ固定する。"""
    store_dir = tmp_path / "rotation"

    def _get(rotation_id: str = "default") -> Any:
        return get_rotation_state(rotation_id, store_dir=store_dir)

    def _commit(*args: Any, **kwargs: Any) -> bool:
        return try_commit_rotation_advance(*args, **kwargs, store_dir=store_dir)

    monkeypatch.setattr(finalizer_module, "get_rotation_state", _get)
    monkeypatch.setattr(finalizer_module, "try_commit_rotation_advance", _commit)
    return store_dir


class _FakeStockDisplayNameResolver:
    def resolve(self, stock_code, fallback_name=None, fallback_name_provider=None):  # noqa: ANN001, ANN201
        if fallback_name:
            return fallback_name
        if fallback_name_provider is not None:
            provided = fallback_name_provider()
            if provided:
                return provided
        return stock_code


class _FakeWatchlistRepository:
    def __init__(self, preexisting: list[Any] | None = None) -> None:
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
    high_throttle_rate_threshold_pct: float = 100.0,
    max_scoring_field_missing_rate_pct: float = 100.0,
    max_data_error_rate_pct: float = 100.0,
    max_not_found_rate_pct: float = 100.0,
    max_terminal_failure_rate_pct: float = 100.0,
    max_required_field_missing_rate_pct: float = 100.0,
) -> SimpleNamespace:
    watchlist_screening = SimpleNamespace(
        candidate_universe=SimpleNamespace(provider="jpx"),
        screening_policy="high_dividend_financial_health",
        max_watchlist_additions_per_run=20,
        notification_enabled=True,
        high_throttle_rate_threshold_pct=high_throttle_rate_threshold_pct,
        max_scoring_field_missing_rate_pct=max_scoring_field_missing_rate_pct,
        max_data_error_rate_pct=max_data_error_rate_pct,
        max_not_found_rate_pct=max_not_found_rate_pct,
        max_terminal_failure_rate_pct=max_terminal_failure_rate_pct,
        max_required_field_missing_rate_pct=max_required_field_missing_rate_pct,
        max_notification_retry_attempts=3,
        scoring=_fake_scoring_config(),
        thresholds=_fake_thresholds_config(),
        stock_display_name=SimpleNamespace(jpx_name_negative_cache_ttl_seconds=60),
        auto_removal=SimpleNamespace(
            enabled=True,
            readd_cooldown_days=30,
            minimum_age_days=90,
            consecutive_not_qualified_required=3,
            minimum_not_qualified_span_days=28,
            stale_recheck_days=30,
            maximum_unconfirmed_days=180,
        ),
    )
    return SimpleNamespace(watchlist_screening=watchlist_screening)


def _providers() -> SimpleNamespace:
    return SimpleNamespace(financial_data=SimpleNamespace(get_financial_summary=lambda code: None))


def _drive_rotation_batch(
    now: dt.datetime,
    batch_id: str,
    candidates: list[tuple[str, str]],
    *,
    job_type: str = "NEW_CANDIDATE_SCREENING",
    rotation_cycle: int | None = 3,
    rotation_start_key: list[str] | None = None,
    rotation_end_key: list[str] | None = None,
    rotation_wrapped: bool = False,
) -> None:
    batch_tracker.try_acquire_dispatch_lease(batch_id, "dispatcher", now, 360, 72)
    batch_tracker.set_watchlist_batch_total(
        batch_id,
        len(candidates),
        72,
        now,
        job_type=job_type,
        rotation_cycle=rotation_cycle,
        rotation_start_key=rotation_start_key,
        rotation_end_key=rotation_end_key,
        rotation_wrapped=rotation_wrapped,
    )
    batch_tracker.create_missing_candidate_progress_rows(
        batch_id, [code for code, _ in candidates], now, 72
    )
    batch_tracker.mark_dispatch_completed(batch_id, now)
    for stock_code, evaluation_result in candidates:
        batch_tracker.claim_candidate_lease(batch_id, stock_code, "owner-a", now, 240)
        if evaluation_result == "PASSED":
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
        else:
            # poison stock相当: watchlist_worker_handler._evaluate_candidate()の
            # try/exceptがUNEXPECTED_ERRORとして即座に終端する経路を模擬する。
            batch_tracker.complete_candidate(
                batch_id,
                stock_code,
                "owner-a",
                terminal_status=WatchlistProgressStatus.FAILED,
                evaluation_result=evaluation_result,
                ranking_entry=None,
                is_provider_failure_suspected=False,
                missing_field_names=[],
                processing_duration_ms=100,
                now=now,
            )


# --- テストF: poison stockはrotation commitをブロックしない ---------------------


def test_poison_stock_does_not_block_rotation_commit(
    dynamo, rotation_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_rotation_state_if_absent(_NOW, store_dir=rotation_store)
    _drive_rotation_batch(
        _NOW,
        "batch-1",
        [("1111", "PASSED"), ("9999", "UNEXPECTED_ERROR")],
        rotation_start_key=None,
        rotation_end_key=["Prime", "9999"],
    )
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = _providers()

    finalized = maybe_finalize("batch-1", _NOW, providers, config, fake_notification)
    assert finalized is True

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.COMPLETED.value

    state = get_rotation_state(store_dir=rotation_store)
    assert state is not None
    assert state.last_stock_code == "9999"
    assert state.pointer_version == 2  # 1(初期作成)→2(commit成功)


# --- 本番検証2026-08対応: _maybe_commit_rotation到達時にrotation dispatch ------
# --- leaseを解放すること(rotation cursor CASとは別責務、両方維持する) ----------


def test_finish_batch_releases_rotation_dispatch_lease(
    dynamo, rotation_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """業務処理の確定(_finish_batch到達)は、rotation cursor commitの成否に
    かかわらず、rotation dispatch leaseを解放する唯一の正規経路でもある。"""
    create_rotation_state_if_absent(_NOW, store_dir=rotation_store)
    _drive_rotation_batch(
        _NOW,
        "batch-1",
        [("1111", "PASSED")],
        rotation_start_key=None,
        rotation_end_key=["Prime", "1111"],
    )
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()

    release_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        finalizer_module,
        "release_rotation_dispatch_lease",
        lambda rotation_id, batch_id: release_calls.append((rotation_id, batch_id)),
    )

    finalized = maybe_finalize("batch-1", _NOW, _providers(), _fake_config(), fake_notification)
    assert finalized is True

    assert release_calls == [("default", "batch-1")]


# --- テストG: ランキング処理の技術的失敗はrotation commitをブロックする ----------


def test_ranking_phase_technical_failure_blocks_rotation_commit(
    dynamo, rotation_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_rotation_state_if_absent(_NOW, store_dir=rotation_store)
    _drive_rotation_batch(
        _NOW,
        "batch-1",
        [("1111", "PASSED"), ("2222", "PASSED")],
        rotation_end_key=["Prime", "2222"],
    )
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = _providers()

    original_record_finalize_target = finalizer_module.record_finalize_target

    def _flaky_record_finalize_target(
        batch_id: str, now: dt.datetime, target_codes: list[str], ranking_json: str
    ) -> bool:
        raise RuntimeError("ranking phase technical failure (simulated)")

    monkeypatch.setattr(finalizer_module, "record_finalize_target", _flaky_record_finalize_target)

    with pytest.raises(RuntimeError, match="ranking phase technical failure"):
        maybe_finalize("batch-1", _NOW, providers, config, fake_notification)

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.FINALIZE_FAILED.value

    # _finish_batch()へ到達していないため、rotation stateは一切変化しない。
    state = get_rotation_state(store_dir=rotation_store)
    assert state is not None
    assert state.pointer_version == 1
    assert state.last_stock_code is None

    monkeypatch.setattr(finalizer_module, "record_finalize_target", original_record_finalize_target)


# --- テストH: ウォッチリスト書き込み途中の技術的失敗もrotation commitをブロックする ---


def test_watchlist_write_phase_technical_failure_blocks_rotation_commit(
    dynamo, rotation_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_rotation_state_if_absent(_NOW, store_dir=rotation_store)
    _drive_rotation_batch(
        _NOW,
        "batch-1",
        [("1111", "PASSED"), ("2222", "PASSED")],
        rotation_end_key=["Prime", "2222"],
    )
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)
    fake_notification = _FakeNotificationService()
    config = _fake_config()
    providers = _providers()

    def _flaky_fetch_stock_name(providers_arg: Any, stock_code: str) -> str | None:
        raise RuntimeError("watchlist write phase technical failure (simulated)")

    monkeypatch.setattr(finalizer_module, "_fetch_stock_name", _flaky_fetch_stock_name)

    with pytest.raises(RuntimeError, match="watchlist write phase technical failure"):
        maybe_finalize("batch-1", _NOW, providers, config, fake_notification)

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.FINALIZE_FAILED.value

    state = get_rotation_state(store_dir=rotation_store)
    assert state is not None
    assert state.pointer_version == 1
    assert state.last_stock_code is None


# --- テストI: 品質基準によるABORTEDは正常な業務判断としてrotation commitする -----


def test_quality_gate_abort_still_commits_rotation(
    dynamo, rotation_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_rotation_state_if_absent(_NOW, store_dir=rotation_store)
    _drive_rotation_batch(
        _NOW,
        "batch-1",
        [("1111", "PASSED"), ("2222", "PASSED"), ("3333", "PASSED"), ("4444", "PASSED")],
        rotation_end_key=["Prime", "4444"],
    )
    # 主要スコア項目の欠損率を意図的に閾値超過させ、ABORTEDへ誘導する
    # (test_watchlist_finalize_integration.pyの同種テストと同じ手法)。
    for stock_code in ("1111", "2222", "3333"):
        batch_tracker._progress_table().update_item(
            Key={"batch_id": "batch-1", "stock_code": stock_code},
            UpdateExpression="SET missing_field_names = :missing",
            ExpressionAttributeValues={":missing": ["dividend_yield_pct"]},
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

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.ABORTED.value
    assert fake_repo.added == []

    # ABORTEDは「今回は追加を見送る」という正常な業務判断のため、rotationは進む。
    state = get_rotation_state(store_dir=rotation_store)
    assert state is not None
    assert state.last_stock_code == "4444"
    assert state.pointer_version == 2


# --- テストK: WATCHLIST_MAINTENANCEはrotation stateへ一切到達しない -------------


def test_maintenance_job_never_touches_rotation_state(
    dynamo, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("WATCHLIST_MAINTENANCEはrotation stateへ到達してはならない")

    monkeypatch.setattr(finalizer_module, "get_rotation_state", _fail_if_called)
    monkeypatch.setattr(finalizer_module, "try_commit_rotation_advance", _fail_if_called)

    class _FakeMaintenanceWatchlistRepository:
        def __init__(self) -> None:
            self._items: dict[str, Any] = {}

        def get(self, stock_code: str) -> Any | None:
            return self._items.get(stock_code)

        def upsert(self, item: Any) -> None:
            self._items[item.stock_code] = item

        def delete(self, stock_code: str) -> bool:
            return self._items.pop(stock_code, None) is not None

    from jstock_advisor.domain.entities.enums import WatchlistRegistrationSource
    from jstock_advisor.domain.entities.watchlist import WatchlistItem
    from jstock_advisor.services.watchlist_maintenance_service import (
        MaintenanceScreeningSummary,
    )

    fake_repo = _FakeMaintenanceWatchlistRepository()
    fake_repo.upsert(
        WatchlistItem(
            stock_code="1111",
            stock_name="テスト銘柄",
            reason="自動追加",
            registration_source=WatchlistRegistrationSource.AUTO_SCREENING,
            registration_policy="high_dividend_financial_health",
            created_at=_NOW - dt.timedelta(days=200),
            updated_at=_NOW - dt.timedelta(days=200),
        )
    )
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: fake_repo)

    auto_removal_config = SimpleNamespace(
        enabled=True,
        minimum_age_days=90,
        consecutive_not_qualified_required=3,
        minimum_not_qualified_span_days=28,
        stale_recheck_days=30,
        maximum_unconfirmed_days=180,
        readd_cooldown_days=30,
    )
    config = SimpleNamespace(
        watchlist_screening=SimpleNamespace(
            screening_policy="high_dividend_financial_health",
            auto_removal=auto_removal_config,
        )
    )

    batch_id = "watchlist-maint-1"
    batch_tracker.try_acquire_dispatch_lease(batch_id, "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total(
        batch_id, 1, 72, _NOW, job_type="WATCHLIST_MAINTENANCE"
    )
    batch_tracker.create_missing_candidate_progress_rows(batch_id, ["1111"], _NOW, 72)
    batch_tracker.mark_dispatch_completed(batch_id, _NOW)
    batch_tracker.claim_candidate_lease(batch_id, "1111", "owner-a", _NOW, 240)
    summary = MaintenanceScreeningSummary(
        passed=True, total_score=80.0, matched_target_types=["INCOME"], policy_name="p"
    )
    batch_tracker.complete_candidate(
        batch_id,
        "1111",
        "owner-a",
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result="PASSED",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=_NOW,
        screening_summary_json=summary.model_dump_json(),
    )

    finalized = maybe_finalize_maintenance(batch_id, _NOW, config)
    assert finalized is True

    batch = batch_tracker.get_watchlist_batch(batch_id)
    assert batch is not None
    assert batch["status"] == WatchlistBatchStatus.COMPLETED.value
    # rotation stateが一切参照されなかったこと(_fail_if_calledが呼ばれれば
    # AssertionErrorでこのテスト自体が失敗する)。
