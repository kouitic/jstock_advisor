import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from jstock_advisor.cli import watchlist_screening as cli_module
from jstock_advisor.domain.entities.classification import StockTypeClassification
from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.entities.watchlist import (
    WatchlistItem,
    WatchlistRegistrationSource,
)
from jstock_advisor.interfaces.candidate_universe import (
    CandidateUniverseError,
)
from jstock_advisor.services.screening_data_provider import (
    ScreeningDataResult,
    ScreeningDataStatus,
    WatchlistScreeningInput,
)
from jstock_advisor.services.watchlist_screening_service import WatchlistScreeningResult

_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)
_runner = CliRunner()

_EMPTY_CLASSIFICATION = StockTypeClassification(
    stock_code="1234",
    classified_at=_NOW,
    types=[],
    primary_type=None,
    confidence=ConfidenceLevel.LOW,
    classification_basis=[],
    data_sources=[],
)


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


def _fake_config(*, enabled: bool = True) -> SimpleNamespace:
    watchlist_screening = SimpleNamespace(
        enabled=enabled,
        scheduled_run_enabled=True,
        candidate_universe=SimpleNamespace(
            provider="csv", csv_path="data/universe/candidate_universe.csv"
        ),
        screening_policy="high_dividend_financial_health",
        screening_data_provider="stock_snapshot",
        max_watchlist_additions_per_run=20,
        notification_enabled=True,
        staged_rollout=SimpleNamespace(candidate_limit=None, market_segment_filter=None),
        scoring=_fake_scoring_config(),
        thresholds=_fake_thresholds_config(),
        stock_display_name=SimpleNamespace(jpx_name_negative_cache_ttl_seconds=60),
    )
    return SimpleNamespace(watchlist_screening=watchlist_screening)


def _watchlist_input(**overrides: object) -> WatchlistScreeningInput:
    from decimal import Decimal

    defaults: dict[str, object] = dict(
        stock_code="1234",
        stock_name="テスト株式会社",
        security_type="STOCK",
        sector=None,
        industry=None,
        current_price=Decimal("3000"),
        shares_outstanding=Decimal("1000000"),
        market_cap=Decimal("3000000000"),
        forecast_eps=None,
        forecast_bps=None,
        current_per=None,
        current_pbr=None,
        equity_ratio_pct=55.0,
        operating_cashflow=Decimal("1000000"),
        payout_ratio_pct=40.0,
        consecutive_dividend_increase_years=3,
        dividend_yield_pct=4.0,
        shareholder_benefit_exists=False,
        shareholder_benefit_yield_pct=None,
        is_dividend_cut_announced=False,
        is_dividend_omission_announced=False,
        is_debt_excess=False,
        is_deficit=False,
        is_going_concern_doubt=False,
        next_earnings_date=None,
        missing_required_fields=[],
        missing_scoring_fields=[],
        stock_type_classification=_EMPTY_CLASSIFICATION,
        avg_trading_value=Decimal("100000000"),
        disclosure_risk_keywords_found=[],
        severe_earnings_decline=False,
        disclosure_available=True,
    )
    defaults.update(overrides)
    return WatchlistScreeningInput(**defaults)  # type: ignore[arg-type]


class _FakeCollector:
    def __init__(self, codes: list[str]) -> None:
        self._codes = codes

    def collect_target_codes(self):
        return CandidateUniverseResultLike(self._codes)


class CandidateUniverseResultLike:
    def __init__(self, codes: list[str]) -> None:
        self.stock_codes = codes
        self.universe_count = len(codes)
        self.duplicate_count = 0
        self.invalid_code_count = 0
        self.holding_excluded_count = 0
        self.watchlist_excluded_count = 0


class _FakeScreeningDataProvider:
    def __init__(self, result: ScreeningDataResult) -> None:
        self._result = result

    def get_screening_input(self, stock_code: str, now: dt.datetime) -> ScreeningDataResult:
        return self._result


def _screening_result(passed: bool) -> WatchlistScreeningResult:
    from jstock_advisor.domain.signals.watchlist_screening import (
        MatchedCriterion,
        ScreeningPolicyResult,
    )

    total_score = 80.0 if passed else 30.0
    policy_result = ScreeningPolicyResult(
        policy_name="high_dividend_financial_health",
        passed=passed,
        score=total_score,
        matched_criteria=[MatchedCriterion.HIGH_DIVIDEND_YIELD] if passed else [],
        exclusion_reasons=[],
        missing_required_fields=[],
        missing_scoring_fields=[],
        score_breakdown={"dividend_yield": total_score},
    )
    return WatchlistScreeningResult(
        stock_code="1234",
        stock_name="テスト株式会社",
        passed=passed,
        policy_results=[policy_result],
        total_score=total_score,
        matched_criteria=[MatchedCriterion.HIGH_DIVIDEND_YIELD] if passed else [],
        exclusion_reasons=[],
        missing_required_fields=[],
        missing_scoring_fields=[],
        evaluated_at=_NOW,
        main_metrics={"配当利回り": "4.2%"},
        classification_basis=[],
    )


