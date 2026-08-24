"""対象確認(直近NORMAL完了BUY候補batch、カテゴリー別一覧、LINE UI第二弾、
読み取り専用、2026-08)のテスト。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from jstock_advisor.domain.entities.buy_candidate_batch_pointer import (
    LatestBuyCandidateBatchPointer,
)
from jstock_advisor.domain.entities.buy_candidate_evaluation_record import (
    BuyCandidateEvaluationRecord,
)
from jstock_advisor.domain.entities.enums import CandidateSource, PurchaseCategory
from jstock_advisor.infrastructure.local_repository.buy_candidate_evaluation_record_repository import (  # noqa: E501
    BuyCandidateEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.latest_buy_candidate_batch_pointer_repository import (  # noqa: E501
    LatestBuyCandidateBatchPointerRepository,
)
from jstock_advisor.services.buy_candidate_target_view_service import (
    CATEGORY_DISPLAY_LABELS,
    BuyCandidateTargetViewService,
    is_valid_category_label,
)
from jstock_advisor.services.latest_batch_records_provider import STILL_PROPAGATING_MESSAGE

_NOW = dt.datetime(2026, 8, 24, 7, 0, tzinfo=dt.UTC)


def _eval_record(
    batch_id: str,
    stock_code: str,
    purchase_category: PurchaseCategory,
    unified_rank: int | None = None,
) -> BuyCandidateEvaluationRecord:
    return BuyCandidateEvaluationRecord(
        evaluation_id=f"{batch_id}:{stock_code}",
        batch_id=batch_id,
        stock_code=stock_code,
        evaluated_at=_NOW,
        rule_version="v1-mvp",
        candidate_source=CandidateSource.WATCHLIST,
        purchase_category=purchase_category,
        unified_rank=unified_rank,
    )


def _service(store_dir: Path) -> BuyCandidateTargetViewService:
    return BuyCandidateTargetViewService(
        evaluation_record_repository=BuyCandidateEvaluationRecordRepository(store_dir=store_dir),
        latest_batch_pointer_repository=LatestBuyCandidateBatchPointerRepository(
            store_dir=store_dir
        ),
        display_name_resolver=None,
    )


def _set_pointer(store_dir: Path, batch_id: str, total: int) -> None:
    LatestBuyCandidateBatchPointerRepository(store_dir=store_dir).update_latest_completed(
        LatestBuyCandidateBatchPointer(
            latest_completed_batch_id=batch_id, completed_at=_NOW, total_candidates=total
        )
    )


def test_seven_category_labels_confirmed() -> None:
    assert CATEGORY_DISPLAY_LABELS == (
        "買い候補",
        "買い間近",
        "買い待ち",
        "買い対象外",
        "要確認",
        "データ不足",
        "処理失敗",
    )
    for label in CATEGORY_DISPLAY_LABELS:
        assert is_valid_category_label(label)
    assert is_valid_category_label("謎のカテゴリ") is False


def test_no_completed_batch_returns_empty_list(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service.build_lines("買い候補") == []


def test_unknown_category_label_returns_empty_list(tmp_path: Path) -> None:
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "9432", PurchaseCategory.BUY_CANDIDATE))
    _set_pointer(tmp_path, "batch-1", 1)
    service = _service(tmp_path)
    assert service.build_lines("謎のカテゴリ") == []


def test_buy_candidate_category_filters_correctly(tmp_path: Path) -> None:
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "9432", PurchaseCategory.BUY_CANDIDATE))
    eval_repo.upsert(_eval_record("batch-1", "8306", PurchaseCategory.WATCH_FOR_PRICE))
    _set_pointer(tmp_path, "batch-1", 2)
    service = _service(tmp_path)

    lines = service.build_lines("買い候補")

    assert lines == ["9432（9432）"]


def test_watch_wait_category_aggregates_watch_for_price_and_watch_before_earnings(
    tmp_path: Path,
) -> None:
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "1111", PurchaseCategory.WATCH_FOR_PRICE))
    eval_repo.upsert(_eval_record("batch-1", "2222", PurchaseCategory.WATCH_BEFORE_EARNINGS))
    eval_repo.upsert(_eval_record("batch-1", "3333", PurchaseCategory.BUY_CANDIDATE))
    _set_pointer(tmp_path, "batch-1", 3)
    service = _service(tmp_path)

    lines = service.build_lines("買い待ち")

    assert {line[:4] for line in lines} == {"1111", "2222"}


def test_not_attractive_category_aggregates_not_attractive_and_excluded(tmp_path: Path) -> None:
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "1111", PurchaseCategory.NOT_ATTRACTIVE))
    eval_repo.upsert(_eval_record("batch-1", "2222", PurchaseCategory.EXCLUDED))
    eval_repo.upsert(_eval_record("batch-1", "3333", PurchaseCategory.MANUAL_REVIEW))
    _set_pointer(tmp_path, "batch-1", 3)
    service = _service(tmp_path)

    lines = service.build_lines("買い対象外")

    assert {line[:4] for line in lines} == {"1111", "2222"}


def test_near_buy_manual_review_data_insufficient_failed_are_each_independent(
    tmp_path: Path,
) -> None:
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "1111", PurchaseCategory.NEAR_BUY))
    eval_repo.upsert(_eval_record("batch-1", "2222", PurchaseCategory.MANUAL_REVIEW))
    eval_repo.upsert(_eval_record("batch-1", "3333", PurchaseCategory.DATA_INSUFFICIENT))
    eval_repo.upsert(_eval_record("batch-1", "4444", PurchaseCategory.FAILED))
    _set_pointer(tmp_path, "batch-1", 4)
    service = _service(tmp_path)

    assert [line[:4] for line in service.build_lines("買い間近")] == ["1111"]
    assert [line[:4] for line in service.build_lines("要確認")] == ["2222"]
    assert [line[:4] for line in service.build_lines("データ不足")] == ["3333"]
    assert [line[:4] for line in service.build_lines("処理失敗")] == ["4444"]


def test_zero_matches_for_category_returns_empty_list(tmp_path: Path) -> None:
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "9432", PurchaseCategory.BUY_CANDIDATE))
    _set_pointer(tmp_path, "batch-1", 1)
    service = _service(tmp_path)

    assert service.build_lines("要確認") == []


def test_multiple_matches_sorted_by_unified_rank_ascending(tmp_path: Path) -> None:
    category = PurchaseCategory.BUY_CANDIDATE
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "3333", category, unified_rank=3))
    eval_repo.upsert(_eval_record("batch-1", "1111", category, unified_rank=1))
    eval_repo.upsert(_eval_record("batch-1", "2222", category, unified_rank=2))
    _set_pointer(tmp_path, "batch-1", 3)
    service = _service(tmp_path)

    lines = service.build_lines("買い候補")

    assert [line[:4] for line in lines] == ["1111", "2222", "3333"]


def test_unranked_records_sort_after_ranked_ones_by_stock_code(tmp_path: Path) -> None:
    """ランクを持たない銘柄(買い対象外/要確認/データ不足/処理失敗等)は
    stock_code順で安定的に末尾へ。"""
    category = PurchaseCategory.BUY_CANDIDATE
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "9999", category, unified_rank=None))
    eval_repo.upsert(_eval_record("batch-1", "1111", category, unified_rank=2))
    eval_repo.upsert(_eval_record("batch-1", "5555", category, unified_rank=None))
    _set_pointer(tmp_path, "batch-1", 3)
    service = _service(tmp_path)

    lines = service.build_lines("買い候補")

    assert [line[:4] for line in lines] == ["1111", "5555", "9999"]


def test_old_batch_records_are_not_mixed_in(tmp_path: Path) -> None:
    """古いbatchが混ざらないこと(latest_completed_batch_id以外は対象外)。"""
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-old", "9999", PurchaseCategory.BUY_CANDIDATE))
    eval_repo.upsert(_eval_record("batch-new", "1111", PurchaseCategory.BUY_CANDIDATE))
    _set_pointer(tmp_path, "batch-new", 1)
    service = _service(tmp_path)

    lines = service.build_lines("買い候補")

    assert lines == ["1111（1111）"]


def test_still_propagating_returns_message_string(tmp_path: Path) -> None:
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_eval_record("batch-1", "9432", PurchaseCategory.BUY_CANDIDATE))
    _set_pointer(tmp_path, "batch-1", 5)  # 実際のレコード数(1)と不一致=反映待ち
    service = _service(tmp_path)

    assert service.build_lines("買い候補") == STILL_PROPAGATING_MESSAGE


def test_does_not_expose_write_methods(tmp_path: Path) -> None:
    """読み取り専用機能としての安全性(19節)。"""
    service = _service(tmp_path)
    assert not hasattr(service, "upsert")
    assert not hasattr(service, "delete")
