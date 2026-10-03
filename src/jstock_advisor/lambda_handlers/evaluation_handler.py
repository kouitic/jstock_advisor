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

Issue #673 / #674(HF-8 / HF-9): 評価結果のaggregate commit失敗(rollback・翌日再試行の契約は
不変)と、実行記録の永続化失敗(監査失敗でも評価runは成功として継続する契約は不変)を、
HF-0契約(#665)のHANDLED_FAILUREとして通知する。★ Productionで通知を有効にするには、
EvaluationFunctionへ`INCIDENT_NOTIFICATION_TOPIC_ARN`と`sns:Publish`を配線するinfra変更が別途要る
(本ファイルの範囲外。配線されるまでpublishはWARNINGログだけを残して失敗する)。

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
from jstock_advisor.lambda_handlers._review_execution_mode import reject_execution_mode
from jstock_advisor.services.evaluation_run_audit import record_run_summary
from jstock_advisor.services.incident_envelope_publisher import publish_incident_envelope
from jstock_advisor.services.provider_factory import build_real_provider_bundle
from jstock_advisor.services.recommendation_evaluation_service import (
    RecommendationEvaluationService,
    TimeBudget,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Issue #673(HF-8)・#674(HF-9): catchされた技術的部分失敗(HANDLED_FAILURE)の通知。
# job_nameは incident_message.py::_INTERNAL_NAME_TO_JOB(`"evaluation"`)がそのまま
# resolve_incident_job()で解決できる既存の値を使う(新規対応表を増やさない)。
_INCIDENT_SOURCE_EVALUATION = "evaluation"
_INCIDENT_JOB_NAME_EVALUATION = "evaluation"
_INCIDENT_FAILURE_TYPE = "UNHANDLED_EXCEPTION"
_FAILURE_STAGE_AGGREGATE_COMMIT = "AGGREGATE_COMMIT"
_REASON_CODE_AGGREGATE_COMMIT_FAILED = "EVALUATION_AGGREGATE_COMMIT_FAILED"
_FAILURE_STAGE_AUDIT_PERSIST = "AUDIT_PERSIST"
_REASON_CODE_AUDIT_PERSIST_FAILED = "EVALUATION_AUDIT_PERSIST_FAILED"


def _notify_handled_failure(
    failure_stage: str, reason_code: str, failure_count: int, now: dt.datetime
) -> None:
    """Issue #673 / #674: catchされた技術的部分失敗をHF-0契約(#665)でUSER通知する。

    envelopeはallowlistのキーのみ(stock_code / owner / holding_id / stack trace /
    生のexception messageは含めない。`publish_incident_envelope()`が最後の防御として再確認する)。
    `failure_class = HANDLED_FAILURE`のため、GitHub Issueは自動起票されない(HF-0)。
    評価本体のrollback・翌日再試行の既存契約はここでは一切変更しない。
    """
    envelope = {
        "source": _INCIDENT_SOURCE_EVALUATION,
        "job_name": _INCIDENT_JOB_NAME_EVALUATION,
        "failure_stage": failure_stage,
        "failure_type": _INCIDENT_FAILURE_TYPE,
        "reason_code": reason_code,
        "occurred_at": now.isoformat(),
        "failure_count": failure_count,
        "failure_class": "HANDLED_FAILURE",
    }
    publish_incident_envelope(envelope)


def _notify_handled_failure_safely(
    failure_stage: str, reason_code: str, failure_count: int, now: dt.datetime
) -> None:
    """`_notify_handled_failure()`の失敗(SNS権限不足・Topic ARN未設定等)が、評価本体・
    監査記録・戻り値を絶対に妨げないためのラッパー(通知自体の失敗で評価runをFAILさせると、
    Lambdaのasync retryで評価処理全体が不要に再実行されるため。#114と同じ理由)。

    失敗は握りつぶさず、WARNINGログへ残す(例外の型のみ。内容・識別子は出さない。#135)。
    """
    try:
        _notify_handled_failure(failure_stage, reason_code, failure_count, now)
    except Exception as exc:  # noqa: BLE001 - HANDLED_FAILURE通知自体の失敗で本処理を止めない
        logger.warning(
            "evaluation_handler: failed to publish HANDLED_FAILURE envelope "
            "failure_stage=%s error_type=%s",
            failure_stage,
            type(exc).__name__,
        )


def _build_time_budget(context: object) -> TimeBudget:
    """Lambda contextから時間予算を作る。

    contextが残時間を提供しない場合(ローカル実行・テスト)は無制限として扱う
    (この場合はタイムアウト自体が存在しないため、打ち切りの必要が無い)。
    """
    if hasattr(context, "get_remaining_time_in_millis"):
        return TimeBudget(source=context)
    return TimeBudget()


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    reject_execution_mode(event, handler_name="evaluation")
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

    # Issue #673(HF-8): 評価結果のaggregate commitに失敗した評価は「保存しない」へ倒れ、
    # 翌日再試行される(rollbackの契約。変更しない)。継続的に失敗していてもUSERが気づけない
    # ため、既に`EvaluationRunSummary`へ返却されている件数をHANDLED_FAILUREとして通知する。
    if summary.aggregate_commit_failed_count > 0:
        _notify_handled_failure_safely(
            _FAILURE_STAGE_AGGREGATE_COMMIT,
            _REASON_CODE_AGGREGATE_COMMIT_FAILED,
            summary.aggregate_commit_failed_count,
            now,
        )

    # Issue #114 Phase B1: run summaryをAuditLogへ永続化し、将来の週次改善レビューが
    # catch-up中かどうかを参照できるようにする。**失敗しても例外を伝播させない**
    # (評価本体は既に成功しており、ここでLambdaをFAILさせるとasync retryで
    # 評価処理全体が不要に再実行されるため)。失敗はrecord_run_summary()内の
    # ERRORログとこのフラグで表現する(無音のfail-softにはしない)。
    audit_persisted = record_run_summary(
        summary,
        run_started_at=now,
        run_completed_at=dt.datetime.now(dt.UTC),
    )
    # Issue #674(HF-9): 監査記録の永続化に失敗しても評価runは成功として継続する(上の契約。
    # 変更しない)。失敗はERRORログとaudit_persisted=falseだけでは利用者に届かないため、
    # run単位の条件(累積値ではない)としてHANDLED_FAILUREを通知する。#673の件数
    # (aggregate commit失敗 = 評価結果のrollback)とは別の条件であり、混同しない。
    if not audit_persisted:
        _notify_handled_failure_safely(
            _FAILURE_STAGE_AUDIT_PERSIST,
            _REASON_CODE_AUDIT_PERSIST_FAILED,
            1,
            now,
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
        # Issue #114 Phase B1: run summaryをAuditLogへ永続化できたか。
        # falseでも評価本体は成功している(監査記録だけが欠けている)。
        "audit_persisted": audit_persisted,
    }
