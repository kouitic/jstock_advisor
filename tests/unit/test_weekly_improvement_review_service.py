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
from jstock_advisor.services.rule_version_service import RuleVersionService
from jstock_advisor.services.weekly_improvement_review_service import (
    WeeklyImprovementReviewService,
    _resolve_review_period,
)

_REGION = "ap-northeast-1"
# 2026-08-10はJSTで月曜。週次レビュー実行日として使う。
_RUN_AT = dt.datetime(2026, 8, 10, 10, 0, tzinfo=dt.UTC)  # JST 19:00


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch):
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


def test_evaluation_date_last_week_but_evaluated_at_this_week_is_included(
    aws_env, repos
) -> None:
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
    repos["recommendation"].save(
        _recommendation("r1", RecommendationType.BUY, "v1", period_start)
    )
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
    period_start, _, _ = _resolve_review_period(_RUN_AT)
    mid_week = dt.datetime.combine(period_start + dt.timedelta(days=2), dt.time(9), tzinfo=dt.UTC)
    for i in range(15):  # WATCHのdefault閾値=10
        rec_id = f"watch{i}"
        repos["recommendation"].save(
            _recommendation(rec_id, RecommendationType.WATCH, "v1", mid_week)
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


def test_success_rate_just_above_threshold_is_not_a_candidate_on_that_axis(
    aws_env, repos
) -> None:
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

    monkeypatch.setattr(
        module.github_issue_service, "process_candidate", _fake_process_candidate
    )

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

    monkeypatch.setattr(
        module.github_issue_service, "process_candidate", _fake_process_candidate
    )

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
