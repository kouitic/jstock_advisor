"""services/weekly_improvement_review_service.pyのテスト(振り返り機能改修)。

GitHub連携部分(services.github_issue_service.process_candidate)はモック化し、
本サービス自身の責務(週次対象期間の決定・WeeklyReviewMetrics生成・rule_version別
分離・Candidate判定・LINE通知タイミング)のみを検証する。GitHub API自体の
挙動はtest_github_issue_service.pyで別途検証済み。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EvaluationLabel,
    ImprovementTaskStatus,
    RecommendationType,
)
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.rule_version import RuleVersion
from jstock_advisor.infrastructure.aws import improvement_task_tracker as tracker
from jstock_advisor.infrastructure.line.client import ConsoleLineClient
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
from jstock_advisor.services import weekly_improvement_review_service as module
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.performance_metrics_service import build_metrics_bucket
from jstock_advisor.services.rule_version_service import RuleVersionService
from jstock_advisor.services.weekly_improvement_review_service import (
    WeeklyImprovementReviewService,
    _resolve_review_period,
)

_REGION = "ap-northeast-1"
# 2026-08-10はJSTで月曜。週次レビュー実行日として使う。
_RUN_AT = dt.datetime(2026, 8, 10, 10, 0, tzinfo=dt.UTC)  # JST 19:00


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch, lambda_runtime_env: None, create_collection_table):
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        dynamo = boto3.client("dynamodb", region_name=_REGION)
        dynamo.create_table(
            TableName="jstock-improvement_tasks",
            KeySchema=[{"AttributeName": "candidate_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "candidate_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        # Issue #367(b): Lambda実行環境では6つのrepositoryも本番と同じDynamoDBを使う
        create_collection_table("evaluation_results.json", "evaluation_id")
        create_collection_table("recommendations.json", "recommendation_id")
        create_collection_table("weekly_review_metrics.json", "metrics_id")
        create_collection_table("improvement_candidates.json", "candidate_id")
        create_collection_table("rule_versions.json", "rule_version")
        create_collection_table("audit_log.json", "audit_id")
        yield


@pytest.fixture
def repos(tmp_path: Path):
    return {
        "evaluation": EvaluationResultRepository(store_dir=tmp_path),
        "recommendation": RecommendationRepository(store_dir=tmp_path),
        "metrics": WeeklyReviewMetricsRepository(store_dir=tmp_path),
        "candidate": ImprovementCandidateRepository(store_dir=tmp_path),
        "rule_version": RuleVersionRepository(store_dir=tmp_path),
        "audit": AuditLogRepository(store_dir=tmp_path),
    }


def _build_service(
    repos: dict,
    line_client=None,
    issue_creation_enabled: bool = False,
    evaluation_horizon_days: int | None = None,
) -> WeeklyImprovementReviewService:
    config = load_config()
    review_config_updates: dict = {"issue_creation_enabled": issue_creation_enabled}
    if evaluation_horizon_days is not None:
        review_config_updates["evaluation_horizon_days"] = evaluation_horizon_days
    review_config = config.review_improvement.model_copy(update=review_config_updates)
    config = config.model_copy(update={"review_improvement": review_config})
    return WeeklyImprovementReviewService(
        config=config,
        evaluation_repository=repos["evaluation"],
        recommendation_repository=repos["recommendation"],
        weekly_review_metrics_repository=repos["metrics"],
        improvement_candidate_repository=repos["candidate"],
        rule_version_service=RuleVersionService(repos["rule_version"]),
        audit_service=AuditService(repos["audit"]),
        line_client=line_client,
        github_repo_owner="owner",
        github_repo_name="repo",
        github_secret_arn="arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:x",
    )


def _recommendation(
    rec_id: str,
    rec_type: RecommendationType,
    rule_version: str,
    recommended_at: dt.datetime,
) -> Recommendation:
    return Recommendation(
        recommendation_id=rec_id,
        stock_code="1234",
        stock_name="test",
        recommended_at=recommended_at,
        recommendation_type=rec_type,
        price_at_recommendation=Decimal("1000"),
        confidence=ConfidenceLevel.HIGH,
        rule_version=rule_version,
    )


def _evaluation(
    eval_id: str,
    rec_id: str,
    label: EvaluationLabel,
    evaluated_at: dt.datetime,
    price_return_pct: float = 1.0,
    excess_return_pct: float | None = 1.0,
    horizon_calendar_days: int = 7,
) -> EvaluationResult:
    return EvaluationResult(
        evaluation_id=eval_id,
        recommendation_id=rec_id,
        horizon_calendar_days=horizon_calendar_days,
        evaluated_at=evaluated_at,
        evaluation_date=evaluated_at.date(),
        price_at_evaluation=Decimal("1010"),
        price_return_pct=price_return_pct,
        excess_return_pct=excess_return_pct,
        evaluation_label=label,
        label_evidence="x",
    )


def _seed_bad_week(
    repos: dict,
    rec_type: RecommendationType,
    rule_version: str,
    count_success: int,
    count_fail: int,
    evaluated_at: dt.datetime,
    prefix: str,
) -> None:
    """success_rate_pctが閾値未満になるよう、成功/失敗ラベルの評価をシードする。"""
    for i in range(count_success):
        rec_id = f"{prefix}-s{i}"
        repos["recommendation"].save(_recommendation(rec_id, rec_type, rule_version, evaluated_at))
        repos["evaluation"].save(
            _evaluation(f"{prefix}-se{i}", rec_id, EvaluationLabel.SUCCESS, evaluated_at)
        )
    for i in range(count_fail):
        rec_id = f"{prefix}-f{i}"
        repos["recommendation"].save(_recommendation(rec_id, rec_type, rule_version, evaluated_at))
        repos["evaluation"].save(
            _evaluation(f"{prefix}-fe{i}", rec_id, EvaluationLabel.PRICE_TOO_HIGH, evaluated_at)
        )


# --- 週次対象期間の決定 ------------------------------------------------


def test_resolve_review_period_is_previous_monday_to_sunday() -> None:
    period_start, period_end, review_week = _resolve_review_period(_RUN_AT)
    assert period_start.weekday() == 0  # 月曜
    assert period_end.weekday() == 6  # 日曜
    assert (period_end - period_start).days == 6
    assert review_week == f"{period_start.isocalendar()[0]}-W{period_start.isocalendar()[1]:02d}"


def test_evaluation_date_last_week_but_evaluated_at_this_week_is_included(aws_env, repos) -> None:
    period_start, period_end, review_week = _resolve_review_period(_RUN_AT)
    # evaluation_date(基準日)は前週より前だが、evaluated_at(確定日時)は対象週内
    stale_evaluated_at = dt.datetime.combine(
        period_start, dt.time(10, 0), tzinfo=dt.UTC
    )  # 対象週の月曜に確定
    recommended_at = dt.datetime.combine(
        period_start - dt.timedelta(days=14), dt.time(3, 0), tzinfo=dt.UTC
    )
    repos["recommendation"].save(
        _recommendation("r1", RecommendationType.BUY, "v1", recommended_at)
    )
    repos["evaluation"].save(_evaluation("e1", "r1", EvaluationLabel.SUCCESS, stale_evaluated_at))

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.review_week == review_week
    assert outcome.joined_count == 1


def test_evaluation_confirmed_next_week_is_excluded_from_this_week(aws_env, repos) -> None:
    period_start, period_end, _ = _resolve_review_period(_RUN_AT)
    next_week_evaluated_at = dt.datetime.combine(
        period_end + dt.timedelta(days=2), dt.time(10, 0), tzinfo=dt.UTC
    )
    repos["recommendation"].save(_recommendation("r1", RecommendationType.BUY, "v1", period_start))
    repos["evaluation"].save(
        _evaluation("e1", "r1", EvaluationLabel.SUCCESS, next_week_evaluated_at)
    )

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.joined_count == 0


def test_monday_catchup_evaluation_is_excluded_from_same_day_review(aws_env, repos) -> None:
    """月曜のEvaluationFunctionで確定したcatch-up分(evaluated_at=当日月曜)は、
    その日19時のレビュー(対象は前週)には含まれない(決定事項6)。"""
    today_jst_evaluated_at = _RUN_AT  # レビュー実行と同じ月曜に確定
    repos["recommendation"].save(
        _recommendation("r1", RecommendationType.BUY, "v1", today_jst_evaluated_at)
    )
    repos["evaluation"].save(
        _evaluation("e1", "r1", EvaluationLabel.SUCCESS, today_jst_evaluated_at)
    )

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.joined_count == 0


# --- WeeklyReviewMetrics: rule_version別分離 -------------------------------


def test_metrics_are_separated_by_rule_version(aws_env, repos) -> None:
    period_start, _, review_week = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    _seed_bad_week(repos, RecommendationType.BUY, "v10", 5, 5, mid_week, "v10batch")
    _seed_bad_week(repos, RecommendationType.BUY, "v11", 8, 2, mid_week, "v11batch")

    service = _build_service(repos)
    service.run(_RUN_AT)

    rows = repos["metrics"].list_by_type_version_segment(RecommendationType.BUY, "v10", None)
    assert len(rows) == 1
    assert rows[0].sample_count == 10
    rows_v11 = repos["metrics"].list_by_type_version_segment(RecommendationType.BUY, "v11", None)
    assert len(rows_v11) == 1
    assert rows_v11[0].sample_count == 10
    assert rows_v11[0].success_rate_pct == pytest.approx(80.0)


def test_metrics_saved_even_when_not_a_candidate(aws_env, repos) -> None:
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    _seed_bad_week(repos, RecommendationType.BUY, "v1", 20, 0, mid_week, "good")

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    rows = repos["metrics"].list_by_type_version_segment(RecommendationType.BUY, "v1", None)
    assert len(rows) == 1
    assert outcome.candidates_detected == 0


# --- 前週比較(同一rule_versionのみ) ----------------------------------------


def test_previous_week_comparison_uses_same_rule_version_only(aws_env, repos) -> None:
    period_start, _, review_week = _resolve_review_period(_RUN_AT)
    previous_week_label = module._previous_week_label(review_week)
    previous_monday = module._monday_of_iso_week(previous_week_label)

    # 前週(v10)は正常データとして保存(WeeklyReviewMetricsを直接投入)
    from jstock_advisor.domain.entities.improvement import WeeklyReviewMetrics

    repos["metrics"].save(
        WeeklyReviewMetrics(
            metrics_id=f"BUY|v10|ALL|{previous_week_label}",
            review_week=previous_week_label,
            recommendation_type=RecommendationType.BUY,
            rule_version="v10",
            segment_key=None,
            sample_count=20,
            conclusive_count=20,
            success_rate_pct=70.0,
            average_return_pct=1.0,
            average_excess_return_pct=1.0,
            period_start=previous_monday,
            period_end=previous_monday + dt.timedelta(days=6),
            generated_at=_RUN_AT,
        )
    )

    # 今週はv11(新ルール)のみで低成績、初週扱いになるはず
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    _seed_bad_week(repos, RecommendationType.BUY, "v11", 2, 18, mid_week, "newver")

    service = _build_service(repos)
    service.run(_RUN_AT)

    candidates = repos["candidate"].list_all()
    assert len(candidates) == 1
    assert candidates[0].previous_success_rate_pct is None
    assert candidates[0].success_rate_change_points is None
    assert candidates[0].consecutive_bad_weeks == 1


# --- Candidate判定 -----------------------------------------------------


def test_insufficient_sample_count_is_not_a_candidate(aws_env, repos) -> None:
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    _seed_bad_week(repos, RecommendationType.SELL, "v1", 1, 2, mid_week, "few")  # SELL閾値=10

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.candidates_detected == 0


def test_single_bad_week_is_candidate_but_not_issue_eligible(aws_env, repos) -> None:
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    _seed_bad_week(repos, RecommendationType.BUY, "v1", 5, 15, mid_week, "onebad")

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.candidates_detected == 1
    assert outcome.issue_eligible_candidates == 0
    candidate = repos["candidate"].list_all()[0]
    assert candidate.problem_category == "PERFORMANCE_DEGRADED"
    assert "SUCCESS_RATE_LOW" in candidate.reason_codes


def test_consecutive_bad_weeks_becomes_issue_eligible(aws_env, repos) -> None:
    period_start, _, review_week = _resolve_review_period(_RUN_AT)
    previous_week_label = module._previous_week_label(review_week)
    previous_monday = module._monday_of_iso_week(previous_week_label)

    from jstock_advisor.domain.entities.improvement import WeeklyReviewMetrics

    repos["metrics"].save(
        WeeklyReviewMetrics(
            metrics_id=f"BUY|v1|ALL|{previous_week_label}",
            review_week=previous_week_label,
            recommendation_type=RecommendationType.BUY,
            rule_version="v1",
            segment_key=None,
            sample_count=20,
            conclusive_count=20,
            success_rate_pct=30.0,  # 前週も閾値(50.0)未満
            average_return_pct=-1.0,
            average_excess_return_pct=-2.0,
            period_start=previous_monday,
            period_end=previous_monday + dt.timedelta(days=6),
            generated_at=_RUN_AT,
        )
    )

    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    _seed_bad_week(repos, RecommendationType.BUY, "v1", 5, 15, mid_week, "twobad")

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.issue_eligible_candidates == 1
    candidate = repos["candidate"].list_all()[0]
    assert candidate.consecutive_bad_weeks == 2
    assert "WEEK_OVER_WEEK_DROP" in candidate.reason_codes


def test_evaluation_undefined_candidate_is_issue_eligible_on_first_week(aws_env, repos) -> None:
    """WATCHはEXIT型評価基準を持つに至ったため(Rule Improvement対応2026-08、
    Issue #9)、ここでは評価基準が引き続き未定義のWATCH_BEFORE_EARNINGSを使う
    (Issue #10、2026-08-20時点で保留中)。この保留がWATCH/REVIEW対応(Issue #9・
    #11)の影響を受けず、引き続き「自動評価の対象外」経路
    (EVALUATION_CRITERIA_UNDEFINED)を使うことの回帰確認を兼ねる。
    """
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    for i in range(15):  # default閾値=10
        rec_id = f"watch{i}"
        repos["recommendation"].save(
            _recommendation(rec_id, RecommendationType.WATCH_BEFORE_EARNINGS, "v1", mid_week)
        )
        repos["evaluation"].save(
            _evaluation(f"watche{i}", rec_id, EvaluationLabel.INCONCLUSIVE, mid_week)
        )

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.issue_eligible_candidates == 1
    candidate = repos["candidate"].list_all()[0]
    assert candidate.problem_category == "EVALUATION_CRITERIA_UNDEFINED"
    assert candidate.recommended_action.value == "DEFINE_EVALUATION_CRITERIA"


def test_none_metrics_are_not_mistaken_for_degradation(aws_env, repos) -> None:
    """conclusive_count=0(DATA_ISSUEのみ)ではsuccess_rate_pct=Noneになり、
    Noneを閾値未満と誤判定してCandidate化しないこと。"""
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    for i in range(25):
        rec_id = f"dataerr{i}"
        repos["recommendation"].save(
            _recommendation(rec_id, RecommendationType.BUY, "v1", mid_week)
        )
        repos["evaluation"].save(
            _evaluation(
                f"dataerre{i}",
                rec_id,
                EvaluationLabel.DATA_ISSUE,
                mid_week,
                excess_return_pct=None,
            )
        )

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.candidates_detected == 0


# --- 現行rule_versionの判定 ----------------------------------------------


def test_past_rule_version_candidate_saved_but_not_issue_eligible_check(aws_env, repos) -> None:
    """is_current_rule_versionは保存されるが、Issue化可否自体はgithub_issue_service
    側の責務(ここではCandidate自体がis_current_rule_version=Falseで保存されることを
    確認する)。"""
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    _seed_bad_week(repos, RecommendationType.BUY, "v10", 5, 15, mid_week, "oldver")

    # v11がより新しいRecommendationとして存在する(=v10はもう現行ではない)
    later = mid_week + dt.timedelta(hours=1)
    repos["recommendation"].save(_recommendation("newest", RecommendationType.BUY, "v11", later))
    repos["evaluation"].save(
        _evaluation("newest-eval", "newest", EvaluationLabel.SUCCESS, mid_week)
    )

    service = _build_service(repos)
    service.run(_RUN_AT)

    candidates = {c.rule_version: c for c in repos["candidate"].list_all()}
    assert candidates["v10"].is_current_rule_version is False


def test_active_rule_version_takes_priority_over_latest_recommendation(aws_env, repos) -> None:
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    _seed_bad_week(repos, RecommendationType.BUY, "v20", 5, 15, mid_week, "activever")

    # 直近Recommendationはv19だが、正式なACTIVEバージョンはv20
    later = mid_week + dt.timedelta(hours=1)
    repos["recommendation"].save(
        _recommendation("latest-v19", RecommendationType.BUY, "v19", later)
    )
    repos["rule_version"].save(
        RuleVersion(
            rule_version="v20",
            created_at=_RUN_AT,
            change_description="x",
            change_reason="x",
            approval_status="ACTIVE",
            is_active=True,
        )
    )

    service = _build_service(repos)
    service.run(_RUN_AT)

    candidates = {c.rule_version: c for c in repos["candidate"].list_all()}
    assert candidates["v20"].is_current_rule_version is True


def test_resolve_current_rule_version_returns_none_when_nothing_available(aws_env, repos) -> None:
    """ACTIVEバージョンも直近Recommendationも存在しない場合はNone(判定不能)を
    返し、推測で決め打ちしないこと(_resolve_current_rule_version単体テスト)。"""
    service = _build_service(repos)
    result = service._resolve_current_rule_version(RecommendationType.URGENT_REVIEW)
    assert result is None
    assert service._compare_rule_version(result, "v1") is None


def test_compare_rule_version_three_states(aws_env, repos) -> None:
    service = _build_service(repos)
    assert service._compare_rule_version("v11", "v11") is True
    assert service._compare_rule_version("v11", "v10") is False
    assert service._compare_rule_version(None, "v10") is None


# --- 閾値の境界値(プラン記載の具体例) --------------------------------------


def test_success_rate_just_below_threshold_is_candidate(aws_env, repos) -> None:
    """success_rate_pct=49.0(閾値50.0)→Candidate。"""
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    # 100件中49件成功 = 49.0%
    _seed_bad_week(repos, RecommendationType.BUY, "v1", 49, 51, mid_week, "b49")

    service = _build_service(repos)
    service.run(_RUN_AT)

    candidate = repos["candidate"].list_all()[0]
    assert candidate.success_rate_pct == pytest.approx(49.0)
    assert "SUCCESS_RATE_LOW" in candidate.reason_codes


def test_success_rate_just_above_threshold_is_not_a_candidate_on_that_axis(aws_env, repos) -> None:
    """success_rate_pct=51.0(閾値50.0)→成功率理由ではCandidateにしない
    (超過リターンも中立なので全くCandidateにならない)。"""
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    _seed_bad_week(repos, RecommendationType.BUY, "v1", 51, 49, mid_week, "b51")

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.candidates_detected == 0


def test_success_rate_change_points_is_a_point_difference_not_a_ratio(aws_env, repos) -> None:
    """前週70.0%→今週45.0%で、success_rate_change_points=-25.0(ポイント差)に
    なること(相対変化率 (45-70)/70*100=-35.7% ではない)。"""
    period_start, _, review_week = _resolve_review_period(_RUN_AT)
    previous_week_label = module._previous_week_label(review_week)
    previous_monday = module._monday_of_iso_week(previous_week_label)
    from jstock_advisor.domain.entities.improvement import WeeklyReviewMetrics

    repos["metrics"].save(
        WeeklyReviewMetrics(
            metrics_id=f"BUY|v1|ALL|{previous_week_label}",
            review_week=previous_week_label,
            recommendation_type=RecommendationType.BUY,
            rule_version="v1",
            segment_key=None,
            sample_count=20,
            conclusive_count=20,
            success_rate_pct=70.0,
            average_return_pct=1.0,
            average_excess_return_pct=1.0,  # 前週は超過リターン軸では問題なし
            period_start=previous_monday,
            period_end=previous_monday + dt.timedelta(days=6),
            generated_at=_RUN_AT,
        )
    )
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    _seed_bad_week(repos, RecommendationType.BUY, "v1", 9, 11, mid_week, "changepoints")

    service = _build_service(repos)
    service.run(_RUN_AT)

    candidate = repos["candidate"].list_all()[0]
    assert candidate.success_rate_pct == pytest.approx(45.0)
    assert candidate.success_rate_change_points == pytest.approx(-25.0)


def test_excess_return_just_below_threshold_triggers_reason_code(aws_env, repos) -> None:
    """average_excess_return_pct=-1.5(閾値-1.0)→EXCESS_RETURN_LOW。"""
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    for i in range(20):
        rec_id = f"excess1-{i}"
        repos["recommendation"].save(
            _recommendation(rec_id, RecommendationType.BUY, "v1", mid_week)
        )
        repos["evaluation"].save(
            _evaluation(
                f"excess1e-{i}",
                rec_id,
                EvaluationLabel.SUCCESS,
                mid_week,
                excess_return_pct=-1.5,
            )
        )

    service = _build_service(repos)
    service.run(_RUN_AT)

    candidate = repos["candidate"].list_all()[0]
    assert candidate.average_excess_return_pct == pytest.approx(-1.5)
    assert "EXCESS_RETURN_LOW" in candidate.reason_codes


def test_excess_return_just_above_threshold_does_not_trigger_reason_code(aws_env, repos) -> None:
    """average_excess_return_pct=-0.5(閾値-1.0)→当該条件ではCandidateにしない。"""
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    for i in range(20):
        rec_id = f"excess2-{i}"
        repos["recommendation"].save(
            _recommendation(rec_id, RecommendationType.BUY, "v1", mid_week)
        )
        repos["evaluation"].save(
            _evaluation(
                f"excess2e-{i}",
                rec_id,
                EvaluationLabel.SUCCESS,
                mid_week,
                excess_return_pct=-0.5,
            )
        )

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.candidates_detected == 0


# --- EXIT型は超過リターンを評価に使わない(コードレビュー対応2026-08-20、
# WATCH/REVIEWをEXIT型評価に追加したことに伴う指摘) -------------------------


def test_watch_decline_is_success_and_not_a_candidate(aws_env, repos) -> None:
    """WATCH推奨後に株価が下落(SUCCESS)した場合、excess_returnが負に振れても
    (良好な下落ほど自社株リターンがベンチマークを下回るため)、EXIT型では
    excess_returnを評価に使わないため候補化しない。"""
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    for i in range(20):
        rec_id = f"watchdecline-{i}"
        repos["recommendation"].save(
            _recommendation(rec_id, RecommendationType.WATCH, "v1", mid_week)
        )
        repos["evaluation"].save(
            _evaluation(
                f"watchdecline-e{i}",
                rec_id,
                EvaluationLabel.SUCCESS,
                mid_week,
                price_return_pct=-8.0,
                excess_return_pct=-8.0,
            )
        )

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.candidates_detected == 0


def test_review_decline_is_success_and_not_a_candidate(aws_env, repos) -> None:
    """REVIEW推奨後に株価が下落(SUCCESS)した場合も、WATCHと同様に候補化しない。"""
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    for i in range(20):
        rec_id = f"reviewdecline-{i}"
        repos["recommendation"].save(
            _recommendation(rec_id, RecommendationType.REVIEW, "v1", mid_week)
        )
        repos["evaluation"].save(
            _evaluation(
                f"reviewdecline-e{i}",
                rec_id,
                EvaluationLabel.SUCCESS,
                mid_week,
                price_return_pct=-8.0,
                excess_return_pct=-8.0,
            )
        )

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.candidates_detected == 0


def test_watch_success_rate_low_is_still_detected_via_success_rate(aws_env, repos) -> None:
    """WATCHはexcess_returnでは検知しないが、成功率(SUCCESS_RATE_LOW)では
    引き続き検知できること(min_success_rate_pct.WATCH=60.0)。"""
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    for i in range(5):
        rec_id = f"watchsrl-s{i}"
        repos["recommendation"].save(
            _recommendation(rec_id, RecommendationType.WATCH, "v1", mid_week)
        )
        repos["evaluation"].save(
            _evaluation(f"watchsrl-se{i}", rec_id, EvaluationLabel.SUCCESS, mid_week)
        )
    for i in range(15):
        rec_id = f"watchsrl-f{i}"
        repos["recommendation"].save(
            _recommendation(rec_id, RecommendationType.WATCH, "v1", mid_week)
        )
        repos["evaluation"].save(
            _evaluation(f"watchsrl-fe{i}", rec_id, EvaluationLabel.SELL_TOO_SENSITIVE, mid_week)
        )

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.candidates_detected == 1
    candidate = repos["candidate"].list_all()[0]
    assert candidate.success_rate_pct == pytest.approx(25.0)
    assert "SUCCESS_RATE_LOW" in candidate.reason_codes
    assert "EXCESS_RETURN_LOW" not in candidate.reason_codes


def test_review_success_rate_low_is_still_detected_via_success_rate(aws_env, repos) -> None:
    """REVIEWも同様にSUCCESS_RATE_LOWでは引き続き検知できること。"""
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    for i in range(5):
        rec_id = f"reviewsrl-s{i}"
        repos["recommendation"].save(
            _recommendation(rec_id, RecommendationType.REVIEW, "v1", mid_week)
        )
        repos["evaluation"].save(
            _evaluation(f"reviewsrl-se{i}", rec_id, EvaluationLabel.SUCCESS, mid_week)
        )
    for i in range(15):
        rec_id = f"reviewsrl-f{i}"
        repos["recommendation"].save(
            _recommendation(rec_id, RecommendationType.REVIEW, "v1", mid_week)
        )
        repos["evaluation"].save(
            _evaluation(f"reviewsrl-fe{i}", rec_id, EvaluationLabel.SELL_TOO_SENSITIVE, mid_week)
        )

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.candidates_detected == 1
    candidate = repos["candidate"].list_all()[0]
    assert candidate.success_rate_pct == pytest.approx(25.0)
    assert "SUCCESS_RATE_LOW" in candidate.reason_codes
    assert "EXCESS_RETURN_LOW" not in candidate.reason_codes


def test_sell_decline_is_success_and_not_a_candidate_via_excess_return(aws_env, repos) -> None:
    """WATCH/REVIEW固有の対応ではなく、既存EXIT型(SELL)全体で同じ修正が
    効いていることを確認する(SELL推奨後に株価が下落しSUCCESSとなった場合、
    excess_returnが負でもEXCESS_RETURN_LOWとして誤検出しない)。"""
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    for i in range(20):
        rec_id = f"selldecline-{i}"
        repos["recommendation"].save(
            _recommendation(rec_id, RecommendationType.SELL, "v1", mid_week)
        )
        repos["evaluation"].save(
            _evaluation(
                f"selldecline-e{i}",
                rec_id,
                EvaluationLabel.SUCCESS,
                mid_week,
                price_return_pct=-8.0,
                excess_return_pct=-8.0,
            )
        )

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.candidates_detected == 0


# --- evaluation_horizon_days設定値の反映(レビュー指摘③) ---------------------


def test_only_evaluations_matching_configured_horizon_are_aggregated(aws_env, repos) -> None:
    """evaluation_horizon_daysを10へ変更した場合、同じ対象週内であっても
    horizon_calendar_days=7の評価結果は集計対象から除外され、10のものだけが
    週次集計に使われること(ハードコードされた7を使わない)。"""
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    repos["recommendation"].save(_recommendation("r7", RecommendationType.BUY, "v1", mid_week))
    repos["evaluation"].save(
        _evaluation("e7", "r7", EvaluationLabel.SUCCESS, mid_week, horizon_calendar_days=7)
    )
    repos["recommendation"].save(_recommendation("r10", RecommendationType.BUY, "v1", mid_week))
    repos["evaluation"].save(
        _evaluation("e10", "r10", EvaluationLabel.SUCCESS, mid_week, horizon_calendar_days=10)
    )

    service = _build_service(repos, evaluation_horizon_days=10)
    outcome = service.run(_RUN_AT)

    assert outcome.total_evaluation_results == 1
    assert outcome.joined_count == 1


# --- join欠損の監査記録 ---------------------------------------------------


def test_missing_recommendation_is_excluded_and_recorded(aws_env, repos) -> None:
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    repos["recommendation"].save(_recommendation("r1", RecommendationType.BUY, "v1", mid_week))
    repos["evaluation"].save(_evaluation("e1", "r1", EvaluationLabel.SUCCESS, mid_week))
    repos["evaluation"].save(_evaluation("e2", "missing-rec", EvaluationLabel.SUCCESS, mid_week))

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.total_evaluation_results == 2
    assert outcome.joined_count == 1
    assert outcome.missing_recommendation_ids == ["missing-rec"]

    entries = repos["audit"].list_all()
    assert len(entries) == 1
    assert entries[0].output_values["weekly_review_recommendation_missing_count"] == 1
    assert entries[0].output_values["weekly_review_recommendation_missing_ids"] == ["missing-rec"]


# --- LINE通知タイミング ---------------------------------------------------


def test_no_candidates_sends_no_notification(aws_env, repos) -> None:
    line_client = ConsoleLineClient()
    service = _build_service(repos, line_client=line_client)
    service.run(_RUN_AT)
    assert line_client.sent_messages == []


def test_new_issue_creation_triggers_notification(
    aws_env, repos, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_process_candidate(candidate, review_week, now, config, owner, repo, secret_arn):
        tracker.ensure_task_exists(
            candidate.candidate_key,
            candidate.recommendation_type,
            candidate.rule_version,
            candidate.segment_key,
            candidate.priority,
            now,
        )
        tracker.mark_issue_created(
            candidate.candidate_key, 1, "https://github.com/o/r/issues/1", now
        )
        return ImprovementTaskStatus.ISSUE_CREATED

    monkeypatch.setattr(module.github_issue_service, "process_candidate", _fake_process_candidate)

    period_start, _, review_week = _resolve_review_period(_RUN_AT)
    previous_week_label = module._previous_week_label(review_week)
    previous_monday = module._monday_of_iso_week(previous_week_label)
    from jstock_advisor.domain.entities.improvement import WeeklyReviewMetrics

    repos["metrics"].save(
        WeeklyReviewMetrics(
            metrics_id=f"BUY|v1|ALL|{previous_week_label}",
            review_week=previous_week_label,
            recommendation_type=RecommendationType.BUY,
            rule_version="v1",
            segment_key=None,
            sample_count=20,
            conclusive_count=20,
            success_rate_pct=30.0,
            average_return_pct=-1.0,
            average_excess_return_pct=-2.0,
            period_start=previous_monday,
            period_end=previous_monday + dt.timedelta(days=6),
            generated_at=_RUN_AT,
        )
    )
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    _seed_bad_week(repos, RecommendationType.BUY, "v1", 5, 15, mid_week, "notif")

    line_client = ConsoleLineClient()
    service = _build_service(repos, line_client=line_client, issue_creation_enabled=True)
    outcome = service.run(_RUN_AT)

    assert outcome.notified_new_issue_count == 1
    assert len(line_client.sent_messages) == 1
    assert "ルール改善タスクを登録しました" in line_client.sent_messages[0]


def test_existing_issue_comment_does_not_trigger_notification(
    aws_env, repos, monkeypatch: pytest.MonkeyPatch
) -> None:
    """process_candidateがISSUE_CREATEDを返しても、既にIssueが存在していた
    (=github_issue_numberが変化しない)場合は新規通知しないこと。"""

    def _fake_process_candidate(candidate, review_week, now, config, owner, repo, secret_arn):
        # 呼ばれる前に既にISSUE_CREATED状態(既存Issue)がセットされている前提
        return ImprovementTaskStatus.ISSUE_CREATED

    monkeypatch.setattr(module.github_issue_service, "process_candidate", _fake_process_candidate)

    period_start, _, review_week = _resolve_review_period(_RUN_AT)
    previous_week_label = module._previous_week_label(review_week)
    previous_monday = module._monday_of_iso_week(previous_week_label)
    from jstock_advisor.domain.entities.improvement import WeeklyReviewMetrics

    repos["metrics"].save(
        WeeklyReviewMetrics(
            metrics_id=f"BUY|v1|ALL|{previous_week_label}",
            review_week=previous_week_label,
            recommendation_type=RecommendationType.BUY,
            rule_version="v1",
            segment_key=None,
            sample_count=20,
            conclusive_count=20,
            success_rate_pct=30.0,
            average_return_pct=-1.0,
            average_excess_return_pct=-2.0,
            period_start=previous_monday,
            period_end=previous_monday + dt.timedelta(days=6),
            generated_at=_RUN_AT,
        )
    )
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    _seed_bad_week(repos, RecommendationType.BUY, "v1", 5, 15, mid_week, "existing")

    from jstock_advisor.domain.entities.improvement import PROBLEM_CATEGORY_PERFORMANCE_DEGRADED
    from jstock_advisor.domain.improvement_rules import build_candidate_key

    candidate_key = build_candidate_key(
        RecommendationType.BUY, "v1", None, PROBLEM_CATEGORY_PERFORMANCE_DEGRADED
    )
    from jstock_advisor.domain.entities.enums import ImprovementPriority

    tracker.ensure_task_exists(
        candidate_key, RecommendationType.BUY, "v1", None, ImprovementPriority.B, _RUN_AT
    )
    tracker.mark_issue_created(candidate_key, 99, "https://github.com/o/r/issues/99", _RUN_AT)

    line_client = ConsoleLineClient()
    service = _build_service(repos, line_client=line_client, issue_creation_enabled=True)
    outcome = service.run(_RUN_AT)

    assert outcome.notified_new_issue_count == 0
    assert line_client.sent_messages == []


# --- Issue #114 Phase B2: 集計軸を evaluation_date へ ---------------------
#
# 従来は evaluated_at(処理をいつ走らせたか)で絞っていたため、定点評価が遅延して
# 後からまとめて処理されると、過去の基準日の評価が「処理した週」へ一括計上され、
# 回復週の母数だけが膨らんでいた。以下はその是正の固定である。


def _evaluation_with_dates(
    eval_id: str,
    rec_id: str,
    label: EvaluationLabel,
    evaluation_date: dt.date,
    evaluated_at: dt.datetime,
) -> EvaluationResult:
    """evaluation_date と evaluated_at を**別々に**指定できる版。

    既存の_evaluation()は evaluation_date = evaluated_at.date() と揃えてしまうため、
    遅延処理(両者がずれる)を再現できない。
    """
    return EvaluationResult(
        evaluation_id=eval_id,
        recommendation_id=rec_id,
        horizon_calendar_days=7,
        evaluated_at=evaluated_at,
        evaluation_date=evaluation_date,
        price_at_evaluation=Decimal("1010"),
        price_return_pct=1.0,
        excess_return_pct=1.0,
        evaluation_label=label,
        label_evidence="x",
    )


def _seed_one(
    repos: dict,
    prefix: str,
    evaluation_date: dt.date,
    evaluated_at: dt.datetime,
    label: EvaluationLabel = EvaluationLabel.SUCCESS,
) -> None:
    rec_id = f"{prefix}-rec"
    repos["recommendation"].save(
        _recommendation(rec_id, RecommendationType.BUY, "v1", evaluated_at)
    )
    repos["evaluation"].save(
        _evaluation_with_dates(f"{prefix}-eval", rec_id, label, evaluation_date, evaluated_at)
    )


def test_delayed_evaluations_are_not_piled_into_the_catch_up_week(aws_env, repos) -> None:
    """遅延評価が回復週へ集中しない(本Issueの中心)。

    evaluated_atは全件同一日(catch-up実行日)だが、evaluation_dateは複数週へ
    散っている入力を与える。集計軸がevaluated_atのままなら1週へ寄る。
    """
    period_start, period_end, review_week = _resolve_review_period(_RUN_AT)
    catch_up_at = dt.datetime.combine(
        period_start + dt.timedelta(days=1), dt.time(9), tzinfo=dt.UTC
    )

    # 対象週(前週)の基準日
    _seed_one(repos, "cur", period_start + dt.timedelta(days=2), catch_up_at)
    # 1週前・2週前の基準日。処理はどちらも catch_up_at(= 対象週)に行われている。
    _seed_one(repos, "w1", period_start - dt.timedelta(days=5), catch_up_at)
    _seed_one(repos, "w2", period_start - dt.timedelta(days=12), catch_up_at)

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    # 対象週へ入るのは基準日が対象週のもの1件だけ。
    assert outcome.total_evaluation_results == 1

    saved = {m.review_week: m for m in repos["metrics"].list_all()}
    assert saved[review_week].sample_count == 1
    # 過去週は作り直され、それぞれの基準日の週へ1件ずつ入る。
    assert saved[module._previous_week_label(review_week)].sample_count == 1
    two_weeks_ago = module._previous_week_label(module._previous_week_label(review_week))
    assert saved[two_weeks_ago].sample_count == 1
    assert outcome.past_weeks_metrics_recomputed >= 2


def test_normal_week_metrics_are_unchanged_by_the_axis_switch(aws_env, repos) -> None:
    """通常運用(evaluation_dateとevaluated_atがほぼ一致)では結果が変わらない。"""
    period_start, _period_end, review_week = _resolve_review_period(_RUN_AT)
    for i in range(3):
        day = period_start + dt.timedelta(days=i)
        at = dt.datetime.combine(day, dt.time(9), tzinfo=dt.UTC)
        _seed_one(repos, f"n{i}", day, at)

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.total_evaluation_results == 3
    saved = {m.review_week: m for m in repos["metrics"].list_all()}
    assert saved[review_week].sample_count == 3
    assert saved[review_week].success_rate_pct == 100.0


def test_week_boundary_is_inclusive_on_both_ends(aws_env, repos) -> None:
    """period_start / period_end ちょうどは含み、その1日外は含まない。"""
    period_start, period_end, review_week = _resolve_review_period(_RUN_AT)
    at = dt.datetime.combine(period_start, dt.time(9), tzinfo=dt.UTC)

    _seed_one(repos, "start", period_start, at)
    _seed_one(repos, "end", period_end, at)
    _seed_one(repos, "before", period_start - dt.timedelta(days=1), at)
    _seed_one(repos, "after", period_end + dt.timedelta(days=1), at)

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.total_evaluation_results == 2
    saved = {m.review_week: m for m in repos["metrics"].list_all()}
    assert saved[review_week].sample_count == 2


def test_recompute_does_not_fail_when_no_past_metrics_exist(aws_env, repos) -> None:
    """初回(過去週のmetricsが1件も無い)でも落ちない。"""
    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.total_evaluation_results == 0
    assert outcome.past_weeks_metrics_recomputed == 0
    assert repos["metrics"].list_all() == []


def test_stale_row_from_the_old_axis_is_overwritten_with_zero(aws_env, repos) -> None:
    """古い軸で作られた行が、新しい軸では0件になる場合も上書きされる。

    放置すると誤った母数の行が残り続けるため、0件として書き直す。
    """
    _period_start, _period_end, review_week = _resolve_review_period(_RUN_AT)
    past_week = module._previous_week_label(review_week)
    past_monday = module._monday_of_iso_week(past_week)

    from jstock_advisor.domain.entities.improvement import WeeklyReviewMetrics

    repos["metrics"].save(
        WeeklyReviewMetrics(
            metrics_id=f"{RecommendationType.BUY.value}|v1|ALL|{past_week}",
            review_week=past_week,
            recommendation_type=RecommendationType.BUY,
            rule_version="v1",
            segment_key=None,
            sample_count=99,
            conclusive_count=99,
            success_rate_pct=10.0,
            average_return_pct=-5.0,
            average_excess_return_pct=-5.0,
            period_start=past_monday,
            period_end=past_monday + dt.timedelta(days=6),
            generated_at=_RUN_AT,
        )
    )

    service = _build_service(repos)
    service.run(_RUN_AT)

    rewritten = repos["metrics"].get(f"{RecommendationType.BUY.value}|v1|ALL|{past_week}")
    assert rewritten is not None
    assert rewritten.sample_count == 0
    assert rewritten.success_rate_pct is None


def test_past_weeks_produce_no_candidates_and_no_notification(aws_env, repos) -> None:
    """過去週の再集計では候補もIssueも通知も作らない(metricsのupsertのみ)。"""
    period_start, _period_end, review_week = _resolve_review_period(_RUN_AT)
    past_monday = module._monday_of_iso_week(module._previous_week_label(review_week))
    catch_up_at = dt.datetime.combine(
        period_start + dt.timedelta(days=1), dt.time(9), tzinfo=dt.UTC
    )

    # 過去週の基準日に、閾値を割る成績の評価だけを置く(対象週は0件)。
    for i in range(5):
        _seed_one(repos, f"ok{i}", past_monday, catch_up_at, EvaluationLabel.SUCCESS)
    for i in range(15):
        _seed_one(repos, f"ng{i}", past_monday, catch_up_at, EvaluationLabel.PRICE_TOO_HIGH)

    line_client = ConsoleLineClient()
    service = _build_service(repos, line_client=line_client, issue_creation_enabled=True)
    outcome = service.run(_RUN_AT)

    # 過去週のmetricsは作られている。
    saved = {m.review_week: m for m in repos["metrics"].list_all()}
    assert saved[module._previous_week_label(review_week)].sample_count == 20
    # しかし候補も通知も作られない。
    assert outcome.candidates_detected == 0
    assert repos["candidate"].list_all() == []
    assert line_client.sent_messages == []


def test_past_week_recompute_is_idempotent(aws_env, repos) -> None:
    """2回続けて実行しても過去週の行は同じ値になる(冪等)。

    metrics_idは決定的、対象週は現在週から機械的に導出され、値は保存済みの
    EvaluationResultだけから決まる。動くのはgenerated_atのみである。
    """
    period_start, _period_end, review_week = _resolve_review_period(_RUN_AT)
    past_monday = module._monday_of_iso_week(module._previous_week_label(review_week))
    catch_up_at = dt.datetime.combine(
        period_start + dt.timedelta(days=1), dt.time(9), tzinfo=dt.UTC
    )
    for i in range(3):
        _seed_one(repos, f"idem{i}", past_monday + dt.timedelta(days=i), catch_up_at)

    service = _build_service(repos)
    first = service.run(_RUN_AT)
    snapshot_first = {
        (m.metrics_id, m.sample_count, m.success_rate_pct, m.review_week)
        for m in repos["metrics"].list_all()
    }

    second = service.run(_RUN_AT)
    snapshot_second = {
        (m.metrics_id, m.sample_count, m.success_rate_pct, m.review_week)
        for m in repos["metrics"].list_all()
    }

    assert snapshot_first == snapshot_second
    assert (
        first.past_weeks_metrics_recomputed_by_week == second.past_weeks_metrics_recomputed_by_week
    )


def test_recomputed_weeks_are_recorded_per_week(aws_env, repos) -> None:
    """どの週を何行書き直したかが outcome に残る(総数だけにしない)。"""
    period_start, _period_end, review_week = _resolve_review_period(_RUN_AT)
    one_week_ago = module._previous_week_label(review_week)
    past_monday = module._monday_of_iso_week(one_week_ago)
    catch_up_at = dt.datetime.combine(
        period_start + dt.timedelta(days=1), dt.time(9), tzinfo=dt.UTC
    )
    _seed_one(repos, "detail", past_monday, catch_up_at)

    service = _build_service(repos)
    outcome = service.run(_RUN_AT)

    assert outcome.past_weeks_metrics_recomputed_by_week == {one_week_ago: 1}
    assert outcome.past_weeks_metrics_recomputed == 1
    assert outcome.past_weeks_metrics_recomputed == 1


def _oracle_collect_for_period(
    repos: dict, target_horizon: int, period_start: dt.date, period_end: dt.date
) -> list[EvaluationResult]:
    """`_aggregate_windows()`とは独立に、単一期間だけをfilterする対照実装(Issue #377)。
    旧`_collect_evaluations_for_period()`(単位3で削除済み)と同じ述語をテスト側で再現し、
    1回のstreaming scanが「対象週ごとに個別filterした場合の和集合」と一致することを、
    本番実装から独立したoracleで確認する。
    """
    return [
        e
        for e in repos["evaluation"].list_all()
        if e.horizon_calendar_days == target_horizon
        and period_start <= e.evaluation_date <= period_end
    ]


def _oracle_join_via_get(
    repos: dict, evaluations: list[EvaluationResult]
) -> tuple[list[tuple[EvaluationResult, Recommendation]], list[str]]:
    """旧実装(RecommendationRepository.get()を1件ずつ呼ぶ版。評価もRecommendationも全件を
    リストへ保持する)を、本番コードから独立にテスト側で再現したoracle(Issue #377)。
    新実装(chunkごとにget_many()して集計器へ足し込む版)が、これと同じ集計値・同じ
    missing_idsになることを突き合わせる。
    """
    joined: list[tuple[EvaluationResult, Recommendation]] = []
    missing_ids: list[str] = []
    for evaluation in evaluations:
        recommendation = repos["recommendation"].get(evaluation.recommendation_id)
        if recommendation is None:
            missing_ids.append(evaluation.recommendation_id)
            continue
        joined.append((evaluation, recommendation))
    return joined, missing_ids


def _oracle_groups(
    joined: list[tuple[EvaluationResult, Recommendation]],
) -> dict[tuple[RecommendationType, str], list[EvaluationResult]]:
    """旧`_group_by_type_and_rule_version()`と同じ(評価の順序と、組の挿入順を保つ)。"""
    groups: dict[tuple[RecommendationType, str], list[EvaluationResult]] = {}
    for evaluation, recommendation in joined:
        key = (recommendation.recommendation_type, recommendation.rule_version)
        groups.setdefault(key, []).append(evaluation)
    return groups


def _five_windows(review_week: str) -> list[tuple[str, dt.date, dt.date]]:
    windows = []
    label = review_week
    for _ in range(5):
        period_start = module._monday_of_iso_week(label)
        windows.append((label, period_start, period_start + dt.timedelta(days=6)))
        label = module._previous_week_label(label)
    return windows


def test_aggregate_windows_matches_per_period_collection(aws_env, repos) -> None:
    """1回のstreaming scanでの週ごとの集計が、windowごとに個別filterして結合した場合
    (oracle)と一致すること(Issue #377。#114 C-5への影響評価の裏付けの一部)。
    週ごとの該当件数・結合できた件数・集計値(全項目)を突き合わせる。
    """
    service = _build_service(repos)
    review_week = "2026-W38"
    monday = module._monday_of_iso_week(review_week)
    windows = _five_windows(review_week)

    for i, (_wlabel, wstart, wend) in enumerate(windows):
        for kind, day, hour in (("mon", wstart, 0), ("sun", wend, 23)):
            rec_id = f"rec{i}{kind}"
            at = dt.datetime.combine(day, dt.time(hour), tzinfo=dt.UTC)
            repos["recommendation"].save(_recommendation(rec_id, RecommendationType.BUY, "v1", at))
            repos["evaluation"].save(_evaluation(f"{kind}{i}", rec_id, EvaluationLabel.SUCCESS, at))
    outside_date = monday - dt.timedelta(days=365)
    repos["evaluation"].save(
        _evaluation(
            "outside",
            "rec-outside",
            EvaluationLabel.SUCCESS,
            dt.datetime.combine(outside_date, dt.time(0), tzinfo=dt.UTC),
        )
    )
    repos["evaluation"].save(
        _evaluation(
            "wrong_horizon",
            "rec-wh",
            EvaluationLabel.SUCCESS,
            dt.datetime.combine(windows[0][1], dt.time(12), tzinfo=dt.UTC),
            horizon_calendar_days=14,
        )
    )

    aggregates = service._aggregate_windows(windows, current_label=windows[0][0])
    target_horizon = service._review_config.evaluation_horizon_days

    for wlabel, wstart, wend in windows:
        individual = _oracle_collect_for_period(repos, target_horizon, wstart, wend)
        joined, missing = _oracle_join_via_get(repos, individual)
        aggregate = aggregates[wlabel]
        assert aggregate.matched == len(individual) == 2
        assert aggregate.joined == len(joined)
        assert aggregate.missing_ids == missing
        expected = {
            key: build_metrics_bucket(key[0].value, evals)
            for key, evals in _oracle_groups(joined).items()
        }
        assert {
            key: accumulator.to_bucket(key[0].value)
            for key, accumulator in aggregate.groups.items()
        } == expected

    assert sum(a.matched for a in aggregates.values()) == 10


def test_aggregate_windows_returns_empty_aggregates_when_no_data(aws_env, repos) -> None:
    service = _build_service(repos)
    windows = [("2026-W38", dt.date(2026, 9, 14), dt.date(2026, 9, 20))]

    result = service._aggregate_windows(windows, current_label=windows[0][0])

    assert list(result) == ["2026-W38"]
    aggregate = result["2026-W38"]
    assert (aggregate.matched, aggregate.joined, aggregate.missing_ids) == (0, 0, [])
    assert aggregate.groups == {}


def test_run_produces_identical_metrics_to_running_five_separate_scans(aws_env, repos) -> None:
    """Issue #377の核心: run()が、評価もRecommendationも保持しない集計へ変わっても、
    週ごとに評価を集めて結合・集計していた旧方式と全く同じmetricsが生成されることを、
    実際にrun()を呼んだ結果で確認する(#114 C-5への影響評価の最終確認)。

    比較対象(oracle)は`_oracle_collect_for_period()`と`_oracle_join_via_get()`と
    `build_metrics_bucket()`で、旧実装の手順(週ごとに評価のリストを作り、get()で結合し、
    リストから集計する)を本番実装から独立に再現したものである。保存された
    WeeklyReviewMetricsの**全項目**(generated_atを除く)を比べる。
    """
    period_start, period_end, review_week = _resolve_review_period(_RUN_AT)
    windows = [(review_week, period_start, period_end), *_five_windows(review_week)[1:]]
    # _five_windows(review_week) の先頭は review_week 自身(_resolve_review_period と一致)。
    assert windows[0] == _five_windows(review_week)[0]

    counter = 0
    for _wlabel, wstart, wend in windows:
        catch_up_at = dt.datetime.combine(wstart + dt.timedelta(days=1), dt.time(9), tzinfo=dt.UTC)
        for _ in range(3):
            counter += 1
            _seed_one(repos, f"w{counter}", wstart, catch_up_at, EvaluationLabel.SUCCESS)
        for _ in range(2):
            counter += 1
            _seed_one(repos, f"w{counter}", wend, catch_up_at, EvaluationLabel.PRICE_TOO_HIGH)

    service = _build_service(repos)
    service.run(_RUN_AT)

    actual = {
        (m.recommendation_type, m.rule_version, m.review_week): m
        for m in repos["metrics"].list_all()
    }

    expected_service = _build_service(repos)
    target_horizon = expected_service._review_config.evaluation_horizon_days
    expected_rows = 0
    for wlabel, wstart, wend in windows:
        evaluations = _oracle_collect_for_period(repos, target_horizon, wstart, wend)
        joined, _ = _oracle_join_via_get(repos, evaluations)
        for (rec_type, rule_version), grouped in _oracle_groups(joined).items():
            expected = expected_service._build_metrics(
                rec_type,
                rule_version,
                wlabel,
                wstart,
                wend,
                _RUN_AT,
                build_metrics_bucket(rec_type.value, grouped),
            )
            key = (rec_type, rule_version, wlabel)
            assert key in actual, f"{key} が run() の保存結果に無い"
            assert actual[key] == expected  # 全項目(generated_at は同じ _RUN_AT)
            expected_rows += 1
    assert expected_rows == len(actual) == 5


def test_run_detects_the_same_candidates_as_the_list_based_oracle(aws_env, repos) -> None:
    """ImprovementCandidateの同値性(Issue #377 Track1): run()が保存した候補が、旧方式
    (週ごとに評価のリストを作り、get()で結合し、リストから集計したmetrics)を
    `_detect_candidate()`へ通した結果と一致する。前週の悪化履歴つきで、複数の
    (種別, rule_version)のうち悪化した組だけが候補になることまで確認する。
    """
    from jstock_advisor.domain.entities.improvement import WeeklyReviewMetrics

    period_start, period_end, review_week = _resolve_review_period(_RUN_AT)
    previous_label = module._previous_week_label(review_week)
    previous_monday = module._monday_of_iso_week(previous_label)
    for rec_type in (RecommendationType.BUY, RecommendationType.SELL):
        repos["metrics"].save(
            WeeklyReviewMetrics(
                metrics_id=f"{rec_type.value}|v1|ALL|{previous_label}",
                review_week=previous_label,
                recommendation_type=rec_type,
                rule_version="v1",
                segment_key=None,
                sample_count=20,
                conclusive_count=20,
                success_rate_pct=30.0,  # 前週も閾値(50.0)未満
                average_return_pct=-1.0,
                average_excess_return_pct=-2.0,
                period_start=previous_monday,
                period_end=previous_monday + dt.timedelta(days=6),
                generated_at=_RUN_AT,
            )
        )
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    _seed_bad_week(repos, RecommendationType.BUY, "v1", 5, 15, mid_week, "buybad")  # 悪化
    _seed_bad_week(repos, RecommendationType.SELL, "v1", 4, 16, mid_week, "sellbad")  # 悪化
    _seed_bad_week(repos, RecommendationType.HOLD, "v1", 18, 2, mid_week, "holdok")  # 健全

    service = _build_service(repos)
    oracle_service = _build_service(repos)
    target_horizon = oracle_service._review_config.evaluation_horizon_days
    oracle_evaluations = _oracle_collect_for_period(repos, target_horizon, period_start, period_end)
    oracle_joined, _ = _oracle_join_via_get(repos, oracle_evaluations)
    expected: dict[str, dict] = {}
    for (rec_type, rule_version), grouped in _oracle_groups(oracle_joined).items():
        metrics = oracle_service._build_metrics(
            rec_type,
            rule_version,
            review_week,
            period_start,
            period_end,
            _RUN_AT,
            build_metrics_bucket(rec_type.value, grouped),
        )
        history = repos["metrics"].list_by_type_version_segment(rec_type, rule_version, None)
        is_current = oracle_service._compare_rule_version(
            oracle_service._resolve_current_rule_version(rec_type), rule_version
        )
        candidate = oracle_service._detect_candidate(metrics, history, is_current)
        if candidate is not None:
            expected[candidate.candidate_id] = candidate.model_dump()

    outcome = service.run(_RUN_AT)

    actual = {c.candidate_id: c.model_dump() for c in repos["candidate"].list_all()}
    assert outcome.candidates_detected == len(expected) == 2  # BUY と SELL の悪化だけ
    assert actual == expected


# --- Issue #377 PR #379是正 / Track1: Recommendationの結合(N+1解消と、保持の有界化)---


def _seed_join_fixture(
    repos: dict, count: int, *, missing_every: int = 0
) -> list[EvaluationResult]:
    """count件のevaluationを、対応するrecommendationとともにシードする。
    missing_everyを指定すると、その倍数番目のevaluationだけ存在しない
    recommendation_idを参照させる(missing_idsのテスト用)。
    """
    base = dt.datetime(2026, 8, 10, 9, 0, tzinfo=dt.UTC)
    evaluations = []
    for i in range(count):
        rec_id = f"missing-{i}" if missing_every and i % missing_every == 0 else f"rec{i}"
        if not (missing_every and i % missing_every == 0):
            repos["recommendation"].save(
                _recommendation(rec_id, RecommendationType.BUY, "v1", base)
            )
        ev = _evaluation(f"e{i}", rec_id, EvaluationLabel.SUCCESS, base)
        repos["evaluation"].save(ev)
        evaluations.append(ev)
    return evaluations


# _seed_join_fixture のevaluationは全て2026-08-10(月。2026-W33)が基準日。
_JOIN_WINDOW = [("2026-W33", dt.date(2026, 8, 10), dt.date(2026, 8, 16))]


def test_aggregate_windows_does_not_call_get(aws_env, repos, monkeypatch) -> None:
    """A: 集計中にRecommendationRepository.get()が呼ばれないこと(N+1解消の直接確認)。"""
    _seed_join_fixture(repos, 5)
    service = _build_service(repos)

    calls = []
    original_get = repos["recommendation"].get

    def spy_get(recommendation_id):
        calls.append(recommendation_id)
        return original_get(recommendation_id)

    monkeypatch.setattr(repos["recommendation"], "get", spy_get)

    aggregate = service._aggregate_windows(_JOIN_WINDOW, current_label=_JOIN_WINDOW[0][0])[
        "2026-W33"
    ]

    assert calls == []
    assert aggregate.joined == 5
    assert aggregate.missing_ids == []


def test_aggregate_windows_uses_bounded_get_many_calls(aws_env, repos, monkeypatch) -> None:
    """B/C: get_many()が呼ばれ、対象件数がchunk上限を超えると複数回のbounded callに
    分割されること。1回のget_many()引数件数がchunk上限を超えないこと。
    """
    chunk_size = module._RECOMMENDATION_JOIN_CHUNK_SIZE
    total = chunk_size * 2 + 3  # ちょうど3チャンクに分かれる件数
    _seed_join_fixture(repos, total)
    service = _build_service(repos)

    calls: list[list[str]] = []
    original_get_many = repos["recommendation"].get_many

    def spy_get_many(recommendation_ids):
        ids = list(recommendation_ids)
        calls.append(ids)
        return original_get_many(ids)

    monkeypatch.setattr(repos["recommendation"], "get_many", spy_get_many)

    aggregate = service._aggregate_windows(_JOIN_WINDOW, current_label=_JOIN_WINDOW[0][0])[
        "2026-W33"
    ]

    assert aggregate.joined == total
    assert aggregate.missing_ids == []
    assert len(calls) == 3  # ceil(total / chunk_size)
    for call_ids in calls:
        assert len(call_ids) <= chunk_size
    assert sum(len(c) for c in calls) == total


def test_aggregate_windows_missing_id_semantics_match_oracle(aws_env, repos) -> None:
    """D/E: missing_idsの意味・結合の結果が、旧get()方式のoracleと同値であること。
    欠落IDが複数(重複を含む)ケースで確認する。
    """
    _seed_join_fixture(repos, 20, missing_every=3)
    # 同じ欠落IDを複数のevaluationに参照させ、重複した欠落が重複排除されずに
    # 残ることも確認する。
    dup_missing = _evaluation(
        "e-dup-missing",
        "missing-0",
        EvaluationLabel.SUCCESS,
        dt.datetime(2026, 8, 10, 9, 0, tzinfo=dt.UTC),
    )
    repos["evaluation"].save(dup_missing)

    service = _build_service(repos)

    aggregate = service._aggregate_windows(_JOIN_WINDOW, current_label=_JOIN_WINDOW[0][0])[
        "2026-W33"
    ]
    evaluations = _oracle_collect_for_period(
        repos,
        service._review_config.evaluation_horizon_days,
        dt.date(2026, 8, 10),
        dt.date(2026, 8, 16),
    )
    oracle_joined, oracle_missing = _oracle_join_via_get(repos, evaluations)

    assert aggregate.matched == len(evaluations)
    assert aggregate.joined == len(oracle_joined)
    # missing_ids の順序は評価の走査順(oracle と同じ入力順)。
    assert aggregate.missing_ids == oracle_missing
    # missing-0 は複数回参照されており、重複排除されず2回(元のevaluation + dup_missing)残る。
    assert aggregate.missing_ids.count("missing-0") == 2
    expected = {
        key: build_metrics_bucket(key[0].value, evals)
        for key, evals in _oracle_groups(oracle_joined).items()
    }
    assert {key: acc.to_bucket(key[0].value) for key, acc in aggregate.groups.items()} == expected


def test_aggregate_windows_duplicate_recommendation_id_is_fetched_once(
    aws_env, repos, monkeypatch
) -> None:
    """F: 同じrecommendation_idを複数のEvaluationResultが参照する場合、Recommendation
    取得回数を必要以上に増やさず、集計はEvaluationResult件数ぶん正しく残ること。
    """
    base = dt.datetime(2026, 8, 10, 9, 0, tzinfo=dt.UTC)
    repos["recommendation"].save(_recommendation("shared-rec", RecommendationType.BUY, "v1", base))
    for i in range(5):
        repos["evaluation"].save(_evaluation(f"e{i}", "shared-rec", EvaluationLabel.SUCCESS, base))
    service = _build_service(repos)

    calls: list[list[str]] = []
    original_get_many = repos["recommendation"].get_many

    def spy_get_many(recommendation_ids):
        ids = list(recommendation_ids)
        calls.append(ids)
        return original_get_many(ids)

    monkeypatch.setattr(repos["recommendation"], "get_many", spy_get_many)

    aggregate = service._aggregate_windows(_JOIN_WINDOW, current_label=_JOIN_WINDOW[0][0])[
        "2026-W33"
    ]

    assert aggregate.joined == 5  # EvaluationResult件数ぶん集計に残る
    assert aggregate.missing_ids == []
    assert len(calls) == 1  # 1チャンクで済む件数のためget_many呼び出しは1回
    # dedupはget_many()内部(dict.fromkeys)の責務であり、呼び出し側では重複除去済みである
    # 必要はない(要件は、呼んだ回数=1回のみ)。
    (((rec_type, rule_version), accumulator),) = aggregate.groups.items()
    assert (rec_type, rule_version) == (RecommendationType.BUY, "v1")
    assert accumulator.count == 5


# --- Issue #367(b): 本番と同じDynamoDBバックエンドを実際に通っていることの確認 ---


def test_review_repositories_run_on_dynamodb_not_local_json(
    aws_env, repos, tmp_path: Path, assert_dynamodb_backend
) -> None:
    """opt-in fixtureを付けただけで完了扱いにしない(条件4)。

    running_on_lambda()==Trueの下で、週次レビューが使う6つのrepositoryが
    すべてDynamoDBバックエンドを選び(store_dirを渡してもローカルJSONへ落ちない)、
    ローカルJSONが作られないことを確認する。
    """
    for repo in repos.values():
        assert_dynamodb_backend(repo._store)
    repos["rule_version"].save(
        RuleVersion(
            rule_version="v-367",
            created_at=_RUN_AT,
            change_description="x",
            change_reason="x",
            approval_status="ACTIVE",
            is_active=True,
        )
    )
    assert [v.rule_version for v in repos["rule_version"].list_all()] == ["v-367"]
    assert not list(tmp_path.glob("*.json")), "ローカルJSONへ書いている(本番と異なる経路)"


# --- Issue #377 Track1(PR #539 再レビュー): 過去週の結合失敗の契約 ----------------


class _JoinBoomError(RuntimeError):
    pass


def _fail_join_for(repos: dict, monkeypatch, prefixes: tuple[str, ...], on_call: int = 1) -> list:
    """recommendation_idが`prefixes`のいずれかで始まるIDを含むget_many()の`on_call`回目以降を
    失敗させる(呼ばれた回の記録を返す)。例外本文には識別子らしい文字列を入れる(漏れの検査用)。
    """
    original = repos["recommendation"].get_many
    calls: list[list[str]] = []

    def wrapper(ids):
        id_list = list(ids)
        if any(i.startswith(p) for i in id_list for p in prefixes):
            calls.append(id_list)
            if len(calls) >= on_call:
                raise _JoinBoomError("SECRET-DETAIL-should-not-leak")
        return original(id_list)

    monkeypatch.setattr(repos["recommendation"], "get_many", wrapper)
    return calls


def _seed_week(
    repos: dict, prefix: str, week_label: str, count: int, label=EvaluationLabel.SUCCESS
):
    start = module._monday_of_iso_week(week_label)
    at = dt.datetime.combine(start + dt.timedelta(days=1), dt.time(9), tzinfo=dt.UTC)
    for i in range(count):
        _seed_one(repos, f"{prefix}{i}", start + dt.timedelta(days=i % 7), at, label)


def _labels() -> tuple[str, str, str]:
    _s, _e, review_week = _resolve_review_period(_RUN_AT)
    w1 = module._previous_week_label(review_week)
    w2 = module._previous_week_label(w1)
    return review_week, w1, w2


def test_current_week_join_failure_fails_the_whole_run(aws_env, repos, monkeypatch) -> None:
    """T1: current weekの結合失敗は従来どおりrun全体を失敗させ、metrics・candidateを作らない。"""
    review_week, w1, _w2 = _labels()
    _seed_week(repos, "cur", review_week, 3)
    _seed_week(repos, "pa", w1, 2)
    _fail_join_for(repos, monkeypatch, ("cur",))

    with pytest.raises(_JoinBoomError):
        _build_service(repos).run(_RUN_AT)

    assert repos["metrics"].list_all() == []
    assert repos["candidate"].list_all() == []


def test_past_week_join_failure_does_not_stop_the_current_week(
    aws_env, repos, monkeypatch, caplog
) -> None:
    """T2: 過去週の結合失敗でも、current weekのmetrics保存・候補検知・監査記録は行われ、
    失敗した週のmetricsは保存されない。失敗は週・段階・例外の型名で識別でき、例外本文は出ない。
    """
    review_week, w1, _w2 = _labels()
    _seed_week(repos, "cur", review_week, 3)
    _seed_week(repos, "cur-bad", review_week, 1, EvaluationLabel.PRICE_TOO_HIGH)
    _seed_week(repos, "pa", w1, 2)
    _fail_join_for(repos, monkeypatch, ("pa",))

    with caplog.at_level("WARNING"):
        outcome = _build_service(repos).run(_RUN_AT)

    saved = {(m.review_week): m for m in repos["metrics"].list_all()}
    assert set(saved) == {review_week}  # 失敗した過去週は保存されない
    assert saved[review_week].sample_count == 4
    assert outcome.metrics_saved == 1
    assert outcome.total_evaluation_results == 4
    assert outcome.past_weeks_metrics_recomputed == 0
    expected_failure = "phase=recommendation_join exception=_JoinBoomError"
    assert outcome.past_weeks_join_failed == {w1: expected_failure}
    # 候補検知・GitHub/LINE判定へ進んでいる(currentの処理の後段まで到達し、監査が残る)
    assert outcome.candidates_detected == len(repos["candidate"].list_all())
    audit = repos["audit"].list_all()
    assert len(audit) == 1
    assert audit[0].output_values["past_weeks_join_failed"] == outcome.past_weeks_join_failed
    # 可視性: WARNINGに週・段階・型名。例外本文は出さない
    text = " ".join(r.getMessage() for r in caplog.records)
    assert w1 in text and "recommendation_join" in text and "_JoinBoomError" in text
    assert "SECRET-DETAIL" not in text
    assert "SECRET-DETAIL" not in str(audit[0].output_values)


def test_partial_past_week_aggregate_is_never_saved(aws_env, repos, monkeypatch) -> None:
    """T3: chunk1は成功しchunk2で失敗する過去週について、chunk1分だけのmetricsを保存しない。
    既に保存済みの同じ週の行も、そのまま変えない。
    """
    from jstock_advisor.domain.entities.improvement import WeeklyReviewMetrics

    monkeypatch.setattr(module, "_RECOMMENDATION_JOIN_CHUNK_SIZE", 2)
    review_week, w1, _w2 = _labels()
    _seed_week(repos, "cur", review_week, 1)
    _seed_week(repos, "pa", w1, 5)  # chunk 2 + 2 + 1
    w1_start = module._monday_of_iso_week(w1)
    previous = WeeklyReviewMetrics(
        metrics_id=f"BUY|v1|ALL|{w1}",
        review_week=w1,
        recommendation_type=RecommendationType.BUY,
        rule_version="v1",
        segment_key=None,
        sample_count=99,
        conclusive_count=99,
        success_rate_pct=10.0,
        average_return_pct=0.1,
        average_excess_return_pct=0.1,
        period_start=w1_start,
        period_end=w1_start + dt.timedelta(days=6),
        generated_at=_RUN_AT,
    )
    repos["metrics"].save(previous)
    calls = _fail_join_for(repos, monkeypatch, ("pa",), on_call=2)

    outcome = _build_service(repos).run(_RUN_AT)

    assert len(calls) == 2  # 1回目は成功、2回目で失敗、以後はfoldしない
    assert repos["metrics"].get(previous.metrics_id) == previous  # 部分値で上書きされていない
    assert outcome.past_weeks_metrics_recomputed == 0
    assert w1 in outcome.past_weeks_join_failed


def test_other_past_weeks_continue_when_one_past_week_fails(aws_env, repos, monkeypatch) -> None:
    """T4: 過去週の1週だけ失敗しても、current weekと失敗していない過去週は通常どおり処理する。"""
    review_week, w1, w2 = _labels()
    _seed_week(repos, "cur", review_week, 2)
    _seed_week(repos, "pa", w1, 3)  # 失敗させる
    _seed_week(repos, "pb", w2, 4)  # 成功する
    _fail_join_for(repos, monkeypatch, ("pa",))

    outcome = _build_service(repos).run(_RUN_AT)

    saved = {m.review_week: m for m in repos["metrics"].list_all()}
    assert set(saved) == {review_week, w2}
    assert saved[review_week].sample_count == 2
    assert saved[w2].sample_count == 4
    assert outcome.past_weeks_metrics_recomputed_by_week == {w2: 1}
    assert list(outcome.past_weeks_join_failed) == [w1]


def test_aggregate_windows_requires_current_label_among_windows() -> None:
    """current weekは位置で暗黙に決めず、windowsに含まれるラベルを明示して渡す。"""
    service = object.__new__(WeeklyImprovementReviewService)
    with pytest.raises(ValueError):
        service._aggregate_windows([("W1", dt.date(2026, 8, 3), dt.date(2026, 8, 9))], "W0")
