"""利確判定(evaluate_profit_taking)の挙動不変の固定(characterization)と、trace の契約
(Issue #878 PR-3a)。

1. characterization: 変更前の実装で生成した期待値
   (tests/fixtures/profit_taking_characterization.json。合成入力 約 11000 件。結果の全 field の
   ハッシュ + 最終 action・origin・売却強度)と、現行の
   evaluate_profit_taking の結果が全件一致する。**この期待値は再生成しない**(trace の追加は判定を
   変えない変更であり、期待値が変わるなら判定が変わったということ)。判定ルール・設定を意図して
   変える変更の場合のみ、その変更の中で再生成し、差分を説明する。
2. 欠測の入力: 適正価格が使えない・bull 等の欠落・momentum なし・含み損・候補なし、の全組み合わせ
   で、例外を出さず、同じ入力で 2 回呼んでも同じ結果(副作用なし)。
"""

from __future__ import annotations

import collections
import json
from decimal import Decimal
from pathlib import Path

from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    StockType,
    TrendClassification,
)
from jstock_advisor.domain.exit_architecture.arbiter import MAX_STRENGTH_BY_TRIGGER
from jstock_advisor.domain.exit_architecture.vocabulary import TriggerKind
from jstock_advisor.domain.signals.profit_taking import (
    CandidatePath,
    ExitTrace,
    ProfitTakingResult,
    VoteKind,
    _apply_mitigating_factors,
    _Level,
    _RawLevelOrigin,
    evaluate_profit_taking,
    evaluate_profit_taking_traced,
)
from tests.support import profit_taking_cases as pc

_GOLDEN_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "profit_taking_characterization.json"
)


def _golden() -> dict[str, str]:
    payload = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    cases: dict[str, str] = payload["cases"]
    return cases


def _all_cases() -> list[pc.Case]:
    return [
        *pc.sampled_cases(),
        *pc.focused_cases(),
        *pc.boundary_cases(),
        *pc.gate_cases(),
        *pc.missing_input_cases(),
    ]


def test_the_characterization_covers_every_case_of_the_grid() -> None:
    golden = _golden()
    assert set(golden) == {case.case_id for case in _all_cases()}
    assert len(golden) > 11000


def test_the_grid_reaches_every_action_and_origin() -> None:
    """格子が退化していない(全 action・全 origin に届く)ことの確認。"""
    golden = _golden()
    parsed = [value.split("|") for value in golden.values() if not value.startswith("EXC:")]
    actions = collections.Counter(parts[1] for parts in parsed)
    origins = collections.Counter(parts[2] for parts in parsed)
    assert set(actions) == {
        "HOLD",
        "WATCH",
        "PARTIAL_PROFIT_TAKE",
        "FULL_PROFIT_TAKE",
    }
    assert set(origins) == {
        "NONE",
        "OTHER_CONDITIONS",
        "PRICE_POSITION",
        "PROFIT_PROTECTION_STRONG",
        "FAIR_VALUE_STRONG",
        "FUNDAMENTAL_CRITICAL_RISK",
    }
    intensities = {parts[3] for parts in parsed}
    assert {"LIGHT", "STANDARD", "STRONG", "VERY_STRONG"} <= intensities


def test_evaluate_profit_taking_is_unchanged_for_every_case_of_the_grid() -> None:
    golden = _golden()
    mismatches: list[str] = []
    for case in _all_cases():
        actual = pc.compact(pc.evaluate(evaluate_profit_taking, case.kwargs))
        if actual != golden[case.case_id]:
            mismatches.append(f"{case.case_id}: {golden[case.case_id]} -> {actual}")
    assert mismatches == [], f"{len(mismatches)} 件の結果が変わった: {mismatches[:5]}"


def test_missing_inputs_never_raise_and_have_no_side_effects() -> None:
    cases = list(pc.missing_input_cases())
    assert len(cases) > 1500
    for case in cases:
        first = pc.evaluate(evaluate_profit_taking, case.kwargs)
        assert not isinstance(first, str), f"{case.case_id}: {first}"
        second = pc.evaluate(evaluate_profit_taking, case.kwargs)
        assert pc.digest(first) == pc.digest(second), case.case_id


# ---------------------------------------------------------------------------
# trace の契約(evaluate_profit_taking_traced)
# ---------------------------------------------------------------------------


