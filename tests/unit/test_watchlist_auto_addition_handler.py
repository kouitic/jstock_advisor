import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.domain.signals.watchlist_screening import (
    ExclusionReason,
    MatchedCriterion,
    RankingEntry,
)
from jstock_advisor.infrastructure.aws.batch_tracker import BatchProgress
from jstock_advisor.interfaces.candidate_universe import CandidateUniverseError
from jstock_advisor.lambda_handlers import watchlist_auto_addition_handler as handler_module
from jstock_advisor.services.screening_data_provider import (
    ScreeningDataResult,
    ScreeningDataStatus,
    WatchlistScreeningInput,
)
from jstock_advisor.services.watchlist_candidate_collector import CollectorResult
from jstock_advisor.services.watchlist_screening_service import WatchlistScreeningResult

_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)


class _FakeContext:
    function_name = "jstock-advisor-watchlist-auto-addition"


def _fake_config(*, enabled: bool = True, weekly_schedule_enabled: bool = True) -> SimpleNamespace:
    watchlist_screening = SimpleNamespace(
        enabled=enabled,
        weekly_schedule_enabled=weekly_schedule_enabled,
        candidate_universe=SimpleNamespace(provider="csv", csv_path="data/universe/x.csv"),
        screening_policy="high_dividend_financial_health",
        screening_data_provider="stock_snapshot",
        max_watchlist_additions_per_run=20,
        notification_enabled=True,
    )
    return SimpleNamespace(watchlist_screening=watchlist_screening)


def _patch_common(monkeypatch: pytest.MonkeyPatch, config: SimpleNamespace | None = None) -> None:
    monkeypatch.setattr(handler_module, "load_config", lambda: config or _fake_config())
    monkeypatch.setattr(
        handler_module, "build_real_provider_bundle", lambda now, cfg: SimpleNamespace()
    )
    monkeypatch.setattr(handler_module, "build_candidate_universe_provider", lambda cfg: object())
    monkeypatch.setattr(handler_module, "build_line_client_from_env", lambda: object())


# --- ディスパッチ分岐: enabled/weekly_schedule_enabled ------------------------


def test_dispatch_skips_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch, _fake_config(enabled=False))
    dispatched: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handler_module,
        "dispatch_async",
        lambda function_name, payload: dispatched.append(payload),
    )

    result = handler_module.handler({}, _FakeContext())

    assert result == {"skipped": True}
    assert dispatched == []


def test_dispatch_skips_when_weekly_schedule_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch, _fake_config(weekly_schedule_enabled=False))
    dispatched: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handler_module,
        "dispatch_async",
        lambda function_name, payload: dispatched.append(payload),
    )

    result = handler_module.handler({}, _FakeContext())

    assert result == {"skipped": True}
    assert dispatched == []


# --- ディスパッチ分岐: 通常フロー ---------------------------------------------


class _FakeCollector:
    def __init__(self, result: CollectorResult) -> None:
        self._result = result

    def collect_target_codes(self) -> CollectorResult:
        return self._result


def test_dispatch_sends_one_call_per_candidate_with_shared_batch_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    collector_result = CollectorResult(
        stock_codes=["1111", "2222"],
        universe_count=5,
        duplicate_count=1,
        invalid_code_count=0,
        holding_excluded_count=1,
        watchlist_excluded_count=1,
    )
    monkeypatch.setattr(
        handler_module,
        "WatchlistCandidateCollector",
        lambda *a, **kw: _FakeCollector(collector_result),
    )
    started_batches: list[tuple[str, int]] = []
    monkeypatch.setattr(
        handler_module,
        "start_batch",
        lambda batch_id, total, now: started_batches.append((batch_id, total)),
    )
    dispatched: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handler_module,
        "dispatch_async",
        lambda function_name, payload: dispatched.append(payload),
    )

    result = handler_module.handler({}, _FakeContext())

    assert result == {"dispatched": 2}
    assert len(dispatched) == 2
    assert {d["stock_code"] for d in dispatched} == {"1111", "2222"}
    batch_ids = {d["batch_id"] for d in dispatched}
    assert len(batch_ids) == 1
    assert started_batches == [(next(iter(batch_ids)), 2)]
    assert all(d["task"] == "screen_candidate" for d in dispatched)
    assert all("started_at" in d for d in dispatched)


