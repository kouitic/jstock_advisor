"""利確判定(要求仕様12節)。

含み益率・適正価格超過率・総合利回り低下を組み合わせて判定候補レベルを算出したうえで、
緩和要因(業績成長・増配継続・長期優待直前等)に応じて判定を弱める。上昇率だけで
機械的に売却判定を出さないよう、緩和要因は必ず考慮する。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import IntEnum

from jstock_advisor.config.models import MitigatingFactors, ProfitTakingRulesConfig
from jstock_advisor.domain.entities.common import PriceWithRationale, SellPriceLevels
from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.valuation.fair_value import compute_target_yield_price, round_yen


class _Level(IntEnum):
    HOLD = 0
    WATCH = 1
    PARTIAL = 2
    FULL = 3


_LEVEL_TO_RECOMMENDATION = {
    _Level.HOLD: RecommendationType.HOLD,
    _Level.WATCH: RecommendationType.WATCH,
    _Level.PARTIAL: RecommendationType.PARTIAL_PROFIT_TAKE,
    _Level.FULL: RecommendationType.FULL_PROFIT_TAKE,
}


@dataclass(frozen=True)
class UnrealizedPnl:
    unrealized_pnl: Decimal
    unrealized_pnl_pct: float
    total_return_including_income: Decimal
    total_return_pct: float


@dataclass(frozen=True)
class MitigatingFactorInputs:
    """各緩和要因の該当有無。判定不能・未評価の場合はFalse扱いとする(判定を弱めない)。"""

    fair_value_rising_with_earnings_growth: bool = False
    continuous_dividend_increase_years: int = 0
    is_progressive_or_doe_policy: bool = False
    long_term_holding_benefit_imminent: bool = False
    few_reinvestment_alternatives: bool = False
    is_nisa_account: bool = False


@dataclass(frozen=True)
class ProfitTakingResult:
    recommendation_type: RecommendationType
    triggered_reasons: list[str]
    mitigating_factors_applied: list[str]
    hold_reasons: list[str]
    sell_prices: SellPriceLevels
    pnl: UnrealizedPnl


def compute_unrealized_pnl(
    current_price: Decimal,
    average_purchase_price: Decimal,
    shares: int,
    total_purchase_amount: Decimal,
    cumulative_dividend_received: Decimal,
    cumulative_benefit_value_received: Decimal,
) -> UnrealizedPnl:
    unrealized_pnl = (current_price - average_purchase_price) * shares
    unrealized_pnl_pct = (
        float(current_price / average_purchase_price - 1) * 100
        if average_purchase_price > 0
        else 0.0
    )
    total_return = unrealized_pnl + cumulative_dividend_received + cumulative_benefit_value_received
    total_return_pct = (
        float(total_return / total_purchase_amount * 100) if total_purchase_amount > 0 else 0.0
    )
    return UnrealizedPnl(
        unrealized_pnl=unrealized_pnl,
        unrealized_pnl_pct=unrealized_pnl_pct,
        total_return_including_income=total_return,
        total_return_pct=total_return_pct,
    )


def _level_from_gain(gain_pct: float, config: ProfitTakingRulesConfig) -> _Level:
    t = config.thresholds
    if gain_pct >= t.unrealized_gain_full_pct:
        return _Level.FULL
    if gain_pct >= t.unrealized_gain_partial_pct:
        return _Level.PARTIAL
    if gain_pct >= t.unrealized_gain_watch_pct:
        return _Level.WATCH
    return _Level.HOLD


def _level_from_fair_value_excess(
    excess_pct: float | None, config: ProfitTakingRulesConfig
) -> _Level:
    if excess_pct is None:
        return _Level.HOLD
    t = config.thresholds
    if excess_pct >= t.fair_value_excess_full_pct:
        return _Level.FULL
    if excess_pct >= t.fair_value_excess_partial_pct:
        return _Level.PARTIAL
    return _Level.HOLD


def _level_from_total_yield(
    total_yield_pct: float | None, config: ProfitTakingRulesConfig
) -> _Level:
    if total_yield_pct is None:
        return _Level.HOLD
    t = config.thresholds
    if total_yield_pct < t.total_yield_strong_caution_pct:
        return _Level.FULL
    if total_yield_pct < t.total_yield_caution_pct:
        return _Level.PARTIAL
    return _Level.HOLD


def _apply_mitigating_factors(
    level: _Level, inputs: MitigatingFactorInputs, config: MitigatingFactors
) -> tuple[_Level, list[str]]:
    applied: list[str] = []
    total_downgrade = 0

    if (
        config.fair_value_rising_with_earnings_growth.enabled
        and inputs.fair_value_rising_with_earnings_growth
    ):
        total_downgrade += config.fair_value_rising_with_earnings_growth.downgrade_levels
        applied.append("業績成長により適正価格自体が上昇している")

    cdi = config.continuous_dividend_increase
    min_years = cdi.min_consecutive_years or 0
    if cdi.enabled and inputs.continuous_dividend_increase_years >= min_years and min_years > 0:
        total_downgrade += cdi.downgrade_levels
        applied.append(f"増配が{inputs.continuous_dividend_increase_years}年連続している")

    if config.progressive_dividend_or_doe_policy.enabled and inputs.is_progressive_or_doe_policy:
        total_downgrade += config.progressive_dividend_or_doe_policy.downgrade_levels
        applied.append("累進配当またはDOE方針がある")

    if (
        config.long_term_holding_benefit_imminent.enabled
        and inputs.long_term_holding_benefit_imminent
    ):
        total_downgrade += config.long_term_holding_benefit_imminent.downgrade_levels
        applied.append("長期保有優待の条件達成が近い")

    if config.few_reinvestment_alternatives.enabled and inputs.few_reinvestment_alternatives:
        total_downgrade += config.few_reinvestment_alternatives.downgrade_levels
        applied.append("売却後に同等以上の再投資候補が少ない")

    if config.nisa_long_term_benefit.enabled and inputs.is_nisa_account:
        total_downgrade += config.nisa_long_term_benefit.downgrade_levels
        applied.append("NISA口座で長期保有メリットが大きい")

    new_level = _Level(max(0, int(level) - total_downgrade))
    return new_level, applied


def _compute_sell_prices(
    average_purchase_price: Decimal,
    fair_value: Decimal | None,
    forecast_annual_dividend_per_share: Decimal | None,
    config: ProfitTakingRulesConfig,
) -> SellPriceLevels:
    t = config.thresholds

    gain_partial_price = round_yen(
        average_purchase_price * (1 + Decimal(str(t.unrealized_gain_partial_pct)) / 100)
    )
    gain_full_price = round_yen(
        average_purchase_price * (1 + Decimal(str(t.unrealized_gain_full_pct)) / 100)
    )

    fv_partial_price = (
        round_yen(fair_value * (1 + Decimal(str(t.fair_value_excess_partial_pct)) / 100))
        if fair_value is not None
        else None
    )
    fv_full_price = (
        round_yen(fair_value * (1 + Decimal(str(t.fair_value_excess_full_pct)) / 100))
        if fair_value is not None
        else None
    )

    partial_candidates = [p for p in (gain_partial_price, fv_partial_price) if p is not None]
    partial_start = min(partial_candidates) if partial_candidates else None

    recommended_candidates = [p for p in (gain_full_price, fv_partial_price) if p is not None]
    recommended = min(recommended_candidates) if recommended_candidates else None

    full_candidates = [p for p in (gain_full_price, fv_full_price) if p is not None]
    full_take = max(full_candidates) if full_candidates else None

    reassessment = compute_target_yield_price(
        forecast_annual_dividend_per_share, t.total_yield_strong_caution_pct
    )
    reassessment = round_yen(reassessment) if reassessment is not None else None

    def _wrap(price: Decimal | None, rationale: str) -> PriceWithRationale | None:
        return PriceWithRationale(price=price, rationale=rationale) if price is not None else None

    return SellPriceLevels(
        partial_take_start=_wrap(
            partial_start,
            f"含み益{t.unrealized_gain_partial_pct}%到達、または適正価格超過{t.fair_value_excess_partial_pct}%到達の低い方",
        ),
        profit_take_recommended=_wrap(
            recommended,
            f"含み益{t.unrealized_gain_full_pct}%、または適正価格超過{t.fair_value_excess_partial_pct}%到達の低い方",
        ),
        full_take_consider=_wrap(
            full_take,
            f"含み益{t.unrealized_gain_full_pct}%、または適正価格超過{t.fair_value_excess_full_pct}%到達の高い方",
        ),
        reassessment_price=_wrap(
            reassessment, f"総合利回りが{t.total_yield_strong_caution_pct}%まで低下する水準"
        ),
    )


def evaluate_profit_taking(
    current_price: Decimal,
    average_purchase_price: Decimal,
    shares: int,
    total_purchase_amount: Decimal,
    cumulative_dividend_received: Decimal,
    cumulative_benefit_value_received: Decimal,
    fair_value: Decimal | None,
    current_total_yield_pct: float | None,
    forecast_annual_dividend_per_share: Decimal | None,
    mitigating_inputs: MitigatingFactorInputs,
    config: ProfitTakingRulesConfig,
) -> ProfitTakingResult:
    pnl = compute_unrealized_pnl(
        current_price,
        average_purchase_price,
        shares,
        total_purchase_amount,
        cumulative_dividend_received,
        cumulative_benefit_value_received,
    )

    fair_value_excess_pct = (
        float(current_price / fair_value - 1) * 100
        if fair_value is not None and fair_value > 0
        else None
    )

    triggered_reasons: list[str] = []
    level_gain = _level_from_gain(pnl.unrealized_pnl_pct, config)
    if level_gain > _Level.HOLD:
        triggered_reasons.append(f"含み益率{pnl.unrealized_pnl_pct:.1f}%")

    level_fv = _level_from_fair_value_excess(fair_value_excess_pct, config)
    if level_fv > _Level.HOLD and fair_value_excess_pct is not None:
        triggered_reasons.append(f"最終適正価格を{fair_value_excess_pct:.1f}%超過")

    level_yield = _level_from_total_yield(current_total_yield_pct, config)
    if level_yield > _Level.HOLD and current_total_yield_pct is not None:
        triggered_reasons.append(f"現在の総合利回りが{current_total_yield_pct:.2f}%まで低下")

    raw_level = max(level_gain, level_fv, level_yield)

    if raw_level == _Level.HOLD:
        return ProfitTakingResult(
            recommendation_type=RecommendationType.HOLD,
            triggered_reasons=triggered_reasons,
            mitigating_factors_applied=[],
            hold_reasons=["利確シグナルに該当する条件がない"],
            sell_prices=_compute_sell_prices(
                average_purchase_price, fair_value, forecast_annual_dividend_per_share, config
            ),
            pnl=pnl,
        )

    final_level, applied_factors = _apply_mitigating_factors(
        raw_level, mitigating_inputs, config.mitigating_factors
    )
    # 何らかの利確シグナルが実際に発生している場合、緩和要因によってもHOLD(無評価)まで
    # 完全に打ち消すのではなく、最低でもWATCH(監視継続)として可視化する。
    if raw_level > _Level.HOLD and final_level == _Level.HOLD:
        final_level = _Level.WATCH

    hold_reasons = list(applied_factors)
    if final_level == _Level.HOLD and not hold_reasons:
        hold_reasons = ["利確シグナルに該当する条件がない"]

    return ProfitTakingResult(
        recommendation_type=_LEVEL_TO_RECOMMENDATION[final_level],
        triggered_reasons=triggered_reasons,
        mitigating_factors_applied=applied_factors,
        hold_reasons=hold_reasons,
        sell_prices=_compute_sell_prices(
            average_purchase_price, fair_value, forecast_annual_dividend_per_share, config
        ),
        pnl=pnl,
    )
