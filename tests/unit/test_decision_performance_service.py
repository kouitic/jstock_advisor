"""services/decision_performance_service.pyのテスト(判定精度向上機能Phase A)。

コードレビュー対応: DecisionSnapshot専用のEvaluationResultは生成しないため、
joinはEvaluationResult.recommendation_id == DecisionSnapshot.recommendation_id
で行い、Phase A対象ホライズン(既定5/20/60/120/250営業日)のみへ絞り込む。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig, DecisionEvaluationConfig
from jstock_advisor.domain.entities.decision_snapshot import (
    DECISION_SNAPSHOT_MODEL_VERSION,
    DecisionSnapshot,
    build_decision_id,
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


def _config(horizons_business_days: list[int] | None = None) -> AppConfig:
    base = load_config()
    return base.model_copy(
        update={
            "decision_evaluation": DecisionEvaluationConfig(
                horizons_business_days=horizons_business_days or [5, 20, 60, 120, 250]
            )
        }
    )


def _decision(
    recommendation_id: str,
    decision_type: DecisionType = DecisionType.BUY,
    existing_action: RecommendationType = RecommendationType.BUY,
) -> DecisionSnapshot:
    return DecisionSnapshot(
        decision_id=build_decision_id(decision_type, recommendation_id),
        decision_type=decision_type,
        stock_code="2914",
        evaluated_at=_NOW,
        evaluation_date_jst=_NOW.date(),
        recommendation_id=recommendation_id,
        existing_action=existing_action,
        market_price=Decimal("1150"),
        rule_version="v1-mvp",
        model_version=DECISION_SNAPSHOT_MODEL_VERSION,
    )


def _evaluation(
    evaluation_id: str,
    recommendation_id: str,
    horizon_business_days: int | None = 5,
    horizon_calendar_days: int | None = None,
    price_return_pct: float = 3.0,
    max_gain_pct: float | None = 5.0,
    max_drawdown_pct: float | None = -2.0,
    label: EvaluationLabel = EvaluationLabel.SUCCESS,
) -> EvaluationResult:
    return EvaluationResult(
        evaluation_id=evaluation_id,
        recommendation_id=recommendation_id,
        horizon_business_days=horizon_business_days,
        horizon_calendar_days=horizon_calendar_days,
        evaluated_at=_NOW,
        evaluation_date=_NOW.date(),
        price_at_evaluation=Decimal("1200"),
        price_return_pct=price_return_pct,
        max_gain_pct=max_gain_pct,
        max_drawdown_pct=max_drawdown_pct,
        evaluation_label=label,
        label_evidence="x",
    )


def test_summarize_with_no_data_returns_empty_overall(tmp_path: Path) -> None:
    service = DecisionPerformanceService(
        evaluation_repository=EvaluationResultRepository(store_dir=tmp_path),
        decision_repository=DecisionSnapshotRepository(store_dir=tmp_path),
        config=_config(),
    )

    summary = service.summarize(now=_NOW)

    assert summary.overall.count == 0
    assert summary.by_decision_type == []


def test_summarize_joins_via_recommendation_id_and_groups(tmp_path: Path) -> None:
    decision_repo = DecisionSnapshotRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)

    decision_repo.save(_decision("rec-1", DecisionType.BUY, RecommendationType.BUY))
    decision_repo.save(_decision("rec-2", DecisionType.PROFIT_TAKING, RecommendationType.SELL))
    eval_repo.save(_evaluation("e1", "rec-1", horizon_business_days=5))
    eval_repo.save(_evaluation("e2", "rec-2", horizon_business_days=5))

    service = DecisionPerformanceService(
        evaluation_repository=eval_repo, decision_repository=decision_repo, config=_config()
    )
    summary = service.summarize(now=_NOW)

    assert summary.overall.count == 2
    assert {b.key for b in summary.by_decision_type} == {"BUY", "PROFIT_TAKING"}
    assert {b.key for b in summary.by_existing_action} == {"BUY", "SELL"}
    assert {b.key for b in summary.by_model_version} == {DECISION_SNAPSHOT_MODEL_VERSION}


def test_summarize_excludes_evaluation_with_missing_decision(tmp_path: Path) -> None:
    """decision_idに対応するDecisionSnapshotが存在しない(join欠損。Phase A導入前の
    Recommendation等)場合は推測補完せず、集計から除外する。"""
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    eval_repo.save(_evaluation("e1", "rec-without-decision", horizon_business_days=5))

    service = DecisionPerformanceService(
        evaluation_repository=eval_repo,
        decision_repository=DecisionSnapshotRepository(store_dir=tmp_path),
        config=_config(),
    )
    summary = service.summarize(now=_NOW)

    assert summary.overall.count == 0


def test_summarize_only_includes_phase_a_horizons(tmp_path: Path) -> None:
    """1営業日の共通チェックポイント・7暦日評価(週次改善レビュー専用)は
    DecisionPerformanceへ混入しない(既定horizons_business_days=[5,20,60,120,250])。"""
    decision_repo = DecisionSnapshotRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    decision_repo.save(_decision("rec-1"))

    eval_repo.save(_evaluation("e-1d", "rec-1", horizon_business_days=1))  # 対象外
    eval_repo.save(
        _evaluation("e-7cal", "rec-1", horizon_business_days=None, horizon_calendar_days=7)
    )  # 対象外(暦日評価)
    eval_repo.save(_evaluation("e-5d", "rec-1", horizon_business_days=5))  # 対象
    eval_repo.save(_evaluation("e-20d", "rec-1", horizon_business_days=20))  # 対象

    service = DecisionPerformanceService(
        evaluation_repository=eval_repo, decision_repository=decision_repo, config=_config()
    )
    summary = service.summarize(now=_NOW)

    assert summary.overall.count == 2  # 5d/20dのみ


def test_summarize_filters_by_specific_horizon(tmp_path: Path) -> None:
    decision_repo = DecisionSnapshotRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    decision_repo.save(_decision("rec-1"))
    eval_repo.save(_evaluation("e1", "rec-1", horizon_business_days=5))
    eval_repo.save(_evaluation("e2", "rec-1", horizon_business_days=20))

    service = DecisionPerformanceService(
        evaluation_repository=eval_repo, decision_repository=decision_repo, config=_config()
    )
    summary = service.summarize(horizon_business_days=5, now=_NOW)

    assert summary.overall.count == 1


def test_summarize_computes_median_mfe_mae(tmp_path: Path) -> None:
    decision_repo = DecisionSnapshotRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    decision_repo.save(_decision("rec-1"))
    decision_repo.save(_decision("rec-2"))
    eval_repo.save(
        _evaluation(
            "e1", "rec-1", horizon_business_days=5,
            price_return_pct=2.0, max_gain_pct=4.0, max_drawdown_pct=-1.0,
        )
    )
    eval_repo.save(
        _evaluation(
            "e2", "rec-2", horizon_business_days=5,
            price_return_pct=6.0, max_gain_pct=8.0, max_drawdown_pct=-3.0,
        )
    )

    service = DecisionPerformanceService(
        evaluation_repository=eval_repo, decision_repository=decision_repo, config=_config()
    )
    summary = service.summarize(now=_NOW)

    assert summary.median_price_return_pct == 4.0
    assert summary.avg_mfe_pct == 6.0
    assert summary.avg_mae_pct == -2.0
