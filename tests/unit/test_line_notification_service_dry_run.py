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
from jstock_advisor.infrastructure.local_repository.notification_claim_repository import (
    NotificationClaimRepository,
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
    tmp_path: Path,
    execution_context: ExecutionContext,
    *,
    claim_repository: NotificationClaimRepository | None = None,
) -> tuple[LineNotificationService, _FakeLineClient, _SpyAuditService, NotificationLogRepository]:
    """Issue #109: `claim_repository` は既定 None(従来どおり)。

    渡した場合のみ `_claims_enabled()` の判定が「repo あり AND not is_validation」の
    AND 条件として意味を持つ。既定を変えないのは、既存 test の期待値
    (claim を使わない前提)を 1 つも動かさないためである。
    """
    store_dir = tmp_path / "local_store"
    client = _FakeLineClient()
    notification_log_repo = NotificationLogRepository(store_dir=store_dir)
    audit_service = _SpyAuditService(
        AuditLogRepository(store_dir=store_dir), execution_context=execution_context
    )
    service = LineNotificationService(
        line_client=client,
        notification_log_repository=notification_log_repo,
        notification_claim_repository=claim_repository,
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

    latest = log_repo.latest_by_stock_and_type("2914", NotificationType.DAILY_BUY_CANDIDATES).latest
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

    assert (
        log_repo.latest_by_stock_and_type(
            "2914", NotificationType.DAILY_BUY_CANDIDATES
        ).latest
        is None
    )


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


# --- Issue #109: 開示速報(notify_disclosure_risk)の VALIDATION 抑止 -----------------
# 本番の VALIDATION 手動起動(2026-09-10)は alerts=0 で notify_disclosure_risk へ
# 一度も到達せず、「LINE 0 / NotificationLog 0 / NotificationClaim 0」が
# ★ **抑止の結果ではなく 0 件だったから 0** という状態だった(#109 の snapshot)。
# ここでは §7.4.2 (a) として「**送信境界まで到達したうえで抑止される**」ことを固定する。
# 到達の証拠は戻り値 True と DRY_RUN 監査記録(最終本文)であり、
# 「呼ばれなかったから 0」では通らない形にしてある。
#
# ★ 実在の銘柄コード・銘柄名は使用しない(架空値のみ)。

_DISCLOSURE_STOCK_CODE = "0000"
_DISCLOSED_AT = _NOW - dt.timedelta(hours=1)
# 既存 test と同じ literal を使う(src の `_VALIDATION_BANNER` を import すると
# 「実装と同じ値を実装から取ってくる」形になり、banner が変わったことを検出できない)。
_VALIDATION_BANNER_PREFIX = "🧪検証｜"


def _notify_disclosure(service: LineNotificationService) -> bool:
    return service.notify_disclosure_risk(
        stock_code=_DISCLOSURE_STOCK_CODE,
        disclosure_title="特別損失の計上に関するお知らせ",
        disclosure_summary="特別損失を計上します。",
        matched_keywords=["特別損失"],
        published_at=_DISCLOSED_AT,
        now=_NOW,
        stock_name="銘柄 X",
    )


def _persisted_audit_count(tmp_path: Path) -> int:
    """本番 AuditLogTable 相当への**永続化件数**。

    `_SpyAuditService` は `record()` の呼び出しを記録するだけで保存有無を表さない。
    VALIDATION では `AuditService.record()` が save より前に return するため、
    「呼ばれたが保存されていない」を区別するには store を直接読む必要がある。
    """
    return len(AuditLogRepository(store_dir=tmp_path / "local_store").list_all())


def test_disclosure_risk_normal_reaches_send_boundary_and_persists(tmp_path: Path) -> None:
    """対照(NORMAL): 開示速報が実際に送信され、log / claim / audit が残ること。

    ★ この 1 本があることで、下の VALIDATION 側の 0 件が
      「そもそも到達しないから 0」ではないと言える。
    """
    ctx = ExecutionContext(mode=ExecutionMode.NORMAL, notification_mode=NotificationMode.SEND)
    claim_repo = NotificationClaimRepository(store_dir=tmp_path / "local_store")
    service, client, audit_service, log_repo = _build_service(
        tmp_path, ctx, claim_repository=claim_repo
    )

    assert _notify_disclosure(service) is True

    assert len(client.sent) == 1, "NORMAL では実 push が起きること"
    assert not client.sent[0].startswith(_VALIDATION_BANNER_PREFIX)
    assert len(log_repo.list_all()) == 1, "NORMAL では NotificationLog が残ること"
    assert len(claim_repo.list_all()) == 1, "NORMAL では NotificationClaim が残ること"
    assert audit_service.dry_run_calls() == [], "NORMAL では DRY_RUN 記録を出さない"
    # ★ 実測: 本経路は NORMAL では監査を 1 件も書かない。DRY_RUN 記録は
    #   `_push()` の DRY_RUN 分岐でのみ生成されるためである。
    #   したがって「VALIDATION で監査 0 件」だけを見ても抑止の証拠にならず、
    #   ★ **生成された DRY_RUN 記録が永続化されていない**ことまで見る必要がある
    #   (下の test_..._suppresses_every_production_write で対にして固定する)。
    assert _persisted_audit_count(tmp_path) == 0, "本経路は NORMAL でも監査を永続化しない"


def test_disclosure_risk_validation_dry_run_suppresses_every_production_write(
    tmp_path: Path,
) -> None:
    """★ VALIDATION+DRY_RUN で、本番side effect が **4 つとも** 起きないこと。

    抑止点(実測)
      1 `_push()` の `is_dry_run`            -> 外部 LINE push
      2 `notify_disclosure_risk()` の
        `if not ...is_validation`             -> NotificationLog の保存
      3 `_claims_enabled()`                   -> NotificationClaim の取得・保存
      4 `AuditService.record()` の
        `if ...is_validation`                 -> DRY_RUN 監査記録の**永続化**
    ★ 4 は #109 の snapshot が挙げた 3 点に含まれていないが、同じ経路で本番
      AuditLog へ書きうるため合わせて固定する。★ ただし 4 は
      「**生成されたのに永続化されない**」という対で見ないと意味がない
      (本経路は NORMAL では監査を 1 件も書かないため、0 件だけでは抑止の証拠に
      ならない)。そのため下で `dry_run_calls()` が 1 件あることも同時に assert する。
    """
    ctx = ExecutionContext(
        mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.DRY_RUN
    )
    claim_repo = NotificationClaimRepository(store_dir=tmp_path / "local_store")
    service, client, audit_service, log_repo = _build_service(
        tmp_path, ctx, claim_repository=claim_repo
    )

    # ★ 戻り値 True = 抑止で早期 return したのではなく、送信境界まで到達している。
    assert _notify_disclosure(service) is True

    assert client.sent == [], "1: 外部 LINE push が発生してはならない"
    assert log_repo.list_all() == [], "2: NotificationLog を保存してはならない"
    assert claim_repo.list_all() == [], "3: NotificationClaim を保存してはならない"
    assert len(audit_service.dry_run_calls()) == 1, "4: DRY_RUN 監査記録は**生成される**"
    assert _persisted_audit_count(tmp_path) == 0, "4: しかし**永続化されない**"


def test_disclosure_risk_validation_dry_run_still_reaches_the_send_boundary(
    tmp_path: Path,
) -> None:
    """★ 抑止が「到達したうえで止めた」ことの直接証拠を固定する。

    DRY_RUN 監査には**最終本文**(VALIDATION banner 付与後)が載る。これが 1 件ある
    ことは、判定・本文生成・banner 付与まで NORMAL と同じ経路を通り、
    ★ 外部 push の直前で止まったことを意味する。
    ★ 本番で観測できなかったのはまさにこの点(alerts=0 で未到達だった)。
    """
    ctx = ExecutionContext(
        mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.DRY_RUN
    )
    service, _client, audit_service, _log_repo = _build_service(tmp_path, ctx)

    assert _notify_disclosure(service) is True

    dry_run_calls = audit_service.dry_run_calls()
    assert len(dry_run_calls) == 1, "DRY_RUN 記録が 1 件だけ出ること"
    message_text = dry_run_calls[0]["output_values"]["message_text"]
    assert message_text.startswith(_VALIDATION_BANNER_PREFIX), "banner が付与されていること"
    assert _DISCLOSURE_STOCK_CODE in message_text, "最終本文が開示速報のものであること"
    assert "特別損失" in message_text, "検出キーワードが本文に載っていること"


def test_disclosure_risk_validation_send_pushes_with_banner_but_records_nothing(
    tmp_path: Path,
) -> None:
    """VALIDATION+SEND は既存の共通契約どおり **送信される**(disclosure だけ独自にしない)。

    `is_dry_run` は `is_validation AND notification_mode == DRY_RUN` の AND 条件で
    あるため、SEND では push が起きる。一方 NotificationLog / NotificationClaim は
    `is_validation` 単独のガードなので **保存されない**。この非対称は意図的であり、
    片方だけ見て「抑止できていない」と誤読しないために固定する。
    """
    ctx = ExecutionContext(mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.SEND)
    claim_repo = NotificationClaimRepository(store_dir=tmp_path / "local_store")
    service, client, audit_service, log_repo = _build_service(
        tmp_path, ctx, claim_repository=claim_repo
    )

    assert _notify_disclosure(service) is True

    assert len(client.sent) == 1, "VALIDATION+SEND では push される"
    assert client.sent[0].startswith(_VALIDATION_BANNER_PREFIX)
    assert log_repo.list_all() == [], "VALIDATION では NotificationLog を保存しない"
    assert claim_repo.list_all() == [], "VALIDATION では claim を使わない"
    assert _persisted_audit_count(tmp_path) == 0, "VALIDATION では監査を永続化しない"
    assert audit_service.dry_run_calls() == [], "SEND では DRY_RUN 記録を出さない"
