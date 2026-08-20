import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.common import (
    BuyPriceLevels,
    PriceWithRationale,
    SellPriceLevels,
)
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    NotificationCategory,
    RecommendationType,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.notification.recommendation_adapter import (
    build_notification_text_input,
)

_NOW = dt.datetime(2026, 8, 14, 8, 0, tzinfo=dt.UTC)


def _make_recommendation(
    *,
    recommendation_type: RecommendationType,
    sell_prices: SellPriceLevels | None = None,
    buy_prices: BuyPriceLevels | None = None,
    suggested_sell_shares: int | None = None,
    suggested_sell_ratio: float | None = None,
) -> Recommendation:
    return Recommendation(
        recommendation_id="rec-1",
        stock_code="2914",
        stock_name="日本たばこ産業",
        recommended_at=_NOW,
        recommendation_type=recommendation_type,
        buy_prices=buy_prices,
        sell_prices=sell_prices,
        price_at_recommendation=Decimal("4200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
        suggested_sell_shares=suggested_sell_shares,
        suggested_sell_ratio=suggested_sell_ratio,
    )


def test_review_routes_to_manual_review_not_sell() -> None:
    # コードレビュー対応(2026-08、LINE通知/監査分離)の回帰テスト。
    # RecommendationType.REVIEWはis_sell_like()に含まれるが、resolve_notification_
    # category()側の特別分岐によりMANUAL_REVIEWへルーティングされるべきであり、
    # 本モジュールのビルダー選択もそれに追従することを確認する。
    rec = _make_recommendation(recommendation_type=RecommendationType.REVIEW)
    text_input = build_notification_text_input(rec, NotificationCategory.MANUAL_REVIEW)
    assert text_input.category == NotificationCategory.MANUAL_REVIEW
    assert text_input.reason == "売買判断を保留"
    assert text_input.target_price is None


def test_watch_price_field_uses_partial_profit_start_price() -> None:
    rec = _make_recommendation(
        recommendation_type=RecommendationType.WATCH,
        sell_prices=SellPriceLevels(
            partial_profit_start_price=PriceWithRationale(price=Decimal("4500"), rationale="x")
        ),
    )
    text_input = build_notification_text_input(rec, NotificationCategory.WATCH)
    assert text_input.target_price == Decimal("4500")
    assert text_input.target_price_label == "利確検討"


def test_watch_price_withheld_when_no_partial_profit_start_price() -> None:
    rec = _make_recommendation(
        recommendation_type=RecommendationType.WATCH, sell_prices=SellPriceLevels()
    )
    text_input = build_notification_text_input(rec, NotificationCategory.WATCH)
    assert text_input.target_price is None
    assert text_input.target_price_withheld_label == "価格目安は算定保留"


# テストコード削減対応2026-08: 価格選択ペアをparametrizeへ統合。各ケースが
# 元々検証していたフィールドの集合(expected_checks)のみをそのまま転記し、
# 新たなassertは追加しない(例: 全部売却falls_back側はlabel_overrideを
# 元々検証していなかったため、統合後もそのままにする)。
@pytest.mark.parametrize(
    ("sell_prices_kwargs", "expected_checks"),
    [
        (
            {
                "recommended_limit_price": PriceWithRationale(price=Decimal("4600"), rationale="x"),
                "partial_profit_start_price": PriceWithRationale(
                    price=Decimal("4300"), rationale="x"
                ),
            },
            {"target_price": Decimal("4600")},
        ),
        (
            {
                "partial_profit_start_price": PriceWithRationale(
                    price=Decimal("4300"), rationale="x"
                )
            },
            {"target_price": Decimal("4300")},
        ),
    ],
    ids=[
        "prefers_recommended_limit_price_over_partial_start",
        "falls_back_to_partial_start_price",
    ],
)
def test_partial_sell_target_price_selection(
    sell_prices_kwargs: dict[str, object], expected_checks: dict[str, object]
) -> None:
    rec = _make_recommendation(
        recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
        sell_prices=SellPriceLevels(**sell_prices_kwargs),
    )
    text_input = build_notification_text_input(rec, NotificationCategory.PARTIAL_SELL)
    for field, expected in expected_checks.items():
        assert getattr(text_input, field) == expected


@pytest.mark.parametrize(
    ("recommendation_type", "sell_prices_kwargs", "expected_checks"),
    [
        (
            RecommendationType.FULL_PROFIT_TAKE,
            {
                "immediate_execution_price": PriceWithRationale(
                    price=Decimal("4200"), rationale="x"
                ),
                "full_profit_consideration_price": PriceWithRationale(
                    price=Decimal("5000"), rationale="x"
                ),
            },
            {
                "target_price": Decimal("4200"),
                "target_price_label": "即時執行",
                "label_override": "全部売却検討",
            },
        ),
        (
            RecommendationType.STRONG_SELL_CONSIDERATION,
            {
                "full_profit_consideration_price": PriceWithRationale(
                    price=Decimal("5000"), rationale="x"
                )
            },
            {"target_price": Decimal("5000"), "target_price_label": "全部売却目安"},
        ),
    ],
    ids=[
        "prefers_immediate_execution_over_full_profit_consideration",
        "falls_back_to_full_profit_consideration_price",
    ],
)
def test_full_sell_target_price_selection(
    recommendation_type: RecommendationType,
    sell_prices_kwargs: dict[str, object],
    expected_checks: dict[str, object],
) -> None:
    rec = _make_recommendation(
        recommendation_type=recommendation_type,
        sell_prices=SellPriceLevels(**sell_prices_kwargs),
    )
    text_input = build_notification_text_input(rec, NotificationCategory.SELL)
    for field, expected in expected_checks.items():
        assert getattr(text_input, field) == expected


def test_full_sell_never_shows_stop_review_price() -> None:
    # 全部売却検討系ではstop_review_price(常に現在値の監視専用フィールド)を
    # 目安価格として参照しない(過去の「見直し{現在値}円」誤表示バグの回帰防止)。
    rec = _make_recommendation(
        recommendation_type=RecommendationType.STRONG_SELL_CONSIDERATION,
        sell_prices=SellPriceLevels(
            stop_review_price=PriceWithRationale(price=Decimal("4200"), rationale="x")
        ),
    )
    text_input = build_notification_text_input(rec, NotificationCategory.SELL)
    assert text_input.target_price is None
    assert text_input.target_price_withheld_label == "全部売却目安は算定保留"


def test_sell_consideration_uses_stop_review_price() -> None:
    rec = _make_recommendation(
        recommendation_type=RecommendationType.SELL_CONSIDERATION,
        sell_prices=SellPriceLevels(
            stop_review_price=PriceWithRationale(price=Decimal("4000"), rationale="x")
        ),
    )
    text_input = build_notification_text_input(rec, NotificationCategory.SELL)
    assert text_input.target_price == Decimal("4000")
    assert text_input.target_price_label == "見直し"
    assert text_input.label_override is None


def test_buy_shows_tentative_and_standard_prices() -> None:
    rec = _make_recommendation(
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            tentative=PriceWithRationale(price=Decimal("3600"), rationale="x"),
            standard=PriceWithRationale(price=Decimal("3400"), rationale="x"),
            aggressive=PriceWithRationale(price=Decimal("2900"), rationale="x"),
        ),
    )
    text_input = build_notification_text_input(rec, NotificationCategory.BUY)
    assert text_input.target_price == Decimal("3600")
    assert text_input.secondary_target_price == Decimal("3400")
    assert text_input.secondary_target_price_label == "通常"


