"""DecisionSnapshotRepositoryのテスト(判定精度向上機能Phase A)。

コードレビュー対応(insert-only保証): DecisionSnapshotは一度保存されたら
後から絶対に上書きしない。upsert()は使用せず、insert_if_absent()のみを使う。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.domain.entities.decision_snapshot import (
    DECISION_SNAPSHOT_MODEL_VERSION,
    DecisionSnapshot,
    build_decision_id,
)
from jstock_advisor.domain.entities.enums import DecisionType
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)

_NOW = dt.datetime(2026, 8, 8, 3, 0, tzinfo=dt.UTC)


def _decision(
    decision_id: str = "dec-1", market_price: Decimal = Decimal("1150")
) -> DecisionSnapshot:
    return DecisionSnapshot(
        decision_id=decision_id,
        decision_type=DecisionType.BUY,
        stock_code="2914",
        evaluated_at=_NOW,
        evaluation_date_jst=dt.date(2026, 8, 8),
        recommendation_id="rec-1",
        market_price=market_price,
        rule_version="v1-mvp",
        model_version=DECISION_SNAPSHOT_MODEL_VERSION,
    )


def test_insert_if_absent_succeeds_on_first_insert(tmp_path: Path) -> None:
    repo = DecisionSnapshotRepository(store_dir=tmp_path)

    assert repo.insert_if_absent(_decision()) is True

    fetched = repo.get("dec-1")
    assert fetched is not None
    assert fetched.stock_code == "2914"
    assert fetched.market_price == Decimal("1150")


def test_get_returns_none_when_missing(tmp_path: Path) -> None:
    repo = DecisionSnapshotRepository(store_dir=tmp_path)
    assert repo.get("does-not-exist") is None


def test_list_all_returns_all_saved(tmp_path: Path) -> None:
    repo = DecisionSnapshotRepository(store_dir=tmp_path)
    repo.insert_if_absent(_decision("dec-1"))
    repo.insert_if_absent(_decision("dec-2"))

    items = repo.list_all()
    assert {d.decision_id for d in items} == {"dec-1", "dec-2"}


def test_insert_if_absent_returns_false_and_does_not_overwrite_existing_id(
    tmp_path: Path,
) -> None:
    """コードレビュー対応(insert-only): 同一decision_idへの2回目のinsert_if_absentは
    Falseを返し、既存の記録は一切変更されない(upsertのような上書きは発生しない)。
    「最初に記録された事実が永続的に正であること」を保証する。"""
    repo = DecisionSnapshotRepository(store_dir=tmp_path)
    assert repo.insert_if_absent(_decision("dec-1", market_price=Decimal("1150"))) is True
    assert repo.insert_if_absent(_decision("dec-1", market_price=Decimal("1200"))) is False

    items = repo.list_all()
    assert len(items) == 1
    assert items[0].market_price == Decimal("1150")


def test_same_recommendation_saved_twice_yields_one_record_with_first_value(
    tmp_path: Path,
) -> None:
    """コードレビュー対応(冪等性): 決定的decision_idにより、同一Recommendationの
    保存処理が再実行されてもDecisionSnapshotが増殖せず、かつ最初に記録された
    値が上書きされない(単に件数が1件のままというだけでは不十分)。"""
    repo = DecisionSnapshotRepository(store_dir=tmp_path)
    decision_id = build_decision_id("rec-1")
    repo.insert_if_absent(_decision(decision_id, market_price=Decimal("1150")))
    repo.insert_if_absent(_decision(decision_id, market_price=Decimal("1160")))  # 再実行(値は微差)

    items = repo.list_all()
    assert len(items) == 1
    assert items[0].market_price == Decimal("1150")
