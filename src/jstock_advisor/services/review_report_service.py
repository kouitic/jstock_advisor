"""定期レビューレポート生成サービス(要求仕様42節)。

performance_metrics_service/rule_proposal_serviceの結果をテキストレポートに
まとめ、LineClient経由でLINEへ送信できるようにする(週次・月次レビュー等の
定期実行を想定)。
"""

from __future__ import annotations

import datetime as dt
import logging

from jstock_advisor.domain.entities.enums import ApprovalStatus
from jstock_advisor.domain.jst import to_jst
from jstock_advisor.infrastructure.line.client import LineClient
from jstock_advisor.services.performance_metrics_service import (
    MetricsBucket,
    PerformanceMetricsService,
    PerformanceSummary,
)
from jstock_advisor.services.rule_proposal_service import RuleProposalService

logger = logging.getLogger(__name__)

_DISCLAIMER = "※最終的な投資判断は利用者が行ってください。"

# Issue #50: 本文の文字数予算(LINEのhard limit 5000に対する安全余白)。
# 本サービスはLineNotificationService._push()を経由せず直接push_messageする
# 例外経路のため、要約の責務はこのサービス自身が負う。
REPORT_TEXT_CHAR_BUDGET = 4500


def _format_bucket_line(bucket: MetricsBucket) -> str:
    # conclusive_count==0(=全件がDATA_ISSUE/INCONCLUSIVE)の場合は「0%」ではなく
    # 「評価対象外」と表示する(WATCH等、自動評価の対象外な種別を誤って
    # 成績不良と読ませないため)。
    if bucket.conclusive_count == 0:
        rate = "評価対象外"
    elif bucket.success_rate_pct is not None:
        rate = f"{bucket.success_rate_pct:.1f}%"
    else:
        rate = "-"
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
        footer_lines = ["", _DISCLAIMER]

        # Issue #50: 改善提案は件数に上限が無く、蓄積すると本文がLINEの上限を
        # 超えてレポート全体が送信できなくなる。末尾を単純に切るのではなく、
        # 「入るだけ表示し、残りは『ほかN件』として件数を保持する」先読み方式で
        # 収める(完成形を毎回組み立てて判定するため、省略行の後付けによる
        # 予算超過が起きない)。
        def assemble(shown: int) -> str:
            body = list(lines)
            if actionable:
                body.append("")
                body.append("【改善提案(要確認)】")
                for proposal in actionable[:shown]:
                    status_label = (
                        "承認待ち" if proposal.status == ApprovalStatus.PROPOSED else "未申請"
                    )
                    body.append(
                        f"  [{status_label}] {proposal.target}: "
                        f"{proposal.current_value} → {proposal.proposed_value}"
                        f"({proposal.reason})"
                    )
                omitted = len(actionable) - min(shown, len(actionable))
                if omitted > 0:
                    body.append(f"  ほか{omitted}件(全件は`jstock review proposals`で確認)")
            return "\n".join(body + footer_lines)

        if not actionable or len(assemble(len(actionable))) <= REPORT_TEXT_CHAR_BUDGET:
            return assemble(len(actionable))

        low, high = 0, len(actionable)  # low は収まる、high は超える
        while high - low > 1:
            mid = (low + high) // 2
            if len(assemble(mid)) <= REPORT_TEXT_CHAR_BUDGET:
                low = mid
            else:
                high = mid
        text = assemble(low)
        logger.warning(
            "振り返りレポートの改善提案を省略しました: total=%d shown=%d budget=%d",
            len(actionable),
            low,
            REPORT_TEXT_CHAR_BUDGET,
        )
        return text

    def send_report(
        self, horizon_business_days: int | None = None, now: dt.datetime | None = None
    ) -> str:
        # 通知検証モード機能(2026-08追加、およびそのDRY_RUN拡張)の対象外
        # (functional_spec.md 12.13節、週次/月次/四半期レビューは個別銘柄の
        # 売買判断通知ではないため)。本サービスはexecution_context/
        # notification_modeを一切保持せず、常にLineNotificationService._push()
        # を経由しない直接push_messageのため、VALIDATION/DRY_RUNから到達しない。
        if self._line_client is None:
            raise ValueError("line_clientが設定されていません")
        text = self.build_report_text(horizon_business_days, now)
        self._line_client.push_message(text)
        return text
