"""買い候補スコアリング(要求仕様15節)。100点満点、内訳と算出根拠を保持する。

減配・重大な不祥事・債務超過・継続企業リスク等はスコアではなく screening/sell_rules 側の
除外条件で扱う(このモジュールはスクリーニングを通過した銘柄のみを対象とする)。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from decimal import Decimal

from jstock_advisor.config.models import ScoringWeightsConfig, UndervaluationCategoryCaps
from jstock_advisor.domain.entities.common import ScoreBreakdown
from jstock_advisor.domain.scoring.undervaluation_categories import (
    score_undervaluation_categories,
)
from jstock_advisor.interfaces.types import DividendInfo, FinancialSummary, PriceBar

# 株主優待利回りがこの値(%)以上で株主優待スコアが満点になる基準値。
# 総合利回り基準(3.5%)の一部として優待単独で意味を持つ水準を想定したMVP初期値。
_BENEFIT_YIELD_FULL_SCORE_PCT = 2.0
# 財務健全性スコアが満点になる自己資本比率の上限アンカー(%)。
_FINANCIAL_HEALTH_UPPER_ANCHOR_PCT = 70.0
# 株価安定性スコアの基準となる年率ボラティリティのレンジ(%)。
_PRICE_STABILITY_LOW_VOL_PCT = 15.0
_PRICE_STABILITY_HIGH_VOL_PCT = 45.0
_TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class UndervaluationSignals:
    """要求仕様9節の割安条件。判定不能な項目はNoneとし、集計対象から除外する。"""

    per_below_median: bool | None = None
    pbr_below_median: bool | None = None
    dividend_yield_above_historical_average: bool | None = None
    drawdown_from_52w_high: bool | None = None
    below_fair_value: bool | None = None
    price_down_despite_stable_earnings: bool | None = None

    def available(self) -> dict[str, bool]:
        values = {
            "per_below_median": self.per_below_median,
            "pbr_below_median": self.pbr_below_median,
            "dividend_yield_above_historical_average": self.dividend_yield_above_historical_average,
            "drawdown_from_52w_high": self.drawdown_from_52w_high,
            "below_fair_value": self.below_fair_value,
            "price_down_despite_stable_earnings": self.price_down_despite_stable_earnings,
        }
        return {k: v for k, v in values.items() if v is not None}


@dataclass(frozen=True)
class ScoreResult:
    breakdown: ScoreBreakdown
    formulas: dict[str, str] = field(default_factory=dict)
    # Phase 2-B「銘柄分析」向け(2026-08): 判定に実際に使用した入力値のうち、
    # breakdown(算出結果の点数)には残らないものをそのまま保持する(表示専用、
    # 判定ロジックからは参照しない)。
    input_facts: dict[str, object] = field(default_factory=dict)


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _linear_score(value: float, weight: float, zero_at: float, full_at: float) -> float:
    if full_at == zero_at:
        return 0.0
    ratio = (value - zero_at) / (full_at - zero_at)
    return weight * _clip(ratio, 0.0, 1.0)


def score_total_yield_attractiveness(
    total_yield_pct: float, config: ScoringWeightsConfig
) -> tuple[float, str]:
    weight = config.weights.total_yield_attractiveness
    params = config.total_yield_attractiveness
    score = _linear_score(
        total_yield_pct,
        weight,
        params.zero_score_total_yield_pct,
        params.full_score_total_yield_pct,
    )
    formula = (
        f"総合利回り{total_yield_pct:.2f}%を"
        f"{params.zero_score_total_yield_pct}%(0点)〜{params.full_score_total_yield_pct}%(満点)"
        f"で線形評価 × 配点{weight}点"
    )
    return score, formula


def score_dividend_sustainability(
    dividend: DividendInfo, financial: FinancialSummary, max_payout_ratio_pct: float, weight: float
) -> tuple[float, str]:
    factor = 0.0
    parts = []
    if dividend.is_progressive_or_doe_policy:
        factor += 0.4
        parts.append("累進配当/DOE方針(+0.4)")
    years = min(dividend.consecutive_dividend_increase_years or 0, 5)
    factor += (years / 5) * 0.4
    parts.append(f"連続増配{years}年評価(+{years / 5 * 0.4:.2f})")
    if financial.payout_ratio_pct is not None and max_payout_ratio_pct > 0:
        headroom = 1 - (financial.payout_ratio_pct / max_payout_ratio_pct)
        contribution = _clip(headroom, 0.0, 1.0) * 0.2
        factor += contribution
        parts.append(f"配当性向の余力評価(+{contribution:.2f})")
    factor = _clip(factor, 0.0, 1.0)
    score = weight * factor
    formula = f"配当持続性係数{factor:.2f}({', '.join(parts)}) × 配点{weight}点"
    return score, formula


def score_financial_health(
    financial: FinancialSummary, min_equity_ratio_pct: float, weight: float
) -> tuple[float, str]:
    if financial.equity_ratio_pct is None:
        return 0.0, "自己資本比率データなしのため0点"
    score = _linear_score(
        financial.equity_ratio_pct, weight, min_equity_ratio_pct, _FINANCIAL_HEALTH_UPPER_ANCHOR_PCT
    )
    formula = (
        f"自己資本比率{financial.equity_ratio_pct:.1f}%を"
        f"{min_equity_ratio_pct}%(0点)〜{_FINANCIAL_HEALTH_UPPER_ANCHOR_PCT}%(満点)"
        f"で線形評価 × 配点{weight}点"
    )
    return score, formula


def score_shareholder_benefit_value(
    benefit_yield_pct: float | None, weight: float
) -> tuple[float, str]:
    if benefit_yield_pct is None or benefit_yield_pct <= 0:
        return 0.0, "株主優待なし、または優待利回りデータなしのため0点"
    ratio = _clip(benefit_yield_pct / _BENEFIT_YIELD_FULL_SCORE_PCT, 0.0, 1.0)
    score = weight * ratio
    formula = (
        f"株主優待利回り{benefit_yield_pct:.2f}%を{_BENEFIT_YIELD_FULL_SCORE_PCT}%基準で評価 "
        f"× 配点{weight}点"
    )
    return score, formula


def score_earnings_stability(
    quarterly_operating_incomes: list[Decimal], weight: float
) -> tuple[float, str, float | None]:
    """3つ目の戻り値は「四半期営業利益が前期比非悪化だった割合」(Phase 2-B
    「銘柄分析」向け、判定時点の入力事実スナップショット用。判定ロジック自体は
    従来と同一、戻り値を1つ追加しているだけ)。データ不足でスコア自体が
    中立評価(0.5)の場合はNone(比率自体を計算していないため)。
    """
    if len(quarterly_operating_incomes) < 2:
        return weight * 0.5, "四半期業績データ不足のため中立(0.5)評価", None
    non_decreasing = sum(
        1
        for i in range(1, len(quarterly_operating_incomes))
        if quarterly_operating_incomes[i] >= quarterly_operating_incomes[i - 1]
    )
    ratio = non_decreasing / (len(quarterly_operating_incomes) - 1)
    score = weight * ratio
    formula = f"四半期営業利益が前期比非悪化だった割合{ratio:.2f} × 配点{weight}点"
    return score, formula, ratio


def score_price_stability(
    price_bars: list[PriceBar], weight: float
) -> tuple[float, str, float | None]:
    """3つ目の戻り値は年率換算ボラティリティ(%)(Phase 2-B「銘柄分析」向け、
    判定時点の入力事実スナップショット用。判定ロジック自体は従来と同一、
    戻り値を1つ追加しているだけ)。データ不足でスコア自体が中立評価(0.5)の
    場合はNone(ボラティリティ自体を計算していないため)。
    """
    if len(price_bars) < 2:
        return weight * 0.5, "株価履歴不足のため中立(0.5)評価", None
    closes = [float(bar.close) for bar in price_bars]
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(returns) < 2:
        return weight * 0.5, "リターン計算に十分なデータがないため中立(0.5)評価", None
    daily_vol = statistics.pstdev(returns)
    annualized_vol_pct = daily_vol * math.sqrt(_TRADING_DAYS_PER_YEAR) * 100
    ratio = 1 - _clip(
        (annualized_vol_pct - _PRICE_STABILITY_LOW_VOL_PCT)
        / (_PRICE_STABILITY_HIGH_VOL_PCT - _PRICE_STABILITY_LOW_VOL_PCT),
        0.0,
        1.0,
    )
    score = weight * ratio
    formula = (
        f"年率ボラティリティ{annualized_vol_pct:.1f}%を"
        f"{_PRICE_STABILITY_LOW_VOL_PCT}%(満点)〜{_PRICE_STABILITY_HIGH_VOL_PCT}%(0点)"
        f"で評価 × 配点{weight}点"
    )
    return score, formula, annualized_vol_pct


def compute_score(
    total_yield_pct: float,
    dividend: DividendInfo,
    financial: FinancialSummary,
    undervaluation_signals: UndervaluationSignals,
    benefit_yield_pct: float | None,
    quarterly_operating_incomes: list[Decimal],
    price_bars: list[PriceBar],
    min_equity_ratio_pct: float,
    max_payout_ratio_pct: float,
    config: ScoringWeightsConfig,
    undervaluation_category_caps: UndervaluationCategoryCaps,
) -> ScoreResult:
    w = config.weights
    yield_score, yield_formula = score_total_yield_attractiveness(total_yield_pct, config)
    sustainability_score, sustainability_formula = score_dividend_sustainability(
        dividend, financial, max_payout_ratio_pct, w.dividend_sustainability
    )
    health_score, health_formula = score_financial_health(
        financial, min_equity_ratio_pct, w.financial_health
    )
    undervaluation_score, undervaluation_formula = score_undervaluation_categories(
        undervaluation_signals, undervaluation_category_caps
    )
    benefit_score, benefit_formula = score_shareholder_benefit_value(
        benefit_yield_pct, w.shareholder_benefit_value
    )
    earnings_score, earnings_formula, operating_income_non_decrease_ratio = (
        score_earnings_stability(quarterly_operating_incomes, w.earnings_stability)
    )
    price_stability_score, price_stability_formula, annualized_volatility_pct = (
        score_price_stability(price_bars, w.price_stability)
    )

    total = (
        yield_score
        + sustainability_score
        + health_score
        + undervaluation_score
        + benefit_score
        + earnings_score
        + price_stability_score
    )

    breakdown = ScoreBreakdown(
        total_yield_attractiveness=round(yield_score, 2),
        dividend_sustainability=round(sustainability_score, 2),
        financial_health=round(health_score, 2),
        undervaluation=round(undervaluation_score, 2),
        shareholder_benefit_value=round(benefit_score, 2),
        earnings_stability=round(earnings_score, 2),
        price_stability=round(price_stability_score, 2),
        total=round(total, 2),
    )
    formulas = {
        "total_yield_attractiveness": yield_formula,
        "dividend_sustainability": sustainability_formula,
        "financial_health": health_formula,
        "undervaluation": undervaluation_formula,
        "shareholder_benefit_value": benefit_formula,
        "earnings_stability": earnings_formula,
        "price_stability": price_stability_formula,
    }
    # Phase 2-B「銘柄分析」向け(2026-08): 判定に使用したがbreakdown(点数)には
    # 残らない入力事実。総合利回り・割安度6シグナルはundervaluation_signals/
    # buy_signal_service.py側で別途PER/PBR実数値等と合わせて記録するため、
    # ここではcompute_score()自身が受け取っている入力値のみを対象とする。
    input_facts: dict[str, object] = {
        "total_yield_pct": total_yield_pct,
        "is_progressive_or_doe_policy": dividend.is_progressive_or_doe_policy,
        "consecutive_dividend_increase_years": dividend.consecutive_dividend_increase_years,
        "payout_ratio_pct": financial.payout_ratio_pct,
        "equity_ratio_pct": financial.equity_ratio_pct,
        "benefit_yield_pct": benefit_yield_pct,
        "undervaluation_signals": undervaluation_signals.available(),
        "quarterly_operating_incomes": [str(v) for v in quarterly_operating_incomes],
        "operating_income_non_decrease_ratio": operating_income_non_decrease_ratio,
        "annualized_volatility_pct": annualized_volatility_pct,
    }
    return ScoreResult(breakdown=breakdown, formulas=formulas, input_facts=input_facts)
