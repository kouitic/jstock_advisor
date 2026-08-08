"""EvaluationResultRepository.exists_for_decision_horizon()のテスト
(判定精度向上機能Phase A)。

既存のexists_for_horizon/exists_for_calendar_horizon(recommendation_idベース)は
変更していないため、ここではテストしない(既存のカバレッジは
test_recommendation_evaluation_service.py側で確認済み)。
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

_NOW = dt.datetime(2026, 8, 8, tzinfo=dt.UTC)


def _evaluation(
    evaluation_id: str, decision_id: str | None, horizon_business_days: int = 5
) -> EvaluationResult:
    return EvaluationResult(
        evaluation_id=evaluation_id,
        recommendation_id="rec-1",
        horizon_business_days=horizon_business_days,
        evaluated_at=_NOW,
        evaluation_date=_NOW.date(),
        price_at_evaluation=Decimal("1200"),
        price_return_pct=1.0,
        evaluation_label=EvaluationLabel.SUCCESS,
        label_evidence="x",
        decision_id=decision_id,
    )


def test_exists_for_decision_horizon_true_when_matching_row_present(tmp_path: Path) -> None:
    repo = EvaluationResultRepository(store_dir=tmp_path)
    repo.save(_evaluation("e1", decision_id="dec-1", horizon_business_days=5))

    assert repo.exists_for_decision_horizon("dec-1", 5) is True


def test_exists_for_decision_horizon_false_when_no_matching_row(tmp_path: Path) -> None:
    repo = EvaluationResultRepository(store_dir=tmp_path)
    repo.save(_evaluation("e1", decision_id="dec-1", horizon_business_days=5))

    assert repo.exists_for_decision_horizon("dec-1", 20) is False
    assert repo.exists_for_decision_horizon("dec-2", 5) is False


def test_exists_for_decision_horizon_ignores_legacy_rows_without_decision_id(
    tmp_path: Path,
) -> None:
    """既存recommendation_idベースの評価(decision_id=None)は、decision_idベースの
    冪等性チェックの対象にならない(既存ロジックとは完全に独立、決定②)。"""
    repo = EvaluationResultRepository(store_dir=tmp_path)
    repo.save(_evaluation("e1", decision_id=None, horizon_business_days=5))

    assert repo.exists_for_decision_horizon("dec-1", 5) is False
