"""ウォッチリスト自動追加(候補ユニバース本格対応)のBatch Reconciler Lambda(2/17節)。

EventBridge毎時トリガー。`batch_processing_timeout_hours`を超えて`DISPATCHING`/
`RUNNING`のまま放置されたバッチのタイムアウト検知・終端確定、および
`TIMEOUT_FINALIZING`/`TIMEOUT_FINALIZE_FAILED`バッチの途中再開を行う。

処理方針(2節):
- `DISPATCHING`でタイムアウト: `DISPATCH_FAILED`へ(候補リスト自体が未確定のため
  finalize系の処理は一切行わない)。
- `RUNNING`: まず`try_finalize_if_ready`(`maybe_finalize`経由)で救済を試みる
  (実際には全件完了しているが、最後の完了主体のfinalize呼び出し自体が
  クラッシュ等で失敗していたケースを、タイムアウト扱いにする前に正規の完了として
  救済する)。救済できずタイムアウトしていれば`TIMEOUT_FINALIZING`へ。
- `TIMEOUT_FINALIZING`/`TIMEOUT_FINALIZE_FAILED`: 17節の再計算方式(案C)で
  `completed`を補正しながら、未完了行を件数上限まで`FAILED`確定する。
- `NOTIFICATION_FAILED`(運用ハードニング第3弾1節): LINE送信のみが例外で
  失敗した状態。`notification_failure_count`が上限未満なら`retry_notification`で
  通知のみを再試行する(ウォッチリスト書込みは再実行されない)。

Issue #506(#132 O-1。上記の既存タイムアウト回復処理とは独立した相乗り検知):
S-2(missed schedule。本日のNEW_CANDIDATE_SCREENING試行が無い)/ S-4(候補
ユニバース取得失敗が3営業日連続)を検知し、IncidentNotificationTopicへ
Internal structured incident payloadをpublishする(#503のIncidentNotifierFunction
が共通処理する。LINE送信・fingerprint計算・claim/release・本文生成はここでは
一切持たない)。同一reason_codeへの通知は1日1回(JST)に抑止する。

Issue #507(#132 U-5。同じ相乗り検知にS-6/S-7を追加): S-6(SQS
ApproximateAgeOfOldestMessageが15分以上600秒を超えて継続=queue backlog。
watchlist-workerのThrottles/Invocations比率〔throttle_rate〕は補助指標として
ログにのみ出し、SNS envelopeには含めない)/ S-7(watchlistからの削除実績が
3営業日連続で0件)。いずれも#506と同じ経路・同じ1日1回抑止を再利用する。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Any, NoReturn

import boto3

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import ExecutionMode
from jstock_advisor.domain.entities.watchlist import WatchlistRemovalHistory
from jstock_advisor.domain.jst import evaluation_date_jst, to_jst
from jstock_advisor.infrastructure.aws.batch_tracker import (
    BatchFamily,
    UnknownWatchlistJobTypeError,
    WatchlistBatchStatus,
    WatchlistJobType,
    get_completion_batch,
    get_incident_detector_state,
    get_streak_state,
    get_watchlist_batch,
    list_new_candidate_screening_batches,
    list_stale_maintenance_triggers,
    list_watchlist_batches_by_status,
    mark_dispatch_failed,
    mark_finalizing_stuck_as_failed,
    record_incident_detector_state,
    record_streak_state,
    resolve_watchlist_job_type,
    run_timeout_finalization_pass,
    set_timeout_finalize_completed_count,
    transition_timeout_finalizing_to_failed,
    transition_timeout_finalizing_to_timed_out,
    try_acquire_timeout_finalization,
)
from jstock_advisor.infrastructure.aws.watchlist_rotation_dispatch_lease import (
    release_rotation_dispatch_lease,
)
from jstock_advisor.infrastructure.aws.watchlist_rotation_state import DEFAULT_ROTATION_ID
from jstock_advisor.infrastructure.line.client import (
    LineClient,
    LineCredentialsMissingError,
    QuickReplyButton,
    build_live_line_client_from_env,
)
from jstock_advisor.infrastructure.local_repository.notification_claim_repository import (
    NotificationClaimRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.local_repository.watchlist_removal_history_repository import (
    WatchlistRemovalHistoryRepository,
)
from jstock_advisor.lambda_handlers._fanout import dispatch_async
from jstock_advisor.lambda_handlers._finalize_recovery import build_finalize_only_payload
from jstock_advisor.lambda_handlers._watchlist_execution_mode import reject_execution_mode
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.provider_factory import build_real_provider_bundle
from jstock_advisor.services.watchlist_batch_finalizer import (
    MAINTENANCE_UNIVERSE_PROVIDER,
    MaintenanceTriggerOutcome,
    compute_batch_metrics,
    maybe_finalize,
    maybe_finalize_maintenance,
    maybe_trigger_maintenance,
    retry_finalize,
    retry_notification,
)
from jstock_advisor.services.watchlist_data_cache import build_cached_provider_bundle
from jstock_advisor.services.watchlist_screening_audit import (
    record_batch_audit,
    resolve_batch_execution_mode,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Medium修正(2026-08再レビュー・再々レビュー): maintenance_trigger_retriedへ
# 計上してよいのは「実際にLambda invoke()を試行した」ケースのみ(handler()内で
# 使用)。CONFIGURATION_ERROR(環境変数未設定によりinvoke()呼び出し自体に
# 到達しない)はここに含めない(invoke未試行のため、別カウンタで区別する)。
_MAINTENANCE_RETRY_ATTEMPTED_OUTCOMES = frozenset(
    {
        MaintenanceTriggerOutcome.TRIGGERED,
        MaintenanceTriggerOutcome.INVOKE_FAILED,
    }
)

_RECONCILE_TARGET_STATUSES = [
    WatchlistBatchStatus.DISPATCHING,
    WatchlistBatchStatus.RUNNING,
    # 運用ハードニング第2弾2節: finalize処理中の4段階すべてをスタック検知の対象に
    # する(旧FINALIZING単一状態を細分化)。
    WatchlistBatchStatus.FINALIZE_PREPARING,
    WatchlistBatchStatus.WATCHLIST_WRITE_COMPLETED,
    WatchlistBatchStatus.NOTIFICATION_PENDING,
    WatchlistBatchStatus.NOTIFICATION_SENT,
    WatchlistBatchStatus.FINALIZE_FAILED,
    # 運用ハードニング第3弾1節: 通知送信のみが例外で失敗した状態
    # (finalize全体はFINALIZE_FAILEDにならない、通知のみ再試行する)。
    WatchlistBatchStatus.NOTIFICATION_FAILED,
    WatchlistBatchStatus.TIMEOUT_FINALIZING,
    WatchlistBatchStatus.TIMEOUT_FINALIZE_FAILED,
]

# 運用ハードニング第2弾2節: finalize処理中とみなす状態一覧(スタック検知の対象)。
_FINALIZE_IN_PROGRESS_STATUSES = frozenset(
    {
        WatchlistBatchStatus.FINALIZE_PREPARING.value,
        WatchlistBatchStatus.WATCHLIST_WRITE_COMPLETED.value,
        WatchlistBatchStatus.NOTIFICATION_PENDING.value,
        WatchlistBatchStatus.NOTIFICATION_SENT.value,
    }
)


# --- Issue #506(#132 O-1): S-2(missed schedule)/ S-4(候補ユニバース連続失敗)検知 ---
#
# USER決定(#506 issuecomment-5805034769。転記元 INSTRUCTION_ID =
# HANAKO-20260924-USERDECISION-506)どおり:
#   - watchlist系のNEW_CANDIDATE_SCREENINGに限定する(他10関数は対象外)
#   - S-4の閾値は3営業日連続
#   - 通知はreason_codeごとに1日1回(JST)。#503の30分dedupには依存せず、
#     reason_code + last_notified_date_jstをBatchRunsTable(既存)へ保持する
#   - reconcilerはallowlist済みのincident envelopeを組み立てSNS publishのみ行う
#     (LINE client構築・LINE push・fingerprint計算・claim/release・本文生成は
#     一切持たない。すべてIncidentNotifierFunction〔#503〕側の共通処理に委ねる)
#   - cloudwatch:GetMetricDataは追加しない(「起動されなかった」/「起動後に途中
#     失敗した」の区別は#506単独では行わない)

_INCIDENT_SOURCE_WATCHLIST_RECONCILER = "watchlist_reconciler"
# IncidentJobの対応表(domain/notification/incident_message.py)は既に
# "watchlist-dispatcher" → WATCHLIST_SCREENING を持つため、新しい対応の追加は不要。
_INCIDENT_JOB_NAME_WATCHLIST_DISPATCHER = "watchlist-dispatcher"
_INCIDENT_JOB_NAME_WATCHLIST_WORKER = "watchlist-worker"

_REASON_CODE_WATCHLIST_MISSED_SCHEDULE = "watchlist_missed_schedule"
_REASON_CODE_WATCHLIST_UNIVERSE_LOAD_FAILURE_STREAK = "watchlist_universe_load_failure_streak"
_REASON_CODE_WATCHLIST_QUEUE_BACKLOG = "watchlist_queue_backlog"
_REASON_CODE_WATCHLIST_DELETION_ZERO_STREAK = "watchlist_deletion_zero_streak"

# USER決定: 3営業日連続(#506 issuecomment-5805034769。2日=一過性障害を拾いやすい、
# 5日=検知が遅すぎる、3日=バランス良いとして確定)。
_UNIVERSE_LOAD_FAILURE_STREAK_THRESHOLD_DAYS = 3

# USER決定(#507 issuecomment。転記元 HANAKO-20260924-USERDECISION-507)どおり:
#   - S-6主指標はSQS OldestMessageAge。閾値>600秒が15分以上継続
#     (Period=300sなら3 datapoint連続相当)。throttle_rateは補助指標のみで
#     単独ではincidentにしない
#   - S-7は削除実績0件が3営業日連続でwarning(重大incidentではない)
#   - 永続化は既存batch/audit系のみ。新規Table禁止
_QUEUE_BACKLOG_THRESHOLD_SECONDS = 600
_QUEUE_BACKLOG_SUSTAINED_DATAPOINTS = 3
_QUEUE_BACKLOG_METRIC_PERIOD_SECONDS = 300
_QUEUE_BACKLOG_LOOKBACK_MINUTES = 20
_DELETION_ZERO_STREAK_THRESHOLD_DAYS = 3

_METRIC_ID_OLDEST_MESSAGE_AGE = "oldest_message_age"
_METRIC_ID_THROTTLES = "throttles"
_METRIC_ID_INVOCATIONS = "invocations"

# dispatchの平日Scheduleはcron(0 6 ? * MON-FRI *) JST(infra/template.yaml
# WeekdayMorning)。reconciler自身はrate(1 hour)で起動時刻の固定アンカーを持たない
# (実測)。dispatch開始直後の一時的な未反映(コールドスタート等)をmissed scheduleと
# 誤判定しないよう、reconciler自身の実行周期(1時間)と同じ幅の猶予を置く。
_MISSED_SCHEDULE_GRACE_HOUR_JST = 7

# #506 USER決定のInternal payload allowlist(incident_notifier_handler.pyの
# _normalize_internal_message()が受け付けるキーと同一。stock_code/owner/
# holding_id/stack trace/生exception message/AWS account ID/ARN/request ID等は
# 決して含めない)。
_SNS_PAYLOAD_ALLOWLIST = frozenset(
    {
        "source",
        "job_name",
        "failure_stage",
        "failure_type",
        "reason_code",
        "occurred_at",
        "failure_count",
        "consecutive_days",
        "is_ongoing",
    }
)


def _started_at_jst_date(batch_item: dict[str, Any]) -> dt.date | None:
    """BatchRunsTable項目の`started_at`(UTCのISO文字列)をJST暦日へ変換する。

    ★ `batch_id`の日時部分(UTC基準。`now.strftime(...)`)ではなく`started_at`を
    使う(#506 D1/実装時の注意点として記録済み: batch_idの日時はUTC基準であり、
    JST営業日境界とずれる)。`started_at`は`try_acquire_dispatch_lease()`が
    `if_not_exists`で設定するため、成功・失敗を問わずbatch項目に必ず存在する。
    """
    raw = batch_item.get("started_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return evaluation_date_jst(parsed)


def _previous_business_day(calendar: BusinessCalendar, date: dt.date) -> dt.date:
    current = date - dt.timedelta(days=1)
    while not calendar.is_business_day(current):
        current -= dt.timedelta(days=1)
    return current


def _detect_watchlist_missed_schedule(
    batches: list[dict[str, Any]], calendar: BusinessCalendar, now: dt.datetime
) -> dict[str, Any] | None:
    """S-2: 本日(JST営業日)、NEW_CANDIDATE_SCREENINGの試行が一件も無いことを検知する。

    正常な非営業日(休日・週末)は対象外(#132本文4節: 仕様どおりのfail-softは障害
    としない)。「起動されなかった」と「起動後に候補ユニバース取得等で失敗した」は
    ここでは区別しない(cloudwatch:GetMetricDataを追加しないというUSER決定により、
    #506単独ではその区別を持たない。区別が必要な場合は#504のErrors監視と組み合わせる)。
    `batches`は起動できたか否かに関わらず全status(COMPLETED/ABORTEDを含む)を含む
    ため、DISPATCH_FAILEDで終端した試行も「起動された」側として正しく数えられる。
    """
    today_jst = evaluation_date_jst(now)
    if not calendar.is_business_day(today_jst):
        return None
    if to_jst(now).hour < _MISSED_SCHEDULE_GRACE_HOUR_JST:
        return None
    for batch_item in batches:
        if _started_at_jst_date(batch_item) == today_jst:
            return None
    return {
        "source": _INCIDENT_SOURCE_WATCHLIST_RECONCILER,
        "job_name": _INCIDENT_JOB_NAME_WATCHLIST_DISPATCHER,
        "failure_stage": "SCHEDULE",
        "failure_type": "MISSED_SCHEDULE",
        "reason_code": _REASON_CODE_WATCHLIST_MISSED_SCHEDULE,
        "occurred_at": now.isoformat(),
    }


def _evaluate_and_persist_business_day_streak(
    reason_code: str, today_continues_streak: bool, calendar: BusinessCalendar, now: dt.datetime
) -> int | None:
    """営業日ごとに1回だけインクリメンタルに評価・永続化する汎用実装(副作用あり:
    DynamoDB書き込みを行う。戻り値は「本日時点で確定した」streak_countで、
    本日分をまだ評価できない場合は`None`を返す)。

    #506 S-4(候補ユニバース連続失敗)・#507 S-7(watchlist削除実績ゼロ連続)が
    共有する(いずれも「ある条件を満たした営業日が何日連続しているか」という
    同型の判定のため)。

    ★ #506レビューF1是正: 過去の実データ行(BatchRunsTable等)を読み返して
    連続日数を再計算する設計を採らない。呼び出し元が`today_continues_streak`
    (本日分のみの判定結果)を渡し、この関数自身が`get_streak_state`/
    `record_streak_state`(reason_codeごとの別行)へ積み上げる。評価に断絶
    (reconcilerが長期間停止していた等)があれば継続性を信用せず、本日の
    結果だけでリセットする(誤って長い連続を主張しないためのfail-safe。
    ログで可視化する)。

    ★ #506レビューiteration 2 R1/R2是正: 非営業日・猶予時刻前は、直前に
    永続化された`streak_count`(=過去の営業日の状態)をそのまま返さない。
    これを返すと、(R1)非営業日にも関わらず「本日確定した」として毎日
    再publishされてしまい(1日1回抑止のキーが暦日単位のため、非営業日も
    「新しい1日」として扱われてしまう)、(R2)猶予時刻前の実行が前日の
    ステータスを「本日の状態」として誤ってpublishし、その後の本日分の
    正しい再評価(結果が変わっていても)を1日1回抑止が握りつぶす。
    「本日時点でまだ確定していない」ことを`None`で明示的に表現し、
    呼び出し元(閾値判定関数)がpublish対象にしない。
    """
    today_jst = evaluation_date_jst(now)
    if not calendar.is_business_day(today_jst):
        return None
    if to_jst(now).hour < _MISSED_SCHEDULE_GRACE_HOUR_JST:
        return None

    state = get_streak_state(reason_code)
    prior_streak = int(state["streak_count"]) if state else 0
    last_evaluated = state.get("last_evaluated_date_jst") if state else None
    if last_evaluated == today_jst.isoformat():
        return prior_streak  # 本日分は評価済み(今日確定した値をそのまま返す)

    expected_previous = _previous_business_day(calendar, today_jst).isoformat()
    if last_evaluated is not None and last_evaluated != expected_previous:
        logger.warning(
            "watchlist reconciler: %s streak evaluation gap "
            "last_evaluated=%s expected_previous=%s today=%s (resetting from gap)",
            reason_code,
            last_evaluated,
            expected_previous,
            today_jst.isoformat(),
        )
        prior_streak = 0
    new_streak = prior_streak + 1 if today_continues_streak else 0
    record_streak_state(reason_code, today_jst.isoformat(), new_streak, now)
    return new_streak


def _evaluate_and_persist_universe_load_failure_streak(
    todays_batches: list[dict[str, Any]], calendar: BusinessCalendar, now: dt.datetime
) -> int | None:
    """S-4: 候補ユニバース取得の失敗(`failure_reason == "universe_load_failed"`)が
    何営業日連続しているかを評価する。

    ★ BatchRunsTableのTTL(`candidate_progress_ttl_hours`。既定72時間)は候補進捗行
    という短命なデータのためのものであり、週末・祝日を跨ぐ複数営業日の履歴を保持
    する契約ではない(実測: 週をまたぐと対象行が既にTTL経過で消えている。TTL削除
    自体も最大48時間遅延するため、同じ「3営業日連続」でも検知の成否が非決定的に
    なっていた)。そのため過去の行を読み返さず、`todays_batches`(本日分のみ。
    常に新しく期限切れの心配が無い)から「本日失敗したか」だけを読み取り、
    `_evaluate_and_persist_business_day_streak()`へ渡す。

    `mark_dispatch_failed(..., reason="universe_load_failed")`は
    `watchlist_dispatcher_handler.py`の`CandidateUniverseError`経路の1箇所のみが
    設定する(実装側で確認済み。`_collect_maintenance_targets()`は候補ユニバース
    providerに触れず、この例外を送出できない構造のため、この理由コードは
    NEW_CANDIDATE_SCREENINGにのみ発生する)。
    """
    today_jst = evaluation_date_jst(now)
    today_failed = any(
        batch_item.get("failure_reason") == "universe_load_failed"
        and _started_at_jst_date(batch_item) == today_jst
        for batch_item in todays_batches
    )
    return _evaluate_and_persist_business_day_streak(
        _REASON_CODE_WATCHLIST_UNIVERSE_LOAD_FAILURE_STREAK, today_failed, calendar, now
    )


def _detect_watchlist_universe_load_failure_streak(
    streak_count: int | None, now: dt.datetime
) -> dict[str, Any] | None:
    """S-4: 連続日数が閾値(3営業日)以上ならincident envelopeを返す(単発は対象外。
    #132本文/#234)。連続日数の評価・永続化自体は
    `_evaluate_and_persist_universe_load_failure_streak`が行う(この関数は
    その結果を閾値判定するだけの純粋関数)。`streak_count`が`None`(本日分は
    まだ評価できていない)なら検知しない。
    """
    if streak_count is None or streak_count < _UNIVERSE_LOAD_FAILURE_STREAK_THRESHOLD_DAYS:
        return None
    return {
        "source": _INCIDENT_SOURCE_WATCHLIST_RECONCILER,
        "job_name": _INCIDENT_JOB_NAME_WATCHLIST_DISPATCHER,
        "failure_stage": "UNIVERSE_LOAD",
        "failure_type": "CONSECUTIVE_UNIVERSE_LOAD_FAILURE",
        "reason_code": _REASON_CODE_WATCHLIST_UNIVERSE_LOAD_FAILURE_STREAK,
        "occurred_at": now.isoformat(),
        "failure_count": streak_count,
        "consecutive_days": streak_count,
        "is_ongoing": True,
    }


def _sorted_values_ascending_by_timestamp(result: dict[str, Any]) -> list[float]:
    """GetMetricDataの1件分の結果から、timestamp昇順(古い→新しい)のValuesを返す。

    ★ #507レビューF1是正: `Values`/`Timestamps`の並び順はAPIの`ScanBy`
    パラメータに依存する(boto3のAPIモデル: 省略時の既定は
    `TimestampDescending`〔新→古〕)。呼び出し側で`ScanBy=TimestampAscending`
    を明示していても、**それだけを信用せず**、ここで`Timestamps`を使って
    明示的に並べ替える(返却順の仮定に依存しない設計。降順のまま
    `datapoints[-3:]`のようにtail-sliceすると、直近3点ではなく最古3点を
    取ってしまい、①滞留の検知漏れ〔false negative〕/ ②解消済みの古い状態を
    検知し続ける〔stale〕という2方向の誤りが起こる)。
    """
    timestamps = result.get("Timestamps") or []
    values = result.get("Values") or []
    paired = sorted(zip(timestamps, values, strict=True), key=lambda pair: pair[0])
    return [value for _timestamp, value in paired]


def _fetch_watchlist_worker_metrics(now: dt.datetime) -> dict[str, list[float]]:
    """S-6: SQS OldestMessageAge(判定対象)・Lambda Throttles/Invocations
    (throttle_rate計算用の補助指標)を1回のGetMetricDataでまとめて取得する
    (副作用あり: CloudWatchへの読み取り専用API呼び出し。書き込みは一切行わない)。

    ★ `cloudwatch:GetMetricData`はCloudWatch側がリソースレベル権限自体を
    サポートしていない(メトリクスはARNを持たない。AWSの既知の制約であり
    本プロジェクト固有の設計判断ではない)ため、IAM側のResourceは"*"になる
    (infra/template.yamlのコメント参照)。

    Throttles/Invocationsの`Period`は、直近`_QUEUE_BACKLOG_LOOKBACK_MINUTES`分
    の問い合わせ窓とちょうど一致させる。

    ★ #507レビューiteration 2 R2是正: 以前はPeriod=86400(1日)を使っていたが、
    これは「表記が紛らわしい」という表現の問題ではなく、**実際に集計範囲が
    広がる正しさの問題**だった。サブちゃんが本番CloudWatchで実測した結果、
    `Period`が実際の問い合わせ窓(StartTime〜EndTime)より大きい場合、
    CloudWatchは`StartTime`を起点に`Period`長のバケットを構成し、
    `EndTime`の外側までデータを含めて集計する(実測: Period=86400・実際の
    窓20分に対し、値が約10倍〔2434 vs 実際の窓243〕になることを確認)。
    つまり旧実装のthrottle_rateは日次スケールの値で、直近20分の状況を
    表していなかった。Periodを問い合わせ窓と一致させることで、この
    過大集計そのものを解消する(表記の修正ではなく、集計対象を正しい
    範囲に収める修正)。
    """
    cloudwatch = boto3.client("cloudwatch")
    queue_name = os.environ["WATCHLIST_SCREENING_QUEUE_NAME"]
    function_name = os.environ["WATCHLIST_WORKER_FUNCTION_NAME"]
    start_time = now - dt.timedelta(minutes=_QUEUE_BACKLOG_LOOKBACK_MINUTES)
    lookback_period_seconds = _QUEUE_BACKLOG_LOOKBACK_MINUTES * 60
    response = cloudwatch.get_metric_data(
        MetricDataQueries=[
            {
                "Id": _METRIC_ID_OLDEST_MESSAGE_AGE,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/SQS",
                        "MetricName": "ApproximateAgeOfOldestMessage",
                        "Dimensions": [{"Name": "QueueName", "Value": queue_name}],
                    },
                    "Period": _QUEUE_BACKLOG_METRIC_PERIOD_SECONDS,
                    "Stat": "Maximum",
                },
                "ReturnData": True,
            },
            {
                "Id": _METRIC_ID_THROTTLES,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/Lambda",
                        "MetricName": "Throttles",
                        "Dimensions": [{"Name": "FunctionName", "Value": function_name}],
                    },
                    "Period": lookback_period_seconds,
                    "Stat": "Sum",
                },
                "ReturnData": True,
            },
            {
                "Id": _METRIC_ID_INVOCATIONS,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/Lambda",
                        "MetricName": "Invocations",
                        "Dimensions": [{"Name": "FunctionName", "Value": function_name}],
                    },
                    "Period": lookback_period_seconds,
                    "Stat": "Sum",
                },
                "ReturnData": True,
            },
        ],
        StartTime=start_time,
        EndTime=now,
        # #507レビューF1是正: 返却順の既定(TimestampDescending)に依存しない
        # よう明示するが、_sorted_values_ascending_by_timestamp()側でも
        # Timestampsを使って独立に並べ替える(二重の防御。片方が外れても
        # 誤判定しない)。
        ScanBy="TimestampAscending",
    )
    metrics: dict[str, list[float]] = {}
    for result in response.get("MetricDataResults", []):
        metric_id = result.get("Id")
        if metric_id is not None:
            metrics[metric_id] = _sorted_values_ascending_by_timestamp(result)
    return metrics


def _detect_watchlist_queue_backlog(
    metrics: dict[str, list[float]], now: dt.datetime
) -> dict[str, Any] | None:
    """S-6: SQS OldestMessageAgeが直近3 datapoint(Period=300秒=15分)連続で
    600秒を超えていることを検知する(USER決定どおり。瞬間的な超過だけでは
    incident化しない)。

    throttle_rate(watchlist-workerのThrottles/Invocations比率)は補助指標として
    ログにのみ出す(#132 H-30のallowlistに比率を運ぶフィールドが無いため、SNS
    envelopeには含めない。USER決定どおり単独ではincidentを発生させない。この
    「envelopeに含めない」という構造自体がその決定を強制する)。
    """
    throttles = sum(metrics.get(_METRIC_ID_THROTTLES, []))
    invocations = sum(metrics.get(_METRIC_ID_INVOCATIONS, []))
    # #507レビュー非BLOCKING指摘: 全スロットル(throttles>0だがinvocations=0。
    # 呼び出しが1件も受理されず「最も見たい状況」)でもログへ残すため、
    # ガードを`invocations > 0`ではなく`throttles + invocations > 0`にする
    # (どちらも0=直近window内にそもそも呼び出し試行が無かった、というときだけ
    # 出力を省略する)。
    if throttles + invocations > 0:
        throttle_rate = throttles / (throttles + invocations)
        logger.info(
            "watchlist reconciler: watchlist-worker 直近%d分のthrottle_rate=%.4f "
            "throttles=%.0f invocations=%.0f(補助指標。単独ではincidentにしない)",
            _QUEUE_BACKLOG_LOOKBACK_MINUTES,
            throttle_rate,
            throttles,
            invocations,
        )

    datapoints = metrics.get(_METRIC_ID_OLDEST_MESSAGE_AGE, [])
    recent = datapoints[-_QUEUE_BACKLOG_SUSTAINED_DATAPOINTS:]
    if len(recent) < _QUEUE_BACKLOG_SUSTAINED_DATAPOINTS:
        return None
    if not all(value > _QUEUE_BACKLOG_THRESHOLD_SECONDS for value in recent):
        return None
    return {
        "source": _INCIDENT_SOURCE_WATCHLIST_RECONCILER,
        "job_name": _INCIDENT_JOB_NAME_WATCHLIST_WORKER,
        "failure_stage": "QUEUE_BACKLOG",
        "failure_type": "OLDEST_MESSAGE_AGE_EXCEEDED",
        "reason_code": _REASON_CODE_WATCHLIST_QUEUE_BACKLOG,
        "occurred_at": now.isoformat(),
    }


def _evaluate_and_persist_watchlist_deletion_zero_streak(
    removal_history: list[WatchlistRemovalHistory], calendar: BusinessCalendar, now: dt.datetime
) -> int | None:
    """S-7: watchlistからの削除実績が0件の営業日が何日連続しているかを評価する。

    「母数が増えること」自体は正常業務(NEW_CANDIDATE_SCREENINGが営業日ごとに
    候補を追加する設計のため)であり異常ではない。異常とみなすのは「削除が機能
    していない」ことのみ(#224と同じ整理。#132本文4節)。

    ★ `WatchlistRemovalHistoryRepository`は「銘柄ごとの最新の削除のみ」を保持する
    設計であり(`removed_at`は再削除で上書きされる。完全な履歴はAuditLogTableが
    正本)、`readd_cooldown_days`(既定30日)のTTLで自動的に消える。3営業日分の
    判定には十分な保持期間があるが、同一銘柄が短期間に複数回削除された場合、
    古い削除イベントの`removed_at`が上書きで失われる可能性がある(readd_cooldown_
    daysが3営業日を大きく上回るため実務上の影響は無視できる規模と判断。PR本文に
    明記)。
    """
    today_jst = evaluation_date_jst(now)
    today_had_deletion = any(
        evaluation_date_jst(item.removed_at) == today_jst for item in removal_history
    )
    return _evaluate_and_persist_business_day_streak(
        _REASON_CODE_WATCHLIST_DELETION_ZERO_STREAK, not today_had_deletion, calendar, now
    )


def _detect_watchlist_deletion_zero_streak(
    streak_count: int | None, now: dt.datetime
) -> dict[str, Any] | None:
    """S-7: 削除実績ゼロの連続日数が閾値(3営業日)以上ならincident envelopeを
    返す。USER決定どおりwarning(重大incidentではない運用warning)として扱う
    (allowlist自体にseverityを表すフィールドは無いため、区別は#503側の運用判断
    〔本文の文言等〕に委ねる。本Issueのscopeは検知・通知の配線のみ)。
    """
    if streak_count is None or streak_count < _DELETION_ZERO_STREAK_THRESHOLD_DAYS:
        return None
    return {
        "source": _INCIDENT_SOURCE_WATCHLIST_RECONCILER,
        "job_name": _INCIDENT_JOB_NAME_WATCHLIST_DISPATCHER,
        "failure_stage": "WATCHLIST_SIZE",
        "failure_type": "DELETION_NOT_KEEPING_PACE",
        "reason_code": _REASON_CODE_WATCHLIST_DELETION_ZERO_STREAK,
        "occurred_at": now.isoformat(),
        "failure_count": streak_count,
        "consecutive_days": streak_count,
        "is_ongoing": True,
    }


def _publish_incident_envelope(envelope: dict[str, Any]) -> None:
    """allowlist済みのincident envelopeをIncidentNotificationTopicへpublishする。

    #506 USER決定のallowlist(source/job_name/failure_stage/failure_type/
    reason_code/occurred_at/failure_count/consecutive_days/is_ongoing)以外の
    キーは、この関数の呼び出し元(検知関数)が最初から持たせない構造にしているが、
    最後の防御としてここでも再確認する(fail-closed: allowlist外のキーがあれば
    publishせず例外にする)。
    """
    if not set(envelope) <= _SNS_PAYLOAD_ALLOWLIST:
        raise ValueError(f"incident envelope has non-allowlisted keys: {set(envelope)}")
    topic_arn = os.environ["INCIDENT_NOTIFICATION_TOPIC_ARN"]
    sns = boto3.client("sns")
    sns.publish(TopicArn=topic_arn, Message=json.dumps(envelope))


def _already_notified_today(reason_code: str, today_jst: dt.date) -> bool:
    state = get_incident_detector_state(reason_code)
    if state is None:
        return False
    return state.get("last_notified_date_jst") == today_jst.isoformat()


def _notify_if_new_today(
    envelope: dict[str, Any] | None, today_jst: dt.date, now: dt.datetime
) -> bool:
    """検知結果(あれば)を、本日まだ通知していない場合のみpublishする(1日1回抑止)。

    戻り値はpublishを実行したかどうか(handler()側のログ・返り値集計用)。
    """
    if envelope is None:
        return False
    reason_code = envelope["reason_code"]
    if _already_notified_today(reason_code, today_jst):
        return False
    _publish_incident_envelope(envelope)
    record_incident_detector_state(
        reason_code, today_jst.isoformat(), envelope.get("consecutive_days"), now
    )
    return True


def _scheduled_watchlist_dispatch_enabled(config: AppConfig) -> bool:
    """dispatcherが実際に起動する条件と同じkill switchを見る(#506レビューF2是正)。

    `watchlist_dispatcher_handler.handler()`は`wc.enabled and wc.scheduled_run_enabled`
    がFalseならbatch行を作る前に早期returnする(意図した運用停止。#132本文4節の
    「仕様どおりのfail-softは障害として扱わない」に該当)。この判定を見ずに
    「本日のbatchが無い」ことだけを見ると、意図的な停止中は毎営業日
    missed scheduleを誤検知し続けてしまう。S-2/S-4いずれもこの間は評価自体を
    見送る(streak状態も進めない。再開後は評価の断絶としてfail-safeに扱われる。
    `_evaluate_and_persist_universe_load_failure_streak`のgap検知を参照)。
    """
    wc = config.watchlist_screening
    return bool(wc.enabled and wc.scheduled_run_enabled)


def _detect_and_notify_watchlist_incidents(now: dt.datetime, config: AppConfig) -> dict[str, bool]:
    """S-2/S-4(#506)/ S-6/S-7(#507)を検知し、未通知のものだけ#503経路へpublishする
    (副作用あり。read-onlyではない: DynamoDB書き込み・CloudWatch読み取り・SQS
    読み取り・SNS publishを行う。CLAUDE.md §3参照)。
    """
    no_op = {
        "missed_schedule_notified": False,
        "universe_load_failure_streak_notified": False,
        "queue_backlog_notified": False,
        "deletion_zero_streak_notified": False,
    }
    if not _scheduled_watchlist_dispatch_enabled(config):
        return no_op

    calendar = BusinessCalendar.from_config(config.holiday_calendar)
    today_jst = evaluation_date_jst(now)
    todays_batches = [
        batch_item
        for batch_item in list_new_candidate_screening_batches()
        if _started_at_jst_date(batch_item) == today_jst
    ]
    missed_schedule = _detect_watchlist_missed_schedule(todays_batches, calendar, now)
    streak_count = _evaluate_and_persist_universe_load_failure_streak(todays_batches, calendar, now)
    universe_load_failure_streak = _detect_watchlist_universe_load_failure_streak(streak_count, now)

    worker_metrics = _fetch_watchlist_worker_metrics(now)
    queue_backlog = _detect_watchlist_queue_backlog(worker_metrics, now)

    removal_history = WatchlistRemovalHistoryRepository(
        config.watchlist_screening.auto_removal.readd_cooldown_days
    ).list_all()
    deletion_streak_count = _evaluate_and_persist_watchlist_deletion_zero_streak(
        removal_history, calendar, now
    )
    deletion_zero_streak = _detect_watchlist_deletion_zero_streak(deletion_streak_count, now)

    return {
        "missed_schedule_notified": _notify_if_new_today(missed_schedule, today_jst, now),
        "universe_load_failure_streak_notified": _notify_if_new_today(
            universe_load_failure_streak, today_jst, now
        ),
        "queue_backlog_notified": _notify_if_new_today(queue_backlog, today_jst, now),
        "deletion_zero_streak_notified": _notify_if_new_today(deletion_zero_streak, today_jst, now),
    }


class _CredentialDeferredLineClient:
    """LINE認証情報が無いときに渡す、送信の瞬間に必ず失敗するclient(Issue #117)。

    reconcilerは複数の独立した回復処理を担う「最後の安全網」であり、LINEを使うのは
    finalizerのPhase 3(通知)の1点だけである。認証情報の欠落を通知サービスの「構築失敗」として
    扱うと、その手前のPhase 1/2(ランキング確定・ウォッチリスト登録)や、通知と無関係な回復処理
    まで止まり、24時間を超えるとTIMED_OUT(部分結果は登録しない)で候補が失われる。

    そこで欠落は「実際の送信時の失敗」として扱う。送信メソッド(push_message等)は**必ず**
    `LineCredentialsMissingError`を送出し、決して成功を返さない(黙って成功扱いにしない)。
    finalizerのPhase 3は例外を捕捉してNOTIFICATION_FAILEDとして記録し(ウォッチリスト登録は
    保持される)、通知だけが既存のretry_notification()の再試行機構に載る。

    ★ Phase 3の例外捕捉により、このままではLambda呼び出しが成功扱いになり欠落が不可視に
      なる(#117が防ぎたい障害の再現)。そのため「欠落のまま送信が試みられた」事実を保持し、
      `raise_if_send_attempted()`をhandlerの全処理完了後に呼んで送出する(登録・
      NOTIFICATION_FAILEDの記録は、その時点で完了している)。
    """

    def __init__(self, missing: LineCredentialsMissingError) -> None:
        self._missing = missing
        self.send_attempted = False

    def _fail(self) -> NoReturn:
        self.send_attempted = True
        raise LineCredentialsMissingError(str(self._missing))

    def push_message(self, text: str) -> None:
        self._fail()

    def reply_message(
        self, reply_token: str, text: str, quick_reply: list[QuickReplyButton] | None = None
    ) -> None:
        self._fail()

    def reply_messages(
        self,
        reply_token: str,
        texts: list[str],
        quick_reply: list[QuickReplyButton] | None = None,
    ) -> None:
        self._fail()

    def raise_if_send_attempted(self) -> None:
        if self.send_attempted:
            raise LineCredentialsMissingError(str(self._missing))


def _build_reconciler_line_client() -> LineClient:
    """認証情報があればLiveLineClient、無ければ送信時に必ず失敗するclientを返す。

    構築の失敗(`LineCredentialsMissingError`)だけを送信時の失敗へ変える。認証情報の欠落以外の
    例外は握りつぶさず、従来どおり伝播する。
    """
    try:
        return build_live_line_client_from_env()
    except LineCredentialsMissingError as exc:
        return _CredentialDeferredLineClient(exc)


def _build_notification_service(
    config: AppConfig, line_client: LineClient | None = None
) -> LineNotificationService:
    return LineNotificationService(
        line_client=line_client if line_client is not None else _build_reconciler_line_client(),
        notification_log_repository=NotificationLogRepository(),
        # LINE通知dedupの原子化(Issue #17): NORMAL実行の送信決定を原子的に
        # 一意化するclaimリポジトリ(VALIDATION/DRY_RUNでは使用されない)。
        notification_claim_repository=NotificationClaimRepository(),
        recommendation_repository=RecommendationRepository(),
        config=config,
    )


def _is_timed_out(batch_item: dict[str, Any], timeout_hours: int, now: dt.datetime) -> bool:
    started_at_raw = batch_item.get("started_at")
    if not started_at_raw:
        return False
    started_at = dt.datetime.fromisoformat(started_at_raw)
    return (now - started_at).total_seconds() / 3600 > timeout_hours


def _process_timeout_finalizing(
    batch_id: str,
    now: dt.datetime,
    max_rows_per_run: int,
    config: AppConfig,
) -> None:
    """17節ステップ1〜10。"""
    result = run_timeout_finalization_pass(batch_id, now, max_rows_per_run)
    if not set_timeout_finalize_completed_count(batch_id, result.terminal_count, now):
        # 他のReconciler実行/主体との競合でstatusが既に変わっていた(冪等スキップ)。
        return

    if result.terminal_count > result.total:
        logger.error(
            "watchlist reconciler: terminal_count exceeds total (data inconsistency) "
            "batch_id=%s terminal_count=%d total=%d",
            batch_id,
            result.terminal_count,
            result.total,
        )
        transition_timeout_finalizing_to_failed(
            batch_id, now, f"terminal_count({result.terminal_count}) > total({result.total})"
        )
        return

    if result.terminal_count < result.total:
        logger.info(
            "watchlist reconciler: timeout finalization in progress batch_id=%s "
            "terminal_count=%d total=%d newly_failed=%d",
            batch_id,
            result.terminal_count,
            result.total,
            result.newly_failed_count,
        )
        return

    # terminal_count == total: 14節「TIMED_OUT時は部分結果を自動登録・通知しない」。
    batch_item = get_watchlist_batch(batch_id) or {}
    started_at_raw = batch_item.get("started_at")
    started_at = dt.datetime.fromisoformat(started_at_raw) if started_at_raw else now
    metrics = compute_batch_metrics(result.all_records)
    completion_rate = (metrics["processed_count"] / result.total) if result.total else 0.0

    # Issue #56: maintenance batchもRUNNING救済に失敗すればここへ到達する。
    # ADD用のcandidate_universe.providerで記録すると、メンテナンス実行が
    # 「新規追加バッチのタイムアウト」として監査に残り意味が食い違う。
    # (TIMED_OUTは14節どおり部分結果の登録・通知を行わないため誤りは監査の
    #  意味論に限られるが、job_typeに追随させる。)
    universe_provider = (
        MAINTENANCE_UNIVERSE_PROVIDER
        if batch_item.get("job_type") == WatchlistJobType.WATCHLIST_MAINTENANCE.value
        else config.watchlist_screening.candidate_universe.provider
    )

    record_batch_audit(
        # Issue #286 (#70 F-B8): reconciler自身はScheduleで動くが、監査が
        # 表すのは**そのbatchの起動経路**であるためbatch行から復元する。
        execution_mode=resolve_batch_execution_mode(batch_item),
        universe_provider=universe_provider,
        screening_policies=[config.watchlist_screening.screening_policy],
        output_values={
            "execution_result": "TIMED_OUT",
            "started_at": started_at.isoformat(),
            "completed_at": now.isoformat(),
            "duration_seconds": (now - started_at).total_seconds(),
            "evaluation_completed_count": metrics["processed_count"],
            "evaluation_total_count": result.total,
            "completion_rate": completion_rate,
            **metrics,
        },
        now=now,
        batch_id=batch_id,
    )
    transition_timeout_finalizing_to_timed_out(batch_id, now)
    # 本番検証2026-08対応: TIMED_OUTは_finish_batch()/_maybe_commit_rotation()の
    # finalize経路を使わないため(モジュールdocstring参照)、rotation dispatch
    # leaseはここで明示的に解放する(未解放のままだとlease_expires_atの自然
    # 失効まで次のNEW_CANDIDATE_SCREENING dispatchがブロックされ続ける)。
    release_rotation_dispatch_lease(DEFAULT_ROTATION_ID, batch_id)
    logger.warning(
        "watchlist reconciler: batch timed out batch_id=%s completion_rate=%.1f%%",
        batch_id,
        completion_rate * 100,
    )


_COMPLETION_RECOVERY_FUNCTION_ENV = {
    BatchFamily.BUY_CANDIDATES: "BUY_CANDIDATES_FUNCTION_NAME",
    BatchFamily.HOLDINGS_WATCHLIST: "HOLDINGS_WATCHLIST_FUNCTION_NAME",
}


def _handle_completion_recovery_candidate(
    batch_item: dict[str, Any], now: dt.datetime
) -> bool | None:
    """buy/holdingsのfinalize recovery候補を処理する(Issue #57 Phase B2)。

    戻り値:
      None  … このバッチはbuy/holdings familyではない(=呼び出し側は
              既存のwatchlist経路をそのまま続行する)
      True  … finalize-only invokeを発行した
      False … buy/holdings familyだが今回は何もしなかった

    **marker不在は None を返す。** 既存のwatchlist batchには`batch_family`が
    無いため、marker不在を一律skipするとwatchlist recoveryを壊す。
    一方、**未知のfamily値はfail-close**(False)とし、既存経路へは流さない。

    reconcilerは**gateを取得しない**。`try_acquire_completion_finalize()`は
    invoke先のhandlerだけが実行する(invokeに失敗したときにgateを占有して
    しまわないため。取得回数も消費しない)。
    """
    raw_family = batch_item.get("batch_family")
    if raw_family is None:
        return None
    batch_id = batch_item["batch_id"]
    try:
        family = BatchFamily(raw_family)
    except ValueError:
        logger.error(
            "watchlist reconciler: unknown batch_family=%r batch_id=%s "
            "(fail-close: neither completion recovery nor watchlist path)",
            raw_family,
            batch_id,
        )
        return False

    record = get_completion_batch(batch_id)
    if record is None:
        logger.warning(
            "watchlist reconciler: completion batch record unavailable batch_id=%s", batch_id
        )
        return False
    if record.is_finalized:
        return False
    if not record.progress.is_complete:
        # 全銘柄の処理が終わっていない=通常進行中。recoveryの対象ではない。
        return False
    if record.execution_context is None or record.execution_context.mode != ExecutionMode.NORMAL:
        # VALIDATIONおよびcontext不明はfail-close(自動re-driveしない)。
        return False
    if record.attempts_exhausted:
        logger.error(
            "watchlist reconciler: finalize recovery exhausted batch_id=%s "
            "batch_family=%s attempt_count=%d reason=FINALIZE_RETRY_EXHAUSTED",
            batch_id,
            family.value,
            record.attempt_count,
        )
        return False

    function_name = os.environ.get(_COMPLETION_RECOVERY_FUNCTION_ENV[family], "")
    if not function_name:
        logger.error(
            "watchlist reconciler: completion recovery target function not configured "
            "batch_id=%s batch_family=%s",
            batch_id,
            family.value,
        )
        return False
    try:
        dispatch_async(function_name, build_finalize_only_payload(record))
    except Exception:  # noqa: BLE001 - 1バッチのinvoke失敗で他バッチの処理を止めない
        # invoke自体の失敗ではgateを取得していないため、attempt_countも増えない。
        # 次回の毎時実行で再試行できる(invoke試行回数とgate取得回数は別物)。
        logger.exception(
            "watchlist reconciler: finalize recovery invoke failed batch_id=%s batch_family=%s",
            batch_id,
            family.value,
        )
        return False
    logger.info(
        "watchlist reconciler: finalize recovery invoked batch_id=%s batch_family=%s "
        "attempt_count=%d",
        batch_id,
        family.value,
        record.attempt_count,
    )
    return True


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    # Issue #286 (#70 F-B4): watchlist系は execution_mode を**受け付けない**。
    reject_execution_mode(event, handler_name="watchlist batch reconciler")
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    wc = config.watchlist_screening
    providers: ProviderBundle = build_cached_provider_bundle(
        build_real_provider_bundle(now, config), config, now
    )
    # Issue #117: 認証情報の欠落は構築の失敗にせず、送信時の失敗として扱う。
    # (_CredentialDeferredLineClient)
    line_client = _build_reconciler_line_client()
    notification_service = _build_notification_service(config, line_client)

    candidates = list_watchlist_batches_by_status(_RECONCILE_TARGET_STATUSES)

    dispatch_failed = 0
    rescued = 0
    finalizing_marked_stuck = 0
    finalize_retried = 0
    finalize_retry_exhausted = 0
    notification_retried = 0
    notification_retry_exhausted = 0
    completion_recovery_invoked = 0
    completion_recovery_skipped = 0
    to_process_timeout: list[str] = []

    for batch_item in candidates:
        batch_id = batch_item["batch_id"]
        status = batch_item["status"]

        # Issue #57 Phase B2: buy/holdingsのfinalize recovery。
        # `list_watchlist_batches_by_status()`はstatusだけで絞るfull scanのため、
        # buy/holdingsの項目(status=RUNNING固定)もここへ届く。従来はstarted_at
        # 不在によりタイムアウト判定が成立せず無害にskipされていたが、B2からは
        # **family markerで積極識別して専用分岐へ隔離**する。
        # **watchlistの既存status分岐へ流してはならない**(#56と同型の
        # 「種別を確認せず既定経路へ流す」誤終端を再生産しないため)。
        family_outcome = _handle_completion_recovery_candidate(batch_item, now)
        if family_outcome is not None:
            if family_outcome:
                completion_recovery_invoked += 1
            else:
                completion_recovery_skipped += 1
            continue

        if status == WatchlistBatchStatus.DISPATCHING.value:
            timed_out = _is_timed_out(batch_item, wc.batch_processing_timeout_hours, now)
            if timed_out and mark_dispatch_failed(batch_id, now, reason="dispatch_timeout"):
                dispatch_failed += 1
                # 本番検証2026-08対応: Dispatcher Lambdaが候補選択・SQS投入の
                # 途中で異常終了しDISPATCHINGのまま放置された場合、rotation
                # dispatch leaseはfinalize経路(_maybe_commit_rotation)に到達
                # しないため明示的に解放する。
                release_rotation_dispatch_lease(DEFAULT_ROTATION_ID, batch_id)
                logger.warning("watchlist reconciler: DISPATCH_FAILED batch_id=%s", batch_id)
            continue

        if status == WatchlistBatchStatus.RUNNING.value:
            # Issue #56: 全件完了しているのにfinalize呼び出し自体が失敗した
            # ケースの救済。job_typeを見ずに常にADD用finalizerを呼ぶと、
            # WATCHLIST_MAINTENANCEバッチがメンテナンス業務(自動削除・
            # 連続非該当カウント更新・監視スコア更新)を一切実行しないまま
            # COMPLETED(終端)になり、二度と実行されない。
            try:
                job_type = resolve_watchlist_job_type(
                    batch_item.get("job_type"),
                    default=WatchlistJobType.NEW_CANDIDATE_SCREENING,
                )
            except UnknownWatchlistJobTypeError:
                # 未知値は暗黙にどちらかへ倒さずfail-closeする(救済しない)。
                # タイムアウト経路は後続のReconciler実行が引き続き担う。
                logger.error(
                    "watchlist reconciler: unknown job_type=%r batch_id=%s (rescue skipped)",
                    batch_item.get("job_type"),
                    batch_id,
                )
                continue
            rescued_now = (
                maybe_finalize_maintenance(batch_id, now, config)
                if job_type is WatchlistJobType.WATCHLIST_MAINTENANCE
                else maybe_finalize(batch_id, now, providers, config, notification_service)
            )
            if rescued_now:
                rescued += 1
                continue
            if not _is_timed_out(batch_item, wc.batch_processing_timeout_hours, now):
                continue
            if try_acquire_timeout_finalization(batch_id):
                to_process_timeout.append(batch_id)
            continue

        if status in _FINALIZE_IN_PROGRESS_STATUSES:
            # 通常のRUNNING→FINALIZE_PREPARING遷移後、finalize処理中の4段階
            # (FINALIZE_PREPARING/WATCHLIST_WRITE_COMPLETED/NOTIFICATION_PENDING/
            # NOTIFICATION_SENT)のいずれかでLambdaが異常終了して二度と進まなく
            # なったケース(運用ハードニング5節・第2弾2節)。閾値未満なら正常に
            # 進行中の可能性があるため何もしない。
            if mark_finalizing_stuck_as_failed(
                batch_id, now, wc.finalizing_stuck_threshold_minutes
            ):
                finalizing_marked_stuck += 1
                logger.warning(
                    "watchlist reconciler: finalize stuck (status=%s), marked "
                    "FINALIZE_FAILED batch_id=%s",
                    status,
                    batch_id,
                )
            continue

        if status == WatchlistBatchStatus.FINALIZE_FAILED.value:
            attempt_count = int(batch_item.get("finalize_attempt_count", 0) or 0)
            if attempt_count >= wc.max_finalize_retry_attempts:
                finalize_retry_exhausted += 1
                logger.warning(
                    "watchlist reconciler: FINALIZE_FAILED retry attempts exhausted "
                    "batch_id=%s attempt_count=%d (manual intervention required, see CLI)",
                    batch_id,
                    attempt_count,
                )
                continue
            try:
                if retry_finalize(batch_id, now, providers, config, notification_service):
                    finalize_retried += 1
            except Exception:  # noqa: BLE001 - 1バッチの想定外エラーで他バッチの処理を止めない
                logger.exception(
                    "watchlist reconciler: retry_finalize unexpected error batch_id=%s", batch_id
                )
            continue

        if status == WatchlistBatchStatus.NOTIFICATION_FAILED.value:
            # 運用ハードニング第3弾1節: LINE送信のみが例外で失敗した状態。
            # finalize全体(ウォッチリスト追加結果)は既に確定・保持されているため、
            # 通知のみを再試行する(finalize_attempt_countとは独立した
            # notification_failure_countで上限を判定する)。
            notification_attempt_count = int(batch_item.get("notification_failure_count", 0) or 0)
            if notification_attempt_count >= wc.max_notification_retry_attempts:
                notification_retry_exhausted += 1
                logger.warning(
                    "watchlist reconciler: NOTIFICATION_FAILED retry attempts exhausted "
                    "batch_id=%s notification_failure_count=%d "
                    "(should already be COMPLETED_WITH_NOTIFICATION_FAILURE; manual "
                    "intervention required if not)",
                    batch_id,
                    notification_attempt_count,
                )
                continue
            try:
                if retry_notification(batch_id, now, providers, config, notification_service):
                    notification_retried += 1
            except Exception:  # noqa: BLE001 - 1バッチの想定外エラーで他バッチの処理を止めない
                logger.exception(
                    "watchlist reconciler: retry_notification unexpected error batch_id=%s",
                    batch_id,
                )
            continue

        if status == WatchlistBatchStatus.TIMEOUT_FINALIZE_FAILED.value:
            if try_acquire_timeout_finalization(batch_id):
                to_process_timeout.append(batch_id)
            continue

        # status == TIMEOUT_FINALIZING(前回Reconciler実行からの継続)。
        to_process_timeout.append(batch_id)

    timeout_processed = 0
    for batch_id in to_process_timeout:
        try:
            _process_timeout_finalizing(batch_id, now, wc.max_timeout_finalize_rows_per_run, config)
            timeout_processed += 1
        except Exception as exc:  # noqa: BLE001 - 1バッチの想定外エラーで他バッチの処理を止めない
            logger.exception("watchlist reconciler: unexpected error batch_id=%s", batch_id)
            transition_timeout_finalizing_to_failed(batch_id, now, str(exc))

    # 平日毎日起動化(2026-08)対応・Medium修正(2026-08再レビュー): WATCHLIST_
    # MAINTENANCE後続起動のinvoke失敗等でmaintenance_trigger_status=TRIGGERING
    # のままlease失効した親バッチを再試行する(maybe_trigger_maintenanceの
    # ConditionExpressionがlease失効時のみ再取得を許すため、初回呼び出しと
    # 同じ関数を再度呼ぶだけでよい)。対象はCOMPLETED等の終端状態のバッチのため、
    # _RECONCILE_TARGET_STATUSESとは別のスキャンで拾う。
    #
    # maintenance_trigger_retriedは、戻り値(MaintenanceTriggerOutcome)を見て
    # 「実際にLambda invoke()を試行した(=TRIGGERED/INVOKE_FAILED)」ケースの
    # みを数える。CONFIGURATION_ERROR(lease再取得には成功したが、環境変数
    # 未設定によりinvoke()呼び出し自体に到達しない設定不備)はinvoke未試行
    # のため、retriedには含めずmaintenance_trigger_retry_configuration_error
    # へ個別に計上する(再々レビュー修正: 従来はCONFIGURATION_ERRORもretried
    # に含めていたが、「実際にinvokeを試行した件数」という定義と矛盾していた)。
    # 他主体が先にleaseを再取得済みだった場合(SKIPPED_LEASE_UNAVAILABLE)や
    # そもそも対象外だった場合(NOT_APPLICABLE、ABORTED等)は「再試行を試みて
    # すらいない」ため、誤解を招かないようretriedへは加算しない
    # (maintenance_trigger_retry_skippedへ計上する)。新規の永続DynamoDB
    # カウンタは追加せず、このReconciler実行1回分のin-memory集計のみで、
    # ログ・戻り値(運用監視・Issue #8観測用)に残す。
    maintenance_trigger_retried = 0
    maintenance_trigger_retry_failed = 0
    maintenance_trigger_retry_skipped = 0
    maintenance_trigger_retry_configuration_error = 0
    for batch_item in list_stale_maintenance_triggers(now):
        batch_id = batch_item["batch_id"]
        try:
            final_status = WatchlistBatchStatus(batch_item.get("status", ""))
            outcome = maybe_trigger_maintenance(batch_id, batch_item, now, config, final_status)
            if outcome in _MAINTENANCE_RETRY_ATTEMPTED_OUTCOMES:
                maintenance_trigger_retried += 1
                if outcome is MaintenanceTriggerOutcome.INVOKE_FAILED:
                    maintenance_trigger_retry_failed += 1
            elif outcome is MaintenanceTriggerOutcome.CONFIGURATION_ERROR:
                maintenance_trigger_retry_configuration_error += 1
            else:
                maintenance_trigger_retry_skipped += 1
        except Exception:  # noqa: BLE001 - 1バッチの想定外エラーで他バッチの処理を止めない
            logger.exception(
                "watchlist reconciler: maintenance trigger retry unexpected error batch_id=%s",
                batch_id,
            )

    logger.info(
        "watchlist reconciler completed: candidates=%d dispatch_failed=%d rescued=%d "
        "finalizing_marked_stuck=%d finalize_retried=%d finalize_retry_exhausted=%d "
        "notification_retried=%d notification_retry_exhausted=%d timeout_processed=%d "
        "maintenance_trigger_retried=%d maintenance_trigger_retry_failed=%d "
        "maintenance_trigger_retry_skipped=%d maintenance_trigger_retry_configuration_error=%d "
        "completion_recovery_invoked=%d completion_recovery_skipped=%d",
        len(candidates),
        dispatch_failed,
        rescued,
        finalizing_marked_stuck,
        finalize_retried,
        finalize_retry_exhausted,
        notification_retried,
        notification_retry_exhausted,
        timeout_processed,
        maintenance_trigger_retried,
        maintenance_trigger_retry_failed,
        maintenance_trigger_retry_skipped,
        maintenance_trigger_retry_configuration_error,
        completion_recovery_invoked,
        completion_recovery_skipped,
    )
    # Issue #506(O-1): 既存の回復処理とは独立した検知(相乗り)。既存処理の成否には
    # 依存させない(既存の回復処理が例外を出しても、ここへは到達しない現状の挙動を
    # 変えない。検知自体の失敗は後述のLINE欠落顕在化より前に出す: 検知の例外は
    # ここで停止させ、既存の回復処理の完了報告〔返り値〕を汚染しない)。
    incident_detection = _detect_and_notify_watchlist_incidents(now, config)

    # Issue #117: 通知と無関係な回復処理・ウォッチリスト登録・NOTIFICATION_FAILEDの記録を全て
    # 終えた後に、認証情報の欠落を顕在化させる(Lambda呼び出しをErrorsとして失敗させる)。
    # Phase 3が例外を捕捉するため、これが無いと欠落が不可視になる。Schedulerの再試行は各処理が
    # 冪等(repository_results・claim補償delete・NotificationLog未保存)で無害。
    if isinstance(line_client, _CredentialDeferredLineClient):
        line_client.raise_if_send_attempted()
    return {
        "candidates": len(candidates),
        "watchlist_missed_schedule_notified": incident_detection["missed_schedule_notified"],
        "watchlist_universe_load_failure_streak_notified": (
            incident_detection["universe_load_failure_streak_notified"]
        ),
        "watchlist_queue_backlog_notified": incident_detection["queue_backlog_notified"],
        "watchlist_deletion_zero_streak_notified": (
            incident_detection["deletion_zero_streak_notified"]
        ),
        "dispatch_failed": dispatch_failed,
        "rescued": rescued,
        "finalizing_marked_stuck": finalizing_marked_stuck,
        "finalize_retried": finalize_retried,
        "finalize_retry_exhausted": finalize_retry_exhausted,
        "notification_retried": notification_retried,
        "notification_retry_exhausted": notification_retry_exhausted,
        "timeout_processed": timeout_processed,
        "maintenance_trigger_retried": maintenance_trigger_retried,
        "completion_recovery_invoked": completion_recovery_invoked,
        "completion_recovery_skipped": completion_recovery_skipped,
        "maintenance_trigger_retry_failed": maintenance_trigger_retry_failed,
        "maintenance_trigger_retry_skipped": maintenance_trigger_retry_skipped,
        "maintenance_trigger_retry_configuration_error": (
            maintenance_trigger_retry_configuration_error
        ),
    }
