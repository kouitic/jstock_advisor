"""DecisionSnapshotRepositoryのテスト(判定精度向上機能Phase A)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.domain.entities.decision_snapshot import (
    DECISION_SNAPSHOT_MODEL_VERSION,
    DecisionSnapshot,
)
from jstock_advisor.domain.entities.enums import DecisionType
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)


def _decision(decision_id: str = "dec-1") -> DecisionSnapshot:
    now = dt.datetime(2026, 8, 8, 3, 0, tzinfo=dt.UTC)
    return DecisionSnapshot(
        decision_id=decision_id,
        decision_type=DecisionType.BUY,
        stock_code="2914",
        evaluated_at=now,
        evaluation_date_jst=dt.date(2026, 8, 8),
        recommendation_id="rec-1",
        market_price=Decimal("1150"),
        rule_version="v1-mvp",
        model_version=DECISION_SNAPSHOT_MODEL_VERSION,
        data_fetched_at=now,
    )


def test_save_and_get(tmp_path: Path) -> None:
    repo = DecisionSnapshotRepository(store_dir=tmp_path)
    repo.save(_decision())

    fetched = repo.get("dec-1")
    assert fetched is not None
    assert fetched.stock_code == "2914"
    assert fetched.market_price == Decimal("1150")


def test_get_returns_none_when_missing(tmp_path: Path) -> None:
    repo = DecisionSnapshotRepository(store_dir=tmp_path)
    assert repo.get("does-not-exist") is None


def test_list_all_returns_all_saved(tmp_path: Path) -> None:
    repo = DecisionSnapshotRepository(store_dir=tmp_path)
    repo.save(_decision("dec-1"))
    repo.save(_decision("dec-2"))

    items = repo.list_all()
    assert {d.decision_id for d in items} == {"dec-1", "dec-2"}


def test_save_upserts_existing_id(tmp_path: Path) -> None:
    repo = DecisionSnapshotRepository(store_dir=tmp_path)
    repo.save(_decision("dec-1"))
    updated = _decision("dec-1").model_copy(update={"market_price": Decimal("1200")})
    repo.save(updated)

    items = repo.list_all()
    assert len(items) == 1
    assert items[0].market_price == Decimal("1200")