def test_dispatch_aborts_when_evaluation_target_count_exceeds_max_ranking_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """評価対象銘柄数がMAX_RANKING_ENTRIESを超える場合、dispatch前にバッチを中止し、
    ranking_entries(DynamoDB文字列セット)の書き込み上限超過を未然に防ぐ(レビュー対応)。
    """
    _patch_common(monkeypatch)
    too_many_codes = [str(1000 + i) for i in range(handler_module.MAX_RANKING_ENTRIES + 1)]
    collector_result = CollectorResult(
        stock_codes=too_many_codes,
        universe_count=len(too_many_codes),
        duplicate_count=0,
        invalid_code_count=0,
        holding_excluded_count=0,
        watchlist_excluded_count=0,
    )
    monkeypatch.setattr(
        handler_module,
        "WatchlistCandidateCollector",
        lambda *a, **kw: _FakeCollector(collector_result),
    )
    monkeypatch.setattr(
        handler_module,
        "start_batch",
        lambda *a, **kw: pytest.fail("start_batch should not be called"),
    )
    monkeypatch.setattr(
        handler_module, "dispatch_async", lambda *a, **kw: pytest.fail("should not dispatch")
    )
    audit_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handler_module,
        "record_batch_audit",
        lambda **kwargs: audit_calls.append(kwargs),
    )

    result = handler_module.handler({}, _FakeContext())

    assert result == {"error": "ranking_capacity_exceeded"}
    assert audit_calls[0]["output_values"]["execution_result"] == "ranking_capacity_exceeded"
    assert audit_calls[0]["output_values"]["evaluation_target_count"] == len(too_many_codes)


def test_dispatch_allows_evaluation_target_count_exactly_at_max_ranking_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    codes = [str(1000 + i) for i in range(handler_module.MAX_RANKING_ENTRIES)]
    collector_result = CollectorResult(
        stock_codes=codes,
        universe_count=len(codes),
        duplicate_count=0,
        invalid_code_count=0,
        holding_excluded_count=0,
        watchlist_excluded_count=0,
    )
    monkeypatch.setattr(
        handler_module,
        "WatchlistCandidateCollector",
        lambda *a, **kw: _FakeCollector(collector_result),
    )
    monkeypatch.setattr(handler_module, "start_batch", lambda *a, **kw: None)
    dispatched: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handler_module,
        "dispatch_async",
        lambda function_name, payload: dispatched.append(payload),
    )

    result = handler_module.handler({}, _FakeContext())

    assert result == {"dispatched": len(codes)}
    assert len(dispatched) == len(codes)


def test_dispatch_with_zero_candidates_records_audit_without_starting_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    collector_result = CollectorResult(
        stock_codes=[],
        universe_count=3,
        duplicate_count=0,
        invalid_code_count=0,
        holding_excluded_count=1,
        watchlist_excluded_count=2,
    )
    monkeypatch.setattr(
        handler_module,
        "WatchlistCandidateCollector",
        lambda *a, **kw: _FakeCollector(collector_result),
    )
    monkeypatch.setattr(
        handler_module,
        "start_batch",
        lambda *a, **kw: pytest.fail("start_batch should not be called"),
    )
    monkeypatch.setattr(
        handler_module, "dispatch_async", lambda *a, **kw: pytest.fail("should not dispatch")
    )
    audit_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handler_module,
        "record_batch_audit",
        lambda **kwargs: audit_calls.append(kwargs),
    )

    result = handler_module.handler({}, _FakeContext())

    assert result == {"dispatched": 0}
    assert len(audit_calls) == 1
    assert audit_calls[0]["output_values"]["universe_count"] == 3
    assert audit_calls[0]["output_values"]["actual_added_count"] == 0
    assert audit_calls[0]["output_values"]["notification_sent"] is False


