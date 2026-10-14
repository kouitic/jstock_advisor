"""購入側 Shadow の観測の集計(#934 BT-A PR-1)の契約テスト。

合成の記録(#605 の契約テストと同じ形・実際の組み立てが作った記録)だけで、実データなしに全指標を
検証する。規則(数える日・欠落件数・版の分離・母数と『測れない』・公開面の秘密)を固定する。
"""

from __future__ import annotations

import ast
import copy
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.cli import allocation as cli
from jstock_advisor.domain.notification.allocation_shadow_cli_view import (
    AllocationShadowScreen,
    DayInput,
    DayState,
    RecordInput,
    RecordState,
    build_screen,
)
from jstock_advisor.domain.notification.allocation_shadow_summary import (
    COUNTING_RULE_SOURCE,
    NOT_MEASURABLE,
    Classification,
    DayRecord,
    Measure,
    NotCounted,
    OwnerKind,
    build_ten_day_report,
    classify_day,
    owner_kind,
    rate,
    report_to_dict,
    summarize,
)
from jstock_advisor.infrastructure.aws import allocation_shadow_reader as reader
from jstock_advisor.infrastructure.aws.allocation_shadow_reader import (
    ReadFailure,
    ReadFailureKind,
    ReadStatus,
    RecordRead,
    read_day,
    read_days,
)
from tests.unit import test_allocation_shadow_cli_view_contract as view_tests
from tests.unit import test_allocation_shadow_compute_contract as compute_tests

_REPO = Path(__file__).resolve().parents[2]
_SUMMARY_PATH = (
    _REPO / "src" / "jstock_advisor" / "domain" / "notification" / "allocation_shadow_summary.py"
)
_REF_A = compute_tests._ref("owner-a")
_REF_B = compute_tests._ref("owner-b")
MISSING = RecordInput(RecordState.NOT_FOUND)


def _found(record: dict[str, Any]) -> RecordInput:
    return RecordInput(RecordState.FOUND, record)


# --- 合成の日(owner の記録は #605 の契約テストの最小の記録を使う)------------------------


def _owner(**run: Any) -> dict[str, Any]:
    record = view_tests._owner_record()
    record["run"].update(run)
    return record


def _skipped_owner() -> dict[str, Any]:
    record = _owner(outcome="SKIPPED", status=None, skip_reason="TIME_BUDGET")
    record["candidates"] = []
    return record


def _failed_owner() -> dict[str, Any]:
    record = _owner(outcome="FAILED", status=None, skip_reason="COMPUTATION_FAILED")
    record["candidates"] = []
    return record


def _day(
    date: str,
    owners: dict[str, dict[str, Any] | None] | None = None,
    *,
    index: dict[str, Any] | None = None,
    skip: dict[str, Any] | None = None,
) -> DayInput:
    owners = {_REF_A: view_tests._owner_record()} if owners is None else owners
    refs = list(owners)
    idx = index if index is not None else view_tests._index(refs)
    items = {ref: _found(rec) if rec is not None else MISSING for ref, rec in owners.items()}
    return DayInput(date, _found(idx), _found(skip) if skip else MISSING, items)


def _screen(date: str = "2026-10-14", **kwargs: Any) -> AllocationShadowScreen:
    return build_screen(_day(date, **kwargs))


def _no_index(date: str, skip: dict[str, Any] | None = None) -> AllocationShadowScreen:
    return build_screen(DayInput(date, MISSING, _found(skip) if skip else MISSING, {}))


def _record(date: str, **kwargs: Any) -> DayRecord:
    return DayRecord(date, _screen(date, **kwargs))


# =================================================================================================
# 『数える日』の判定表(USER 承認済みの暫定ルール)
# =================================================================================================


def test_a_complete_day_with_a_computed_owner_counts() -> None:
    verdict = classify_day(_screen())
    assert verdict.counted is True
    assert verdict.not_counted is None
    assert verdict.state is DayState.COMPLETE
    assert verdict.computed_owners == 1
    assert verdict.missing_count == 0


@pytest.mark.parametrize("make", [_skipped_owner, _failed_owner])
def test_a_day_where_every_owner_skipped_or_failed_is_not_counted(make: Any) -> None:
    verdict = classify_day(_screen(owners={_REF_A: make()}))
    assert verdict.counted is False
    assert verdict.not_counted is NotCounted.NO_COMPUTED_OWNER
    assert verdict.computed_owners == 0


