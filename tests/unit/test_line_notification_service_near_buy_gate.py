"""NEAR BUYがLineNotificationServiceの3つの判定経路すべてで正しく通知評価まで
到達することの回帰テスト(BUY候補裾野拡大機能2026-08、指摘1)。

旧ゲート(buy_action not in BUY_FAMILY_ACTIONS)はNEAR BUY(buy_action=
WATCH_FOR_PRICE, watch_type=NEAR_BUY)を誤って抑止していた。
resolve_notification_category()経由の新ゲートでは、
evaluate_notification_status()・check_data_quality_eligibility()・
check_resend_eligibility()のいずれもNEAR BUYを即座に非適格にしないことを確認する。
"""

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import (
    BuyAction,
    ConfidenceLevel,
    NotificationStatus,
    RecommendationType,
    WatchType,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.line.client import LineClient
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


class _FakeLineClient(LineClient):
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)


def _near_buy_recommendation() -> Recommendation:
    return Recommendation(
        recommendation_id="rec-near-buy-1",
        stock_code="9432",
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
        near_buy_consecutive_business_days=1,
        company_quality_score=65.0,
        current_vs_entry_price_pct=Decimal("5.3"),
        required_decline_to_entry_pct=Decimal("5.1"),
        reasons=["財務健全性が高評価"],
    )


@pytest.fixture
def service(tmp_path: Path) -> LineNotificationService:
    store_dir = tmp_path / "local_store"
    return LineNotificationService(
        line_client=_FakeLineClient(),
        notification_log_repository=NotificationLogRepository(store_dir=store_dir),
        recommendation_repository=RecommendationRepository(store_dir=store_dir),
        config=_CONFIG,
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
    )


def test_evaluate_notification_status_reaches_sent_for_near_buy(
    service: LineNotificationService,
) -> None:
    rec = _near_buy_recommendation()
    outcome = service.evaluate_notification_status(rec, _NOW)
    assert outcome.status == NotificationStatus.SENT
    assert outcome.data_quality_blocked is False


def test_check_data_quality_eligibility_does_not_block_near_buy(
    service: LineNotificationService,
) -> None:
    rec = _near_buy_recommendation()
    eligibility = service.check_data_quality_eligibility(rec, _NOW)
    assert eligibility.eligible is True


def test_check_resend_eligibility_allows_near_buy_every_business_day(
    service: LineNotificationService,
) -> None:
    rec = _near_buy_recommendation()
    eligibility = service.check_resend_eligibility(rec, _NOW)
    assert eligibility.eligible is True


def test_ordinary_watch_for_price_without_watch_type_is_still_not_notifiable(
    service: LineNotificationService,
) -> None:
    """NEAR BUY非該当の通常WATCH_FOR_PRICE(watch_type=None)は引き続き
    通知対象外のまま(既存動作を変更しない回帰確認)。"""
    rec = _near_buy_recommendation().model_copy(
        update={"watch_type": None, "near_buy_consecutive_business_days": None}
    )
    outcome = service.evaluate_notification_status(rec, _NOW)
    assert outcome.status == NotificationStatus.NOT_REQUIRED