def test_dispatch_handles_candidate_universe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)

    class _RaisingCollector:
        def collect_target_codes(self) -> CollectorResult:
            raise CandidateUniverseError("CSVが見つかりません")

    monkeypatch.setattr(
        handler_module, "WatchlistCandidateCollector", lambda *a, **kw: _RaisingCollector()
    )
    audit_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handler_module, "record_batch_audit", lambda **kwargs: audit_calls.append(kwargs)
    )

    result = handler_module.handler({}, _FakeContext())

    assert result == {"error": "universe_load_failed"}
    assert audit_calls[0]["output_values"] == {"execution_result": "universe_load_failed"}


# --- ワーカー分岐: カテゴリ分類とrecord_result --------------------------------


class _FakeScreeningDataProvider:
    def __init__(self, result: ScreeningDataResult) -> None:
        self._result = result

    def get_screening_input(self, stock_code: str, now: dt.datetime) -> ScreeningDataResult:
        return self._result


def _watchlist_input(**overrides: object) -> WatchlistScreeningInput:
    from decimal import Decimal

    defaults: dict[str, object] = dict(
        stock_code="1234",
        stock_name="テスト",
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


class _FakeWatchlistScreeningPolicyService:
    """WatchlistScreeningServiceの代わりに、あらかじめ用意したWatchlistScreeningResultを返す。"""

    def __init__(self, result: WatchlistScreeningResult) -> None:
        self._result = result

    def evaluate(self, stock_code, stock_name, input, now):  # noqa: A002
        return self._result

    def to_ranking_entry(self, result):
        from jstock_advisor.domain.signals.watchlist_screening import RankingEntry

        return RankingEntry(
            stock_code=result.stock_code,
            total_score=result.total_score,
            policy_scores={},
            matched_criteria=result.matched_criteria,
            main_metrics=result.main_metrics,
        )


def _screening_result(passed: bool, exclusion_reasons=None) -> WatchlistScreeningResult:
    return WatchlistScreeningResult(
        stock_code="1234",
        stock_name="テスト",
        passed=passed,
        policy_results=[],
        total_score=80.0 if passed else 30.0,
        matched_criteria=[MatchedCriterion.HIGH_DIVIDEND_YIELD] if passed else [],
        exclusion_reasons=exclusion_reasons or [],
        missing_required_fields=[],
        missing_scoring_fields=[],
        evaluated_at=_NOW,
        main_metrics={},
    )


def test_process_single_candidate_data_not_found_records_data_insufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handler_module,
        "StockSnapshotScreeningDataProvider",
        lambda providers, config: _FakeScreeningDataProvider(
            ScreeningDataResult(
                status=ScreeningDataStatus.NOT_FOUND,
                input=None,
                missing_fields=[],
                error_message="x",
            )
        ),
    )
    audit_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handler_module,
        "record_candidate_audit",
        lambda *a, **kw: audit_calls.append({"args": a, "kwargs": kw}),
    )
    record_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda *a, **kw: record_calls.append({"args": a, "kwargs": kw}) or None,
    )

    category, _progress = handler_module._process_single_candidate(
        "1234", "batch-1", _NOW, SimpleNamespace(), _fake_config()
    )

    assert category == "data_insufficient"
    assert audit_calls[0]["args"][1] is None
    assert audit_calls[0]["args"][2] == "DATA_INSUFFICIENT"
    assert audit_calls[0]["kwargs"]["batch_id"] == "batch-1"
    assert record_calls[0]["args"][:2] == ("batch-1", "data_insufficient")


def test_process_single_candidate_passed_includes_ranking_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dto = _watchlist_input()
    monkeypatch.setattr(
        handler_module,
        "StockSnapshotScreeningDataProvider",
        lambda providers, config: _FakeScreeningDataProvider(
            ScreeningDataResult(
                status=ScreeningDataStatus.OK,
                input=input_dto,
                missing_fields=[],
                error_message=None,
            )
        ),
    )
    monkeypatch.setattr(
        handler_module,
        "WatchlistScreeningService",
        lambda config: _FakeWatchlistScreeningPolicyService(_screening_result(passed=True)),
    )
    monkeypatch.setattr(handler_module, "record_candidate_audit", lambda *a, **kw: None)
    record_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda *a, **kw: record_calls.append({"args": a, "kwargs": kw}) or None,
    )

    category, _progress = handler_module._process_single_candidate(
        "1234", "batch-1", _NOW, SimpleNamespace(), _fake_config()
    )

    assert category == "passed"
    assert record_calls[0]["kwargs"]["ranking_entry"] is not None


