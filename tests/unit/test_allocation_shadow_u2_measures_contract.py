"""購入側 Shadow の観測の集計(#934 U2 = PR-1b + 候補の重なり)の契約テスト。

実際の組み立てが作った形の記録(#605/#934 の契約テストの最小の記録)に候補を差し込み、
新しい指標の件数が手で数えた値と一致することを固定する。出力に銘柄コードを出さないこと・
読めない行は別に数えること・版をまたいで合算しないことも固定する。
"""

from __future__ import annotations

import copy
import json
from typing import Any

from jstock_advisor.cli.allocation_summary import render_table
from jstock_advisor.domain.notification import allocation_shadow_cli_view as cli_view
from jstock_advisor.domain.notification.allocation_shadow_summary import (
    NOT_MEASURABLE,
    Classification,
    DayRecord,
    Measure,
    build_ten_day_report,
    report_to_dict,
    summarize,
)
from jstock_advisor.domain.signals import allocation_shadow_record as record_module
from tests.unit import test_allocation_shadow_cli_view_contract as view_tests
from tests.unit import test_allocation_shadow_compute_contract as compute_tests
from tests.unit import test_allocation_shadow_summary_contract as base

A = base._REF_A
B = base._REF_B
C = compute_tests._ref("owner-c")


def _cand(
    code: str,
    *,
    sector: str | None = "S1",
    verdict: str | None = "FRESH",
    as_of: str | None = "2026-10-13",
    state: str = "PROVISIONAL",
    value: str | None = "10.0",
    decision: str = "ALLOCATED",
    unapplied: list[str] | None = None,
    key: str | None = "k1",
    reasons: list[str] | None = None,
    risk: list[str] | None = None,
    contributions: dict[str, str] | None = None,
) -> dict[str, Any]:
    candidate: dict[str, Any] = copy.deepcopy(view_tests._owner_record()["candidates"][0])
    candidate["stock_code"] = code
    candidate["attrs"]["sector"] = sector
    candidate["price"]["freshness_verdict"] = verdict
    candidate["price"]["price_as_of_date"] = as_of
    candidate["raer_out"].update(
        {
            "state": state,
            "value": value,
            "unapplied_axes": unapplied or [],
            "comparison_key": key,
            "reason_codes": reasons or [],
            "risk_flags": risk or [],
            "extra": {"contributions": contributions},
        }
    )
    candidate["allocation"]["decision"] = decision
    return candidate


def _owner_with(candidates: list[Any], ref: str) -> dict[str, Any]:
    record: dict[str, Any] = view_tests._owner_record()
    record["candidates"] = candidates
    record["owner_ref"] = ref
    return record


def _day(date: str, owners: dict[str, list[Any]]) -> DayRecord:
    return base._record(
        date, owners={ref: _owner_with(cands, ref) for ref, cands in owners.items()}
    )


def _measures(*days: DayRecord) -> dict[str, Measure]:
    summary = summarize(list(days))
    assert len(summary.periods) == 1
    return {m.key: m for m in summary.periods[0].measures}


def _buckets(measure: Measure) -> dict[str, int]:
    return dict(measure.buckets)


# =================================================================================================
# 出所のパスは記録の allowlist の部分集合(allowlist から消えたら落ちる)
# =================================================================================================


def _flatten(spec: Any, prefix: str = "") -> set[str]:
    paths = {prefix} if prefix else set()
    if isinstance(spec, record_module._Opt):
        spec = spec.inner
    if isinstance(spec, dict):
        for key, inner in spec.items():
            paths |= _flatten(inner, f"{prefix}.{key}" if prefix else key)
    return paths


def test_u2_record_paths_are_a_subset_of_the_record_allowlist() -> None:
    allowed = _flatten(record_module._CANDIDATE_SPEC)
    allowed |= {f"raer_out.extra.{key}" for key in record_module._RAER_EXTRA_SPEC}
    assert [p for p in cli_view.U2_RECORD_PATHS if p not in allowed] == []


