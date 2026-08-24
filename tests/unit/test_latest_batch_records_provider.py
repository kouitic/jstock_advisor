"""直近NORMAL完了batch評価レコード取得の共通ヘルパー(LINE UI第二弾、2026-08)。

GSI結果整合性対策(total_candidates比較+1回の短い再試行)を、EvaluationRecord
保存欠損とは独立した懸念として検証する(sleepは注入してテストを高速化する)。
"""

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
from jstock_advisor.services.latest_batch_records_provider import (
    LATEST_BATCH_STILL_PROPAGATING,
    fetch_latest_normal_batch_records,
)

_NOW = dt.datetime(2026, 8, 24, 7, 0, tzinfo=dt.UTC)


def _record(batch_id: str, stock_code: str) -> BuyCandidateEvaluationRecord:
    return BuyCandidateEvaluationRecord(
        evaluation_id=f"{batch_id}:{stock_code}",
        batch_id=batch_id,
        stock_code=stock_code,
        evaluated_at=_NOW,
        rule_version="v1-mvp",
        candidate_source=CandidateSource.WATCHLIST,
        purchase_category=PurchaseCategory.BUY_CANDIDATE,
    )


def test_returns_none_when_no_pointer_ever_set(tmp_path: Path) -> None:
    pointer_repo = LatestBuyCandidateBatchPointerRepository(store_dir=tmp_path)
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)

    result = fetch_latest_normal_batch_records(pointer_repo, eval_repo, sleep=lambda s: None)

    assert result is None


def test_returns_records_when_count_matches_total(tmp_path: Path) -> None:
    pointer_repo = LatestBuyCandidateBatchPointerRepository(store_dir=tmp_path)
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_record("batch-1", "1111"))
    eval_repo.upsert(_record("batch-1", "2222"))
    pointer_repo.update_latest_completed(
        LatestBuyCandidateBatchPointer(
            latest_completed_batch_id="batch-1", completed_at=_NOW, total_candidates=2
        )
    )

    result = fetch_latest_normal_batch_records(pointer_repo, eval_repo, sleep=lambda s: None)

    assert result is not None
    assert result.batch_id == "batch-1"
    assert set(result.records_by_stock_code) == {"1111", "2222"}


def test_retries_once_then_succeeds_if_second_read_matches(tmp_path: Path) -> None:
    """1回目のQueryで件数が不足していても、再試行時に一致すれば成功として扱う
    (GSI反映遅延を模した遅延書き込み)。"""
    pointer_repo = LatestBuyCandidateBatchPointerRepository(store_dir=tmp_path)
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_record("batch-1", "1111"))
    pointer_repo.update_latest_completed(
        LatestBuyCandidateBatchPointer(
            latest_completed_batch_id="batch-1", completed_at=_NOW, total_candidates=2
        )
    )

    sleep_calls: list[float] = []

    def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        eval_repo.upsert(_record("batch-1", "2222"))  # 遅延反映を模す

    result = fetch_latest_normal_batch_records(pointer_repo, eval_repo, sleep=_fake_sleep)

    assert len(sleep_calls) == 1
    assert result is not None
    assert set(result.records_by_stock_code) == {"1111", "2222"}


def test_still_propagating_when_count_never_matches_after_retry(tmp_path: Path) -> None:
    pointer_repo = LatestBuyCandidateBatchPointerRepository(store_dir=tmp_path)
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_record("batch-1", "1111"))
    pointer_repo.update_latest_completed(
        LatestBuyCandidateBatchPointer(
            latest_completed_batch_id="batch-1", completed_at=_NOW, total_candidates=5
        )
    )

    sleep_calls: list[float] = []
    result = fetch_latest_normal_batch_records(
        pointer_repo, eval_repo, sleep=lambda s: sleep_calls.append(s)
    )

    assert result is LATEST_BATCH_STILL_PROPAGATING
    assert len(sleep_calls) == 1


def test_does_not_mix_older_batch_records(tmp_path: Path) -> None:
    """latest_completed_batch_id以外のbatch(過去に完了した別batch)の
    レコードが混ざらないこと。"""
    pointer_repo = LatestBuyCandidateBatchPointerRepository(store_dir=tmp_path)
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    eval_repo.upsert(_record("batch-old", "9999"))
    eval_repo.upsert(_record("batch-1", "1111"))
    pointer_repo.update_latest_completed(
        LatestBuyCandidateBatchPointer(
            latest_completed_batch_id="batch-1", completed_at=_NOW, total_candidates=1
        )
    )

    result = fetch_latest_normal_batch_records(pointer_repo, eval_repo, sleep=lambda s: None)

    assert result is not None
    assert set(result.records_by_stock_code) == {"1111"}