class _FakeWatchlistScreeningPolicyServiceUnrankable(_FakeWatchlistScreeningPolicyService):
    """to_ranking_entry()がNoneを返すケース(MAX_RANKING_ENTRY_BYTES超過)を模擬する。"""

    def to_ranking_entry(self, result):
        return None


def test_process_single_candidate_passed_but_unrankable_is_recorded_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RankingEntryが上限を超えて構築できない場合、"passed"ではなく"failed"として
    記録され、ranking_entryは渡されない(レビュー対応)。
    """
    input_dto = _watchlist_input()
    monkeypatch.setattr(
        handler_module,
        "StockSnapshotScreeningDataProvider",
        lambda providers, config: _FakeScreeningDataProvider(
            ScreeningDataResult(
                status=ScreeningDataStatus.OK,
                input=input_dto,
                missing_fields=[],
                error_message=None,
            )
        ),
    )
    monkeypatch.setattr(
        handler_module,
        "WatchlistScreeningService",
        lambda config: _FakeWatchlistScreeningPolicyServiceUnrankable(
            _screening_result(passed=True)
        ),
    )
    audit_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handler_module,
        "record_candidate_audit",
        lambda *a, **kw: audit_calls.append({"args": a, "kwargs": kw}),
    )
    record_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda *a, **kw: record_calls.append({"args": a, "kwargs": kw}) or None,
    )

    category, _progress = handler_module._process_single_candidate(
        "1234", "batch-1", _NOW, SimpleNamespace(), _fake_config()
    )

    assert category == "failed"
    assert record_calls[0]["args"] == ("batch-1", "failed")
    assert record_calls[0]["kwargs"]["ranking_entry"] is None
    assert audit_calls[0]["args"][2] == "PASSED_RANKING_ENTRY_TOO_LARGE"


def test_process_single_candidate_required_condition_failed_no_ranking_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dto = _watchlist_input()
    monkeypatch.setattr(
        handler_module,
        "StockSnapshotScreeningDataProvider",
        lambda providers, config: _FakeScreeningDataProvider(
            ScreeningDataResult(
                status=ScreeningDataStatus.OK,
                input=input_dto,
                missing_fields=[],
                error_message=None,
            )
        ),
    )
    monkeypatch.setattr(
        handler_module,
        "WatchlistScreeningService",
        lambda config: _FakeWatchlistScreeningPolicyService(
            _screening_result(passed=False, exclusion_reasons=[ExclusionReason.DEBT_EXCESS])
        ),
    )
    monkeypatch.setattr(handler_module, "record_candidate_audit", lambda *a, **kw: None)
    record_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda *a, **kw: record_calls.append({"args": a, "kwargs": kw}) or None,
    )

    category, _progress = handler_module._process_single_candidate(
        "1234", "batch-1", _NOW, SimpleNamespace(), _fake_config()
    )

    assert category == "required_condition_failed"
    assert record_calls[0]["kwargs"]["ranking_entry"] is None


# --- ワーカー分岐: finalize呼び出し制御 ---------------------------------------


def _worker_event(stock_code: str = "1234") -> dict[str, Any]:
    return {
        "task": "screen_candidate",
        "stock_code": stock_code,
        "batch_id": "batch-1",
        "started_at": _NOW.isoformat(),
    }


def _progress(completed: int, total: int) -> BatchProgress:
    return BatchProgress(
        total=total,
        completed=completed,
        category_counts={},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[],
        sector_entries=[],
        holding_count=0,
    )


def test_worker_invokes_finalize_when_complete_and_finalize_lock_acquired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        handler_module,
        "_process_single_candidate",
        lambda *a, **kw: ("passed", _progress(3, 3)),
    )
    monkeypatch.setattr(handler_module, "try_acquire_finalize", lambda batch_id: True)
    finalize_calls: list[str] = []
    monkeypatch.setattr(
        handler_module, "_finalize", lambda *a, **kw: finalize_calls.append(a[1])
    )
    mark_complete_calls: list[str] = []
    monkeypatch.setattr(
        handler_module,
        "mark_finalize_complete",
        lambda batch_id: mark_complete_calls.append(batch_id),
    )

    handler_module.handler(_worker_event(), _FakeContext())

    assert finalize_calls == ["batch-1"]
    assert mark_complete_calls == ["batch-1"]


def test_worker_marks_finalize_failed_and_reraises_when_finalize_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """finalize処理が例外を送出した場合、mark_finalize_completeではなく
    mark_finalize_failedが呼ばれ、バッチはCOMPLETEDへ遷移しない(レビュー対応)。
    """
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        handler_module,
        "_process_single_candidate",
        lambda *a, **kw: ("passed", _progress(3, 3)),
    )
    monkeypatch.setattr(handler_module, "try_acquire_finalize", lambda batch_id: True)

    def _raise_finalize(*a, **kw):
        raise RuntimeError("finalize boom")

    monkeypatch.setattr(handler_module, "_finalize", _raise_finalize)
    mark_complete_calls: list[str] = []
    monkeypatch.setattr(
        handler_module,
        "mark_finalize_complete",
        lambda batch_id: mark_complete_calls.append(batch_id),
    )
    mark_failed_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        handler_module,
        "mark_finalize_failed",
        lambda batch_id, error_message: mark_failed_calls.append((batch_id, error_message)),
    )

    with pytest.raises(RuntimeError, match="finalize boom"):
        handler_module.handler(_worker_event(), _FakeContext())

    assert mark_complete_calls == []
    assert mark_failed_calls == [("batch-1", "finalize boom")]


def test_worker_does_not_finalize_when_lock_not_acquired(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        handler_module,
        "_process_single_candidate",
        lambda *a, **kw: ("passed", _progress(3, 3)),
    )
    monkeypatch.setattr(handler_module, "try_acquire_finalize", lambda batch_id: False)
    finalize_calls: list[str] = []
    monkeypatch.setattr(
        handler_module, "_finalize", lambda *a, **kw: finalize_calls.append("called")
    )
    monkeypatch.setattr(
        handler_module,
        "mark_finalize_complete",
        lambda batch_id: pytest.fail("mark_finalize_complete should not be called"),
    )

    handler_module.handler(_worker_event(), _FakeContext())

    assert finalize_calls == []


def test_worker_does_not_finalize_when_batch_not_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        handler_module,
        "_process_single_candidate",
        lambda *a, **kw: ("passed", _progress(1, 3)),
    )
    monkeypatch.setattr(
        handler_module,
        "try_acquire_finalize",
        lambda batch_id: pytest.fail("try_acquire_finalize should not be called"),
    )
    finalize_calls: list[str] = []
    monkeypatch.setattr(
        handler_module, "_finalize", lambda *a, **kw: finalize_calls.append("called")
    )

    handler_module.handler(_worker_event(), _FakeContext())

    assert finalize_calls == []


def test_worker_records_failed_category_and_continues_on_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common(monkeypatch)

    def _raise(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(handler_module, "_process_single_candidate", _raise)
    record_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handler_module,
        "record_result",
        lambda *a, **kw: record_calls.append({"args": a, "kwargs": kw}) or _progress(1, 3),
    )
    monkeypatch.setattr(
        handler_module,
        "try_acquire_finalize",
        lambda batch_id: pytest.fail("should not reach finalize check"),
    )

    result = handler_module.handler(_worker_event(), _FakeContext())

    assert result == {"stock_code": "1234", "category": "failed"}
    assert record_calls[0]["args"] == ("batch-1", "failed")
    assert record_calls[0]["kwargs"] == {"stock_code": "1234"}


# --- _finalize本体の配線 ------------------------------------------------------


class _FakeWatchlistRepository:
    def __init__(self, existing_codes: set[str] | None = None) -> None:
        self.existing_codes = existing_codes or set()
        self.added_items: list[Any] = []

    def add_if_new(self, item: Any) -> bool:
        if item.stock_code in self.existing_codes:
            return False
        self.added_items.append(item)
        return True


class _FakeNotificationService:
    def __init__(self, sent: bool = True) -> None:
        self._sent = sent
        self.calls: list[dict[str, Any]] = []

    def notify_watchlist_additions(self, added_items, results_by_code, policy_name, now):
        self.calls.append(
            {
                "added_items": added_items,
                "results_by_code": results_by_code,
                "policy_name": policy_name,
            }
        )
        return self._sent


def _ranking_entry_json(stock_code: str, score: float) -> str:
    return RankingEntry(
        stock_code=stock_code,
        total_score=score,
        policy_scores={"high_dividend_financial_health": score},
        matched_criteria=[MatchedCriterion.HIGH_DIVIDEND_YIELD],
        main_metrics={"配当利回り": "4.2%"},
    ).model_dump_json()


def test_finalize_adds_ranked_entries_and_sends_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(handler_module, "WatchlistRepository", lambda: fake_repo)
    monkeypatch.setattr(handler_module, "_fetch_stock_name", lambda providers, code: f"銘柄{code}")
    audit_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handler_module, "record_batch_audit", lambda **kwargs: audit_calls.append(kwargs)
    )
    repo_result_calls: list[tuple] = []
    monkeypatch.setattr(
        handler_module,
        "record_repository_result_audit",
        lambda *a, **kw: repo_result_calls.append(a),
    )
    notification_service = _FakeNotificationService(sent=True)

    progress = BatchProgress(
        total=2,
        completed=2,
        category_counts={
            "data_insufficient": 0,
            "required_condition_failed": 0,
            "score_failed": 0,
            "passed": 2,
        },
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[_ranking_entry_json("1111", 60.0), _ranking_entry_json("2222", 90.0)],
        sector_entries=[],
        holding_count=0,
    )

    handler_module._finalize(
        progress,
        "batch-1",
        _NOW,
        _NOW + dt.timedelta(minutes=5),
        SimpleNamespace(),
        _fake_config(),
        notification_service,
    )

    assert [item.stock_code for item in fake_repo.added_items] == ["2222", "1111"]
    assert all(
        item.registration_source.value == "AUTO_SCREENING" for item in fake_repo.added_items
    )
    assert len(notification_service.calls) == 1
    assert {i.stock_code for i in notification_service.calls[0]["added_items"]} == {"1111", "2222"}
    assert audit_calls[0]["output_values"]["actual_added_count"] == 2
    assert audit_calls[0]["output_values"]["passed_count"] == 2
    assert len(repo_result_calls) == 2
    assert {call[1] for call in repo_result_calls} == {"1111", "2222"}
    assert all(call[5] == "added" for call in repo_result_calls)


def test_finalize_excludes_concurrently_registered_stock_from_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_repo = _FakeWatchlistRepository(existing_codes={"1111"})
    monkeypatch.setattr(handler_module, "WatchlistRepository", lambda: fake_repo)
    monkeypatch.setattr(handler_module, "_fetch_stock_name", lambda providers, code: None)
    monkeypatch.setattr(handler_module, "record_batch_audit", lambda **kwargs: None)
    repo_result_calls: list[tuple] = []
    monkeypatch.setattr(
        handler_module,
        "record_repository_result_audit",
        lambda *a, **kw: repo_result_calls.append(a),
    )
    notification_service = _FakeNotificationService(sent=True)

    progress = BatchProgress(
        total=1,
        completed=1,
        category_counts={
            "data_insufficient": 0,
            "required_condition_failed": 0,
            "score_failed": 0,
            "passed": 1,
        },
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[_ranking_entry_json("1111", 60.0)],
        sector_entries=[],
        holding_count=0,
    )

    handler_module._finalize(
        progress, "batch-1", _NOW, _NOW, SimpleNamespace(), _fake_config(), notification_service
    )

    assert fake_repo.added_items == []
    assert notification_service.calls == []
    assert len(repo_result_calls) == 1
    assert repo_result_calls[0][5] == "skipped_existing"
    assert repo_result_calls[0][6] is False


def test_finalize_applies_addition_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(handler_module, "WatchlistRepository", lambda: fake_repo)
    monkeypatch.setattr(handler_module, "_fetch_stock_name", lambda providers, code: None)
    audit_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        handler_module, "record_batch_audit", lambda **kwargs: audit_calls.append(kwargs)
    )
    repo_result_calls: list[tuple] = []
    monkeypatch.setattr(
        handler_module,
        "record_repository_result_audit",
        lambda *a, **kw: repo_result_calls.append(a),
    )
    notification_service = _FakeNotificationService(sent=True)

    config = _fake_config()
    config.watchlist_screening.max_watchlist_additions_per_run = 1
    progress = BatchProgress(
        total=2,
        completed=2,
        category_counts={
            "data_insufficient": 0,
            "required_condition_failed": 0,
            "score_failed": 0,
            "passed": 2,
        },
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[_ranking_entry_json("1111", 60.0), _ranking_entry_json("2222", 90.0)],
        sector_entries=[],
        holding_count=0,
    )

    handler_module._finalize(
        progress, "batch-1", _NOW, _NOW, SimpleNamespace(), config, notification_service
    )

    assert [item.stock_code for item in fake_repo.added_items] == ["2222"]
    assert audit_calls[0]["output_values"]["addition_candidate_count"] == 1
    assert audit_calls[0]["output_values"]["actual_added_count"] == 1
    # 上限内(2222)はadded、上限外(1111)はskipped_over_limitとして記録される
    assert len(repo_result_calls) == 2
    by_stock_code = {call[1]: call for call in repo_result_calls}
    assert by_stock_code["2222"][5] == "added"
    assert by_stock_code["1111"][5] == "skipped_over_limit"
    assert by_stock_code["1111"][3] == 2  # rank(上限適用前の全合格ランキングでの順位)


class _FailingWatchlistRepository:
    def add_if_new(self, item: Any) -> bool:
        raise RuntimeError("dynamodb unavailable")


def test_finalize_records_repository_failed_audit_on_add_if_new_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handler_module, "WatchlistRepository", lambda: _FailingWatchlistRepository()
    )
    monkeypatch.setattr(handler_module, "_fetch_stock_name", lambda providers, code: None)
    monkeypatch.setattr(handler_module, "record_batch_audit", lambda **kwargs: None)
    repo_result_calls: list[tuple] = []
    monkeypatch.setattr(
        handler_module,
        "record_repository_result_audit",
        lambda *a, **kw: repo_result_calls.append((a, kw)),
    )
    notification_service = _FakeNotificationService(sent=True)

    progress = BatchProgress(
        total=1,
        completed=1,
        category_counts={
            "data_insufficient": 0,
            "required_condition_failed": 0,
            "score_failed": 0,
            "passed": 1,
        },
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[_ranking_entry_json("1111", 60.0)],
        sector_entries=[],
        holding_count=0,
    )

    handler_module._finalize(
        progress, "batch-1", _NOW, _NOW, SimpleNamespace(), _fake_config(), notification_service
    )

    assert len(repo_result_calls) == 1
    args, kwargs = repo_result_calls[0]
    assert args[5] == "repository_failed"
    assert args[6] is False
    assert isinstance(kwargs["error"], RuntimeError)
    assert notification_service.calls == []


def test_finalize_does_not_notify_when_no_candidates_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_repo = _FakeWatchlistRepository()
    monkeypatch.setattr(handler_module, "WatchlistRepository", lambda: fake_repo)
    monkeypatch.setattr(handler_module, "record_batch_audit", lambda **kwargs: None)
    monkeypatch.setattr(handler_module, "record_repository_result_audit", lambda *a, **kw: None)
    notification_service = _FakeNotificationService(sent=True)

    progress = BatchProgress(
        total=1,
        completed=1,
        category_counts={
            "data_insufficient": 1,
            "required_condition_failed": 0,
            "score_failed": 0,
            "passed": 0,
        },
        data_insufficient_stock_codes=["1111"],
        failed_stock_codes=[],
        ranking_entries=[],
        sector_entries=[],
        holding_count=0,
    )

    handler_module._finalize(
        progress, "batch-1", _NOW, _NOW, SimpleNamespace(), _fake_config(), notification_service
    )

    assert fake_repo.added_items == []
    assert notification_service.calls == []
