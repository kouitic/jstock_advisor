"""利確判定(要求仕様12節)。

含み益率・適正価格超過率・総合利回り低下を組み合わせて判定候補レベルを算出したうえで、
緩和要因(業績成長・増配継続・長期優待直前等)に応じて判定を弱める。上昇率だけで
機械的に売却判定を出さないよう、緩和要因は必ず考慮する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import IntEnum

from jstock_advisor.config.models import MitigatingFactors, ProfitTakingRulesConfig
from jstock_advisor.domain.entities.common import PriceWithRationale, SellPriceLevels
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    DividendComparisonOutcome,
    PriceFieldBasis,
    RecommendationType,
    StockType,
    TimingAction,
    TrendClassification,
)
from jstock_advisor.domain.entities.momentum import MomentumSnapshot
from jstock_advisor.domain.entities.valuation import FairValueRange
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
class ProfitTakingConditionInputs:
    """PARTIAL/FULL判定に使う複数条件の該当有無・関連値(要求仕様6節・8節・9節)。

    判定不能・未評価の項目はFalse/None扱いとし、捏造した根拠で強い判定を
    出さない(推測で補完しない原則)。
    """

    stock_types: list[StockType] = field(default_factory=list)
    fair_value_range: FairValueRange | None = None
    momentum: MomentumSnapshot | None = None
    dividend_comparison_outcome: DividendComparisonOutcome | None = None
    cashflow_fundamentally_driven: bool | None = None
    guidance_revision_disclosed: bool = False
    severe_earnings_decline: bool = False
    investment_premise_broken: bool = False
    accounting_or_scandal_or_delisting_risk: bool = False
    portfolio_concentration_over_limit: bool = False
    earnings_event_risk_reduction_rationale: bool = False
    profit_target_price: Decimal | None = None
    profit_target_rate: float | None = None


@dataclass(frozen=True)
class ProfitTakingResult:
    recommendation_type: RecommendationType  # final_actionと同値(後方互換のため維持)
    fundamental_action: RecommendationType
    timing_action: TimingAction
    final_action: RecommendationType
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


def _count_partial_conditions(
    pnl: UnrealizedPnl,
    fair_value_excess_pct: float | None,
    current_total_yield_pct: float | None,
    inputs: ProfitTakingConditionInputs,
    config: ProfitTakingRulesConfig,
) -> tuple[int, list[str]]:
    """一部利確(PARTIAL)の根拠となる独立条件を数える(要求仕様9節)。

    含み益率単独ではPARTIALへ到達できない設計(min_conditions_for_partial以上が必要)。
    """
    t = config.thresholds
    is_growth = StockType.GROWTH in inputs.stock_types
    reasons: list[str] = []

    if pnl.unrealized_pnl_pct >= t.unrealized_gain_partial_pct:
        reasons.append(f"含み益率{pnl.unrealized_pnl_pct:.1f}%が一部利確閾値に到達")

    # FairValueRangeが渡されていない場合(呼び出し側が単純なスカラーfair_valueのみを
    # 使っている場合)は従来通りfair_value_excess_pctをそのまま使う。FairValueRangeが
    # 渡されている場合のみ、使用不可(usable_for_trading_judgment=False)なら無視する
    # (要求仕様7節: 適正価格を売買判定に使用不可の場合は使用しない)。
    fv_range = inputs.fair_value_range
    fv_condition_usable = fv_range is None or fv_range.usable_for_trading_judgment
    if (
        fv_condition_usable
        and fair_value_excess_pct is not None
        and fair_value_excess_pct >= t.fair_value_excess_partial_pct
    ):
        reasons.append(f"適正価格レンジ上限を{fair_value_excess_pct:.1f}%超過")

    # 成長株は業績予想の下方修正・急激な業績悪化があった場合のみ「成長鈍化」を条件化する
    # (要求仕様7節: GROWTHは配当利回り低下だけを利確理由にしない)。
    if is_growth and (inputs.guidance_revision_disclosed or inputs.severe_earnings_decline):
        reasons.append("成長鈍化または業績予想の下方修正の可能性")

    if inputs.momentum is not None and inputs.momentum.trend_classification in (
        TrendClassification.DOWNTREND,
        TrendClassification.STRONG_DOWNTREND,
    ):
        reasons.append("株価トレンドが悪化")

    # GROWTHは配当・優待利回り低下を利確条件に含めない(要求仕様7節)。
    if (
        not is_growth
        and current_total_yield_pct is not None
        and current_total_yield_pct < t.total_yield_caution_pct
    ):
        reasons.append(f"総合利回りが{current_total_yield_pct:.2f}%まで低下")

    if inputs.portfolio_concentration_over_limit:
        reasons.append("ポートフォリオ内の保有比率が上限を超過")

    if inputs.earnings_event_risk_reduction_rationale:
        reasons.append("決算イベントに備えたリスク低減の合理性")

    return len(reasons), reasons


def _full_strong_conditions(
    current_price: Decimal,
    pnl: UnrealizedPnl,
    inputs: ProfitTakingConditionInputs,
    config: ProfitTakingRulesConfig,
) -> list[str]:
    """全株利確(FULL)を単独で正当化できる強い条件(要求仕様9節)。

    「含み益率が高い」というだけの条件はここに含めない(gain単独でFULLに
    到達させないという要求仕様9節の明示的な制約)。
    """
    reasons: list[str] = []
    is_income = StockType.INCOME in inputs.stock_types

    if inputs.investment_premise_broken:
        reasons.append("投資前提が明確に崩れた")

    if inputs.accounting_or_scandal_or_delisting_risk:
        reasons.append("会計・不祥事・上場維持リスクが発生")

    if (
        is_income
        and inputs.dividend_comparison_outcome == DividendComparisonOutcome.ACTUAL_DIVIDEND_CUT
        and inputs.cashflow_fundamentally_driven is True
    ):
        reasons.append("配当投資銘柄で確定的な減配とフリーキャッシュフロー悪化が重なった")

    if (
        inputs.fair_value_range is not None
        and inputs.fair_value_range.usable_for_trading_judgment
        and inputs.fair_value_range.neutral is not None
        and inputs.fair_value_range.neutral > 0
        and current_price > 0
    ):
        forward_return_pct = float(inputs.fair_value_range.neutral / current_price - 1) * 100
        cbj = config.condition_based_judgment
        if forward_return_pct <= cbj.forward_return_inferior_threshold_pct:
            reasons.append(
                f"適正価格基準の期待リターンが{forward_return_pct:.1f}%と、"
                "保有継続の合理性が低い"
            )

    if inputs.profit_target_price is not None and current_price >= inputs.profit_target_price:
        reasons.append(f"ユーザー設定の全利確目標価格({inputs.profit_target_price}円)に到達")
    elif (
        inputs.profit_target_rate is not None
        and pnl.unrealized_pnl_pct >= inputs.profit_target_rate
    ):
        reasons.append(f"ユーザー設定の全利確目標利回り({inputs.profit_target_rate}%)に到達")

    return reasons


def _count_full_moderate_conditions(
    pnl: UnrealizedPnl,
    fair_value_excess_pct: float | None,
    current_total_yield_pct: float | None,
    inputs: ProfitTakingConditionInputs,
    config: ProfitTakingRulesConfig,
) -> tuple[int, list[str]]:
    """全株利確(FULL)を、複数該当した場合にのみ正当化する中程度の条件。"""
    t = config.thresholds
    is_growth = StockType.GROWTH in inputs.stock_types
    reasons: list[str] = []

    if pnl.unrealized_pnl_pct >= t.unrealized_gain_full_pct:
        reasons.append(f"含み益率{pnl.unrealized_pnl_pct:.1f}%が全株利確閾値に到達")

    fv_range = inputs.fair_value_range
    fv_condition_usable = fv_range is None or fv_range.usable_for_trading_judgment
    if (
        fv_condition_usable
        and fair_value_excess_pct is not None
        and fair_value_excess_pct >= t.fair_value_excess_full_pct
    ):
        reasons.append(f"適正価格レンジ上限を{fair_value_excess_pct:.1f}%超過(全株利確水準)")

    if (
        not is_growth
        and current_total_yield_pct is not None
        and current_total_yield_pct < t.total_yield_strong_caution_pct
    ):
        reasons.append(f"総合利回りが{current_total_yield_pct:.2f}%まで大幅低下")

    if inputs.momentum is not None and inputs.momentum.trend_classification == (
        TrendClassification.STRONG_DOWNTREND
    ):
        reasons.append("株価トレンドが強く悪化")

    if is_growth and inputs.guidance_revision_disclosed and inputs.severe_earnings_decline:
        reasons.append("業績予想の下方修正と深刻な業績悪化が重なった")

    return len(reasons), reasons


def _wrap(
    price: Decimal | None,
    rationale: str,
    basis: PriceFieldBasis = PriceFieldBasis.TARGET_PRICE,
) -> PriceWithRationale | None:
    """算出不能(price is None)の場合はNoneのまま返す(現在値へのフォールバックは行わない、
    要求仕様11節)。"""
    return PriceWithRationale(price=price, rationale=rationale, basis=basis) if price else None


def _compute_sell_prices(
    current_price: Decimal,
    average_purchase_price: Decimal,
    fair_value: Decimal | None,
    forecast_annual_dividend_per_share: Decimal | None,
    level_gain: _Level,
    level_fv: _Level,
    config: ProfitTakingRulesConfig,
) -> SellPriceLevels:
    """4価格フィールドを算出する。

    旧実装は「利確推奨価格」「全株利確検討価格」を同じ2候補(含み益基準・
    適正価格基準)からmin/maxで導出したうえで、両方を無条件に現在値へ
    切り上げていた。これにより、判定が実際にはどの軸(含み益/適正価格/
    総合利回り)で発火したかと無関係な価格が表示され、「FULL判定なのに
    全株利確検討価格が現在値より高い」という矛盾を生んでいた(2914の事例)。

    修正方針: recommended_limit_price(実際に指値候補として提示する価格)は、
    現在の判定水準(level_gain/level_fv)に実際に寄与した軸からのみ導出する。
    総合利回り低下(level_yield)のみで判定が発火した場合、含み益・適正価格
    いずれの軸からも「到達済みの具体的な指値」は導出できないため、
    無理に現在値を割り当てず算出不能(None)とする。
    """
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

    # 一部利確開始価格: 含み益・適正価格いずれか早く到達する方(=より緩い基準)。
    # 既に到達済み(現在値以下)であれば「即時執行目安」として現在値との
    # 前後関係を明示する(現在値への無条件フォールバックではなく、実際に
    # 計算された閾値が現在値を下回っている、という事実に基づく判断)。
    partial_candidates = [p for p in (gain_partial_price, fv_partial_price) if p is not None]
    partial_start = min(partial_candidates) if partial_candidates else None

    # 実際に「利確検討」水準へ到達させた軸だけを、指値候補の根拠に使う。
    # level_gain/level_fvのうちPARTIAL以上に達している軸の価格のみを候補とし、
    # どちらも達していない(=総合利回り低下のみで発火した)場合はNoneとする。
    recommended_candidates: list[Decimal] = []
    if level_gain >= _Level.PARTIAL:
        recommended_candidates.append(gain_full_price)
    if level_fv >= _Level.PARTIAL and fv_full_price is not None:
        recommended_candidates.append(fv_full_price)
    recommended = min(recommended_candidates) if recommended_candidates else None
    recommended_basis = PriceFieldBasis.TARGET_PRICE
    if recommended is not None and recommended <= current_price:
        recommended_basis = PriceFieldBasis.IMMEDIATE_EXECUTION_REFERENCE

    # 全株利確検討価格: 含み益・適正価格の両基準のうち、より厳しい方(高い方)。
    # 「利確推奨価格」よりも先の、更に強い確信を持てる水準を示す参考値であり、
    # 判定が既にFULLへ達している場合でも、この値自体は将来の追加確認水準
    # (=まだ現在値を超えていて構わない)であることをrationaleで明示する。
    full_candidates = [p for p in (gain_full_price, fv_full_price) if p is not None]
    full_take = max(full_candidates) if full_candidates else None

    reevaluation_upside = compute_target_yield_price(
        forecast_annual_dividend_per_share, t.total_yield_strong_caution_pct
    )
    reevaluation_upside = (
        round_yen(reevaluation_upside) if reevaluation_upside is not None else None
    )
    # 「上昇時の再評価価格」は定義上、現在値より高い水準でなければ意味をなさない
    # (既に現在値がこの水準を下回って計算される=とうに通過済み、という場合は
    # 「上昇時」の名にそぐわないため算出不能扱いとする。現在値へのフォールバックは行わない)。
    if reevaluation_upside is not None and reevaluation_upside <= current_price:
        reevaluation_upside = None

    return SellPriceLevels(
        partial_profit_start_price=_wrap(
            partial_start,
            f"含み益{t.unrealized_gain_partial_pct}%到達、または適正価格超過"
            f"{t.fair_value_excess_partial_pct}%到達の早い方",
            basis=(
                PriceFieldBasis.IMMEDIATE_EXECUTION_REFERENCE
                if partial_start is not None and partial_start <= current_price
                else PriceFieldBasis.TARGET_PRICE
            ),
        ),
        recommended_limit_price=_wrap(
            recommended,
            "利確検討水準に実際に到達した軸(含み益・適正価格超過)から算出した指値候補。"
            "総合利回り低下のみが根拠の場合は具体的な指値を算出しない",
            basis=recommended_basis,
        ),
        full_profit_consideration_price=_wrap(
            full_take,
            f"含み益{t.unrealized_gain_full_pct}%かつ適正価格超過{t.fair_value_excess_full_pct}%の"
            "両方を満たす、より強い確信が持てる参考水準(現在値を上回っていても矛盾ではない)",
        ),
        reevaluation_price_upside=_wrap(
            reevaluation_upside,
            f"総合利回りが{t.total_yield_strong_caution_pct}%まで低下する水準(上昇時の再評価目安)",
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
    condition_inputs: ProfitTakingConditionInputs | None = None,
) -> ProfitTakingResult:
    """利確判定(要求仕様6節・7節・8節・9節・10節)。

    含み益率・適正価格超過率単独ではPARTIAL/FULLへ到達できない設計とする
    (複数の独立条件が該当した場合のみ、または強い条件が1つ該当した場合のみ
    到達する)。ファンダメンタル評価(fundamental_action)とタイミング評価
    (timing_action)を分離し、上昇トレンドはfundamental_actionを最大1段階
    までしか緩和できない(final_action)。
    """
    condition_inputs = condition_inputs or ProfitTakingConditionInputs()
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

    # 「利確」は含み益があって初めて成立する概念のため、含み損の状態では
    # 適正価格超過・総合利回り低下・その他の条件による判定は考慮しない
    # (株価下落そのものによる売却判断はsell_signal側の投資前提悪化判定の担当)。
    has_unrealized_gain = pnl.unrealized_pnl_pct > 0

    # level_gain/level_fvは価格フィールド算出(_compute_sell_prices)専用の補助値であり、
    # 判定レベル自体(raw_level)は下記の複数条件方式で別途決定する。含み損の場合は
    # 適正価格超過だけで指値候補が出てしまわないよう、level_fvもHOLDに固定する。
    level_gain = _level_from_gain(pnl.unrealized_pnl_pct, config)
    level_fv = (
        _level_from_fair_value_excess(fair_value_excess_pct, config)
        if has_unrealized_gain
        else _Level.HOLD
    )

    triggered_reasons: list[str] = []
    if has_unrealized_gain:
        partial_count, partial_reasons = _count_partial_conditions(
            pnl, fair_value_excess_pct, current_total_yield_pct, condition_inputs, config
        )
        full_strong_reasons = _full_strong_conditions(
            current_price, pnl, condition_inputs, config
        )
        full_moderate_count, full_moderate_reasons = _count_full_moderate_conditions(
            pnl, fair_value_excess_pct, current_total_yield_pct, condition_inputs, config
        )
    else:
        partial_count, partial_reasons = 0, []
        full_strong_reasons = []
        full_moderate_count, full_moderate_reasons = 0, []

    cbj = config.condition_based_judgment
    if full_strong_reasons or full_moderate_count >= cbj.min_moderate_conditions_for_full:
        raw_level = _Level.FULL
        triggered_reasons.extend(full_strong_reasons or full_moderate_reasons)
    elif partial_count >= cbj.min_conditions_for_partial:
        raw_level = _Level.PARTIAL
        triggered_reasons.extend(partial_reasons)
    elif (
        partial_count >= 1
        or pnl.unrealized_pnl_pct >= config.thresholds.unrealized_gain_watch_pct
    ):
        raw_level = _Level.WATCH
        triggered_reasons.extend(partial_reasons)
        if not partial_reasons:
            triggered_reasons.append(f"含み益率{pnl.unrealized_pnl_pct:.1f}%が監視水準に到達")
    else:
        raw_level = _Level.HOLD

    if raw_level == _Level.HOLD:
        fundamental_level = _Level.HOLD
        applied_factors: list[str] = []
        hold_reasons = ["利確シグナルに該当する条件がない"]
    else:
        fundamental_level, applied_factors = _apply_mitigating_factors(
            raw_level, mitigating_inputs, config.mitigating_factors
        )
        # 何らかの利確シグナルが実際に発生している場合、緩和要因によってもHOLD(無評価)まで
        # 完全に打ち消すのではなく、最低でもWATCH(監視継続)として可視化する。
        if fundamental_level == _Level.HOLD:
            fundamental_level = _Level.WATCH
        hold_reasons = list(applied_factors)

    # タイミング層(要求仕様9節・10節): ファンダメンタル評価とは独立した軸として算出する。
    # 上昇トレンドはfundamental_actionを最大1段階までしか緩和できず、適正価格レンジ上限を
    # 明確に超過している(usable かつ 信頼度がLOWでない)場合は緩和自体を禁止する
    # (上昇トレンドだけを理由に割高評価そのものを無効化しない)。
    timing_action = TimingAction.NEUTRAL
    final_level = fundamental_level
    momentum = condition_inputs.momentum
    if momentum is not None:
        trend = momentum.trend_classification
        if trend in (TrendClassification.STRONG_UPTREND, TrendClassification.UPTREND):
            timing_action = TimingAction.WAIT_UPTREND_CONTINUES
            margin = config.condition_based_judgment.timing_downgrade_block_margin_pct
            fv_range = condition_inputs.fair_value_range
            hard_overvalued = (
                fv_range is not None
                and fv_range.usable_for_trading_judgment
                and fv_range.overall_confidence != ConfidenceLevel.LOW
                and fv_range.bull is not None
                and current_price > fv_range.bull * (1 + Decimal(str(margin)) / 100)
            )
            if fundamental_level > _Level.HOLD and not hard_overvalued:
                final_level = _Level(max(0, int(fundamental_level) - 1))
        elif trend in (TrendClassification.STRONG_DOWNTREND, TrendClassification.DOWNTREND):
            timing_action = TimingAction.ACCELERATE_DOWNTREND_CONFIRMED
        else:
            timing_action = TimingAction.PROCEED_NO_TIMING_SIGNAL

    fundamental_action = _LEVEL_TO_RECOMMENDATION[fundamental_level]
    final_action = _LEVEL_TO_RECOMMENDATION[final_level]

    sell_prices = _compute_sell_prices(
        current_price,
        average_purchase_price,
        fair_value,
        forecast_annual_dividend_per_share,
        level_gain,
        level_fv,
        config,
    )
    if momentum is not None and momentum.trailing_stop_reference_price is not None:
        sell_prices = sell_prices.model_copy(
            update={
                "trailing_stop_reference_price": _wrap(
                    momentum.trailing_stop_reference_price,
                    "直近高値からのトレーリングストップ参考水準(モメンタム層算出)",
                )
            }
        )

    return ProfitTakingResult(
        recommendation_type=final_action,
        fundamental_action=fundamental_action,
        timing_action=timing_action,
        final_action=final_action,
        triggered_reasons=triggered_reasons,
        mitigating_factors_applied=applied_factors,
        hold_reasons=hold_reasons,
        sell_prices=sell_prices,
        pnl=pnl,
    )
