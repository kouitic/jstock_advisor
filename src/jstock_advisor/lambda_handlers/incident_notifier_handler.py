"""異常通知の中継 Lambda(Issue #132 X-4〔#503〕。段階1。Issue #506〔O-1〕でInternal
structured incident payloadの共通処理を追加)。

CloudWatch Alarm → SNS Topic(IncidentNotificationTopic)→ 本 handler、という経路と、
reconciler等が発行するInternal structured incident payload → 同じSNS Topic → 本 handler、
という経路の両方の終端(USER決定。#506 issuecomment-5805278274。通知経路のOption 1
採用: 発生源に関わらず単一のIncidentNotificationTopicを通り、以降は完全共通処理とする)。

1件の SNS メッセージから:

    0. payloadの形状(CloudWatch Alarm由来かInternal由来か)を判別し、
       `domain.notification.incident_signal.IncidentSignal`へ正規化する(本Issueで追加)
    1. IncidentSignalから、#502 fingerprint計算に使う**安定した値**を取り出す
       (StateReason等の自由文はそのまま使わない。baseline: 「Alarm payload の扱い」)
    2. #502 compute_fingerprint() で fingerprint を計算する
    3. incident_state_tracker.try_claim() で原子的に claim する(dedup・stale takeover)
    4. claim できたときだけ、#501 build_incident_message() で本文を組み立て、LINE push する
    5. 成功したら mark_sent、失敗したら release_claim してから Lambda を失敗させる
       (SNS/Lambda の retry に任せる。baseline の LINE 失敗時契約)

**この handler 自身は fingerprint・claim_token 等の内部値を CloudWatch Logs へ出す**
(識別子ではなく運用上のハッシュ値・状態遷移名であり、H-30 の allowlist が禁じる
「識別子・銘柄・所有者」ではない)。

Internal payloadのallowlist(#506 USER決定): source / job_name / failure_stage /
failure_type / reason_code / occurred_at / failure_count / consecutive_days /
is_ongoing のみ。stock_code / owner / holding_id / stack trace / 生exception message /
AWS account ID / ARN / request ID は禁止(#501/#503のH-30契約を維持する)。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.jst import require_timezone_aware
from jstock_advisor.domain.notification.incident_fingerprint import (
    IncidentFingerprintInput,
    compute_fingerprint,
)
from jstock_advisor.domain.notification.incident_message import (
    IncidentNotice,
    build_incident_message,
    resolve_incident_job,
)
from jstock_advisor.domain.notification.incident_signal import IncidentSignal
from jstock_advisor.infrastructure.aws import incident_state_tracker as tracker
from jstock_advisor.infrastructure.line.client import build_live_line_client_from_env

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 本システムは単一の Production スタックのみで運用しており、staging 等の別環境を
# 持たない(2026-09時点)。将来 environment を分ける場合は、ここを実行環境から
# 解決するよう変える(fingerprint の environment 要素が初めて意味を持つ)。
_ENVIRONMENT = "production"

_ALARM_METRIC_NAMESPACE_STAGE = "cloudwatch_alarm"
_ALARM_ERROR_TYPE = "CloudWatchAlarm"

# Internal payload(reconciler等)の発生源識別子(source)の既知値。
# fingerprintの入力(job_name)としてそのまま使うのはこれらの値ではなく、
# payload自身が持つjob_nameフィールドである(sourceは「誰が検知したか」、
# job_nameは「どのjobの異常か」で意味が異なる)。
_INTERNAL_SOURCE_WATCHLIST_RECONCILER = "watchlist_reconciler"

_CLAIMED_OUTCOMES = frozenset(
    {
        tracker.IncidentClaimOutcome.CLAIMED_NEW,
        tracker.IncidentClaimOutcome.CLAIMED_AFTER_DEDUP_WINDOW,
        tracker.IncidentClaimOutcome.CLAIMED_STALE_TAKEOVER,
    }
)


def _is_internal_payload(message: dict[str, Any]) -> bool:
    """CloudWatch AlarmのSNS payloadと区別する。

    CloudWatch AlarmのSNS payloadは必ず`AlarmName`と`Trigger`を持つ(AWSの固定形式)。
    Internal payload(reconciler等)はこの2つを持たず、代わりに`source`を持つ
    (本system内部の約束。#506で新設)。両方が欠けている場合はAlarm由来として扱う
    (fail-closedではなく既存動作を優先する。既存のAlarm処理はこの2つが無くても
    "unknown"へ落ちる安全側の実装のため、誤判定しても致命的にならない)。
    """
    return "source" in message and "AlarmName" not in message and "Trigger" not in message


_ALARM_TARGET_DIMENSION_NAMES = ("FunctionName", "QueueName")


def _extract_alarm_target(alarm_message: dict[str, Any]) -> str:
    """Alarm の SNS payload から、対象(Lambda 関数名 または SQS キュー名)の Dimension 値を
    取り出す。

    FunctionName dimension を優先し、無ければ QueueName dimension を見る(Issue #349:
    DLQ 滞留の Alarm〔SQS ベース〕にも対応するため。#503 時点の Errors/Duration の2 alarm は
    いずれも FunctionName dimension のみを持ち、既存の解決結果は変わらない)。
    どちらも見つからない場合は "unknown" とする(handler 自体を失敗させない)。
    """
    trigger = alarm_message.get("Trigger") or {}
    dimensions = trigger.get("Dimensions") or []
    for wanted in _ALARM_TARGET_DIMENSION_NAMES:
        for dimension in dimensions:
            if dimension.get("name") == wanted:
                value = dimension.get("value")
                if isinstance(value, str) and value:
                    return value
    return "unknown"


def _extract_metric_name(alarm_message: dict[str, Any]) -> str:
    trigger = alarm_message.get("Trigger") or {}
    metric_name = trigger.get("MetricName")
    return metric_name if isinstance(metric_name, str) and metric_name else "unknown"


def _extract_occurred_at(raw: object, now: dt.datetime) -> dt.datetime:
    """ISO8601文字列(通常UTC)を解釈する。解釈できなければ now にfallbackする。"""
    if not isinstance(raw, str):
        return now
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return now
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def _normalize_alarm_message(alarm_message: dict[str, Any], now: dt.datetime) -> IncidentSignal:
    """CloudWatch AlarmのSNS payloadをIncidentSignalへ正規化する。

    baseline(#503 issuecomment-5796588796)「Alarm payload の扱い」のとおり、
    StateReason の自由文(タイムスタンプ・実測値を含む)はそのまま使わない。
    AlarmName(deploy 時に固定される安定した文字列)を error_message とし、
    MetricName(Errors / Duration)を failure_type とする。
    failure_count / consecutive_days / is_ongoingはAlarm payload自体には無い情報のため
    Noneのままにする(failure_countは、claim後にIncidentStateTrackerのoccurrence_countから
    別途補う。#503から変更しない既存の挙動)。
    """
    return IncidentSignal(
        source=_ALARM_METRIC_NAMESPACE_STAGE,
        job_name=_extract_alarm_target(alarm_message),
        failure_stage=_ALARM_METRIC_NAMESPACE_STAGE,
        failure_type=_extract_metric_name(alarm_message),
        error_type=_ALARM_ERROR_TYPE,
        error_message=alarm_message.get("AlarmName") or "unknown",
        occurred_at=_extract_occurred_at(alarm_message.get("StateChangeTime"), now),
    )


def _require_allowlisted_str(message: dict[str, Any], key: str) -> str:
    value = message.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"internal incident payload missing required field: {key}")
    return value


def _optional_int(message: dict[str, Any], key: str) -> int | None:
    value = message.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"internal incident payload field {key} must be int or null")
    return value


def _optional_bool(message: dict[str, Any], key: str) -> bool | None:
    value = message.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise TypeError(f"internal incident payload field {key} must be bool or null")
    return value


def _normalize_internal_message(message: dict[str, Any], now: dt.datetime) -> IncidentSignal:
    """Internal structured incident payload(reconciler等)をIncidentSignalへ正規化する。

    許可するキーは#506 USER決定のallowlistのみ(source / job_name / failure_stage /
    failure_type / reason_code / occurred_at / failure_count / consecutive_days /
    is_ongoing)。reason_codeはfingerprint計算のerror_type(識別性の高い安定値)として
    使う(#502のnormalize_error_signature()が数字列を正規化してしまうため、
    failure_count/consecutive_daysのような可変値はfingerprintの入力に含めない。
    reason_codeは固定の識別子文字列であり数字を含まない設計とする)。
    """
    return IncidentSignal(
        source=_require_allowlisted_str(message, "source"),
        job_name=_require_allowlisted_str(message, "job_name"),
        failure_stage=_require_allowlisted_str(message, "failure_stage"),
        failure_type=_require_allowlisted_str(message, "failure_type"),
        error_type=_require_allowlisted_str(message, "reason_code"),
        error_message=_require_allowlisted_str(message, "reason_code"),
        occurred_at=_extract_occurred_at(message.get("occurred_at"), now),
        failure_count=_optional_int(message, "failure_count"),
        consecutive_days=_optional_int(message, "consecutive_days"),
        is_ongoing=_optional_bool(message, "is_ongoing"),
    )


def _normalize(message: dict[str, Any], now: dt.datetime) -> IncidentSignal:
    if _is_internal_payload(message):
        return _normalize_internal_message(message, now)
    return _normalize_alarm_message(message, now)


def _build_fingerprint_input(signal: IncidentSignal) -> IncidentFingerprintInput:
    return IncidentFingerprintInput(
        environment=_ENVIRONMENT,
        job_name=signal.job_name,
        failure_stage=signal.failure_stage,
        failure_type=signal.failure_type,
        error_type=signal.error_type,
        error_message=signal.error_message,
    )


def _process_signal(signal: IncidentSignal, config: AppConfig, now: dt.datetime) -> None:
    fingerprint = compute_fingerprint(_build_fingerprint_input(signal))
    dedup_window = dt.timedelta(minutes=config.incident_notification.dedup_window_minutes)
    claim_stale = dt.timedelta(minutes=config.incident_notification.claim_stale_minutes)

    outcome, claim_token = tracker.try_claim(fingerprint, now, dedup_window, claim_stale)
    logger.info(
        "incident_notifier claim source=%s fingerprint=%s outcome=%s",
        signal.source,
        fingerprint,
        outcome.value,
    )
    if outcome not in _CLAIMED_OUTCOMES or claim_token is None:
        return  # 抑止(重複 or 他実行が処理中)。LINEは送らない。

    state = tracker.get_incident_state(fingerprint)
    occurrence_count = int(state["occurrence_count"]) if state else 1
    occurred_at = signal.occurred_at
    require_timezone_aware(occurred_at)
    # Internal payloadがfailure_countを明示している場合はそれを使う(reconciler等が
    # 業務上の意味〔連続営業日数等〕を持つ値として計算済みのため)。無ければ、Alarm経路の
    # 既存挙動どおりIncidentStateTrackerのoccurrence_countを使う。
    failure_count = signal.failure_count if signal.failure_count is not None else occurrence_count
    notice = IncidentNotice(
        job=resolve_incident_job(signal.job_name),
        occurred_at=occurred_at,
        failure_count=failure_count,
        consecutive_days=signal.consecutive_days,
        is_ongoing=signal.is_ongoing,
    )
    text = build_incident_message(notice)

    is_new = outcome is tracker.IncidentClaimOutcome.CLAIMED_NEW
    try:
        line_client = build_live_line_client_from_env()
        line_client.push_message(text)
    except Exception:
        # baseline: LINE push失敗 → claimをCASで解除 → Lambdaを失敗させてSNS/Lambda
        # retryに任せる(通知欠落を避けることを、稀な二重通知より優先する)。
        tracker.release_claim(fingerprint, claim_token, is_new=is_new)
        logger.warning("incident_notifier line push failed fingerprint=%s", fingerprint)
        raise

    tracker.mark_sent(fingerprint, claim_token, now)
    logger.info("incident_notifier line push done fingerprint=%s", fingerprint)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    records = event.get("Records") or []
    for record in records:
        sns = record.get("Sns") or {}
        message = json.loads(sns["Message"])
        signal = _normalize(message, now)
        _process_signal(signal, config, now)
    logger.info("incident_notifier_handler done: records=%d", len(records))
    return {"processed": len(records)}
