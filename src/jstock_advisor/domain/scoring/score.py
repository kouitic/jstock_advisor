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
from jstock_advisor.domain.entities.enums import EvidenceCoverageStatus
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
    # Issue #22 Phase 3.5(2026-08-28、観測性強化): 7componentそれぞれの
    # 判定時点の評価状況スナップショット。key=component名、value=
    # {"state": EvidenceCoverageStatusの値, "reason_codes": [...],
    #  "excluded_from_denominator": bool}。
    # 観測専用であり、v1のスコア算出(breakdown/total)へは一切適用しない。
    # v1は全componentを常に分母へ含めるため excluded_from_denominator は
    # 常にFalse(v1の実際の挙動をそのまま記録する)。
    # stateはEVALUATED/NOT_EVALUATEDのみを使う。NOT_APPLICABLEは「判定時点の
    # 事実だけから明確に評価対象外と断定できる」場合にのみ許され、v1の
    # スコアリングにはそのような判定基準が存在しないため生成しない
    # (例: 優待利回りNoneは「優待制度なし」と「レジストリ未登録」を判別
    # できないため、非断定のNOT_EVALUATEDとする)。
    component_states: dict[str, object] = field(default_factory=dict)


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
        # Issue #30 Phase 1: 3状態(True/False/None=UNKNOWN)をそのまま記録し、
        # あわせて監査用のstatus/type/source/checked_atをスナップショットする
        # (evidence_text全文はレジストリ側が正本のためコピーしない)。
        "is_progressive_or_doe_policy": dividend.is_progressive_or_doe_policy,
        "shareholder_return_policy_status": (
            "UNKNOWN" if dividend.is_progressive_or_doe_policy is None else "CONFIRMED"
        ),
        "shareholder_return_policy_type": dividend.shareholder_return_policy_type,
        "shareholder_return_policy_source": dividend.shareholder_return_policy_source_reference,
        "shareholder_return_policy_checked_at": (
            dividend.shareholder_return_policy_checked_at.isoformat()
            if dividend.shareholder_return_policy_checked_at is not None
            else None
        ),
        "consecutive_dividend_increase_years": dividend.consecutive_dividend_increase_years,
        "payout_ratio_pct": financial.payout_ratio_pct,
        "equity_ratio_pct": financial.equity_ratio_pct,
        "benefit_yield_pct": benefit_yield_pct,
        "undervaluation_signals": undervaluation_signals.available(),
        "quarterly_operating_incomes": [str(v) for v in quarterly_operating_incomes],
        "operating_income_non_decrease_ratio": operating_income_non_decrease_ratio,
        "annualized_volatility_pct": annualized_volatility_pct,
    }
    component_states = _build_component_states(
        dividend=dividend,
        financial=financial,
        undervaluation_signals=undervaluation_signals,
        benefit_yield_pct=benefit_yield_pct,
        operating_income_non_decrease_ratio=operating_income_non_decrease_ratio,
        annualized_volatility_pct=annualized_volatility_pct,
    )
    return ScoreResult(
        breakdown=breakdown,
        formulas=formulas,
        input_facts=input_facts,
        component_states=component_states,
    )


def _component_state_entry(
    state: EvidenceCoverageStatus, reason_codes: list[str]
) -> dict[str, object]:
    # excluded_from_denominatorはv1の実際の挙動の記録(v1は全componentを常に
    # 分母へ含めるため常にFalse)。将来のv2でN/A項目を分母から除外する設計に
    # なった場合に意味を持つフィールドを、観測用として先行して持たせている。
    return {
        "state": state.value,
        "reason_codes": reason_codes,
        "excluded_from_denominator": False,
    }


