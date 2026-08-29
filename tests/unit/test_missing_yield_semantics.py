"""総合利回りの欠測semantics(Issue #55 Phase B-1)。

「値が0である」と「値が不明である」を分離した結果、保有判断のcoverage gateが
正しく機能すること、利確の利回り条件が捏造された0.00%で成立しないこと、
表示層が断定的な0.00%を出さないことを固定する。

本Issueの目的は「欠測を採点対象から除外すること」ではない。
NOT_EVALUATEDはdenominator(available_weight)に残り points=0 として計上される
既存契約を維持したまま、coverage_ratioを正しく下げてgateを機能させることが目的
(Phase A Decision 3)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EvidenceCoverageStatus,
    RecommendationType,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.investment_thesis_scoring import (
    InvestmentThesisInputs,
    score_investment_thesis,
)
from jstock_advisor.domain.signals.profit_taking import (
    MitigatingFactorInputs,
    ProfitTakingConditionInputs,
    evaluate_profit_taking,
)
from jstock_advisor.domain.valuation.yield_calc import BenefitProgramState

_CFG = load_config()
_WEIGHTS = _CFG.holding_decision.investment_thesis_weights
_TEMPLATE = _CFG.investment_thesis_template
_FRESH = _CFG.holding_decision.fresh_within_days
_STALE = _CFG.holding_decision.stale_after_days
_NOW = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)


def _inputs(**overrides) -> InvestmentThesisInputs:
    base = dict(
        current_total_yield_pct=_TEMPLATE.min_total_yield_pct,
        has_shareholder_benefit=True,
        benefit_abolished_or_downgraded=False,
        dividend_cut_or_omission_confirmed=False,
        profit_cf_premise_broken=False,
        financial_premise_broken=False,
        thesis=None,
    )
    base.update(overrides)
    return InvestmentThesisInputs(**base)


def _score(**overrides):
    return score_investment_thesis(
        _inputs(**overrides), _WEIGHTS, _TEMPLATE, _FRESH, _STALE, _NOW
    )


def _item(result, code: str):
    return next(i for i in result.items if i.item_code == code)


# --- coverage semantics ------------------------------------------------------


def test_known_total_yield_is_evaluated() -> None:
    item = _item(_score(current_total_yield_pct=3.7), "total_yield")
    assert item.status == EvidenceCoverageStatus.EVALUATED


def test_confirmed_zero_total_yield_is_evaluated_not_missing() -> None:
    """確定0%は「不明」ではない。従来どおり採点され、coverageも下がらない。"""
    zero = _score(current_total_yield_pct=0.0)
    known = _score(current_total_yield_pct=3.7)

    item = _item(zero, "total_yield")
    assert item.status == EvidenceCoverageStatus.EVALUATED
    assert item.points_earned == 0.0
    assert zero.coverage_ratio == known.coverage_ratio


def test_unknown_total_yield_is_not_evaluated_and_lowers_coverage() -> None:
    """欠測はNOT_EVALUATEDとなり coverage_ratio を下げる(=gateが機能する)。

    修正前は compute_total_yield_pct が None を 0.0 へ潰していたため、
    この分岐は到達不能な死んだコードだった。
    """
    known = _score(current_total_yield_pct=3.7)
    unknown = _score(current_total_yield_pct=None)

    assert _item(unknown, "total_yield").status == EvidenceCoverageStatus.NOT_EVALUATED
    assert unknown.coverage_ratio < known.coverage_ratio


def test_not_evaluated_stays_in_denominator_existing_contract() -> None:
    """既存契約の回帰(Decision 3): NOT_EVALUATED は denominator に残るため、
    確定0%と欠測でスコアは同一になる。差が出るのは coverage のみ。

    この契約を変更することは Issue #55 のスコープ外。
    """
    zero = _score(current_total_yield_pct=0.0)
    unknown = _score(current_total_yield_pct=None)
    assert zero.score == unknown.score
    assert unknown.coverage_ratio < zero.coverage_ratio


def test_no_benefit_program_alone_does_not_degrade_coverage() -> None:
    """Decision 6: 「優待制度なし」だけを理由に coverage が下がってはならない。

    優待制度が無い銘柄は市場の大多数であり、これを欠測扱いすると
    confidence が市場全体で一斉降格する。制度なしは benefit_condition が
    NOT_APPLICABLE になるだけで、total_yield は配当のみで EVALUATED となる。
    """
    with_benefit = _score(has_shareholder_benefit=True, current_total_yield_pct=3.7)
    without_benefit = _score(
        has_shareholder_benefit=False,
        benefit_abolished_or_downgraded=None,
        current_total_yield_pct=3.7,
    )

    assert _item(without_benefit, "total_yield").status == EvidenceCoverageStatus.EVALUATED
    assert without_benefit.coverage_ratio == with_benefit.coverage_ratio == 1.0


# --- profit taking -----------------------------------------------------------


def _evaluate(total_yield: float | None):
    return evaluate_profit_taking(
        current_price=Decimal("1200"),  # 含み益あり(利確判定が成立する前提)
        average_purchase_price=Decimal("1000"),
        shares=100,
        total_purchase_amount=Decimal("100000"),
        cumulative_dividend_received=Decimal("0"),
        cumulative_benefit_value_received=Decimal("0"),
        current_total_yield_pct=total_yield,
        forecast_annual_dividend_per_share=Decimal("10"),
        mitigating_inputs=MitigatingFactorInputs(),
        config=_CFG.profit_taking,
        condition_inputs=ProfitTakingConditionInputs(),
    )


def _yield_reasons(result) -> list[str]:
    return [r for r in result.triggered_reasons if "総合利回り" in r]


def test_unknown_total_yield_does_not_produce_fabricated_low_yield_reason() -> None:
    """欠測時に「総合利回りが0.00%まで低下」という捏造された根拠を出さない。

    修正前は None が 0.0 へ潰れ、0.00 < 2.50 が成立して実際にこの文言が
    ユーザーへ通知されていた。
    """
    result = _evaluate(None)
    assert _yield_reasons(result) == []
    assert not any("0.00%" in r for r in result.triggered_reasons)


def test_known_low_total_yield_still_produces_reason() -> None:
    """既存挙動の回帰: 既知の低い利回りでは従来どおり条件が成立する。"""
    low = _CFG.profit_taking.thresholds.total_yield_strong_caution_pct - 0.5
    result = _evaluate(low)
    assert _yield_reasons(result), "既知の低利回りでは利回り低下の根拠が出るべき"


def test_confirmed_zero_total_yield_still_produces_reason() -> None:
    """確定0%は「不明」ではないため、従来どおり利回り低下条件が成立する。"""
    result = _evaluate(0.0)
    assert _yield_reasons(result), "確定0%は判定に使える値である"


def test_unknown_total_yield_does_not_change_thresholds() -> None:
    """閾値そのものは変更していない(既知値の判定結果が不変であること)。"""
    high = _CFG.profit_taking.thresholds.total_yield_caution_pct + 1.0
    assert _yield_reasons(_evaluate(high)) == []


# --- 表示層 -------------------------------------------------------------------


def test_line_total_yield_line_does_not_assert_zero_for_unknown() -> None:
    from jstock_advisor.services.line_notification_service import _total_yield_line

    unknown = _total_yield_line(None)
    assert "不明" in unknown
    assert "0.00%" not in unknown
    assert _total_yield_line(3.75) == "総合利回り: 3.75%"
    assert _total_yield_line(0.0) == "総合利回り: 0.00%"


def _buy_recommendation(total_yield: float | None) -> Recommendation:
    return Recommendation(
        recommendation_id="rec-1",
        stock_code="1234",
        stock_name="テスト",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        price_at_recommendation=Decimal("1000"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1",
        total_yield_pct_at_recommendation=total_yield,
    )


def test_cli_analyze_prints_unknown_total_yield_without_type_error(capsys) -> None:
    """Optional化で唯一クラッシュしうる箇所(`:.2f` へ None)の回帰。

    修正前は None を渡すと TypeError で落ちた。
    """
    from jstock_advisor.cli.analyze import _print_buy_recommendation

    _print_buy_recommendation(_buy_recommendation(None))
    out = capsys.readouterr().out
    assert "不明" in out
    assert "0.00%" not in out


def test_cli_analyze_prints_known_total_yield_unchanged(capsys) -> None:
    from jstock_advisor.cli.analyze import _print_buy_recommendation

    _print_buy_recommendation(_buy_recommendation(3.75))
    assert "総合利回り: 3.75%" in capsys.readouterr().out


# --- consistency validator ----------------------------------------------------


def test_consistency_validator_distinguishes_unknown_from_below_threshold() -> None:
    """unknown(検証不能)と known-below-threshold(事実として基準未満)を区別する。

    いずれも違反にはならないが理由が異なる。単一の or 条件で同時に skip しない。
    """
    import inspect

    from jstock_advisor.services import recommendation_consistency_validator as mod

    source = inspect.getsource(mod._check_yield_sufficient_full_take_on_yield_alone)
    assert "if yield_pct is None:" in source
    assert "if yield_pct < min_yield_pct:" in source
    assert "yield_pct is None or yield_pct < min_yield_pct" not in source


# --- BUY側 semantics 不変 -----------------------------------------------------


def test_buy_side_still_scores_unknown_total_yield_as_zero() -> None:
    """Decision: 買い側のスコア意味論は変更しない(#55 スコープ外)。

    snapshot.total_yield_pct が Optional になっても、買い側は呼び出し側で
    明示的に 0.0 へ落とし、component_state は EVALUATED のままとする。
    """
    import inspect

    from jstock_advisor.services import buy_signal_service

    source = inspect.getsource(buy_signal_service)
    assert "snapshot.total_yield_pct if snapshot.total_yield_pct is not None else 0.0" in source


def test_recommendation_type_enum_unchanged() -> None:
    """本Issueで判定区分の追加・変更はしていないことの明示的な回帰。"""
    assert RecommendationType.FULL_PROFIT_TAKE in RecommendationType
    assert BenefitProgramState.NO_PROGRAM.value == "NO_PROGRAM"
