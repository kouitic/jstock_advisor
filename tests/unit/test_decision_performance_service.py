"""services/decision_performance_service.pyのテスト(判定精度向上機能Phase A)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.domain.entities.decision_snapshot import (
    DECISION_SNAPSHOT_MODEL_VERSION,
    DecisionSnapshot,
)
from jstock_advisor.domain.entities.enums import DecisionType, EvaluationLabel, RecommendationType
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.services.decision_performance_service import DecisionPerformanceService

_NOW = dt.datetime(2026, 8, 8, tzinfo=dt.UTC)


def _decision(
    decision_id: str,
    decision_type: DecisionType = DecisionType.BUY,
    existing_action: RecommendationType = RecommendationType.BUY,
) -> DecisionSnapshot:
    return DecisionSnapshot(
        decision_id=decision_id,
        decision_type=decision_type,
        stock_code="2914",
        evaluated_at=_NOW,
        evaluation_date_jst=_NOW.date(),
        recommendation_id="rec-1",
        existing_action=existing_action,
        market_price=Decimal("1150"),
        rule_version="v1-mvp",
        model_version=DECISION_SNAPSHOT_MODEL_VERSION,
        data_fetched_at=_NOW,
    )


def _evaluation(
    evaluation_id: str,
    decision_id: str | None,
    horizon_business_days: int = 5,
    price_return_pct: float = 3.0,
    label: EvaluationLabel = EvaluationLabel.SUCCESS,
) -> EvaluationResult:
    return EvaluationResult(
        evaluation_id=evaluation_id,
        recommendation_id="rec-1",
        horizon_business_days=horizon_business_days,
        evaluated_at=_NOW,
        evaluation_date=_NOW.date(),
        price_at_evaluation=Decimal("1200"),
        price_return_pct=price_return_pct,
        evaluation_label=label,
        label_evidence="x",
        decision_id=decision_id,
    )


def test_summarize_with_no_data_returns_empty_overall(tmp_path: Path) -> None:
    service = DecisionPerformanceService(
        evaluation_repository=EvaluationResultRepository(store_dir=tmp_path),
        decision_repository=DecisionSnapshotRepository(store_dir=tmp_path),
    )

    summary = service.summarize(now=_NOW)

    assert summary.overall.count == 0
    assert summary.by_decision_type == []


def test_summarize_excludes_evaluations_without_decision_id(tmp_path: Path) -> None:
    """既存recommendation_idベースの評価(decision_id=None)は集計対象外
    (既存の週次改善レビュー等とは完全に独立させる、決定②)。"""
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    eval_repo.save(_evaluation("e-legacy", decision_id=None))

    service = DecisionPerformanceService(
        evaluation_repository=eval_repo,
        decision_repository=DecisionSnapshotRepository(store_dir=tmp_path),
    )
    summary = service.summarize(now=_NOW)

    assert summary.overall.count == 0


def test_summarize_joins_via_decision_id_and_groups(tmp_path: Path) -> None:
    decision_repo = DecisionSnapshotRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)

    decision_repo.save(_decision("dec-1", DecisionType.BUY, RecommendationType.BUY))
    decision_repo.save(_decision("dec-2", DecisionType.PROFIT_TAKING, RecommendationType.SELL))
    eval_repo.save(_evaluation("e1", decision_id="dec-1"))
    eval_repo.save(_evaluation("e2", decision_id="dec-2"))

    service = DecisionPerformanceService(
        evaluation_repository=eval_repo, decision_repository=decision_repo
    )
    summary = service.summarize(now=_NOW)

    assert summary.overall.count == 2
    by_type_keys = {b.key for b in summary.by_decision_type}
    assert by_type_keys == {"BUY", "PROFIT_TAKING"}
    by_action_keys = {b.key for b in summary.by_existing_action}
    assert by_action_keys == {"BUY", "SELL"}
    by_version_keys = {b.key for b in summary.by_model_version}
    assert by_version_keys == {DECISION_SNAPSHOT_MODEL_VERSION}


def test_summarize_filters_by_horizon(tmp_path: Path) -> None:
    decision_repo = DecisionSnapshotRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    decision_repo.save(_decision("dec-1"))
    eval_repo.save(_evaluation("e1", decision_id="dec-1", horizon_business_days=5))
    eval_repo.save(_evaluation("e2", decision_id="dec-1", horizon_business_days=20))

    service = DecisionPerformanceService(
        evaluation_repository=eval_repo, decision_repository=decision_repo
    )
    summary = service.summarize(horizon_business_days=5, now=_NOW)

    assert summary.overall.count == 1


def test_summarize_excludes_evaluation_with_missing_decision(tmp_path: Path) -> None:
    """decision_idに対応するDecisionSnapshotが存在しない(join欠損)場合は推測補完せず、
    グループ集計から除外する(overall集計には残る)。"""
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    eval_repo.save(_evaluation("e1", decision_id="missing-decision"))

    service = DecisionPerformanceService(
        evaluation_repository=eval_repo,
        decision_repository=DecisionSnapshotRepository(store_dir=tmp_path),
    )
    summary = service.summarize(now=_NOW)

    assert summary.overall.count == 1  # overallはdecision_idがNoneでないもの全て
    assert summary.by_decision_type == []  # joinできないためグループには出てこない