def test_one_computed_owner_among_skipped_ones_counts_the_day() -> None:
    owners = {_REF_A: _skipped_owner(), _REF_B: view_tests._owner_record()}
    verdict = classify_day(_screen(owners=owners))
    assert verdict.counted is True
    assert (verdict.computed_owners, verdict.skipped_owners) == (1, 1)


def test_a_partial_day_with_a_computed_owner_and_no_unparsed_counts_and_keeps_the_missing() -> None:
    index = view_tests._index([_REF_A], unrecorded=1, reasons={"WRITE_FAILED": 1})
    verdict = classify_day(_screen(index=index))
    assert verdict.state is DayState.PARTIAL
    assert verdict.counted is True
    assert (verdict.attempted, verdict.recorded, verdict.missing_count) == (2, 1, 1)


def test_a_partial_day_with_an_unparsed_item_is_not_counted() -> None:
    index = view_tests._index([_REF_A], unrecorded=1, reasons={"WRITE_FAILED": 1})
    record = view_tests._owner_record()
    record["candidates"][0]["allocation"]["reason_code"] = "NOT_A_KNOWN_REASON"
    verdict = classify_day(_screen(owners={_REF_A: record}, index=index))
    assert verdict.state is DayState.PARTIAL
    assert verdict.counted is False
    assert verdict.not_counted is NotCounted.UNPARSED
    assert verdict.unparsed >= 1


def test_a_complete_day_with_an_unparsed_item_is_not_counted() -> None:
    record = view_tests._owner_record()
    record["candidates"][0]["allocation"]["reason_code"] = "NOT_A_KNOWN_REASON"
    verdict = classify_day(_screen(owners={_REF_A: record}))
    assert verdict.state is DayState.COMPLETE
    assert verdict.counted is False
    assert verdict.not_counted is NotCounted.UNPARSED


def test_an_empty_index_day_is_not_counted() -> None:
    index = view_tests._index([], recorded=0)
    verdict = classify_day(build_screen(DayInput("2026-10-14", _found(index), MISSING, {})))
    assert verdict.state is DayState.EMPTY
    assert verdict.not_counted is NotCounted.EMPTY


def test_a_day_without_an_index_is_not_counted_and_the_skip_is_distinguished() -> None:
    assert classify_day(_no_index("2026-10-14")).not_counted is NotCounted.NO_INDEX
    skip = view_tests._skip_record()
    verdict = classify_day(_no_index("2026-10-14", skip))
    assert verdict.not_counted is NotCounted.NO_INDEX_DAY_SKIP
    assert (verdict.attempted, verdict.recorded, verdict.missing_count) == (None, None, None)


def test_an_unsupported_index_day_is_not_counted() -> None:
    index = view_tests._index([_REF_A])
    index["schema_version"] = 99
    verdict = classify_day(_screen(index=index))
    assert verdict.state is DayState.UNSUPPORTED
    assert verdict.not_counted is NotCounted.UNSUPPORTED


def test_the_nine_day_shapes_from_the_desk_rehearsal_are_each_classified() -> None:
    """全 DayState × computed の有無 × unparsed の網羅(期待は表で固定)。"""
    broken = view_tests._owner_record()
    broken["candidates"][0]["allocation"]["reason_code"] = "NOT_A_KNOWN_REASON"
    partial = view_tests._index([_REF_A], unrecorded=1, reasons={"WRITE_FAILED": 1})
    unsupported = view_tests._index([_REF_A])
    unsupported["schema_version"] = 99
    cases: list[tuple[AllocationShadowScreen, DayState, bool]] = [
        (_screen(), DayState.COMPLETE, True),
        (_screen(owners={_REF_A: _skipped_owner()}), DayState.COMPLETE, False),
        (_screen(owners={_REF_A: broken}), DayState.COMPLETE, False),
        (_screen(index=partial), DayState.PARTIAL, True),
        (_screen(owners={_REF_A: _skipped_owner()}, index=partial), DayState.PARTIAL, False),
        (_screen(owners={_REF_A: broken}, index=partial), DayState.PARTIAL, False),
        (_no_index("2026-10-14"), DayState.NO_INDEX, False),
        (_no_index("2026-10-14", view_tests._skip_record()), DayState.NO_INDEX_DAY_SKIP, False),
        (_screen(index=unsupported), DayState.UNSUPPORTED, False),
    ]
    for screen, state, counted in cases:
        verdict = classify_day(screen)
        assert (verdict.state, verdict.counted) == (state, counted), (state, counted)


