"""買い判定(要求仕様9節・10節)。

screening_ruleを通過した銘柄について、割安条件・スコアを踏まえて買い候補として
提示すべきかを判定する。急落中というだけの理由で割安評価を高くしないよう、
業績が重大に悪化している場合は価格系の割安シグナルを無効化する。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.config.models import ScoringWeightsConfig
from jstock_advisor.domain.entities.common import BuyPriceLevels
from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.scoring.score import ScoreResult, UndervaluationSignals
from jstock_advisor.domain.screening.rules import ScreeningResult
from jstock_advisor.interfaces.types import PriceBar, ShareholderBenefit

# 以下はいずれもMVPの初期値。バックテスト(要求仕様44節)を通じて見直す対象とする。
_EARNINGS_SEVERE_DECLINE_THRESHOLD_PCT = -30.0
_DRAWDOWN_FROM_HIGH_THRESHOLD_PCT = -15.0
_PRICE_DOWN_DESPITE_STABLE_EARNINGS_THRESHOLD_PCT = -10.0
_STRONG_SCORE_RATIO = 0.7
_WEAK_SCORE_RATIO = 0.3

_SCORE_LABELS: dict[str, str] = {
    "total_yield_attractiveness": "総合利回りの魅力度",
    "dividend_sustainability": "配当持続性",
    "financial_health": "財務健全性",
    "undervaluation": "割安度",
    "shareholder_benefit_value": "株主優待価値",
    "earnings_stability": "業績安定性",
    "price_stability": "株価安定性",
}


@dataclass(frozen=True)
class BuySignalResult:
    recommended: bool
    exclusion_reasons: list[str]
    positive_reasons: list[str]
    counter_factors: list[str]
    key_risks: list[str]
    confidence: ConfidenceLevel


def has_severe_earnings_decline(quarterly_operating_incomes: list[Decimal]) -> bool:
    if len(quarterly_operating_incomes) < 2:
        return False
    latest, previous = quarterly_operating_incomes[-1], quarterly_operating_incomes[-2]
    if previous <= 0:
        return False
    change_pct = float((latest / previous - 1) * 100)
    return change_pct <= _EARNINGS_SEVERE_DECLINE_THRESHOLD_PCT


def is_earnings_trend_non_decreasing(quarterly_operating_incomes: list[Decimal]) -> bool | None:
    if len(quarterly_operating_incomes) < 2:
        return None
    return all(
        quarterly_operating_incomes[i] >= quarterly_operating_incomes[i - 1]
        for i in range(1, len(quarterly_operating_incomes))
    )


def compute_drawdown_from_52w_high_pct(
    current_price: Decimal, bars: list[PriceBar], as_of_date: dt.date
) -> float | None:
    window_start = as_of_date - dt.timedelta(days=365)
    highs = [b.high for b in bars if window_start <= b.date <= as_of_date]
    if not highs:
        return None
    high_52w = max(highs)
    if high_52w <= 0:
        return None
    return float(current_price / high_52w - 1) * 100


def estimate_historical_average_dividend_yield_pct(
    previous_fiscal_year_dividend_per_share: Decimal | None, bars: list[PriceBar]
) -> float | None:
    """簡易推定値。真の履歴配当利回り時系列データが無いためのMVP代替指標として、
    過去の株価推移の平均値に対する前期配当実績の利回りを近似値として用いる。
    """
    if (
        previous_fiscal_year_dividend_per_share is None
        or previous_fiscal_year_dividend_per_share <= 0
    ):
        return None
    closes = [b.close for b in bars if b.close > 0]
    if not closes:
        return None
    average_price = sum(closes, Decimal("0")) / len(closes)
    if average_price <= 0:
        return None
    return float(previous_fiscal_year_dividend_per_share / average_price * 100)


def compute_recent_price_change_pct(
    bars: list[PriceBar], as_of_date: dt.date, lookback_days: int
) -> float | None:
    window_start = as_of_date - dt.timedelta(days=lookback_days)
    candidates = sorted((b for b in bars if b.date <= as_of_date), key=lambda b: b.date)
    past_candidates = [b for b in candidates if b.date <= window_start]
    if not candidates or not past_candidates:
        return None
    current = candidates[-1].close
    past = past_candidates[-1].close
    if past <= 0:
        return None
    return float(current / past - 1) * 100


def compute_undervaluation_signals(
    current_price: Decimal,
    current_per: Decimal | None,
    historical_per_median: Decimal | None,
    current_pbr: Decimal | None,
    historical_pbr_median: Decimal | None,
    current_dividend_yield_pct: float | None,
    historical_average_dividend_yield_pct: float | None,
    drawdown_from_52w_high_pct: float | None,
    buy_prices: BuyPriceLevels | None,
    recent_price_change_pct: float | None,
    earnings_trend_non_decreasing: bool | None,
    severe_earnings_decline: bool,
) -> UndervaluationSignals:
    per_below_median = None
    if current_per is not None and historical_per_median is not None:
        per_below_median = current_per < historical_per_median

    pbr_below_median = None
    if current_pbr is not None and historical_pbr_median is not None:
        pbr_below_median = current_pbr < historical_pbr_median

    dividend_yield_above_historical_average = None
    if current_dividend_yield_pct is not None and historical_average_dividend_yield_pct is not None:
        dividend_yield_above_historical_average = (
            current_dividend_yield_pct > historical_average_dividend_yield_pct
        )

    drawdown_signal = None
    if drawdown_from_52w_high_pct is not None:
        drawdown_signal = drawdown_from_52w_high_pct <= _DRAWDOWN_FROM_HIGH_THRESHOLD_PCT

    below_fair_value = None
    if buy_prices is not None and buy_prices.standard is not None:
        below_fair_value = current_price <= buy_prices.standard.price

    price_down_despite_stable_earnings = None
    if (
        recent_price_change_pct is not None
        and earnings_trend_non_decreasing is not None
        and not severe_earnings_decline
    ):
        price_down_despite_stable_earnings = (
            recent_price_change_pct <= _PRICE_DOWN_DESPITE_STABLE_EARNINGS_THRESHOLD_PCT
            and earnings_trend_non_decreasing
        )

    # 急落中というだけで割安評価を高くしない(要求仕様9節末尾)。
    # 業績が重大に悪化している場合、価格ベースの割安シグナルは無効化(False)する。
    if severe_earnings_decline:
        if drawdown_signal is not None:
            drawdown_signal = False
        if below_fair_value is not None:
            below_fair_value = False

    return UndervaluationSignals(
        per_below_median=per_below_median,
        pbr_below_median=pbr_below_median,
        dividend_yield_above_historical_average=dividend_yield_above_historical_average,
        drawdown_from_52w_high=drawdown_signal,
        below_fair_value=below_fair_value,
        price_down_despite_stable_earnings=price_down_despite_stable_earnings,
    )


def _score_areas(
    score_result: ScoreResult, config: ScoringWeightsConfig, ratio: float, above: bool
) -> list[str]:
    weights = config.weights
    areas = []
    for field_name, label in _SCORE_LABELS.items():
        max_weight = getattr(weights, field_name)
        score = getattr(score_result.breakdown, field_name)
        if max_weight <= 0:
            continue
        component_ratio = score / max_weight
        if (above and component_ratio >= ratio) or (not above and component_ratio < ratio):
            areas.append(f"{label}({score:.1f}/{max_weight}点)")
    return areas


def determine_confidence(
    fair_value_methods_used_count: int,
    data_sources_count: int,
    has_stale_data_warning: bool,
) -> ConfidenceLevel:
    if has_stale_data_warning or fair_value_methods_used_count == 0:
        return ConfidenceLevel.LOW
    if fair_value_methods_used_count >= 3 and data_sources_count >= 3:
        return ConfidenceLevel.HIGH
    return ConfidenceLevel.MEDIUM


def evaluate_buy_signal(
    screening_result: ScreeningResult,
    severe_earnings_decline: bool,
    benefit: ShareholderBenefit | None,
    score_result: ScoreResult,
    scoring_config: ScoringWeightsConfig,
    fair_value: Decimal | None,
    buy_prices: BuyPriceLevels | None,
    fair_value_methods_used_count: int,
    data_sources_count: int,
    has_stale_data_warning: bool,
) -> BuySignalResult:
    exclusion_reasons = list(screening_result.exclusion_reasons)
    if severe_earnings_decline:
        exclusion_reasons.append("直近決算で重大な業績悪化(営業利益が前期比30%超悪化)")
    if benefit is not None and benefit.is_abolished:
        exclusion_reasons.append("株主優待の廃止が発表されている")
    if fair_value is None or buy_prices is None:
        exclusion_reasons.append("適正価格を算出できるデータがない")

    recommended = not exclusion_reasons

    counter_factors = list(screening_result.warnings)
    if benefit is not None and benefit.is_major_downgrade:
        counter_factors.append("株主優待の内容が改悪された可能性がある")
    counter_factors.extend(
        f"{area}が弱い"
        for area in _score_areas(score_result, scoring_config, _WEAK_SCORE_RATIO, above=False)
    )

    positive_reasons: list[str] = []
    if recommended:
        positive_reasons.extend(
            f"{area}が高評価"
            for area in _score_areas(score_result, scoring_config, _STRONG_SCORE_RATIO, above=True)
        )
        if not positive_reasons:
            positive_reasons.append("必須条件を満たし、総合利回り基準をクリア")

    key_risks = list(counter_factors)

    confidence = determine_confidence(
        fair_value_methods_used_count, data_sources_count, has_stale_data_warning
    )

    return BuySignalResult(
        recommended=recommended,
        exclusion_reasons=exclusion_reasons,
        positive_reasons=positive_reasons,
        counter_factors=counter_factors,
        key_risks=key_risks,
        confidence=confidence,
    )
