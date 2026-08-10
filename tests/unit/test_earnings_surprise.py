"""domain/signals/earnings_surprise.pyのテスト(判定精度向上機能Phase C)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from jstock_advisor.config.models import (
    EarningsSurpriseCategoryThresholds,
    EarningsSurpriseRulesConfig,
)
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    DividendComparisonOutcome,
    EarningsDateStatus,
    EarningsReleaseConfirmationState,
    EarningsSurpriseCategory,
    EarningsSurpriseEvaluationState,
)
from jstock_advisor.domain.signals.earnings_surprise import (
    earnings_surprise_config_values,
    earnings_surprise_result_to_metrics,
    evaluate_earnings_surprise,
)
from jstock_advisor.interfaces.types import EarningsSurpriseRecord

_NOW = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)


def _config(**overrides: object) -> EarningsSurpriseRulesConfig:
    defaults: dict[str, object] = dict(
        model_version="test-fixture",
        analyst_consensus_weight=1.0,
        dividend_revision_weight=0.5,
        analyst_consensus_strong_negative_pct=-0.20,
        analyst_consensus_negative_pct=-0.05,
        analyst_consensus_positive_pct=0.05,
        analyst_consensus_strong_positive_pct=0.20,
        dividend_actual_cut_score=-100.0,
        dividend_forecast_cut_score=-50.0,
        dividend_maintained_score=0.0,
        dividend_increase_score=80.0,
        min_coverage_required=0.3,
        coverage_high_threshold=0.9,
        coverage_medium_threshold=0.5,
        category_thresholds=EarningsSurpriseCategoryThresholds(
            strong_positive=50.0, positive=15.0, negative=-15.0, strong_negative=-50.0
        ),
    )
    defaults.update(overrides)
    return EarningsSurpriseRulesConfig.model_validate(defaults)


_CONFIG = _config()

_TEST_SOURCE = DataSourceReference(provider="test-fixture", fetched_at=_NOW)
_PERIOD_END = dt.date(2026, 6, 30)


def _record(
    quarter_end: dt.date = _PERIOD_END,
    eps_actual: Decimal | None = Decimal("100"),
    eps_estimate: Decimal | None = Decimal("90"),
    surprise_pct: float | None = None,
) -> EarningsSurpriseRecord:
    if surprise_pct is None and eps_actual is not None and eps_estimate is not None:
        surprise_pct = float((eps_actual - eps_estimate) / eps_estimate)
    return EarningsSurpriseRecord(
        stock_code="0000",
        quarter_end=quarter_end,
        eps_actual=eps_actual,
        eps_estimate=eps_estimate,
        surprise_pct=surprise_pct,
        source=_TEST_SOURCE,
    )


def _evaluate(
    history: list[EarningsSurpriseRecord],
    resolved_period_end: dt.date | None = _PERIOD_END,
    dividend_comparison_outcome: DividendComparisonOutcome | None = None,
    earnings_date_status: EarningsDateStatus = EarningsDateStatus.UNAVAILABLE,
    release_confirmation_state: EarningsReleaseConfirmationState = (
        EarningsReleaseConfirmationState.NOT_APPLICABLE
    ),
    config: EarningsSurpriseRulesConfig | None = None,
):
    return evaluate_earnings_surprise(
        history,
        resolved_period_end,
        dividend_comparison_outcome,
        earnings_date_status,
        release_confirmation_state,
        _NOW,
        config or _CONFIG,
    )


# ===== 正常系: 両成分利用可能 =====


def test_both_components_evaluated_positive_surprise_and_dividend_increase() -> None:
    result = _evaluate(
        [_record(surprise_pct=0.10)],
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
    )
    assert result.state == EarningsSurpriseEvaluationState.EVALUATED
    assert result.analyst_consensus_component == 50.0
    assert result.dividend_revision_component == 80.0
    assert result.coverage == 1.0
    assert result.confidence == ConfidenceLevel.HIGH
    assert result.score is not None
    assert result.score > 0


# ===== Analyst Consensus成分の段階評価 =====


@pytest.mark.parametrize(
    ("surprise_pct", "expected"),
    [
        (0.25, 100.0),
        (0.10, 50.0),
        (0.0, 0.0),
        (-0.10, -50.0),
        (-0.25, -100.0),
    ],
)
def test_analyst_consensus_component_bands(surprise_pct: float, expected: float) -> None:
    result = _evaluate([_record(surprise_pct=surprise_pct)])
    assert result.analyst_consensus_component == expected


# ===== Dividend Revision成分のマッピング =====


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (DividendComparisonOutcome.ACTUAL_DIVIDEND_CUT, -100.0),
        (DividendComparisonOutcome.FORECAST_DIVIDEND_CUT, -50.0),
        (DividendComparisonOutcome.DIVIDEND_MAINTAINED, 0.0),
        (DividendComparisonOutcome.DIVIDEND_INCREASE, 80.0),
    ],
)
def test_dividend_component_mapping(
    outcome: DividendComparisonOutcome, expected: float
) -> None:
    result = _evaluate([_record()], dividend_comparison_outcome=outcome)
    assert result.dividend_revision_component == expected


@pytest.mark.parametrize(
    "outcome",
    [
        DividendComparisonOutcome.SPLIT_ADJUSTMENT_ONLY,
        DividendComparisonOutcome.COMPARISON_NOT_POSSIBLE,
        None,
    ],
)
def test_dividend_component_unavailable_for_non_directional_outcomes(
    outcome: DividendComparisonOutcome | None,
) -> None:
    result = _evaluate([_record()], dividend_comparison_outcome=outcome)
    assert result.dividend_revision_component is None
    assert "DIVIDEND_REVISION_UNAVAILABLE" in result.reason_codes


# ===== resolved_period_endとの突合 =====


def test_analyst_component_unavailable_when_no_matching_quarter() -> None:
    result = _evaluate(
        [_record(quarter_end=dt.date(2026, 3, 31))],
        resolved_period_end=_PERIOD_END,
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_MAINTAINED,
    )
    assert result.analyst_consensus_component is None
    assert "ANALYST_CONSENSUS_UNAVAILABLE" in result.reason_codes


def test_analyst_component_unavailable_when_surprise_pct_missing() -> None:
    result = _evaluate(
        [_record(eps_actual=Decimal("50"), eps_estimate=None, surprise_pct=None)],
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_MAINTAINED,
    )
    assert result.analyst_consensus_component is None
    assert "ANALYST_CONSENSUS_UNAVAILABLE" in result.reason_codes


# ===== NOT_APPLICABLE(決算反映未確認)ゲート =====


@pytest.mark.parametrize(
    "state",
    [
        EarningsReleaseConfirmationState.AWAITING_CONFIRMATION,
        EarningsReleaseConfirmationState.DELAYED,
    ],
)
def test_not_applicable_when_awaiting_earnings_confirmation(
    state: EarningsReleaseConfirmationState,
) -> None:
    result = _evaluate(
        [_record()],
        earnings_date_status=EarningsDateStatus.STALE_PAST_DATE,
        release_confirmation_state=state,
    )
    assert result.state == EarningsSurpriseEvaluationState.NOT_APPLICABLE
    assert result.reason_codes == ("AWAITING_EARNINGS_CONFIRMATION",)
    assert result.score is None


def test_evaluated_when_data_updated_even_if_stale_past_date() -> None:
    result = _evaluate(
        [_record(surprise_pct=0.10)],
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
        earnings_date_status=EarningsDateStatus.STALE_PAST_DATE,
        release_confirmation_state=EarningsReleaseConfirmationState.DATA_UPDATED,
    )
    assert result.state == EarningsSurpriseEvaluationState.EVALUATED


# ===== coverage/confidence/NOT_EVALUATEDゲート =====


def test_not_evaluated_when_no_components_available() -> None:
    result = _evaluate([], resolved_period_end=None, dividend_comparison_outcome=None)
    assert result.state == EarningsSurpriseEvaluationState.NOT_EVALUATED
    assert result.coverage == 0.0
    assert result.score is None


def test_confidence_medium_when_only_dividend_component_available() -> None:
    result = _evaluate(
        [], resolved_period_end=None,
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
    )
    # coverage = 0.5/1.5 = 0.333... >= min_coverage_required(0.3)なので評価される
    assert result.state == EarningsSurpriseEvaluationState.EVALUATED
    assert result.confidence == ConfidenceLevel.LOW


# ===== カテゴリ分類 =====


def test_category_strong_positive_surprise() -> None:
    result = _evaluate(
        [_record(surprise_pct=0.25)],
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
    )
    assert result.category == EarningsSurpriseCategory.STRONG_POSITIVE_SURPRISE


def test_category_strong_negative_surprise() -> None:
    result = _evaluate(
        [_record(surprise_pct=-0.25)],
        dividend_comparison_outcome=DividendComparisonOutcome.ACTUAL_DIVIDEND_CUT,
    )
    assert result.category == EarningsSurpriseCategory.STRONG_NEGATIVE_SURPRISE


def test_category_neutral() -> None:
    result = _evaluate(
        [_record(surprise_pct=0.0)],
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_MAINTAINED,
    )
    assert result.category == EarningsSurpriseCategory.NEUTRAL


# ===== 監査情報 =====


def test_result_to_metrics_contains_components() -> None:
    result = _evaluate(
        [_record(surprise_pct=0.10)],
        dividend_comparison_outcome=DividendComparisonOutcome.DIVIDEND_INCREASE,
    )
    metrics = earnings_surprise_result_to_metrics(result)
    assert metrics["analyst_consensus_component"] == 50.0
    assert metrics["dividend_revision_component"] == 80.0
    assert metrics["state"] == "EVALUATED"


def test_config_values_include_category_thresholds() -> None:
    values = earnings_surprise_config_values(_CONFIG)
    assert values["category_thresholds"] == _CONFIG.category_thresholds.model_dump()


# ===== Config validation =====


def test_config_rejects_zero_weight_sum() -> None:
    with pytest.raises(ValidationError):
        _config(analyst_consensus_weight=0.0, dividend_revision_weight=0.0)


def test_config_rejects_unordered_analyst_consensus_boundaries() -> None:
    with pytest.raises(ValidationError):
        _config(analyst_consensus_negative_pct=0.10, analyst_consensus_positive_pct=0.05)


def test_config_rejects_unordered_dividend_scores() -> None:
    with pytest.raises(ValidationError):
        _config(dividend_forecast_cut_score=10.0, dividend_maintained_score=0.0)


def test_config_rejects_dividend_score_out_of_range() -> None:
    with pytest.raises(ValidationError):
        _config(dividend_increase_score=150.0)


def test_config_rejects_invalid_coverage_chain() -> None:
    with pytest.raises(ValidationError):
        _config(min_coverage_required=0.6, coverage_medium_threshold=0.5)


def test_config_category_thresholds_rejects_unordered() -> None:
    with pytest.raises(ValidationError):
        EarningsSurpriseCategoryThresholds(
            strong_positive=10.0, positive=15.0, negative=-15.0, strong_negative=-50.0
        )
