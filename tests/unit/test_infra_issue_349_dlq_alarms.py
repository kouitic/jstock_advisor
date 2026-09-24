"""infra/template.yamlのAlarm定義がIssue #349(DLQ滞留のCloudWatch Alarm)の
設計どおりであることの回帰テスト。

対象4本(WatchlistTerminalFailureDLQ / AsyncInvokeFailureDLQ /
BuyCandidateTerminalFailureDLQ / HoldingsWatchlistTerminalFailureDLQ)いずれも、
真正のDLQへメッセージが1件でも見えたら即座にincident化する(処理遅延のような
一時的な揺らぎではなく、再試行を使い切った終端失敗が既に確定した事実のため、
S-6〔#507〕のような持続判定は不要)。

テンプレートの静的検証のみで、AWSへのアクセスは行わない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

_ALARM_LOGICAL_IDS = [
    "WatchlistTerminalFailureDLQMessagesAlarm",
    "AsyncInvokeFailureDLQMessagesAlarm",
    "BuyCandidateTerminalFailureDLQMessagesAlarm",
    "HoldingsWatchlistTerminalFailureDLQMessagesAlarm",
]

_ALARM_TO_QUEUE = {
    "WatchlistTerminalFailureDLQMessagesAlarm": "WatchlistTerminalFailureDLQ",
    "AsyncInvokeFailureDLQMessagesAlarm": "AsyncInvokeFailureDLQ",
    "BuyCandidateTerminalFailureDLQMessagesAlarm": "BuyCandidateTerminalFailureDLQ",
    "HoldingsWatchlistTerminalFailureDLQMessagesAlarm": "HoldingsWatchlistTerminalFailureDLQ",
}


def _load_template() -> dict[str, Any]:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda _l, suffix, node: {f"Fn::{suffix}": node.value})
    return yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)


def _resources() -> dict[str, Any]:
    return _load_template()["Resources"]


@pytest.mark.parametrize("logical_id", _ALARM_LOGICAL_IDS)
def test_alarm_exists_as_cloudwatch_alarm(logical_id: str) -> None:
    resource = _resources()[logical_id]
    assert resource["Type"] == "AWS::CloudWatch::Alarm"


@pytest.mark.parametrize("logical_id", _ALARM_LOGICAL_IDS)
def test_alarm_watches_the_expected_queue_depth_metric(logical_id: str) -> None:
    props = _resources()[logical_id]["Properties"]

    assert props["Namespace"] == "AWS/SQS"
    assert props["MetricName"] == "ApproximateNumberOfMessagesVisible"
    dimensions = props["Dimensions"]
    assert len(dimensions) == 1
    assert dimensions[0]["Name"] == "QueueName"
    expected_queue = _ALARM_TO_QUEUE[logical_id]
    assert dimensions[0]["Value"] == {"Fn::GetAtt": f"{expected_queue}.QueueName"}


@pytest.mark.parametrize("logical_id", _ALARM_LOGICAL_IDS)
def test_alarm_fires_on_a_single_visible_message_immediately(logical_id: str) -> None:
    """DLQは自動消費者が無く、1件でも到達すれば再試行を使い切った終端失敗が
    確定している。処理遅延のような持続判定(#507のS-6)は不要なため、
    Period=300・EvaluationPeriods=1・Threshold=1の即時検知であることを固定する。"""
    props = _resources()[logical_id]["Properties"]

    assert props["Statistic"] == "Maximum"
    assert props["Period"] == 300
    assert props["EvaluationPeriods"] == 1
    assert props["Threshold"] == 1
    assert props["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"


@pytest.mark.parametrize("logical_id", _ALARM_LOGICAL_IDS)
def test_alarm_treats_missing_data_as_not_breaching(logical_id: str) -> None:
    """SQSのApproximateNumberOfMessagesVisibleは、キューが動かない時間帯には
    データ点が飛ぶことがある(observer roleでの実測。24節)。既存のErrors/Duration
    alarmと同じ判断根拠(動きが無い状態を誤ってALARMにしない)でnotBreachingとする。"""
    props = _resources()[logical_id]["Properties"]

    assert props["TreatMissingData"] == "notBreaching"


@pytest.mark.parametrize("logical_id", _ALARM_LOGICAL_IDS)
def test_alarm_connects_to_the_existing_incident_notification_topic(logical_id: str) -> None:
    """新規のTopic・Topic Policyは作らず、#503のIncidentNotificationTopicを再利用する。"""
    props = _resources()[logical_id]["Properties"]

    assert props["AlarmActions"] == [{"Fn::Ref": "IncidentNotificationTopic"}]


def test_exactly_four_dlq_alarms_exist() -> None:
    """対象は4本のみ(想定外のDLQ Alarmが増減していないことの網羅性ガード)。"""
    resources = _resources()
    alarm_logical_ids = {
        name
        for name, resource in resources.items()
        if resource.get("Type") == "AWS::CloudWatch::Alarm"
        and resource.get("Properties", {}).get("Namespace") == "AWS/SQS"
    }

    assert alarm_logical_ids == set(_ALARM_LOGICAL_IDS)


def test_no_new_topic_or_topic_policy_was_added() -> None:
    """#503のIncidentNotificationTopic/IncidentNotificationTopicPolicyを再利用し、
    新規のSNS Topic・Topic Policyを追加していないこと(USER決定どおり)。"""
    resources = _resources()
    topic_logical_ids = {
        name for name, resource in resources.items() if resource.get("Type") == "AWS::SNS::Topic"
    }
    topic_policy_logical_ids = {
        name
        for name, resource in resources.items()
        if resource.get("Type") == "AWS::SNS::TopicPolicy"
    }

    assert topic_logical_ids == {"IncidentNotificationTopic"}
    assert topic_policy_logical_ids == {"IncidentNotificationTopicPolicy"}
