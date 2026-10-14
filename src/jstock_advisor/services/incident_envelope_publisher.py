"""Issue #665(HF-0): incident envelope(Internal structured incident payload)を
`IncidentNotificationTopic`へpublishする共通helper。

`watchlist_batch_reconciler_handler.py`のprivate関数`_publish_incident_envelope()`
(#506〜#507で確立)と同型のallowlist fail-closedチェック付きpublish関数を、新規に
HANDLED_FAILUREを発行する各handler(買い候補日次バッチ・保有監視日次バッチ等)が
再実装せずに使えるよう、共有moduleとして切り出した。

既存の`watchlist_batch_reconciler_handler.py`内のprivateコピーは、動作している
既存コードへのopportunistic touchを避けるため、本Issueでは本moduleへの置き換えを
行わない(#665設計§4 OUT_OF_SCOPE参照。統合要否は別途判断する)。
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3

# #506 USER決定のInternal payload allowlist(incident_notifier_handler.pyの
# _normalize_internal_message()が受け付けるキーと同一)に、#665でfailure_classを
# 追加したもの。stock_code/owner/holding_id/stack trace/生exception message/
# AWS account ID/ARN/request ID等は決して含めない。
INCIDENT_ENVELOPE_ALLOWLIST = frozenset(
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
        "failure_class",
    }
)


def publish_incident_envelope(
    envelope: dict[str, Any],
    *,
    allowlist: frozenset[str] = INCIDENT_ENVELOPE_ALLOWLIST,
    timeout_seconds: float | None = None,
) -> None:
    """allowlist済みのincident envelopeを`IncidentNotificationTopic`へpublishする。

    呼び出し元(検知関数)がallowlist外のキーを最初から持たせない構造にしていても、
    最後の防御としてここでも再確認する(fail-closed: allowlist外のキーがあれば
    publishせず例外にする)。

    `timeout_seconds`(Issue #670): None(既定)なら従来と完全に同じ(boto3の既定のtimeout・再試行)。
    数値を渡すと、接続・読取のtimeoutをその秒数にし、再試行しない(総待ちの上限を持たせたい
    同期の経路〔LINE webhookの応答など〕向け。加法の変更で、既存の呼び出し元は影響を受けない)。
    """
    if not set(envelope) <= allowlist:
        raise ValueError(f"incident envelope has non-allowlisted keys: {set(envelope)}")
    topic_arn = os.environ["INCIDENT_NOTIFICATION_TOPIC_ARN"]
    if timeout_seconds is None:
        sns = boto3.client("sns")
    else:
        from botocore.config import Config  # noqa: PLC0415 - 待ちの上限を使うときだけ必要

        sns = boto3.client(
            "sns",
            config=Config(
                connect_timeout=timeout_seconds,
                read_timeout=timeout_seconds,
                retries={"total_max_attempts": 1},
            ),
        )
    sns.publish(TopicArn=topic_arn, Message=json.dumps(envelope))
