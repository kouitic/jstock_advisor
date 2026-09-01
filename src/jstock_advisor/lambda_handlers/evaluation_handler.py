"""定点評価Lambda(schedule.yaml point_in_time_evaluation、平日18:00)。

CLIの`jstock evaluation run --source real`と同じロジックをEventBridge
Scheduler経由で自動実行する薄いアダプタ。通知は行わない(CLIと同様)。

振り返り機能改修: 既存の営業日ベース評価に加え、週次改善レビューが使うJST暦日
ベース評価(既定7暦日後)も同じLambda・同じスケジュールで実行する(要求仕様1.1節
「日次評価」)。

判定精度向上機能Phase A: DecisionSnapshotの成績評価(5/20/60/120/250営業日)は、
専用のEvaluationResultを新規生成せず、この既存の定点評価が
RecommendationType別ホライズン(config/schedule.yamlのall_types_common)で
既に生成しているEvaluationResultをrecommendation_id経由でそのまま再利用する
(DecisionPerformanceService参照)。よってこのハンドラの評価ロジックは変更不要。

Issue #113(2026-08-31): 従来は`run_due_evaluations()`と
`run_due_calendar_evaluations()`を順に呼び、1回の実行で
`jstock-recommendations`(約118MB)を2回フルScanしていた。さらに暦日評価が
後段にあったため、前段のコスト増大により暦日評価へ到達しなくなっていた。
現在は`run_due_evaluations_single_pass()`で1パスにまとめ、
Lambda contextの残時間を予算として**タイムアウトで殺される前に正常終了**し、
必ずrun summaryを出力する。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.services.provider_factory import build_real_provider_bundle
from jstock_advisor.services.recommendation_evaluation_service import (
    RecommendationEvaluationService,
    TimeBudget,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _build_time_budget(context: object) -> TimeBudget:
    """Lambda contextから時間予算を作る。

    contextが残時間を提供しない場合(ローカル実行・テスト)は無制限として扱う
    (この場合はタイムアウト自体が存在しないため、打ち切りの必要が無い)。
    """
    if hasattr(context, "get_remaining_time_in_millis"):
        return TimeBudget(source=context)
    return TimeBudget()


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    calendar = BusinessCalendar.from_config(config.holiday_calendar)
    providers = build_real_provider_bundle(now, config)
    service = RecommendationEvaluationService(
        market_data_provider=providers.market_data, config=config, business_calendar=calendar
    )
    logger.info("evaluation_handler start: now=%s", now.isoformat())

    outcome = service.run_due_evaluations_single_pass(
        now,
        calendar_horizon_days=config.review_improvement.evaluation_horizon_days,
        budget=_build_time_budget(context),
    )

    summary = outcome.summary

    # Issue #113: 部分実行(予算切れ)でも必ずここへ到達し、進捗が観測できるようにする。
    logger.info(
        "evaluation_handler done: evaluated=%d (business=%d calendar=%d) skipped=%d "
        "due_horizons=%d already_evaluated=%d pending_horizons=%d "
        "pending_recommendations=%d backlog_remaining=%d "
        "budget_exhausted=%s recommendations_scanned=%d missing=%d "
        "provider_calls=%d duration_ms=%d",
        summary.evaluated_count,
        summary.business_evaluated_count,
        summary.calendar_evaluated_count,
        summary.skipped_due_to_data_error_count,
        summary.due_count,
        summary.already_evaluated_count,
        summary.pending_count,
        summary.pending_recommendation_count,
        summary.backlog_remaining,
        summary.budget_exhausted,
        summary.recommendations_scanned,
        summary.missing_recommendation_count,
        summary.provider_call_count,
        summary.duration_ms,
    )
    if summary.backlog_remaining > 0:
        # backlog recovery中であることを明示する(catch-up期間中のweekly-reviewは
        # 通常週と同等に解釈できない。docs/functional_spec.md 12.4節参照)。
        logger.warning(
            "evaluation backlog remaining: %d (budget_exhausted=%s)",
            summary.backlog_remaining,
            summary.budget_exhausted,
        )

    return {
        # 既存の戻り値キーは維持する(呼び出し側・ログ解析の互換性のため)。
        "evaluated": summary.business_evaluated_count,
        "skipped_due_to_data_error": summary.business_skipped_count,
        "calendar_evaluated": summary.calendar_evaluated_count,
        "calendar_skipped_due_to_data_error": summary.calendar_skipped_count,
        # Issue #113で追加した可観測性フィールド。
        "due_count": summary.due_count,
        "already_evaluated_count": summary.already_evaluated_count,
        "pending_count": summary.pending_count,
        "pending_recommendation_count": summary.pending_recommendation_count,
        "backlog_remaining": summary.backlog_remaining,
        "budget_exhausted": summary.budget_exhausted,
        "recommendations_scanned": summary.recommendations_scanned,
        "missing_recommendation_count": summary.missing_recommendation_count,
        "provider_call_count": summary.provider_call_count,
        "duration_ms": summary.duration_ms,
    }
