"""週次改善レビュー(振り返り機能改修)。

毎週月曜、その日の日次評価(EvaluationFunction)完了後に実行する。前週
(月曜00:00〜日曜23:59:59 JST)にevaluated_atが確定した7暦日評価
(EvaluationResult.horizon_calendar_days=7)を集計し、RecommendationType×
rule_version単位でWeeklyReviewMetricsを保存、閾値に基づき改善候補
(ImprovementCandidate)を検出する。十分な証拠がある候補のみGitHub Issueを
自動起票し、Issue作成に成功した場合のみLINE通知する。改善候補が無い週・
GitHub未設定の週は一切通知しない。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from jstock_advisor.config.models import AppConfig, ReviewImprovementConfig
from jstock_advisor.domain.entities.enums import (
    ImprovementAction,
    ImprovementPriority,
    ImprovementTaskStatus,
    RecommendationType,
)
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.improvement import (
    PROBLEM_CATEGORY_EVALUATION_CRITERIA_UNDEFINED,
    PROBLEM_CATEGORY_PERFORMANCE_DEGRADED,
    ImprovementCandidate,
    WeeklyReviewMetrics,
)
from jstock_advisor.domain.evaluation_rules import is_entry_type, is_performance_evaluated_type
from jstock_advisor.domain.improvement_rules import build_candidate_key
from jstock_advisor.domain.jst import evaluation_date_jst, require_timezone_aware, to_jst
from jstock_advisor.infrastructure.aws import improvement_task_tracker as tracker
from jstock_advisor.infrastructure.line.client import LineClient
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.infrastructure.local_repository.evaluation_repository import (
    EvaluationResultRepository,
)
from jstock_advisor.infrastructure.local_repository.improvement_candidate_repository import (
    ImprovementCandidateRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.local_repository.rule_version_repository import (
    RuleVersionRepository,
)
from jstock_advisor.infrastructure.local_repository.weekly_review_metrics_repository import (
    WeeklyReviewMetricsRepository,
)
from jstock_advisor.services import github_issue_service
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.performance_metrics_service import build_metrics_bucket
from jstock_advisor.services.rule_version_service import RuleVersionService

_AUDIT_RULE_VERSION = "review-improvement-v1"  # 本サービス自体のロジックバージョン
_DISCLAIMER = "※最終的な投資判断は利用者が行ってください。"


@dataclass(frozen=True)
class WeeklyImprovementReviewOutcome:
    review_week: str
    period_start: dt.date
    period_end: dt.date
    total_evaluation_results: int
    joined_count: int
    missing_recommendation_ids: list[str] = field(default_factory=list)
    metrics_saved: int = 0
    candidates_detected: int = 0
    issue_eligible_candidates: int = 0
    github_statuses: dict[str, int] = field(default_factory=dict)
    notified_new_issue_count: int = 0


def _iso_week_label(d: dt.date) -> str:
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def _monday_of_iso_week(label: str) -> dt.date:
    year_str, week_str = label.split("-W")
    return dt.date.fromisocalendar(int(year_str), int(week_str), 1)


def _previous_week_label(label: str) -> str:
    return _iso_week_label(_monday_of_iso_week(label) - dt.timedelta(days=7))


def _resolve_review_period(now: dt.datetime) -> tuple[dt.date, dt.date, str]:
    """前週(月曜〜日曜 JST)を対象期間とする(決定事項6)。"""
    today_jst = evaluation_date_jst(now)
    this_week_monday = today_jst - dt.timedelta(days=today_jst.weekday())
    period_end = this_week_monday - dt.timedelta(days=1)
    period_start = period_end - dt.timedelta(days=6)
    return period_start, period_end, _iso_week_label(period_start)


class WeeklyImprovementReviewService:
    def __init__(
        self,
        config: AppConfig,
        evaluation_repository: EvaluationResultRepository | None = None,
        recommendation_repository: RecommendationRepository | None = None,
        weekly_review_metrics_repository: WeeklyReviewMetricsRepository | None = None,
        improvement_candidate_repository: ImprovementCandidateRepository | None = None,
        rule_version_service: RuleVersionService | None = None,
        audit_service: AuditService | None = None,
        line_client: LineClient | None = None,
        github_repo_owner: str | None = None,
        github_repo_name: str | None = None,
        github_secret_arn: str | None = None,
    ) -> None:
        self._config = config
        self._review_config: ReviewImprovementConfig = config.review_improvement
        self._evaluations = evaluation_repository or EvaluationResultRepository()
        self._recommendations = recommendation_repository or RecommendationRepository()
        self._metrics_repo = weekly_review_metrics_repository or WeeklyReviewMetricsRepository()
        self._candidates_repo = (
            improvement_candidate_repository or ImprovementCandidateRepository()
        )
        self._rule_versions = rule_version_service or RuleVersionService(RuleVersionRepository())
        self._audit = audit_service or AuditService(AuditLogRepository())
        self._line_client = line_client
        self._github_repo_owner = github_repo_owner
        self._github_repo_name = github_repo_name
        self._github_secret_arn = github_secret_arn

    def run(self, now: dt.datetime) -> WeeklyImprovementReviewOutcome:
        require_timezone_aware(now)
        period_start, period_end, review_week = _resolve_review_period(now)

        if not self._review_config.weekly_review_enabled:
            return WeeklyImprovementReviewOutcome(
                review_week=review_week,
                period_start=period_start,
                period_end=period_end,
                total_evaluation_results=0,
                joined_count=0,
            )

        candidate_results = self._collect_this_week_evaluations(period_start, period_end)
        joined, missing_ids = self._join_recommendations(candidate_results)

        groups = self._group_by_type_and_rule_version(joined)
        metrics_saved = 0
        candidates: list[ImprovementCandidate] = []
        current_rule_version_cache: dict[RecommendationType, str | None] = {}

        for (rec_type, rule_version), evaluations in groups.items():
            history = self._metrics_repo.list_by_type_version_segment(
                rec_type, rule_version, None
            )
            metrics = self._build_metrics(
                rec_type, rule_version, review_week, period_start, period_end, now, evaluations
            )
            self._metrics_repo.save(metrics)
            metrics_saved += 1

            if rec_type not in current_rule_version_cache:
                current_rule_version_cache[rec_type] = self._resolve_current_rule_version(
                    rec_type
                )
            is_current = self._compare_rule_version(
                current_rule_version_cache[rec_type], rule_version
            )

            candidate = self._detect_candidate(metrics, history, is_current)
            if candidate is not None:
                self._candidates_repo.save(candidate)
                candidates.append(candidate)

        issue_eligible = [c for c in candidates if self._is_issue_eligible(c)]
        github_statuses: dict[str, int] = {}
        notified_new_issue_count = 0
        for candidate in issue_eligible:
            status, is_new = self._process_github_issue(candidate, review_week, now)
            github_statuses[status.value] = github_statuses.get(status.value, 0) + 1
            if is_new and self._line_client is not None:
                self._line_client.push_message(_format_new_issue_notification(candidate))
                notified_new_issue_count += 1
            elif (
                status == ImprovementTaskStatus.CONFIGURATION_ERROR
                and self._line_client is not None
            ):
                self._line_client.push_message(
                    _format_configuration_error_notification(candidate)
                )
            elif (
                status == ImprovementTaskStatus.ISSUE_CREATION_FAILED
                and self._line_client is not None
            ):
                self._line_client.push_message(
                    _format_issue_creation_failed_notification(candidate)
                )

        outcome = WeeklyImprovementReviewOutcome(
            review_week=review_week,
            period_start=period_start,
            period_end=period_end,
            total_evaluation_results=len(candidate_results),
            joined_count=len(joined),
            missing_recommendation_ids=missing_ids,
            metrics_saved=metrics_saved,
            candidates_detected=len(candidates),
            issue_eligible_candidates=len(issue_eligible),
            github_statuses=github_statuses,
            notified_new_issue_count=notified_new_issue_count,
        )
        self._record_audit(outcome, now)
        return outcome

    # --- データ収集・join ---------------------------------------------

    def _collect_this_week_evaluations(
        self, period_start: dt.date, period_end: dt.date
    ) -> list[EvaluationResult]:
        # config/review_improvement.yamlのevaluation_horizon_daysと一致するものだけを
        # 対象にする(値を変更した場合に、異なるホライズンの評価結果が混在しないため)。
        target_horizon = self._review_config.evaluation_horizon_days
        results = []
        for evaluation in self._evaluations.list_all():
            if evaluation.horizon_calendar_days != target_horizon:
                continue
            evaluated_date_jst = to_jst(evaluation.evaluated_at).date()
            if period_start <= evaluated_date_jst <= period_end:
                results.append(evaluation)
        return results

    def _join_recommendations(
        self, evaluations: list[EvaluationResult]
    ) -> tuple[list[tuple[EvaluationResult, Any]], list[str]]:
        joined = []
        missing_ids: list[str] = []
        for evaluation in evaluations:
            recommendation = self._recommendations.get(evaluation.recommendation_id)
            if recommendation is None:
                missing_ids.append(evaluation.recommendation_id)
                continue
            joined.append((evaluation, recommendation))
        return joined, missing_ids

    def _group_by_type_and_rule_version(
        self, joined: list[tuple[EvaluationResult, Any]]
    ) -> dict[tuple[RecommendationType, str], list[EvaluationResult]]:
        groups: dict[tuple[RecommendationType, str], list[EvaluationResult]] = {}
        for evaluation, recommendation in joined:
            key = (recommendation.recommendation_type, recommendation.rule_version)
            groups.setdefault(key, []).append(evaluation)
        return groups

    # --- WeeklyReviewMetrics -------------------------------------------

    def _build_metrics(
        self,
        rec_type: RecommendationType,
        rule_version: str,
        review_week: str,
        period_start: dt.date,
        period_end: dt.date,
        now: dt.datetime,
        evaluations: list[EvaluationResult],
    ) -> WeeklyReviewMetrics:
        bucket = build_metrics_bucket(rec_type.value, evaluations)
        return WeeklyReviewMetrics(
            metrics_id=f"{rec_type.value}|{rule_version}|ALL|{review_week}",
            review_week=review_week,
            recommendation_type=rec_type,
            rule_version=rule_version,
            segment_key=None,
            sample_count=bucket.count,
            conclusive_count=bucket.conclusive_count,
            success_rate_pct=bucket.success_rate_pct,
            average_return_pct=bucket.avg_price_return_pct,
            average_excess_return_pct=bucket.avg_excess_return_pct,
            period_start=period_start,
            period_end=period_end,
            generated_at=now,
        )

    # --- rule_version解決(決定事項12) -----------------------------------

    def _resolve_current_rule_version(self, recommendation_type: RecommendationType) -> str | None:
        active = self._rule_versions.get_active_version()
        if active is not None:
            return active.rule_version
        latest = self._recommendations.get_latest_by_type(recommendation_type)
        return latest.rule_version if latest is not None else None

    @staticmethod
    def _compare_rule_version(current: str | None, candidate_rule_version: str) -> bool | None:
        if current is None:
            return None
        return current == candidate_rule_version

    # --- Candidate判定(決定事項10) ---------------------------------------

    def _detect_candidate(
        self,
        metrics: WeeklyReviewMetrics,
        history: list[WeeklyReviewMetrics],
        is_current: bool | None,
    ) -> ImprovementCandidate | None:
        if is_performance_evaluated_type(metrics.recommendation_type):
            return self._detect_performance_candidate(metrics, history, is_current)
        return self._detect_evaluation_undefined_candidate(metrics, is_current)

    def _detect_performance_candidate(
        self,
        metrics: WeeklyReviewMetrics,
        history: list[WeeklyReviewMetrics],
        is_current: bool | None,
    ) -> ImprovementCandidate | None:
        min_sample = self._review_config.min_sample_count.get(
            metrics.recommendation_type.value, self._review_config.min_sample_count["default"]
        )
        if metrics.conclusive_count < min_sample:
            return None

        min_success_rate = self._review_config.min_success_rate_pct.get(
            metrics.recommendation_type.value
        )
        min_excess_return = self._review_config.min_average_excess_return_pct
        # 超過リターン(自社株リターン-ベンチマークリターン)ベースの悪化検知は
        # ENTRY型(株価上昇=SUCCESS)にのみ適用する。EXIT型(株価下落=SUCCESS)は
        # 良好な下落ほど超過リターンが負に振れるため、そのまま使うと方向が逆になり
        # 誤検出する(2026-08-20、Issue #9・#11のコードレビュー対応)。
        is_entry = is_entry_type(metrics.recommendation_type)
        reason_codes: list[str] = []
        if (
            min_success_rate is not None
            and metrics.success_rate_pct is not None
            and metrics.success_rate_pct < min_success_rate
        ):
            reason_codes.append("SUCCESS_RATE_LOW")
        if (
            is_entry
            and metrics.average_excess_return_pct is not None
            and metrics.average_excess_return_pct < min_excess_return
        ):
            reason_codes.append("EXCESS_RETURN_LOW")
        if not reason_codes:
            return None

        previous, consecutive_bad_weeks = self._compute_history_stats(metrics, history)
        change_points = (
            metrics.success_rate_pct - previous.success_rate_pct
            if previous is not None
            and previous.success_rate_pct is not None
            and metrics.success_rate_pct is not None
            else None
        )

        if consecutive_bad_weeks >= self._review_config.consecutive_bad_weeks_for_issue:
            reason_codes.append("WEEK_OVER_WEEK_DROP")
        if (
            change_points is not None
            and change_points <= -self._review_config.critical_success_rate_drop_threshold_points
        ):
            reason_codes.append("CRITICAL_DROP")
        if (
            is_entry
            and metrics.average_excess_return_pct is not None
            and metrics.average_excess_return_pct
            <= self._review_config.critical_average_excess_return_pct
            and "CRITICAL_DROP" not in reason_codes
        ):
            reason_codes.append("CRITICAL_DROP")

        priority = self._determine_priority(reason_codes)
        candidate_key = build_candidate_key(
            metrics.recommendation_type,
            metrics.rule_version,
            None,
            PROBLEM_CATEGORY_PERFORMANCE_DEGRADED,
        )

        return ImprovementCandidate(
            candidate_id=f"{candidate_key}|{metrics.review_week}",
            candidate_key=candidate_key,
            recommendation_type=metrics.recommendation_type,
            rule_version=metrics.rule_version,
            segment_key=None,
            review_week=metrics.review_week,
            evaluation_period_start=metrics.period_start,
            evaluation_period_end=metrics.period_end,
            sample_count=metrics.sample_count,
            conclusive_count=metrics.conclusive_count,
            success_rate_pct=metrics.success_rate_pct,
            average_return_pct=metrics.average_return_pct,
            average_excess_return_pct=metrics.average_excess_return_pct,
            previous_success_rate_pct=previous.success_rate_pct if previous else None,
            success_rate_change_points=change_points,
            consecutive_bad_weeks=consecutive_bad_weeks,
            priority=priority,
            problem_category=PROBLEM_CATEGORY_PERFORMANCE_DEGRADED,
            reason_codes=tuple(reason_codes),
            expected_improvement_pct=None,
            recommended_action=ImprovementAction.ADJUST_THRESHOLD,
            evidence=(),
            is_current_rule_version=is_current,
        )

    def _detect_evaluation_undefined_candidate(
        self, metrics: WeeklyReviewMetrics, is_current: bool | None
    ) -> ImprovementCandidate | None:
        min_sample = self._review_config.min_sample_count.get(
            metrics.recommendation_type.value, self._review_config.min_sample_count["default"]
        )
        if metrics.sample_count < min_sample or metrics.conclusive_count != 0:
            return None

        candidate_key = build_candidate_key(
            metrics.recommendation_type,
            metrics.rule_version,
            None,
            PROBLEM_CATEGORY_EVALUATION_CRITERIA_UNDEFINED,
        )

        return ImprovementCandidate(
            candidate_id=f"{candidate_key}|{metrics.review_week}",
            candidate_key=candidate_key,
            recommendation_type=metrics.recommendation_type,
            rule_version=metrics.rule_version,
            segment_key=None,
            review_week=metrics.review_week,
            evaluation_period_start=metrics.period_start,
            evaluation_period_end=metrics.period_end,
            sample_count=metrics.sample_count,
            conclusive_count=metrics.conclusive_count,
            success_rate_pct=None,
            average_return_pct=metrics.average_return_pct,
            average_excess_return_pct=None,
            previous_success_rate_pct=None,
            success_rate_change_points=None,
            consecutive_bad_weeks=0,
            priority=ImprovementPriority.B,
            problem_category=PROBLEM_CATEGORY_EVALUATION_CRITERIA_UNDEFINED,
            reason_codes=("EVALUATION_CRITERIA_UNDEFINED",),
            expected_improvement_pct=None,
            recommended_action=ImprovementAction.DEFINE_EVALUATION_CRITERIA,
            evidence=(
                f"{metrics.sample_count}件のうち、自動評価の対象外(INCONCLUSIVE)が"
                f"{metrics.sample_count - metrics.conclusive_count}件でした。",
            ),
            is_current_rule_version=is_current,
        )

    def _compute_history_stats(
        self, metrics: WeeklyReviewMetrics, history: list[WeeklyReviewMetrics]
    ) -> tuple[WeeklyReviewMetrics | None, int]:
        """historyは同一type×rule_versionの過去週(review_week降順、この週は含まない)。
        previous=直前の週(review_weekが厳密に1週前の場合のみ)、consecutive_bad_weeks=
        この週を含め、間断なく閾値を割り続けている週数。"""
        expected_week = metrics.review_week
        previous: WeeklyReviewMetrics | None = None
        consecutive = 1  # この週自体が既に問題週として呼ばれている前提
        for entry in history:
            expected_week = _previous_week_label(expected_week)
            if entry.review_week != expected_week:
                break
            if previous is None:
                previous = entry
            if not self._breaches_threshold(entry):
                break
            consecutive += 1
        return previous, consecutive

    def _breaches_threshold(self, metrics: WeeklyReviewMetrics) -> bool:
        min_success_rate = self._review_config.min_success_rate_pct.get(
            metrics.recommendation_type.value
        )
        if (
            min_success_rate is not None
            and metrics.success_rate_pct is not None
            and metrics.success_rate_pct < min_success_rate
        ):
            return True
        # EXIT型は超過リターンの方向が逆になるため対象外(_detect_performance_
        # candidate()と同じ理由、2026-08-20、Issue #9・#11のコードレビュー対応)。
        if not is_entry_type(metrics.recommendation_type):
            return False
        min_excess_return = self._review_config.min_average_excess_return_pct
        return (
            metrics.average_excess_return_pct is not None
            and metrics.average_excess_return_pct < min_excess_return
        )

    @staticmethod
    def _determine_priority(reason_codes: list[str]) -> ImprovementPriority:
        if "CRITICAL_DROP" in reason_codes:
            return ImprovementPriority.A
        if "WEEK_OVER_WEEK_DROP" in reason_codes:
            return ImprovementPriority.B
        return ImprovementPriority.C

    def _is_issue_eligible(self, candidate: ImprovementCandidate) -> bool:
        if candidate.problem_category == PROBLEM_CATEGORY_EVALUATION_CRITERIA_UNDEFINED:
            return True
        return (
            "WEEK_OVER_WEEK_DROP" in candidate.reason_codes
            or "CRITICAL_DROP" in candidate.reason_codes
        )

    # --- GitHub連携 --------------------------------------------------

    def _process_github_issue(
        self, candidate: ImprovementCandidate, review_week: str, now: dt.datetime
    ) -> tuple[ImprovementTaskStatus, bool]:
        before = tracker.get_improvement_task(candidate.candidate_key)
        before_issue_number = before.get("github_issue_number") if before else None

        status = github_issue_service.process_candidate(
            candidate,
            review_week,
            now,
            self._review_config,
            self._github_repo_owner or "",
            self._github_repo_name or "",
            self._github_secret_arn,
        )

        after = tracker.get_improvement_task(candidate.candidate_key)
        after_issue_number = after.get("github_issue_number") if after else None
        is_new = (
            status == ImprovementTaskStatus.ISSUE_CREATED
            and before_issue_number != after_issue_number
        )
        return status, is_new

    # --- 監査ログ -------------------------------------------------------

    def _record_audit(self, outcome: WeeklyImprovementReviewOutcome, now: dt.datetime) -> None:
        self._audit.record(
            decision_type="weekly_improvement_review",
            stock_code=None,
            input_values={
                "review_week": outcome.review_week,
                "period_start": outcome.period_start.isoformat(),
                "period_end": outcome.period_end.isoformat(),
            },
            calculation_formulas={},
            output_values={
                "total_evaluation_results": outcome.total_evaluation_results,
                "joined_count": outcome.joined_count,
                "weekly_review_recommendation_missing_count": len(
                    outcome.missing_recommendation_ids
                ),
                "weekly_review_recommendation_missing_ids": outcome.missing_recommendation_ids,
                "metrics_saved": outcome.metrics_saved,
                "candidates_detected": outcome.candidates_detected,
                "issue_eligible_candidates": outcome.issue_eligible_candidates,
                "github_statuses": outcome.github_statuses,
                "notified_new_issue_count": outcome.notified_new_issue_count,
            },
            data_sources=[],
            rule_version=_AUDIT_RULE_VERSION,
            timestamp=now,
        )


def _fmt_pct(value: float | None) -> str:
    return "評価対象外" if value is None else f"{value:.1f}%"


def _format_new_issue_notification(candidate: ImprovementCandidate) -> str:
    task = tracker.get_improvement_task(candidate.candidate_key)
    issue_number = task.get("github_issue_number") if task else None
    issue_line = (
        f"GitHub Issue: #{issue_number}" if issue_number is not None else "GitHub Issue: (取得失敗)"
    )
    lines = [
        "🤖 ルール改善タスクを登録しました",
        f"対象: {candidate.recommendation_type.value}判定",
        f"理由: 直近週の評価{candidate.sample_count}件で成功率"
        f"{_fmt_pct(candidate.success_rate_pct)}、"
        f"平均超過リターン{_fmt_pct(candidate.average_excess_return_pct)}",
        issue_line,
        "推奨アクション: Issueの改善仮説と根拠を確認してください。",
        "",
        _DISCLAIMER,
    ]
    return "\n".join(lines)


def _format_configuration_error_notification(candidate: ImprovementCandidate) -> str:
    return (
        "⚠️ 改善候補を検出しましたが、GitHub連携の設定に問題があるため登録できません"
        "でした。\n"
        f"対象: {candidate.recommendation_type.value}判定\n"
        "運用者による設定確認が必要です(docs/operations_manual.md参照)。\n\n"
        f"{_DISCLAIMER}"
    )


def _format_issue_creation_failed_notification(candidate: ImprovementCandidate) -> str:
    return (
        "⚠️ 改善候補を検出しましたが、GitHub Issueの登録に失敗しました。\n"
        f"対象: {candidate.recommendation_type.value}判定\n"
        "しばらくしてから次回の週次レビューで再試行されます。\n\n"
        f"{_DISCLAIMER}"
    )
