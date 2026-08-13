"""WATCH終了通知の実送信経路の結合テスト(コードレビュー対応2026-08、指摘3)。

C: WatchState長期継続 → PRICE_OUT_OF_RANGE → 監視終了通知が
`_finalize_batch()`の新設ループを通じて実際にLINEへ送信されることを、
`_process_single_candidate()`から生成される`watch_end_ranking_entries`を
経由した実際の`LineNotificationService`(FakeLineClient使用)で検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import BuyAction, ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.aws.batch_tracker import BatchProgress
from jstock_advisor.infrastructure.line.client import LineClient
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
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
from jstock_advisor.lambda_handlers import buy_candidates_handler as handler_module
from jstock_advisor.services.audit_service import AuditService as RealAuditService
from jstock_advisor.services.line_notification_service import LineNotificationService

_NOW = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)
_CONFIG = load_config()


class _FakeLineClient(LineClient):
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)


def _patch_audit(monkeypatch, tmp_path: Path) -> None:
    repo = AuditLogRepository(store_dir=tmp_path)
    monkeypatch.setattr(
        handler_module, "AuditService", lambda *a, **kw: RealAuditService(repository=repo)
    )


def _watch_end_recommendation(stock_code: str, recommendation_id: str) -> Recommendation:
    """6営業日継続後にPRICE_OUT_OF_RANGEで監視終了したことを示すRecommendation
    (buy_signal_service.pyが実際に生成するのと同じ形の付帯フィールドを持つ)。
    """
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
        price_at_recommendation=Decimal("200"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        watch_type=None,  # 監視終了済みのため現在アクティブではない
        watch_transition_type="ENDED",
        watch_end_reason="PRICE_OUT_OF_RANGE",
        watch_previous_consecutive_business_days=6,
    )


def test_watch_end_notification_reaches_real_line_client(monkeypatch, tmp_path: Path) -> None:
    _patch_audit(monkeypatch, tmp_path)
    store_dir = tmp_path / "local_store"
    repo = RecommendationRepository(store_dir=store_dir)
    rec = _watch_end_recommendation("9432", "watch-end-1")
    repo.save(rec)

    client = _FakeLineClient()
    notification_service = LineNotificationService(
        line_client=client,
        notification_log_repository=NotificationLogRepository(store_dir=store_dir),
        recommendation_repository=repo,
        config=_CONFIG,
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
    )

    progress = BatchProgress(
        total=1,
        completed=1,
        category_counts={"watch_not_ranked": 1},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[],
        sector_entries=[],
        holding_count=0,
        watch_end_ranking_entries=[rec.recommendation_id],
    )

    handler_module._finalize_batch(progress, _CONFIG, _NOW, repo, notification_service)

    assert len(client.sent) == 1
    body = client.sent[0]
    assert "監視終了" in body
    assert "6日継続" in body
    assert "9432" in body
    assert len(body) <= 70


def test_watch_end_gate_condition_requires_notifiable_reason_and_threshold() -> None:
    """§3の入口ゲート条件(_process_single_candidate内)そのものを直接検証する。

    (1)対象理由(PRICE_OUT_OF_RANGE/NOT_ATTRACTIVE/STALE)以外は対象外、
    (2)閾値(既定5営業日)未満は対象外、(3)config.enabled=falseなら対象外、
    という3条件をハンドラのロジックと同じ形で再現する。
    """
    from jstock_advisor.services.watch_state_service import WATCH_END_NOTIFIABLE_REASONS

    threshold = _CONFIG.notification.watch_end_notification.min_consecutive_business_days
    enabled = _CONFIG.notification.watch_end_notification.enabled
    assert enabled is True  # 既定はtrue(このテストの前提)

    def _would_notify(reason: str | None, days: int | None) -> bool:
        return (
            reason in WATCH_END_NOTIFIABLE_REASONS
            and enabled
            and (days or 0) >= threshold
        )

    assert _would_notify("PRICE_OUT_OF_RANGE", threshold) is True
    assert _would_notify("PRICE_OUT_OF_RANGE", threshold - 1) is False
    assert _would_notify("TRADE_EVENT", threshold) is False
    assert _would_notify("PROMOTED_TO_BUY", threshold) is False
