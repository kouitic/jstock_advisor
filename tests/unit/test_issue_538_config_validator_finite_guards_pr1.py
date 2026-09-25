"""Issue #538 PR-1: config/models.pyの18項目がNaN/inf/-infを起動時に拒否する。

#259(PR #536)がInvestmentThesisWeights.dividend_policyだけに入れた有限性検査を、
同じ弱点を持つ残りの項目へ広げたもの。各項目について
  * NaN注入 → ValidationError
  * +inf / -inf注入(その項目で修正前にすり抜けた側)→ ValidationError
  * 本番configが引き続きload_config()で読めること(最重要のregression guard)
を確認する。合計検査を持つクラスは、+infと-infを2項目へ入れて合計をNaNへ相殺させる
組み合わせ(#538が指摘した抜け道そのもの)も拒否されることを確認する。
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from pydantic import ValidationError

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import (
    CompanyQualityWeights,
    EarningsSurpriseRulesConfig,
    EarningsTrendRulesConfig,
    HistoricalValuationRulesConfig,
    HoldingDecisionRatioRulesConfig,
    InvestmentThesisWeights,
    NearBuyConfig,
    ProfitProtectionCandidateThresholds,
    ProfitProtectionStrongThresholds,
    RatioClampRange,
    RiskCategoryCaps,
    RiskFactorTable,
    RiskSignal,
    TimingScoreRulesConfig,
    UndervaluationCategoryCaps,
)

NON_FINITE = [math.nan, math.inf, -math.inf]


# --------------------------------------------------------------------------
# 本番config由来のbaseline。実際の値をそのまま使うため、「現行configが通る」と
# 「1項目だけ壊すと落ちる」を同じ土台で確認できる。
# --------------------------------------------------------------------------
def _dump(model: Any) -> dict[str, Any]:
    """YAMLのキー(alias)でbaseline dictを得る。aliasを持つ`yield`のため必須。"""
    return dict(model.model_dump(mode="python", by_alias=True))


@pytest.fixture(scope="module")
def config() -> Any:
    return load_config()


# --------------------------------------------------------------------------
# 本番configのregression guard(YAMLファイル単位)
# --------------------------------------------------------------------------
def test_production_config_still_loads(config: Any) -> None:
    """今回触れた全YAMLを含むload_config()が成功すること(最重要)。"""
    assert config is not None


def test_production_holding_decision_rules_yaml_values_are_accepted(config: Any) -> None:
    """holding_decision_rules.yaml: #1 CompanyQualityWeights / #2 InvestmentThesisWeights。"""
    quality = config.holding_decision.company_quality_weights
    thesis = config.holding_decision.investment_thesis_weights
    assert CompanyQualityWeights(**_dump(quality)) == quality
    assert InvestmentThesisWeights(**_dump(thesis)) == thesis


def test_production_holding_decision_risk_rules_yaml_values_are_accepted(config: Any) -> None:
    """holding_decision_risk_rules.yaml: #3 caps / #4 factors / #5 signals。"""
    risk = config.holding_decision_risk
    assert RiskCategoryCaps(**_dump(risk.category_caps)) == risk.category_caps
    assert RiskFactorTable(**_dump(risk.factors)) == risk.factors
    assert risk.signals
    for signal in risk.signals.values():
        assert RiskSignal(**_dump(signal)) == signal


def test_production_buy_decision_rules_yaml_values_are_accepted(config: Any) -> None:
    """buy_decision_rules.yaml: #6 undervaluation_category_caps / #18 near_buy。"""
    caps = config.buy_decision.undervaluation_category_caps
    near_buy = config.buy_decision.near_buy
    assert UndervaluationCategoryCaps(**_dump(caps)) == caps
    assert NearBuyConfig(**_dump(near_buy)) == near_buy


def test_production_historical_valuation_rules_yaml_values_are_accepted(config: Any) -> None:
    """historical_valuation_rules.yaml: #7〜#10。"""
    hv = config.historical_valuation
    assert HistoricalValuationRulesConfig(**_dump(hv)) == hv