# =================================================================================================
# 軸別の欠落・比較キー・理由コード(項目 3)
# =================================================================================================


def test_unapplied_axes_keys_and_reason_codes_match_hand_counts() -> None:
    day = _day(
        "2026-10-14",
        {
            A: [
                _cand(
                    "1001",
                    unapplied=["EARNINGS_TREND", "EARNINGS_TREND"],
                    key="k1",
                    reasons=["R_TREND", "R_TREND"],
                ),
                _cand("1002", unapplied=["EARNINGS_TREND", "DATA_QUALITY"], key="k2"),
                _cand("1003", unapplied=[], key=None, reasons=["R_TREND", "R_OTHER"]),
            ]
        },
    )
    measures = _measures(day)
    assert measures["unapplied_axes"].denominator == 3
    assert _buckets(measures["unapplied_axes"]) == {"EARNINGS_TREND": 2, "DATA_QUALITY": 1}
    assert _buckets(measures["comparison_keys"]) == {"k1": 1, "k2": 1, "NONE": 1}
    assert _buckets(measures["raer_reason_codes"]) == {"R_TREND": 2, "R_OTHER": 1}


# =================================================================================================
# 寄与度(項目 4)
# =================================================================================================


def test_contribution_measures_use_only_value_bearing_rows() -> None:
    day = _day(
        "2026-10-14",
        {
            A: [
                _cand(
                    "1001",
                    risk=["HIGH_PER", "HIGH_PER"],
                    contributions={"FAIR_VALUE_CONFIDENCE": "5", "BUSINESS_RISK": "2"},
                ),
                _cand("1002", contributions={"FAIR_VALUE_CONFIDENCE": "3", "BUSINESS_RISK": "0"}),
                _cand("1003", state="UNAVAILABLE", value=None),
            ]
        },
    )
    measures = _measures(day)
    assert measures["deduction_top_axis"].denominator == 2
    assert _buckets(measures["deduction_top_axis"]) == {"FAIR_VALUE_CONFIDENCE": 2}
    assert _buckets(measures["contribution_nonzero_axes"]) == {
        "FAIR_VALUE_CONFIDENCE": 2,
        "BUSINESS_RISK": 1,
    }
    assert _buckets(measures["risk_flag_words"]) == {"HIGH_PER": 1}


# =================================================================================================
# 業種の偏り(項目 5)
# =================================================================================================


def test_allocated_sector_spread_and_unknown() -> None:
    day = _day(
        "2026-10-14",
        {
            A: [
                _cand("1001", sector="S1"),
                _cand("1002", sector="S1"),
                _cand("1003", sector=None),
                _cand("1004", sector="S2", decision="NOT_ALLOCATED"),
            ],
            B: [_cand("1005", sector="S2"), _cand("1006", sector="S3")],
        },
    )
    measures = _measures(day)
    spread = measures["allocated_sector_spread"]
    assert spread.denominator == 2
    assert _buckets(spread) == {"sectors=1": 1, "sectors=2": 1}
    unknown = measures["allocated_sector_unknown"]
    assert (unknown.count, unknown.denominator) == (1, 5)


def test_a_day_without_allocation_is_unmeasurable_not_zero() -> None:
    day = _day("2026-10-14", {A: [_cand("1001", decision="NOT_ALLOCATED")]})
    measures = _measures(day)
    assert measures["allocated_sector_spread"].denominator == 0
    assert measures["allocated_sector_unknown"].denominator == 0
    assert (
        measures["allocated_sector_unknown"].classification is Classification.NEEDS_MORE_OBSERVATION
    )


# =================================================================================================
# 価格の鮮度(項目 8)
# =================================================================================================