def test_a_day_that_could_not_be_read_or_was_not_read_is_not_counted_and_is_not_zero() -> None:
    summary = summarize(
        [DayRecord("2026-10-13", read_failure="ACCESS_DENIED"), DayRecord("2026-10-14")]
    )
    first, second = summary.verdicts
    assert first.not_counted is NotCounted.READ_FAILED
    assert first.read_failure == "ACCESS_DENIED"
    assert second.not_counted is NotCounted.NOT_READ
    assert summary.counted_days == 0
    assert dict(summary.state_counts) == {"READ_FAILED": 1, "NOT_READ": 1}


# =================================================================================================
# 欠落件数(= attempted − recorded + 索引にあるが記録の無い符号の数)
# =================================================================================================


def test_the_missing_count_counts_owners_in_the_index_without_a_record() -> None:
    owners: dict[str, dict[str, Any] | None] = {_REF_A: view_tests._owner_record(), _REF_B: None}
    verdict = classify_day(_screen(owners=owners))
    assert verdict.state is DayState.PARTIAL
    # 索引は attempted = recorded = 2(unrecorded = 0)なのに記録が 1 件無い = 欠落 1
    assert (verdict.attempted, verdict.recorded, verdict.missing_count) == (2, 2, 1)


def test_the_missing_count_adds_the_unrecorded_and_the_absent_records() -> None:
    index = view_tests._index([_REF_A, _REF_B], unrecorded=1, reasons={"WRITE_FAILED": 1})
    owners: dict[str, dict[str, Any] | None] = {_REF_A: view_tests._owner_record(), _REF_B: None}
    verdict = classify_day(_screen(owners=owners, index=index))
    assert (verdict.attempted, verdict.recorded, verdict.missing_count) == (3, 2, 2)


def test_an_inconsistent_index_still_gives_a_positive_missing_count() -> None:
    index = view_tests._index([_REF_A], recorded=1)
    index["attempted_owner_count"] = 3  # 索引が矛盾(unrecorded_owner_count = 0 のまま)
    verdict = classify_day(_screen(index=index))
    assert verdict.state is DayState.PARTIAL
    assert verdict.missing_count == 2


# =================================================================================================
# 集計: 並び・版・母数
# =================================================================================================


def _three_days() -> list[DayRecord]:
    return [
        _record("2026-10-15"),
        _record("2026-10-16", owners={_REF_A: _skipped_owner()}),
        _record("2026-10-19"),
    ]


def test_the_summary_does_not_depend_on_the_input_order_and_is_deterministic() -> None:
    days = _three_days()
    forward = summarize(days)
    backward = summarize(list(reversed(days)))
    assert forward == backward
    assert [v.business_date for v in forward.verdicts] == sorted(r.business_date for r in days)
    assert summarize(days) == forward


def test_a_duplicate_date_is_refused() -> None:
    with pytest.raises(ValueError, match="重複"):
        summarize([_record("2026-10-15"), _record("2026-10-15")])


def _versioned(date: str, version: str) -> DayRecord:
    record = view_tests._owner_record()
    record["model"]["raer_model_version"] = version
    return _record(date, owners={_REF_A: record})


def test_a_version_change_splits_the_periods_and_never_adds_across_versions() -> None:
    days = [
        _versioned("2026-10-15", "v1"),
        _versioned("2026-10-16", "v1"),
        DayRecord("2026-10-19", _no_index("2026-10-19")),  # 版が決まらない日は期間を分けない
        _versioned("2026-10-20", "v2"),
    ]
    summary = summarize(days)
    assert [(p.version.model_version, p.day_count) for p in summary.periods] == [
        ("v1", 2),
        ("v2", 1),
    ]
    by_period = [
        next(m for m in p.measures if m.key == "owner_computed_rate").denominator
        for p in summary.periods
    ]
    assert by_period == [2, 1]  # 合算(3)ではなく期間ごと
    assert summary.counted_days == 3 and summary.total_days == 4


