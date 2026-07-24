"""定期レビューレポート生成サービス(要求仕様42節)。

performance_metrics_service/rule_proposal_serviceの結果をテキストレポートに
まとめ、LineClient経由でLINEへ送信できるようにする(週次・月次レビュー等の
定期実行を想定)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.enums import ApprovalStatus
from jstock_advisor.domain.jst import to_jst
from jstock_advisor.infrastructure.line.client import LineClient
from jstock_advisor.services.performance_metrics_service import (
    MetricsBucket,
    PerformanceMetricsService,
    PerformanceSummary,
)
from jstock_advisor.services.rule_proposal_service import RuleProposalService

_DISCLAIMER = "※最終的な投資判断は利用者が行ってください。"


def _format_bucket_line(bucket: MetricsBucket) -> str:
    rate = f"{bucket.success_rate_pct:.1f}%" if bucket.success_rate_pct is not None else "-"
    return f"  {bucket.key}: {bucket.count}件 成功率{rate}"


def _format_summary(summary: PerformanceSummary) -> list[str]:
    horizon_label = (
        f"{summary.horizon_business_days}営業日後"
        if summary.horizon_business_days is not None
        else "全期間合算"
    )
    lines = [f"【成績サマリ({horizon_label})】", f"評価件数: {summary.overall.count}件"]
    if summary.overall.success_rate_pct is not None:
        lines.append(f"成功率(SUCCESS+ACCEPTABLE): {summary.overall.success_rate_pct:.1f}%")
    if summary.overall.avg_price_return_pct is not None:
        lines.append(f"平均株価リターン: {summary.overall.avg_price_return_pct:.1f}%")
    if summary.overall.avg_excess_return_pct is not None:
        lines.append(
            f"平均超過リターン(対ベンチマーク): {summary.overall.avg_excess_return_pct:.1f}%"
        )
    lines.extend(_format_bucket_line(bucket) for bucket in summary.by_recommendation_type)
    return lines


class ReviewReportService:
    def __init__(
        self,
        performance_metrics_service: PerformanceMetricsService | None = None,
        rule_proposal_service: RuleProposalService | None = None,
        line_client: LineClient | None = None,
    ) -> None:
        self._metrics = performance_metrics_service or PerformanceMetricsService()
        self._proposals = rule_proposal_service or RuleProposalService()
        self._line_client = line_client

    def build_report_text(
        self, horizon_business_days: int | None = None, now: dt.datetime | None = None
    ) -> str:
        report_time = now or dt.datetime.now(dt.UTC)
        summary = self._metrics.summarize(
            horizon_business_days=horizon_business_days, now=report_time
        )

        lines = [f"■ 振り返りレポート({to_jst(report_time).date().isoformat()})"]
        lines.extend(_format_summary(summary))

        proposals = self._proposals.list_all()
        actionable = [
            p for p in proposals if p.status in (ApprovalStatus.DRAFT, ApprovalStatus.PROPOSED)
        ]
        if actionable:
            lines.append("")
            lines.append("【改善提案(要確認)】")
            for proposal in actionable:
                status_label = (
                    "承認待ち" if proposal.status == ApprovalStatus.PROPOSED else "未申請"
                )
                lines.append(
                    f"  [{status_label}] {proposal.target}: "
                    f"{proposal.current_value} → {proposal.proposed_value}({proposal.reason})"
                )

        lines.append("")
        lines.append(_DISCLAIMER)
        return "\n".join(lines)

    def send_report(
        self, horizon_business_days: int | None = None, now: dt.datetime | None = None
    ) -> str:
        if self._line_client is None:
            raise ValueError("line_clientが設定されていません")
        text = self.build_report_text(horizon_business_days, now)
        self._line_client.push_message(text)
        return text