def test_price_freshness_measures() -> None:
    day = _day(
        "2026-10-14",
        {
            A: [
                _cand("1001", verdict="FRESH", as_of="2026-10-13"),
                _cand("1002", verdict="STALE", as_of="2026-10-09", state="UNAVAILABLE", value=None),
                _cand("1003", verdict=None, as_of=None),
            ]
        },
    )
    measures = _measures(day)
    assert _buckets(measures["price_freshness_verdicts"]) == {"FRESH": 1, "STALE": 1, "UNKNOWN": 1}
    assert _buckets(measures["price_freshness_by_raer_state"]) == {
        "FRESH:AVAILABLE": 1,
        "STALE:UNAVAILABLE": 1,
        "UNKNOWN:AVAILABLE": 1,
    }
    assert _buckets(measures["price_age_days"]) == {"1": 1, "5": 1, "UNKNOWN": 1}


# =================================================================================================
# 候補の重なり
# =================================================================================================


def _overlap(*days: DayRecord) -> dict[str, int]:
    return _buckets(_measures(*days)["candidate_overlap"])


def test_same_stock_on_three_owners_in_one_day() -> None:
    day = _day("2026-10-14", {A: [_cand("1001")], B: [_cand("1001")], C: [_cand("1001")]})
    assert _overlap(day) == {
        "candidate_days": 3,
        "distinct_stocks": 1,
        "stocks_on_2plus_days": 0,
    }


def test_same_stock_on_two_days_counts_once_as_a_repeat() -> None:
    d1 = _day("2026-10-14", {A: [_cand("1001")]})
    d2 = _day("2026-10-15", {A: [_cand("1001")]})
    assert _overlap(d1, d2) == {
        "candidate_days": 2,
        "distinct_stocks": 1,
        "stocks_on_2plus_days": 1,
    }


def test_mixed_overlap_example() -> None:
    d1 = _day("2026-10-14", {A: [_cand("1001")], B: [_cand("2002")], C: [_cand("2002")]})
    d2 = _day("2026-10-15", {B: [_cand("2002")], A: [_cand("3003")]})
    assert _overlap(d1, d2) == {
        "candidate_days": 5,
        "distinct_stocks": 3,
        "stocks_on_2plus_days": 1,
    }


def test_unreadable_rows_are_reported_separately_and_do_not_change_counts() -> None:
    day = _day("2026-10-14", {A: [_cand("1001"), "not-a-dict", _cand("1002")]})
    measures = _measures(day)
    assert _buckets(measures["candidate_overlap"])["candidate_days"] == 2
    assert measures["unapplied_axes"].denominator == 2
    assert measures["comparison_keys"].denominator == 2
    unreadable = measures["unreadable_candidate_rows"]
    assert (unreadable.count, unreadable.denominator) == (1, 3)


def test_zero_unreadable_rows_shows_zero() -> None:
    unreadable = _measures(_day("2026-10-14", {A: [_cand("1001")]}))["unreadable_candidate_rows"]
    assert (unreadable.count, unreadable.denominator) == (0, 1)


# =================================================================================================
# 公開面・既存の出力・版の分離
# =================================================================================================


def test_no_stock_code_or_owner_ref_reaches_the_json_output() -> None:
    day = _day("2026-10-14", {A: [_cand("7777"), _cand("8888")], B: [_cand("7777")]})
    summary = summarize([day])
    report = build_ten_day_report(summary, ["2026-10-14"])
    text = json.dumps(report_to_dict(report, summary), ensure_ascii=False)
    for secret in ("7777", "8888", A, B):
        assert secret not in text


def test_measures_are_separated_by_version_period() -> None:
    d1 = _day("2026-10-14", {A: [_cand("1001")]})
    record = _owner_with([_cand("1001")], A)
    record["model"]["policy_id"] = "p2"
    d2 = base._record("2026-10-15", owners={A: record})
    periods = summarize([d1, d2]).periods
    assert len(periods) == 2
    for period in periods:
        overlap = {m.key: m for m in period.measures}["candidate_overlap"]
        assert _buckets(overlap) == {
            "candidate_days": 1,
            "distinct_stocks": 1,
            "stocks_on_2plus_days": 0,
        }