def test_production_timing_score_rules_yaml_values_are_accepted(config: Any) -> None:
    """timing_score_rules.yaml: #11。"""
    timing = config.timing_score
    assert TimingScoreRulesConfig(**_dump(timing)) == timing


def test_production_earnings_surprise_rules_yaml_values_are_accepted(config: Any) -> None:
    """earnings_surprise_rules.yaml: #12。"""
    surprise = config.earnings_surprise
    assert EarningsSurpriseRulesConfig(**_dump(surprise)) == surprise


def test_production_earnings_trend_rules_yaml_values_are_accepted(config: Any) -> None:
    """earnings_trend_rules.yaml: #13 / #14。"""
    trend = config.earnings_trend
    assert EarningsTrendRulesConfig(**_dump(trend)) == trend


def test_production_holding_decision_ratio_rules_yaml_values_are_accepted(config: Any) -> None:
    """holding_decision_ratio_rules.yaml: #15 clamp / #16 正値3項目。"""
    ratio = config.holding_decision_ratio
    assert HoldingDecisionRatioRulesConfig(**_dump(ratio)) == ratio
    assert RatioClampRange(**_dump(ratio.clamp)) == ratio.clamp


def test_production_profit_taking_rules_yaml_values_are_accepted(config: Any) -> None:
    """profit_taking_rules.yaml: #17 profit_protection.candidate/.strong。"""
    protection = config.profit_taking.profit_protection
    assert (
        ProfitProtectionCandidateThresholds(**_dump(protection.candidate)) == protection.candidate
    )
    assert ProfitProtectionStrongThresholds(**_dump(protection.strong)) == protection.strong


# --------------------------------------------------------------------------
# #1 CompanyQualityWeights(合計50点、項目ごとの下限は無かった)
# --------------------------------------------------------------------------
_QUALITY_FIELDS = (
    "financial_health_equity_ratio",
    "financial_health_debt_excess",
    "cash_generation_cf_income_ratio",
    "cash_generation_cf_streak",
    "profitability_roe",
    "profitability_eps_stability",
    "stability_operating_income",
    "stability_deficit",
    "governance_going_concern",
    "governance_listing_risk",
)


@pytest.mark.parametrize("field", _QUALITY_FIELDS)
@pytest.mark.parametrize("value", NON_FINITE)
def test_company_quality_weights_rejects_non_finite(config: Any, field: str, value: float) -> None:
    base = _dump(config.holding_decision.company_quality_weights)
    with pytest.raises(ValidationError, match="有限値"):
        CompanyQualityWeights(**{**base, field: value})


def test_company_quality_weights_rejects_zero_weight_is_not_introduced(config: Any) -> None:
    """0点は従来通り許す(有限性検査で新たに禁止していないこと)。"""
    base = _dump(config.holding_decision.company_quality_weights)
    moved = base["governance_listing_risk"]
    weights = {
        **base,
        "governance_listing_risk": 0.0,
        "governance_going_concern": base["governance_going_concern"] + moved,
    }
    assert CompanyQualityWeights(**weights).governance_listing_risk == 0.0


def test_company_quality_weights_negative_sign_contract_is_unchanged(config: Any) -> None:
    """サブちゃんレビュー(PR #571 F1): 修正前は項目ごとの下限検査が無く、有限の負値も
    合計検査(50点)だけ通れば受理されていた。#538(NaN/inf対策)の範囲外の新規制約を
    入れないため、有限の負値は引き続き受理する(isfinite以外の新しい制約を入れていない
    ことの直接固定。#5 RiskSignal.base_pointsと同じ判断)。"""
    base = _dump(config.holding_decision.company_quality_weights)
    moved = base["governance_listing_risk"] + 1.0
    weights = {
        **base,
        "governance_listing_risk": -1.0,
        "governance_going_concern": base["governance_going_concern"] + moved,
    }
    assert CompanyQualityWeights(**weights).governance_listing_risk == -1.0


