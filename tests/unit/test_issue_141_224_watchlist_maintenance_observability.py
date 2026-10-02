"""Issue #141(NOT_EVALUABLEの削除カウンタ分離)・#224 O-1(watchlist maintenance
finalizeのbatch auditへ母数・条件充足内訳を追加)の、finalizer側(`_finalize_
maintenance_completed()`)統合テスト。

`evaluate_maintenance_decision()`自体の単体テストはtest_watchlist_maintenance_
service.pyが担う。本ファイルは、finalizerがその結果を正しく集計し、
`record_batch_audit()`のoutput_valuesへ反映することを確認する。

`query_all_candidate_progress()`を直接monkeypatchし、`CandidateProgressRecord`を
手組みすることで、moto DynamoDBを使わずに軽量にテストする(`WatchlistRepository`/
`WatchlistRemovalHistoryRepository`も同様に直接monkeypatchする)。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from jstock_advisor.domain.entities.enums import WatchlistRegistrationSource
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.domain.signals.watchlist_screening import HardExclusionCode
from jstock_advisor.infrastructure.aws.batch_tracker import CandidateProgressRecord
from jstock_advisor.services import watchlist_batch_finalizer as finalizer_module
from jstock_advisor.services.watchlist_maintenance_service import MaintenanceScreeningSummary

_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)

_CONFIG = type(
    "_Config",
    (),
    {
        "watchlist_screening": type(
            "_WatchlistScreeningConfig",
            (),
            {
                "screening_policy": "multi_style_monitoring",
                "auto_removal": type(
                    "_AutoRemovalConfig",
                    (),
                    {
                        "enabled": True,
                        "minimum_age_days": 90,
                        "consecutive_not_qualified_required": 3,
                        "minimum_not_qualified_span_days": 28,
                        "stale_recheck_days": 30,
                        "maximum_unconfirmed_days": 180,
                        "readd_cooldown_days": 30,
                    },
                )(),
            },
        )(),
    },
)()


class _FakeWatchlistRemovalHistoryRepository:
    def get(self, stock_code: str) -> Any | None:
        return None

    def upsert(self, item: Any) -> None:
        pass


class _FakeWatchlistRepository:
    def __init__(self, items: list[WatchlistItem]) -> None:
        self._items: dict[str, WatchlistItem] = {item.stock_code: item for item in items}

    def get(self, stock_code: str) -> WatchlistItem | None:
        return self._items.get(stock_code)

    def upsert(self, item: WatchlistItem) -> None:
        self._items[item.stock_code] = item

    def delete(self, stock_code: str) -> bool:
        return self._items.pop(stock_code, None) is not None

    def iter_all(self) -> Any:
        return iter(self._items.values())


def _item(
    stock_code: str,
    *,
    created_at: dt.datetime,
    consecutive_not_qualified_count: int = 0,
    removal_candidate_since: dt.datetime | None = None,
) -> WatchlistItem:
    return WatchlistItem(
        stock_code=stock_code,
        stock_name=f"テスト銘柄{stock_code}",
        reason="自動追加",
        registration_source=WatchlistRegistrationSource.AUTO_SCREENING,
        registration_policy="multi_style_monitoring",
        created_at=created_at,
        updated_at=created_at,
        consecutive_not_qualified_count=consecutive_not_qualified_count,
        removal_candidate_since=removal_candidate_since,
    )


def _record(
    stock_code: str, summary: MaintenanceScreeningSummary | None
) -> CandidateProgressRecord:
    return CandidateProgressRecord(
        batch_id="watchlist-maint-1",
        stock_code=stock_code,
        status="COMPLETED",
        dispatched=True,
        evaluation_result="PASSED" if summary is not None and summary.passed else "FAILED_SCORE",
        ranking_entry=None,
        lease_owner_id=None,
        attempt_count=1,
        total_processing_duration_ms=100,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        total_score=summary.total_score if summary is not None else None,
        notification_detail=None,
        screening_summary_json=summary.model_dump_json() if summary is not None else None,
    )


def _run_finalize(
    monkeypatch: Any, items: list[WatchlistItem], records: list[CandidateProgressRecord]
) -> dict[str, Any]:
    audits: list[dict[str, Any]] = []
    monkeypatch.setattr(
        finalizer_module, "query_all_candidate_progress", lambda *a, **k: records
    )
    monkeypatch.setattr(
        finalizer_module, "WatchlistRepository", lambda: _FakeWatchlistRepository(items)
    )
    monkeypatch.setattr(
        finalizer_module,
        "WatchlistRemovalHistoryRepository",
        lambda *a, **k: _FakeWatchlistRemovalHistoryRepository(),
    )
    monkeypatch.setattr(
        finalizer_module, "get_watchlist_batch", lambda _b: {"batch_id": "watchlist-maint-1"}
    )
    monkeypatch.setattr(finalizer_module, "record_batch_audit", lambda **kw: audits.append(kw))
    monkeypatch.setattr(finalizer_module, "mark_watchlist_batch_completed", lambda *a, **k: True)

    finalizer_module._finalize_maintenance_completed("watchlist-maint-1", _NOW, _CONFIG)

    assert len(audits) == 1
    return audits[0]["output_values"]


# --- Issue #141: NOT_EVALUABLEのoutcome集計・stale_not_evaluable_count ------------


def test_not_evaluable_outcome_is_counted_separately(monkeypatch: Any) -> None:
    item = _item("1111", created_at=_NOW - dt.timedelta(days=120))
    summary = MaintenanceScreeningSummary(
        passed=False,
        total_score=0.0,
        hard_exclusion_codes=[HardExclusionCode.UNSUPPORTED_INDUSTRY],
        policy_name="multi_style_monitoring",
    )
    output_values = _run_finalize(monkeypatch, [item], [_record("1111", summary)])

    assert output_values["outcome_counts"] == {"NOT_EVALUABLE": 1}
    assert output_values["stale_not_evaluable_count"] == 0


def test_stale_not_evaluable_count_is_aggregated(monkeypatch: Any) -> None:
    far_past = _NOW - dt.timedelta(days=200)
    item = _item("1111", created_at=far_past)
    summary = MaintenanceScreeningSummary(
        passed=False,
        total_score=0.0,
        data_insufficient=True,
        policy_name="multi_style_monitoring",
    )
    output_values = _run_finalize(monkeypatch, [item], [_record("1111", summary)])

    assert output_values["stale_not_evaluable_count"] == 1


# --- Issue #224(O-1): 母数・条件充足内訳 ------------------------------------------


def test_watchlist_total_count_includes_non_auto_screening_items(monkeypatch: Any) -> None:
    """母数(watchlist_total_count)は全registration_source(MANUAL含む)。
    auto_screening_countはAUTO_SCREENING(= 今回処理したrecords件数)のみ。"""
    auto_item = _item("1111", created_at=_NOW - dt.timedelta(days=120))
    manual_item = WatchlistItem(
        stock_code="2222",
        stock_name="手動銘柄",
        reason="手動登録",
        registration_source=WatchlistRegistrationSource.MANUAL,
        created_at=_NOW,
        updated_at=_NOW,
    )
    summary = MaintenanceScreeningSummary(passed=True, total_score=80.0, policy_name="p")
    output_values = _run_finalize(
        monkeypatch, [auto_item, manual_item], [_record("1111", summary)]
    )

    assert output_values["watchlist_total_count"] == 2
    assert output_values["auto_screening_count"] == 1


def test_blocked_by_minimum_age_count_reflects_age_gate(monkeypatch: Any) -> None:
    """年齢条件未達のみの銘柄は、blocked_by_minimum_age_countへ計上され、
    件数条件・期間条件は満たしている(blocked_by_count_condition/
    blocked_by_span_conditionは0)。"""
    item = _item(
        "1111",
        created_at=_NOW - dt.timedelta(days=30),  # minimum_age_days(90)未達
        consecutive_not_qualified_count=2,
        removal_candidate_since=_NOW - dt.timedelta(days=30),
    )
    summary = MaintenanceScreeningSummary(
        passed=False,
        total_score=0.0,
        hard_exclusion_reasons=["開示情報にリスクキーワードを検出しました"],
        hard_exclusion_codes=[HardExclusionCode.DISCLOSURE_RISK],
        policy_name="multi_style_monitoring",
    )
    output_values = _run_finalize(monkeypatch, [item], [_record("1111", summary)])

    assert output_values["blocked_by_minimum_age_count"] == 1
    assert output_values["blocked_by_count_condition"] == 0
    assert output_values["blocked_by_span_condition"] == 0
    assert output_values["eligible_for_removal_count"] == 0
    assert output_values["removed_count"] == 0


def test_eligible_for_removal_and_removed_count_match_outcome_counts(monkeypatch: Any) -> None:
    """3条件すべて満たす銘柄は、eligible_for_removal_count・removed_countへ
    計上され、outcome_counts["CONSECUTIVE_NOT_QUALIFIED_REMOVAL"]と一致する。"""
    item = _item(
        "1111",
        created_at=_NOW - dt.timedelta(days=120),
        consecutive_not_qualified_count=2,
        removal_candidate_since=_NOW - dt.timedelta(days=30),
    )
    summary = MaintenanceScreeningSummary(
        passed=False,
        total_score=0.0,
        hard_exclusion_reasons=["開示情報にリスクキーワードを検出しました"],
        hard_exclusion_codes=[HardExclusionCode.DISCLOSURE_RISK],
        policy_name="multi_style_monitoring",
    )
    output_values = _run_finalize(monkeypatch, [item], [_record("1111", summary)])

    assert output_values["outcome_counts"] == {"CONSECUTIVE_NOT_QUALIFIED_REMOVAL": 1}
    assert output_values["eligible_for_removal_count"] == 1
    assert output_values["removed_count"] == 1
    assert output_values["blocked_by_minimum_age_count"] == 0
    assert output_values["blocked_by_count_condition"] == 0
    assert output_values["blocked_by_span_condition"] == 0


def test_zero_blocked_counts_when_no_route_b_candidates(monkeypatch: Any) -> None:
    """全銘柄PASSEDの通常回では、blocked_by_*・eligible_for_removal_count・
    removed_countはいずれも0(誤発火しないことの回帰確認)。"""
    item = _item("1111", created_at=_NOW - dt.timedelta(days=120))
    summary = MaintenanceScreeningSummary(passed=True, total_score=80.0, policy_name="p")
    output_values = _run_finalize(monkeypatch, [item], [_record("1111", summary)])

    assert output_values["blocked_by_minimum_age_count"] == 0
    assert output_values["blocked_by_count_condition"] == 0
    assert output_values["blocked_by_span_condition"] == 0
    assert output_values["eligible_for_removal_count"] == 0
    assert output_values["removed_count"] == 0
