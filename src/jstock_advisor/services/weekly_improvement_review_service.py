"""週次改善レビュー(振り返り機能改修)。

毎週月曜、その日の日次評価(EvaluationFunction)完了後に実行する。前週
(月曜〜日曜 JST)を**評価基準日(EvaluationResult.evaluation_date)**とする
7暦日評価(EvaluationResult.horizon_calendar_days=7)を集計し、RecommendationType×
rule_version単位でWeeklyReviewMetricsを保存、閾値に基づき改善候補
(ImprovementCandidate)を検出する。十分な証拠がある候補のみGitHub Issueを
自動起票し、Issue作成に成功した場合のみLINE通知する。改善候補が無い週・
GitHub未設定の週は一切通知しない。

★ 集計軸は`evaluation_date`である(Issue #114 Phase B2で`evaluated_at`から
  変更した)。加えて、遅延して処理された評価をその基準日の週へ反映するため、
  直近history_weeks_for_comparison週のmetricsを毎回作り直す。
  ★ 過去週では候補検出・GitHub Issue起票・LINE通知を一切行わない
    (正しいmetricsを後から作れることと、その時点で改善判断してよいことは別問題。
     catch-up中の起票抑止そのものはPhase B3の担当であり本モジュールにはまだ無い)。
"""

from __future__ import annotations

import datetime as dt
import logging
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
from jstock_advisor.domain.evaluation_rules import (
    is_entry_type,
    is_evaluation_excluded_type,
    is_performance_evaluated_type,
)
from jstock_advisor.domain.improvement_rules import build_candidate_key
from jstock_advisor.domain.jst import evaluation_date_jst, require_timezone_aware
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

logger = logging.getLogger(__name__)
# Issue #413: INFO を CloudWatch Logs へ出力する(Lambda の root logger の既定は WARNING で、
# module が宣言しないと INFO は出ない)。出力する値に、生の owner / holding_id 等を含めない
# (#135 / #416)。有効化の時点の PII 確認は PR に記録した。
logger.setLevel(logging.INFO)

_AUDIT_RULE_VERSION = "review-improvement-v1"  # 本サービス自体のロジックバージョン
_DISCLAIMER = "※最終的な投資判断は利用者が行ってください。"