def test_company_quality_weights_rejects_cancelling_infinities(config: Any) -> None:
    """+infと-infは合計がNaNになり`abs(total - 50) > 0.01`がFalse=合格へ倒れる。"""
    base = _dump(config.holding_decision.company_quality_weights)
    broken = {
        **base,
        "profitability_roe": math.inf,
        "stability_deficit": -math.inf,
    }
    total = sum(broken.values())
    assert math.isnan(total)
    assert not abs(total - 50.0) > 0.01  # 修正前の合計検査は通ってしまう
    with pytest.raises(ValidationError, match="有限値"):
        CompanyQualityWeights(**broken)


def test_company_quality_weights_sum_check_still_applies(config: Any) -> None:
    base = _dump(config.holding_decision.company_quality_weights)
    with pytest.raises(ValidationError, match="配点合計は50点"):
        CompanyQualityWeights(**{**base, "profitability_roe": base["profitability_roe"] + 1.0})


# --------------------------------------------------------------------------
# #2 InvestmentThesisWeights(dividend_policy以外の5項目)
# --------------------------------------------------------------------------
_THESIS_OTHER_FIELDS = (
    "total_yield",
    "benefit_condition",
    "profit_cf_premise",
    "financial_premise",
    "custom_conditions",
)


@pytest.mark.parametrize("field", _THESIS_OTHER_FIELDS)
@pytest.mark.parametrize("value", NON_FINITE)
def test_investment_thesis_other_weights_reject_non_finite(
    config: Any, field: str, value: float
) -> None:
    base = _dump(config.holding_decision.investment_thesis_weights)
    with pytest.raises(ValidationError, match="有限値"):
        InvestmentThesisWeights(**{**base, field: value})


def test_investment_thesis_dividend_policy_guard_is_untouched(config: Any) -> None:
    """#259のdividend_policy検査(0を禁止)はそのまま残っていること。"""
    base = _dump(config.holding_decision.investment_thesis_weights)
    weights = {
        **base,
        "dividend_policy": 0.0,
        "total_yield": base["total_yield"] + base["dividend_policy"],
    }
    with pytest.raises(ValidationError, match="0より大きい有限値"):
        InvestmentThesisWeights(**weights)


def test_investment_thesis_zero_is_still_legal_for_other_weights(config: Any) -> None:
    base = _dump(config.holding_decision.investment_thesis_weights)
    weights = {
        **base,
        "benefit_condition": 0.0,
        "total_yield": base["total_yield"] + base["benefit_condition"],
    }
    assert InvestmentThesisWeights(**weights).benefit_condition == 0.0


def test_investment_thesis_other_weights_negative_sign_contract_is_unchanged(config: Any) -> None:
    """サブちゃんレビュー(PR #571 F1)対応: dividend_policy以外の5項目には符号契約が
    元から無く、有限の負値も合計検査だけ通れば受理されていた。#538の範囲外の新規制約
    (>=0)を入れないため、有限の負値は引き続き受理する。"""
    base = _dump(config.holding_decision.investment_thesis_weights)
    moved = base["benefit_condition"] + 1.0
    weights = {
        **base,
        "benefit_condition": -1.0,
        "total_yield": base["total_yield"] + moved,
    }
    assert InvestmentThesisWeights(**weights).benefit_condition == -1.0


def test_investment_thesis_weights_reject_cancelling_infinities(config: Any) -> None:
    base = _dump(config.holding_decision.investment_thesis_weights)
    broken = {**base, "total_yield": math.inf, "custom_conditions": -math.inf}
    assert math.isnan(sum(broken.values()))
    with pytest.raises(ValidationError, match="有限値"):
        InvestmentThesisWeights(**broken)


# --------------------------------------------------------------------------
# #3 RiskCategoryCaps(合計100点)
# --------------------------------------------------------------------------
_RISK_CAP_FIELDS = (
    "business_cashflow_deterioration",
    "shareholder_return_deterioration",
    "financial_crisis",
    "governance_and_listing_risk",
    "structural_change",
)


@pytest.mark.parametrize("field", _RISK_CAP_FIELDS)
@pytest.mark.parametrize("value", NON_FINITE)
def test_risk_category_caps_reject_non_finite(config: Any, field: str, value: float) -> None:
    base = _dump(config.holding_decision_risk.category_caps)
    with pytest.raises(ValidationError, match="有限値"):
        RiskCategoryCaps(**{**base, field: value})


