"""Issue #34: MANUAL_REVIEW_REQUIRED 通知の再送抑止と claim 取得。

以前は送信前に NotificationLog を読まず claim も取得していなかったため、
条件が続く限り毎バッチ再送され、Lambda retry では即座に二重送信されていた。
本テストは他の通知種別と同じ 2 段構えの抑止が働くことと、
**初回は必ず送る**という安全弁の性質が変わっていないことを固定する。

値はすべて架空値（銘柄コード・銘柄名とも実在の企業を指さない）。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import PriceWithRationale, SellPriceLevels
from jstock_advisor.domain.entities.data_quality_alert import DataQualityAlert
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    ExecutionMode,
    NotificationType,
    RecommendationType,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.notification import NotificationLog
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.daily_notification_priority_repository import (
    DailyNotificationPriorityRepository,
)
from jstock_advisor.infrastructure.local_repository.holdings_snapshot_repository import (
    HoldingsSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_claim_repository import (
    NotificationClaimRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.line_notification_service import LineNotificationService

_CONFIG = load_config()
_NOW = dt.datetime(2026, 7, 24, 8, 0, tzinfo=dt.UTC)
_STOCK_CODE = "9999"


class _FakeLineClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)

    def reply_message(self, reply_token: str, text: str) -> None:
        self.sent.append(text)


def _build_service(
    store_dir: Path, *, claims_enabled: bool = True, validation: bool = False
) -> tuple[LineNotificationService, NotificationLogRepository, _FakeLineClient]:
    log_repo = NotificationLogRepository(store_dir=store_dir)
    client = _FakeLineClient()
    service = LineNotificationService(
        line_client=client,
        notification_log_repository=log_repo,
        recommendation_repository=RecommendationRepository(store_dir=store_dir),
        config=_CONFIG,
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
        notification_claim_repository=(
            NotificationClaimRepository(store_dir=store_dir) if claims_enabled else None
        ),
        **(
            {"execution_context": ExecutionContext(mode=ExecutionMode.VALIDATION)}
            if validation
            else {}
        ),
    )
    return service, log_repo, client


def _recommendation(recommendation_id: str = "rec-1") -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=_STOCK_CODE,
        stock_name="テスト銘柄",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.SELL,
        sell_prices=SellPriceLevels(
            stop_review_price=PriceWithRationale(price=Decimal("4000"), rationale="x")
        ),
        price_at_recommendation=Decimal("4384"),
        average_purchase_price_at_recommendation=Decimal("3745"),
        shares_at_recommendation=100,
        reasons=["[test] 架空の根拠"],
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        recommended_action_summary="売却を検討してください。",
        holding_risks=["[test] 架空のリスク"],
        independent_evidence_group_count=1,
    )


def _alert(rec: Recommendation) -> DataQualityAlert:
    return DataQualityAlert(
        stock_code=rec.stock_code,
        stock_name=rec.stock_name,
        detected_at=_NOW,
        process="notify_recommendation",
        contradictions=["[test] 架空の矛盾"],
        suppressed_values={},
        recalculation_result=None,
        action_required=True,
        recommended_action="要確認",
    )


def _existing_log(sent_at: dt.datetime, *, notification_id: str = "log-1") -> NotificationLog:
    return NotificationLog(
        notification_id=notification_id,
        notification_type=NotificationType.MANUAL_REVIEW_REQUIRED,
        stock_code=_STOCK_CODE,
        content_hash="x" * 16,
        sent_at=sent_at,
    )


# --- 安全弁: 初回は必ず送る --------------------------------------------------


def test_first_notification_is_always_sent(tmp_path: Path) -> None:
    """送信実績が無ければ抑止しない。安全弁の性質を変えていないことの固定。"""
    service, log_repo, client = _build_service(tmp_path / "s")
    rec = _recommendation()

    sent = service.notify_manual_review_required(rec, _alert(rec), _NOW)

    assert sent is True
    assert len(client.sent) == 1
    assert len(log_repo.list_all()) == 1


# --- U1: claim による retry の二重送信抑止 -----------------------------------


def test_retry_after_log_save_failure_does_not_send_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """push 成功 → log 保存前に落ちた、という retry で 2 通目を送らない。

    log が残っていれば U2 が先に止めるため、claim が単独で効くのはこの窓だけである。
    1 回目の log 保存を握りつぶして窓を再現し、**claim だけ**で止まることを確かめる。
    """
    service, log_repo, client = _build_service(tmp_path / "s")
    rec = _recommendation()

    monkeypatch.setattr(log_repo, "save", lambda log: None)
    assert service.notify_manual_review_required(rec, _alert(rec), _NOW) is True
    monkeypatch.undo()
    assert log_repo.list_all() == [], "送信実績が残っていない状態を作れている"

    second = service.notify_manual_review_required(rec, _alert(rec), _NOW)

    assert second is False, "claim が同一 identity を抑止する"
    assert len(client.sent) == 1, "LINE へは 1 通だけ"


def test_send_succeeds_when_claim_repository_is_absent(tmp_path: Path) -> None:
    """claim 基盤が無い構成でも安全弁は素通りさせる（送らない側へ倒さない）。"""
    service, _log_repo, client = _build_service(tmp_path / "s", claims_enabled=False)
    rec = _recommendation()

    assert service.notify_manual_review_required(rec, _alert(rec), _NOW) is True
    assert len(client.sent) == 1


# --- U2: resend_after_days による再送抑止 ------------------------------------


def test_within_resend_interval_is_suppressed(tmp_path: Path) -> None:
    service, log_repo, client = _build_service(tmp_path / "s")
    rec = _recommendation()
    log_repo.save(_existing_log(_NOW - dt.timedelta(days=1)))

    sent = service.notify_manual_review_required(rec, _alert(rec), _NOW)

    assert sent is False
    assert client.sent == [], "LINE へ送らない"


def test_after_resend_interval_is_sent_again(tmp_path: Path) -> None:
    service, log_repo, client = _build_service(tmp_path / "s")
    rec = _recommendation()
    elapsed = _CONFIG.notification.resend_after_days
    log_repo.save(_existing_log(_NOW - dt.timedelta(days=elapsed)))

    sent = service.notify_manual_review_required(rec, _alert(rec), _NOW)

    assert sent is True
    assert len(client.sent) == 1


def test_resend_interval_uses_the_existing_config_value(tmp_path: Path) -> None:
    """新しい閾値を作らず既存の resend_after_days をそのまま使う。

    境界（N-1 日経過）で抑止され、N 日経過で送られることを両側から固定する。
    """
    threshold = _CONFIG.notification.resend_after_days
    rec = _recommendation()

    service_before, log_before, client_before = _build_service(tmp_path / "before")
    log_before.save(_existing_log(_NOW - dt.timedelta(days=threshold - 1)))
    assert service_before.notify_manual_review_required(rec, _alert(rec), _NOW) is False
    assert client_before.sent == []

    service_after, log_after, client_after = _build_service(tmp_path / "after")
    log_after.save(_existing_log(_NOW - dt.timedelta(days=threshold)))
    assert service_after.notify_manual_review_required(rec, _alert(rec), _NOW) is True
    assert len(client_after.sent) == 1


def test_validation_mode_bypasses_resend_suppression(tmp_path: Path) -> None:
    """VALIDATION は _notification_status_for_send と同じく再送防止のみ無効化する。"""
    service, log_repo, client = _build_service(tmp_path / "s", validation=True)
    rec = _recommendation()
    log_repo.save(_existing_log(_NOW - dt.timedelta(days=1)))

    sent = service.notify_manual_review_required(rec, _alert(rec), _NOW)

    assert sent is True
    assert len(client.sent) == 1


# --- 他種別・旧形式との関係 --------------------------------------------------


def test_other_notification_types_do_not_suppress_manual_review(tmp_path: Path) -> None:
    """同銘柄でも別種別の送信実績は本通知の再送判定に影響しない。"""
    service, log_repo, client = _build_service(tmp_path / "s")
    rec = _recommendation()
    log_repo.save(
        NotificationLog(
            notification_id="other-1",
            notification_type=NotificationType.SELL_SIGNAL,
            stock_code=_STOCK_CODE,
            content_hash="y" * 16,
            sent_at=_NOW - dt.timedelta(days=1),
        )
    )

    assert service.notify_manual_review_required(rec, _alert(rec), _NOW) is True
    assert len(client.sent) == 1


def test_legacy_log_without_scope_fields_is_honoured(tmp_path: Path) -> None:
    """owner / holding_id を持たない旧形式の NotificationLog でも抑止が働く。

    Issue #33 以前に保存されたレコードは owner / holding_id が None である。
    stock-scope の推奨はこれらを読めなければならない。
    """
    service, log_repo, client = _build_service(tmp_path / "s")
    rec = _recommendation()
    legacy = NotificationLog(
        notification_id="legacy-1",
        notification_type=NotificationType.MANUAL_REVIEW_REQUIRED,
        stock_code=_STOCK_CODE,
        content_hash="z" * 16,
        sent_at=_NOW - dt.timedelta(days=1),
    )
    assert legacy.owner is None and legacy.holding_id is None
    log_repo.save(legacy)

    assert service.notify_manual_review_required(rec, _alert(rec), _NOW) is False
    assert client.sent == []


# --- 保存されるログの形 ------------------------------------------------------


def test_saved_log_keeps_type_and_scope(tmp_path: Path) -> None:
    service, log_repo, _client = _build_service(tmp_path / "s")
    rec = _recommendation()

    service.notify_manual_review_required(rec, _alert(rec), _NOW)

    saved = log_repo.list_all()
    assert len(saved) == 1
    assert saved[0].notification_type is NotificationType.MANUAL_REVIEW_REQUIRED
    assert saved[0].stock_code == _STOCK_CODE
    assert saved[0].related_recommendation_id == rec.recommendation_id


def test_no_log_is_written_in_validation_mode(tmp_path: Path) -> None:
    service, log_repo, client = _build_service(tmp_path / "s", validation=True)
    rec = _recommendation()

    service.notify_manual_review_required(rec, _alert(rec), _NOW)

    assert len(client.sent) == 1
    assert log_repo.list_all() == [], "VALIDATION は送信実績を残さない"


@pytest.mark.parametrize("days", [0, 1, 2])
def test_consecutive_batches_within_interval_send_only_once(
    tmp_path: Path, days: int
) -> None:
    """条件が続いても毎バッチ送らない（本 Issue の主眼）。"""
    service, _log_repo, client = _build_service(tmp_path / "s")
    rec = _recommendation()

    service.notify_manual_review_required(rec, _alert(rec), _NOW)
    service.notify_manual_review_required(rec, _alert(rec), _NOW + dt.timedelta(days=days))

    assert len(client.sent) == 1