# --- 指摘3対応: suggested_sell_shares/ratio 整合性(コードレビュー対応2026-08) ---


# テストコード削減対応2026-08: 指摘3(コードレビュー対応2026-08)のRegression
# ペアをparametrizeへ統合。300株/0.60ケースとNoneケースの両方をそのまま維持。
@pytest.mark.parametrize(
    ("sell_prices_kwargs", "suggested_sell_shares", "suggested_sell_ratio"),
    [
        (
            {
                "recommended_limit_price": PriceWithRationale(price=Decimal("4600"), rationale="x")
            },
            300,
            0.60,
        ),
        (
            {
                "partial_profit_start_price": PriceWithRationale(
                    price=Decimal("4300"), rationale="x"
                )
            },
            None,
            None,
        ),
    ],
    ids=["forwards_suggested_shares_and_ratio", "none_suggested_shares_handled"],
)
def test_partial_sell_suggested_shares_and_ratio_forwarding(
    sell_prices_kwargs: dict[str, object],
    suggested_sell_shares: int | None,
    suggested_sell_ratio: float | None,
) -> None:
    """_build_partial_sell() がRecommendationのsuggested_sell_shares/
    suggested_sell_ratioをNotificationTextInputへ正しく転送する(Noneの場合も
    含む)ことを確認する(コードレビュー対応2026-08、指摘3)。
    """
    rec = _make_recommendation(
        recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
        sell_prices=SellPriceLevels(**sell_prices_kwargs),
        suggested_sell_shares=suggested_sell_shares,
        suggested_sell_ratio=suggested_sell_ratio,
    )

    text_input = build_notification_text_input(rec, NotificationCategory.PARTIAL_SELL)
    assert text_input.suggested_sell_shares == suggested_sell_shares
    assert text_input.suggested_sell_ratio == suggested_sell_ratio


def test_case_l_full_sell_and_critical_risk_do_not_forward_suggested_shares() -> None:
    """FULL_PROFIT_TAKE/SELL/URGENT(CRITICAL_RISK)系のNotificationTextInputには、
    Recommendationにsuggested_sell_shares/ratioが(誤って)設定されていても
    転送されない(PARTIAL専用フィールドが他カテゴリへ混入しないことの回帰、
    再コードレビュー対応2026-08、指摘2 Case L)。_build_sell()/_build_critical_
    risk()はいずれもsuggested_sell_shares/ratio引数をNotificationTextInputへ
    一切渡さない(構造上常にNone)ことを確認する。
    """
    full_sell_rec = _make_recommendation(
        recommendation_type=RecommendationType.FULL_PROFIT_TAKE,
        sell_prices=SellPriceLevels(
            immediate_execution_price=PriceWithRationale(price=Decimal("4200"), rationale="x")
        ),
        suggested_sell_shares=300,
        suggested_sell_ratio=0.60,
    )
    sell_text_input = build_notification_text_input(full_sell_rec, NotificationCategory.SELL)
    assert sell_text_input.suggested_sell_shares is None
    assert sell_text_input.suggested_sell_ratio is None

    critical_rec = _make_recommendation(
        recommendation_type=RecommendationType.STRONG_SELL_CONSIDERATION,
        sell_prices=SellPriceLevels(
            immediate_execution_price=PriceWithRationale(price=Decimal("4200"), rationale="x")
        ),
        suggested_sell_shares=300,
        suggested_sell_ratio=0.60,
    )
    critical_text_input = build_notification_text_input(
        critical_rec, NotificationCategory.CRITICAL_RISK
    )
    assert critical_text_input.suggested_sell_shares is None
    assert critical_text_input.suggested_sell_ratio is None