def test_risk_category_caps_reject_cancelling_infinities(config: Any) -> None:
    base = _dump(config.holding_decision_risk.category_caps)
    broken = {**base, "financial_crisis": math.inf, "structural_change": -math.inf}
    total = sum(broken.values())
    assert math.isnan(total)
    assert not abs(total - 100.0) > 0.01
    with pytest.raises(ValidationError, match="有限値"):
        RiskCategoryCaps(**broken)


def test_risk_category_caps_sum_check_still_applies(config: Any) -> None:
    base = _dump(config.holding_decision_risk.category_caps)
    with pytest.raises(ValidationError, match="合計は100点"):
        RiskCategoryCaps(**{**base, "structural_change": base["structural_change"] + 1.0})


def test_risk_category_caps_negative_sign_contract_is_unchanged(config: Any) -> None:
    """サブちゃんレビュー(PR #571 F1)対応: 修正前は項目ごとの下限検査が無く、有限の
    負値も合計検査(100点)だけ通れば受理されていた。#538の範囲外の新規制約(>=0)を
    入れないため、有限の負値は引き続き受理する。"""
    base = _dump(config.holding_decision_risk.category_caps)
    moved = base["structural_change"] + 1.0
    caps = {
        **base,
        "structural_change": -1.0,
        "financial_crisis": base["financial_crisis"] + moved,
    }
    assert RiskCategoryCaps(**caps).structural_change == -1.0


# --------------------------------------------------------------------------
# #4 RiskFactorTable(項目ごとに`value < 0`だけ)
# --------------------------------------------------------------------------
_RISK_FACTOR_FIELDS = (
    "persistence_single_occurrence",
    "persistence_two_periods",
    "persistence_structural",
    "confidence_primary_source_confirmed",
    "confidence_secondary_source_only",
)


@pytest.mark.parametrize("field", _RISK_FACTOR_FIELDS)
@pytest.mark.parametrize("value", [math.nan, math.inf])
def test_risk_factor_table_rejects_non_finite(config: Any, field: str, value: float) -> None:
    """NaNも+infも修正前は`value < 0`をすり抜けていた(-infは元から拒否)。"""
    base = _dump(config.holding_decision_risk.factors)
    with pytest.raises(ValidationError, match="0以上の有限値"):
        RiskFactorTable(**{**base, field: value})


@pytest.mark.parametrize("field", _RISK_FACTOR_FIELDS)
def test_risk_factor_table_still_rejects_negative_infinity(config: Any, field: str) -> None:
    base = _dump(config.holding_decision_risk.factors)
    with pytest.raises(ValidationError):
        RiskFactorTable(**{**base, field: -math.inf})


def test_risk_factor_table_zero_is_still_legal(config: Any) -> None:
    base = _dump(config.holding_decision_risk.factors)
    assert RiskFactorTable(**{**base, "persistence_structural": 0.0}).persistence_structural == 0.0


# --------------------------------------------------------------------------
# #5 RiskSignal.base_points(修正前は検査そのものが無かった)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("value", NON_FINITE)
def test_risk_signal_base_points_rejects_non_finite(value: float) -> None:
    with pytest.raises(ValidationError, match="有限値"):
        RiskSignal(base_points=value, category="financial_crisis")


def test_risk_signal_base_points_accepts_ordinary_value() -> None:
    assert RiskSignal(base_points=15.0, category="financial_crisis").base_points == 15.0


def test_risk_signal_base_points_sign_contract_is_unchanged() -> None:
    """符号の契約は既存に無いため、有限性のみを追加した(負値は従来通り受け付ける)。"""
    assert RiskSignal(base_points=-1.0, category="financial_crisis").base_points == -1.0


# --------------------------------------------------------------------------
# #6 UndervaluationCategoryCaps(合計20点)
# --------------------------------------------------------------------------
_UNDERVALUATION_FIELDS = ("valuation_multiple", "yield", "fair_value", "market_price_action")


