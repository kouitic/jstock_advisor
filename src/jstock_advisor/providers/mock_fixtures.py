"""モックProvider共通の合成データ。

ここで生成される株価・財務・配当・優待データはすべて開発・テスト専用の
合成データであり、実在する企業の実際の値ではない(銘柄コード・銘柄名は
実在の会社を借りているが、数値はシード固定の乱数で生成した架空の値)。
本番運用では providers/*/xxx_impl.py として実データ提供元の実装に差し替える。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import random
from dataclasses import dataclass, field
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar

_calendar = BusinessCalendar.from_config(load_config().holiday_calendar)


def business_calendar() -> BusinessCalendar:
    """モック実装が共有する営業日カレンダー(価格系列の生成に使ったものと同一)。

    Issue #52 Phase B2 regression 是正で、mock provider が返す `as_of_date` を
    市場セッションの契約へ揃えるために公開した。系列生成と打ち切り判定で
    別々のカレンダーを使うと、生成された営業日が「存在しない日」と扱われうる。
    """
    return _calendar


_PRICE_SERIES_START = dt.date(2021, 1, 4)
_PRICE_SERIES_END = dt.date(2026, 12, 30)


@dataclass(frozen=True)
class QuarterlyProfile:
    operating_income: Decimal
    ordinary_income: Decimal
    operating_cashflow: Decimal


@dataclass(frozen=True)
class BenefitProfile:
    category: str
    description: str
    estimated_value: Decimal
    min_shares_for_tier: int
    long_term_holding_condition_months: int | None = None


@dataclass(frozen=True)
class MockStockProfile:
    stock_code: str
    stock_name: str
    industry: str
    market_segment: str
    base_price: float
    annual_drift_pct: float
    annual_volatility_pct: float
    base_avg_volume: int
    is_financial_sector: bool = False
    equity_ratio_pct: float = 45.0
    payout_ratio_pct: float = 40.0
    forecast_eps: Decimal = Decimal("0")
    forecast_bps: Decimal = Decimal("0")
    forecast_annual_dividend_per_share: Decimal = Decimal("0")
    previous_fiscal_year_dividend_per_share: Decimal = Decimal("0")
    is_progressive_or_doe_policy: bool = False
    consecutive_dividend_increase_years: int | None = None
    quarters: tuple[QuarterlyProfile, ...] = field(default_factory=tuple)
    benefits: tuple[BenefitProfile, ...] = field(default_factory=tuple)
    benefit_min_shares: int = 100
    benefit_frequency_per_year: int = 2


MOCK_STOCKS: dict[str, MockStockProfile] = {
    "2914": MockStockProfile(
        stock_code="2914",
        stock_name="日本たばこ産業(モックデータ)",
        industry="食料品",
        market_segment="プライム",
        base_price=4200.0,
        annual_drift_pct=3.0,
        annual_volatility_pct=18.0,
        base_avg_volume=3_500_000,
        equity_ratio_pct=52.0,
        payout_ratio_pct=65.0,
        forecast_eps=Decimal("270"),
        forecast_bps=Decimal("2100"),
        forecast_annual_dividend_per_share=Decimal("194"),
        previous_fiscal_year_dividend_per_share=Decimal("188"),
        is_progressive_or_doe_policy=True,
        consecutive_dividend_increase_years=3,
        quarters=(
            # TTM(直近12ヶ月移動合計)による季節調整の検証に必要なため8四半期(2年)分を保持する
            QuarterlyProfile(
                Decimal("160000000000"), Decimal("155000000000"), Decimal("145000000000")
            ),
            QuarterlyProfile(
                Decimal("165000000000"), Decimal("160000000000"), Decimal("150000000000")
            ),
            QuarterlyProfile(
                Decimal("170000000000"), Decimal("165000000000"), Decimal("155000000000")
            ),
            QuarterlyProfile(
                Decimal("175000000000"), Decimal("170000000000"), Decimal("158000000000")
            ),
            QuarterlyProfile(
                Decimal("180000000000"), Decimal("175000000000"), Decimal("160000000000")
            ),
            QuarterlyProfile(
                Decimal("190000000000"), Decimal("185000000000"), Decimal("170000000000")
            ),
            QuarterlyProfile(
                Decimal("195000000000"), Decimal("190000000000"), Decimal("175000000000")
            ),
            QuarterlyProfile(
                Decimal("200000000000"), Decimal("196000000000"), Decimal("182000000000")
            ),
        ),
        benefits=(),
    ),
    "9861": MockStockProfile(
        stock_code="9861",
        stock_name="吉野家ホールディングス(モックデータ)",
        industry="小売業",
        market_segment="プライム",
        base_price=2600.0,
        annual_drift_pct=4.0,
        annual_volatility_pct=25.0,
        base_avg_volume=800_000,
        equity_ratio_pct=48.0,
        payout_ratio_pct=35.0,
        forecast_eps=Decimal("55"),
        forecast_bps=Decimal("980"),
        forecast_annual_dividend_per_share=Decimal("20"),
        previous_fiscal_year_dividend_per_share=Decimal("18"),
        is_progressive_or_doe_policy=False,
        consecutive_dividend_increase_years=1,
        quarters=(
            QuarterlyProfile(Decimal("1800000000"), Decimal("1750000000"), Decimal("1600000000")),
            QuarterlyProfile(Decimal("1900000000"), Decimal("1850000000"), Decimal("1700000000")),
            QuarterlyProfile(Decimal("1950000000"), Decimal("1900000000"), Decimal("1750000000")),
            QuarterlyProfile(Decimal("2000000000"), Decimal("1950000000"), Decimal("1800000000")),
            QuarterlyProfile(Decimal("2100000000"), Decimal("2050000000"), Decimal("1900000000")),
            QuarterlyProfile(Decimal("2200000000"), Decimal("2150000000"), Decimal("2000000000")),
            QuarterlyProfile(Decimal("2300000000"), Decimal("2250000000"), Decimal("2100000000")),
            QuarterlyProfile(Decimal("2400000000"), Decimal("2350000000"), Decimal("2200000000")),
        ),
        benefits=(
            BenefitProfile(
                category="IN_HOUSE_SERVICE",
                description="優待食事券(300円券×10枚相当)",
                estimated_value=Decimal("3000"),
                min_shares_for_tier=100,
                long_term_holding_condition_months=None,
            ),
        ),
        benefit_min_shares=100,
        benefit_frequency_per_year=2,
    ),
    "8136": MockStockProfile(
        stock_code="8136",
        stock_name="サンリオ(モックデータ)",
        industry="その他製品",
        market_segment="プライム",
        base_price=4300.0,
        annual_drift_pct=8.0,
        annual_volatility_pct=32.0,
        base_avg_volume=1_200_000,
        equity_ratio_pct=60.0,
        payout_ratio_pct=45.0,
        forecast_eps=Decimal("160"),
        forecast_bps=Decimal("900"),
        forecast_annual_dividend_per_share=Decimal("70"),
        previous_fiscal_year_dividend_per_share=Decimal("60"),
        is_progressive_or_doe_policy=False,
        consecutive_dividend_increase_years=2,
        quarters=(
            QuarterlyProfile(Decimal("6500000000"), Decimal("6300000000"), Decimal("5200000000")),
            QuarterlyProfile(Decimal("7200000000"), Decimal("7000000000"), Decimal("5800000000")),
            QuarterlyProfile(Decimal("7900000000"), Decimal("7700000000"), Decimal("6500000000")),
            QuarterlyProfile(Decimal("8400000000"), Decimal("8200000000"), Decimal("7000000000")),
            QuarterlyProfile(Decimal("9000000000"), Decimal("8800000000"), Decimal("7500000000")),
            QuarterlyProfile(Decimal("9500000000"), Decimal("9300000000"), Decimal("8000000000")),
            QuarterlyProfile(Decimal("10200000000"), Decimal("10000000000"), Decimal("8600000000")),
            QuarterlyProfile(Decimal("11000000000"), Decimal("10800000000"), Decimal("9200000000")),
        ),
        benefits=(
            BenefitProfile(
                category="IN_HOUSE_SERVICE",
                description="サンリオピューロランド優待入園券",
                estimated_value=Decimal("2400"),
                min_shares_for_tier=100,
                long_term_holding_condition_months=12,
            ),
        ),
        benefit_min_shares=100,
        benefit_frequency_per_year=1,
    ),
    "8306": MockStockProfile(
        stock_code="8306",
        stock_name="三菱UFJフィナンシャル・グループ(モックデータ)",
        industry="銀行業",
        market_segment="プライム",
        base_price=1800.0,
        annual_drift_pct=5.0,
        annual_volatility_pct=22.0,
        base_avg_volume=40_000_000,
        is_financial_sector=True,
        equity_ratio_pct=6.0,  # 銀行は自己資本比率規制が別体系(一次スクリーニングで除外対象)
        payout_ratio_pct=40.0,
        forecast_eps=Decimal("150"),
        forecast_bps=Decimal("1400"),
        forecast_annual_dividend_per_share=Decimal("60"),
        previous_fiscal_year_dividend_per_share=Decimal("55"),
        quarters=(
            QuarterlyProfile(
                Decimal("370000000000"), Decimal("390000000000"), Decimal("470000000000")
            ),
            QuarterlyProfile(
                Decimal("380000000000"), Decimal("400000000000"), Decimal("480000000000")
            ),
            QuarterlyProfile(
                Decimal("390000000000"), Decimal("410000000000"), Decimal("490000000000")
            ),
            QuarterlyProfile(
                Decimal("395000000000"), Decimal("415000000000"), Decimal("495000000000")
            ),
            QuarterlyProfile(
                Decimal("400000000000"), Decimal("420000000000"), Decimal("500000000000")
            ),
            QuarterlyProfile(
                Decimal("410000000000"), Decimal("430000000000"), Decimal("510000000000")
            ),
            QuarterlyProfile(
                Decimal("420000000000"), Decimal("440000000000"), Decimal("520000000000")
            ),
            QuarterlyProfile(
                Decimal("430000000000"), Decimal("450000000000"), Decimal("530000000000")
            ),
        ),
        benefits=(),
    ),
}


def _seed_for(*parts: str) -> int:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return int(digest[:16], 16)


def _generate_close_and_volume_series(
    profile: MockStockProfile,
) -> dict[dt.date, tuple[float, int]]:
    rng = random.Random(_seed_for("price", profile.stock_code))
    price = profile.base_price
    daily_drift = profile.annual_drift_pct / 100 / 252
    daily_vol = profile.annual_volatility_pct / 100 / (252**0.5)
    series: dict[dt.date, tuple[float, int]] = {}
    current = _PRICE_SERIES_START
    while current <= _PRICE_SERIES_END:
        if _calendar.is_business_day(current):
            price *= 1 + daily_drift + rng.gauss(0, daily_vol)
            price = max(price, 1.0)
            volume = max(
                int(rng.gauss(profile.base_avg_volume, profile.base_avg_volume * 0.3)), 1000
            )
            series[current] = (round(price, 1), volume)
        current += dt.timedelta(days=1)
    return series


_SERIES_CACHE: dict[str, dict[dt.date, tuple[float, int]]] = {}


def get_price_volume_series(stock_code: str) -> dict[dt.date, tuple[float, int]]:
    if stock_code not in MOCK_STOCKS:
        return {}
    if stock_code not in _SERIES_CACHE:
        _SERIES_CACHE[stock_code] = _generate_close_and_volume_series(MOCK_STOCKS[stock_code])
    return _SERIES_CACHE[stock_code]


_BENCHMARK_START_VALUE = 2500.0
_BENCHMARK_ANNUAL_DRIFT_PCT = 4.0
_BENCHMARK_ANNUAL_VOL_PCT = 15.0


def _generate_benchmark_series(symbol: str) -> dict[dt.date, float]:
    rng = random.Random(_seed_for("benchmark", symbol))
    value = _BENCHMARK_START_VALUE
    daily_drift = _BENCHMARK_ANNUAL_DRIFT_PCT / 100 / 252
    daily_vol = _BENCHMARK_ANNUAL_VOL_PCT / 100 / (252**0.5)
    series: dict[dt.date, float] = {}
    current = _PRICE_SERIES_START
    while current <= _PRICE_SERIES_END:
        if _calendar.is_business_day(current):
            value *= 1 + daily_drift + rng.gauss(0, daily_vol)
            value = max(value, 1.0)
            series[current] = round(value, 2)
        current += dt.timedelta(days=1)
    return series


_BENCHMARK_CACHE: dict[str, dict[dt.date, float]] = {}


def get_benchmark_series(symbol: str) -> dict[dt.date, float]:
    if symbol not in _BENCHMARK_CACHE:
        _BENCHMARK_CACHE[symbol] = _generate_benchmark_series(symbol)
    return _BENCHMARK_CACHE[symbol]
