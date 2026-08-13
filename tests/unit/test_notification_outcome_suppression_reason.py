"""NotificationOutcomeの具体的抑止理由伝播の結合テスト(コードレビュー対応2026-08、指摘2)。

check_trade_cooldown_eligibility()・check_cross_pipeline_priority_eligibility()等の
NotificationEligibility(block_category/block_reason)が、evaluate_notification_status()
経由でNotificationOutcomeまで伝播し、holdings_watchlist_handler._resolve_suppression_reason()
がholdings側監査のnotification_suppression_reasonとして具体的な理由を記録できることを
確認する(単にstatus.valueだけになっていた不備の再発防止)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import SellPriceLevels
from jstock_advisor.domain.entities.enums import (
    BuyAction,
    ConfidenceLevel,
    EligibilityBlockCategory,
    NotificationStatus,
    RecommendationType,
    WatchType,
)
from jstock_advisor.domain.entities.holdings_snapshot import HoldingsSnapshotEntry
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
from jstock_advisor.lambda_handlers.holdings_watchlist_handler import _resolve_suppression_reason
from jstock_advisor.services.line_notification_service import LineNotificationService

_NOW = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)
_CONFIG = load_config()


class _FakeLineClient(LineClient):
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)


def _service(
    store_dir: Path, trade_detection_confirmed: bool = True
) -> tuple[LineNotificationService, _FakeLineClient]:
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
        trade_detection_confirmed=trade_detection_confirmed,
    )
    return svc, client


def _sell_recommendation(stock_code: str, recommendation_id: str) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.SELL,
        sell_prices=SellPriceLevels(),
        price_at_recommendation=Decimal("1000"),
        average_purchase_price_at_recommendation=Decimal("900"),
        shares_at_recommendation=100,
        reasons=["業績悪化"],
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )


def _near_buy_recommendation(stock_code: str, recommendation_id: str) -> Recommendation:
    from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale

    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
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
        near_buy_consecutive_business_days=1,
        company_quality_score=65.0,
        required_decline_to_entry_pct=Decimal("5.1"),
    )


def test_trade_cooldown_suppression_reason_is_specific(tmp_path: Path) -> None:
    """A: TradeCooldownによる抑止でTRADE_COOLDOWNが監査から確認できる。"""
    store_dir = tmp_path / "local_store"
    svc, _client = _service(store_dir)
    HoldingsSnapshotRepository(store_dir=store_dir).upsert(
        HoldingsSnapshotEntry(
            stock_code="4631",
            shares=100,
            average_purchase_price=Decimal("1000"),
            recorded_at=_NOW.date(),
            cooldown_until_date=_NOW.date() + dt.timedelta(days=3),
            active_holding=True,
        )
    )
    rec = _sell_recommendation("4631", "rec-cooldown-1")

    outcome = svc.evaluate_notification_status(rec, _NOW)

    assert outcome.status == NotificationStatus.NOT_REQUIRED
    assert outcome.block_category == EligibilityBlockCategory.TRADE_COOLDOWN
    assert outcome.block_reason == "TRADE_COOLDOWN"
    assert _resolve_suppression_reason(outcome) == "TRADE_COOLDOWN"


def test_trade_detection_in_progress_suppression_reason_is_specific(tmp_path: Path) -> None:
    """B: TradeDetection未確認による抑止でTRADE_DETECTION_IN_PROGRESSが確認できる。"""
    store_dir = tmp_path / "local_store"
    svc, _client = _service(store_dir, trade_detection_confirmed=False)
    rec = _sell_recommendation("4631", "rec-tdip-1")

    outcome = svc.evaluate_notification_status(rec, _NOW)

    assert outcome.status == NotificationStatus.NOT_REQUIRED
    assert outcome.block_category == EligibilityBlockCategory.TRADE_DETECTION_IN_PROGRESS
    assert outcome.block_reason == "TRADE_DETECTION_IN_PROGRESS"
    assert _resolve_suppression_reason(outcome) == "TRADE_DETECTION_IN_PROGRESS"


def test_low_priority_suppression_reason_is_specific_after_sell_sent(tmp_path: Path) -> None:
    """C: SELL通知済み後のNEAR BUYでLOW_PRIORITYが確認できる。"""
    store_dir = tmp_path / "local_store"
    svc, _client = _service(store_dir)
    stock_code = "9432"
    sell = _sell_recommendation(stock_code, "rec-sell-lp-1")
    svc.send_recommendation_notification(sell, _NOW)

    near_buy = _near_buy_recommendation(stock_code, "rec-nb-lp-1")
    outcome = svc.evaluate_notification_status(near_buy, _NOW)

    assert outcome.status == NotificationStatus.NOT_REQUIRED
    assert outcome.block_category == EligibilityBlockCategory.LOW_PRIORITY
    assert outcome.block_reason == "LOW_PRIORITY"
    assert _resolve_suppression_reason(outcome) == "LOW_PRIORITY"


def test_duplicate_stock_notification_suppression_reason_is_specific(tmp_path: Path) -> None:
    """D: 同一優先度の通知が既送の場合、DUPLICATE_STOCK_NOTIFICATIONが確認できる。"""
    store_dir = tmp_path / "local_store"
    svc, _client = _service(store_dir)
    stock_code = "9432"
    first = _near_buy_recommendation(stock_code, "rec-nb-dup-1")
    svc.send_recommendation_notification(first, _NOW)

    second = _near_buy_recommendation(stock_code, "rec-nb-dup-2")
    outcome = svc.evaluate_notification_status(second, _NOW)

    assert outcome.status == NotificationStatus.NOT_REQUIRED
    assert outcome.block_category == EligibilityBlockCategory.DUPLICATE_STOCK_NOTIFICATION
    assert outcome.block_reason == "DUPLICATE_STOCK_NOTIFICATION"
    assert _resolve_suppression_reason(outcome) == "DUPLICATE_STOCK_NOTIFICATION"


def test_ordinary_not_required_is_distinguishable_from_specific_reasons(tmp_path: Path) -> None:
    """E: 通常のNOT_REQUIRED(再送間隔未到達等)は具体的な抑止理由とは区別できる。

    block_category/block_reasonがいずれもNoneのままとなり、
    _resolve_suppression_reason()はstatus.value(RESEND_INTERVAL_NOT_REACHED)へ
    フォールバックする(TRADE_COOLDOWN等のEligibilityBlockCategoryとは異なる
    値であることを確認する)。

    cross-pipeline優先度記録(_record_daily_priority)の影響を受けないよう、
    send_recommendation_notification()は使わず、NotificationLog/Recommendation
    を直接リポジトリへ保存して「直前に同一内容で通知済み」の状態のみを再現する
    (優先度記録が無いstock_codeであれば、priorityチェックは無条件でeligibleに
    なるため、resend-interval判定のみを単独で検証できる)。
    """
    import uuid

    from jstock_advisor.domain.entities.enums import NotificationType
    from jstock_advisor.domain.entities.notification import NotificationLog

    store_dir = tmp_path / "local_store"
    svc, _client = _service(store_dir)
    rec = _sell_recommendation("4631", "rec-resend-1")
    RecommendationRepository(store_dir=store_dir).save(rec)
    NotificationLogRepository(store_dir=store_dir).save(
        NotificationLog(
            notification_id=str(uuid.uuid4()),
            notification_type=NotificationType.SELL_SIGNAL,
            stock_code=rec.stock_code,
            content_hash="dummy-hash-matching-current-recommendation-type",
            sent_at=_NOW,
            related_recommendation_id=rec.recommendation_id,
        )
    )

    # 同一価格・直後の再評価は再送間隔未到達でNOT_REQUIREDになる(価格変動・
    # 判定区分変化のいずれも無いため)。
    outcome = svc.evaluate_notification_status(rec, _NOW + dt.timedelta(hours=1))

    assert outcome.status != NotificationStatus.SENT
    assert outcome.block_category is None
    assert outcome.block_reason is None
    reason = _resolve_suppression_reason(outcome)
    assert reason == outcome.status.value
    assert reason not in {
        EligibilityBlockCategory.TRADE_COOLDOWN.value,
        EligibilityBlockCategory.TRADE_DETECTION_IN_PROGRESS.value,
        EligibilityBlockCategory.LOW_PRIORITY.value,
        EligibilityBlockCategory.DUPLICATE_STOCK_NOTIFICATION.value,
    }


def test_sent_notification_has_no_suppression_reason(tmp_path: Path) -> None:
    """送信済みの場合はnotification_suppression_reasonがNoneになること(既存動作)。"""
    from jstock_advisor.services.line_notification_service import NotificationOutcome

    outcome = NotificationOutcome(status=NotificationStatus.SENT, sent=True)
    assert _resolve_suppression_reason(outcome) is None