@pytest.mark.parametrize("field", _UNDERVALUATION_FIELDS)
@pytest.mark.parametrize("value", NON_FINITE)
def test_undervaluation_category_caps_reject_non_finite(
    config: Any, field: str, value: float
) -> None:
    base = _dump(config.buy_decision.undervaluation_category_caps)
    with pytest.raises(ValidationError, match="有限値"):
        UndervaluationCategoryCaps(**{**base, field: value})


def test_undervaluation_category_caps_reject_cancelling_infinities(config: Any) -> None:
    base = _dump(config.buy_decision.undervaluation_category_caps)
    broken = {**base, "valuation_multiple": math.inf, "fair_value": -math.inf}
    total = sum(broken.values())
    assert math.isnan(total)
    assert not abs(total - 20.0) > 1e-9
    with pytest.raises(ValidationError, match="有限値"):
        UndervaluationCategoryCaps(**broken)


def test_undervaluation_category_caps_sum_check_still_applies(config: Any) -> None:
    base = _dump(config.buy_decision.undervaluation_category_caps)
    with pytest.raises(ValidationError, match="合計は20点"):
        UndervaluationCategoryCaps(**{**base, "yield": base["yield"] + 1.0})


def test_undervaluation_category_caps_negative_sign_contract_is_unchanged(config: Any) -> None:
    """サブちゃんレビュー(PR #571 F1)対応: 修正前は項目ごとの下限検査が無く、有限の
    負値も合計検査(20点)だけ通れば受理されていた。#538の範囲外の新規制約(>=0)を
    入れないため、有限の負値は引き続き受理する。"""
    base = _dump(config.buy_decision.undervaluation_category_caps)
    moved = base["yield"] + 1.0
    caps = {**base, "yield": -1.0, "fair_value": base["fair_value"] + moved}
    assert UndervaluationCategoryCaps(**caps).yield_ == -1.0


# --------------------------------------------------------------------------
# #7〜#10 HistoricalValuationRulesConfig
# --------------------------------------------------------------------------
@pytest.mark.parametrize("field", ["per_weight", "pbr_weight"])
@pytest.mark.parametrize("value", NON_FINITE)
def test_historical_valuation_weights_reject_non_finite(
    config: Any, field: str, value: float
) -> None:
    """#7。NaNは`< 0`と合計の`<= 0`の両方を、+infは合計の検査をすり抜けていた。"""
    base = _dump(config.historical_valuation)
    with pytest.raises(ValidationError, match="per_weight/pbr_weightは0以上の有限値"):
        HistoricalValuationRulesConfig(**{**base, field: value})


def test_historical_valuation_zero_weight_on_one_side_is_still_legal(config: Any) -> None:
    """片側0(合計>0)は従来通り許す。"""
    base = _dump(config.historical_valuation)
    loaded = HistoricalValuationRulesConfig(**{**base, "pbr_weight": 0.0})
    assert loaded.pbr_weight == 0.0


def test_historical_valuation_both_weights_zero_is_still_rejected(config: Any) -> None:
    base = _dump(config.historical_valuation)
    with pytest.raises(ValidationError, match="合計は0より大きい"):
        HistoricalValuationRulesConfig(**{**base, "per_weight": 0.0, "pbr_weight": 0.0})


@pytest.mark.parametrize("value", [math.nan, math.inf])
def test_historical_valuation_outlier_mad_threshold_rejects_non_finite(
    config: Any, value: float
) -> None:
    """#8。`<= 0`はNaNでも+infでもFalse=合格へ倒れる(-infは元から拒否)。"""
    base = _dump(config.historical_valuation)
    with pytest.raises(ValidationError, match="outlier_mad_thresholdは正の有限値"):
        HistoricalValuationRulesConfig(**{**base, "outlier_mad_threshold": value})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("per_absolute_min", math.nan, "per_absolute_min"),
        ("per_absolute_max", math.nan, "per_absolute_min"),
        ("per_absolute_min", -math.inf, "per_absolute_min"),
        ("per_absolute_max", math.inf, "per_absolute_min"),
        ("pbr_absolute_min", math.nan, "pbr_absolute_min"),
        ("pbr_absolute_max", math.nan, "pbr_absolute_min"),
        ("pbr_absolute_min", -math.inf, "pbr_absolute_min"),
        ("pbr_absolute_max", math.inf, "pbr_absolute_min"),
    ],
)
def test_historical_valuation_absolute_range_rejects_non_finite(
    config: Any, field: str, value: float, message: str
) -> None:
    """#9。NaNは`min >= max`が常にFalseで通り、±infはレンジとして無意味。"""
    base = _dump(config.historical_valuation)
    with pytest.raises(ValidationError, match=f"{message}は.*未満の有限値"):
        HistoricalValuationRulesConfig(**{**base, field: value})


