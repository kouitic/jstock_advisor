"""infra/template.yamlのErrors alarm配線がIssue #504どおりであることの回帰テスト(#132 X-5。段階2)。

段階1(#503)はEvaluationFunctionのみを監視対象としていた。本Issueで残る11関数へ
Errors alarmを追加し、Lambda 12本すべてがIncidentNotificationTopicへ接続されることを
固定する。EvaluationFunctionErrorsAlarmは新規作成せず再利用する(既に#503で配線済み)。

IncidentNotifierFunction自身は本Issueの対象外(#503でAlarmActionsを意図的に空にした
自己再帰回避の設計を維持する)。

テンプレートの静的検証のみで、AWSへのアクセスは行わない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

_TOPIC_LOGICAL_ID = "IncidentNotificationTopic"

# Issue #504対象の全12関数(監視対象Lambda。#449の「handler 9本」+ Monthly/Quarterly/
# Evaluationのうち、EvaluationFunctionのみ#503で先行済み)。
_ALL_12_FUNCTIONS_TO_ALARM = {
    "BuyCandidatesFunction": "BuyCandidatesFunctionErrorsAlarm",
    "HoldingsWatchlistFunction": "HoldingsWatchlistFunctionErrorsAlarm",
    "DisclosureCheckFunction": "DisclosureCheckFunctionErrorsAlarm",
    "EvaluationFunction": "EvaluationFunctionErrorsAlarm",  # #503で先行済み。再利用のみ
    "WatchlistDispatcherFunction": "WatchlistDispatcherFunctionErrorsAlarm",
    "WatchlistWorkerFunction": "WatchlistWorkerFunctionErrorsAlarm",
    "WatchlistTerminalFailureHandlerFunction": "WatchlistTerminalFailureHandlerFunctionErrorsAlarm",
    "WatchlistBatchReconcilerFunction": "WatchlistBatchReconcilerFunctionErrorsAlarm",
    "WeeklyReviewFunction": "WeeklyReviewFunctionErrorsAlarm",
    "MonthlyReviewFunction": "MonthlyReviewFunctionErrorsAlarm",
    "QuarterlyReviewFunction": "QuarterlyReviewFunctionErrorsAlarm",
    "LineWebhookFunction": "LineWebhookFunctionErrorsAlarm",
}

# 本Issueで新規に追加する11本(EvaluationFunctionErrorsAlarmは#503で既存のため除く)。
_NEW_ALARMS = {
    logical: func
    for func, logical in _ALL_12_FUNCTIONS_TO_ALARM.items()
    if logical != "EvaluationFunctionErrorsAlarm"
}


def _load_template() -> dict[str, Any]:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda _l, suffix, node: {f"Fn::{suffix}": node.value})
    return yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)


def _resources() -> dict[str, Any]:
    return _load_template()["Resources"]


def test_all_12_target_functions_exist_in_the_template() -> None:
    resources = _resources()
    for function_name in _ALL_12_FUNCTIONS_TO_ALARM:
        assert function_name in resources, f"{function_name}がtemplateに存在しない"
        assert resources[function_name]["Type"] == "AWS::Serverless::Function"


def test_all_12_functions_have_an_errors_alarm_wired_to_the_incident_topic() -> None:
    """★ 完全性の直接固定: 12関数それぞれに対応するErrors alarmが存在し、
    AlarmActionsがIncidentNotificationTopicを指すこと。
    """
    resources = _resources()
    for function_name, alarm_logical_id in _ALL_12_FUNCTIONS_TO_ALARM.items():
        assert alarm_logical_id in resources, f"{alarm_logical_id}がtemplateに存在しない"
        props = resources[alarm_logical_id]["Properties"]
        assert props["MetricName"] == "Errors"
        assert props["Dimensions"] == [
            {"Name": "FunctionName", "Value": {"Fn::Ref": function_name}}
        ], f"{alarm_logical_id}のDimensionsが{function_name}を指していない"
        assert props["AlarmActions"] == [{"Fn::Ref": _TOPIC_LOGICAL_ID}], (
            f"{alarm_logical_id}のAlarmActionsがIncidentNotificationTopicを指していない"
        )


def test_new_11_alarms_use_the_same_design_as_the_existing_evaluation_alarm() -> None:
    """新設11本が、既存EvaluationFunctionErrorsAlarmと同一のalarm設計
    (Statistic/Period/EvaluationPeriods/Threshold/ComparisonOperator/TreatMissingData)
    に統一されていることを固定する。
    """
    resources = _resources()
    expected = {
        "Namespace": "AWS/Lambda",
        "MetricName": "Errors",
        "Statistic": "Sum",
        "Period": 900,
        "EvaluationPeriods": 1,
        "Threshold": 1,
        "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        "TreatMissingData": "notBreaching",
    }
    for alarm_logical_id in _NEW_ALARMS:
        props = resources[alarm_logical_id]["Properties"]
        for key, value in expected.items():
            assert props[key] == value, f"{alarm_logical_id}.{key}が既存設計と異なる"


def test_existing_evaluation_errors_alarm_is_reused_not_recreated() -> None:
    """#503で既に配線済みのEvaluationFunctionErrorsAlarmは、本Issueで変更しない(再利用のみ)。"""
    resources = _resources()
    props = resources["EvaluationFunctionErrorsAlarm"]["Properties"]
    assert props["AlarmActions"] == [{"Fn::Ref": _TOPIC_LOGICAL_ID}]
    assert props["Dimensions"] == [
        {"Name": "FunctionName", "Value": {"Fn::Ref": "EvaluationFunction"}}
    ]