def _build_component_states(
    dividend: DividendInfo,
    financial: FinancialSummary,
    undervaluation_signals: UndervaluationSignals,
    benefit_yield_pct: float | None,
    operating_income_non_decrease_ratio: float | None,
    annualized_volatility_pct: float | None,
) -> dict[str, object]:
    """Issue #22 Phase 3.5: 7componentの判定時点評価状況(観測専用)。

    保存時に意味を推測しない、が大原則(ScoreResult.component_statesの
    docstring参照)。各stateは「v1がこのcomponentを実データから評価したか
    (EVALUATED)、データ不足でフォールバック値(0点または中立0.5)を使ったか
    (NOT_EVALUATED)」という判定時点の事実のみを記録する。
    """
    evaluated = EvidenceCoverageStatus.EVALUATED
    not_evaluated = EvidenceCoverageStatus.NOT_EVALUATED

    # total_yield: compute_total_yield_pct()が上流でNone→0.0へ潰すため、
    # ここでは常に値が評価される(無配とデータ欠測はこの層では判別不能。
    # 配当利回り自体の有無はRecommendation.dividend_yield_pct_at_recommendation
    # で別途観測可能)。
    total_yield_state = _component_state_entry(evaluated, [])

    # 配当持続性はv1では常に係数式で評価される(欠測要素は加点0として扱われる)
    # ため state=EVALUATED とし、どの入力が欠測だったかをreason_codesへ残す。
    # Issue #30 Phase 1: is_progressive_or_doe_policyは3状態化済み。方針factorだけが
    # UNKNOWN/NONE確認済みでも、連続増配・配当性向のfactorは評価可能なため
    # component全体はEVALUATEDのまま、subfactor理由としてreason_codesへ記録する
    # (POLICY_STATUS_UNKNOWN=レジストリ未登録・取得不能 /
    #  POLICY_NONE_CONFIRMED=人間確認済みで方針なし。いずれもscoreは方針分0点で、
    #  UNKNOWNへの中立加点・再正規化は行わない)。
    sustainability_reasons: list[str] = []
    if dividend.is_progressive_or_doe_policy is None:
        sustainability_reasons.append("POLICY_STATUS_UNKNOWN")
    elif dividend.is_progressive_or_doe_policy is False:
        sustainability_reasons.append("POLICY_NONE_CONFIRMED")
    if financial.payout_ratio_pct is None:
        sustainability_reasons.append("PAYOUT_RATIO_UNAVAILABLE")
    if dividend.consecutive_dividend_increase_years is None:
        sustainability_reasons.append("CONSECUTIVE_DIVIDEND_INCREASE_YEARS_UNAVAILABLE")
    sustainability_state = _component_state_entry(evaluated, sustainability_reasons)

    if financial.equity_ratio_pct is None:
        health_state = _component_state_entry(not_evaluated, ["EQUITY_RATIO_UNAVAILABLE"])
    else:
        health_state = _component_state_entry(evaluated, [])

    available_signals = undervaluation_signals.available()
    if not available_signals:
        undervaluation_state = _component_state_entry(
            not_evaluated, ["NO_UNDERVALUATION_SIGNALS_AVAILABLE"]
        )
    elif len(available_signals) < 6:
        undervaluation_state = _component_state_entry(evaluated, ["PARTIAL_SIGNAL_COVERAGE"])
    else:
        undervaluation_state = _component_state_entry(evaluated, [])

    # 優待利回りNoneは「優待制度が存在しない」と「制度はあるがレジストリ
    # 未登録」を判定時点のデータから判別できないため、断定的な
    # NO_BENEFIT_PROGRAM等ではなく非断定のUNAVAILABLEとする(Issue #27参照)。
    if benefit_yield_pct is None:
        benefit_state = _component_state_entry(not_evaluated, ["BENEFIT_YIELD_UNAVAILABLE"])
    else:
        benefit_state = _component_state_entry(evaluated, [])

    # ratio/volがNone ⟺ v1がデータ不足の中立(0.5)フォールバックを適用した
    # (score_earnings_stability/score_price_stabilityの戻り値仕様)。
    if operating_income_non_decrease_ratio is None:
        earnings_state = _component_state_entry(
            not_evaluated, ["INSUFFICIENT_QUARTERLY_PERIODS", "NEUTRAL_FALLBACK_APPLIED"]
        )
    else:
        earnings_state = _component_state_entry(evaluated, [])
    if annualized_volatility_pct is None:
        price_stability_state = _component_state_entry(
            not_evaluated, ["INSUFFICIENT_PRICE_HISTORY", "NEUTRAL_FALLBACK_APPLIED"]
        )
    else:
        price_stability_state = _component_state_entry(evaluated, [])

    return {
        "total_yield_attractiveness": total_yield_state,
        "dividend_sustainability": sustainability_state,
        "financial_health": health_state,
        "undervaluation": undervaluation_state,
        "shareholder_benefit_value": benefit_state,
        "earnings_stability": earnings_state,
        "price_stability": price_stability_state,
    }
