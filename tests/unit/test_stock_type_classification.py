import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.classification.stock_type import classify_stock_type
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import ConfidenceLevel, StockType
from jstock_advisor.interfaces.types import Disclosure, FinancialSummary

_NOW = dt.datetime(2026, 7, 27, tzinfo=dt.UTC)
_SOURCE = DataSourceReference(provider="test", fetched_at=_NOW)
_CONFIG = load_config().stock_classification


def _financial(**overrides: object) -> FinancialSummary:
    base: dict[str, object] = {
        "stock_code": "0000",
        "fiscal_period_end": _NOW.date(),
        "industry": None,
        "payout_ratio_pct": None,
        "forecast_bps": None,
        "equity_ratio_pct": None,
        "is_deficit": False,
        "source": _SOURCE,
    }
    base.update(overrides)
    return FinancialSummary(**base)  # type: ignore[arg-type]


def test_cyclical_and_income_composite_like_nippon_steel() -> None:
    financial = _financial(
        stock_code="5401", industry="鉄鋼", payout_ratio_pct=40.0
    )
    result = classify_stock_type(
        financial=financial,
        dividend_yield_pct=3.7,
        current_price=Decimal("636.9"),
        quarterly_operating_incomes=[Decimal("100"), Decimal("90"), Decimal("80")],
        disclosures=[],
        now=_NOW,
        config=_CONFIG,
        data_sources=[_SOURCE],
    )
    assert StockType.CYCLICAL in result.types
    assert StockType.INCOME in result.types
    assert result.confidence == ConfidenceLevel.HIGH


def test_growth_like_sanrio() -> None:
    financial = _financial(stock_code="8136", industry="その他製品")
    result = classify_stock_type(
        financial=financial,
        dividend_yield_pct=1.28,
        current_price=Decimal("1245.5"),
        quarterly_operating_incomes=[Decimal("100"), Decimal("120"), Decimal("150")],
        disclosures=[],
        now=_NOW,
        config=_CONFIG,
        data_sources=[_SOURCE],
    )
    assert StockType.GROWTH in result.types
    assert StockType.INCOME not in result.types


def test_income_and_defensive_composite_like_jt() -> None:
    financial = _financial(stock_code="2914", industry="食品", payout_ratio_pct=70.0)
    result = classify_stock_type(
        financial=financial,
        dividend_yield_pct=3.7,
        current_price=Decimal("6531"),
        quarterly_operating_incomes=[],
        disclosures=[],
        now=_NOW,
        config=_CONFIG,
        data_sources=[_SOURCE],
    )
    assert StockType.INCOME in result.types
    assert StockType.DEFENSIVE in result.types


def test_value_classification_capped_at_low_confidence() -> None:
    financial = _financial(forecast_bps=Decimal("1000"))
    result = classify_stock_type(
        financial=financial,
        dividend_yield_pct=3.0,
        current_price=Decimal("800"),  # PBR 0.8倍
        quarterly_operating_incomes=[],
        disclosures=[],
        now=_NOW,
        config=_CONFIG,
        data_sources=[_SOURCE],
    )
    assert StockType.VALUE in result.types
    assert result.confidence == ConfidenceLevel.LOW


def test_asset_play_classification_capped_at_low_confidence() -> None:
    financial = _financial(forecast_bps=Decimal("1000"), equity_ratio_pct=60.0)
    result = classify_stock_type(
        financial=financial,
        dividend_yield_pct=None,
        current_price=Decimal("600"),  # PBR 0.6倍
        quarterly_operating_incomes=[],
        disclosures=[],
        now=_NOW,
        config=_CONFIG,
        data_sources=[_SOURCE],
    )
    assert StockType.ASSET_PLAY in result.types
    assert result.confidence == ConfidenceLevel.LOW


def test_turnaround_classification() -> None:
    financial = _financial(is_deficit=True)
    result = classify_stock_type(
        financial=financial,
        dividend_yield_pct=None,
        current_price=Decimal("500"),
        quarterly_operating_incomes=[Decimal("-300"), Decimal("-200"), Decimal("-50")],
        disclosures=[],
        now=_NOW,
        config=_CONFIG,
        data_sources=[_SOURCE],
    )
    assert StockType.TURNAROUND in result.types


def test_event_driven_classification_from_disclosure_keyword() -> None:
    financial = _financial()
    disclosures = [
        Disclosure(
            stock_code="0000",
            published_at=_NOW,
            title="自己株式取得に関するお知らせ",
            category=None,
            source=_SOURCE,
        )
    ]
    result = classify_stock_type(
        financial=financial,
        dividend_yield_pct=None,
        current_price=Decimal("1000"),
        quarterly_operating_incomes=[],
        disclosures=disclosures,
        now=_NOW,
        config=_CONFIG,
        data_sources=[_SOURCE],
    )
    assert StockType.EVENT_DRIVEN in result.types


def test_no_match_results_in_empty_types_and_low_confidence() -> None:
    financial = _financial()
    result = classify_stock_type(
        financial=financial,
        dividend_yield_pct=None,
        current_price=Decimal("1000"),
        quarterly_operating_incomes=[],
        disclosures=[],
        now=_NOW,
        config=_CONFIG,
        data_sources=[_SOURCE],
    )
    assert result.types == []
    assert result.primary_type is None
    assert result.confidence == ConfidenceLevel.LOW
