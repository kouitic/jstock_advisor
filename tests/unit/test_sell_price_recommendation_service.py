from decimal import Decimal

from jstock_advisor.domain.entities.enums import ConfidenceLevel, HoldingDecisionCategory
from jstock_advisor.domain.entities.valuation import FairValueMethodResult, FairValueRange
from jstock_advisor.services.sell_price_recommendation_service import recommend_sell_prices


def _fair_value_range(*, bear: Decimal, usable_for_trading_judgment: bool) -> FairValueRange:
    return FairValueRange(
        bear=bear,
        neutral=bear,
        bull=bear,
        overall_confidence=ConfidenceLevel.MEDIUM,
        methods_used=[
            FairValueMethodResult(
                method="method0", fair_value=bear, confidence=ConfidenceLevel.MEDIUM
            )
        ],
        methods_excluded=[],
        usable_for_trading_judgment=usable_for_trading_judgment,
    )


def test_strong_sell_full_price_omitted_when_fair_value_unusable() -> None:
    # LINE通知/監査分離のコードレビュー対応回帰テスト。旧実装は
    # usable_for_trading_judgment=Falseでもbearが存在すれば全部売却検討価格の
    # 算出に使ってしまい、現在値へのフォールバック捏造も行っていた。新実装では
    # 目安価格を捏造せずNone(算定保留)のままとする。
    prices = recommend_sell_prices(
        current_price=Decimal("1600"),
        category=HoldingDecisionCategory.STRONG_SELL_CONSIDERATION,
        hard_gate_triggered=False,
        fair_value_range=_fair_value_range(bear=Decimal("2000"), usable_for_trading_judgment=False),
    )
    assert prices.full_profit_consideration_price is None
    # 監視専用フィールド(見直し目安)は目安価格ではなく現在値を提示し続ける。
    assert prices.stop_review_price is not None
    assert prices.stop_review_price.price == Decimal("1600")


def test_strong_sell_full_price_uses_bear_when_fair_value_usable() -> None:
    prices = recommend_sell_prices(
        current_price=Decimal("1600"),
        category=HoldingDecisionCategory.STRONG_SELL_CONSIDERATION,
        hard_gate_triggered=False,
        fair_value_range=_fair_value_range(bear=Decimal("2000"), usable_for_trading_judgment=True),
    )
    assert prices.full_profit_consideration_price is not None
    assert prices.full_profit_consideration_price.price == Decimal("2000")


def test_sell_consideration_stop_review_omits_bear_when_fair_value_unusable() -> None:
    prices = recommend_sell_prices(
        current_price=Decimal("1600"),
        category=HoldingDecisionCategory.SELL_CONSIDERATION,
        hard_gate_triggered=False,
        fair_value_range=_fair_value_range(bear=Decimal("2000"), usable_for_trading_judgment=False),
    )
    assert prices.stop_review_price is None
    assert prices.partial_profit_start_price is not None
    assert prices.partial_profit_start_price.price == Decimal("1600")


def test_hard_gate_triggered_ignores_fair_value_usability() -> None:
    prices = recommend_sell_prices(
        current_price=Decimal("1600"),
        category=HoldingDecisionCategory.STRONG_SELL_CONSIDERATION,
        hard_gate_triggered=True,
        fair_value_range=_fair_value_range(bear=Decimal("2000"), usable_for_trading_judgment=False),
    )
    assert prices.immediate_execution_price is not None
    assert prices.immediate_execution_price.price == Decimal("1600")
