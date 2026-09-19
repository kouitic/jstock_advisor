"""Issue #160 PR-1: 判断の安全条件の評価(純関数)。

本流未接続・純関数・「評価していない」と「該当なし」の区別を固定する。
時刻・営業日には触れない(TIME_SEMANTICS_IMPACT = NO)。
"""

from __future__ import annotations

import ast
import copy
import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import (
    BuyAction,
    ConfidenceLevel,
    EarningsDateStatus,
    RecommendationType,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.judgment_safety import (
    UNMEASURABLE_G3_INPUTS,
    CorporateActionFacts,
    ProfitTakingMitigationFacts,
    SafetyFacts,
    SafetyFinding,
    evaluate_safety_conditions,
)
from jstock_advisor.domain.signals.judgment_safety_shadow_config import (
    JudgmentSafetyShadowConfig,
    ShadowMode,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE = _REPO_ROOT / "src" / "jstock_advisor" / "domain" / "signals" / "judgment_safety.py"
_NOW = dt.datetime(2026, 9, 20, tzinfo=dt.UTC)
_CFG = JudgmentSafetyShadowConfig()
_BUY_FAMILY = [BuyAction.STRONG_BUY, BuyAction.BUY, BuyAction.SMALL_ENTRY]
_NON_STRONG_BUY = [
    BuyAction.WATCH_FOR_PRICE,
    BuyAction.WATCH_BEFORE_EARNINGS,
    BuyAction.MANUAL_REVIEW,
    BuyAction.NOT_ATTRACTIVE,
]
_NON_STRONG_TYPES = [
    RecommendationType.HOLD,
    RecommendationType.WATCH,
    RecommendationType.PARTIAL_PROFIT_TAKE,
    RecommendationType.SELL,
    RecommendationType.URGENT_REVIEW,
    RecommendationType.SELL_CONSIDERATION,
    RecommendationType.STRONG_SELL_CONSIDERATION,
    RecommendationType.URGENT_HOLDING_REVIEW,
    RecommendationType.MANUAL_REVIEW_REQUIRED,
]
_UNKNOWN_STATUSES = [EarningsDateStatus.UNAVAILABLE, EarningsDateStatus.STALE_PAST_DATE]


def _rec(
    *,
    recommendation_type: RecommendationType = RecommendationType.BUY,
    buy_action: BuyAction | None = None,
    earnings_date_status: EarningsDateStatus | None = None,
) -> Recommendation:
    return Recommendation(
        recommendation_id="rec-1",
        stock_code="0000",
        stock_name="銘柄A",
        recommended_at=_NOW,
        recommendation_type=recommendation_type,
        buy_action=buy_action,
        price_at_recommendation=Decimal("1000"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-test",
        earnings_date_status=earnings_date_status,
    )


def _buy(status: EarningsDateStatus | None = EarningsDateStatus.CONFIRMED) -> Recommendation:
    return _rec(buy_action=BuyAction.BUY, earnings_date_status=status)


def _full_take(status: EarningsDateStatus | None = EarningsDateStatus.CONFIRMED) -> Recommendation:
    return _rec(
        recommendation_type=RecommendationType.FULL_PROFIT_TAKE, earnings_date_status=status
    )


def _codes(rec: Recommendation, facts: SafetyFacts) -> list[str]:
    return [f.reason_code for f in evaluate_safety_conditions(rec, facts, _CFG).findings]


# ============================================================================
# G1: 決算日が不明のまま強い判定
# ============================================================================


@pytest.mark.parametrize("status", _UNKNOWN_STATUSES)
@pytest.mark.parametrize("action", _BUY_FAMILY)
def test_g1_buy_family_with_unknown_earnings_date_is_flagged(
    action: BuyAction, status: EarningsDateStatus
) -> None:
    rec = _rec(buy_action=action, earnings_date_status=status)

    assert _codes(rec, SafetyFacts()) == ["EARNINGS_DATE_UNKNOWN"]


@pytest.mark.parametrize("status", _UNKNOWN_STATUSES)
def test_g1_full_profit_take_with_unknown_earnings_date_is_flagged(
    status: EarningsDateStatus,
) -> None:
    assert _codes(_full_take(status), SafetyFacts()) == ["EARNINGS_DATE_UNKNOWN"]


def test_g1_confirmed_earnings_date_is_not_flagged() -> None:
    assert _codes(_buy(EarningsDateStatus.CONFIRMED), SafetyFacts()) == []
    assert _codes(_full_take(EarningsDateStatus.CONFIRMED), SafetyFacts()) == []


def test_g1_unset_status_is_not_evaluated_not_assumed_unknown() -> None:
    """earnings_date_statusが未設定(None)は「決算日が不明」と推測しない。not_evaluatedにする。"""
    result = evaluate_safety_conditions(_buy(None), SafetyFacts(), _CFG)

    assert result.findings == ()
    assert "G1" in result.not_evaluated


@pytest.mark.parametrize("action", _NON_STRONG_BUY)
def test_g1_non_strong_buy_actions_are_out_of_scope(action: BuyAction) -> None:
    rec = _rec(
        recommendation_type=RecommendationType.WATCH_BUY,
        buy_action=action,
        earnings_date_status=EarningsDateStatus.UNAVAILABLE,
    )

    result = evaluate_safety_conditions(rec, SafetyFacts(), _CFG)

    assert result.findings == ()
    assert result.not_evaluated == ()


@pytest.mark.parametrize("rtype", _NON_STRONG_TYPES)
def test_sell_side_and_other_types_are_never_flagged_q_b(rtype: RecommendationType) -> None:
    """SELL/URGENT等へは拡張しない(USER決定 Q-B)。全factsが最悪でも該当しない。"""
    facts = SafetyFacts(
        financials_are_stale=True,
        profit_taking_mitigation=ProfitTakingMitigationFacts(None, None),
        corporate_action=CorporateActionFacts("EVALUATED", ("SPLIT", "REVERSE_SPLIT")),
    )
    rec = _rec(recommendation_type=rtype, earnings_date_status=EarningsDateStatus.UNAVAILABLE)

    result = evaluate_safety_conditions(rec, facts, _CFG)

    assert result.findings == ()
    assert result.not_evaluated == ()


# ============================================================================
# G2: BUYの強い判定で財務STALE(BUYのみ・Q-D)
# ============================================================================


def test_g2_stale_financials_on_buy_is_flagged() -> None:
    assert _codes(_buy(), SafetyFacts(financials_are_stale=True)) == ["STALE_FINANCIALS"]


def test_g2_fresh_financials_are_not_flagged() -> None:
    result = evaluate_safety_conditions(_buy(), SafetyFacts(financials_are_stale=False), _CFG)

    assert result.findings == ()
    assert "G2" not in result.not_evaluated


def test_g2_unsupplied_fact_is_not_evaluated() -> None:
    assert "G2" in evaluate_safety_conditions(_buy(), SafetyFacts(), _CFG).not_evaluated


def test_g2_does_not_apply_to_profit_taking_q_d() -> None:
    """利確ではsafety conditionとして再使用しない(既存のconfidence減点/HIGH禁止で消費済み)。"""
    result = evaluate_safety_conditions(
        _full_take(), SafetyFacts(financials_are_stale=True), _CFG
    )

    assert "STALE_FINANCIALS" not in [f.reason_code for f in result.findings]
    assert "G2" not in result.not_evaluated


# ============================================================================
# G3: 利確FULLの緩和要因が不明(測定可能な2項目のいずれかがUNKNOWN。U1)
# ============================================================================

_YEARS = "REQUIRED_INPUT_MISSING:continuous_dividend_increase_years"
_POLICY = "REQUIRED_INPUT_MISSING:is_progressive_or_doe_policy"


@pytest.mark.parametrize(
    ("years", "policy", "expected"),
    [
        (None, True, [_YEARS]),
        (3, None, [_POLICY]),
        (None, None, [_YEARS, _POLICY]),
    ],
)
def test_g3_any_unknown_measurable_input_is_flagged(
    years: int | None, policy: bool | None, expected: list[str]
) -> None:
    facts = SafetyFacts(profit_taking_mitigation=ProfitTakingMitigationFacts(years, policy))

    assert _codes(_full_take(), facts) == expected


@pytest.mark.parametrize(("years", "policy"), [(0, False), (5, True), (0, True), (3, False)])
def test_g3_confirmed_values_including_zero_and_false_are_not_unknown(
    years: int, policy: bool
) -> None:
    """0年・Falseは「確認した結果」であり、UNKNOWNではない(FalseをUNKNOWNと推測しない)。"""
    facts = SafetyFacts(profit_taking_mitigation=ProfitTakingMitigationFacts(years, policy))

    assert _codes(_full_take(), facts) == []


def test_g3_unsupplied_facts_are_not_evaluated() -> None:
    result = evaluate_safety_conditions(_full_take(), SafetyFacts(), _CFG)

    assert "G3" in result.not_evaluated
    assert result.findings == ()


def test_g3_only_targets_inputs_listed_in_the_config() -> None:
    cfg = JudgmentSafetyShadowConfig(g3_required_inputs=("continuous_dividend_increase_years",))
    facts = SafetyFacts(profit_taking_mitigation=ProfitTakingMitigationFacts(3, None))

    result = evaluate_safety_conditions(_full_take(), facts, cfg)

    assert result.findings == ()  # is_progressive_or_doe_policy はconfigの対象外


def test_g3_does_not_apply_to_buy() -> None:
    facts = SafetyFacts(profit_taking_mitigation=ProfitTakingMitigationFacts(None, None))

    result = evaluate_safety_conditions(_buy(), facts, _CFG)

    assert result.findings == ()
    assert "G3" not in result.not_evaluated


def test_g3_unmeasurable_inputs_are_declared_and_never_counted() -> None:
    assert set(UNMEASURABLE_G3_INPUTS) == {
        "fair_value_rising_with_earnings_growth",
        "long_term_holding_benefit_imminent",
        "few_reinvestment_alternatives",
    }
    facts = SafetyFacts(profit_taking_mitigation=ProfitTakingMitigationFacts(None, None))

    codes = _codes(_full_take(), facts)

    assert codes  # 空でないこと(下のassertが自明に通らないため)
    assert not any(name in c for c in codes for name in UNMEASURABLE_G3_INPUTS)


# ============================================================================
# G4: 株式分割・併合が未解決(SPLIT / REVERSE_SPLITのみ・Q-A)
# ============================================================================


@pytest.mark.parametrize("kind", ["SPLIT", "REVERSE_SPLIT"])
@pytest.mark.parametrize("make", [_buy, _full_take])
def test_g4_unresolved_split_or_reverse_split_is_flagged(
    kind: str, make: Callable[[], Recommendation]
) -> None:
    facts = SafetyFacts(corporate_action=CorporateActionFacts("EVALUATED", (kind,)))  # type: ignore[arg-type]

    assert _codes(make(), facts) == [f"CORPORATE_ACTION_UNRESOLVED:{kind}"]


def test_g4_both_kinds_are_reported_once_each_in_stable_order() -> None:
    facts = SafetyFacts(
        corporate_action=CorporateActionFacts("EVALUATED", ("SPLIT", "REVERSE_SPLIT", "SPLIT"))
    )

    assert _codes(_buy(), facts) == [
        "CORPORATE_ACTION_UNRESOLVED:REVERSE_SPLIT",
        "CORPORATE_ACTION_UNRESOLVED:SPLIT",
    ]


def test_g4_evaluated_with_nothing_unresolved_is_clean() -> None:
    result = evaluate_safety_conditions(
        _buy(), SafetyFacts(corporate_action=CorporateActionFacts("EVALUATED", ())), _CFG
    )

    assert result.findings == ()
    assert "G4" not in result.not_evaluated


@pytest.mark.parametrize("state", ["NOT_EVALUATED", "COMPUTATION_FAILED"])
def test_g4_failed_or_not_evaluated_is_not_counted_as_a_finding(state: str) -> None:
    """取得・評価に失敗した銘柄は「未解決」ではなく「評価していない」(件数に含めない)。"""
    facts = SafetyFacts(corporate_action=CorporateActionFacts(state, ("SPLIT",)))  # type: ignore[arg-type]

    result = evaluate_safety_conditions(_buy(), facts, _CFG)

    assert result.findings == ()
    assert "G4" in result.not_evaluated


def test_g4_unsupplied_facts_are_not_evaluated() -> None:
    assert "G4" in evaluate_safety_conditions(_buy(), SafetyFacts(), _CFG).not_evaluated


# ============================================================================
# 純関数性・不変性・PIIなし・未接続
# ============================================================================


def test_findings_are_frozen_and_carry_only_condition_id_and_reason_code() -> None:
    finding = SafetyFinding("G1", "EARNINGS_DATE_UNKNOWN")

    with pytest.raises(AttributeError):
        finding.reason_code = "x"  # type: ignore[misc]
    assert set(vars(finding)) == {"condition_id", "reason_code", "would_suppress"}


def test_evaluation_contains_no_identifier_or_price_even_when_findings_exist() -> None:
    rec = _rec(buy_action=BuyAction.BUY, earnings_date_status=EarningsDateStatus.UNAVAILABLE)
    facts = SafetyFacts(
        financials_are_stale=True,
        corporate_action=CorporateActionFacts("EVALUATED", ("SPLIT",)),
    )

    result = evaluate_safety_conditions(rec, facts, _CFG)

    assert result.findings  # 空でないこと(下のassertが自明に通らないため)
    text = repr(result)
    assert "0000" not in text
    assert "銘柄A" not in text
    assert "1000" not in text


def test_evaluation_is_deterministic_and_does_not_mutate_inputs() -> None:
    rec = _buy(EarningsDateStatus.UNAVAILABLE)
    facts = SafetyFacts(
        financials_are_stale=True,
        corporate_action=CorporateActionFacts("EVALUATED", ("REVERSE_SPLIT", "SPLIT")),
    )
    rec_before, facts_before = rec.model_dump(), copy.deepcopy(facts)

    first = evaluate_safety_conditions(rec, facts, _CFG)
    second = evaluate_safety_conditions(rec, facts, _CFG)

    assert first == second
    assert rec.model_dump() == rec_before
    assert facts == facts_before


@pytest.mark.parametrize("mode", [ShadowMode.OFF, ShadowMode.SHADOW])
def test_result_does_not_depend_on_the_mode(mode: ShadowMode) -> None:
    """modeによる実行の有無は呼び出し側の責務。関数自体はmodeを参照しない。"""
    cfg = JudgmentSafetyShadowConfig(mode=mode)

    result = evaluate_safety_conditions(_buy(EarningsDateStatus.UNAVAILABLE), SafetyFacts(), cfg)

    assert [f.reason_code for f in result.findings] == ["EARNINGS_DATE_UNKNOWN"]


def _imported_top_level_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_module_is_pure_no_io_time_or_global_state_imports() -> None:
    forbidden = {"datetime", "time", "os", "logging", "boto3", "requests", "random", "pathlib"}

    assert _imported_top_level_modules(_MODULE) & forbidden == set()


def test_the_module_is_wired_only_into_the_facts_supply_of_buy_and_profit_taking() -> None:
    """参照してよいのは事実の供給側(buy_signal_service / profit_taking_service)のみ。

    PR-1は型と純関数だけで参照元0件だった。以降は、判定前の事実を`SafetyFacts`へ載せるため
    供給側が型だけをimportする(評価関数は呼ばない)。評価関数`evaluate_safety_conditions`を
    呼ぶのはPR-3(handler合流点)以降である。
    """
    referrers = sorted(
        p.relative_to(_REPO_ROOT).as_posix()
        for p in (_REPO_ROOT / "src").rglob("*.py")
        if p != _MODULE and "signals.judgment_safety import" in p.read_text(encoding="utf-8")
    )
    callers = [
        p.relative_to(_REPO_ROOT).as_posix()
        for p in (_REPO_ROOT / "src").rglob("*.py")
        if p != _MODULE and "evaluate_safety_conditions" in p.read_text(encoding="utf-8")
    ]

    assert referrers == [
        "src/jstock_advisor/services/buy_signal_service.py",
        "src/jstock_advisor/services/profit_taking_service.py",
    ]
    assert callers == []  # 評価は本流から呼ばれない(挙動不変)


def test_g3_duplicate_config_inputs_never_inflate_the_count() -> None:
    """同じ項目が複数回書かれても、G3の件数は変わらない(評価側の防御。設定側でも弾く)。"""
    cfg = JudgmentSafetyShadowConfig.model_construct(  # 検証を迂回して重複を注入する
        version=1,
        mode=ShadowMode.SHADOW,
        g3_required_inputs=(
            "continuous_dividend_increase_years",
            "continuous_dividend_increase_years",
            "is_progressive_or_doe_policy",
        ),
    )
    facts = SafetyFacts(profit_taking_mitigation=ProfitTakingMitigationFacts(None, None))

    result = evaluate_safety_conditions(_full_take(), facts, cfg)

    assert [f.reason_code for f in result.findings] == [_YEARS, _POLICY]


def test_every_finding_is_suppressible_by_default() -> None:
    """would_suppressの既定値(True)を固定する。

    「将来のenforcementで抑止対象になるか」を表す値であり、既定が静かにFalseへ反転すると、
    shadow集計で「抑止されうる件数」が0として観測される。全条件がvalidator型(抑止対象)である。
    """
    rec = _rec(
        recommendation_type=RecommendationType.FULL_PROFIT_TAKE,
        buy_action=BuyAction.BUY,
        earnings_date_status=EarningsDateStatus.UNAVAILABLE,
    )
    facts = SafetyFacts(
        financials_are_stale=True,
        profit_taking_mitigation=ProfitTakingMitigationFacts(None, None),
        corporate_action=CorporateActionFacts("EVALUATED", ("SPLIT",)),
    )

    findings = evaluate_safety_conditions(rec, facts, _CFG).findings

    assert {f.condition_id for f in findings} == {"G1", "G2", "G3", "G4"}
    assert all(f.would_suppress is True for f in findings)
    assert SafetyFinding("G1", "X").would_suppress is True
