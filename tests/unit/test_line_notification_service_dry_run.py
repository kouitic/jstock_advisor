"""通知ドライラン機能(2026-08追加)の`LineNotificationService`側の確認。

`_push()`への外部LINE送信抑止の一元化、DRY_RUN時の監査記録(message_text等)、
NotificationLog/本番AuditLogTableを汚さないこと、`notify_buy_candidates_digest()`
の`WOULD_SEND_DRY_RUN`分岐を検証する。同時進行中の買い候補サマリー表示改修の
テスト追加(test_line_notification_service.py)との衝突を避けるため独立ファイル
とした(既存の`test_line_notification_service_near_buy_gate.py`と同じ、自己完結型
フィクスチャの流儀を踏襲する)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import (
    BuyAction,
    ConfidenceLevel,
    ExecutionMode,
    NotificationMode,
    RecommendationType,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.recommendation import Recommendation
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
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.line_notification_service import LineNotificationService

_NOW = dt.datetime(2026, 8, 24, 8, 0, tzinfo=dt.UTC)
_CONFIG = load_config()


class _FakeLineClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)

    def reply_message(self, reply_token: str, text: str) -> None:
        self.sent.append(text)


class _SpyAuditService(AuditService):
    """実物のAuditService.record()をそのまま呼びつつ、呼び出し引数を記録する
    (DRY_RUN監査の内容確認用。is_validation時はrecord()自体が本番AuditLog
    Tableへ保存しないため、保存有無だけでなく渡された内容も直接検証したい)。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[dict[str, Any]] = []

    def record(self, decision_type: str, stock_code, input_values, calculation_formulas,
               output_values, data_sources, rule_version, timestamp, **kwargs):
        self.calls.append(
            {
                "decision_type": decision_type,
                "stock_code": stock_code,
                "output_values": output_values,
                "rule_version": rule_version,
                "timestamp": timestamp,
            }
        )
        return super().record(
            decision_type, stock_code, input_values, calculation_formulas, output_values,
            data_sources, rule_version, timestamp, **kwargs,
        )

    def dry_run_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["decision_type"] == "notification_dry_run"]


