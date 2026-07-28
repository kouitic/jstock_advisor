"""適正価格算出(要求仕様10節)。

利用可能な方式のみを使用し、算出できない方式はNoneを返す(推測で補完しない)。
最終適正価格は設定(valuation_rules.yaml)で指定された方式で複数候補を集約する。
"""

from __future__ import annotations

import datetime as dt
import statistics
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, NamedTuple

from jstock_advisor.interfaces.types import HistoricalValuation, PriceBar

FairValueMethod = Literal["target_yield", "per", "pbr", "historical_range"]


class FairValueCandidate(NamedTuple):
    method: FairValueMethod
    price: Decimal
    rationale: str


def round_yen(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def compute_target_yield_price(
    forecast_annual_dividend_per_share: Decimal | None, target_dividend_yield_pct: float
) -> Decimal | None:
    """配当基準価格 = 予想年間配当金 ÷ 目標配当利回り。"""
    if forecast_annual_dividend_per_share is None or forecast_annual_dividend_per_share <= 0:
        return None
    if target_dividend_yield_pct <= 0:
        return None
    return forecast_annual_dividend_per_share / (Decimal(str(target_dividend_yield_pct)) / 100)


def compute_target_total_yield_price(
    forecast_annual_dividend_per_share: Decimal,
    annual_benefit_value_at_min_lot: Decimal,
    min_shares_required: int,
    target_total_yield_pct: float,
) -> Decimal | None:
    """優待取得株数(min_shares_required)を保有する前提の総合利回り基準価格。

    総合利回り(price) = 配当利回り(price) + 優待利回り(price)
                      = (配当金/price) + (優待評価額/(min_shares*price))
                      = (配当金 + 優待評価額/min_shares) / price
    上式をprice = ... の形に解いたもの。
    """
    if target_total_yield_pct <= 0 or min_shares_required <= 0:
        return None
    if forecast_annual_dividend_per_share <= 0 and annual_benefit_value_at_min_lot <= 0:
        return None
    per_share_equivalent = forecast_annual_dividend_per_share + (
        annual_benefit_value_at_min_lot / min_shares_required
    )
    return per_share_equivalent / (Decimal(str(target_total_yield_pct)) / 100)


def compute_per_price(
    forecast_eps: Decimal | None, historical_per_median: Decimal | None
) -> Decimal | None:
    """PER基準価格 = 予想EPS × 過去PER中央値。"""
    if forecast_eps is None or historical_per_median is None:
        return None
    if forecast_eps <= 0 or historical_per_median <= 0:
        return None
    return forecast_eps * historical_per_median


def compute_pbr_price(
    forecast_bps: Decimal | None, historical_pbr_median: Decimal | None
) -> Decimal | None:
    """PBR基準価格 = 予想BPS × 過去PBR中央値。"""
    if forecast_bps is None or historical_pbr_median is None:
        return None
    if forecast_bps <= 0 or historical_pbr_median <= 0:
        return None
    return forecast_bps * historical_pbr_median


def compute_dcf_price(
    operating_cashflow: Decimal | None,
    capital_expenditure: Decimal | None,
    shares_outstanding: Decimal | None,
    discount_rate_pct: float,
    terminal_growth_rate_pct: float,
    projection_years: int,
) -> Decimal | None:
    """簡易DCF法(要求仕様8節)。

    完全なCAPM(リスクフリーレート・株式リスクプレミアム・ベータ)を算出する
    データソースが無いため、固定割引率(discount_rate_pct)による簡易DCFとする。
    FCF = 営業CF + 設備投資(yfinanceのCapital Expenditureは通常、キャッシュ
    アウトフローを表す負値として報告されるため加算する)。この手法は割引率が
    固定である旨を利用側に伝え、confidenceをMEDIUM上限として扱うこと。
    """
    if operating_cashflow is None or capital_expenditure is None or shares_outstanding is None:
        return None
    if shares_outstanding <= 0 or discount_rate_pct <= terminal_growth_rate_pct:
        return None

    fcf = operating_cashflow + capital_expenditure
    if fcf <= 0:
        return None

    discount_rate = Decimal(str(discount_rate_pct)) / 100
    terminal_growth = Decimal(str(terminal_growth_rate_pct)) / 100

    pv_sum = Decimal("0")
    for year in range(1, projection_years + 1):
        pv_sum += fcf / ((1 + discount_rate) ** year)

    terminal_value = fcf * (1 + terminal_growth) / (discount_rate - terminal_growth)
    pv_terminal = terminal_value / ((1 + discount_rate) ** projection_years)

    enterprise_value_proxy = pv_sum + pv_terminal
    return enterprise_value_proxy / shares_outstanding


def median_historical_per(values: list[HistoricalValuation]) -> Decimal | None:
    pers = [v.per for v in values if v.per is not None and v.per > 0]
    if not pers:
        return None
    return statistics.median(pers)


def median_historical_pbr(values: list[HistoricalValuation]) -> Decimal | None:
    pbrs = [v.pbr for v in values if v.pbr is not None and v.pbr > 0]
    if not pbrs:
        return None
    return statistics.median(pbrs)


def _low_over_window(
    bars: list[PriceBar], as_of_date: dt.date, lookback_days: int
) -> Decimal | None:
    window_start = as_of_date - dt.timedelta(days=lookback_days)
    lows = [bar.low for bar in bars if window_start <= bar.date <= as_of_date]
    if not lows:
        return None
    return min(lows)


def compute_historical_range_price(
    bars: list[PriceBar],
    as_of_date: dt.date,
    lookback_years: int,
    use_52_week_low: bool = True,
) -> Decimal | None:
    """過去株価レンジ方式の基準価格。MVPでは52週安値・過去N年安値の平均を用いる。

    支持線候補・業績変化調整レンジは将来拡張ポイント(現状は未実装で無視される)。
    """
    candidates: list[Decimal] = []
    if use_52_week_low:
        low_52w = _low_over_window(bars, as_of_date, 365)
        if low_52w is not None:
            candidates.append(low_52w)
    low_ny = _low_over_window(bars, as_of_date, 365 * lookback_years)
    if low_ny is not None:
        candidates.append(low_ny)
    if not candidates:
        return None
    return sum(candidates, Decimal("0")) / len(candidates)


def aggregate_fair_value(
    candidates: dict[str, Decimal | None],
    aggregation_method: str,
    method_weights: dict[str, float] | None = None,
) -> Decimal | None:
    available = {k: v for k, v in candidates.items() if v is not None}
    if not available:
        return None

    if aggregation_method == "median":
        return round_yen(statistics.median(available.values()))
    if aggregation_method == "mean":
        return round_yen(sum(available.values(), Decimal("0")) / len(available))
    if aggregation_method == "weighted":
        weights = method_weights or {}
        total_weight = sum(weights.get(k, 0.0) for k in available)
        if total_weight <= 0:
            return None
        weighted_sum = sum(
            (available[k] * Decimal(str(weights.get(k, 0.0))) for k in available), Decimal("0")
        )
        return round_yen(weighted_sum / Decimal(str(total_weight)))
    raise ValueError(f"unknown aggregation method: {aggregation_method}")
