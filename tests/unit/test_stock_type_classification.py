import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.classification.stock_type import classify_stock_type
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import ConfidenceLevel, StockType
from jstock_advisor.interfaces.types import Disclosure, DividendInfo, FinancialSummary

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


def test_growth_independent_of_dividend_yield() -> None:
    """BUY候補裾野拡大機能(2026-08、指摘4): GROWTHは配当利回りに関係なく
    営業利益トレンドのみで独立判定される(高配当かつ成長トレンドの銘柄も
    GROWTHへ分類される)。"""
    financial = _financial(stock_code="5401", industry="鉄鋼", payout_ratio_pct=40.0)
    result = classify_stock_type(
        financial=financial,
        dividend_yield_pct=5.0,  # 高配当
        current_price=Decimal("636.9"),
        quarterly_operating_incomes=[Decimal("80"), Decimal("90"), Decimal("100")],  # 増加トレンド
        disclosures=[],
        now=_NOW,
        config=_CONFIG,
        data_sources=[_SOURCE],
    )
    assert StockType.GROWTH in result.types
    assert StockType.INCOME in result.types  # 複合タイプとして両方成立してよい


def test_growth_does_not_require_high_roe() -> None:
    """簡易予想ROEはGROWTHの分類条件には使わない(参考情報のみ)。
    forecast_eps/forecast_bps未設定(ROE算出不可)でも営業利益トレンドのみで
    GROWTHに分類される。"""
    financial = _financial(forecast_bps=None)
    result = classify_stock_type(
        financial=financial,
        dividend_yield_pct=None,
        current_price=Decimal("1000"),
        quarterly_operating_incomes=[Decimal("100"), Decimal("120"), Decimal("150")],
        disclosures=[],
        now=_NOW,
        config=_CONFIG,
        data_sources=[_SOURCE],
    )
    assert StockType.GROWTH in result.types


def test_value_independent_of_dividend_yield_via_pbr() -> None:
    """BUY候補裾野拡大機能(2026-08、指摘4): VALUEは配当利回り0でもPBR基準のみで成立する。"""
    financial = _financial(forecast_bps=Decimal("1000"))
    result = classify_stock_type(
        financial=financial,
        dividend_yield_pct=None,
        current_price=Decimal("800"),  # PBR 0.8倍
        quarterly_operating_incomes=[],
        disclosures=[],
        now=_NOW,
        config=_CONFIG,
        data_sources=[_SOURCE],
    )
    assert StockType.VALUE in result.types


def test_value_via_per_only_without_pbr_data() -> None:
    """forecast_bpsが無くforecast_epsのみある場合でも、PER基準単独でVALUEが成立する
    (current_per/current_pbrはclassify_stock_type内で自己完結的に算出する)。"""
    financial = _financial(forecast_bps=None, forecast_eps=Decimal("100"))
    result = classify_stock_type(
        financial=financial,
        dividend_yield_pct=None,
        current_price=Decimal("1000"),  # PER 10倍 < config.value.max_per(15)
        quarterly_operating_incomes=[],
        disclosures=[],
        now=_NOW,
        config=_CONFIG,
        data_sources=[_SOURCE],
    )
    assert StockType.VALUE in result.types


def test_dividend_growth_classified_with_long_streak_despite_low_growth_rate() -> None:
    """BUY候補裾野拡大機能(2026-08、指摘4): 増配率が小さくても連続増配年数が
    基準を満たせばDIVIDEND_GROWTHに分類される(min_dividend_growth_pctは
    ハード条件として使わない)。"""
    financial = _financial()
    dividend = DividendInfo(
        stock_code="0000",
        fiscal_year="2026",
        forecast_annual_dividend_per_share=Decimal("100.1"),
        previous_fiscal_year_dividend_per_share=Decimal("100.0"),  # 増配率0.1%のみ
        consecutive_dividend_increase_years=10,
        source=_SOURCE,
    )
    result = classify_stock_type(
        financial=financial,
        dividend_yield_pct=None,
        current_price=Decimal("1000"),
        quarterly_operating_incomes=[],
        disclosures=[],
        now=_NOW,
        config=_CONFIG,
        data_sources=[_SOURCE],
        dividend=dividend,
    )
    assert StockType.DIVIDEND_GROWTH in result.types


def test_dividend_growth_excluded_when_forecast_dividend_is_a_cut() -> None:
    """今期予想配当が前期実績を下回る場合はDIVIDEND_GROWTHに分類しない
    (連続増配年数の基準を満たしていても、今期予想の減配を優先する)。"""
    financial = _financial()
    dividend = DividendInfo(
        stock_code="0000",
        fiscal_year="2026",
        forecast_annual_dividend_per_share=Decimal("90"),
        previous_fiscal_year_dividend_per_share=Decimal("100"),
        consecutive_dividend_increase_years=10,
        source=_SOURCE,
    )
    result = classify_stock_type(
        financial=financial,
        dividend_yield_pct=None,
        current_price=Decimal("1000"),
        quarterly_operating_incomes=[],
        disclosures=[],
        now=_NOW,
        config=_CONFIG,
        data_sources=[_SOURCE],
        dividend=dividend,
    )
    assert StockType.DIVIDEND_GROWTH not in result.types


def test_dividend_growth_not_classified_below_min_years() -> None:
    financial = _financial()
    dividend = DividendInfo(
        stock_code="0000",
        fiscal_year="2026",
        consecutive_dividend_increase_years=2,  # 下限3年未満
        source=_SOURCE,
    )
    result = classify_stock_type(
        financial=financial,
        dividend_yield_pct=None,
        current_price=Decimal("1000"),
        quarterly_operating_incomes=[],
        disclosures=[],
        now=_NOW,
        config=_CONFIG,
        data_sources=[_SOURCE],
        dividend=dividend,
    )
    assert StockType.DIVIDEND_GROWTH not in result.types


def test_quality_classification_when_all_conditions_met() -> None:
    financial = _financial(
        equity_ratio_pct=45.0,  # config.quality.min_equity_ratio_pct(40)以上
        forecast_eps=Decimal("80"),
        forecast_bps=Decimal("800"),  # 簡易予想ROE=10% >= min_roe_pct(8)
        operating_cashflow=Decimal("100"),
    )
    result = classify_stock_type(
        financial=financial,
        dividend_yield_pct=None,
        current_price=Decimal("2000"),
        quarterly_operating_incomes=[Decimal("100"), Decimal("110"), Decimal("120")],
        disclosures=[],
        now=_NOW,
        config=_CONFIG,
        data_sources=[_SOURCE],
    )
    assert StockType.QUALITY in result.types


def test_quality_not_classified_when_roe_below_threshold() -> None:
    financial = _financial(
        equity_ratio_pct=45.0,
        forecast_eps=Decimal("10"),
        forecast_bps=Decimal("800"),  # 簡易予想ROE=1.25% < min_roe_pct(8)
        operating_cashflow=Decimal("100"),
    )
    result = classify_stock_type(
        financial=financial,
        dividend_yield_pct=None,
        current_price=Decimal("2000"),
        quarterly_operating_incomes=[Decimal("100"), Decimal("110"), Decimal("120")],
        disclosures=[],
        now=_NOW,
        config=_CONFIG,
        data_sources=[_SOURCE],
    )
    assert StockType.QUALITY not in result.types


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
