"""EvaluationResultRepository.iter_all()のテスト(Issue #377)。

RecommendationRepositoryのiter_all()(Issue #113)にはこの粒度の単体テストが
無く、下位のCollectionStore側(test_dynamodb_store.py)でのみ一般的に検証
されている。本テストはEvaluationResultRepositoryという具体的な呼び出し元
経由でも同じ契約(list_all()と列挙順・件数・内容が一致する)が成立することを
固定する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.domain.entities.enums import EvaluationLabel
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)


def _evaluation(eval_id: str, evaluated_at: dt.datetime) -> EvaluationResult:
    return EvaluationResult(
        evaluation_id=eval_id,
        recommendation_id=f"rec-{eval_id}",
        horizon_calendar_days=7,
        evaluated_at=evaluated_at,
        evaluation_date=evaluated_at.date(),
        price_at_evaluation=Decimal("1010"),
        price_return_pct=1.0,
        excess_return_pct=1.0,
        evaluation_label=EvaluationLabel.SUCCESS,
        label_evidence="x",
    )


def test_iter_all_returns_same_items_as_list_all(tmp_path: Path) -> None:
    repo = EvaluationResultRepository(store_dir=tmp_path)
    base = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    for i in range(5):
        repo.save(_evaluation(f"e{i}", base + dt.timedelta(days=i)))

    assert list(repo.iter_all()) == repo.list_all()


def test_iter_all_returns_empty_for_empty_repository(tmp_path: Path) -> None:
    repo = EvaluationResultRepository(store_dir=tmp_path)

    assert list(repo.iter_all()) == []
    assert repo.list_all() == []
