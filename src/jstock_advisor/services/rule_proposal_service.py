"""ルール改善提案サービス(要求仕様41・44・45節)。

backtest_service/performance_metrics_serviceの結果をもとに改善提案(RuleProposal)を
作成する。評価件数が要求仕様45節の最低件数(閾値変更: 60件、それ以外: 30件)に
満たない場合は提案を作成せず、明示的に「データ不足」のエラーとする。

作成された提案はDRAFT状態であり、人間が明示的にapprove()するまで一切の
自動適用は行わない。承認後の設定ファイル反映・ルールバージョン有効化も、
本サービスは行わない(rule_version_serviceで別途、人間が操作する)。
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import asdict
from typing import Any

from jstock_advisor.domain.entities.enums import ApprovalStatus
from jstock_advisor.domain.entities.rule_version import RuleProposal
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.rule_version_repository import (
    RuleProposalRepository,
)
from jstock_advisor.services.backtest_service import BacktestResult, BacktestService
from jstock_advisor.services.performance_metrics_service import MetricsBucket


def _bucket_to_dict(bucket: MetricsBucket | None) -> dict[str, Any]:
    if bucket is None:
        return {}
    return asdict(bucket)


def _compute_performance_diff(
    current: MetricsBucket | None, proposed: MetricsBucket | None
) -> dict[str, Any]:
    if current is None or proposed is None:
        return {}
    diff: dict[str, Any] = {}
    if current.success_rate_pct is not None and proposed.success_rate_pct is not None:
        diff["success_rate_pct_diff"] = proposed.success_rate_pct - current.success_rate_pct
    if current.avg_price_return_pct is not None and proposed.avg_price_return_pct is not None:
        diff["avg_price_return_pct_diff"] = (
            proposed.avg_price_return_pct - current.avg_price_return_pct
        )
    if current.avg_excess_return_pct is not None and proposed.avg_excess_return_pct is not None:
        diff["avg_excess_return_pct_diff"] = (
            proposed.avg_excess_return_pct - current.avg_excess_return_pct
        )
    return diff


class RuleProposalService:
    def __init__(
        self,
        proposal_repository: RuleProposalRepository | None = None,
        backtest_service: BacktestService | None = None,
        evaluation_repository: EvaluationResultRepository | None = None,
    ) -> None:
        self._repo = proposal_repository or RuleProposalRepository()
        self._backtest = backtest_service or BacktestService()
        self._evaluations = evaluation_repository or EvaluationResultRepository()

    def create_proposal(
        self,
        target: str,
        current_value: Any,
        proposed_value: Any,
        reason: str,
        risk_impact: str,
        overfitting_risk_assessment: str,
        rollback_condition: str,
        recommended_application_period: str | None = None,
        now: dt.datetime | None = None,
    ) -> RuleProposal:
        backtest_result: BacktestResult | None = None
        if isinstance(current_value, int | float) and isinstance(proposed_value, int | float):
            backtest_result = self._backtest.run(
                target, float(current_value), float(proposed_value)
            )

        if backtest_result is not None and backtest_result.supported:
            evaluation_count = backtest_result.evaluation_count_current
            min_required = RuleProposal.MIN_EVALUATION_COUNT_FOR_THRESHOLD_CHANGE
            current_performance = _bucket_to_dict(backtest_result.current_performance)
            proposed_performance = _bucket_to_dict(backtest_result.proposed_performance)
            performance_diff = _compute_performance_diff(
                backtest_result.current_performance, backtest_result.proposed_performance
            )
        else:
            evaluation_count = len(self._evaluations.list_all())
            min_required = RuleProposal.MIN_EVALUATION_COUNT_FOR_PROPOSAL
            current_performance = {}
            proposed_performance = {
                "supported": False,
                "reason": (
                    backtest_result.reason_unsupported
                    if backtest_result is not None
                    else "current_value/proposed_valueが数値でないためバックテストを実行できません"
                ),
            }
            performance_diff = {}

        if evaluation_count < min_required:
            raise ValueError(
                f"評価件数が不足しているため提案を作成できません"
                f"(現在{evaluation_count}件、最低{min_required}件必要)"
            )

        proposal = RuleProposal(
            proposal_id=str(uuid.uuid4()),
            created_at=now or dt.datetime.now(dt.UTC),
            target=target,
            current_value=current_value,
            proposed_value=proposed_value,
            reason=reason,
            evaluation_count=evaluation_count,
            current_rule_performance=current_performance,
            proposed_rule_backtest_performance=proposed_performance,
            performance_diff=performance_diff,
            risk_impact=risk_impact,
            overfitting_risk_assessment=overfitting_risk_assessment,
            recommended_application_period=recommended_application_period,
            rollback_condition=rollback_condition,
            status=ApprovalStatus.DRAFT,
        )
        self._repo.save(proposal)
        return proposal

    def list_all(self) -> list[RuleProposal]:
        return self._repo.list_all()

    def get(self, proposal_id: str) -> RuleProposal | None:
        return self._repo.get(proposal_id)

    def _require(self, proposal_id: str) -> RuleProposal:
        proposal = self._repo.get(proposal_id)
        if proposal is None:
            raise ValueError(f"proposal_id={proposal_id} が見つかりません")
        return proposal

    def submit_for_review(self, proposal_id: str) -> RuleProposal:
        proposal = self._require(proposal_id)
        if proposal.status != ApprovalStatus.DRAFT:
            raise ValueError(f"DRAFT状態の提案のみ申請できます(現在: {proposal.status.value})")
        updated = proposal.model_copy(update={"status": ApprovalStatus.PROPOSED})
        self._repo.save(updated)
        return updated

    def approve(self, proposal_id: str) -> RuleProposal:
        proposal = self._require(proposal_id)
        if proposal.status != ApprovalStatus.PROPOSED:
            raise ValueError(f"PROPOSED状態の提案のみ承認できます(現在: {proposal.status.value})")
        updated = proposal.model_copy(update={"status": ApprovalStatus.APPROVED})
        self._repo.save(updated)
        return updated

    def reject(self, proposal_id: str) -> RuleProposal:
        proposal = self._require(proposal_id)
        if proposal.status != ApprovalStatus.PROPOSED:
            raise ValueError(f"PROPOSED状態の提案のみ却下できます(現在: {proposal.status.value})")
        updated = proposal.model_copy(update={"status": ApprovalStatus.REJECTED})
        self._repo.save(updated)
        return updated