def test_not_measurable_still_lists_the_unmeasured_deliverables() -> None:
    deliverables = {item.deliverable for item in NOT_MEASURABLE}
    assert {4, 7, 9} <= deliverables


def test_a_candidate_without_a_stock_code_is_counted_as_a_day_but_not_as_a_stock() -> None:
    day = _day("2026-10-14", {A: [_cand("1001"), _cand("")]})
    assert _overlap(day) == {
        "candidate_days": 2,
        "distinct_stocks": 1,
        "stocks_on_2plus_days": 0,
    }


def test_the_top_deduction_axis_is_the_largest_contribution() -> None:
    day = _day(
        "2026-10-14",
        {A: [_cand("1001", contributions={"FAIR_VALUE_CONFIDENCE": "1", "BUSINESS_RISK": "4"})]},
    )
    assert _buckets(_measures(day)["deduction_top_axis"]) == {"BUSINESS_RISK": 1}


def test_all_zero_contributions_have_no_top_axis() -> None:
    day = _day(
        "2026-10-14",
        {A: [_cand("1001", contributions={"FAIR_VALUE_CONFIDENCE": "0", "BUSINESS_RISK": "0"})]},
    )
    measures = _measures(day)
    assert _buckets(measures["deduction_top_axis"]) == {"NONE": 1}
    assert _buckets(measures["contribution_nonzero_axes"]) == {}


# =================================================================================================
# 寄与が厳密に同値のときの軸の優先(RAER の軸の固定順。サブちゃんの SHOULD-3)
# =================================================================================================


def test_a_tie_in_contribution_picks_the_earlier_axis_in_the_fixed_order() -> None:
    """記録が軸をどの順に書いても、同値なら固定順の先の軸が勝つ(分布が記録のキー順へ偏らない)。"""
    order = list(cli_view._AXIS_ORDER)
    reversed_keys = list(reversed(order))  # 記録が固定順と逆に書いた場合
    contributions = {axis: "1" for axis in reversed_keys}
    day = _day("2026-10-14", {A: [_cand("1001", contributions=contributions)]})
    assert _buckets(_measures(day)["deduction_top_axis"]) == {order[0]: 1}
    # 先頭の 2 軸だけが同値の最大のとき、固定順の先(= order の小さい添字)が勝つ
    tie = {order[-1]: "5", order[1]: "5", order[0]: "2"}
    tie = {key: tie[key] for key in sorted(tie, key=order.index, reverse=True)}
    day2 = _day("2026-10-14", {A: [_cand("1002", contributions=tie)]})
    assert _buckets(_measures(day2)["deduction_top_axis"]) == {order[1]: 1}


# =================================================================================================
# 『実際に読む ⊆ 宣言』を挙動で固定する(サブちゃんの SHOULD-1)
#   cli_view.py 全体の .get は today の画面の読みも含むため、AST ではなく外側から依存を固定する。
# =================================================================================================

_U2_MEASURE_KEYS = (
    "unapplied_axes",
    "comparison_keys",
    "raer_reason_codes",
    "deduction_top_axis",
    "contribution_nonzero_axes",
    "risk_flag_words",
    "allocated_sector_spread",
    "allocated_sector_unknown",
    "price_freshness_verdicts",
    "price_freshness_by_raer_state",
    "price_age_days",
    "candidate_overlap",
    "unreadable_candidate_rows",
)