class _FakeWatchlistScreeningService:
    def __init__(self, config: object) -> None:
        pass

    def evaluate(self, stock_code, stock_name, input, now):  # noqa: A002
        return _screening_result(passed=True)

    def to_ranking_entry(self, result):
        from jstock_advisor.domain.signals.watchlist_screening import RankingEntry

        return RankingEntry(
            stock_code=result.stock_code,
            total_score=result.total_score,
            policy_scores={"high_dividend_financial_health": result.total_score},
            matched_criteria=result.matched_criteria,
            main_metrics=result.main_metrics,
        )

    @staticmethod
    def rank(entries):
        from jstock_advisor.services.watchlist_screening_service import (
            WatchlistScreeningService,
        )

        return WatchlistScreeningService.rank(entries)

    @staticmethod
    def rank_and_limit(entries, limit):
        from jstock_advisor.services.watchlist_screening_service import (
            WatchlistScreeningService,
        )

        return WatchlistScreeningService.rank_and_limit(entries, limit)


def _patch_common(monkeypatch: pytest.MonkeyPatch, config: SimpleNamespace | None = None) -> None:
    monkeypatch.setattr(cli_module, "load_config", lambda: config or _fake_config())
    monkeypatch.setattr(
        cli_module, "build_real_provider_bundle", lambda now, cfg: SimpleNamespace()
    )
    monkeypatch.setattr(
        cli_module, "build_candidate_universe_provider", lambda cfg, now: object()
    )
    monkeypatch.setattr(cli_module, "build_line_client_from_env", lambda: object())
    monkeypatch.setattr(
        cli_module,
        "build_screening_data_provider",
        lambda providers, config: _FakeScreeningDataProvider(
            ScreeningDataResult(
                status=ScreeningDataStatus.OK,
                input=_watchlist_input(),
                missing_fields=[],
                error_message=None,
            )
        ),
    )
    monkeypatch.setattr(cli_module, "WatchlistScreeningService", _FakeWatchlistScreeningService)
    monkeypatch.setattr(
        cli_module, "WatchlistCandidateCollector", lambda *a, **kw: _FakeCollector(["1234"])
    )
    monkeypatch.setattr(
        cli_module,
        "build_stock_display_name_resolver",
        lambda *_a, **_kw: _FakeStockDisplayNameResolver(),
    )


def test_dry_run_does_not_write_repository_or_send_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        cli_module,
        "WatchlistRepository",
        lambda: pytest.fail("WatchlistRepository should not be instantiated during dry-run"),
    )
    monkeypatch.setattr(
        cli_module,
        "LineNotificationService",
        lambda **kw: pytest.fail(
            "LineNotificationService should not be instantiated during dry-run"
        ),
    )
    audit_calls: list[Any] = []
    monkeypatch.setattr(
        cli_module, "record_candidate_audit", lambda *a, **kw: audit_calls.append((a, kw))
    )
    monkeypatch.setattr(cli_module, "record_batch_audit", lambda **kw: audit_calls.append(kw))
    monkeypatch.setattr(
        cli_module,
        "record_repository_result_audit",
        lambda *a, **kw: pytest.fail(
            "record_repository_result_audit should not be called during dry-run"
        ),
    )

    result = _runner.invoke(cli_module.app, ["run", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert audit_calls == []
    assert "dry-run" in result.output
    assert "追加予定" in result.output
    assert "登録およびLINE通知は行っていません" in result.output


def test_dry_run_disabled_by_config_is_still_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch, _fake_config(enabled=False))
    monkeypatch.setattr(cli_module, "record_candidate_audit", lambda *a, **kw: None)

    result = _runner.invoke(cli_module.app, ["run", "--dry-run"])

    assert result.exit_code == 0, result.output


def test_real_run_rejects_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch, _fake_config(enabled=False))

    result = _runner.invoke(cli_module.app, ["run"])

    assert result.exit_code == 1
    assert "無効化" in result.output


class _FakeWatchlistRepository:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add_if_new(self, item: Any) -> bool:
        self.added.append(item)
        return True


