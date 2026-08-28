"""通知ドライラン機能(2026-08追加)の`_finalize_batch()`側の集計確認。

`notify_buy_candidates_digest()`が"WOULD_SEND_DRY_RUN"を返した銘柄が、
「通知済み」(sent)にも「送信失敗」(send_failed)にも計上されず、独立した
`dry_run_would_send`区分としてのみ集計されることを確認する。既存の通知対象
選定・ランキング・上限判定(ゲート評価)自体は本テスト対象外(この機能追加では
一切変更していない)。

この機能追加(_finalize_batch()内の送信結果分岐へのWOULD_SEND_DRY_RUN追加)は
`tests/unit/test_buy_candidates_handler.py`の既存フィクスチャと同じパターンを
踏襲しつつ、同ファイルへの同時編集(買い候補サマリー表示改修のテスト追加)との
衝突を避けるため独立ファイルとした。

コードレビュー対応(2026-08、コミットc570264への指摘)により以下を追加確認する。
(1) WOULD_SEND_DRY_RUNの監査記録(unified_buy_candidate_notification_outcome)
について、notification_eligibility(通知条件を通過したか)はeligible=Trueの
まま維持しつつ、actual send outcome(実際に外部送信したか)を表す
notification_statusフィールドへ"SENT"ではなく"WOULD_SEND_DRY_RUN"を記録し、
「条件は満たしたが実送信はしていない」ことを監査上も区別できることを確認する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import BuyAction, ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers import buy_candidates_handler as handler_module

_NOW = dt.datetime(2026, 8, 24, 7, 0, tzinfo=dt.UTC)
_CONFIG = load_config()


def _make_recommendation(
    stock_code: str,
    recommendation_id: str,
    *,
    purchase_attractiveness_score: float = 50.0,
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            entry=PriceWithRationale(price=Decimal("3500"), rationale="x"),
            standard=PriceWithRationale(price=Decimal("3300"), rationale="x"),
            strong=PriceWithRationale(price=Decimal("3100"), rationale="x"),
        ),
        price_at_recommendation=Decimal("4200"),
        total_score=60.0,
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
        buy_action=BuyAction.BUY,
        base_buy_action=BuyAction.BUY,
        company_quality_score=60.0,
        purchase_attractiveness_score=purchase_attractiveness_score,
    )


class _NoopAuditService:
    """`_finalize_batch()`が既定で構築するAuditService()は本番隣接のローカル
    JSONストア(data/local_store/audit_log.json)へ書き込むため、他テスト
    プロセスとの並行実行時にファイルロック競合を起こしうる(既存の
    test_buy_candidates_handler.py側の`_patch_audit`と同じ対策)。本テストの
    関心はfinalize集計そのものであり監査記録内容ではないため無害化する。
    """

    def record(self, *args: object, **kwargs: object) -> None:
        return None


def _patch_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: _NoopAuditService())


class _RecordingAuditService:
    """AuditService.record()の呼び出し内容(decision_type/output_values等)を
    そのまま記録するフェイク(コードレビュー対応2026-08、監査内容そのものの
    確認用。_NoopAuditServiceは内容を破棄するため使えない)。
    """

    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def record(
        self,
        decision_type: str,
        stock_code: str | None = None,
        input_values: dict[str, object] | None = None,
        calculation_formulas: dict[str, object] | None = None,
        output_values: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        self.records.append(
            {
                "decision_type": decision_type,
                "stock_code": stock_code,
                "output_values": output_values or {},
            }
        )

    def records_by_type(self, decision_type: str) -> list[dict[str, object]]:
        return [r for r in self.records if r["decision_type"] == decision_type]


def _patch_recording_audit(monkeypatch: pytest.MonkeyPatch) -> _RecordingAuditService:
    recorder = _RecordingAuditService()
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: recorder)
    return recorder


def _progress(ranking_entries: list[str], total: int, category_counts: dict[str, int]):
    return handler_module.BatchProgress(
        total=total,
        completed=total,
        category_counts=category_counts,
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=ranking_entries,
        sector_entries=[],
        holding_count=0,
    )


class _FakeNotificationServiceForDryRun:
    """`notify_buy_candidates_digest()`の戻り値を任意に指定できる最小フェイク
    (通知ドライラン機能のfinalize集計確認専用)。ゲート評価(データ品質/売買
    クールダウン/cross-pipeline/再送抑止)は常に許可を返す——本テストの関心は
    ゲート評価そのものではなく、ゲート通過後の送信結果カウントの分岐先。
    """

    def __init__(self, send_result: dict[str, str]) -> None:
        self._send_result = send_result
        self.digest_calls: list[list[Recommendation]] = []
        self.batch_summary_calls: list[dict[str, object]] = []

    def check_data_quality_eligibility(self, recommendation, now, context=None):
        from jstock_advisor.domain.entities.notification_eligibility import (
            NotificationEligibility,
        )

        return NotificationEligibility(eligible=True)

    def check_trade_cooldown_eligibility(self, recommendation, now):
        from jstock_advisor.domain.entities.notification_eligibility import (
            NotificationEligibility,
        )

        return NotificationEligibility(eligible=True)

    def check_cross_pipeline_priority_eligibility(self, recommendation, now):
        from jstock_advisor.domain.entities.notification_eligibility import (
            NotificationEligibility,
        )

        return NotificationEligibility(eligible=True)

    def check_resend_eligibility(self, recommendation, now):
        from jstock_advisor.domain.entities.notification_eligibility import (
            NotificationEligibility,
        )

        return NotificationEligibility(eligible=True)

    def notify_buy_candidates_digest(
        self, winners: list[Recommendation], now: dt.datetime, *, batch_id: str | None = None
    ) -> dict[str, str]:
        self.digest_calls.append(list(winners))
        return {r.stock_code: self._send_result.get(r.stock_code, "SEND_FAILED") for r in winners}

    def notify_batch_summary(
        self,
        process_name,
        total,
        category_counts,
        now,
        data_insufficient_stock_codes=None,
        failed_stock_codes=None,
        buy_candidates_sent_count=None,
        near_buy_sent_count=None,
        send_empty_summary=True,
        purchase_judgment_counts=None,
        notification_result_counts=None,
        **_kwargs,
    ) -> bool:
        self.batch_summary_calls.append(
            {
                "purchase_judgment_counts": (
                    dict(purchase_judgment_counts) if purchase_judgment_counts is not None else None
                ),
                "notification_result_counts": (
                    dict(notification_result_counts)
                    if notification_result_counts is not None
                    else None
                ),
            }
        )
        return True


def test_would_send_dry_run_not_counted_as_sent_nor_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """全件がWOULD_SEND_DRY_RUNの場合、sent=0・send_failed=0、独立の
    dry_run_would_send=件数として計上される(要求仕様の例と同じ形)。
    """
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    recs = [_make_recommendation(f"{1000 + i}", f"rec-{i}") for i in range(5)]
    for rec in recs:
        repo.save(rec)
    ranking_entries = [handler_module._encode_buy_ranking_entry(rec) for rec in recs]

    send_result = {rec.stock_code: "WOULD_SEND_DRY_RUN" for rec in recs}
    fake_service = _FakeNotificationServiceForDryRun(send_result)
    progress = _progress(ranking_entries, total=5, category_counts={"candidate_not_ranked": 5})

    handler_module._finalize_batch(progress, "batch-dry-run-1", _CONFIG, _NOW, repo, fake_service)

    counts = fake_service.batch_summary_calls[0]["notification_result_counts"]
    assert counts["sent"] == 0
    assert counts["send_failed"] == 0
    assert counts["dry_run_would_send"] == 5
    # 買い候補判定の総数(purchase_judgment_counts)自体は通知結果に影響されない。
    pj = fake_service.batch_summary_calls[0]["purchase_judgment_counts"]
    assert pj["buy_candidate"] == 5


def test_would_send_dry_run_mixed_with_sent_and_failed_is_independent_bucket(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """通知済み・送信失敗・DRY_RUN予定が混在する場合でも、それぞれ独立に
    集計される(いずれかへ混ぜて計上しない)。
    """
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    recs = [_make_recommendation(f"{2000 + i}", f"rec-{i}") for i in range(3)]
    for rec in recs:
        repo.save(rec)
    ranking_entries = [handler_module._encode_buy_ranking_entry(rec) for rec in recs]

    send_result = {
        recs[0].stock_code: "SENT_AND_RECORDED",
        recs[1].stock_code: "WOULD_SEND_DRY_RUN",
        recs[2].stock_code: "SEND_FAILED",
    }
    fake_service = _FakeNotificationServiceForDryRun(send_result)
    progress = _progress(ranking_entries, total=3, category_counts={"candidate_not_ranked": 3})

    handler_module._finalize_batch(progress, "batch-dry-run-2", _CONFIG, _NOW, repo, fake_service)

    counts = fake_service.batch_summary_calls[0]["notification_result_counts"]
    assert counts["sent"] == 1
    assert counts["send_failed"] == 1
    assert counts["dry_run_would_send"] == 1
    assert counts["sent"] + counts["send_failed"] + counts["dry_run_would_send"] == 3


def test_would_send_dry_run_does_not_raise_log_failed_runtime_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """WOULD_SEND_DRY_RUNはSENT_LOG_FAILEDと異なり、運用検知用のRuntimeError
    (log_failedリスト)を一切発火させない。
    """
    _patch_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    rec = _make_recommendation("3001", "rec-only")
    repo.save(rec)
    ranking_entries = [handler_module._encode_buy_ranking_entry(rec)]

    fake_service = _FakeNotificationServiceForDryRun({rec.stock_code: "WOULD_SEND_DRY_RUN"})
    progress = _progress(ranking_entries, total=1, category_counts={"candidate_not_ranked": 1})

    # 例外を送出しないことそのものが確認事項。
    handler_module._finalize_batch(progress, "batch-dry-run-3", _CONFIG, _NOW, repo, fake_service)


def test_would_send_dry_run_audit_records_would_send_not_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """コードレビュー対応(2026-08、コミットc570264への指摘1)。WOULD_SEND_DRY_RUN
    の監査記録(unified_buy_candidate_notification_outcome)は、通知条件を通過
    したこと(eligible=True、block_category/block_reasonがNone)は維持しつつ、
    実際の送信結果(notification_status)へ"SENT"ではなく"WOULD_SEND_DRY_RUN"を
    記録する。将来のLINEからの理由照会機能で「実送信済み」と誤認されないための
    区別。
    """
    recorder = _patch_recording_audit(monkeypatch)
    repo = RecommendationRepository(store_dir=tmp_path)
    rec = _make_recommendation("4001", "rec-dry-audit")
    repo.save(rec)
    ranking_entries = [handler_module._encode_buy_ranking_entry(rec)]

    fake_service = _FakeNotificationServiceForDryRun({rec.stock_code: "WOULD_SEND_DRY_RUN"})
    progress = _progress(ranking_entries, total=1, category_counts={"candidate_not_ranked": 1})

    handler_module._finalize_batch(
        progress, "batch-dry-run-audit", _CONFIG, _NOW, repo, fake_service
    )

    outcome_records = recorder.records_by_type("unified_buy_candidate_notification_outcome")
    assert len(outcome_records) == 1
    output_values = outcome_records[0]["output_values"]
    assert output_values["notification_status"] == "WOULD_SEND_DRY_RUN"
    assert output_values["notification_status"] != "SENT"
    # eligible=True(通知条件は通過)であることは、block_category/block_reasonが
    # いずれもNoneであることから確認する(_record_notification_outcome_auditは
    # NotificationEligibility.eligibleそのものを別フィールドとしては保存しない、
    # 既存の記録形式)。
    assert output_values["block_category"] is None
    assert output_values["block_reason"] is None
