"""L1 の verdict adapter(Issue #882 PR-1 = N6。dormant)の契約テスト。

## 何を固定するか(#882 の受入条件 AC-1〜AC-10 のうち PR-1 の範囲)

    AC-1 最終スコアだけで投資前提の悪化を判断しない(thesis_state は evidence から決める)
    AC-2 同義の採点機構を作らない(採点式・weight・threshold を adapter が持たない)
    AC-3 3 部品が重ねて見る同じ事実は root ごとに 1 票(冗長な部品を足しても強さが増えない)
    AC-4 fact_key の対応表: 17 ルール・7 理由コード・企業品質 10 項目・投資ストーリー 6 軸の全てが
         root × 事実に写る(写されないものが無い。root なしは明示)
    AC-5 総合利回りの軸は L1 の evidence を作らない(L2 の構成要素。UJ-1)
    AC-6 推定のみの根拠は FE-1 の独立根拠に単独で数えられない(M11)
    AC-8 UNDETERMINED と INTACT を区別する(不明は INTACT ではない)
    +   evaluate() / audit / thesis_service / providers に依存しない(読取専用の hidden write 禁止)
    +   primary_source_confirmed は、確認できた理由コードだけ True(不明を True にしない)

## 閾値について

ここで使う policy の数値は、**このテストの中だけの例示値**であり、運用の閾値ではない(値は事前登録 ->
replay -> USER 承認で確定する。module は既定値を持たない)。

## 何を固定しないか

AC-7(additive な保存 field)・AC-9(W-1 / W-2 の replay 報告)・AC-10(replay の限界の報告)は
PR-2 / 報告の範囲。arbiter・FULL の判定は N2。
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml

from jstock_advisor.domain.entities.enums import (
    BaselineOrigin,
    EvidenceCoverageStatus,
    ExecutionPlanReason,
    HoldingDecisionCategory,
    HoldingDecisionConfidenceLevel,
)
from jstock_advisor.domain.entities.holding_decision import (
    CompanyQualityScore,
    ComponentCoverage,
    HoldingDecisionHardGate,
    HoldingDecisionResult,
    InvestmentThesisScore,
    RiskDeductionScore,
    ScoreItemDetail,
)
from jstock_advisor.domain.exit_architecture import thesis_adapter
from jstock_advisor.domain.exit_architecture.decision import (
    ALLOWED_ROOTS_BY_KIND,
    ContractViolationError,
    FullEvidence,
)
from jstock_advisor.domain.exit_architecture.determination import Determination
from jstock_advisor.domain.exit_architecture.evidence import EvidenceStatus, distinct_roots
from jstock_advisor.domain.exit_architecture.thesis_adapter import (
    HARD_GATE_FACTS,
    ITEM_FACTS,
    NOT_REACHABLE_IN_PRODUCTION,
    RELIABILITY_BY_CONFIDENCE,
    SIGNAL_FACTS,
    ThesisAdaptation,
    ThesisMappingPolicy,
    adapt_holding_decision_to_thesis,
)
from jstock_advisor.domain.exit_architecture.verdicts import ThesisVerdict
from jstock_advisor.domain.exit_architecture.vocabulary import (
    FullEvidenceKind,
    ReliabilityClass,
    RootFactor,
    ThesisState,
    UndeterminedReason,
)
from jstock_advisor.domain.signals.holding_decision_hard_gate import _REASON_LABELS
from jstock_advisor.domain.signals.sell_signal import _RULE_EVIDENCE_GROUP

_ROOT = Path(__file__).resolve().parents[2]
_SRC = Path(thesis_adapter.__file__)
_NOW = dt.datetime(2026, 1, 5, 9, 0, tzinfo=dt.UTC)
_HIGH = HoldingDecisionConfidenceLevel.HIGH

#: このテストの中だけの例示値(運用の閾値ではない)
_POLICY = ThesisMappingPolicy(
    min_distinct_roots_for_weakening=2,
    item_shortfall_max_ratio=0.3,
    weakening_required_categories=None,
)

_L1_ROOTS = ALLOWED_ROOTS_BY_KIND[FullEvidenceKind.THESIS_DETERIORATION]


def _item(
    code: str,
    *,
    weight: float = 10.0,
    points: float = 0.0,
    status: EvidenceCoverageStatus = EvidenceCoverageStatus.EVALUATED,
) -> ScoreItemDetail:
    return ScoreItemDetail(
        item_code=code, axis=code, weight=weight, status=status, points_earned=points
    )


def _result(
    *,
    reasons: tuple[str, ...] = (),
    gate: tuple[str, ...] = (),
    cq_items: tuple[ScoreItemDetail, ...] = (),
    thesis_items: tuple[ScoreItemDetail, ...] = (),
    confidence: HoldingDecisionConfidenceLevel = _HIGH,
    category: HoldingDecisionCategory = HoldingDecisionCategory.HOLD,
    final_score: float = 50.0,
) -> HoldingDecisionResult:
    return HoldingDecisionResult(
        holding_decision_result_id="test-result-id",
        holding_id="test-holding-id",
        stock_code="0000",
        evaluated_at=_NOW,
        company_quality=CompanyQualityScore(score=30.0, coverage_ratio=1.0, items=cq_items),
        investment_thesis=InvestmentThesisScore(score=20.0, coverage_ratio=1.0, items=thesis_items),
        risk_deduction=RiskDeductionScore(score=90.0, coverage_ratio=1.0),
        base_score=final_score,
        hard_gate=HoldingDecisionHardGate(triggered=bool(gate), reason_codes=gate),
        final_score=final_score,
        display_value=int(final_score),
        category=category,
        coverage=ComponentCoverage(
            overall=1.0, company_quality=1.0, investment_thesis=1.0, risk_deduction=1.0
        ),
        confidence=confidence,
        should_notify=False,
        baseline_origin=BaselineOrigin.HUMAN_APPROVED,
        scoring_model_version=1,
        runtime_config_version=1,
        execution_plan_reason=ExecutionPlanReason.NORMAL_SHADOW,
        new_reason_codes=reasons,
    )


def _adapt(
    result: HoldingDecisionResult, policy: ThesisMappingPolicy = _POLICY
) -> ThesisAdaptation:
    return adapt_holding_decision_to_thesis(result, policy)


def _state(
    result: HoldingDecisionResult, policy: ThesisMappingPolicy = _POLICY
) -> Determination[ThesisState]:
    return _adapt(result, policy).verdict.thesis_state


# ---------------------------------------------------------------------------
# (1) AC-4 対応表の網羅と形(写されないものが無い)
# ---------------------------------------------------------------------------


def _yaml(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((_ROOT / "config" / name).read_text(encoding="utf-8"))
    return loaded


def test_signal_table_covers_every_e1_rule_exactly() -> None:
    assert set(SIGNAL_FACTS) == set(_RULE_EVIDENCE_GROUP)
    assert len(SIGNAL_FACTS) == 17


def test_signal_table_covers_every_risk_deduction_signal_in_the_config() -> None:
    config_signals = set(_yaml("holding_decision_risk_rules.yaml")["signals"])
    assert len(config_signals) == 16
    assert config_signals <= set(SIGNAL_FACTS)
    # E1 のルールのうち、リスク控除が再利用しないのは regulatory_capital_breach だけ
    assert set(SIGNAL_FACTS) - config_signals == {"regulatory_capital_breach"}


def test_hard_gate_table_covers_every_reason_code() -> None:
    assert set(HARD_GATE_FACTS) == set(_REASON_LABELS)
    assert len(HARD_GATE_FACTS) == 7


def test_item_table_covers_every_company_quality_item_and_thesis_axis() -> None:
    rules = _yaml("holding_decision_rules.yaml")
    cq = set(rules["company_quality_weights"])
    axes = set(rules["investment_thesis_weights"])
    assert len(cq) == 10
    assert len(axes) == 6
    assert set(ITEM_FACTS) == cq | axes


def _all_specs() -> list[tuple[str, thesis_adapter.FactSpec]]:
    return [
        (f"{table_name}:{name}", spec)
        for table_name, table in (
            ("signal", SIGNAL_FACTS),
            ("hard_gate", HARD_GATE_FACTS),
            ("item", ITEM_FACTS),
        )
        for name, spec in table.items()
    ]


def test_every_root_is_an_l1_non_price_root_or_explicitly_none() -> None:
    for name, spec in _all_specs():
        assert spec.root is None or spec.root in _L1_ROOTS, name
        assert spec.root not in (RootFactor.PRICE_PATH, RootFactor.USER_DIRECTIVE), name


def test_the_supporting_only_inputs_are_exactly_the_documented_ones() -> None:
    none_root = {name for name, spec in _all_specs() if spec.root is None}
    assert none_root == {
        "signal:investment_premise_broken",
        "signal:regulatory_capital_breach",
        "hard_gate:INVESTMENT_THESIS_COLLAPSE",
        "item:total_yield",
        "item:custom_conditions",
    }


def test_every_fact_key_names_its_root() -> None:
    """fact_key は『root:事実:向き』。root と接頭辞が食い違わない(root なしは SUPPORTING)。"""
    for name, spec in _all_specs():
        prefix = "SUPPORTING" if spec.root is None else spec.root.value
        assert spec.fact_key.split(":")[0] == prefix, name


def test_the_return_policy_events_share_an_event_id_per_economic_event() -> None:
    dividend = {name for name, spec in _all_specs() if spec.event_id == "RETURN_POLICY:dividend"}
    benefit = {name for name, spec in _all_specs() if spec.event_id == "RETURN_POLICY:benefit"}
    assert dividend == {
        "signal:dividend_cut",
        "signal:dividend_omission",
        "signal:unfavorable_dividend_policy_change",
        "hard_gate:DIVIDEND_OMISSION_AND_CASHFLOW_CRISIS",
        "item:dividend_policy",
    }
    # long_term_holding_condition_unfavorable_change は benefit_condition の入力ではない(X-2)ので
    # event_id を共有しない
    assert benefit == {
        "signal:shareholder_benefit_abolished",
        "signal:shareholder_benefit_major_downgrade",
        "item:benefit_condition",
    }
    # それ以外の事象は event_id を持たない
    assert all(
        spec.event_id is None for name, spec in _all_specs() if name not in dividend | benefit
    )


def test_the_unreachable_inputs_are_fixed_and_still_mapped() -> None:
    """現行の Production で成立しない入力(コードの読み。X-1〜: TARO の独立検証)。
    provider や配線が変わって到達しうるようになったら、この集合と表を見直す合図。
    到達しない入力にも対応は持つ(将来成立したときに写されないものを作らない)。"""
    tables = {"signal": SIGNAL_FACTS, "hard_gate": HARD_GATE_FACTS, "item": ITEM_FACTS}
    assert set(NOT_REACHABLE_IN_PRODUCTION) == set(tables)
    for key, names in NOT_REACHABLE_IN_PRODUCTION.items():
        assert names <= set(tables[key]), key
    assert NOT_REACHABLE_IN_PRODUCTION["hard_gate"] == {
        "DEBT_EXCESS",
        "GOING_CONCERN_DOUBT",
        "DIVIDEND_OMISSION_AND_CASHFLOW_CRISIS",
    }
    assert NOT_REACHABLE_IN_PRODUCTION["item"] == {"governance_going_concern"}
    assert len(NOT_REACHABLE_IN_PRODUCTION["signal"]) == 9
    # 到達しうる hard gate は開示キーワード経路(#888 / #889)と点数ベースだけ
    reachable_gates = set(HARD_GATE_FACTS) - NOT_REACHABLE_IN_PRODUCTION["hard_gate"]
    assert reachable_gates == {
        "BANKRUPTCY_FILING",
        "DELISTING_OR_KANRI",
        "ACCOUNTING_FRAUD",
        "INVESTMENT_THESIS_COLLAPSE",
    }


def test_the_facts_that_are_measured_differently_have_different_fact_keys() -> None:
    """同じ系列でも測る事実が違うものは別の fact_key(X-3 / X-4)。同じ事実のものは同じ fact_key。"""
    streak = ITEM_FACTS["cash_generation_cf_streak"].fact_key
    assert streak != SIGNAL_FACTS["continuous_operating_cashflow_decline"].fact_key
    instab = ITEM_FACTS["stability_operating_income"].fact_key
    assert instab != SIGNAL_FACTS["continuous_operating_income_decline"].fact_key
    words = ITEM_FACTS["governance_listing_risk"].fact_key
    assert words != SIGNAL_FACTS["listing_maintenance_risk"].fact_key
    # 同じ事実: is_debt_excess(自己資本比率 < 0)と balance_sheet_insolvency の SUSPECTED の条件
    insolvency = SIGNAL_FACTS["balance_sheet_insolvency"].fact_key
    assert ITEM_FACTS["financial_health_debt_excess"].fact_key == insolvency
    assert HARD_GATE_FACTS["DEBT_EXCESS"].fact_key == insolvency
    assert (
        ITEM_FACTS["governance_going_concern"].fact_key
        == HARD_GATE_FACTS["GOING_CONCERN_DOUBT"].fact_key
    )


# ---------------------------------------------------------------------------
# (2) primary_source_confirmed: 確認できたものだけ True(不明を True にしない)
# ---------------------------------------------------------------------------


def test_primary_source_is_true_only_for_the_confirmed_inputs() -> None:
    confirmed = {name for name, spec in _all_specs() if spec.primary_source_confirmed}
    assert confirmed == {
        # E1 のルールが自分で『公式発表 / 登録簿』と判定する 4 本だけ。ハードゲートの理由コードは、
        # #888 / #889(キーワード経路の確認の質)の方針が出るまで全て False
        "signal:dividend_cut",
        "signal:dividend_omission",
        "signal:shareholder_benefit_abolished",
        "signal:shareholder_benefit_major_downgrade",
    }
    assert not any(spec.primary_source_confirmed for spec in HARD_GATE_FACTS.values())


@pytest.mark.parametrize(
    "name",
    [
        "signal:major_scandal",
        "signal:accounting_problem",
        "signal:listing_maintenance_risk",
        "hard_gate:BANKRUPTCY_FILING",
        "hard_gate:DELISTING_OR_KANRI",
        "hard_gate:ACCOUNTING_FRAUD",
        "hard_gate:GOING_CONCERN_DOUBT",
        "hard_gate:INVESTMENT_THESIS_COLLAPSE",
        "hard_gate:DEBT_EXCESS",
        "hard_gate:DIVIDEND_OMISSION_AND_CASHFLOW_CRISIS",
    ],
)
def test_keyword_flag_and_score_based_inputs_are_never_primary_confirmed(name: str) -> None:
    """キーワード一致のみでも E1 の rule は primary = True になる(F-2)/ snapshot の判定フラグ /
    点数ベース。保存済みの結果から確認の段階を区別できないため False。"""
    specs = dict(_all_specs())
    assert specs[name].primary_source_confirmed is False


def test_item_derived_evidence_is_always_suspected_and_never_primary() -> None:
    for name, spec in ITEM_FACTS.items():
        if spec.root is not None:
            assert spec.status is EvidenceStatus.SUSPECTED, name
            assert spec.primary_source_confirmed is False, name


def test_signal_and_hard_gate_evidence_is_triggered() -> None:
    for table in (SIGNAL_FACTS, HARD_GATE_FACTS):
        for name, spec in table.items():
            assert spec.status is EvidenceStatus.TRIGGERED, name


# ---------------------------------------------------------------------------
# (3) thesis_state: BROKEN / UNDETERMINED / WEAKENING / INTACT
# ---------------------------------------------------------------------------


def test_a_hard_gate_makes_the_thesis_broken_with_its_reasons() -> None:
    adaptation = _adapt(_result(gate=("DEBT_EXCESS",)))
    verdict = adaptation.verdict
    assert verdict.thesis_state.value is ThesisState.BROKEN
    assert verdict.hard_gate_triggered is True
    assert verdict.hard_gate_reasons == ("DEBT_EXCESS",)
    assert [e.fact_key for e in verdict.evidence] == ["BALANCE_SHEET:insolvency"]
    assert verdict.evidence[0].primary_source_confirmed is False


def test_insufficient_evidence_without_a_hard_gate_is_undetermined_not_intact() -> None:
    state = _state(_result(confidence=HoldingDecisionConfidenceLevel.INSUFFICIENT_EVIDENCE))
    assert not state.is_determined
    assert state.reason is UndeterminedReason.COVERAGE_INSUFFICIENT


def test_a_hard_gate_stands_even_with_insufficient_evidence() -> None:
    """一次情報で確認できたハードゲートがある場合は、信頼度が不足していても BROKEN。"""
    verdict = _adapt(
        _result(
            gate=("DIVIDEND_OMISSION_AND_CASHFLOW_CRISIS",),
            confidence=HoldingDecisionConfidenceLevel.INSUFFICIENT_EVIDENCE,
        )
    ).verdict
    assert verdict.thesis_state.value is ThesisState.BROKEN
    assert verdict.reliability is ReliabilityClass.UNUSABLE


def test_the_reliability_mapping_is_fixed() -> None:
    assert RELIABILITY_BY_CONFIDENCE == {
        HoldingDecisionConfidenceLevel.HIGH: ReliabilityClass.RELIABLE,
        HoldingDecisionConfidenceLevel.MEDIUM: ReliabilityClass.RELIABLE,
        HoldingDecisionConfidenceLevel.LOW: ReliabilityClass.DEGRADED,
        HoldingDecisionConfidenceLevel.INSUFFICIENT_EVIDENCE: ReliabilityClass.UNUSABLE,
    }
    assert set(RELIABILITY_BY_CONFIDENCE) == set(HoldingDecisionConfidenceLevel)
    for confidence, expected in RELIABILITY_BY_CONFIDENCE.items():
        assert _adapt(_result(confidence=confidence)).verdict.reliability is expected


def test_two_distinct_non_price_roots_make_the_thesis_weakening() -> None:
    state = _state(
        _result(
            reasons=("continuous_operating_income_decline", "continuous_operating_cashflow_decline")
        )
    )
    assert state.value is ThesisState.WEAKENING


def test_one_root_is_not_enough_for_weakening() -> None:
    assert _state(_result(reasons=("continuous_operating_income_decline",))).value is (
        ThesisState.INTACT
    )


def test_the_same_root_counts_once_however_many_rules_fire() -> None:
    """EARNINGS の根拠が 2 件(営業利益の連続減少 + 業績予想の下方修正)でも root は 1(R-A)。"""
    result = _result(
        reasons=("continuous_operating_income_decline", "large_earnings_guidance_downgrade")
    )
    adaptation = _adapt(result)
    assert len(adaptation.verdict.evidence) == 2
    assert distinct_roots(adaptation.verdict.evidence) == {RootFactor.EARNINGS}
    assert adaptation.verdict.thesis_state.value is ThesisState.INTACT


def test_the_threshold_of_distinct_roots_is_the_policys() -> None:
    reasons = (
        "continuous_operating_income_decline",
        "continuous_operating_cashflow_decline",
        "financial_health_severe_deterioration",
    )
    for needed, expected in ((1, ThesisState.WEAKENING), (3, ThesisState.WEAKENING)):
        policy = dataclasses.replace(_POLICY, min_distinct_roots_for_weakening=needed)
        assert _state(_result(reasons=reasons), policy).value is expected
    policy4 = dataclasses.replace(_POLICY, min_distinct_roots_for_weakening=4)
    assert _state(_result(reasons=reasons), policy4).value is ThesisState.INTACT


def test_suspected_item_shortfalls_alone_never_make_the_thesis_weakening() -> None:
    """水準の不足は推定(SUSPECTED)の補助 evidence。独立根拠に数えない(M11 / AC-6)。"""
    result = _result(
        cq_items=(
            _item("financial_health_equity_ratio"),
            _item("cash_generation_cf_income_ratio"),
            _item("profitability_roe"),
        ),
        thesis_items=(_item("dividend_policy"), _item("benefit_condition")),
    )
    adaptation = _adapt(result)
    assert len(adaptation.verdict.evidence) == 5
    assert all(e.status is EvidenceStatus.SUSPECTED for e in adaptation.verdict.evidence)
    assert distinct_roots(adaptation.verdict.evidence) == frozenset()
    assert adaptation.verdict.thesis_state.value is ThesisState.INTACT


# ---------------------------------------------------------------------------
# (4) AC-1 最終スコアだけで判断しない / W-1・W-2
# ---------------------------------------------------------------------------

_EVIDENCE_REASONS = ("continuous_operating_income_decline", "continuous_operating_cashflow_decline")


@pytest.mark.parametrize("final_score", [-100.0, -30.0, 0.0, 40.0, 70.0, 100.0])
def test_the_final_score_alone_does_not_change_the_thesis_state(final_score: float) -> None:
    weak = _result(reasons=_EVIDENCE_REASONS, final_score=final_score)
    intact = _result(reasons=(), final_score=final_score)
    assert _state(weak).value is ThesisState.WEAKENING
    assert _state(intact).value is ThesisState.INTACT


@pytest.mark.parametrize("category", list(HoldingDecisionCategory))
def test_w1_does_not_use_the_judgment_category_as_a_condition(
    category: HoldingDecisionCategory,
) -> None:
    assert _state(_result(reasons=_EVIDENCE_REASONS, category=category)).value is (
        ThesisState.WEAKENING
    )
    assert _state(_result(reasons=(), category=category)).value is ThesisState.INTACT


def test_w2_requires_the_category_in_addition_to_the_evidence() -> None:
    bands = frozenset(
        {
            HoldingDecisionCategory.SELL_WATCH,
            HoldingDecisionCategory.SELL_CONSIDERATION,
        }
    )
    w2 = dataclasses.replace(_POLICY, weakening_required_categories=bands)
    inside = _result(reasons=_EVIDENCE_REASONS, category=HoldingDecisionCategory.SELL_WATCH)
    outside = _result(reasons=_EVIDENCE_REASONS, category=HoldingDecisionCategory.HOLD)
    no_evidence = _result(reasons=(), category=HoldingDecisionCategory.SELL_WATCH)
    assert _state(inside, w2).value is ThesisState.WEAKENING
    assert _state(outside, w2).value is ThesisState.INTACT
    assert _state(no_evidence, w2).value is ThesisState.INTACT


def test_the_category_never_overrides_a_hard_gate_or_undetermined() -> None:
    bands = frozenset({HoldingDecisionCategory.SELL_WATCH})
    w2 = dataclasses.replace(_POLICY, weakening_required_categories=bands)
    gate = _result(gate=("DEBT_EXCESS",), category=HoldingDecisionCategory.HOLD)
    assert _state(gate, w2).value is ThesisState.BROKEN
    unknown = _result(
        category=HoldingDecisionCategory.HOLD,
        confidence=HoldingDecisionConfidenceLevel.INSUFFICIENT_EVIDENCE,
    )
    assert not _state(unknown, w2).is_determined


# ---------------------------------------------------------------------------
# (5) AC-3 / AC-5 重ね合わせは root ごとに 1 票・総合利回りは evidence を作らない
# ---------------------------------------------------------------------------


def test_the_same_fact_seen_by_two_parts_collapses_into_one_evidence() -> None:
    """同じ事実(fact_key が同じ)を 2 つの部品が見ているとき、1 件にまとまる(R-D)。
    ハードゲート DEBT_EXCESS と企業品質の debt_excess は『自己資本比率 < 0』という同じ事実。
    TRIGGERED(ハードゲート)が SUSPECTED(水準の不足)に勝つ。"""
    result = _result(gate=("DEBT_EXCESS",), cq_items=(_item("financial_health_debt_excess"),))
    evidence = _adapt(result).verdict.evidence
    assert [e.fact_key for e in evidence] == ["BALANCE_SHEET:insolvency"]
    assert evidence[0].status is EvidenceStatus.TRIGGERED


def test_a_series_seen_through_different_facts_still_counts_as_one_root() -> None:
    """同じ四半期営業利益の系列でも、測る事実が違えば fact_key は別(2 件)。ただし root は 1(R-A)で、
    独立根拠の数(root 単位)は増えない。"""
    result = _result(
        reasons=("continuous_operating_income_decline",),
        cq_items=(_item("stability_operating_income"),),
    )
    evidence = _adapt(result).verdict.evidence
    assert len(evidence) == 2
    assert distinct_roots(evidence) == {RootFactor.EARNINGS}
    assert _state(result).value is ThesisState.INTACT


def test_redundant_parts_never_add_independent_roots() -> None:
    base = _result(reasons=("continuous_operating_income_decline",))
    redundant = _result(
        reasons=("continuous_operating_income_decline",),
        cq_items=(
            _item("stability_operating_income"),
            _item("profitability_roe"),
            _item("stability_deficit"),
        ),
        thesis_items=(_item("profit_cf_premise"),),
    )
    assert distinct_roots(_adapt(redundant).verdict.evidence) == distinct_roots(
        _adapt(base).verdict.evidence
    )
    assert _state(redundant).value is _state(base).value


def test_the_dividend_facts_share_one_economic_event() -> None:
    evidence = _adapt(
        _result(
            reasons=("dividend_cut", "dividend_omission"),
            thesis_items=(_item("dividend_policy"),),
        )
    ).verdict.evidence
    assert {e.event_id for e in evidence} == {"RETURN_POLICY:dividend"}
    assert distinct_roots(evidence) == {RootFactor.RETURN_POLICY}


def test_total_yield_and_custom_conditions_create_no_evidence() -> None:
    adaptation = _adapt(
        _result(thesis_items=(_item("total_yield"), _item("custom_conditions"))),
    )
    assert adaptation.verdict.evidence == ()
    assert adaptation.supporting_only == ("total_yield", "custom_conditions")


def test_supporting_only_inputs_are_recorded_not_silently_dropped() -> None:
    adaptation = _adapt(
        _result(
            reasons=("investment_premise_broken", "regulatory_capital_breach"),
            gate=("INVESTMENT_THESIS_COLLAPSE",),
        )
    )
    assert adaptation.verdict.evidence == ()
    assert set(adaptation.supporting_only) == {
        "investment_premise_broken",
        "regulatory_capital_breach",
        "INVESTMENT_THESIS_COLLAPSE",
    }
    assert adaptation.unmapped == ()


def test_unknown_names_are_reported_not_raised_and_create_no_evidence() -> None:
    adaptation = _adapt(_result(reasons=("a_rule_added_later",), gate=("A_REASON_ADDED_LATER",)))
    assert adaptation.verdict.evidence == ()
    assert set(adaptation.unmapped) == {"a_rule_added_later", "A_REASON_ADDED_LATER"}


def test_a_thesis_collapse_alone_is_broken_but_gives_no_fe1_evidence() -> None:
    """F-3: 点数ベースの INVESTMENT_THESIS_COLLAPSE だけの BROKEN は root の根拠が無く、
    FE-1 の FullEvidence を構築できない(『BROKEN なのに FE-1 でない』ケース)。"""
    verdict = _adapt(_result(gate=("INVESTMENT_THESIS_COLLAPSE",))).verdict
    assert verdict.thesis_state.value is ThesisState.BROKEN
    with pytest.raises(ContractViolationError):
        FullEvidence(FullEvidenceKind.THESIS_DETERIORATION, verdict.evidence, thesis=verdict)


# ---------------------------------------------------------------------------
# (6) 評価軸の不足の判定
# ---------------------------------------------------------------------------


def test_the_shortfall_boundary_is_inclusive() -> None:
    at_boundary = _result(cq_items=(_item("profitability_roe", weight=10.0, points=3.0),))
    above = _result(cq_items=(_item("profitability_roe", weight=10.0, points=3.1),))
    assert len(_adapt(at_boundary).verdict.evidence) == 1
    assert _adapt(above).verdict.evidence == ()


@pytest.mark.parametrize(
    "status", [EvidenceCoverageStatus.NOT_EVALUATED, EvidenceCoverageStatus.NOT_APPLICABLE]
)
def test_items_that_were_not_evaluated_create_no_evidence(
    status: EvidenceCoverageStatus,
) -> None:
    """評価できなかった軸は根拠にも否定にも数えない。"""
    result = _result(cq_items=(_item("profitability_roe", points=0.0, status=status),))
    assert _adapt(result).verdict.evidence == ()


def test_an_item_with_no_weight_is_ignored() -> None:
    result = _result(cq_items=(_item("profitability_roe", weight=0.0, points=0.0),))
    assert _adapt(result).verdict.evidence == ()


# ---------------------------------------------------------------------------
# (7) 出力は C0 の型と整合する / 決定的
# ---------------------------------------------------------------------------


def test_the_output_is_a_thesis_verdict_that_builds_a_full_evidence_when_it_should() -> None:
    verdict = _adapt(_result(gate=("DIVIDEND_OMISSION_AND_CASHFLOW_CRISIS",))).verdict
    assert isinstance(verdict, ThesisVerdict)
    built = FullEvidence(FullEvidenceKind.THESIS_DETERIORATION, verdict.evidence, thesis=verdict)
    assert built.evidence[0].root_factor is RootFactor.RETURN_POLICY


def test_a_weakening_verdict_builds_a_full_evidence_from_independent_roots() -> None:
    verdict = _adapt(_result(reasons=_EVIDENCE_REASONS)).verdict
    built = FullEvidence(FullEvidenceKind.THESIS_DETERIORATION, verdict.evidence, thesis=verdict)
    assert distinct_roots(built.evidence) == {RootFactor.EARNINGS, RootFactor.CASHFLOW}


def test_an_intact_verdict_never_builds_a_full_evidence() -> None:
    verdict = _adapt(_result(reasons=("continuous_operating_income_decline",))).verdict
    with pytest.raises(ContractViolationError):
        FullEvidence(FullEvidenceKind.THESIS_DETERIORATION, verdict.evidence, thesis=verdict)


def test_the_result_is_deterministic_and_order_independent() -> None:
    a = _result(
        reasons=("continuous_operating_income_decline", "dividend_cut"),
        cq_items=(_item("profitability_roe"), _item("stability_deficit")),
    )
    b = _result(
        reasons=("dividend_cut", "continuous_operating_income_decline"),
        cq_items=(_item("stability_deficit"), _item("profitability_roe")),
    )
    assert _adapt(a) == _adapt(a)
    assert _state(a) == _state(b)
    assert distinct_roots(_adapt(a).verdict.evidence) == distinct_roots(_adapt(b).verdict.evidence)
    assert {e.fact_key for e in _adapt(a).verdict.evidence} == {
        e.fact_key for e in _adapt(b).verdict.evidence
    }


def test_the_result_input_is_not_modified() -> None:
    result = _result(reasons=_EVIDENCE_REASONS)
    before = result.model_dump()
    _adapt(result)
    assert result.model_dump() == before


# ---------------------------------------------------------------------------
# (8) policy: 既定値なし・検証
# ---------------------------------------------------------------------------


def test_policy_has_no_default_values() -> None:
    for field in dataclasses.fields(ThesisMappingPolicy):
        assert field.default is dataclasses.MISSING
        assert field.default_factory is dataclasses.MISSING


@pytest.mark.parametrize("bad", [0, -1])
def test_policy_rejects_a_non_positive_root_count(bad: int) -> None:
    with pytest.raises(ValueError):
        ThesisMappingPolicy(bad, 0.3, None)


def test_policy_rejects_a_non_integer_root_count() -> None:
    with pytest.raises(TypeError):
        ThesisMappingPolicy(True, 0.3, None)
    with pytest.raises(TypeError):
        ThesisMappingPolicy(2.0, 0.3, None)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [-0.01, 1.01, float("nan"), float("inf")])
def test_policy_rejects_a_shortfall_ratio_outside_zero_to_one(bad: float) -> None:
    with pytest.raises(ValueError):
        ThesisMappingPolicy(2, bad, None)


def test_policy_rejects_an_empty_w2_category_set() -> None:
    with pytest.raises(ValueError):
        ThesisMappingPolicy(2, 0.3, frozenset())


# ---------------------------------------------------------------------------
# (9) AC-2 採点機構を作らない・読取専用(hidden write 禁止)
# ---------------------------------------------------------------------------


def _tree() -> ast.Module:
    return ast.parse(_SRC.read_text(encoding="utf-8"))


def test_the_module_defines_no_scoring_constants() -> None:
    """採点式・weight・threshold を持たない: 小数リテラルが無く、整数は 0 / 1 のみ。"""
    floats = [
        n.lineno
        for n in ast.walk(_tree())
        if isinstance(n, ast.Constant) and isinstance(n.value, float)
    ]
    assert floats == []
    integers = {
        n.value
        for n in ast.walk(_tree())
        if isinstance(n, ast.Constant)
        and isinstance(n.value, int)
        and not isinstance(n.value, bool)
    }
    assert integers <= {0, 1}


_FORBIDDEN_NAMES = {
    "evaluate",
    "HoldingDecisionService",
    "AuditService",
    "InvestmentThesisService",
    "ProviderBundle",
    "build_stock_snapshot",
    "score_company_quality",
    "score_investment_thesis",
    "score_risk_deduction",
    "evaluate_hard_gate",
    "combine_holding_decision",
    "get_or_create_thesis",
    "activate_baseline",
}


def test_the_module_never_references_the_service_or_the_scoring_functions() -> None:
    """HoldingDecisionService.evaluate() は外部読取・AuditLog 書込・baseline 作成の副作用を持つ。
    採点関数を呼ぶ = 新しい採点を行う。どちらも参照しない。"""
    tree = _tree()
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not names & _FORBIDDEN_NAMES


def test_the_module_imports_only_entities_types_and_the_package() -> None:
    imported = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
    external = {m for m in imported if m.startswith("jstock_advisor.")}
    assert {m for m in external if not m.startswith("jstock_advisor.domain.exit_architecture")} == {
        "jstock_advisor.domain.entities.enums",
        "jstock_advisor.domain.entities.holding_decision",
    }
    assert not any(
        m.split(".")[1] in {"services", "infrastructure", "lambda_handlers", "providers"}
        for m in external
    )


def test_the_module_has_no_case_specific_code() -> None:
    assert "9536" not in _SRC.read_text(encoding="utf-8")


def test_the_thesis_verdict_docstring_no_longer_claims_every_hard_gate_is_primary_confirmed() -> (
    None
):
    doc = ThesisVerdict.__doc__ or ""
    assert "一律に言えず" in doc
    assert "一次情報で確認できた場合のみ(既存の仕様)" not in doc