def test_real_run_adds_to_watchlist_and_sends_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(cli_module, "WatchlistRepository", lambda: fake_repo)

    notify_calls: list[Any] = []

    class _FakeNotificationService:
        def __init__(self, **kwargs: object) -> None:
            pass

        def notify_watchlist_additions(self, summary, content_hash):  # noqa: ANN001, ANN201
            notify_calls.append(list(summary.items))
            return True

    monkeypatch.setattr(cli_module, "LineNotificationService", _FakeNotificationService)
    audit_calls: list[Any] = []
    candidate_audit_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cli_module,
        "record_candidate_audit",
        lambda *a, **kw: (audit_calls.append((a, kw)), candidate_audit_calls.append(kw)),
    )
    monkeypatch.setattr(cli_module, "record_batch_audit", lambda **kw: audit_calls.append(kw))
    repo_result_calls: list[tuple] = []
    monkeypatch.setattr(
        cli_module,
        "record_repository_result_audit",
        lambda *a, **kw: repo_result_calls.append(a),
    )

    result = _runner.invoke(cli_module.app, ["run"])

    assert result.exit_code == 0, result.output
    assert len(fake_repo.added) == 1
    assert fake_repo.added[0].stock_code == "1234"
    assert len(notify_calls) == 1
    assert "1件追加しました" in result.output
    # 通常実行(非dry-run)ではrecord_candidate_audit/record_batch_auditの両方が
    # 呼ばれる(dry-runでは一切呼ばれない、別テストで確認済み)。
    assert len(audit_calls) >= 2
    # 実追加件数(1件)とrepository_result="added"の監査件数が一致する
    added_calls = [c for c in repo_result_calls if c[5] == "added"]
    assert len(added_calls) == len(fake_repo.added) == 1
    # batch_id経由で評価AuditLog(record_candidate_audit)とRepository結果AuditLog
    # (record_repository_result_audit)を関連付けられる
    candidate_batch_id = candidate_audit_calls[0]["batch_id"]
    repository_result_batch_id = repo_result_calls[0][0]
    assert candidate_batch_id == repository_result_batch_id


def test_real_run_reports_candidate_universe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)

    class _RaisingCollector:
        def collect_target_codes(self):
            raise CandidateUniverseError("見つかりません")

    monkeypatch.setattr(
        cli_module, "WatchlistCandidateCollector", lambda *a, **kw: _RaisingCollector()
    )

    result = _runner.invoke(cli_module.app, ["run", "--dry-run"])

    assert result.exit_code == 1
    assert "見つかりません" in result.output


# ============================================================================
# Issue #58 Phase B3 (F-O6): CLI screening の provenance
#
# `registration_batch_id` は「どの実行がこの銘柄を追加したか」の記録である。
# Lambda側(watchlist_batch_finalizer.py:518)は設定していたが、CLIは設定して
# いなかったため、CLI追加分は provenance を辿れず、finalizer の復旧分岐
# (同 :550-554)の判定材料にもならなかった。
#
# **resume(途中結果の復旧)は本Phaseでは実装していない。**
# CLI の batch_id は実行ごとに新規発行される実行インスタンスIDであり、
# 再実行では必ず別値になる。そのため provenance を保存しただけでは
# 復旧分岐は成立しない(詳細は Issue #58 の Phase A コメント)。
# ここで固定するのは provenance の記録と、それが誤って復旧の根拠に
# 使われない(別batch・MANUAL・未設定を own-added とみなさない)ことである。
# ============================================================================


class _ExistingAwareWatchlistRepository:
    """既存項目を持つ WatchlistRepository の fake。

    `add_if_new` は「既存があれば触らず False」という本物の契約
    (`local_repository/watchlist_repository.py:29-35`)を再現する。
    """

    def __init__(self, preexisting: dict[str, Any] | None = None) -> None:
        self.items: dict[str, Any] = dict(preexisting or {})
        self.added: list[Any] = []
        self.write_attempts: list[str] = []

    def add_if_new(self, item: Any) -> bool:
        self.write_attempts.append(item.stock_code)
        if item.stock_code in self.items:
            return False
        self.items[item.stock_code] = item
        self.added.append(item)
        return True

    def get(self, stock_code: str) -> Any:
        return self.items.get(stock_code)


def _run_cli_with_repo(
    monkeypatch: pytest.MonkeyPatch, repo: _ExistingAwareWatchlistRepository
) -> tuple[Any, list[tuple], list[Any]]:
    _patch_common(monkeypatch)
    monkeypatch.setattr(cli_module, "WatchlistRepository", lambda: repo)

    notify_calls: list[Any] = []

    class _FakeNotificationService:
        def __init__(self, **kwargs: object) -> None:
            pass

        def notify_watchlist_additions(self, summary, content_hash):  # noqa: ANN001, ANN201
            notify_calls.append((list(summary.items), content_hash))
            return True

    monkeypatch.setattr(cli_module, "LineNotificationService", _FakeNotificationService)
    monkeypatch.setattr(cli_module, "record_candidate_audit", lambda *a, **kw: None)
    monkeypatch.setattr(cli_module, "record_batch_audit", lambda **kw: None)
    repo_result_calls: list[tuple] = []
    monkeypatch.setattr(
        cli_module,
        "record_repository_result_audit",
        lambda *a, **kw: repo_result_calls.append(a),
    )

    result = _runner.invoke(cli_module.app, ["run"])
    assert result.exit_code == 0, result.output
    return result, repo_result_calls, notify_calls