def test_incident_notifier_function_is_not_accidentally_included() -> None:
    """★ IncidentNotifierFunction自身は#504の対象外(#503の自己再帰回避設計を維持)。
    誤って本Issueの12関数一覧へ含めていないこと、既存のown alarmにAlarmActionsが
    追加されていないことを固定する。
    """
    assert "IncidentNotifierFunction" not in _ALL_12_FUNCTIONS_TO_ALARM
    resources = _resources()
    props = resources["IncidentNotifierFunctionErrorsAlarm"]["Properties"]
    assert "AlarmActions" not in props


def test_new_alarms_do_not_touch_duration_alarm() -> None:
    """EvaluationFunctionDurationAlarm(#113/#503)は#504のscope外であり変更しない。"""
    resources = _resources()
    props = resources["EvaluationFunctionDurationAlarm"]["Properties"]
    assert props["MetricName"] == "Duration"
    assert props["Threshold"] == 720000
    assert props["AlarmActions"] == [{"Fn::Ref": _TOPIC_LOGICAL_ID}]


def test_alarm_names_are_unique_across_every_alarm_in_the_stack() -> None:
    """★ レビュー指摘 F2 の直接固定(iteration 2): 11本を手書きの`!Sub`のAlarmNameで
    一度に追加するため、衝突すると deploy失敗、またはCloudFormationが同名resourceの
    一方を暗黙に置換してしまう。固定したい性質は「AlarmNameはスタック内で一意」であり、
    本Issueの12関数分だけに限定する理由はない(Duration alarm・IncidentNotifierFunction
    自身のalarmを含む、templateに存在する`AWS::CloudWatch::Alarm`全件を対象にする。
    将来alarmが増えてもこのテストがそのまま効く)。
    """
    resources = _resources()
    alarm_logical_ids = [
        logical_id
        for logical_id, resource in resources.items()
        if resource.get("Type") == "AWS::CloudWatch::Alarm"
    ]
    assert len(alarm_logical_ids) >= 14, (
        "既知のalarm数(12関数のErrors alarm + Duration alarm + IncidentNotifierFunction自身の"
        "alarm)を下回っている"
    )
    alarm_names = [
        resources[logical_id]["Properties"]["AlarmName"]["Fn::Sub"]
        for logical_id in alarm_logical_ids
    ]
    assert len(alarm_names) == len(set(alarm_names)), (
        f"AlarmNameに重複がある: {sorted(n for n in alarm_names if alarm_names.count(n) > 1)}"
    )
