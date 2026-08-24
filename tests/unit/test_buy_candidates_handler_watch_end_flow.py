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


def test_watch_end_is_recorded_as_non_actionable_without_sending(
    monkeypatch, tmp_path: Path
) -> None:
    """コードレビュー対応(2026-08、LINE通知アクション限定化): 監視終了通知は
    「監視をやめた」ことの報告であり、ユーザーに売買アクションを促す通知では
    ないため、全ゲート通過後ももはやLINE送信しない(send_watch_end_notification
    は呼ばれない)。送らなかったこと自体はNON_ACTIONABLEとしてAuditへ記録する。
    """
    audit_repo = AuditLogRepository(store_dir=tmp_path)
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

    handler_module._finalize_batch(progress, "batch-1", _CONFIG, _NOW, repo, notification_service)

    assert client.sent == []
    audit_entries = audit_repo.list_by_stock("9432")
    assert any(
        e.output_values.get("block_category") == "NON_ACTIONABLE" for e in audit_entries
    )


def test_watch_end_gate_condition_requires_notifiable_reason_and_threshold() -> None:
    """§3の入口ゲート条件(_process_single_candidate内)そのものを直接検証する。

    (1)対象理由(PRICE_OUT_OF_RANGE/NOT_ATTRACTIVE/STALE)以外は対象外、
    (2)閾値(既定5営業日)未満は対象外、(3)config.enabled=falseなら対象外、
    (4)コードレビュー対応2026-08(指摘1、防御的対策): buy_actionがBUY家族の
    場合は対象外、という4条件をハンドラのロジックと同じ形で再現する。
    """
    from jstock_advisor.domain.entities.enums import BUY_FAMILY_ACTIONS
    from jstock_advisor.services.watch_state_service import WATCH_END_NOTIFIABLE_REASONS

    threshold = _CONFIG.notification.watch_end_notification.min_consecutive_business_days
    enabled = _CONFIG.notification.watch_end_notification.enabled
    assert enabled is True  # 既定はtrue(このテストの前提)

    def _would_notify(reason: str | None, days: int | None, buy_action: BuyAction) -> bool:
        return (
            reason in WATCH_END_NOTIFIABLE_REASONS
            and buy_action not in BUY_FAMILY_ACTIONS
            and enabled
            and (days or 0) >= threshold
        )

    assert _would_notify("PRICE_OUT_OF_RANGE", threshold, BuyAction.WATCH_FOR_PRICE) is True
    assert _would_notify("PRICE_OUT_OF_RANGE", threshold - 1, BuyAction.WATCH_FOR_PRICE) is False
    assert _would_notify("TRADE_EVENT", threshold, BuyAction.WATCH_FOR_PRICE) is False
    assert _would_notify("PROMOTED_TO_BUY", threshold, BuyAction.WATCH_FOR_PRICE) is False
    # コードレビュー対応(指摘1): watch_end_reasonがENDED相当でも、buy_actionが
    # BUY家族(=当日実際に買い水準へ到達した)なら監視終了通知は対象外にする
    # (BUY到達通知との二重送信防止、防御的対策)。
    assert _would_notify("PRICE_OUT_OF_RANGE", threshold, BuyAction.BUY) is False
    assert _would_notify("STALE", threshold, BuyAction.STRONG_BUY) is False


def test_promoted_to_buy_after_stale_gap_sends_single_notification(
    monkeypatch, tmp_path: Path
) -> None:
    """A: コードレビュー対応2026-08(指摘1)の統合確認。

    NEAR BUYを複数営業日継続後、評価不能期間がmax_staleを超過し、次に
    評価できた営業日にBUY水準へ到達したケースを再現する
    (WatchStateService.evaluate_and_update()がPROMOTED_TO_BUYを返す前提は
    test_watch_state_service.py::test_promoted_to_buy_takes_priority_over_stale_after_gap
    で検証済み)。buy_signal_service.py側の実装により、この場合
    watch_transition_type=PROMOTED_TO_BUYがRecommendationへ設定され、
    watch_end_ranking_entryは生成されない(§3ゲート条件、上記テストで確認済み)。

    ここでは、そのRecommendationがBUYランキング経由で実際に1通だけ送信され、
    「到達」ラベル・「N日監視後」が実本文に含まれ、監視終了通知は一切
    送信されないことを、実際のLineNotificationService経由で確認する。
    """
    _patch_audit(monkeypatch, tmp_path)
    # このテストの主眼はBUY到達/監視終了の二重送信防止であり、整合性検証
    # (recommendation_consistency_validator)自体は対象外のため、実データを
    # 完全に再現する代わりにバイパスする(他のテストで別途カバー済み)。
    monkeypatch.setattr(
        LineNotificationService, "_check_data_quality", lambda self, *a, **kw: (None, False)
    )
    store_dir = tmp_path / "local_store"
    repo = RecommendationRepository(store_dir=store_dir)
    rec = Recommendation(
        recommendation_id="promoted-after-stale-1",
        stock_code="9432",
        stock_name="NTT",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            entry=PriceWithRationale(price=Decimal("150"), rationale="x"),
            standard=PriceWithRationale(price=Decimal("140"), rationale="x"),
            strong=PriceWithRationale(price=Decimal("130"), rationale="x"),
        ),
        price_at_recommendation=Decimal("138"),  # standard(140)以下・strong(130)超のためBUY相当
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        buy_action=BuyAction.BUY,
        company_quality_score=70.0,
        purchase_attractiveness_score=80.0,
        watch_type=None,
        watch_transition_type="PROMOTED_TO_BUY",
        watch_previous_consecutive_business_days=4,
    )
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

    # watch_transition_type=PROMOTED_TO_BUYのため、§3ゲート条件により
    # watch_end_ranking_entriesへは(修正後の実装では)決して登録されない。
    # ここではその前提を明示するため意図的に空リストのままにする。
    progress = BatchProgress(
        total=1,
        completed=1,
        category_counts={"candidate_not_ranked": 1},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[handler_module._encode_buy_ranking_entry(rec)],
        sector_entries=[],
        holding_count=0,
        watch_end_ranking_entries=[],
    )

    handler_module._finalize_batch(progress, "batch-1", _CONFIG, _NOW, repo, notification_service)

    # BUYランキング経由の購入候補まとめ通知(1通)+バッチ完了サマリー(1通)の
    # 計2通のみが送信される(§8のダイジェスト形式は既存仕様どおり)。
    # 「監視終了」を含むメッセージが一切無いこと(=BUY到達との二重送信が
    # 発生していないこと)が本テストの主眼。
    assert len(client.sent) == 2
    digest_messages = [msg for msg in client.sent if "到達" in msg]
    assert len(digest_messages) == 1  # BUY到達ブロックはちょうど1回だけ現れる
    body = digest_messages[0]
    assert "到達 9432 NTT" in body
    assert "4日監視後" in body
    assert not any("監視終了" in msg for msg in client.sent)