def test_a_day_whose_owners_disagree_on_the_version_is_its_own_mixed_period() -> None:
    other = view_tests._owner_record()
    other["model"]["raer_model_version"] = "v-other"
    summary = summarize(
        [_record("2026-10-15", owners={_REF_A: view_tests._owner_record(), _REF_B: other})]
    )
    assert [p.version.model_version for p in summary.periods] == ["MIXED"]


def _measures(summary: Any, index: int = 0) -> dict[str, Measure]:
    return {m.key: m for m in summary.periods[index].measures}


def test_the_measures_carry_the_counts_and_denominators_of_the_recorded_day() -> None:
    summary = summarize([_record("2026-10-14")])
    m = _measures(summary)
    assert (m["owner_computed_rate"].count, m["owner_computed_rate"].denominator) == (1, 1)
    assert (m["raer_available_rate"].count, m["raer_available_rate"].denominator) == (2, 2)
    assert m["raer_unavailable_rate"].count == 0
    assert dict(m["decisions"].buckets) == {"ALLOCATED": 1, "NOT_ALLOCATED": 1}
    assert dict(m["ranks"].buckets) == {"1": 1, "2": 1}
    assert dict(m["allocation_reasons"].buckets) == {
        "ALLOCATED": 1,
        "INSUFFICIENT_CASH_REMAINING": 1,
    }
    assert dict(m["allocated_per_owner_day"].buckets) == {"1": 1}
    assert dict(m["remaining_cash_stage"].buckets) == {"POSITIVE": 1}
    # 買付余力の最終照合日 2026-10-11 → 対象日 2026-10-14 = 3 日
    assert dict(m["cash_staleness_days"].buckets) == {"3": 1}
    assert dict(m["buy_actions"].buckets) == {"BUY": 1, "SMALL_ENTRY": 1}


def test_a_zero_denominator_is_not_measurable_and_never_a_zero_percent() -> None:
    summary = summarize([_record("2026-10-15", owners={_REF_A: _skipped_owner()})])
    m = _measures(summary)
    candidates = m["raer_available_rate"]
    assert candidates.denominator == 0
    assert rate(candidates) is None
    assert candidates.classification is Classification.NEEDS_MORE_OBSERVATION
    report = build_ten_day_report(summary, ["2026-10-15"])
    payload = report_to_dict(report, summary)
    shown = next(x for x in payload["periods"][0]["measures"] if x["key"] == "raer_available_rate")
    assert shown["status"] == "測れない"
    assert shown["rate"] == "測れない"  # 0.0000 ではない


def test_a_judgeable_classification_is_never_assigned_while_the_criterion_is_unapproved() -> None:
    summary = summarize(_three_days())
    report = build_ten_day_report(summary, ["2026-10-15", "2026-10-16", "2026-10-19"])
    assigned = {m.classification for p in summary.periods for m in p.measures}
    assigned |= {n.classification for n in report.not_measurable}
    assert Classification.JUDGEABLE_IN_10_DAYS not in assigned


def test_the_skipped_reasons_and_the_exclusions_are_counted_by_code() -> None:
    skipped = _skipped_owner()
    record = view_tests._owner_record()
    record["counts"]["exclusions"] = {"PRICE_UNAVAILABLE": 2, "ACTION_MISSING": 1}
    record["counts"]["incomplete_reasons"] = ["HOLDING_PRICE_MISSING"]
    summary = summarize([_record("2026-10-14", owners={_REF_A: skipped, _REF_B: record})])
    m = _measures(summary)
    assert dict(m["skip_reasons"].buckets) == {"TIME_BUDGET": 1}
    assert m["skip_reasons"].denominator == 1
    assert dict(m["exclusions"].buckets) == {"ACTION_MISSING": 1, "PRICE_UNAVAILABLE": 2}
    assert m["exclusions"].denominator == 3
    assert dict(m["incomplete_reasons"].buckets) == {"HOLDING_PRICE_MISSING": 1}


def test_the_owner_kinds_follow_the_screen_status_and_outcome() -> None:
    screen = _screen(
        owners={_REF_A: view_tests._owner_record(), _REF_B: None},
    )
    kinds = {owner.ref: owner_kind(owner) for owner in screen.owners}
    assert kinds == {_REF_A: OwnerKind.COMPUTED, _REF_B: OwnerKind.MISSING}