def _grid_traces() -> list[tuple[pc.Case, ProfitTakingResult, ExitTrace]]:
    out = []
    for case in _all_cases():
        result, trace = evaluate_profit_taking_traced(**case.kwargs)
        out.append((case, result, trace))
    return out


_TRACES = _grid_traces()


def test_the_traced_result_is_identical_to_the_plain_result() -> None:
    for case, result, _ in _TRACES:
        plain = evaluate_profit_taking(**case.kwargs)
        assert pc.digest(plain) == pc.digest(result), case.case_id


def test_the_winner_recomputed_from_the_trace_matches_the_engine() -> None:
    """現行の勝者の規則(最大 level -> 同 level の最大 origin)を trace の候補へ適用すると、
    エンジンの raw_level・origin に一致する。"""
    for case, result, trace in _TRACES:
        if trace.candidates:
            level = max(c.level for c in trace.candidates)
            origin = max(
                _RawLevelOrigin[c.origin].value for c in trace.candidates if c.level == level
            )
            assert trace.raw_level == level, case.case_id
            assert _RawLevelOrigin(origin).name == trace.origin == result.origin, case.case_id
        else:
            assert trace.raw_level == 0, case.case_id
            assert trace.origin == "NONE" == result.origin, case.case_id


def test_every_candidate_path_is_reached_by_the_grid() -> None:
    reached = {c.path for _, _, trace in _TRACES for c in trace.candidates}
    assert reached == set(CandidatePath)


def test_the_candidate_paths_are_the_same_names_as_the_trigger_kinds() -> None:
    """adapter(PR-3b)が名前で TriggerKind へ写せる(1:1)。profit_taking.py は import しない。"""
    assert {p.value for p in CandidatePath} == {k.value for k in TriggerKind}
    assert all(p.value == p.name for p in CandidatePath)


_EXPECTED_SHAPE = {
    # 経路 -> (level の集合, origin の集合)
    CandidatePath.FULL_STRONG_CRITICAL: ({3}, {"FUNDAMENTAL_CRITICAL_RISK"}),
    CandidatePath.FAIR_VALUE_STRONG: ({3}, {"FAIR_VALUE_STRONG"}),
    CandidatePath.USER_TARGET_PRICE: ({3}, {"PRICE_POSITION"}),
    CandidatePath.USER_TARGET_RATE: ({3}, {"PRICE_POSITION"}),
    CandidatePath.FULL_MODERATE_CONDITIONS: ({3}, {"OTHER_CONDITIONS"}),
    CandidatePath.PRICE_UPSIDE_MATRIX: ({1, 2, 3}, {"PRICE_POSITION"}),
    CandidatePath.FAIR_VALUE_PARTIAL_GATE: ({2}, {"FAIR_VALUE_STRONG"}),
    CandidatePath.PROFIT_PROTECTION_STRONG: ({2}, {"PROFIT_PROTECTION_STRONG"}),
    CandidatePath.PARTIAL_CONDITIONS: ({1, 2}, {"OTHER_CONDITIONS"}),
}


def test_each_path_has_the_level_and_origin_of_the_current_engine() -> None:
    assert set(_EXPECTED_SHAPE) == set(CandidatePath)
    for case, _, trace in _TRACES:
        for candidate in trace.candidates:
            levels, origins = _EXPECTED_SHAPE[candidate.path]
            assert candidate.level in levels, (case.case_id, candidate)
            assert candidate.origin in origins, (case.case_id, candidate)


def test_the_strength_cap_table_of_the_arbiter_matches_the_levels_the_engine_reaches() -> None:
    """Arbiter の MAX_STRENGTH_BY_TRIGGER(現行が到達できる最大)が、格子で実際に到達する最大の
    level と一致する(ユーザー目標は通知のみのため Arbiter の入力ではない)。"""
    reached: dict[str, int] = {}
    for _, _, trace in _TRACES:
        for candidate in trace.candidates:
            name = candidate.path.value
            reached[name] = max(reached.get(name, 0), candidate.level)
    for path in CandidatePath:
        kind = TriggerKind(path.value)
        if kind in MAX_STRENGTH_BY_TRIGGER:
            assert int(MAX_STRENGTH_BY_TRIGGER[kind]) == reached[path.value], path


