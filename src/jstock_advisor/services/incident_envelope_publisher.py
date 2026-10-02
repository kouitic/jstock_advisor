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
    envelope: dict[str, Any], *, allowlist: frozenset[str] = INCIDENT_ENVELOPE_ALLOWLIST
) -> None:
    """allowlist済みのincident envelopeを`IncidentNotificationTopic`へpublishする。

    呼び出し元(検知関数)がallowlist外のキーを最初から持たせない構造にしていても、
    最後の防御としてここでも再確認する(fail-closed: allowlist外のキーがあれば
    publishせず例外にする)。
    """
    if not set(envelope) <= allowlist:
        raise ValueError(f"incident envelope has non-allowlisted keys: {set(envelope)}")
    topic_arn = os.environ["INCIDENT_NOTIFICATION_TOPIC_ARN"]
    sns = boto3.client("sns")
    sns.publish(TopicArn=topic_arn, Message=json.dumps(envelope))