def test_cli_add_records_registration_batch_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """T1: 通常の CLI 追加で `registration_batch_id` が保存される。

    値はその実行の batch_id と一致し、CLI が発行した形式であること。
    """
    repo = _ExistingAwareWatchlistRepository()
    result, repo_result_calls, _ = _run_cli_with_repo(monkeypatch, repo)

    assert len(repo.added) == 1
    added = repo.added[0]
    assert added.registration_batch_id is not None
    # record_repository_result_audit の第1引数が当該実行の batch_id
    assert added.registration_batch_id == repo_result_calls[0][0]
    assert added.registration_batch_id.startswith("watchlist-screening-cli-")
    # provenance は AUTO_SCREENING 追加にのみ付く
    assert added.registration_source is WatchlistRegistrationSource.AUTO_SCREENING


def test_cli_prints_batch_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """T2: batch_id が CLI 出力に現れる(障害時に運用者が控えられる)。"""
    repo = _ExistingAwareWatchlistRepository()
    result, _, _ = _run_cli_with_repo(monkeypatch, repo)

    assert "batch_id: " in result.output
    assert repo.added[0].registration_batch_id in result.output


def test_cli_batch_id_differs_between_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """batch_id は実行インスタンスIDであり、再実行では必ず別値になる。

    これが「provenance を保存しただけでは resume 復旧が成立しない」根拠であり、
    resume を別途設計する必要がある理由(Issue #58 Phase A)。
    """
    first, _, _ = _run_cli_with_repo(monkeypatch, _ExistingAwareWatchlistRepository())
    second, _, _ = _run_cli_with_repo(monkeypatch, _ExistingAwareWatchlistRepository())

    def _batch_id(output: str) -> str:
        line = next(ln for ln in output.splitlines() if ln.startswith("batch_id: "))
        return line.removeprefix("batch_id: ").strip()

    assert _batch_id(first.output) != _batch_id(second.output)


@pytest.mark.parametrize(
    ("case", "preexisting_kwargs"),
    [
        # T4: 別 batch の AUTO_SCREENING 既存項目
        ("different_batch", {
            "registration_source": WatchlistRegistrationSource.AUTO_SCREENING,
            "registration_batch_id": "watchlist-screening-cli-20260101T000000-deadbeef",
        }),
        # T5: 手動登録の既存項目
        ("manual", {"registration_source": WatchlistRegistrationSource.MANUAL}),
        # T6: registration_batch_id 未設定の旧レコード
        ("legacy_without_batch_id", {
            "registration_source": WatchlistRegistrationSource.AUTO_SCREENING,
            "registration_batch_id": None,
        }),
    ],
)
def test_cli_existing_item_is_skipped_and_preserved(
    monkeypatch: pytest.MonkeyPatch, case: str, preexisting_kwargs: dict[str, Any]
) -> None:
    """T4 / T5 / T6: 既存項目は上書きせず `skipped_existing` とする。

    CLI は provenance を**書く**だけで、それを根拠に既存項目を own-added として
    復元する分岐を持たない。別batch・MANUAL・未設定のいずれも、
    誤って「自分が追加した」とみなされないことを固定する。
    """
    preexisting = WatchlistItem(
        stock_code="1234",
        stock_name="既存の銘柄",
        memo="利用者が書いたメモ",
        reason="既存の理由",
        registration_policy="preexisting_policy",
        created_at=_NOW - dt.timedelta(days=30),
        updated_at=_NOW - dt.timedelta(days=30),
        **preexisting_kwargs,
    )
    repo = _ExistingAwareWatchlistRepository({"1234": preexisting})

    _, repo_result_calls, notify_calls = _run_cli_with_repo(monkeypatch, repo)

    # 追加は発生しない
    assert repo.added == []
    assert repo.write_attempts == ["1234"], "add_if_new は1回だけ試行される"
    # skipped_existing として監査される(added にはならない)
    results = [c[5] for c in repo_result_calls]
    assert results == ["skipped_existing"], f"{case}: {results}"
    # 既存項目の内容は一切変わらない
    survivor = repo.get("1234")
    assert survivor is preexisting
    assert survivor.memo == "利用者が書いたメモ"
    assert survivor.registration_source is preexisting.registration_source
    assert survivor.registration_batch_id == preexisting.registration_batch_id
    # 追加0件のため追加通知も送らない
    assert notify_calls == []
