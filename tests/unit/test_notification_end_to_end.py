"""通知簡潔化・cross-pipeline重複抑止の結合テスト(コードレビュー対応2026-08)。

単体formatter(format_notification_text())を直接呼んだ出力ではなく、
LineNotificationService → FakeLineClient.push_message という実際の送信経路を
通った本文を検証する(指摘1・指摘5)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import (
    BuyPriceLevels,
    PriceWithRationale,
    SellPriceLevels,
)
from jstock_advisor.domain.entities.enums import (
    BuyAction,
    ConfidenceLevel,
    NotificationStatus,
    RecommendationType,
    StockType,
    WatchTransitionType,
    WatchType,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.line.client import LineClient
from jstock_advisor.infrastructure.local_repository.daily_notification_priority_repository import (
    DailyNotificationPriorityRepository,
)
from jstock_advisor.infrastructure.local_repository.holdings_snapshot_repository import (
    HoldingsSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.line_notification_service import LineNotificationService

_NOW = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)
_CONFIG = load_config()
_MAX_CHARS = 70


class _FakeLineClient(LineClient):
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture
def service(tmp_path: Path) -> tuple[LineNotificationService, _FakeLineClient]:
    store_dir = tmp_path / "local_store"
    client = _FakeLineClient()
    svc = LineNotificationService(
        line_client=client,
        notification_log_repository=NotificationLogRepository(store_dir=store_dir),
        recommendation_repository=RecommendationRepository(store_dir=store_dir),
        config=_CONFIG,
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
    )
    return svc, client


def _buy_recommendation(
    stock_code: str = "4516", recommendation_id: str = "rec-buy-1"
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name="日本新薬",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            entry=PriceWithRationale(price=Decimal("3440"), rationale="x"),
            standard=PriceWithRationale(price=Decimal("3300"), rationale="x"),
            strong=PriceWithRationale(price=Decimal("3100"), rationale="x"),
        ),
        price_at_recommendation=Decimal("3395"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        buy_action=BuyAction.BUY,
        company_quality_score=72.0,
        stock_types=[StockType.INCOME, StockType.QUALITY],
        reasons=["財務健全性が高評価", "連続増配実績あり"],
    )


def _near_buy_recommendation(
    stock_code: str = "9432", recommendation_id: str = "rec-nb-1"
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name="NTT",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            entry=PriceWithRationale(price=Decimal("150"), rationale="x"),
            standard=PriceWithRationale(price=Decimal("140"), rationale="x"),
            strong=PriceWithRationale(price=Decimal("130"), rationale="x"),
        ),
        price_at_recommendation=Decimal("158"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        watch_type=WatchType.NEAR_BUY,
        near_buy_consecutive_business_days=4,
        company_quality_score=65.0,
        required_decline_to_entry_pct=Decimal("5.1"),
        reasons=["財務健全性が高評価"],
    )


def _watch_before_earnings_recommendation(stock_code: str = "7203") -> Recommendation:
    return Recommendation(
        recommendation_id="rec-wbe-1",
        stock_code=stock_code,
        stock_name="トヨタ自動車",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(),
        price_at_recommendation=Decimal("2800"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        buy_action=BuyAction.WATCH_BEFORE_EARNINGS,
    )


def _sell_recommendation(
    stock_code: str = "4631", recommendation_id: str = "rec-sell-1"
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name="ＤＩＣ",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.SELL,
        sell_prices=SellPriceLevels(
            stop_review_price=PriceWithRationale(price=Decimal("4000"), rationale="x")
        ),
        price_at_recommendation=Decimal("4384"),
        average_purchase_price_at_recommendation=Decimal("3745"),
        shares_at_recommendation=100,
        reasons=["減配(major)", "営業利益の継続悪化(major)"],
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )


def _critical_risk_recommendation(stock_code: str = "1234") -> Recommendation:
    long_reason = (
        "継続企業の前提に重大な疑義が生じたため、緊急に保有内容の見直しを検討してください。"
        "詳細はIR資料をご確認ください。"
    )
    return Recommendation(
        recommendation_id="rec-critical-1",
        stock_code=stock_code,
        stock_name="サンプル株式会社",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.URGENT_HOLDING_REVIEW,
        price_at_recommendation=Decimal("500"),
        shares_at_recommendation=100,
        reasons=[long_reason],
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


# --- A: Recommendation → LineNotificationService → FakeLineClient → 実本文 ---


def test_buy_notification_actual_pushed_body_within_70_chars(service) -> None:
    svc, client = service
    rec = _buy_recommendation()

    # send_recommendation_notification()を直接呼ぶ(データ品質・整合性検証
    # ゲートは別の既存テストで検証済みのため、ここでは実送信経路を通った
    # 本文の簡潔化のみを検証する。notify_recommendation_with_status経由の
    # 送信可否判定はtest_line_notification_service.pyで別途カバーされている)。
    svc.send_recommendation_notification(rec, _NOW)

    assert len(client.sent) == 1
    body = client.sent[0]
    assert rec.stock_code in body
    # G: BUYでは打診買い価格の「打診」表現が従来どおり使われる
    # (コードレビュー対応2026-08、指摘3)。
    assert "打診" in body
    assert len(body) <= _MAX_CHARS
    # 旧長文formatterが誤って使われていないことの確認(旧専用の見出し文言)。
    assert "算出手法間のばらつき" not in body
    assert "通知ID" not in body


def test_near_buy_notification_actual_pushed_body_within_70_chars(service) -> None:
    """コードレビュー対応(2026-08、LINE通知アクション限定化): NEAR BUYは
    evaluate_notification_status経由ではもはや送信されない(NON_ACTIONABLE)。
    本テストの主眼は実送信の可否ではなく、send_recommendation_notification()を
    直接呼んだ場合の本文フォーマット自体(短文・「接近」「打診」表現)であるため、
    そちらは従来どおり直接呼び出して検証する。
    """
    svc, client = service
    rec = _near_buy_recommendation()

    outcome = svc.evaluate_notification_status(rec, _NOW)
    assert outcome.status == NotificationStatus.NOT_REQUIRED
    assert outcome.block_category is not None and outcome.block_category.value == "NON_ACTIONABLE"
    svc.send_recommendation_notification(rec, _NOW)

    body = client.sent[0]
    assert "接近" in body
    assert rec.stock_code in body
    assert len(body) <= _MAX_CHARS
    # G: NEAR BUYでも打診買い価格の「打診」表現が従来どおり使われる
    # (コードレビュー対応2026-08、指摘3)。
    assert "打診" in body


def test_watch_before_earnings_notification_actual_pushed_body_within_70_chars(
    service,
) -> None:
    svc, client = service
    rec = _watch_before_earnings_recommendation()

    svc.send_recommendation_notification(rec, _NOW)

    body = client.sent[0]
    assert "決算待ち" in body
    assert rec.stock_code in body
    assert len(body) <= _MAX_CHARS


def test_sell_notification_actual_pushed_body_within_70_chars(service) -> None:
    svc, client = service
    rec = _sell_recommendation()

    svc.notify_recommendation(rec, _NOW)

    body = client.sent[0]
    assert rec.stock_code in body
    assert len(body) <= _MAX_CHARS
    assert "投資前提悪化の可能性" not in body  # 旧_format_sell_messageのタイトル文言
    # F: SELLでは「打診」を使わず、価格フィールドの業務的意味に応じた
    # ラベル(このfixtureはstop_review_price設定のため「見直し」)を使う
    # (コードレビュー対応2026-08、指摘3)。
    assert "打診" not in body
    assert "見直し" in body


def test_critical_risk_notification_keeps_reason_even_if_over_70_chars(service) -> None:
    svc, client = service
    rec = _critical_risk_recommendation()

    svc.notify_recommendation(rec, _NOW)

    body = client.sent[0]
    assert "継続企業の前提に重大な疑義が生じたため" in body
    assert "IR資料をご確認ください" in body


# --- B: WatchState day4 → BUY昇格 → Recommendation → 通知 → 「4日監視後」 ---


def test_promoted_to_buy_notification_shows_reached_label_and_days(service) -> None:
    svc, client = service
    rec = _buy_recommendation(stock_code="9432", recommendation_id="rec-promoted-1").model_copy(
        update={
            "watch_transition_type": WatchTransitionType.PROMOTED_TO_BUY.value,
            "watch_previous_consecutive_business_days": 4,
        }
    )

    svc.send_recommendation_notification(rec, _NOW)

    body = client.sent[0]
    assert body.startswith("到達")
    assert "4日監視後" in body
    assert rec.stock_code in body
    assert len(body) <= _MAX_CHARS


# --- E/F: cross-pipeline優先度(同一銘柄・同一日) ---


def test_near_buy_sent_then_sell_still_sent_higher_priority(service) -> None:
    """E: 同日NEAR BUY通知済み → SELL発生 → SELLは送信される(高優先度は必ず貫通)。"""
    svc, client = service
    stock_code = "9432"
    near_buy = _near_buy_recommendation(stock_code=stock_code, recommendation_id="rec-nb-e")
    sell = _sell_recommendation(stock_code=stock_code, recommendation_id="rec-sell-e")

    svc.send_recommendation_notification(near_buy, _NOW)
    assert len(client.sent) == 1

    priority = svc.check_cross_pipeline_priority_eligibility(sell, _NOW)
    assert priority.eligible is True

    svc.send_recommendation_notification(sell, _NOW)
    assert len(client.sent) == 2
    assert "売却検討" in client.sent[1]


def test_sell_sent_then_near_buy_suppressed_lower_priority(service) -> None:
    """F: 同日SELL通知済み → NEAR BUY発生 → NEAR BUYは送信されない。

    コードレビュー対応(2026-08、LINE通知アクション限定化): NEAR_BUYはもはや
    LINE送信されない(NON_ACTIONABLE)カテゴリのため、cross-pipeline重複抑止
    (_NOTIFICATION_PRIORITY)の対象から外れた(priority=0扱い、eligible=True)。
    抑止の理由がLOW_PRIORITYからNON_ACTIONABLEへ変わっただけで、「NEAR BUYが
    追加送信されない」という結果自体は変わらない。
    """
    svc, client = service
    stock_code = "9432"
    sell = _sell_recommendation(stock_code=stock_code, recommendation_id="rec-sell-f")
    near_buy = _near_buy_recommendation(stock_code=stock_code, recommendation_id="rec-nb-f")

    svc.send_recommendation_notification(sell, _NOW)
    assert len(client.sent) == 1

    priority = svc.check_cross_pipeline_priority_eligibility(near_buy, _NOW)
    assert priority.eligible is True  # NEAR_BUYはcross-pipeline優先度表の対象外(priority=0)

    # evaluate_notification_status経由でNOT_REQUIRED(NON_ACTIONABLE)となり、
    # 実送信されないこと。
    outcome = svc.evaluate_notification_status(near_buy, _NOW)
    assert outcome.status == NotificationStatus.NOT_REQUIRED
    assert outcome.block_category is not None and outcome.block_category.value == "NON_ACTIONABLE"
    assert len(client.sent) == 1  # SELLの1件のみ、NEAR BUYは追加送信されない
