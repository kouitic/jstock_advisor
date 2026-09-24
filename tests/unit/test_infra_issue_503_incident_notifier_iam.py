"""infra/template.yamlのIAM・配線がIssue #503の最小権限どおりであることの回帰テスト。

IncidentNotifierFunction が IncidentStateTable に対して行うのは、fingerprint単位の
GetItem/PutItem/UpdateItem/DeleteItemだけである(Scan/Queryは付与しない = GSIが無く
そもそも使わないことのIAM側の担保)。

既存2 alarm(EvaluationFunctionErrorsAlarm/EvaluationFunctionDurationAlarm)へは
AlarmActionsの追加のみ(閾値・メトリクス・Dimensions等は変更しない = LOCK_LEVEL 1)。

IncidentNotifierFunction自身のErrors Alarmは、AlarmActionsを持たない(自己再帰を
避けるための意図的な設計。baseline)。

テンプレートの静的検証のみで、AWSへのアクセスは行わない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

_TABLE_LOGICAL_ID = "IncidentStateTable"
_TOPIC_LOGICAL_ID = "IncidentNotificationTopic"
_NOTIFIER_SID = "ReadWriteIncidentState"


def _load_template() -> dict[str, Any]:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda _l, suffix, node: {f"Fn::{suffix}": node.value})
    return yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)


def _resources() -> dict[str, Any]:
    return _load_template()["Resources"]


def _incident_state_statements(function_name: str) -> list[dict[str, Any]]:
    policies = _resources()[function_name]["Properties"].get("Policies", [])
    found: list[dict[str, Any]] = []
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        for statement in policy.get("Statement", []) or []:
            if _TABLE_LOGICAL_ID in str(statement.get("Resource", "")):
                found.append(statement)
    return found


def _actions(statement: dict[str, Any]) -> set[str]:
    action = statement["Action"]
    return {action} if isinstance(action, str) else set(action)


# --- IncidentStateTable -------------------------------------------------------


def test_incident_state_table_is_retained_and_recoverable() -> None:
    table = _resources()[_TABLE_LOGICAL_ID]
    assert table["Type"] == "AWS::DynamoDB::Table"
    assert table["DeletionPolicy"] == "Retain"
    assert table["UpdateReplacePolicy"] == "Retain"
    props = table["Properties"]
    assert props["DeletionProtectionEnabled"] is True
    assert props["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True
    assert "TimeToLiveSpecification" not in props  # TTLはcleanup専用として本moduleでは書かない


def test_incident_state_table_key_schema_is_fingerprint_only() -> None:
    props = _resources()[_TABLE_LOGICAL_ID]["Properties"]
    assert [k["AttributeName"] for k in props["KeySchema"]] == ["fingerprint"]
    assert props["KeySchema"][0]["KeyType"] == "HASH"


# --- IncidentNotifierFunction の IAM ---------------------------------------------


def test_incident_notifier_can_only_crud_single_items_no_scan_or_query() -> None:
    statements = _incident_state_statements("IncidentNotifierFunction")
    assert len(statements) == 1, "IncidentStateTableへの権限は1つのStatementに集約する"

    statement = statements[0]
    assert statement["Sid"] == _NOTIFIER_SID
    assert statement["Effect"] == "Allow"
    actions = _actions(statement)
    assert actions == {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
    }
    assert "dynamodb:Scan" not in actions
    assert "dynamodb:Query" not in actions
    assert "*" not in str(statement["Resource"]), "ワイルドカードを使わない"


def test_incident_notifier_is_subscribed_to_the_topic_via_sns_event() -> None:
    events = _resources()["IncidentNotifierFunction"]["Properties"]["Events"]
    sns_events = [e for e in events.values() if e.get("Type") == "SNS"]
    assert len(sns_events) == 1
    assert sns_events[0]["Properties"]["Topic"] == {"Fn::Ref": _TOPIC_LOGICAL_ID}


# --- SNS Topic / TopicPolicy ----------------------------------------------------


def test_topic_policy_grants_cloudwatch_publish_scoped_to_this_account() -> None:
    policy = _resources()["IncidentNotificationTopicPolicy"]
    assert policy["Type"] == "AWS::SNS::TopicPolicy"
    assert policy["Properties"]["Topics"] == [{"Fn::Ref": _TOPIC_LOGICAL_ID}]
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    cloudwatch_statements = [
        s for s in statements if s.get("Principal") == {"Service": "cloudwatch.amazonaws.com"}
    ]
    [statement] = cloudwatch_statements
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "sns:Publish"
    assert statement["Condition"]["StringEquals"]["aws:SourceAccount"] == {
        "Fn::Ref": "AWS::AccountId"
    }


def test_topic_policy_grants_reconciler_publish_without_wildcard_resource() -> None:
    """Issue #506(O-1): reconciler発のInternal payloadも同じTopicへpublishできるよう、
    reconciler実行ロール向けのStatementを追加した(Resource="*"は付与しない)。
    """
    policy = _resources()["IncidentNotificationTopicPolicy"]
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    reconciler_statements = [
        s for s in statements if s.get("Principal") != {"Service": "cloudwatch.amazonaws.com"}
    ]
    [statement] = reconciler_statements
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "sns:Publish"
    assert statement["Resource"] == {"Fn::Ref": _TOPIC_LOGICAL_ID}
    assert "*" not in str(statement["Principal"])


# --- 既存2 alarmへの接続(閾値・メトリクスは変更しない) ---------------------------


def test_existing_evaluation_alarms_are_wired_to_the_incident_topic() -> None:
    for name in ("EvaluationFunctionErrorsAlarm", "EvaluationFunctionDurationAlarm"):
        props = _resources()[name]["Properties"]
        assert props["AlarmActions"] == [{"Fn::Ref": _TOPIC_LOGICAL_ID}]


_ERRORS_ALARM_FIXED_PROPS = {
    "AlarmName": {"Fn::Sub": "${AWS::StackName}-evaluation-errors"},
    "AlarmDescription": "定点評価Lambdaが失敗した(タイムアウトを含む)。Issue #113参照。",
    "Namespace": "AWS/Lambda",
    "MetricName": "Errors",
    "Dimensions": [{"Name": "FunctionName", "Value": {"Fn::Ref": "EvaluationFunction"}}],
    "Statistic": "Sum",
    "Period": 900,
    "EvaluationPeriods": 1,
    "Threshold": 1,
    "ComparisonOperator": "GreaterThanOrEqualToThreshold",
    "TreatMissingData": "notBreaching",
}

_DURATION_ALARM_FIXED_PROPS = {
    "AlarmName": {"Fn::Sub": "${AWS::StackName}-evaluation-duration"},
    "AlarmDescription": "定点評価Lambdaの実行時間がTimeoutの80%(720秒)に達した。Issue #113参照。",
    "Namespace": "AWS/Lambda",
    "MetricName": "Duration",
    "Dimensions": [{"Name": "FunctionName", "Value": {"Fn::Ref": "EvaluationFunction"}}],
    "Statistic": "Maximum",
    "Period": 900,
    "EvaluationPeriods": 1,
    "Threshold": 720000,
    "ComparisonOperator": "GreaterThanOrEqualToThreshold",
    "TreatMissingData": "notBreaching",
}


def test_existing_evaluation_alarms_thresholds_are_unchanged() -> None:
    """★ レビュー指摘 F3 の直接固定: LOCK_LEVEL 1(追加のみ)の裏付けとして、AlarmActions
    以外の全プロパティ(閾値・比較演算子・TreatMissingData・EvaluationPeriods等)が一切
    変わっていないことを網羅的に固定する。

    対象Lambdaは平日18:00のみ稼働のため、TreatMissingDataがnotBreachingから変わると
    非稼働時間のたびにALARMとなり、AlarmActionsが繋がった今はLINEへの誤送信に直結する
    (3プロパティだけの部分確認では、この種の変異を検知できなかった)。
    """
    for name, expected in (
        ("EvaluationFunctionErrorsAlarm", _ERRORS_ALARM_FIXED_PROPS),
        ("EvaluationFunctionDurationAlarm", _DURATION_ALARM_FIXED_PROPS),
    ):
        props = dict(_resources()[name]["Properties"])
        props.pop("AlarmActions", None)  # #503で追加した唯一の差分。ここでは比較しない
        assert props == expected, name


# --- self-monitoring(自己再帰を避ける) -------------------------------------------


def test_incident_notifier_own_errors_alarm_has_no_alarm_actions() -> None:
    """★ 自身のAlarmを同じTopicへ流すと自己再帰になるため、意図的にAlarmActionsを持たない。"""
    props = _resources()["IncidentNotifierFunctionErrorsAlarm"]["Properties"]
    assert "AlarmActions" not in props
    assert props["Dimensions"] == [
        {"Name": "FunctionName", "Value": {"Fn::Ref": "IncidentNotifierFunction"}}
    ]