def test_condition_paths_carry_enough_votes() -> None:
    config = pc.CONFIG.condition_based_judgment
    for case, _, trace in _TRACES:
        for candidate in trace.candidates:
            if candidate.path is CandidatePath.FULL_MODERATE_CONDITIONS:
                assert len(candidate.votes) >= config.min_moderate_conditions_for_full, case
            if candidate.path is CandidatePath.PARTIAL_CONDITIONS and candidate.level == 2:
                assert len(candidate.votes) >= config.min_conditions_for_partial, case
            if candidate.path not in (
                CandidatePath.FULL_MODERATE_CONDITIONS,
                CandidatePath.PARTIAL_CONDITIONS,
            ):
                assert candidate.votes == (), (case.case_id, candidate)


def test_the_mitigation_steps_are_the_downgrade_the_mitigation_applied() -> None:
    for case, result, trace in _TRACES:
        if trace.raw_level == 0 or trace.origin == "FUNDAMENTAL_CRITICAL_RISK":
            assert trace.mitigation_steps == 0, case.case_id
            continue
        after, _ = _apply_mitigating_factors(
            _Level(trace.raw_level),
            case.kwargs["mitigating_inputs"],
            pc.CONFIG.mitigating_factors,
        )
        assert trace.mitigation_steps == trace.raw_level - int(after), case.case_id
        assert 0 <= trace.mitigation_steps <= trace.raw_level, case.case_id
        # 緩和が実際に下げた結果は、エンジンの報告(mitigating_downgrade_applied)と矛盾しない
        if result.mitigating_downgrade_applied:
            assert trace.mitigation_steps > 0, case.case_id


def test_uptrend_and_hard_overvalued_follow_the_engine_definitions() -> None:
    margin = Decimal(str(pc.CONFIG.condition_based_judgment.timing_downgrade_block_margin_pct))
    for case, result, trace in _TRACES:
        inputs = case.kwargs["condition_inputs"]
        momentum = inputs.momentum
        expected_uptrend = momentum is not None and momentum.trend_classification in (
            TrendClassification.STRONG_UPTREND,
            TrendClassification.UPTREND,
        )
        assert trace.uptrend is expected_uptrend, case.case_id
        fv = inputs.fair_value_range
        expected_hard = bool(
            fv is not None
            and fv.usable_for_trading_judgment
            and fv.overall_confidence != ConfidenceLevel.LOW
            and fv.bull is not None
            and case.kwargs["current_price"] > fv.bull * (1 + margin / 100)
        )
        assert trace.hard_overvalued is expected_hard, case.case_id
        # タイミング層の降格が報告されるのは、上昇トレンドで hard_overvalued でないときだけ
        if result.timing_downgrade_applied:
            assert trace.uptrend and not trace.hard_overvalued, case.case_id
            assert trace.origin != "FUNDAMENTAL_CRITICAL_RISK", case.case_id


def test_a_hold_has_no_candidate_and_a_candidate_never_has_level_zero() -> None:
    for case, result, trace in _TRACES:
        assert all(c.level >= 1 for c in trace.candidates), case.case_id
        if trace.raw_level == 0:
            assert trace.candidates == (), case.case_id
            assert result.final_action.value == "HOLD", case.case_id


def test_every_vote_kind_is_backed_by_the_input_that_produces_it() -> None:
    """票の種別は理由の文字列ではなく構造化された値。各票は、それを成立させる入力がある場合にだけ
    付く(取り違えると落ちる)。格子の中で全ての種別に届くことも確認する。"""
    thresholds = pc.CONFIG.thresholds
    seen: set[VoteKind] = set()
    for case, _, trace in _TRACES:
        inputs = case.kwargs["condition_inputs"]
        yield_pct = case.kwargs["current_total_yield_pct"]
        growth = StockType.GROWTH in inputs.stock_types
        trend = inputs.momentum.trend_classification if inputs.momentum is not None else None
        for candidate in trace.candidates:
            for vote in candidate.votes:
                seen.add(vote)
                if vote is VoteKind.FAIR_VALUE_WEAK:
                    assert inputs.fair_value_range is not None, case.case_id
                elif vote is VoteKind.GROWTH_SLOWDOWN:
                    assert growth and (
                        inputs.guidance_revision_disclosed or inputs.severe_earnings_decline
                    )
                elif vote is VoteKind.TREND_WORSENING:
                    assert trend in (
                        TrendClassification.DOWNTREND,
                        TrendClassification.STRONG_DOWNTREND,
                    )
                elif vote is VoteKind.LOW_YIELD:
                    assert not growth and yield_pct is not None
                    assert yield_pct < thresholds.total_yield_caution_pct, case.case_id
                elif vote is VoteKind.CONCENTRATION:
                    assert inputs.portfolio_concentration_over_limit, case.case_id
                elif vote is VoteKind.EARNINGS_EVENT_RISK_REDUCTION:
                    assert inputs.earnings_event_risk_reduction_rationale, case.case_id
                elif vote is VoteKind.PROFIT_PROTECTION_CANDIDATE:
                    assert inputs.profit_protection is not None, case.case_id
                    assert inputs.profit_protection.candidate_signal, case.case_id
                elif vote is VoteKind.VERY_LOW_YIELD:
                    assert not growth and yield_pct is not None
                    assert yield_pct < thresholds.total_yield_strong_caution_pct, case.case_id
                elif vote is VoteKind.STRONG_TREND_WORSENING:
                    assert trend is TrendClassification.STRONG_DOWNTREND, case.case_id
                elif vote is VoteKind.GROWTH_COLLAPSE:
                    assert growth and inputs.guidance_revision_disclosed, case.case_id
                    assert inputs.severe_earnings_decline, case.case_id
    assert seen == set(VoteKind)


