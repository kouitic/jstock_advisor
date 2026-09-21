"""Issue #259: InvestmentThesisWeightsは、dividend_policyが0以下の配点を拒否する。"""

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
