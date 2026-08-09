import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.signals.eps_normalization import normalize_eps
from jstock_advisor.interfaces.types import HistoricalValuation

_SOURCE = DataSourceReference(
    provider="yfinance", fetched_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
)


def _point(year: int, eps: str) -> HistoricalValuation:
    return HistoricalValuation(
        stock_code="1384",
        date=dt.date(year, 3, 31),
        eps=Decimal(eps),
        available_at=_SOURCE.fetched_at,
        source=_SOURCE,
    )


def test_no_forecast_eps_returns_none() -> None:
    result = normalize_eps(None, [], is_cyclical_industry=True)
    assert result.normalized_eps is None
    assert result.confidence == ConfidenceLevel.LOW


def test_non_cyclical_industry_uses_forecast_eps_as_is() -> None:
    result = normalize_eps(Decimal("100"), [], is_cyclical_industry=False)
    assert result.normalized_eps == Decimal("100")
    assert result.method == "NOT_APPLICABLE_NON_CYCLICAL"
    assert result.confidence == ConfidenceLevel.HIGH


def test_cyclical_industry_uses_min_of_forecast_and_historical_median() -> None:
    # 過去5年の黒字EPS中央値は80、予想EPSは150(市況上昇による一時的な増益)
    years_and_eps = [(2021, "60"), (2022, "70"), (2023, "80"), (2024, "90"), (2025, "100")]
    history = [_point(y, eps) for y, eps in years_and_eps]
    result = normalize_eps(Decimal("150"), history, is_cyclical_industry=True)
    assert result.normalized_eps == Decimal("80")
    assert result.method == "MIN_FORECAST_AND_5Y_MEDIAN_POSITIVE_EPS"
    assert result.confidence == ConfidenceLevel.MEDIUM


def test_forecast_eps_used_when_lower_than_historical_median() -> None:
    history = [_point(y, eps) for y, eps in [(2023, "80"), (2024, "90"), (2025, "100")]]
    result = normalize_eps(Decimal("50"), history, is_cyclical_industry=True)
    assert result.normalized_eps == Decimal("50")


def test_deficit_years_excluded_from_median() -> None:
    history = [
        _point(2022, "-30"),
        _point(2023, "80"),
        _point(2024, "90"),
    ]
    result = normalize_eps(Decimal("150"), history, is_cyclical_industry=True)
    assert result.normalized_eps == Decimal("85")  # median of 80,90
    assert any("赤字転落" in reason for reason in result.excluded_years)


def test_insufficient_positive_history_falls_back_to_forecast_with_medium_confidence() -> None:
    history = [_point(2025, "-30")]
    result = normalize_eps(Decimal("100"), history, is_cyclical_industry=True)
    assert result.normalized_eps == Decimal("100")
    assert result.method == "FORECAST_EPS_INSUFFICIENT_HISTORY"
    assert result.confidence == ConfidenceLevel.MEDIUM
