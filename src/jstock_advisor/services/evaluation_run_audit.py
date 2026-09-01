"""定点評価のrun summaryをAuditLogへ記録する(Issue #114 Phase B1)。

`EvaluationRunSummary`はこれまでCloudWatchの構造化ログとLambdaの戻り値にしか
存在せず、**他のLambdaから参照できる永続データが無かった**。#114 Phase Aの調査で、
週次改善レビューがcatch-up中かどうかを判定するにはこのsummaryを永続化する必要が
あると確認したため、既存の`jstock-audit_log`へ`decision_type`を分けて記録する
(新規テーブル・schema変更・migration・backfillはいずれも不要)。

記録の形は`watchlist_screening_audit.record_batch_audit()`を踏襲する
(銘柄によらないバッチ単位のauditという同じ性質のため、新しい抽象を作らない)。
`RecommendationEvaluationService`はAWS非依存の純粋なサービスとして
`EvaluationRunSummary`を返すだけであり、永続化の責務はこのモジュールが持つ。

**保証範囲(Issue #114 Phase A・B1で明示的に決めた境界)**:
正常完了とtime budgetによる自主終了のsummaryのみ記録する。OOM・タイムアウトでは
Lambdaのプロセスが強制終了しハンドラ終端へ到達しないため、**記録は残らない**。
その検知は#113で追加したCloudWatch Alarm(Errors)の責務であり、
ここに別のfailure collectorを作らない。読み取り側(#114 B3)は
「記録が無い/古すぎる/未知のstatus」をすべてUNKNOWNへ倒すfail-closeで扱うため、
記録欠落があっても「catch-up完了」と誤判定しない。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING, Any

from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.buy_signal_service import RULE_VERSION_PLACEHOLDER

if TYPE_CHECKING:  # pragma: no cover - 型チェック専用(実行時importの循環を避ける)
    from jstock_advisor.services.recommendation_evaluation_service import EvaluationRunSummary

logger = logging.getLogger(__name__)

DECISION_TYPE_EVALUATION_RUN_SUMMARY = "evaluation_run_summary"

# run_statusはB1では次の2値のみ。将来値が増えても読み取り側(#114 B3)が
# 未知の値をUNKNOWN(fail-close)として扱う契約のため、安全側に倒れる。
RUN_STATUS_COMPLETED = "COMPLETED"
RUN_STATUS_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"

# 永続化失敗時の構造化ログのイベント名(運用手順・アラート設定から参照する)。
PERSIST_FAILED_EVENT = "evaluation_run_summary_persist_failed"


def build_audit_id(run_started_at: dt.datetime) -> str:
    """決定的なaudit_id。

    同一invocation内で保存処理だけを再試行しても重複記録されないようにする
    (`AuditService.record_if_absent()`と組み合わせる)。既存の
    `f"watchlist_batch_audit:{batch_id}"`と同じく`<用途>:<識別子>`形式に揃える。

    **Lambdaのasync retry(初回/retry1/retry2)は`run_started_at`が異なるため
    別のaudit_idになり、別runとして記録される。** 各attemptの結果を個別に
    残すのが監査として正しく、読み取り側は`run_started_at`が最大のものを見る。
    """
    return f"{DECISION_TYPE_EVALUATION_RUN_SUMMARY}:{run_started_at.isoformat()}"


def resolve_run_status(summary: EvaluationRunSummary) -> str:
    return RUN_STATUS_BUDGET_EXHAUSTED if summary.budget_exhausted else RUN_STATUS_COMPLETED


def record_run_summary(
    summary: EvaluationRunSummary,
    run_started_at: dt.datetime,
    run_completed_at: dt.datetime,
    audit_service: AuditService | None = None,
) -> bool:
    """run summaryをAuditLogへ記録する。永続化できたらTrue。

    **例外を呼び出し側へ伝播させない。** 評価本体(EvaluationResultの保存)は
    既に成功しているため、監査書き込みの失敗でLambdaをFAILさせると
    async retryで評価処理全体(外部provider呼び出し・DynamoDB read/write)が
    不要に再実行される。そのためここで捕捉し、**ERRORログとFalseで表現する**
    (無音のfail-softにはしない。呼び出し側は戻り値を`audit_persisted`として返す)。

    既に同じaudit_idの記録が存在する場合もTrueを返す(冪等な成功)。
    """
    service = audit_service or AuditService()
    try:
        service.record_if_absent(
            audit_id=build_audit_id(run_started_at),
            decision_type=DECISION_TYPE_EVALUATION_RUN_SUMMARY,
            stock_code=None,
            input_values=_build_input_values(run_started_at, run_completed_at, summary),
            calculation_formulas={},
            output_values=_build_output_values(summary),
            data_sources=[],
            rule_version=RULE_VERSION_PLACEHOLDER,
            timestamp=run_completed_at,
        )
    except Exception as exc:  # noqa: BLE001 - 監査書き込みの失敗で評価runを失敗させない
        logger.error(
            "event=%s error_type=%s error_summary=%s run_started_at=%s "
            "backlog_remaining=%d budget_exhausted=%s",
            PERSIST_FAILED_EVENT,
            type(exc).__name__,
            str(exc)[:200],
            run_started_at.isoformat(),
            summary.backlog_remaining,
            summary.budget_exhausted,
        )
        return False
    return True


def _build_input_values(
    run_started_at: dt.datetime,
    run_completed_at: dt.datetime,
    summary: EvaluationRunSummary,
) -> dict[str, Any]:
    """runを識別するメタデータ(B1で新規に生成する3項目)。

    `run_started_at`は読み取り側が「最新run」を決めるための順序キー、
    `run_status`は「その値を信頼してよいrunか」の判定に使う。
    実行種別(自然スケジュール/manual invoke)は現行のhandlerが
    execution_contextを保持しておらず**識別できない**ため、ここでは持たない
    (推測で埋めない。必要になった時点で別途設計する)。
    """
    return {
        "run_started_at": run_started_at.isoformat(),
        "run_completed_at": run_completed_at.isoformat(),
        "run_status": resolve_run_status(summary),
    }


def _build_output_values(summary: EvaluationRunSummary) -> dict[str, Any]:
    """`EvaluationRunSummary`の全項目を意味を変えずそのまま記録する。

    due_count / already_evaluated_count / pending_count / backlog_remaining は
    **(recommendation, horizon)の組**を単位とし、pending_recommendation_countだけが
    Recommendation件数を単位とする(#113のsummary定義と同じ)。
    """
    return {
        "due_count": summary.due_count,
        "already_evaluated_count": summary.already_evaluated_count,
        "pending_count": summary.pending_count,
        "pending_recommendation_count": summary.pending_recommendation_count,
        "evaluated_count": summary.evaluated_count,
        "skipped_due_to_data_error_count": summary.skipped_due_to_data_error_count,
        "business_evaluated_count": summary.business_evaluated_count,
        "calendar_evaluated_count": summary.calendar_evaluated_count,
        "business_skipped_count": summary.business_skipped_count,
        "calendar_skipped_count": summary.calendar_skipped_count,
        "backlog_remaining": summary.backlog_remaining,
        "budget_exhausted": summary.budget_exhausted,
        "recommendations_scanned": summary.recommendations_scanned,
        "missing_recommendation_count": summary.missing_recommendation_count,
        "provider_call_count": summary.provider_call_count,
        "duration_ms": summary.duration_ms,
    }


# --- 読み取り側の契約(実装は#114 Phase B3) -------------------------------
#
# B1では**readerを実装しない**。`jstock-audit_log`は33,000件規模・GSI無しであり、
# 「最新run」をlist_all()/full Scanで探す実装をWeeklyReviewFunction
# (Timeout 300秒 / Memory 512MB)へ持ち込むと、#113で除去したのと同じ
# 全件materialize構造を再導入することになる。
#
# B3で実装する際の契約(Phase Aで確定済み):
#
#   CatchUpState = ONGOING | COMPLETE | UNKNOWN
#     ONGOING   最新runがCOMPLETED/BUDGET_EXHAUSTEDかつbacklog_remaining > 0
#     COMPLETE  最新runがCOMPLETED/BUDGET_EXHAUSTEDかつbacklog_remaining == 0
#     UNKNOWN   記録が無い / run_statusが未知 / 最新runが直近スケジュールより古い
#
#   **UNKNOWNをCOMPLETEとして扱わない(fail-close)。**
#   すなわち「最新runが失敗・OOM・タイムアウトしたとき、より古い成功runの
#   backlog_remaining==0を採用してはならない」。
#
# 取得方式(pointer行 / 決定的idでのGetItem / 別索引)は、B3で実際に必要な
# 問い合わせ("最新1件"なのか"直近N時間以内に完了したrunがあるか"なのか)と、
# #113のProduction実測(catch-upが何run続くか)が出てから選定する。