# Issue #377 PR #379是正: _join_recommendations()がRecommendationRepository.get()を
# 対象件数ぶん繰り返すN+1になっていた。get_many()(BatchGetItem)へ切り替えるが、
# 全IDを一度に渡すと戻り値のdictが対象週の全Recommendationを同時保持することに
# なる(Recommendationは1件あたり実測約26KB。jstock-recommendations実測)。
# evaluationsをこの件数ずつのchunkに区切り、chunkごとにget_many()を呼んで
# 直ちにjoinすることでピークメモリを1chunk分に有界化する(iter_all()と同じ
# 設計思想)。DynamoDbCollectionStore.get_many()自体もBatchGetItemの上限
# (100件/リクエスト)でAPI呼び出しを内部chunkするため、本値をそれと揃えることで
# 1chunk = 1 BatchGetItemリクエストになり、無駄な往復を作らない。
_RECOMMENDATION_JOIN_CHUNK_SIZE = 100


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
    # Issue #114 Phase B2: 過去週を作り直した件数。0でも「やらなかった」ではなく
    # 「作り直す対象が無かった」を意味する(無音にしないため監査へも出す)。
    past_weeks_metrics_recomputed: int = 0
    # 週ラベル -> その週で書き直した行数。総数だけでは「どの週を触ったか」が
    # 分からず、上書きの影響範囲を後から検証できないため併せて残す。
    past_weeks_metrics_recomputed_by_week: dict[str, int] = field(default_factory=dict)


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
        self._candidates_repo = improvement_candidate_repository or ImprovementCandidateRepository()
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

        # Issue #377: 当該週+過去history_weeks_for_comparison週の窓を先に
        # 全部確定してから、1回のstreaming scanでまとめて集める(従来は
        # 週ごとに個別へ全件走査していた。1 + weeks_back回 -> 1回)。
        # 窓の計算自体は純粋計算であり副作用を持たない。
        weeks_back = self._review_config.history_weeks_for_comparison
        windows: list[tuple[str, dt.date, dt.date]] = [
            (review_week, period_start, period_end)
        ]
        past_labels: list[str] = []
        label = review_week
        for _ in range(max(weeks_back, 0)):
            label = _previous_week_label(label)
            past_period_start = _monday_of_iso_week(label)
            windows.append(
                (label, past_period_start, past_period_start + dt.timedelta(days=6))
            )
            past_labels.append(label)

        buckets = self._collect_evaluations_for_windows(windows)
        candidate_results = buckets[review_week]
        joined, missing_ids = self._join_recommendations(candidate_results)

        groups = self._group_by_type_and_rule_version(joined)
        metrics_saved = 0
        candidates: list[ImprovementCandidate] = []
        current_rule_version_cache: dict[RecommendationType, str | None] = {}

        for (rec_type, rule_version), evaluations in groups.items():
            history = self._metrics_repo.list_by_type_version_segment(rec_type, rule_version, None)
            metrics = self._build_metrics(
                rec_type, rule_version, review_week, period_start, period_end, now, evaluations
            )
            self._metrics_repo.save(metrics)
            metrics_saved += 1

            if rec_type not in current_rule_version_cache:
                current_rule_version_cache[rec_type] = self._resolve_current_rule_version(rec_type)
            is_current = self._compare_rule_version(
                current_rule_version_cache[rec_type], rule_version
            )

            candidate = self._detect_candidate(metrics, history, is_current)
            if candidate is not None:
                self._candidates_repo.save(candidate)
                candidates.append(candidate)

        # 通知検証モード機能(2026-08追加、およびそのDRY_RUN拡張)の対象外
        # (functional_spec.md 12.13節、週次改善レビューは個別銘柄の売買判断
        # 通知ではないため)。本サービスはexecution_context/notification_modeを
        # 一切保持せず、以下のpush_messageはLineNotificationService._push()を
        # 経由しない直接呼び出しのため、VALIDATION/DRY_RUNから到達しない。
        #
        # Issue #50(LINE文字数上限)についても、_push()を経由しない
        # 「文書化された例外経路」として扱う。以下の3種の通知本文はいずれも
        # 固定長テンプレート(_format_*_notification、概ね200文字程度)であり、
        # 件数に比例して伸びる要素を持たないため、要約処理を持たない。
        # 上限違反の検出自体はLineClient側(protocol validation)が担保する。
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
                self._line_client.push_message(_format_configuration_error_notification(candidate))
            elif (
                status == ImprovementTaskStatus.ISSUE_CREATION_FAILED
                and self._line_client is not None
            ):
                self._line_client.push_message(
                    _format_issue_creation_failed_notification(candidate)
                )

        # Issue #114 Phase B2: 遅延して処理された評価はその基準日が属する過去週へ
        # 計上されるべきだが、その週の集計は既に走り終わっている。ここで作り直す。
        # ★ 起票・通知の後に置くのは、過去週の再集計が今週の起票判断へ影響しない
        #   ことを実行順序でも明らかにするため(metricsの再集計と自動起票の分離)。
        past_weeks_recomputed, past_weeks_detail = self._recompute_past_weeks_from_buckets(
            past_labels, buckets, now
        )

        outcome = WeeklyImprovementReviewOutcome(
            review_week=review_week,
            period_start=period_start,
            period_end=period_end,
            total_evaluation_results=len(candidate_results),
            joined_count=len(joined),
            missing_recommendation_ids=missing_ids,
            metrics_saved=metrics_saved,
            past_weeks_metrics_recomputed=past_weeks_recomputed,
            past_weeks_metrics_recomputed_by_week=past_weeks_detail,
            candidates_detected=len(candidates),
            issue_eligible_candidates=len(issue_eligible),
            github_statuses=github_statuses,
            notified_new_issue_count=notified_new_issue_count,
        )
        self._record_audit(outcome, now)
        return outcome

    # --- データ収集・join ---------------------------------------------

    def _collect_evaluations_for_windows(
        self, windows: list[tuple[str, dt.date, dt.date]]
    ) -> dict[str, list[EvaluationResult]]:
        """1回のstreaming scanで、複数の対象週(当該週+過去N週)へ同時に振り分ける
        (Issue #377)。

        `windows`は`(review_week_label, period_start, period_end)`の列。
        呼び出し側は互いに重複しない7日間の集合を渡すこと(呼び出し側が
        `_previous_week_label()`の連鎖で作るため、設計上必ず非重複・連続する)。

        evaluation_dateがどのwindowにも該当しない評価は捨てる。これは
        `_collect_evaluations_for_period()`を対象週ごとに個別に呼んだ場合と
        集合として同じ結果になる(個別に絞り込んで含まれないレコードは、
        まとめて絞り込んでも含まれない。windowsが非重複であるため、1件の
        evaluationが複数のwindowへ二重に入ることもない)。

        Issue #113と同じ理由でiter_all()を使う(全ページをlistへ保持しない)。
        Issue #377: 従来は対象週ごとに`list_all()`(または個別filter)を
        呼んでおり、history_weeks_for_comparison分だけ全件走査が繰り返されて
        いた(1 + weeks_back回)。本メソッドは1回の走査で済ませる。
        """
        target_horizon = self._review_config.evaluation_horizon_days
        buckets: dict[str, list[EvaluationResult]] = {label: [] for label, _, _ in windows}
        scanned = matched = 0
        for evaluation in self._evaluations.iter_all():
            scanned += 1
            if scanned % 10_000 == 0:
                logger.info(
                    "weekly review single-pass scan progress scanned=%d matched=%d",
                    scanned,
                    matched,
                )
            if evaluation.horizon_calendar_days != target_horizon:
                continue
            for label, period_start, period_end in windows:
                if period_start <= evaluation.evaluation_date <= period_end:
                    buckets[label].append(evaluation)
                    matched += 1
                    break  # windowsは非重複なので複数バケツへは入らない
        logger.info(
            "weekly review single-pass scan done scanned=%d matched=%d windows=%d",
            scanned,
            matched,
            len(windows),
        )
        return buckets

    def _recompute_past_weeks_from_buckets(
        self,
        past_labels: list[str],
        buckets: dict[str, list[EvaluationResult]],
        now: dt.datetime,
    ) -> tuple[int, dict[str, int]]:
        """buckets(既に1回のstreaming scanで振り分け済み)から、直近
        history_weeks_for_comparison週分のmetricsを作り直す(Issue #377。
        旧`_recompute_past_weeks`から「窓の計算」と「表の再scan」を除いたもの。
        upsertのロジック・冪等性・以下の設計判断はいずれも変更していない)。

        遅延して処理された評価は、その基準日が属する過去週へ計上されるべきだが、
        その週の集計は既に走り終わっている。`evaluation_date`は決定論的で、
        遅れて処理しても評価値はon-time実行と一致するため、後から作り直した値は
        「捏造」ではなく**本来あるべきだった値**である(Phase A 6節の判断)。

        ★ 行うのはmetricsのupsertだけである。過去週では候補検知・GitHub Issue
          起票・LINE通知を**一切行わない**。正しいmetricsを後から作れることと、
          その時点で改善判断してよいことは別問題であるため(Phase A 7節)。
          catch-up中の起票抑止そのものはPhase B3の担当であり本実装には含まない。

        ★ EvaluationResultは1件も書き換えない。`evaluated_at`は「処理した日時」
          として正しく記録されているままにする。

        ★ 冪等である。metrics_idは`{型}|{ルール版}|ALL|{週}`で決定的、対象週は
          現在週から機械的に導出され、値は保存済みEvaluationResultだけから決まる。
          同じ入力で何度実行しても同じ行になる(generated_atのみnowで動く)。

        ★ 反映後の**初回**の週次レビューでは、旧軸(evaluated_at)で作られていた
          直近4週の行が新軸の値へ**1回だけ書き換わる**。これは想定内である。
          2回目以降は同じ値の上書きとなり、内容は変化しない。

        戻り値は(作り直した行の総数, 週ごとの件数)。週ごとの件数はINFOログと
        監査へ残す(どの週を触ったかが後から分からないと、上書きの影響範囲を
        検証できないため)。
        """
        if not past_labels:
            logger.info(
                "weekly review past-week recompute skipped history_weeks_for_comparison=%d",
                self._review_config.history_weeks_for_comparison,
            )
            return 0, {}
        # 既存行は「古い軸(evaluated_at)で作られた行が、新しい軸では0件になる」
        # 組み合わせを拾うために使う。放置すると誤った母数の行が残り続ける。
        existing = self._metrics_repo.list_all()
        recomputed = 0
        per_week: dict[str, int] = {}
        for label in past_labels:
            period_start = _monday_of_iso_week(label)
            period_end = period_start + dt.timedelta(days=6)
            evaluations = buckets[label]
            joined, _missing = self._join_recommendations(evaluations)
            groups = self._group_by_type_and_rule_version(joined)
            for stale in existing:
                if stale.review_week != label:
                    continue
                # 0件として上書きする。sample_count=0の行はsuccess_rate等がNoneに
                # なり_breaches_threshold()はFalseを返すため、誤検知の方向へは
                # 働かない(連続悪化週のカウントを不当に伸ばさない)。
                groups.setdefault((stale.recommendation_type, stale.rule_version), [])
            for (rec_type, rule_version), grouped in groups.items():
                self._metrics_repo.save(
                    self._build_metrics(
                        rec_type, rule_version, label, period_start, period_end, now, grouped
                    )
                )
                recomputed += 1
                per_week[label] = per_week.get(label, 0) + 1
        # 上書きは「どの週を何行」触ったかまで残す。総数だけでは影響範囲を
        # 後から検証できない(0件でも「対象が無かった」として記録する)。
        logger.info(
            "weekly review past-week recompute weeks_back=%d rows=%d per_week=%s",
            len(past_labels),
            recomputed,
            per_week,
        )
        return recomputed, per_week

    def _join_recommendations(
        self, evaluations: list[EvaluationResult]
    ) -> tuple[list[tuple[EvaluationResult, Any]], list[str]]:
        """evaluationsへ対応するRecommendationをjoinする(Issue #377 PR #379是正)。

        `RecommendationRepository.get_many()`(BatchGetItem)を使い、対象件数ぶん
        `get()`(GetItem)を繰り返さない(N+1回避)。evaluationsを
        `_RECOMMENDATION_JOIN_CHUNK_SIZE`件ずつのchunkへ区切り、chunkごとに
        `get_many()`を1回呼んでから直ちにjoinし、次のchunkへ進む。全件ぶんの
        Recommendationを同時に保持しない(ピークメモリは1chunk分に有界)。

        ★ evaluationsの順序をそのまま保持する。`joined`は入力の順序で追加され、
          `missing_ids`も見つからなかった評価ごとに(重複IDでも1件ずつ)追加する。
          旧実装(`get()`を1件ずつ呼ぶ版)と、同じ入力に対して同じ`joined`・
          同じ`missing_ids`を返す。
        ★ 同じrecommendation_idを複数のevaluationが参照する場合(horizonの
          異なる複数評価が同一推奨を指す等)、`get_many()`はID単位で重複排除して
          1回だけ取得する。`joined`内では同じRecommendationオブジェクトが
          複数のタプルから参照される(Recommendationの取得回数は増えない)。
        """
        joined: list[tuple[EvaluationResult, Any]] = []
        missing_ids: list[str] = []
        for start in range(0, len(evaluations), _RECOMMENDATION_JOIN_CHUNK_SIZE):
            chunk = evaluations[start : start + _RECOMMENDATION_JOIN_CHUNK_SIZE]
            found = self._recommendations.get_many(
                evaluation.recommendation_id for evaluation in chunk
            )
            for evaluation in chunk:
                recommendation = found.get(evaluation.recommendation_id)
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
            # Issue #270: 評価対象外であることが**仕様として妥当**な型は起票しない。
            # 起票しても直しようがなく(直すべき未整備ではない)、
            # 同じIssueが**毎週立ち続ける**だけになる(#10 / #241 がその実例)。
            # ★ 改善候補そのものは従来どおり生成する。止めるのは**起票だけ**であり、
            #   週次指標からその型が消えるわけではない。
            return not is_evaluation_excluded_type(candidate.recommendation_type)
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
                # Issue #114 Phase B2: 過去週を作り直した件数。0でも記録する
                # (再集計を「やらなかった」のか「対象が無かった」のかを
                #  後から区別できるようにする)。
                "past_weeks_metrics_recomputed": outcome.past_weeks_metrics_recomputed,
                "past_weeks_metrics_recomputed_by_week": (
                    outcome.past_weeks_metrics_recomputed_by_week
                ),
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
