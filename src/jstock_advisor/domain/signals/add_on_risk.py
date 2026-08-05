"""保有銘柄の買い増し固有リスクゲート(2026-07 統合BUY候補パイプライン)。

共通購入判断(BuySignalService)がBUY系判定を出しても、保有銘柄については
「保有しているから買う」を許さず、追加で以下を確認する。

- 売却・利確判定(SellSignalService/ProfitTakingService)との競合
- 保有データ(株数・平均取得単価)の整合性
- ポートフォリオ集中度計算に使うデータそのものの信頼性
  (全保有銘柄の時価が判明している場合のみ計算する。一部でも欠落・矛盾が
  あれば「比率が計算できない」ため、上限超過とは別カテゴリで通知を禁止する)
- 銘柄集中度・業種集中度(買い増し後の構成比。最低売買単位1単元を仮定)

判定は優先順位付きの単一パスで行い、最初に該当したブロック理由だけを返す
(複数該当していても後続はチェックしない。監査上の理由を単純明快にするため)。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.config.models import AddOnRulesConfig
from jstock_advisor.domain.entities.enums import (
    EligibilityBlockCategory,
    PortfolioValuationBasis,
    RecommendationType,
)
from jstock_advisor.domain.entities.notification_eligibility import NotificationEligibility

PROJECTION_BASIS_MINIMUM_TRADING_UNIT = "MINIMUM_TRADING_UNIT_AT_CURRENT_PRICE"


@dataclass(frozen=True)
class AddOnRiskAssessment:
    projection_basis: str
    projected_add_on_quantity: int
    projected_add_on_price: Decimal
    projected_add_on_amount: Decimal
    current_position_ratio: Decimal | None
    projected_position_ratio: Decimal | None
    current_sector_ratio: Decimal | None
    projected_sector_ratio: Decimal | None
    position_limit_exceeded: bool
    sector_limit_exceeded: bool
    portfolio_data_reliable: bool
    reasons: tuple[str, ...]


def _safe_ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def evaluate_add_on_eligibility(
    *,
    current_market_value: Decimal,
    current_price: Decimal,
    trading_unit: int,
    portfolio_total_market_value: Decimal | None,
    sector_total_market_value: Decimal | None,
    portfolio_valuation_basis: PortfolioValuationBasis,
    conflicting_holding_action: RecommendationType | None,
    holding_data_inconsistent: bool,
    holding_is_odd_lot: bool,
    config: AddOnRulesConfig,
) -> tuple[AddOnRiskAssessment, NotificationEligibility]:
    """買い増し固有リスクを評価する(要求仕様§5・§6・§7)。

    portfolio_total_market_value/sector_total_market_valueは、finalize時に
    sector_entries(全保有銘柄が報告した業種・時価)を集計した値を渡すこと
    (この関数自体は追加のデータ取得を行わない)。current_market_valueは
    引数のsector_total_market_value/portfolio_total_market_valueに含まれて
    いる前提(=集計対象銘柄自身の値も合算済み)。
    """
    projected_add_on_amount = current_price * trading_unit

    portfolio_data_reliable = (
        portfolio_valuation_basis == PortfolioValuationBasis.MARKET_VALUE
        and portfolio_total_market_value is not None
        and portfolio_total_market_value > 0
        and sector_total_market_value is not None
    )

    current_position_ratio: Decimal | None = None
    projected_position_ratio: Decimal | None = None
    current_sector_ratio: Decimal | None = None
    projected_sector_ratio: Decimal | None = None
    position_limit_exceeded = False
    sector_limit_exceeded = False

    if portfolio_data_reliable:
        assert portfolio_total_market_value is not None
        assert sector_total_market_value is not None
        current_position_ratio = _safe_ratio(current_market_value, portfolio_total_market_value)
        projected_position_ratio = _safe_ratio(
            current_market_value + projected_add_on_amount,
            portfolio_total_market_value + projected_add_on_amount,
        )
        current_sector_ratio = _safe_ratio(sector_total_market_value, portfolio_total_market_value)
        projected_sector_ratio = _safe_ratio(
            sector_total_market_value + projected_add_on_amount,
            portfolio_total_market_value + projected_add_on_amount,
        )
        position_limit_exceeded = (
            projected_position_ratio is not None
            and projected_position_ratio > Decimal(str(config.block_add_on_single_stock_ratio))
        )
        sector_limit_exceeded = (
            projected_sector_ratio is not None
            and projected_sector_ratio > Decimal(str(config.block_add_on_sector_ratio))
        )

    reasons: list[str] = []
    if conflicting_holding_action is not None:
        reasons.append(f"CONFLICTING_HOLDING_ACTION:{conflicting_holding_action.value}")
    if holding_data_inconsistent:
        reasons.append("HOLDING_DATA_INCONSISTENT")
    if holding_is_odd_lot:
        reasons.append("ODD_LOT_HOLDING")
    if not portfolio_data_reliable:
        reasons.append("CONCENTRATION_RELIABILITY_INSUFFICIENT")
    if position_limit_exceeded:
        reasons.append("POSITION_LIMIT_EXCEEDED")
    if sector_limit_exceeded:
        reasons.append("SECTOR_LIMIT_EXCEEDED")

    assessment = AddOnRiskAssessment(
        projection_basis=PROJECTION_BASIS_MINIMUM_TRADING_UNIT,
        projected_add_on_quantity=trading_unit,
        projected_add_on_price=current_price,
        projected_add_on_amount=projected_add_on_amount,
        current_position_ratio=current_position_ratio,
        projected_position_ratio=projected_position_ratio,
        current_sector_ratio=current_sector_ratio,
        projected_sector_ratio=projected_sector_ratio,
        position_limit_exceeded=position_limit_exceeded,
        sector_limit_exceeded=sector_limit_exceeded,
        portfolio_data_reliable=portfolio_data_reliable,
        reasons=tuple(reasons),
    )

    if not config.enabled:
        return assessment, NotificationEligibility(eligible=True)

    # 優先順位: 売却競合 → 保有データ整合性(単元未満株を含む) →
    # ポートフォリオデータ信頼性 → 銘柄集中 → 業種集中。最初に該当した
    # 1件だけを理由として返す。
    #
    # 【保有判断スコア方式移行に伴う既知の未対応事項】conflicting_holding_actionは
    # buy_candidates_handler.pyがSellSignalService(旧エンジン)を直接呼んで
    # 得た値であり、旧エンジンが返すrecommendation_typeであれば種類を問わず
    # ここでブロック対象になる(is_sell_like()等のフィルタは介在しない)。
    # mode=legacy/shadowの間はSellSignalServiceが引き続き権威であるため問題ないが、
    # mode=active移行後(HoldingDecisionServiceが権威になった後)は
    # buy_candidates_handler.py側もHoldingDecisionServiceの結果(SELL_CONSIDERATION/
    # STRONG_SELL_CONSIDERATION/URGENT_HOLDING_REVIEW)を参照するよう更新が必要。
    # Phase3切替の一部として別途対応する。
    if conflicting_holding_action is not None and config.block_on_sell_signal:
        return assessment, NotificationEligibility(
            eligible=False,
            block_category=EligibilityBlockCategory.CONFLICTING_HOLDING_ACTION,
            block_reason=conflicting_holding_action.value,
        )
    if holding_data_inconsistent and config.require_holding_data_consistency:
        return assessment, NotificationEligibility(
            eligible=False,
            block_category=EligibilityBlockCategory.HOLDING_DATA_INCONSISTENT,
            block_reason="HOLDING_DATA_INCONSISTENT",
        )
    if holding_is_odd_lot and config.block_add_on_on_odd_lot:
        return assessment, NotificationEligibility(
            eligible=False,
            block_category=EligibilityBlockCategory.HOLDING_DATA_INCONSISTENT,
            block_reason="ODD_LOT_HOLDING",
        )
    if not portfolio_data_reliable:
        return assessment, NotificationEligibility(
            eligible=False,
            block_category=EligibilityBlockCategory.PORTFOLIO_DATA_RELIABILITY,
            block_reason="CONCENTRATION_RELIABILITY_INSUFFICIENT",
        )
    if position_limit_exceeded:
        return assessment, NotificationEligibility(
            eligible=False,
            block_category=EligibilityBlockCategory.POSITION_CONCENTRATION,
            block_reason="POSITION_LIMIT_EXCEEDED",
        )
    if sector_limit_exceeded:
        return assessment, NotificationEligibility(
            eligible=False,
            block_category=EligibilityBlockCategory.SECTOR_CONCENTRATION,
            block_reason="SECTOR_LIMIT_EXCEEDED",
        )

    return assessment, NotificationEligibility(eligible=True)