def test_historical_valuation_absolute_range_order_check_still_applies(config: Any) -> None:
    base = _dump(config.historical_valuation)
    with pytest.raises(ValidationError, match="per_absolute_min"):
        HistoricalValuationRulesConfig(**{**base, "per_absolute_min": base["per_absolute_max"]})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("coverage_medium_threshold", math.nan),
        ("coverage_high_threshold", math.nan),
        ("coverage_medium_threshold", -math.inf),
        ("coverage_high_threshold", math.inf),
    ],
)
def test_historical_valuation_coverage_order_rejects_non_finite(
    config: Any, field: str, value: float
) -> None:
    """#10。"""
    base = _dump(config.historical_valuation)
    with pytest.raises(ValidationError, match="coverage_medium_thresholdは.*未満の有限値"):
        HistoricalValuationRulesConfig(**{**base, field: value})


# --------------------------------------------------------------------------
# #11 TimingScoreRulesConfig(7成分の重み)
# --------------------------------------------------------------------------
_TIMING_WEIGHT_FIELDS = (
    "trend_quality_weight",
    "price_vs_ma20_weight",
    "price_vs_ma60_weight",
    "rsi_weight",
    "macd_weight",
    "drawdown_weight",
    "volume_weight",
)


@pytest.mark.parametrize("field", _TIMING_WEIGHT_FIELDS)
@pytest.mark.parametrize("value", NON_FINITE)
def test_timing_score_weights_reject_non_finite(config: Any, field: str, value: float) -> None:
    """NaNと+infは`any(w < 0)`も`sum(weights) <= 0`もすり抜けていた。"""
    base = _dump(config.timing_score)
    with pytest.raises(ValidationError, match="各成分の重みは0以上の有限値"):
        TimingScoreRulesConfig(**{**base, field: value})


def test_timing_score_single_zero_weight_is_still_legal(config: Any) -> None:
    base = _dump(config.timing_score)
    assert TimingScoreRulesConfig(**{**base, "volume_weight": 0.0}).volume_weight == 0.0


def test_timing_score_all_zero_weights_is_still_rejected(config: Any) -> None:
    base = _dump(config.timing_score)
    zeroed = {**base, **dict.fromkeys(_TIMING_WEIGHT_FIELDS, 0.0)}
    with pytest.raises(ValidationError, match="合計は0より大きい"):
        TimingScoreRulesConfig(**zeroed)


# --------------------------------------------------------------------------
# #12 EarningsSurpriseRulesConfig.analyst_consensus_weight
# --------------------------------------------------------------------------
@pytest.mark.parametrize("value", [math.nan, math.inf])
def test_earnings_surprise_weight_rejects_non_finite(config: Any, value: float) -> None:
    """`<= 0`はNaNでも+infでもFalse=合格へ倒れる(-infは元から拒否)。"""
    base = _dump(config.earnings_surprise)
    with pytest.raises(ValidationError, match="analyst_consensus_weightは正の有限値"):
        EarningsSurpriseRulesConfig(**{**base, "analyst_consensus_weight": value})


def test_earnings_surprise_weight_still_rejects_zero(config: Any) -> None:
    base = _dump(config.earnings_surprise)
    with pytest.raises(ValidationError, match="analyst_consensus_weightは正の有限値"):
        EarningsSurpriseRulesConfig(**{**base, "analyst_consensus_weight": 0.0})


