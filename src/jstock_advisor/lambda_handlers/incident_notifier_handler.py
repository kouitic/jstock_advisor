"""異常通知の中継 Lambda(Issue #132 X-4〔#503〕。段階1)。

CloudWatch Alarm → SNS Topic(IncidentNotificationTopic)→ 本 handler、という経路の終端。
1件の SNS メッセージ(= 1回の Alarm 状態遷移)から:

    1. Alarm の SNS payload から、fingerprint 計算に使う**安定した値**を取り出す
       (StateReason の自由文はそのまま使わない。baseline: 「Alarm payload の扱い」)
    2. #502 compute_fingerprint() で fingerprint を計算する
    3. incident_state_tracker.try_claim() で原子的に claim する(dedup・stale takeover)
    4. claim できたときだけ、#501 build_incident_message() で本文を組み立て、LINE push する
    5. 成功したら mark_sent、失敗したら release_claim してから Lambda を失敗させる
       (SNS/Lambda の retry に任せる。baseline の LINE 失敗時契約)

**この handler 自身は fingerprint・claim_token 等の内部値を CloudWatch Logs へ出す**
(識別子ではなく運用上のハッシュ値・状態遷移名であり、H-30 の allowlist が禁じる
「識別子・銘柄・所有者」ではない)。
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

_CLAIMED_OUTCOMES = frozenset(
    {
        tracker.IncidentClaimOutcome.CLAIMED_NEW,
        tracker.IncidentClaimOutcome.CLAIMED_AFTER_DEDUP_WINDOW,
        tracker.IncidentClaimOutcome.CLAIMED_STALE_TAKEOVER,
    }
)


def _extract_function_name(alarm_message: dict[str, Any]) -> str:
    """Alarm の SNS payload から、対象 Lambda 関数名(Dimensions の FunctionName)を取り出す。

    見つからない場合は "unknown" とする(handler 自体を失敗させない。#503 は Errors/Duration
    の2 alarm のみが対象で、いずれも FunctionName dimension を持つ)。
    """
    trigger = alarm_message.get("Trigger") or {}
    for dimension in trigger.get("Dimensions") or []:
        if dimension.get("name") == "FunctionName":
            value = dimension.get("value")
            if isinstance(value, str) and value:
                return value
    return "unknown"


def _extract_metric_name(alarm_message: dict[str, Any]) -> str:
    trigger = alarm_message.get("Trigger") or {}
    metric_name = trigger.get("MetricName")
    return metric_name if isinstance(metric_name, str) and metric_name else "unknown"


def _extract_occurred_at(alarm_message: dict[str, Any], now: dt.datetime) -> dt.datetime:
    """Alarm の StateChangeTime(ISO8601、通常UTC)を使う。解釈できなければ now にfallbackする。"""
    raw = alarm_message.get("StateChangeTime")
    if not isinstance(raw, str):
        return now
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return now
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def _build_fingerprint_input(alarm_message: dict[str, Any]) -> IncidentFingerprintInput:
    """Alarm payload から、fingerprint 計算用の安定した5要素を組み立てる。

    baseline(#503 issuecomment-5796588796)「Alarm payload の扱い」のとおり、
    StateReason の自由文(タイムスタンプ・実測値を含む)はそのまま使わない。
    AlarmName(deploy 時に固定される安定した文字列)を error_message とし、
    MetricName(Errors / Duration)を failure_type とする。
    """
    return IncidentFingerprintInput(
        environment=_ENVIRONMENT,
        job_name=_extract_function_name(alarm_message),
        failure_stage=_ALARM_METRIC_NAMESPACE_STAGE,
        failure_type=_extract_metric_name(alarm_message),
        error_type=_ALARM_ERROR_TYPE,
        error_message=alarm_message.get("AlarmName") or "unknown",
    )


def _process_alarm_message(
    alarm_message: dict[str, Any], config: AppConfig, now: dt.datetime
) -> None:
    fingerprint = compute_fingerprint(_build_fingerprint_input(alarm_message))
    dedup_window = dt.timedelta(minutes=config.incident_notification.dedup_window_minutes)
    claim_stale = dt.timedelta(minutes=config.incident_notification.claim_stale_minutes)

    outcome, claim_token = tracker.try_claim(fingerprint, now, dedup_window, claim_stale)
    logger.info(
        "incident_notifier claim fingerprint=%s outcome=%s",
        fingerprint,
        outcome.value,
    )
    if outcome not in _CLAIMED_OUTCOMES or claim_token is None:
        return  # 抑止(重複 or 他実行が処理中)。LINEは送らない。

    state = tracker.get_incident_state(fingerprint)
    occurrence_count = int(state["occurrence_count"]) if state else 1
    occurred_at = _extract_occurred_at(alarm_message, now)
    require_timezone_aware(occurred_at)
    notice = IncidentNotice(
        job=resolve_incident_job(_extract_function_name(alarm_message)),
        occurred_at=occurred_at,
        failure_count=occurrence_count,
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
        alarm_message = json.loads(sns["Message"])
        _process_alarm_message(alarm_message, config, now)
    logger.info("incident_notifier_handler done: records=%d", len(records))
    return {"processed": len(records)}
