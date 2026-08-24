"""最新完了BUY候補batchポインタのリポジトリテスト(LINE UI第二弾、2026-08)。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from jstock_advisor.domain.entities.buy_candidate_batch_pointer import (
    LatestBuyCandidateBatchPointer,
)
from jstock_advisor.infrastructure.local_repository.latest_buy_candidate_batch_pointer_repository import (  # noqa: E501
    LatestBuyCandidateBatchPointerRepository,
)

_NOW = dt.datetime(2026, 8, 24, 7, 0, tzinfo=dt.UTC)


def test_get_returns_none_when_never_set(tmp_path: Path) -> None:
    repo = LatestBuyCandidateBatchPointerRepository(store_dir=tmp_path)
    assert repo.get() is None


def test_update_latest_completed_then_get_round_trips(tmp_path: Path) -> None:
    repo = LatestBuyCandidateBatchPointerRepository(store_dir=tmp_path)
    repo.update_latest_completed(
        LatestBuyCandidateBatchPointer(
            latest_completed_batch_id="batch-1", completed_at=_NOW, total_candidates=470
        )
    )

    pointer = repo.get()
    assert pointer is not None
    assert pointer.latest_completed_batch_id == "batch-1"
    assert pointer.total_candidates == 470


def test_update_latest_completed_overwrites_previous_value(tmp_path: Path) -> None:
    repo = LatestBuyCandidateBatchPointerRepository(store_dir=tmp_path)
    repo.update_latest_completed(
        LatestBuyCandidateBatchPointer(
            latest_completed_batch_id="batch-1", completed_at=_NOW, total_candidates=100
        )
    )
    repo.update_latest_completed(
        LatestBuyCandidateBatchPointer(
            latest_completed_batch_id="batch-2",
            completed_at=_NOW + dt.timedelta(days=1),
            total_candidates=200,
        )
    )

    pointer = repo.get()
    assert pointer is not None
    assert pointer.latest_completed_batch_id == "batch-2"
    assert pointer.total_candidates == 200