# --------------------------------------------------------------------------
# #13 / #14 EarningsTrendRulesConfig
# --------------------------------------------------------------------------
_TREND_WEIGHT_FIELDS = (
    "operating_income_trend_weight",
    "operating_cashflow_trend_weight",
    "dividend_direction_weight",
    "acceleration_weight",
)


@pytest.mark.parametrize("field", _TREND_WEIGHT_FIELDS)
@pytest.mark.parametrize("value", NON_FINITE)
def test_earnings_trend_weights_reject_non_finite(config: Any, field: str, value: float) -> None:
    """#13。"""
    base = _dump(config.earnings_trend)
    with pytest.raises(ValidationError, match="各成分の重みは0以上の有限値"):
        EarningsTrendRulesConfig(**{**base, field: value})


def test_earnings_trend_single_zero_weight_is_still_legal(config: Any) -> None:
    base = _dump(config.earnings_trend)
    loaded = EarningsTrendRulesConfig(**{**base, "acceleration_weight": 0.0})
    assert loaded.acceleration_weight == 0.0


def test_earnings_trend_all_zero_weights_is_still_rejected(config: Any) -> None:
    base = _dump(config.earnings_trend)
    zeroed = {**base, **dict.fromkeys(_TREND_WEIGHT_FIELDS, 0.0)}
    with pytest.raises(ValidationError, match="合計は0より大きい"):
        EarningsTrendRulesConfig(**zeroed)


@pytest.mark.parametrize("value", [math.nan, math.inf])
def test_earnings_trend_acceleration_full_scale_rejects_non_finite(
    config: Any, value: float
) -> None:
    """#14。`<= 0`はNaNでも+infでもFalse=合格へ倒れる(-infは元から拒否)。"""
    base = _dump(config.earnings_trend)
    with pytest.raises(ValidationError, match="acceleration_full_scale_pctは正の有限値"):
        EarningsTrendRulesConfig(**{**base, "acceleration_full_scale_pct": value})


def test_earnings_trend_acceleration_full_scale_still_rejects_zero(config: Any) -> None:
    base = _dump(config.earnings_trend)
    with pytest.raises(ValidationError, match="acceleration_full_scale_pctは正の有限値"):
        EarningsTrendRulesConfig(**{**base, "acceleration_full_scale_pct": 0.0})


# --------------------------------------------------------------------------
# #15 RatioClampRange
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("ratio_clamp_min", math.nan, "ratio_clamp_min"),
        ("ratio_clamp_max", math.nan, "ratio_clamp_min"),
        ("ratio_clamp_min", -math.inf, "ratio_clamp_min"),
        ("ratio_clamp_max", math.inf, "ratio_clamp_min"),
        ("roe_clamp_min", math.nan, "roe_clamp_min"),
        ("roe_clamp_max", math.nan, "roe_clamp_min"),
        ("roe_clamp_min", -math.inf, "roe_clamp_min"),
        ("roe_clamp_max", math.inf, "roe_clamp_min"),
    ],
)
def test_ratio_clamp_range_rejects_non_finite(
    config: Any, field: str, value: float, message: str
) -> None:
    base = _dump(config.holding_decision_ratio.clamp)
    with pytest.raises(ValidationError, match=f"{message}は.*未満の有限値"):
        RatioClampRange(**{**base, field: value})


def test_ratio_clamp_range_order_check_still_applies(config: Any) -> None:
    base = _dump(config.holding_decision_ratio.clamp)
    with pytest.raises(ValidationError, match="ratio_clamp_min"):
        RatioClampRange(**{**base, "ratio_clamp_min": base["ratio_clamp_max"]})


# --------------------------------------------------------------------------
# #16 HoldingDecisionRatioRulesConfig(正値3項目)
# --------------------------------------------------------------------------
_RATIO_POSITIVE_FIELDS = (
    "min_operating_income_absolute_yen",
    "min_mean_for_cv_yen",
    "outlier_clip_zscore",
)


@pytest.mark.parametrize("field", _RATIO_POSITIVE_FIELDS)
@pytest.mark.parametrize("value", [math.nan, math.inf])
def test_holding_decision_ratio_positive_fields_reject_non_finite(
    config: Any, field: str, value: float
) -> None:
    """`<= 0`はNaNでも+infでもFalse=合格へ倒れる(-infは元から拒否)。"""
    base = _dump(config.holding_decision_ratio)
    with pytest.raises(ValidationError, match=f"{field}は正の有限値"):
        HoldingDecisionRatioRulesConfig(**{**base, field: value})


