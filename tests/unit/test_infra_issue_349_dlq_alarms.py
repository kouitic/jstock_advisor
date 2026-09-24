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


def _referenced_logical_id(value: object) -> str | None:
    """`!GetAtt X.Arn`(ロード後は{"Fn::GetAtt": "X.Arn"})からXを取り出す。"""
    if isinstance(value, dict) and isinstance(value.get("Fn::GetAtt"), str):
        return value["Fn::GetAtt"].split(".")[0]
    return None


def _terminal_failure_sink_queue_logical_ids(resources: dict[str, Any]) -> set[str]:
    """真正のDLQ(終端の失敗の受け皿)のlogical IDを、命名規約(例: `-dlq`サフィックス)
    ではなく実際の構造から特定する(サブちゃんレビューF1。#505 F1と同じ「内容で
    特定する」考え方。tests/unit/test_issue_501_incident_message.pyの同名の関数と
    同型)。「終端の失敗の受け皿」とは、次のいずれかを満たし、かつ自身は
    RedrivePolicyを持たないQueueである(サブちゃんレビューR1: 「配線されている
    (参照されている)」だけでは、まだどこからも配線されていない孤立DLQを
    拾えない退行があったため、和集合にした)。

        (a) 他のQueueのRedrivePolicy.deadLetterTargetArn、または
            LambdaのEventInvokeConfig.DestinationConfig.OnFailure.Destinationの
            宛先として参照されている(配線済み)
        (b) MessageRetentionPeriod=1209600(14日。運用調査用の長期保持。既存4本
            すべてがこの値を明示的に持つ。中間キューは既定4日で明示しない)
    """
    queue_logical_ids = {
        name for name, r in resources.items() if r.get("Type") == "AWS::SQS::Queue"
    }
    has_own_redirect = {
        name for name in queue_logical_ids if "RedrivePolicy" in resources[name]["Properties"]
    }

    referenced_as_failure_target: set[str] = set()
    for resource in resources.values():
        props = resource.get("Properties", {})
        redrive = props.get("RedrivePolicy")
        if isinstance(redrive, dict):
            target = _referenced_logical_id(redrive.get("deadLetterTargetArn"))
            if target:
                referenced_as_failure_target.add(target)
        on_failure = (
            props.get("EventInvokeConfig", {}).get("DestinationConfig", {}).get("OnFailure", {})
        )
        if isinstance(on_failure, dict):
            target = _referenced_logical_id(on_failure.get("Destination"))
            if target:
                referenced_as_failure_target.add(target)

    long_retention = {
        name
        for name in queue_logical_ids
        if resources[name]["Properties"].get("MessageRetentionPeriod") == 1209600
    }

    return ((referenced_as_failure_target & queue_logical_ids) | long_retention) - (
        has_own_redirect
    )


def test_every_terminal_dlq_has_a_queue_depth_alarm_watching_it() -> None:
    """全ての終端DLQ(redrive chainの終端。命名規約ではなく構造で特定する)が、
    ApproximateNumberOfMessagesVisibleを見るAlarmを持つ(Issue #349サブちゃん
    レビューF1: `-dlq`という名前ではなく、実際にredrive chainの終端であるかで
    判定することで、別の命名規約で追加されたDLQの監視漏れを防ぐ)。"""
    resources = _resources()
    terminal_ids = _terminal_failure_sink_queue_logical_ids(resources)

    watched_queue_logical_ids = {
        _referenced_logical_id(dimension["Value"])
        for name, resource in resources.items()
        if resource.get("Type") == "AWS::CloudWatch::Alarm"
        and resource.get("Properties", {}).get("Namespace") == "AWS/SQS"
        and resource.get("Properties", {}).get("MetricName")
        == "ApproximateNumberOfMessagesVisible"
        for dimension in resource["Properties"]["Dimensions"]
        if dimension.get("Name") == "QueueName"
    }

    assert terminal_ids == watched_queue_logical_ids


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