# =================================================================================================
# 10 営業日の観測レポート
# =================================================================================================


def _days(dates: list[str], skipped: tuple[str, ...] = ()) -> list[DayRecord]:
    return [
        _record(d, owners={_REF_A: _skipped_owner()}) if d in skipped else _record(d) for d in dates
    ]


_BUSINESS_DAYS = [
    "2026-10-15",
    "2026-10-16",
    "2026-10-19",
    "2026-10-20",
    "2026-10-21",
    "2026-10-22",
    "2026-10-23",
    "2026-10-26",
    "2026-10-27",
    "2026-10-28",
    "2026-10-29",
]


def test_the_report_counts_days_and_names_day_one_and_the_tenth_day() -> None:
    summary = summarize(_days(_BUSINESS_DAYS[:10]))
    report = build_ten_day_report(summary, _BUSINESS_DAYS)
    assert (report.counted_days, report.reached) == (10, True)
    assert report.first_counted_date == "2026-10-15"
    assert report.tenth_counted_date == "2026-10-28"
    assert report.projected_tenth_date is None  # 到達済みなら見込みは不要(確定の日がある)
    assert report.expected_days_total == 11
    # 10/29 は期待される営業日なのに入力が無い = 未読(0 件と混同しない)
    assert dict(report.not_counted_counts) == {"NOT_READ": 1}
    assert report.rule_source == COUNTING_RULE_SOURCE
    assert "6165459615" in report.rule_source


def test_a_skipped_day_delays_the_tenth_day_and_the_projection_is_conditional() -> None:
    summary = summarize(_days(_BUSINESS_DAYS[:4], skipped=("2026-10-16",)))
    report = build_ten_day_report(summary, _BUSINESS_DAYS)
    assert report.counted_days == 3
    assert report.reached is False
    assert report.tenth_counted_date is None
    # 以後(最後に読んだ 10/20 の後)の営業日が全て数える日になるなら 7 日目が 10 日目
    assert report.projected_tenth_date == "2026-10-29"
    assert dict(report.not_counted_counts)["NO_COMPUTED_OWNER"] == 1


def test_a_holiday_is_not_an_expected_day_so_it_is_not_a_missing_day() -> None:
    expected = ["2026-10-15", "2026-10-19"]  # 10/16 を営業日として渡さない
    summary = summarize(_days(expected))
    report = build_ten_day_report(summary, expected)
    assert report.expected_days_total == 2
    assert report.not_counted_counts == ()


def test_the_partial_days_keep_the_missing_count_and_the_denominator() -> None:
    index = view_tests._index([_REF_A], unrecorded=2, reasons={"WRITE_FAILED": 2})
    summary = summarize([_record("2026-10-15", index=index)])
    report = build_ten_day_report(summary, ["2026-10-15"])
    (partial,) = report.partial_days
    assert (partial.missing_count, partial.attempted) == (2, 3)


def test_the_improvement_candidates_fill_only_the_mechanical_columns() -> None:
    record = view_tests._owner_record()
    record["candidates"][1]["raer_out"] = {"state": "UNAVAILABLE", "value": None}
    summary = summarize(
        [_record("2026-10-14", owners={_REF_A: record}), *_days(["2026-10-15"], ("2026-10-15",))]
    )
    report = build_ten_day_report(summary, ["2026-10-14", "2026-10-15"])
    assert report.candidates
    for candidate in report.candidates:
        assert candidate.summary and candidate.facts and candidate.logic in {"A", "B", "C", "D"}
        assert candidate.related_issues
        assert "初案・未確定" in candidate.interpretation
    logics = {c.logic for c in report.candidates}
    assert {"A", "B", "C", "D"} <= logics
    # 件数と母数が事実の欄に入る
    assert any("算出不能 1 / 候補 2" in c.facts for c in report.candidates)


def test_the_not_measurable_items_are_listed_with_their_reasons() -> None:
    summary = summarize([_record("2026-10-14")])
    report = build_ten_day_report(summary, ["2026-10-14"])
    assert report.not_measurable == NOT_MEASURABLE
    deliverables = {n.deliverable for n in NOT_MEASURABLE}
    assert {4, 7, 9} <= deliverables
    assert all(n.reason for n in NOT_MEASURABLE)
    backtest = [n for n in NOT_MEASURABLE if n.classification is Classification.NEEDS_BACKTEST]
    assert backtest  # 事後の評価・妥当性は『要バックテスト』(USER 判断)


