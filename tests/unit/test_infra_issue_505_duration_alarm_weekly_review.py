"""infra/template.yamlのDuration alarm配線がIssue #505どおりであることの回帰テスト
(#132 X-6。段階2)。

段階1(#503)・段階2前半(#504)ではErrors alarmを扱った。本Issueは、実行時間が長い関数の
Timeoutの80%到達を検知するDuration alarmを、WeeklyReviewFunctionのみへ追加する
(既存のEvaluationFunctionDurationAlarmは変更しない)。

他10関数(WatchlistWorker/WatchlistDispatcher/LineWebhook/BuyCandidates/HoldingsWatchlist/
DisclosureCheck/WatchlistBatchReconciler/MonthlyReview/QuarterlyReview/
WatchlistTerminalFailureHandler)は今回見送り(MANAGER判断)であり、本テストは追加しない。

テンプレートの静的検証のみで、AWSへのアクセスは行わない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

_TOPIC_LOGICAL_ID = "IncidentNotificationTopic"
_NEW_ALARM_LOGICAL_ID = "WeeklyReviewFunctionDurationAlarm"


def _load_template() -> dict[str, Any]:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda _l, suffix, node: {f"Fn::{suffix}": node.value})
    return yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)


def _resources() -> dict[str, Any]:
    return _load_template()["Resources"]


def test_weekly_review_function_timeout_is_still_300_seconds() -> None:
    """★ Threshold(240秒=80%)の計算根拠の前提: WeeklyReviewFunctionのTimeoutが
    #539(OOM修正)後も300秒のまま変わっていないことを固定する。Timeoutが変われば
    240秒という値自体を見直す必要があるため、この前提を独立して固定する。
    """
    resources = _resources()
    props = resources["WeeklyReviewFunction"]["Properties"]
    globals_timeout = _load_template()["Globals"]["Function"]["Timeout"]
    timeout = props.get("Timeout", globals_timeout)
    assert timeout == 300


def test_weekly_review_duration_alarm_exists_and_is_wired_to_the_incident_topic() -> None:
    resources = _resources()
    assert _NEW_ALARM_LOGICAL_ID in resources
    props = resources[_NEW_ALARM_LOGICAL_ID]["Properties"]
    assert props["Namespace"] == "AWS/Lambda"
    assert props["MetricName"] == "Duration"
    assert props["Dimensions"] == [
        {"Name": "FunctionName", "Value": {"Fn::Ref": "WeeklyReviewFunction"}}
    ]
    assert props["Statistic"] == "Maximum"
    assert props["Period"] == 900
    assert props["EvaluationPeriods"] == 1
    assert props["Threshold"] == 240000
    assert props["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
    assert props["TreatMissingData"] == "notBreaching"
    assert props["AlarmActions"] == [{"Fn::Ref": _TOPIC_LOGICAL_ID}]


def test_threshold_is_exactly_80_percent_of_the_function_timeout() -> None:
    """★ 240,000msという数値そのものが、Timeout(300秒)×0.8から来ていることを直接固定する
    (レビューでの反証: Timeoutだけ将来変わってThresholdの追従を忘れる回帰を検知する)。
    """
    resources = _resources()
    globals_timeout = _load_template()["Globals"]["Function"]["Timeout"]
    timeout_seconds = resources["WeeklyReviewFunction"]["Properties"].get(
        "Timeout", globals_timeout
    )
    threshold_ms = resources[_NEW_ALARM_LOGICAL_ID]["Properties"]["Threshold"]
    assert threshold_ms == int(timeout_seconds * 1000 * 0.8)


def test_existing_evaluation_duration_alarm_is_unchanged() -> None:
    """EvaluationFunctionDurationAlarm(#113/#503)は#505のscope外であり変更しない。"""
    resources = _resources()
    props = resources["EvaluationFunctionDurationAlarm"]["Properties"]
    assert props["MetricName"] == "Duration"
    assert props["Threshold"] == 720000
    assert props["Statistic"] == "Maximum"
    assert props["TreatMissingData"] == "notBreaching"
    assert props["AlarmActions"] == [{"Fn::Ref": _TOPIC_LOGICAL_ID}]


def test_no_duration_alarm_added_for_the_deferred_functions() -> None:
    """★ 今回見送った10関数へDuration alarmを誤って追加していないことを固定する。"""
    resources = _resources()
    deferred_functions = [
        "WatchlistWorkerFunction",
        "WatchlistDispatcherFunction",
        "LineWebhookFunction",
        "BuyCandidatesFunction",
        "HoldingsWatchlistFunction",
        "DisclosureCheckFunction",
        "WatchlistBatchReconcilerFunction",
        "MonthlyReviewFunction",
        "QuarterlyReviewFunction",
        "WatchlistTerminalFailureHandlerFunction",
    ]
    for function_name in deferred_functions:
        assert f"{function_name}DurationAlarm" not in resources, (
            f"{function_name}へのDuration alarmは今回見送りのはずだが存在する"
        )


def test_duration_alarm_name_does_not_collide_with_any_other_alarm() -> None:
    """AlarmNameがスタック内で一意であること(#504 F2と同じ観点。全alarmを対象にする)。"""
    resources = _resources()
    alarm_logical_ids = [
        logical_id
        for logical_id, resource in resources.items()
        if resource.get("Type") == "AWS::CloudWatch::Alarm"
    ]
    alarm_names = [
        resources[logical_id]["Properties"]["AlarmName"]["Fn::Sub"]
        for logical_id in alarm_logical_ids
    ]
    assert len(alarm_names) == len(set(alarm_names)), (
        f"AlarmNameに重複がある: {sorted(n for n in alarm_names if alarm_names.count(n) > 1)}"
    )
