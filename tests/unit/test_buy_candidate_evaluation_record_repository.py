"""BuyCandidateEvaluationRecord/BuyCandidateEvaluationRecordRepositoryのテスト
(買い候補サマリー表示改修2026-08)。

将来のLINE詳細理由照会機能に向けた参照用ストアであり、既存のRecommendation/
DecisionSnapshot/NotificationLog/AuditLogTableの代替ではない。ここでは
(1)リポジトリ自体の基本的な入出力、(2)buy_candidates_handler.py側の
fire-and-forgetラッパー(_save_evaluation_record_safely/_update_evaluation_
record_outcome_safely)が保存失敗時も本処理を絶対にブロックしないこと、
(3)EXCLUDED/DATA_INSUFFICIENTを含む全評価対象について判定時点でレコードが
作成されること、を検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.buy_candidate_evaluation_record import (
    BuyCandidateEvaluationRecord,
    build_evaluation_id,
)
from jstock_advisor.domain.entities.enums import BuyAction, CandidateSource, PurchaseCategory
from jstock_advisor.infrastructure.local_repository import (
    buy_candidate_evaluation_record_repository as repo_module,
)
from jstock_advisor.infrastructure.local_repository.buy_candidate_evaluation_record_repository import (  # noqa: E501
    BuyCandidateEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers import buy_candidates_handler as handler_module

_NOW = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)
_CONFIG = load_config()


def _record(
    evaluation_id: str = "batch-1:2914",
    batch_id: str = "batch-1",
    stock_code: str = "2914",
    evaluated_at: dt.datetime = _NOW,
    purchase_category: PurchaseCategory = PurchaseCategory.BUY_CANDIDATE,
) -> BuyCandidateEvaluationRecord:
    return BuyCandidateEvaluationRecord(
        evaluation_id=evaluation_id,
        batch_id=batch_id,
        stock_code=stock_code,
        evaluated_at=evaluated_at,
        rule_version="v1-mvp",
        candidate_source=CandidateSource.WATCHLIST,
        purchase_category=purchase_category,
        final_buy_action=BuyAction.BUY,
        raw_buy_action=BuyAction.BUY,
        recommendation_id=f"rec-{stock_code}",
    )


# --- リポジトリ自体の基本的な入出力 ---------------------------------------------


def test_build_evaluation_id_is_batch_id_colon_stock_code() -> None:
    assert build_evaluation_id("batch-1", "2914") == "batch-1:2914"


def test_get_returns_none_when_missing(tmp_path: Path) -> None:
    repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    assert repo.get("does-not-exist") is None


def test_upsert_then_get_round_trips(tmp_path: Path) -> None:
    repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    repo.upsert(_record())

    fetched = repo.get("batch-1:2914")
    assert fetched is not None
    assert fetched.stock_code == "2914"
    assert fetched.purchase_category == PurchaseCategory.BUY_CANDIDATE


def test_upsert_overwrites_same_evaluation_id(tmp_path: Path) -> None:
    """finalize時のupdateは判定時点と同じevaluation_idへ単純upsertする設計
    (楽観ロック不要)。2回目のupsertが同じ行を更新することを確認する。"""
    repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    repo.upsert(_record())
    updated = _record().model_copy(
        update={"unified_rank": 1, "notification_rank": 1, "send_outcome": "SENT_AND_RECORDED"}
    )
    repo.upsert(updated)

    items = repo.list_by_stock("2914")
    assert len(items) == 1
    assert items[0].unified_rank == 1
    assert items[0].send_outcome == "SENT_AND_RECORDED"


def test_list_by_stock_returns_only_matching_stock_sorted_by_evaluated_at(tmp_path: Path) -> None:
    repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    repo.upsert(_record(evaluation_id="batch-2:2914", batch_id="batch-2", evaluated_at=_NOW))
    repo.upsert(
        _record(
            evaluation_id="batch-1:2914",
            batch_id="batch-1",
            evaluated_at=_NOW - dt.timedelta(days=1),
        )
    )
    repo.upsert(_record(evaluation_id="batch-1:8136", batch_id="batch-1", stock_code="8136"))

    items = repo.list_by_stock("2914")
    assert [item.batch_id for item in items] == ["batch-1", "batch-2"]


def test_list_by_batch_returns_only_matching_batch_sorted_by_stock_code(tmp_path: Path) -> None:
    """LINE UI第二弾「対象確認」機能(2026-08)向け。batch_id-index(GSI)経由の
    Query(ローカルJSON実装はquery_by_index()のfind()フォールバック)で、
    指定batch_idの全レコードのみをstock_code昇順で取得できること。"""
    repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    repo.upsert(_record(evaluation_id="batch-1:8136", batch_id="batch-1", stock_code="8136"))
    repo.upsert(_record(evaluation_id="batch-1:2914", batch_id="batch-1", stock_code="2914"))
    repo.upsert(_record(evaluation_id="batch-2:2914", batch_id="batch-2", stock_code="2914"))

    items = repo.list_by_batch("batch-1")

    assert [item.stock_code for item in items] == ["2914", "8136"]


def test_list_by_batch_returns_empty_list_when_no_match(tmp_path: Path) -> None:
    repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    repo.upsert(_record(evaluation_id="batch-1:2914", batch_id="batch-1"))
    assert repo.list_by_batch("does-not-exist") == []


def test_default_ttl_seconds_is_ninety_days() -> None:
    """既定90日のTTL(DEFAULT_TTL_SECONDS)。BuyCandidateEvaluationRecordRepositoryの
    __init__のデフォルト引数として使われるモジュールレベル定数であり、他の
    ローカルリポジトリ(holdings_snapshot_repository.py等)の_VALIDATION_TTL_SECONDS
    と同じ流儀(モジュールレベル定数)である。"""
    assert repo_module.DEFAULT_TTL_SECONDS == 90 * 24 * 60 * 60


# --- 判定時点(_process_single_candidate)で全評価対象にレコードが作成される ------


def test_process_single_candidate_creates_record_for_excluded_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """EXCLUDED(投資対象スクリーニングで除外)は既存のRecommendation/
    DecisionSnapshotには一切保存されないが、BuyCandidateEvaluationRecordには
    判定時点で必ず記録される(将来の理由照会に備えるための本改修の要点)。"""
    monkeypatch.setattr(handler_module, "build_stock_snapshot", lambda *a, **kw: (object(), None))
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: _NoopAuditService())
    from jstock_advisor.services.buy_signal_service import BuyAnalysisOutcome

    outcome = BuyAnalysisOutcome(
        stock_code="9861",
        recommendation=None,
        screening_passed=False,
        exclusion_reasons=["総合利回りが基準未満"],
        data_error=None,
        buy_action=BuyAction.EXCLUDED,
        ranking_group="excluded",
    )
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)

    class _NoSaveRepo:
        def save(self, *_a: object, **_kw: object) -> None:
            raise AssertionError("EXCLUDEDの場合はRecommendationを保存しないはず")

        def get(self, *_a: object, **_kw: object) -> None:
            return None

    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    fake_service = _FakeNotificationService()

    handler_module._process_single_candidate(
        "9861",
        CandidateSource.WATCHLIST,
        None,
        None,
        "batch-1",
        _NOW,
        object(),
        _CONFIG,
        object(),
        _NoSaveRepo(),
        fake_service,
        handler_module._DEFAULT_EXECUTION_CONTEXT,
        eval_repo,
    )

    record = eval_repo.get(build_evaluation_id("batch-1", "9861"))
    assert record is not None
    assert record.purchase_category == PurchaseCategory.EXCLUDED
    assert record.final_buy_action == BuyAction.EXCLUDED
    assert record.recommendation_id is None


def test_process_single_candidate_creates_record_for_data_insufficient_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DATA_INSUFFICIENT(個別データ取得エラー)も判定時点でレコードが作成される。"""
    monkeypatch.setattr(handler_module, "build_stock_snapshot", lambda *a, **kw: (object(), None))
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: _NoopAuditService())
    monkeypatch.setattr(handler_module.WatchlistService, "get_item", lambda self, code: None)

    class _FakeOutcome:
        data_error = "テストエラー"
        recommendation = None
        buy_action = None
        ranking_group = None

    monkeypatch.setattr(
        handler_module.BuySignalService, "analyze", lambda self, *a, **kw: _FakeOutcome()
    )
    repo = RecommendationRepository(store_dir=tmp_path / "recs")
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path / "eval")
    fake_service = _FakeNotificationService()

    handler_module._process_single_candidate(
        "2914",
        CandidateSource.WATCHLIST,
        None,
        None,
        "batch-1",
        _NOW,
        object(),
        _CONFIG,
        object(),
        repo,
        fake_service,
        handler_module._DEFAULT_EXECUTION_CONTEXT,
        eval_repo,
    )

    record = eval_repo.get(build_evaluation_id("batch-1", "2914"))
    assert record is not None
    assert record.purchase_category == PurchaseCategory.DATA_INSUFFICIENT
    assert record.final_buy_action == BuyAction.DATA_INSUFFICIENT


