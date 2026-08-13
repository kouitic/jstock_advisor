from decimal import Decimal

from jstock_advisor.domain.entities.enums import EvidenceCoverageStatus
from jstock_advisor.domain.signals.simple_roe import compute_simple_forecast_roe


def test_computes_ratio_from_forecast_eps_and_bps() -> None:
    result = compute_simple_forecast_roe(Decimal("80"), Decimal("1000"))
    assert result.status is EvidenceCoverageStatus.EVALUATED
    assert result.value is not None
    assert abs(result.value - 0.08) < 1e-9


def test_not_evaluated_when_forecast_eps_missing() -> None:
    result = compute_simple_forecast_roe(None, Decimal("1000"))
    assert result.status is EvidenceCoverageStatus.NOT_EVALUATED
    assert result.value is None


def test_not_evaluated_when_forecast_bps_missing() -> None:
    result = compute_simple_forecast_roe(Decimal("80"), None)
    assert result.status is EvidenceCoverageStatus.NOT_EVALUATED
    assert result.value is None


def test_not_evaluated_when_forecast_bps_not_positive() -> None:
    result = compute_simple_forecast_roe(Decimal("80"), Decimal("0"))
    assert result.status is EvidenceCoverageStatus.NOT_EVALUATED
    assert result.value is None

    result_negative = compute_simple_forecast_roe(Decimal("80"), Decimal("-100"))
    assert result_negative.status is EvidenceCoverageStatus.NOT_EVALUATED