@pytest.mark.parametrize("field", _RATIO_POSITIVE_FIELDS)
def test_holding_decision_ratio_positive_fields_still_reject_zero(config: Any, field: str) -> None:
    base = _dump(config.holding_decision_ratio)
    with pytest.raises(ValidationError, match=f"{field}は正の有限値"):
        HoldingDecisionRatioRulesConfig(**{**base, field: 0.0})


# --------------------------------------------------------------------------
# #17 ProfitProtection*.min_current_gain_pct(上限は意図的に設けない)
# --------------------------------------------------------------------------
_PROFIT_PROTECTION_TARGETS = [
    ("candidate", ProfitProtectionCandidateThresholds),
    ("strong", ProfitProtectionStrongThresholds),
]


def _protection_base(config: Any, label: str) -> dict[str, Any]:
    return _dump(getattr(config.profit_taking.profit_protection, label))


@pytest.mark.parametrize(("label", "model"), _PROFIT_PROTECTION_TARGETS)
@pytest.mark.parametrize("value", [math.nan, math.inf])
def test_profit_protection_min_current_gain_rejects_non_finite(
    config: Any, label: str, model: Any, value: float
) -> None:
    """上限を持たない項目なので、+infも`< 0`をすり抜けていた(-infは元から拒否)。"""
    base = _protection_base(config, label)
    with pytest.raises(ValidationError, match="min_current_gain_pctは0以上の有限値"):
        model(**{**base, "min_current_gain_pct": value})


@pytest.mark.parametrize(("label", "model"), _PROFIT_PROTECTION_TARGETS)
def test_profit_protection_min_current_gain_still_rejects_negative(
    config: Any, label: str, model: Any
) -> None:
    base = _protection_base(config, label)
    with pytest.raises(ValidationError, match="min_current_gain_pctは0以上の有限値"):
        model(**{**base, "min_current_gain_pct": -0.1})


@pytest.mark.parametrize(("label", "model"), _PROFIT_PROTECTION_TARGETS)
def test_profit_protection_min_current_gain_has_no_upper_bound(
    config: Any, label: str, model: Any
) -> None:
    """株価が取得価格の何倍にもなり得るため、上限は引き続き設けない。"""
    base = _protection_base(config, label)
    assert model(**{**base, "min_current_gain_pct": 100000.0}).min_current_gain_pct == 100000.0


# --------------------------------------------------------------------------
# #18 NearBuyConfig(start <= continueのヒステリシス)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start_required_decline_pct", math.nan),
        ("continue_required_decline_pct", math.nan),
        ("start_required_decline_pct", -math.inf),
        ("continue_required_decline_pct", math.inf),
    ],
)
def test_near_buy_decline_order_rejects_non_finite(config: Any, field: str, value: float) -> None:
    """NaNは`start > continue`が常にFalseで通り、±infはヒステリシスとして無意味。"""
    base = _dump(config.buy_decision.near_buy)
    with pytest.raises(ValidationError, match="いずれも有限値"):
        NearBuyConfig(**{**base, field: value})


def test_near_buy_decline_order_check_still_applies(config: Any) -> None:
    base = _dump(config.buy_decision.near_buy)
    broken = {
        **base,
        "start_required_decline_pct": base["continue_required_decline_pct"] + 1.0,
    }
    with pytest.raises(ValidationError, match="continue_required_decline_pct"):
        NearBuyConfig(**broken)


def test_near_buy_equal_decline_values_are_still_legal(config: Any) -> None:
    """start == continueは従来通り許す(`>`のみを禁止していた)。"""
    base = _dump(config.buy_decision.near_buy)
    equalized = {
        **base,
        "start_required_decline_pct": base["continue_required_decline_pct"],
    }
    loaded = NearBuyConfig(**equalized)
    assert loaded.start_required_decline_pct == loaded.continue_required_decline_pct