# =================================================================================================
# 公開面: 秘密が出ない・owner 別は明示のときだけ
# =================================================================================================


def _canary_summary() -> tuple[Any, Any]:
    record = view_tests._owner_record()
    record["candidates"][0]["stock_code"] = "CANARYCODE"
    record["cash"]["available_cash"] = "98765432"
    record["portfolio"]["portfolio_value"] = "87654321"
    summary = summarize([_record("2026-10-14", owners={_REF_A: record})])
    return summary, build_ten_day_report(summary, ["2026-10-14"])


def test_the_public_output_has_no_reference_code_amount_or_label() -> None:
    summary, report = _canary_summary()
    text = json.dumps(report_to_dict(report, summary), ensure_ascii=False)
    digest = _REF_A.rsplit(":", 1)[-1]
    assert _REF_A not in text and digest not in text and digest[:6] not in text
    assert f"owner-{digest[:6]}" not in text
    for secret in ("CANARYCODE", "98765432", "87654321", "98,765,432", "kv1"):
        assert secret not in text
    assert "by_owner" not in text


def test_the_by_owner_table_appears_only_when_asked_and_uses_the_short_display_name_only() -> None:
    summary, report = _canary_summary()
    text = json.dumps(report_to_dict(report, summary, include_by_owner=True), ensure_ascii=False)
    digest = _REF_A.rsplit(":", 1)[-1]
    assert f"owner-{digest[:6]}" in text
    assert _REF_A not in text and digest not in text.replace(f"owner-{digest[:6]}", "")
    assert "kv1" not in text


def test_the_output_names_the_rule_that_counted_the_days() -> None:
    """『数える日』の規則は暫定。規則が変わると『数えた日数 / 10』の意味が変わるため、出所を出力に残す。"""
    summary, report = _canary_summary()
    payload = report_to_dict(report, summary)
    assert payload["counting_rule_source"] == report.rule_source == COUNTING_RULE_SOURCE
    assert "6165459615" in payload["counting_rule_source"]


def test_the_report_dict_is_json_serialisable_and_deterministic() -> None:
    summary, report = _canary_summary()
    first = json.dumps(report_to_dict(report, summary), ensure_ascii=False, sort_keys=True)
    again = json.dumps(report_to_dict(report, summary), ensure_ascii=False, sort_keys=True)
    assert first == again


def test_no_junk_in_a_record_makes_the_summary_raise() -> None:
    record = view_tests._owner_record()
    for junk in (None, 1, "x", [], {}, [1], {"a": 1}, 1.5, True):
        mutated = copy.deepcopy(record)
        mutated["candidates"] = junk
        mutated["run"] = junk
        mutated["model"] = junk
        mutated["cash"] = junk
        summary = summarize([_record("2026-10-14", owners={_REF_A: mutated})])
        build_ten_day_report(summary, ["2026-10-14"])


# =================================================================================================
# 実際の組み立てが作った記録(F-74 の builder = 実形式)
# =================================================================================================


def test_a_record_built_by_the_real_assembly_is_counted_without_unparsed(tmp_path: Path) -> None:
    day, _ = view_tests._real(tmp_path)
    verdict = classify_day(build_screen(day))
    assert verdict.counted is True
    assert verdict.unparsed == 0
    summary = summarize([DayRecord(day.business_date, build_screen(day))])
    assert summary.counted_days == 1


# =================================================================================================
# 静的な契約(読取専用・純粋・秘密を作らない)
# =================================================================================================

_ALLOWED_IMPORT_ROOTS = {
    "__future__",
    "datetime",
    "collections",
    "dataclasses",
    "decimal",
    "enum",
    "typing",
    "jstock_advisor",
}


def _tree() -> ast.Module:
    return ast.parse(_SUMMARY_PATH.read_text(encoding="utf-8"))


def test_the_summary_imports_only_pure_modules_and_the_cli_view() -> None:
    imported: set[str] = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    assert {m.split(".")[0] for m in imported} <= _ALLOWED_IMPORT_ROOTS
    own = {m for m in imported if m.startswith("jstock_advisor")}
    assert own == {"jstock_advisor.domain.notification.allocation_shadow_cli_view"}


