import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import (
    ApprovalStatus,
    ConfidenceLevel,
    EvaluationLabel,
    RecommendationType,
)
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.local_repository.rule_version_repository import (
    RuleProposalRepository,
)
from jstock_advisor.services.backtest_service import BacktestService
from jstock_advisor.services.rule_proposal_service import RuleProposalService

_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)
_TARGET = "screening.total_yield.min_total_yield_pct"


def _seed(
    tmp_path: Path, count: int
) -> tuple[RecommendationRepository, EvaluationResultRepository]:
    rec_repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    for i in range(count):
        rec_id = f"rec-{i}"
        yield_pct = 3.6 + (i % 5) * 0.5
        rec_repo.save(
            Recommendation(
                recommendation_id=rec_id,
                stock_code="2914",
                stock_name="test",
                recommended_at=_NOW,
                recommendation_type=RecommendationType.BUY,
                price_at_recommendation=Decimal("1000"),
                total_yield_pct_at_recommendation=yield_pct,
                confidence=ConfidenceLevel.HIGH,
                rule_version="v1",
            )
        )
        eval_repo.save(
            EvaluationResult(
                evaluation_id=f"e-{i}",
                recommendation_id=rec_id,
                horizon_business_days=20,
                evaluated_at=_NOW,
                evaluation_date=_NOW.date(),
                price_at_evaluation=Decimal("1100"),
                price_return_pct=5.0,
                evaluation_label=EvaluationLabel.SUCCESS,
                label_evidence="x",
            )
        )
    return rec_repo, eval_repo


def _build_service(tmp_path: Path, count: int) -> RuleProposalService:
    rec_repo, eval_repo = _seed(tmp_path, count)
    backtest = BacktestService(recommendation_repository=rec_repo, evaluation_repository=eval_repo)
    return RuleProposalService(
        proposal_repository=RuleProposalRepository(store_dir=tmp_path),
        backtest_service=backtest,
        evaluation_repository=eval_repo,
    )


@pytest.fixture
def service_with_sufficient_data(tmp_path: Path) -> RuleProposalService:
    return _build_service(tmp_path, 60)


def test_create_proposal_with_sufficient_data(
    service_with_sufficient_data: RuleProposalService,
) -> None:
    proposal = service_with_sufficient_data.create_proposal(
        target=_TARGET,
        current_value=3.5,
        proposed_value=4.0,
        reason="test",
        risk_impact="low",
        overfitting_risk_assessment="low",
        rollback_condition="revert if success rate drops",
    )
    assert proposal.status == ApprovalStatus.DRAFT
    assert proposal.evaluation_count == 60
    assert proposal.current_rule_performance
    assert proposal.proposed_rule_backtest_performance


def test_create_proposal_fails_with_insufficient_data(tmp_path: Path) -> None:
    service = _build_service(tmp_path, 10)
    with pytest.raises(ValueError, match="評価件数が不足"):
        service.create_proposal(
            target=_TARGET,
            current_value=3.5,
            proposed_value=4.0,
            reason="test",
            risk_impact="low",
            overfitting_risk_assessment="low",
            rollback_condition="revert",
        )


def test_create_proposal_unsupported_target_uses_general_threshold(tmp_path: Path) -> None:
    service = _build_service(tmp_path, 30)
    proposal = service.create_proposal(
        target="sell.rules.some_new_rule.enabled",
        current_value=0.0,
        proposed_value=1.0,
        reason="test",
        risk_impact="low",
        overfitting_risk_assessment="low",
        rollback_condition="revert",
    )
    assert proposal.evaluation_count == 30
    assert proposal.proposed_rule_backtest_performance["supported"] is False


def test_create_proposal_lifecycle(service_with_sufficient_data: RuleProposalService) -> None:
    proposal = service_with_sufficient_data.create_proposal(
        target=_TARGET,
        current_value=3.5,
        proposed_value=4.0,
        reason="test",
        risk_impact="low",
        overfitting_risk_assessment="low",
        rollback_condition="revert",
    )
    submitted = service_with_sufficient_data.submit_for_review(proposal.proposal_id)
    assert submitted.status == ApprovalStatus.PROPOSED
    approved = service_with_sufficient_data.approve(proposal.proposal_id)
    assert approved.status == ApprovalStatus.APPROVED


def test_reject_requires_proposed_status(
    service_with_sufficient_data: RuleProposalService,
) -> None:
    proposal = service_with_sufficient_data.create_proposal(
        target=_TARGET,
        current_value=3.5,
        proposed_value=4.0,
        reason="test",
        risk_impact="low",
        overfitting_risk_assessment="low",
        rollback_condition="revert",
    )
    with pytest.raises(ValueError):
        service_with_sufficient_data.reject(proposal.proposal_id)


def test_approve_requires_proposed_status(
    service_with_sufficient_data: RuleProposalService,
) -> None:
    proposal = service_with_sufficient_data.create_proposal(
        target=_TARGET,
        current_value=3.5,
        proposed_value=4.0,
        reason="test",
        risk_impact="low",
        overfitting_risk_assessment="low",
        rollback_condition="revert",
    )
    with pytest.raises(ValueError):
        service_with_sufficient_data.approve(proposal.proposal_id)