#: 宣言した各 path を欠いたとき、値(分母・件数・bucket)が変わる指標の集合(= 依存する指標)
_DEPENDENTS = {
    "stock_code": {"candidate_overlap"},
    "attrs.sector": {"allocated_sector_spread", "allocated_sector_unknown"},
    "price.freshness_verdict": {"price_freshness_verdicts", "price_freshness_by_raer_state"},
    "price.price_as_of_date": {"price_age_days"},
    "raer_out.state": {"price_freshness_by_raer_state"},
    "raer_out.value": {"deduction_top_axis", "contribution_nonzero_axes", "risk_flag_words"},
    "raer_out.unapplied_axes": {"unapplied_axes"},
    "raer_out.comparison_key": {"comparison_keys"},
    "raer_out.reason_codes": {"raer_reason_codes"},
    "raer_out.risk_flags": {"risk_flag_words"},
    "raer_out.extra.contributions": {"deduction_top_axis", "contribution_nonzero_axes"},
    "allocation.decision": {"allocated_sector_spread", "allocated_sector_unknown"},
}


def _minimal_candidate() -> dict[str, Any]:
    """U2_RECORD_PATHS の path だけを持つ候補(それ以外の項目は持たない)。"""
    return {
        "stock_code": "1001",
        "attrs": {"sector": "S1"},
        "price": {"freshness_verdict": "FRESH", "price_as_of_date": "2026-10-13"},
        "raer_out": {
            "state": "PROVISIONAL",
            "value": "10.0",
            "unapplied_axes": ["EARNINGS_TREND"],
            "comparison_key": "k1",
            "reason_codes": ["R_TREND"],
            "risk_flags": ["HIGH_PER"],
            "extra": {"contributions": {"FAIR_VALUE_CONFIDENCE": "5", "BUSINESS_RISK": "2"}},
        },
        "allocation": {"decision": "ALLOCATED"},
    }


def _drop(candidate: dict[str, Any], path: str) -> dict[str, Any]:
    out = copy.deepcopy(candidate)
    *parents, leaf = path.split(".")
    node = out
    for key in parents:
        node = node[key]
    del node[leaf]
    return out


def _signature(candidate: dict[str, Any]) -> dict[str, tuple[Any, ...]]:
    day = _day("2026-10-14", {A: [candidate]})
    return {
        key: (m.denominator, m.count, m.buckets)
        for key, m in _measures(day).items()
        if key in _U2_MEASURE_KEYS
    }


def test_the_declared_paths_alone_give_every_u2_measure_a_value() -> None:
    signature = _signature(_minimal_candidate())
    assert set(signature) == set(_U2_MEASURE_KEYS)
    unmeasurable = [key for key, (denominator, *_rest) in signature.items() if denominator <= 0]
    assert unmeasurable == []


def test_the_dependents_table_covers_exactly_the_declared_paths() -> None:
    assert set(_DEPENDENTS) == set(cli_view.U2_RECORD_PATHS)


def test_dropping_a_declared_path_changes_only_its_dependent_measures() -> None:
    """宣言した path を 1 つ欠くと、依存する指標だけが変わる(死んだ宣言・申告漏れの両方を検出)。"""
    full = _signature(_minimal_candidate())
    wrong: dict[str, set[str]] = {}
    for path in cli_view.U2_RECORD_PATHS:
        degraded = _signature(_drop(_minimal_candidate(), path))
        changed = {key for key in full if degraded[key] != full[key]}
        if changed != _DEPENDENTS[path]:
            wrong[path] = changed
    assert wrong == {}


# =================================================================================================
# 表と JSON の parity(サブちゃんの SHOULD-2)— 全 measure が両方に現れる
# =================================================================================================


def test_every_measure_appears_in_both_the_table_and_the_json() -> None:
    day = _day("2026-10-14", {A: [_cand("1001")]})
    summary = summarize([day])
    report = build_ten_day_report(summary, ["2026-10-14"])
    table = render_table(report, summary, by_owner=False)
    payload = report_to_dict(report, summary)
    period_measures = payload["periods"][0]["measures"]
    keys_in_json = [m["key"] for m in period_measures]
    for measure in summary.periods[0].measures:
        assert measure.title in table
        assert measure.key in keys_in_json
    assert set(_U2_MEASURE_KEYS) <= set(keys_in_json)
