"""買い判定の統計ヘルパー(要求仕様9節)。

割安条件・急落判定等の純粋なデータ変換関数を提供する。BuyAction判定の
オーケストレーションは`domain/signals/buy_decision.py`が担う(2026-07
BUYパイプライン再設計により、旧`evaluate_buy_signal()`/`determine_confidence()`/
`BuySignalResult`は`buy_decision.py`/`valuation_confidence.py`へ置き換えられた)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.config.models import ScoringWeightsConfig
from jstock_advisor.domain.scoring.score import ScoreResult, UndervaluationSignals
from jstock_advisor.interfaces.types import PriceBar

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
    valuation_anchor: Decimal | None,
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
    if valuation_anchor is not None:
        below_fair_value = current_price <= valuation_anchor

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


def score_areas(
    score_result: ScoreResult, config: ScoringWeightsConfig, ratio: float, above: bool
) -> list[str]:
    """スコア内訳のうち、配点比でratio以上(above=True)/未満(above=False)の
    項目をラベル付きで返す(通知文の「主な評価理由」「弱み」表示に使う)。
    """
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


