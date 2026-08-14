"""売却価格候補の算出(実装プラン10節・15節)。

保有判断スコアの算出には現在株価・取得単価・含み益率を一切使わない
(投資前提悪化判定と利確判断を混在させない)。売却価格候補の算出はこの
サービスに分離し、score < notify_below_scoreの通知対象になった場合のみ呼ぶ。
既存の利確ロジック(profit_taking.py)を統合・削除するものではない。
"""

from __future__ import annotations

from decimal import Decimal

from jstock_advisor.domain.entities.common import PriceWithRationale, SellPriceLevels
from jstock_advisor.domain.entities.enums import HoldingDecisionCategory, PriceFieldBasis
from jstock_advisor.domain.entities.valuation import FairValueRange


def recommend_sell_prices(
    current_price: Decimal,
    category: HoldingDecisionCategory,
    hard_gate_triggered: bool,
    fair_value_range: FairValueRange | None,
) -> SellPriceLevels:
    """category/ハードゲート発動有無に応じて売却価格候補を組み立てる。

    - ハードゲート発動時: 即時執行目安価格(現在値ベース)を提示する。
    - STRONG_SELL_CONSIDERATION: 全部売却検討価格(適正価格弱気水準が使用可能な
      場合のみ)を提示する。適正価格が使用不能な場合は目安価格を捏造せず
      Noneのままとする(コードレビュー対応2026-08、LINE通知/監査分離)。
    - SELL_CONSIDERATION: 一部売却開始価格の目安(現在値)と、適正価格弱気水準が
      あれば売却目安価格として併記する。
    """
    if hard_gate_triggered:
        return SellPriceLevels(
            immediate_execution_price=PriceWithRationale(
                price=current_price,
                rationale="重大条件のため即時執行目安として現在値を提示",
                basis=PriceFieldBasis.IMMEDIATE_EXECUTION_REFERENCE,
                basis_type=None,
            )
        )

    # コードレビュー対応(2026-08、LINE通知/監査分離): usable_for_trading_judgment=False
    # の場合はbearを一切参照しない(判定ロジック側と同じ使用可否基準に揃える)。
    bear_price = (
        fair_value_range.bear
        if fair_value_range is not None and fair_value_range.usable_for_trading_judgment
        else None
    )

    if category == HoldingDecisionCategory.STRONG_SELL_CONSIDERATION:
        return SellPriceLevels(
            full_profit_consideration_price=(
                PriceWithRationale(
                    price=bear_price,
                    rationale="適正価格弱気水準を全部売却検討の目安とする",
                    basis=PriceFieldBasis.TARGET_PRICE,
                    basis_type=None,
                )
                if bear_price is not None
                else None
            ),
            stop_review_price=PriceWithRationale(
                price=current_price,
                rationale="投資前提再確認の目安として現在値を提示",
                basis=PriceFieldBasis.MONITORING_ONLY_NOT_A_SELL_TARGET,
                basis_type=None,
            ),
        )

    # SELL_CONSIDERATION
    return SellPriceLevels(
        partial_profit_start_price=PriceWithRationale(
            price=current_price,
            rationale="一部売却検討の目安として現在値を提示",
            basis=PriceFieldBasis.MONITORING_ONLY_NOT_A_SELL_TARGET,
            basis_type=None,
        ),
        stop_review_price=(
            PriceWithRationale(
                price=bear_price,
                rationale="適正価格弱気水準を投資前提再確認の目安とする",
                basis=PriceFieldBasis.TARGET_PRICE,
                basis_type=None,
            )
            if bear_price is not None
            else None
        ),
    )
