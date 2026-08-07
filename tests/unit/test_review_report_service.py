import datetime as dt
from collections.abc import Callable
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
from jstock_advisor.domain.entities.rule_version import RuleProposal
from jstock_advisor.infrastructure.line.client import ConsoleLineClient, LineClient
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.local_repository.rule_version_repository import (
    RuleProposalRepository,
)
from jstock_advisor.services.performance_metrics_service import PerformanceMetricsService
from jstock_advisor.services.review_report_service import ReviewReportService
from jstock_advisor.services.rule_proposal_service import RuleProposalService

_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)


@pytest.fixture
def build_review_service(tmp_path: Path) -> Callable[[LineClient | None], ReviewReportService]:
    def _build(line_client: LineClient | None = None) -> ReviewReportService:
        rec_repo = RecommendationRepository(store_dir=tmp_path)
        eval_repo = EvaluationResultRepository(store_dir=tmp_path)
        rec_repo.save(
            Recommendation(
                recommendation_id="rec-1",
                stock_code="2914",
                stock_name="test",
                recommended_at=_NOW,
                recommendation_type=RecommendationType.BUY,
                price_at_recommendation=Decimal("1000"),
                confidence=ConfidenceLevel.HIGH,
                rule_version="v1",
            )
        )
        eval_repo.save(
            EvaluationResult(
                evaluation_id="e-1",
                recommendation_id="rec-1",
                horizon_business_days=20,
                evaluated_at=_NOW,
                evaluation_date=_NOW.date(),
                price_at_evaluation=Decimal("1100"),
                price_return_pct=10.0,
                evaluation_label=EvaluationLabel.SUCCESS,
                label_evidence="x",
            )
        )

        proposal_repo = RuleProposalRepository(store_dir=tmp_path)
        proposal_repo.save(
            RuleProposal(
                proposal_id="p-1",
                created_at=_NOW,
                target="screening.total_yield.min_total_yield_pct",
                current_value=3.5,
                proposed_value=4.0,
                reason="test",
                evaluation_count=60,
                current_rule_performance={},
                proposed_rule_backtest_performance={},
                performance_diff={},
                risk_impact="low",
                overfitting_risk_assessment="low",
                rollback_condition="revert",
                status=ApprovalStatus.PROPOSED,
            )
        )

        return ReviewReportService(
            performance_metrics_service=PerformanceMetricsService(
                evaluation_repository=eval_repo, recommendation_repository=rec_repo
            ),
            rule_proposal_service=RuleProposalService(proposal_repository=proposal_repo),
            line_client=line_client,
        )

    return _build


def test_build_report_text_includes_summary_and_proposal(
    build_review_service: Callable[[LineClient | None], ReviewReportService],
) -> None:
    service = build_review_service(None)
    text = service.build_report_text(now=_NOW)
    assert "成績サマリ" in text
    assert "評価件数: 1件" in text
    assert "screening.total_yield.min_total_yield_pct" in text
    assert "承認待ち" in text


def test_build_report_text_shows_out_of_scope_instead_of_zero_percent(tmp_path: Path) -> None:
    """WATCHのようにINCONCLUSIVEしか付かない種別は「0%」ではなく「評価対象外」と
    表示すること(振り返り機能改修での回帰確認)。"""
    rec_repo = RecommendationRepository(store_dir=tmp_path)
    eval_repo = EvaluationResultRepository(store_dir=tmp_path)
    rec_repo.save(
        Recommendation(
            recommendation_id="rec-watch",
            stock_code="2914",
            stock_name="test",
            recommended_at=_NOW,
            recommendation_type=RecommendationType.WATCH,
            price_at_recommendation=Decimal("1000"),
            confidence=ConfidenceLevel.HIGH,
            rule_version="v1",
        )
    )
    eval_repo.save(
        EvaluationResult(
            evaluation_id="e-watch",
            recommendation_id="rec-watch",
            horizon_business_days=20,
            evaluated_at=_NOW,
            evaluation_date=_NOW.date(),
            price_at_evaluation=Decimal("1100"),
            price_return_pct=10.0,
            evaluation_label=EvaluationLabel.INCONCLUSIVE,
            label_evidence="x",
        )
    )
    service = ReviewReportService(
        performance_metrics_service=PerformanceMetricsService(
            evaluation_repository=eval_repo, recommendation_repository=rec_repo
        ),
        rule_proposal_service=RuleProposalService(
            proposal_repository=RuleProposalRepository(store_dir=tmp_path)
        ),
    )

    text = service.build_report_text(now=_NOW)
    assert "WATCH: 1件 成功率評価対象外" in text
    assert "WATCH: 1件 成功率0.0%" not in text


def test_send_report_requires_line_client(
    build_review_service: Callable[[LineClient | None], ReviewReportService],
) -> None:
    service = build_review_service(None)
    with pytest.raises(ValueError):
        service.send_report()


def test_send_report_pushes_via_line_client(
    build_review_service: Callable[[LineClient | None], ReviewReportService],
) -> None:
    client = ConsoleLineClient()
    service = build_review_service(client)
    text = service.send_report(now=_NOW)
    assert client.sent_messages == [text]