def _make_recommendation(
    recommendation_id: str = "rec-1", stock_code: str = "2914"
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name="日本たばこ産業",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        buy_action=BuyAction.BUY,
        entry_buy_price=Decimal("4200"),
        standard_buy_price=Decimal("3359"),
        strong_buy_price=Decimal("2900"),
        buy_prices=BuyPriceLevels(
            tentative=PriceWithRationale(price=Decimal("3600"), rationale="x"),
            standard=PriceWithRationale(price=Decimal("3359"), rationale="x"),
            aggressive=PriceWithRationale(price=Decimal("2900"), rationale="x"),
        ),
        price_at_recommendation=Decimal("4200"),
        dividend_yield_pct_at_recommendation=4.5,
        total_yield_pct_at_recommendation=4.5,
        total_score=60.0,
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


def _build_service(
    tmp_path: Path, execution_context: ExecutionContext
) -> tuple[LineNotificationService, _FakeLineClient, _SpyAuditService, NotificationLogRepository]:
    store_dir = tmp_path / "local_store"
    client = _FakeLineClient()
    notification_log_repo = NotificationLogRepository(store_dir=store_dir)
    audit_service = _SpyAuditService(
        AuditLogRepository(store_dir=store_dir), execution_context=execution_context
    )
    service = LineNotificationService(
        line_client=client,
        notification_log_repository=notification_log_repo,
        recommendation_repository=RecommendationRepository(store_dir=store_dir),
        config=_CONFIG,
        audit_service=audit_service,
        execution_context=execution_context,
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
    )
    return service, client, audit_service, notification_log_repo


def test_normal_mode_sends_via_line_client(tmp_path: Path) -> None:
    service, client, audit_service, log_repo = _build_service(
        tmp_path, ExecutionContext.normal()
    )
    rec = _make_recommendation()

    service.send_recommendation_notification(rec, _NOW)

    assert len(client.sent) == 1
    assert not client.sent[0].startswith("🧪検証｜")
    assert audit_service.dry_run_calls() == []
    from jstock_advisor.domain.entities.enums import NotificationType

    latest = log_repo.latest_by_stock_and_type("2914", NotificationType.DAILY_BUY_CANDIDATES)
    assert latest is not None


def test_validation_send_mode_sends_via_line_client_with_banner(tmp_path: Path) -> None:
    ctx = ExecutionContext(mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.SEND)
    service, client, audit_service, _ = _build_service(tmp_path, ctx)
    rec = _make_recommendation()

    service.send_recommendation_notification(rec, _NOW)

    assert len(client.sent) == 1
    assert client.sent[0].startswith("🧪検証｜")
    assert audit_service.dry_run_calls() == []


def test_validation_notification_mode_unspecified_behaves_like_send(tmp_path: Path) -> None:
    """VALIDATION+notification_mode未指定は明示的なSENDと同一動作。"""
    ctx = ExecutionContext(mode=ExecutionMode.VALIDATION)
    assert ctx.notification_mode == NotificationMode.SEND
    service, client, audit_service, _ = _build_service(tmp_path, ctx)
    rec = _make_recommendation()

    service.send_recommendation_notification(rec, _NOW)

    assert len(client.sent) == 1
    assert client.sent[0].startswith("🧪検証｜")
    assert audit_service.dry_run_calls() == []


def test_validation_dry_run_does_not_call_push_message(tmp_path: Path) -> None:
    ctx = ExecutionContext(
        mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.DRY_RUN
    )
    service, client, _, _ = _build_service(tmp_path, ctx)
    rec = _make_recommendation()

    service.send_recommendation_notification(rec, _NOW)

    assert client.sent == []


def test_validation_dry_run_still_generates_final_message_text(tmp_path: Path) -> None:
    """DRY_RUNでも判定・通知文生成・VALIDATIONバナー付与まではSENDと同じ経路を通り、
    最終文面がAuditへ記録される(message_textは全文であることが分かる名前)。
    """
    ctx = ExecutionContext(
        mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.DRY_RUN
    )
    service, client, audit_service, _ = _build_service(tmp_path, ctx)
    rec = _make_recommendation()

    service.send_recommendation_notification(rec, _NOW)

    calls = audit_service.dry_run_calls()
    assert len(calls) == 1
    message_text = calls[0]["output_values"]["message_text"]
    assert message_text.startswith("🧪検証｜")
    assert "2914" in message_text


def test_validation_dry_run_audit_records_required_fields(tmp_path: Path) -> None:
    ctx = ExecutionContext(
        mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.DRY_RUN
    )
    service, client, audit_service, _ = _build_service(tmp_path, ctx)
    rec = _make_recommendation()

    service.send_recommendation_notification(rec, _NOW)

    call = audit_service.dry_run_calls()[0]
    assert call["stock_code"] == "2914"
    outputs = call["output_values"]
    assert outputs["execution_mode"] == "VALIDATION"
    assert outputs["notification_mode"] == "DRY_RUN"
    assert outputs["would_send"] is True
    assert outputs["related_recommendation_id"] == rec.recommendation_id
    assert outputs["content_hash"]
    assert outputs["notification_type"] is not None


def test_validation_dry_run_does_not_write_notification_log(tmp_path: Path) -> None:
    ctx = ExecutionContext(
        mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.DRY_RUN
    )
    service, client, _, log_repo = _build_service(tmp_path, ctx)
    rec = _make_recommendation()

    service.send_recommendation_notification(rec, _NOW)

    from jstock_advisor.domain.entities.enums import NotificationType

    assert log_repo.latest_by_stock_and_type("2914", NotificationType.DAILY_BUY_CANDIDATES) is None


def test_validation_dry_run_does_not_grow_production_style_audit_log(tmp_path: Path) -> None:
    """DRY_RUNはis_validation=Trueを必ず伴うため、既存のVALIDATION監査隔離
    (AuditService.record()がis_validation時に本番AuditLogTableへ保存しない)を
    そのまま継承する。実物のAuditLogRepository(tmp_path隔離)に何も保存されない
    ことを確認する。
    """
    store_dir = tmp_path / "local_store"
    audit_repo = AuditLogRepository(store_dir=store_dir)
    ctx = ExecutionContext(
        mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.DRY_RUN
    )
    client = _FakeLineClient()
    service = LineNotificationService(
        line_client=client,
        notification_log_repository=NotificationLogRepository(store_dir=store_dir),
        recommendation_repository=RecommendationRepository(store_dir=store_dir),
        config=_CONFIG,
        audit_service=AuditService(audit_repo, execution_context=ctx),
        execution_context=ctx,
        holdings_snapshot_repository=HoldingsSnapshotRepository(store_dir=store_dir),
        daily_notification_priority_repository=DailyNotificationPriorityRepository(
            store_dir=store_dir
        ),
    )
    rec = _make_recommendation()

    service.send_recommendation_notification(rec, _NOW)

    assert audit_repo.list_all() == []


def test_notify_buy_candidates_digest_dry_run_returns_would_send_and_suppresses_push(
    tmp_path: Path,
) -> None:
    ctx = ExecutionContext(
        mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.DRY_RUN
    )
    service, client, audit_service, log_repo = _build_service(tmp_path, ctx)
    winners = [
        _make_recommendation("rec-a", stock_code="1000"),
        _make_recommendation("rec-b", stock_code="1001"),
    ]

    results = service.notify_buy_candidates_digest(winners, _NOW, batch_id="batch-test")

    assert client.sent == []
    assert results == {"1000": "WOULD_SEND_DRY_RUN", "1001": "WOULD_SEND_DRY_RUN"}
    # コードレビュー対応(2026-08、監査二重記録整理): チャンク単位の_push()呼び出し
    # (emit_dry_run_record=False)はもはや記録を残さない。銘柄単位の記録のみが
    # 正本として残り、銘柄数とちょうど一致する(二重計上されない)。
    dry_run_calls = audit_service.dry_run_calls()
    assert len(dry_run_calls) == len(winners)
    recorded_stock_codes = {c["stock_code"] for c in dry_run_calls}
    assert recorded_stock_codes == {"1000", "1001"}
    for call in dry_run_calls:
        assert call["output_values"]["notification_type"] is not None
        assert call["output_values"]["content_hash"]
        assert call["output_values"]["related_recommendation_id"] is not None
        assert call["output_values"]["message_text"]
        assert call["output_values"]["would_send"] is True


def test_notify_buy_candidates_digest_dry_run_multi_chunk_no_double_counting(
    tmp_path: Path,
) -> None:
    """複数チャンクにまたがる大量の候補でも、DRY_RUN監査記録は銘柄数と
    ちょうど一致する(チャンク単位の重複記録が発生しない)。1銘柄分のブロックは
    約42文字、チャンク上限は4500文字のため、150件あれば必ず複数チャンクに
    分割される(107件/チャンク程度)。
    """
    ctx = ExecutionContext(
        mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.DRY_RUN
    )
    service, client, audit_service, _ = _build_service(tmp_path, ctx)
    winner_count = 150
    winners = [
        _make_recommendation(f"rec-{i}", stock_code=f"{2000 + i}") for i in range(winner_count)
    ]

    results = service.notify_buy_candidates_digest(winners, _NOW, batch_id="batch-test")

    assert client.sent == []
    assert len(results) == winner_count
    assert all(outcome == "WOULD_SEND_DRY_RUN" for outcome in results.values())
    dry_run_calls = audit_service.dry_run_calls()
    assert len(dry_run_calls) == winner_count
    assert {c["stock_code"] for c in dry_run_calls} == {
        str(2000 + i) for i in range(winner_count)
    }


def test_notify_buy_candidates_digest_validation_send_unaffected(tmp_path: Path) -> None:
    """VALIDATION+SEND(既存動作)では、notify_buy_candidates_digestは従来どおり
    SENT_VALIDATIONを返し、DRY_RUN分岐は一切発生しない(回帰確認)。
    """
    ctx = ExecutionContext(mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.SEND)
    service, client, audit_service, _ = _build_service(tmp_path, ctx)
    winners = [_make_recommendation("rec-a")]

    results = service.notify_buy_candidates_digest(winners, _NOW, batch_id="batch-test")

    assert len(client.sent) == 1
    assert client.sent[0].startswith("🧪検証｜")
    assert results == {"2914": "SENT_VALIDATION"}
    assert audit_service.dry_run_calls() == []