def test_the_hard_overvalued_margin_is_applied_as_a_strict_boundary() -> None:
    """現行の設定の余白は 0 のため、余白を無視する変更は格子では区別できない。余白を持つ設定で、
    bull * (1 + 余白) ちょうどは hard_overvalued でなく、それを超えると hard_overvalued になる。"""
    judgment = pc.CONFIG.condition_based_judgment.model_copy(
        update={"timing_downgrade_block_margin_pct": 5.0}
    )
    config = pc.CONFIG.model_copy(update={"condition_based_judgment": judgment})
    cases = {c.case_id: c for c in pc.boundary_cases()}
    base = next(
        c
        for c in cases.values()
        if c.kwargs["condition_inputs"].fair_value_range is not None
        and c.kwargs["condition_inputs"].momentum is not None
        and c.kwargs["condition_inputs"].momentum.trend_classification
        is TrendClassification.UPTREND
        and c.kwargs["condition_inputs"].fair_value_range.bull == Decimal("1300")
        and c.kwargs["condition_inputs"].fair_value_range.usable_for_trading_judgment
        and c.kwargs["condition_inputs"].fair_value_range.overall_confidence
        is not ConfidenceLevel.LOW
    )
    edge = Decimal("1300") * Decimal("1.05")  # 1365
    outcomes = {}
    for label, price in (
        ("at_bull", Decimal("1300")),
        ("inside_margin", edge),
        ("above_margin", edge + Decimal("1")),
    ):
        kwargs = {**base.kwargs, "current_price": price, "config": config}
        result, trace = evaluate_profit_taking_traced(**kwargs)
        plain = evaluate_profit_taking(**kwargs)
        assert pc.digest(plain) == pc.digest(result)
        outcomes[label] = trace.hard_overvalued
    assert outcomes == {"at_bull": False, "inside_margin": False, "above_margin": True}


def test_the_grid_exercises_each_side_of_the_partial_and_full_reaching_conditions() -> None:
    """PARTIAL への到達条件 `partial_count >= min or fv_partial_gate_ok` の右の項と、適正価格の強い
    条件による FULL が、格子の中で『単独で』最終判定を決める case を持つ。持たないと、右の項を外す
    変更を characterization が捉えられない(#896 の独立 review の SHOULD-1)。"""
    gate_alone_partial = 0
    fair_value_strong_full = 0
    for _, result, trace in _TRACES:
        paths = {(c.path, c.level) for c in trace.candidates}
        gate = (CandidatePath.FAIR_VALUE_PARTIAL_GATE, 2) in paths
        conditions = (CandidatePath.PARTIAL_CONDITIONS, 2) in paths
        if (
            gate
            and not conditions
            and result.final_action.value == "PARTIAL_PROFIT_TAKE"
            and result.origin == "FAIR_VALUE_STRONG"
        ):
            gate_alone_partial += 1
        if (CandidatePath.FAIR_VALUE_STRONG, 3) in paths and result.origin == "FAIR_VALUE_STRONG":
            fair_value_strong_full += 1
    assert gate_alone_partial >= 20
    assert fair_value_strong_full >= 20
