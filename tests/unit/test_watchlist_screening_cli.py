import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from jstock_advisor.cli import watchlist_screening as cli_module
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


def _fake_config(*, enabled: bool = True) -> SimpleNamespace:
    watchlist_screening = SimpleNamespace(
        enabled=enabled,
        weekly_schedule_enabled=True,
        candidate_universe=SimpleNamespace(
            provider="csv", csv_path="data/universe/candidate_universe.csv"
        ),
        screening_policy="high_dividend_financial_health",
        screening_data_provider="stock_snapshot",
        max_watchlist_additions_per_run=20,
        notification_enabled=True,
        staged_rollout=SimpleNamespace(candidate_limit=None, market_segment_filter=None),
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
    from jstock_advisor.domain.signals.watchlist_screening import MatchedCriterion

    return WatchlistScreeningResult(
        stock_code="1234",
        stock_name="テスト株式会社",
        passed=passed,
        policy_results=[],
        total_score=80.0 if passed else 30.0,
        matched_criteria=[MatchedCriterion.HIGH_DIVIDEND_YIELD] if passed else [],
        exclusion_reasons=[],
        missing_required_fields=[],
        missing_scoring_fields=[],
        evaluated_at=_NOW,
        main_metrics={"配当利回り": "4.2%"},
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
        "StockSnapshotScreeningDataProvider",
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

    result = _runner.invoke(cli_module.app, ["--dry-run"])

    assert result.exit_code == 0, result.output
    assert audit_calls == []
    assert "dry-run" in result.output
    assert "追加予定" in result.output
    assert "登録およびLINE通知は行っていません" in result.output


def test_dry_run_disabled_by_config_is_still_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch, _fake_config(enabled=False))
    monkeypatch.setattr(cli_module, "record_candidate_audit", lambda *a, **kw: None)

    result = _runner.invoke(cli_module.app, ["--dry-run"])

    assert result.exit_code == 0, result.output


def test_real_run_rejects_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch, _fake_config(enabled=False))

    result = _runner.invoke(cli_module.app, [])

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

        def notify_watchlist_additions(self, added_items, results_by_code, policy_name, now):
            notify_calls.append(added_items)
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

    result = _runner.invoke(cli_module.app, [])

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

    result = _runner.invoke(cli_module.app, ["--dry-run"])

    assert result.exit_code == 1
    assert "見つかりません" in result.output
