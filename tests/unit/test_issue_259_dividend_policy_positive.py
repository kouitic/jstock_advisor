"""Issue #259: InvestmentThesisWeightsは、dividend_policyが0以下の配点を拒否する。"""

import math

import pytest
from pydantic import ValidationError

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import InvestmentThesisWeights

_VALID = {
    "dividend_policy": 15.0,
    "total_yield": 10.0,
    "benefit_condition": 5.0,
    "profit_cf_premise": 10.0,
    "financial_premise": 5.0,
    "custom_conditions": 5.0,
}


def test_valid_weights_are_accepted() -> None:
    assert InvestmentThesisWeights(**_VALID).dividend_policy == 15.0


def test_production_config_is_still_accepted() -> None:
    weights = load_config().holding_decision.investment_thesis_weights
    assert weights.dividend_policy > 0


@pytest.mark.parametrize("value", [0.0, -5.0])
def test_non_positive_dividend_policy_is_rejected_even_when_sum_is_50(value: float) -> None:
    weights = {
        **_VALID,
        "dividend_policy": value,
        "total_yield": _VALID["total_yield"] + 15.0 - value,
    }
    assert abs(sum(weights.values()) - 50.0) < 0.01
    with pytest.raises(ValidationError, match="dividend_policy"):
        InvestmentThesisWeights(**weights)


def test_sum_check_still_applies() -> None:
    with pytest.raises(ValidationError, match="50点"):
        InvestmentThesisWeights(**{**_VALID, "total_yield": 11.0})


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_dividend_policy_is_rejected(value: float) -> None:
    """NaNは`> 0`も合計の比較も常にFalseになり、検査をすり抜ける(レビュー指摘)。"""
    # ★ matchは検査固有の文言にする。"dividend_policy"だけだと、Pydanticのエラー表示に入る
    #   入力値のecho(input_value={'dividend_policy': inf, ...})で満たされてしまい、
    #   +infが既存の合計検査で拒否されても通る(有限性の検査を外した変異を検出できない)。
    with pytest.raises(ValidationError, match="0より大きい有限値"):
        InvestmentThesisWeights(**{**_VALID, "dividend_policy": value})


def test_nan_dividend_policy_is_rejected_when_loaded_from_yaml(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """YAMLの`.nan`でも起動時の検証で拒否される(configの実際の入口)。"""
    import yaml

    from jstock_advisor.config.models import HoldingDecisionRulesConfig

    src = load_config().holding_decision.model_dump(mode="python")
    src["investment_thesis_weights"]["dividend_policy"] = math.nan
    dumped = yaml.safe_dump(src)
    assert ".nan" in dumped
    with pytest.raises(ValidationError, match="dividend_policy"):
        HoldingDecisionRulesConfig(**yaml.safe_load(dumped))