def test_the_summary_has_no_clock_environment_io_or_aws_calls() -> None:
    forbidden_names = {"open", "print", "input", "eval", "exec", "__import__"}
    forbidden_attrs = {"now", "today", "utcnow", "environ", "getenv", "read_text", "write_text"}
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                assert func.id not in forbidden_names, func.id
            if isinstance(func, ast.Attribute):
                assert func.attr not in forbidden_attrs, func.attr
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"environ", "getenv", "scan", "query", "put_item"}, node.attr


def test_the_summary_does_not_reach_the_coefficients_the_policy_or_the_heavy_record() -> None:
    source = _SUMMARY_PATH.read_text(encoding="utf-8")
    for heavy in (
        "allocation_shadow_record",
        "allocation_shadow_compute",
        "raer_coefficients",
        "allocation_policy",
        "portfolio_allocation",
        "replay_items_record",
    ):
        assert f"import {heavy}" not in source
        assert f"{heavy} import" not in source
    assert "from jstock_advisor.domain.signals" not in source
    assert "from jstock_advisor.domain.valuation" not in source


# =================================================================================================
# 読取: 実装は 1 つ(CLI と集計が同じ read_day を呼ぶ)・日単位の隔離
# =================================================================================================


class _FakeSource:
    def __init__(self, records: dict[str, dict[str, Any]], fail_on: str | None = None) -> None:
        self.records = records
        self.fail_on = fail_on
        self.calls: list[str] = []

    def get(self, audit_id: str) -> RecordRead:
        self.calls.append(audit_id)
        if self.fail_on is not None and self.fail_on in audit_id:
            raise ReadFailure(ReadFailureKind.ACCESS_DENIED)
        record = self.records.get(audit_id)
        return RecordRead(ReadStatus.FOUND, record) if record else RecordRead(ReadStatus.NOT_FOUND)


def test_the_cli_and_the_summary_share_one_read_day() -> None:
    assert cli.read_day is read_day
    assert read_day.__module__ == reader.__name__
    sources = [
        node
        for node in ast.walk(ast.parse(Path(cli.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef) and node.name in {"read_day", "_input"}
    ]
    assert sources == []  # CLI の module はキーの順序の実装を持たない


def test_read_day_reads_the_index_then_the_skip_then_the_items_in_that_order() -> None:
    day = "2026-10-14"
    index = view_tests._index([_REF_A])
    source = _FakeSource(
        {
            f"allocation_shadow_index:{day}": index,
            f"allocation_shadow_items:{day}:{_REF_A}": view_tests._owner_record(),
        }
    )
    result = read_day(source, dt.date(2026, 10, 14))
    assert source.calls == [
        f"allocation_shadow_index:{day}",
        f"allocation_shadow_day_skip:{day}",
        f"allocation_shadow_items:{day}:{_REF_A}",
    ]
    assert result.business_date == day and list(result.items) == [_REF_A]


def test_read_days_is_the_loop_over_read_day_and_isolates_a_failed_day() -> None:
    dates = [dt.date(2026, 10, 14), dt.date(2026, 10, 15), dt.date(2026, 10, 16)]
    source = _FakeSource({}, fail_on="2026-10-15")
    results = read_days(source, dates)
    assert [r.business_date for r in results] == dates
    assert [r.failure for r in results] == [None, ReadFailureKind.ACCESS_DENIED, None]
    assert results[0].day is not None and results[1].day is None
    # 失敗した日の後の日も読んでいる(他の日の読取を止めない)
    assert any("2026-10-16" in call for call in source.calls)


def test_read_days_only_ever_calls_get_on_the_source() -> None:
    class _Strict:
        def __init__(self) -> None:
            self.used: set[str] = set()

        def get(self, audit_id: str) -> RecordRead:
            self.used.add("get")
            return RecordRead(ReadStatus.NOT_FOUND)

        def __getattr__(self, name: str) -> Any:  # get 以外は呼べない
            raise AssertionError(name)

    strict = _Strict()
    read_days(strict, [dt.date(2026, 10, 14)])
    assert strict.used == {"get"}


def test_the_failure_kind_has_no_value_or_message() -> None:
    results = read_days(_FakeSource({}, fail_on="allocation"), [dt.date(2026, 10, 14)])
    assert results[0].failure is ReadFailureKind.ACCESS_DENIED
    assert results[0].day is None