# --- fire-and-forget: 保存失敗が既存の判定・通知フローを絶対にブロックしない ------


class _NoopAuditService:
    def record(self, *args: object, **kwargs: object) -> None:
        return None


class _FakeNotificationService:
    def notify_data_error(self, *args: object, **kwargs: object) -> bool:
        return False

    def check_data_quality_eligibility(self, recommendation, now, context=None):
        from jstock_advisor.domain.entities.notification_eligibility import (
            NotificationEligibility,
        )

        return NotificationEligibility(eligible=True)

    def check_resend_eligibility(self, recommendation, now):
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

    def notify_buy_candidates_digest(self, winners, now, *, batch_id=None):
        return {r.stock_code: "SENT_AND_RECORDED" for r in winners}

    def notify_batch_summary(self, *args: object, **kwargs: object) -> bool:
        return True


class _RaisingEvaluationRecordRepo:
    """upsert/get双方が常に例外を送出するフェイク(保存障害を模す)。"""

    def upsert(self, record: object) -> None:
        raise RuntimeError("boom")

    def get(self, evaluation_id: str) -> None:
        raise RuntimeError("boom")

    def list_by_stock(self, stock_code: str) -> list[object]:
        raise RuntimeError("boom")


def test_save_evaluation_record_safely_swallows_exception_and_still_returns_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_save_evaluation_record_safely()の保存失敗は、save_decision_snapshot_safely()
    と同じ契約で、_process_single_candidate自体の正常完了・戻り値を一切妨げない。"""
    monkeypatch.setattr(handler_module, "build_stock_snapshot", lambda *a, **kw: (object(), None))
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: _NoopAuditService())
    recommendation_repo = RecommendationRepository(store_dir=tmp_path)

    from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
    from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
    from jstock_advisor.domain.entities.recommendation import Recommendation
    from jstock_advisor.services.buy_signal_service import BuyAnalysisOutcome

    recommendation = Recommendation(
        recommendation_id="rec-1",
        stock_code="2914",
        stock_name="銘柄2914",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            entry=PriceWithRationale(price=Decimal("3500"), rationale="x"),
        ),
        price_at_recommendation=Decimal("4200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
        buy_action=BuyAction.BUY,
        base_buy_action=BuyAction.BUY,
    )
    outcome = BuyAnalysisOutcome(
        stock_code="2914",
        recommendation=recommendation,
        screening_passed=True,
        exclusion_reasons=[],
        data_error=None,
        buy_action=BuyAction.BUY,
        ranking_group="buy_candidate",
    )
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)

    result = handler_module._process_single_candidate(
        "2914",
        CandidateSource.WATCHLIST,
        None,
        None,
        "batch-1",
        _NOW,
        object(),
        _CONFIG,
        object(),
        recommendation_repo,
        _FakeNotificationService(),
        handler_module._DEFAULT_EXECUTION_CONTEXT,
        _RaisingEvaluationRecordRepo(),  # type: ignore[arg-type]
    )

    assert result == {"stock_code": "2914", "recommended": True, "notified": False}
    assert recommendation_repo.get("rec-1") is not None


def test_update_evaluation_record_outcome_safely_swallows_exception(tmp_path: Path) -> None:
    """_update_evaluation_record_outcome_safely()も同様に、finalize処理全体を
    ブロックしない(直接呼び出しでの単体確認)。"""
    # 例外を送出せずに正常終了することそのものがテスト対象。
    handler_module._update_evaluation_record_outcome_safely(
        _RaisingEvaluationRecordRepo(),  # type: ignore[arg-type]
        "batch-1",
        "2914",
        1,
        None,
        False,
        "OUTSIDE_TOP_5",
        "OUTSIDE_TOP_5",
        (),
        None,
    )


def test_update_evaluation_record_outcome_safely_warns_when_record_not_found(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """finalize時点で対応する行が見つからない(判定時点の保存に失敗していた等)
    場合はWARNINGログのみに留め、例外は送出しない。"""
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    with caplog.at_level("WARNING"):
        handler_module._update_evaluation_record_outcome_safely(
            eval_repo, "batch-1", "9999", 1, None, False, "OUTSIDE_TOP_5", "OUTSIDE_TOP_5", (), None
        )
    assert "not found at finalize" in caplog.text


# --- finalize時の更新は判定時点と同じ行を更新する(新しい行を作らない) -----------


def test_update_evaluation_record_outcome_safely_updates_same_row_created_at_judgment_time(
    tmp_path: Path,
) -> None:
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_record())

    handler_module._update_evaluation_record_outcome_safely(
        eval_repo,
        "batch-1",
        "2914",
        1,
        1,
        True,
        None,
        None,
        (),
        "SENT_AND_RECORDED",
    )

    items = eval_repo.list_by_stock("2914")
    assert len(items) == 1  # 新しい行が増えていない(同じevaluation_idの更新)
    record = items[0]
    assert record.unified_rank == 1
    assert record.notification_rank == 1
    assert record.notification_eligible is True
    assert record.send_outcome == "SENT_AND_RECORDED"
    # 判定時点のフィールドは変わらない。
    assert record.purchase_category == PurchaseCategory.BUY_CANDIDATE
    assert record.final_buy_action == BuyAction.BUY
