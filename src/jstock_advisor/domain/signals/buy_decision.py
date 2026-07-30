"""購入対象判定の中核ロジック(2026-07 BUYパイプライン再設計。要求仕様3節・4節・14節・16節)。

「企業として投資候補になり得るか(company_quality_score)」と「現在の株価で
実際に購入すべきか(purchase_attractiveness_score + BuyAction)」を分離する。

第1段階(投資対象スクリーニング)は`screen_investment_universe()`、
第3段階(現在価格での購入判断)は`decide_buy_action()`がそれぞれ担う。
第2段階(企業魅力度評価)は`domain/scoring/score.py::compute_score()`が担う
(このモジュールでは扱わない)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from jstock_advisor.config.models import BuyDecisionRulesConfig
from jstock_advisor.domain.entities.buy_decision import BuyDecisionReason
from jstock_advisor.domain.entities.common import BuyPriceLevels
from jstock_advisor.domain.entities.enums import BUY_FAMILY_ACTIONS, BuyAction, ConfidenceLevel
from jstock_advisor.domain.screening.rules import ScreeningResult
from jstock_advisor.domain.valuation.valuation_methods import DispersionBand
from jstock_advisor.interfaces.types import ShareholderBenefit

# 以下はMVPの初期値。バックテストを通じて見直す対象とする(既存buy_signal.pyの
# 方針を踏襲)。
_PRICE_POSITION_MAX = 50.0
_PRICE_POSITION_MIDPOINT_SCALE = 1.25  # entry価格から±20%でスコアが0/満点になる係数
_CONFIDENCE_MAX = 15.0
_DISPERSION_MAX = 10.0
_EARNINGS_MAX = 10.0
_RECENT_PRICE_MAX = 5.0
_INDUSTRY_MODEL_MAX = 5.0
_DATA_QUALITY_MAX = 5.0
_RECENT_DROP_THRESHOLD_PCT = -10.0


@dataclass(frozen=True)
class ScreeningOutcome:
    passed: bool
    exclusion_reasons: list[str]


def screen_investment_universe(
    screening_result: ScreeningResult,
    severe_earnings_decline: bool,
    benefit: ShareholderBenefit | None,
) -> ScreeningOutcome:
    """第1段階: 投資対象スクリーニング。EXCLUDEDにすべきかを判定する。

    データ不足(DATA_INSUFFICIENT)はスナップショット取得失敗時に呼び出し側
    (buy_signal_service.py)で別途判定するため、ここでは扱わない。
    """
    exclusion_reasons = list(screening_result.exclusion_reasons)
    if severe_earnings_decline:
        exclusion_reasons.append("直近決算で重大な業績悪化(営業利益が前期比30%超悪化)")
    if benefit is not None and benefit.is_abolished:
        exclusion_reasons.append("株主優待の廃止が発表されている")
    return ScreeningOutcome(passed=not exclusion_reasons, exclusion_reasons=exclusion_reasons)


@dataclass(frozen=True)
class BuyActionDecision:
    action: BuyAction
    raw_action: BuyAction  # 価格条件のみによる仮判定(スコア・決算調整の前)
    reasons: list[BuyDecisionReason] = field(default_factory=list)


def _price_tier_action(current_price: Decimal, buy_price_levels: BuyPriceLevels) -> BuyAction:
    if buy_price_levels.entry is None:
        return BuyAction.WATCH_FOR_PRICE
    if buy_price_levels.strong is not None and current_price <= buy_price_levels.strong.price:
        return BuyAction.STRONG_BUY
    if buy_price_levels.standard is not None and current_price <= buy_price_levels.standard.price:
        return BuyAction.BUY
    if current_price <= buy_price_levels.entry.price:
        return BuyAction.SMALL_ENTRY
    return BuyAction.WATCH_FOR_PRICE


def decide_buy_action(
    *,
    current_price: Decimal,
    buy_price_levels: BuyPriceLevels,
    company_quality_score: float,
    business_days_to_earnings: int | None,
    valuation_dispersion_ratio: float | None,
    config: BuyDecisionRulesConfig,
) -> BuyActionDecision:
    """第3段階: 現在価格での購入判断。

    価格条件は昇格・購入判定の必須条件とし、スコアは原則として格下げにのみ
    使用する(スコアだけでBUY系へ昇格させない)。現在値が打診買い価格を
    上回っている銘柄は、スコアがどれだけ高くてもBUY系判定にしない。
    """
    reasons: list[BuyDecisionReason] = []

    action = _price_tier_action(current_price, buy_price_levels)
    raw_action = action

    if buy_price_levels.entry is not None:
        reasons.append(
            BuyDecisionReason(
                code="PRICE_TIER",
                message="現在値と買付価格3段階を比較して仮判定した",
                actual_value=current_price,
                threshold_value=buy_price_levels.entry.price,
            )
        )
    else:
        reasons.append(
            BuyDecisionReason(
                code="NO_VALUATION_ANCHOR",
                message="購入判断基準価格を算出できないため、自動の買付価格を生成していない",
            )
        )

    thresholds = config.score_thresholds
    before_score_downgrade = action
    if action == BuyAction.STRONG_BUY and company_quality_score < thresholds.strong_buy:
        action = BuyAction.BUY
    if action == BuyAction.BUY and company_quality_score < thresholds.buy:
        action = BuyAction.SMALL_ENTRY
    if action == BuyAction.SMALL_ENTRY and company_quality_score < thresholds.small_entry:
        action = BuyAction.WATCH_FOR_PRICE
    if company_quality_score < thresholds.watch:
        action = BuyAction.NOT_ATTRACTIVE
    if action != before_score_downgrade:
        reasons.append(
            BuyDecisionReason(
                code="SCORE_BELOW_THRESHOLD",
                message="企業魅力度スコアが基準未満のため判定を格下げした",
                actual_value=company_quality_score,
                threshold_value=thresholds.watch,
            )
        )

    earnings_config = config.earnings_window
    if (
        business_days_to_earnings is not None
        and business_days_to_earnings <= earnings_config.block_buy_business_days
        and action in BUY_FAMILY_ACTIONS
    ):
        action = BuyAction.WATCH_BEFORE_EARNINGS
        reasons.append(
            BuyDecisionReason(
                code="EARNINGS_WINDOW",
                message="次回決算が近いため、新規購入を決算後まで待つ",
                actual_value=business_days_to_earnings,
                threshold_value=earnings_config.block_buy_business_days,
            )
        )

    dispersion_config = config.valuation_dispersion
    if (
        valuation_dispersion_ratio is not None
        and valuation_dispersion_ratio > dispersion_config.auto_buy_block
        and action in BUY_FAMILY_ACTIONS
    ):
        action = BuyAction.MANUAL_REVIEW
        reasons.append(
            BuyDecisionReason(
                code="VALUATION_DISPERSION_TOO_HIGH",
                message="適正価格算出手法間のばらつきが大きく、自動購入判定を禁止する",
                actual_value=valuation_dispersion_ratio,
                threshold_value=dispersion_config.auto_buy_block,
            )
        )

    return BuyActionDecision(action=action, raw_action=raw_action, reasons=reasons)


def _price_position_score(current_price: Decimal, buy_price_levels: BuyPriceLevels) -> float:
    if buy_price_levels.entry is None or buy_price_levels.entry.price <= 0:
        return 0.0
    entry_price = buy_price_levels.entry.price
    pct_below_entry = float((entry_price - current_price) / entry_price * 100)
    score = _PRICE_POSITION_MAX / 2 + pct_below_entry * _PRICE_POSITION_MIDPOINT_SCALE
    return max(0.0, min(_PRICE_POSITION_MAX, score))


def _confidence_score(valuation_confidence: ConfidenceLevel) -> float:
    if valuation_confidence == ConfidenceLevel.HIGH:
        return _CONFIDENCE_MAX
    if valuation_confidence == ConfidenceLevel.MEDIUM:
        return _CONFIDENCE_MAX * 0.5
    return 0.0


def _dispersion_score(dispersion_band: DispersionBand | None) -> float:
    if dispersion_band == "LOW":
        return _DISPERSION_MAX
    if dispersion_band == "MEDIUM":
        return _DISPERSION_MAX * 0.5
    return 0.0


def _earnings_score(business_days_to_earnings: int | None, config: BuyDecisionRulesConfig) -> float:
    if business_days_to_earnings is None:
        return _EARNINGS_MAX * 0.5
    if business_days_to_earnings <= config.earnings_window.block_buy_business_days:
        return 0.0
    if business_days_to_earnings <= config.earnings_window.add_margin_business_days:
        return _EARNINGS_MAX * 0.5
    return _EARNINGS_MAX


def _recent_price_score(recent_price_change_pct: float | None) -> float:
    if recent_price_change_pct is None:
        return 0.0
    if recent_price_change_pct <= _RECENT_DROP_THRESHOLD_PCT:
        return _RECENT_PRICE_MAX
    return _RECENT_PRICE_MAX * 0.5


def compute_purchase_attractiveness_score(
    *,
    current_price: Decimal,
    buy_price_levels: BuyPriceLevels,
    valuation_confidence: ConfidenceLevel,
    dispersion_band: DispersionBand | None,
    business_days_to_earnings: int | None,
    recent_price_change_pct: float | None,
    industry_model_applied: bool,
    data_quality_warning: bool,
    config: BuyDecisionRulesConfig,
) -> float:
    """第3段階: 現在価格での購入魅力度(要求仕様5節)。

    企業魅力度(company_quality_score)が高くても、現在値が高い場合は
    このスコアを低くする。このスコアだけでBUY系への昇格には使わない
    (`decide_buy_action`の価格条件が必須条件、スコアは格下げにのみ使用)。
    ランキング専用の目安であり、company_quality_scoreのような監査対象の
    厳密な配点根拠は持たない(MVPの初期値、バックテストで見直す対象)。
    """
    score = _price_position_score(current_price, buy_price_levels)
    score += _confidence_score(valuation_confidence)
    score += _dispersion_score(dispersion_band)
    score += _earnings_score(business_days_to_earnings, config)
    score += _recent_price_score(recent_price_change_pct)
    score += _INDUSTRY_MODEL_MAX if industry_model_applied else 0.0
    score += 0.0 if data_quality_warning else _DATA_QUALITY_MAX
    return round(score, 2)
