"""売却数量の生成から通知本文までの一気通貫統合テスト(再コードレビュー
対応2026-08、指摘5)。

以前のformatter/adapterテストはsuggested_sell_shares=300・ratio=0.60を
fixtureへ直書きしていたため、実際に

    compute_suggested_sell_shares()(数量計算)
        -> Recommendation(構築)
        -> build_notification_text_input()(adapter変換)
        -> format_notification_text()(通知本文生成)

まで一貫して値が壊れずに伝播することは検証できていなかった。本モジュールは
サンリオ8136相当(500株保有・SellIntensity.STRONG・比率60%)で、数量計算関数の
出力をそのままRecommendationへ渡し、最終的な通知本文に「300株(60%)」が
現れることを確認する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import PriceWithRationale, SellPriceLevels
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    NotificationCategory,
    RecommendationType,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.notification.message_formatter import format_notification_text
from jstock_advisor.domain.notification.recommendation_adapter import (
    build_notification_text_input,
)
from jstock_advisor.domain.signals.trading_unit_feasibility import (
    compute_suggested_sell_shares,
    evaluate_trading_unit_feasibility,
)

_APP_CONFIG = load_config()
_NOW = dt.datetime(2026, 8, 14, 8, 0, tzinfo=dt.UTC)


def test_case_q_sanrio_500_shares_strong_intensity_end_to_end() -> None:
    """サンリオ8136相当: 500株保有・SellIntensity.STRONG・config比率60%から、
    数量計算 -> Recommendation -> adapter -> formatterまで一気通貫で
    「300株(60%)」に到達することを確認する(Case Q)。
    """
    holding_shares = 500
    target_ratio = _APP_CONFIG.profit_taking.partial_sell_ratios.strong
    assert target_ratio == 0.60

    feasibility = evaluate_trading_unit_feasibility(
        shares=holding_shares,
        trading_unit=100,
        odd_lot_trading_available=False,
    )
    assert feasibility.partial_sale_executable is True

    suggestion = compute_suggested_sell_shares(
        shares=holding_shares,
        trading_unit=feasibility.trading_unit,
        odd_lot_trading_available=feasibility.odd_lot_trading_available,
        target_sell_ratio=target_ratio,
    )
    assert suggestion is not None
    assert suggestion.shares == 300
    assert suggestion.ratio == 0.60

    rec = Recommendation(
        recommendation_id="rec-sanrio-8136",
        stock_code="8136",
        stock_name="サンリオ",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
        sell_prices=SellPriceLevels(
            recommended_limit_price=PriceWithRationale(price=Decimal("4600"), rationale="x")
        ),
        price_at_recommendation=Decimal("4200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
        suggested_sell_shares=suggestion.shares,
        suggested_sell_ratio=suggestion.ratio,
    )

    text_input = build_notification_text_input(rec, NotificationCategory.PARTIAL_SELL)
    assert text_input.suggested_sell_shares == 300
    assert text_input.suggested_sell_ratio == 0.60

    text = format_notification_text(text_input)
    assert "300株(60%)" in text
