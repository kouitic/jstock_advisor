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

Issue #508(#132 X-9): LINE送信後(成功・失敗いずれの場合も)、GitHub Issue自動起票
(`services/incident_github_issue_service.py`)を試行する。GitHub側の処理は独立した
try/exceptで例外を完全に握りつぶし、本handlerの成否・LINE通知経路には一切影響しない
(★最重要要件)。config.incident_notification.issue_creation_enabled=false(既定)の
間はGitHub API・Secrets Manager呼び出しを一切行わない。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.jst import require_timezone_aware
from jstock_advisor.domain.notification.incident_fingerprint import (
    IncidentFingerprintInput,
    compute_fingerprint,
)
from jstock_advisor.domain.notification.incident_github_issue_message import (
    IncidentIssueNotice,
    resolve_incident_failure_stage,
)
from jstock_advisor.domain.notification.incident_message import (
    IncidentNotice,
    build_incident_message,
    resolve_incident_job,
)
from jstock_advisor.domain.notification.incident_signal import IncidentSignal
from jstock_advisor.infrastructure.aws import incident_state_tracker as tracker
from jstock_advisor.infrastructure.line.client import build_live_line_client_from_env
from jstock_advisor.services import incident_github_issue_service

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


def _split_github_repository() -> tuple[str | None, str | None]:
    """GITHUB_REPOSITORY環境変数("owner/repo"形式)からowner/repoを取り出す
    (`weekly_review_handler.py`の同名関数と同じ契約。infra配線がまだ無い間は
    未設定のため両方Noneを返す=正常にnot configured扱いとなる)。
    """
    value = os.environ.get("GITHUB_REPOSITORY")
    if not value or "/" not in value:
        return None, None
    owner, _, repo = value.partition("/")
    return owner, repo


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

    line_failure: Exception | None = None
    github_safe_to_attempt = True
    if outcome in _CLAIMED_OUTCOMES and claim_token is not None:
        line_failure = _send_line(fingerprint, claim_token, outcome, signal, now)
        if line_failure is not None and outcome is tracker.IncidentClaimOutcome.CLAIMED_NEW:
            # ★ release_claim(is_new=True)はfingerprint行そのものをDeleteItemする
            # (#503既存契約。まだ何の記録も無い「初出」の履歴なので消しても失う
            # ものが無い、という前提)。#508でGitHub側の記録先をこの行に同居させた
            # ため、この場合にGitHub側を書き込むと削除後の行を部分的に再生成して
            # しまい、status欠落のままfingerprintが「存在する」状態になって
            # 以降のLINE再claim(_put_new)が永久に失敗する重大な回帰になる
            # (テストで実測・検知済み)。このケースに限りGitHub側は今回試行せず、
            # SNS/Lambda retryによる次のCLAIMED_NEWへ委ねる。
            github_safe_to_attempt = False

    # Issue #508: GitHub Issue作成はLINEの成否・claim outcomeに関わらず試行する
    # (★最重要要件。GitHub側の失敗はLINE経路に一切影響せず、LINE失敗時もGitHub側の
    # 記録機会を失わない)。ただし上記のCLAIMED_NEW削除競合を避けるため、fingerprint行が
    # 削除された可能性がある場合は例外的に今回スキップする。fingerprintごとの
    # 重複防止はincident_github_issue_service側の独立したclaim/statusが担う。
    #
    # ★ _attempt_github_issue()自体(IncidentIssueNotice構築等)はGitHub APIを
    # 呼ぶ前の段階であり、`incident_github_issue_service.process_incident_issue()`
    # 内部のtry/exceptより外側にある。このtry/exceptが無いと、GitHub側の
    # セットアップコードの不具合がLINE成功後の応答まで壊してしまい、最重要要件
    # (LINE側はGitHub側の失敗の影響を一切受けない)に違反する。
    if github_safe_to_attempt:
        try:
            _attempt_github_issue(signal, fingerprint, config, now)
        except Exception:
            logger.exception(
                "incident_notifier github issue attempt failed unexpectedly fingerprint=%s",
                fingerprint,
            )

    if line_failure is not None:
        # baseline: LINE push失敗 → claimは_send_line内でCAS解除済み → Lambdaを
        # 失敗させてSNS/Lambda retryに任せる(通知欠落を避けることを、稀な二重通知
        # より優先する)。GitHub側の試行を終えてから元の例外を再raiseする。
        raise line_failure


def _send_line(
    fingerprint: str,
    claim_token: str,
    outcome: tracker.IncidentClaimOutcome,
    signal: IncidentSignal,
    now: dt.datetime,
) -> Exception | None:
    """LINE送信を行う。成功時はNoneを返す。失敗時はclaimを解放し、例外をraiseせず
    呼び出し元へ返す(GitHub側の試行を先に終えてから、呼び出し元がまとめて
    re-raiseできるようにするため)。
    """
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
    except Exception as exc:
        tracker.release_claim(fingerprint, claim_token, is_new=is_new)
        logger.warning("incident_notifier line push failed fingerprint=%s", fingerprint)
        return exc

    tracker.mark_sent(fingerprint, claim_token, now)
    logger.info("incident_notifier line push done fingerprint=%s", fingerprint)
    return None


def _attempt_github_issue(
    signal: IncidentSignal, fingerprint: str, config: AppConfig, now: dt.datetime
) -> None:
    """GitHub Issue自動起票(Issue #508)を試行する。GitHub API呼び出し自体の例外は
    `incident_github_issue_service.process_incident_issue()`内で完全に握りつぶされる。
    本関数自身(`IncidentIssueNotice`構築等、API呼び出し前のセットアップ)の例外は、
    呼び出し元(`_process_signal()`)側のtry/exceptが最終防衛線として捕捉する
    (★最重要要件: いずれの段階の例外も、LINE通知経路の成否に影響しない)。
    """
    state = tracker.get_incident_state(fingerprint)
    occurrence_count = int(state["occurrence_count"]) if state else 1
    occurred_at = signal.occurred_at
    require_timezone_aware(occurred_at)
    failure_count = signal.failure_count if signal.failure_count is not None else occurrence_count
    notice = IncidentIssueNotice(
        job=resolve_incident_job(signal.job_name),
        occurred_at=occurred_at,
        fingerprint=fingerprint,
        occurrence_count=occurrence_count,
        failure_stage=resolve_incident_failure_stage(signal.failure_stage),
        failure_count=failure_count,
        consecutive_days=signal.consecutive_days,
        is_ongoing=signal.is_ongoing,
    )
    repo_owner, repo_name = _split_github_repository()
    incident_github_issue_service.process_incident_issue(
        notice,
        config.incident_notification,
        now,
        repo_owner=repo_owner,
        repo_name=repo_name,
        github_secret_arn=os.environ.get("GITHUB_APP_SECRET_ARN"),
    )


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
