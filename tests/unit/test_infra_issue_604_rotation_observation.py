"""資金入替の観測 P の infra の契約テスト(Issue #604 P-b4 A2)。

対象: `infra/template.yaml` の P の資源(関数・Scheduler 2 本・起動用 Role・Errors Alarm・
Parameter)。設計: #604 P-b4 統合文書(…6188895158 §3〜§5)・USER 決定 #604 …6195577714
(承認の範囲 = 資源の新設のみ。★ 出荷は Scheduler = DISABLED かつ mode = OFF ★)。
template の静的解析のみで、AWS へのアクセスは行わない。

★ 本テストは YAML を読むだけで、CloudFormation / SAM の構文検証ではない ★(ローカルにも CI にも
template の検証は無い)。SAM の transform 結果は ChangeSet の CREATE で確認する(手順書 42.3 節)。

  1  Parameter: 既定 DISABLED・許可値は ENABLED / DISABLED だけ(出荷は何も起動しない)
  2  関数: Timeout・同時実行・非同期の再試行・DLQ なし・環境変数 0 件・Role を持たない(SAM が生成)
  3  ★ IAM の権限の完全一致 ★: Action・Resource をリテラルで固定(付けない権限も列挙)
  4  Scheduler 2 本: 時刻・タイムゾーン・State = Parameter・明示の RoleArn・最後の回 ≦ 上限
  5  ★ 起動用 Role ★: 信頼ポリシー = scheduler.amazonaws.com だけ・P の 1 関数への Invoke だけ
  6  Errors Alarm: 既存の保有バッチと同じ形・TreatMissingData = notBreaching・既存の Topic
  7  ★ ChangeSet の想定差分 ★: 既存の関数の変更は Layers だけ・P の資源の完全な列挙
  8  運用手順書(42.3 節)に想定差分と『想定外なら EXECUTE しない』がある
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.notification.incident_message import IncidentJob, resolve_incident_job
from jstock_advisor.domain.signals.rotation_observation_schedule import (
    DEFAULT_SCHEDULED_TIME,
    latest_safe_run_at,
    scheduled_at,
)
from jstock_advisor.lambda_handlers import rotation_observation_handler as handler_module
from jstock_advisor.services.rotation_observation_runner import TOTAL_BUDGET_SECONDS
from tests.support.iam_contract_helpers import load_template, resources

_REPO = Path(__file__).resolve().parents[2]
_FUNCTION = "RotationObservationFunction"
_ROLE = "RotationObservationSchedulerRole"
_ALARM = "RotationObservationFunctionErrorsAlarm"
_PARAMETER = "RotationObservationScheduleState"
_P_RESOURCES = {_FUNCTION, _ROLE, _ALARM}
_FUNCTION_ARN = {"Fn::GetAtt": f"{_FUNCTION}.Arn"}


def _function() -> dict[str, Any]:
    return resources()[_FUNCTION]["Properties"]


def _statements() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in _function()["Policies"]:
        assert isinstance(entry, dict) and "Statement" in entry, (
            entry
        )  # SAM の policy template は使わない
        out.extend(entry["Statement"])
    return out


def _actions(statement: dict[str, Any]) -> list[str]:
    action = statement["Action"]
    return action if isinstance(action, list) else [action]


# =================================================================================================
# 1 Parameter
# =================================================================================================


def test_the_schedule_state_parameter_ships_disabled() -> None:
    parameter = load_template()["Parameters"][_PARAMETER]

    assert parameter["Type"] == "String"
    assert parameter["Default"] == "DISABLED"  # 出荷は何も起動しない
    assert parameter["AllowedValues"] == ["ENABLED", "DISABLED"]


# =================================================================================================
# 2 関数
# =================================================================================================


def test_the_function_is_the_handler_skin_with_the_agreed_limits() -> None:
    function = _function()

    assert (
        function["Handler"] == "jstock_advisor.lambda_handlers.rotation_observation_handler.handler"
    )
    assert (
        _REPO / "src" / "jstock_advisor" / "lambda_handlers" / "rotation_observation_handler.py"
    ).exists()
    assert function["CodeUri"] == "../src/"
    assert function["Timeout"] == 60
    assert function["MemorySize"] == 512
    assert function["ReservedConcurrentExecutions"] == 1  # 同時実行 1
    assert function["EventInvokeConfig"] == {"MaximumRetryAttempts": 0}  # 非同期の再試行 0
    assert "DeadLetterQueue" not in function  # DLQ なし
    assert "Role" not in function  # SAM が実行 Role を生成する(既存の関数と同じ)
    assert "Layers" not in function  # Globals(依存 + 設定)をそのまま使う


def test_the_total_budget_fits_the_timeout_with_margin() -> None:
    """総予算(読取 40 + 書込の予約 10 = 50 秒)≦ Timeout 60 秒の 0.9(54 秒)。"""
    assert _function()["Timeout"] * 0.9 >= TOTAL_BUDGET_SECONDS


def test_the_function_has_no_environment_of_its_own() -> None:
    """P 固有の環境変数は 0 件(mode は設定ファイル。Globals 由来の変数だけ)。"""
    assert "Environment" not in _function()
    globals_env = load_template()["Globals"]["Function"]["Environment"]["Variables"]
    assert sorted(globals_env) == sorted(
        [
            "DYNAMODB_TABLE_PREFIX",
            "JSTOCK_CONFIG_DIR",
            "EDINET_API_KEY",
            "LINE_CHANNEL_ACCESS_TOKEN",
            "LINE_USER_ID",
            "LINE_CREDENTIAL_ROTATION_VERSION",
            "EDINET_CREDENTIAL_ROTATION_VERSION",
        ]
    )  # 既存の 7 件のまま(増えていない)


# =================================================================================================
# 3 IAM の権限
# =================================================================================================

#: ★ 完全一致(リテラル)★: (Sid, Action, Resource)。template から導出しない
_EXPECTED_STATEMENTS = [
    ("ReadBatchRuns", ["dynamodb:GetItem"], [{"Fn::GetAtt": "BatchRunsTable.Arn"}]),
    (
        "ReadRecommendations",
        ["dynamodb:BatchGetItem"],
        [{"Fn::GetAtt": "RecommendationsTable.Arn"}],
    ),
    (
        "QueryHoldingEvaluationRecordsIndex",
        ["dynamodb:Query"],
        [{"Fn::Sub": "${HoldingEvaluationRecordsTable.Arn}/index/holding_id-index"}],
    ),
    ("ScanHoldingsTableV2", ["dynamodb:Scan"], [{"Fn::GetAtt": "HoldingsTableV2.Arn"}]),
    ("PutObservationAudit", ["dynamodb:PutItem"], [{"Fn::GetAtt": "AuditLogTable.Arn"}]),
]


def test_the_policy_is_exactly_the_five_statements() -> None:
    actual = [(s["Sid"], _actions(s), s["Resource"]) for s in _statements()]

    assert actual == _EXPECTED_STATEMENTS
    assert all(s["Effect"] == "Allow" for s in _statements())
    assert [
        e for e in _function()["Policies"] if not isinstance(e, dict)
    ] == []  # managed policy 文字列なし


def test_no_wildcard_no_write_beyond_the_audit_put_and_no_other_service() -> None:
    allowed_actions = {
        "dynamodb:GetItem",
        "dynamodb:BatchGetItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:PutItem",
    }
    for statement in _statements():
        assert set(_actions(statement)) <= allowed_actions
        for resource in statement["Resource"]:
            assert resource != "*" and "*" not in str(resource)
    forbidden = {
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:BatchWriteItem",
        "dynamodb:TransactWriteItems",
    }
    assert not {a for s in _statements() for a in _actions(s)} & forbidden
    text = str(_function()["Policies"]).lower()
    for word in ("sqs", "secretsmanager", "lambda:invoke", "sns:", "s3:", "kms"):
        assert word not in text, word


#: HoldingsTableV2 への Scan を明示の Statement で持っていた既存の関数(A2 より前から)。
#: A2 は既存の Role へ何も足さない = この一覧が増えない(P だけが加わる)
_EXISTING_HOLDINGS_SCAN_HOLDERS = [
    "BuyCandidateWorkerFunction",
    "BuyCandidatesFunction",
    "HoldingsWatchlistFunction",
    "HoldingsWatchlistWorkerFunction",
    "LineWebhookFunction",
    "WatchlistDispatcherFunction",
]


def _explicit_scan_resources(name: str) -> list[Any]:
    return [
        s["Resource"]
        for entry in resources()[name]["Properties"].get("Policies", [])
        if isinstance(entry, dict)
        for s in entry.get("Statement", [])
        if "dynamodb:Scan" in _actions(s)
    ]


def test_scan_for_p_is_one_table_and_no_existing_role_gains_a_holdings_scan() -> None:
    assert _explicit_scan_resources(_FUNCTION) == [[{"Fn::GetAtt": "HoldingsTableV2.Arn"}]]
    holders = sorted(
        name
        for name, r in resources().items()
        if r.get("Type") == "AWS::Serverless::Function"
        and any("HoldingsTableV2" in str(resource) for resource in _explicit_scan_resources(name))
    )
    assert holders == sorted(
        [*_EXISTING_HOLDINGS_SCAN_HOLDERS, _FUNCTION]
    )  # 既存の Role へ足していない


def test_the_put_is_on_the_audit_log_only_and_the_query_is_on_the_index_only() -> None:
    puts = [s["Resource"] for s in _statements() if "dynamodb:PutItem" in _actions(s)]
    queries = [s["Resource"] for s in _statements() if "dynamodb:Query" in _actions(s)]

    assert puts == [[{"Fn::GetAtt": "AuditLogTable.Arn"}]]
    assert len(queries) == 1 and "/index/holding_id-index" in str(queries[0])
    assert "HoldingEvaluationRecordsTable.Arn}" in str(queries[0])  # 表の ARN ではなく index の ARN


# =================================================================================================
# 4 Scheduler
# =================================================================================================


def _events() -> dict[str, dict[str, Any]]:
    return _function()["Events"]


def test_exactly_two_schedules_with_the_agreed_expressions_and_the_parameter_state() -> None:
    events = _events()

    assert set(events) == {"EarlyRuns", "LastRun"}
    expressions = {name: e["Properties"]["ScheduleExpression"] for name, e in events.items()}
    assert expressions == {
        "EarlyRuns": "cron(15,30,45 8 ? * MON-FRI *)",
        "LastRun": "cron(0 9 ? * MON-FRI *)",
    }
    for event in events.values():
        assert event["Type"] == "ScheduleV2"
        props = event["Properties"]
        assert props["ScheduleExpressionTimezone"] == "Asia/Tokyo"
        assert props["State"] == {"Fn::Ref": _PARAMETER}  # 出荷 DISABLED(Parameter の既定)
        assert props["RoleArn"] == {"Fn::GetAtt": f"{_ROLE}.Arn"}  # 明示の起動用 Role
        assert props["RetryPolicy"]["MaximumRetryAttempts"] == 0
        assert props["FlexibleTimeWindow"] == {"Mode": "OFF"}


def _times(expression: str) -> list[dt.time]:
    match = re.fullmatch(r"cron\(([0-9,]+) ([0-9,]+) \? \* MON-FRI \*\)", expression)
    assert match, expression
    return [
        dt.time(int(hour), int(minute))
        for hour in match.group(2).split(",")
        for minute in match.group(1).split(",")
    ]


def test_the_run_times_are_08_15_08_30_08_45_and_09_00() -> None:
    times = sorted(
        t for e in _events().values() for t in _times(e["Properties"]["ScheduleExpression"])
    )

    assert times == [dt.time(8, 15), dt.time(8, 30), dt.time(8, 45), dt.time(9, 0)]


def test_every_run_is_after_the_holdings_batch_and_the_last_is_inside_the_ttl_limit() -> None:
    day = dt.date(2026, 10, 14)
    times = sorted(
        t for e in _events().values() for t in _times(e["Properties"]["ScheduleExpression"])
    )
    last = times[-1]

    assert times[0] > DEFAULT_SCHEDULED_TIME  # 保有バッチの 08:00 の山の後
    assert last == handler_module.LAST_RUN_TIME  # 最後の回 = runner が『最後』と見なす時刻
    assert scheduled_at(day, last) <= latest_safe_run_at(day, DEFAULT_SCHEDULED_TIME)  # TTL の内側


# =================================================================================================
# 5 起動用 Role
# =================================================================================================


def test_the_invocation_role_trusts_only_the_scheduler_service() -> None:
    role = resources()[_ROLE]["Properties"]
    trust = role["AssumeRolePolicyDocument"]

    assert trust["Statement"] == [
        {
            "Effect": "Allow",
            "Principal": {"Service": "scheduler.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ]
    assert "ManagedPolicyArns" not in role and "PermissionsBoundary" not in role


def test_the_invocation_role_may_invoke_this_one_function_only() -> None:
    role = resources()[_ROLE]["Properties"]

    [policy] = role["Policies"]
    [statement] = policy["PolicyDocument"]["Statement"]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "lambda:InvokeFunction"
    assert statement["Resource"] == [_FUNCTION_ARN]  # 他の関数・ワイルドカードを許さない


def test_the_only_explicit_iam_role_in_the_stack_is_this_one() -> None:
    explicit = sorted(name for name, r in resources().items() if r.get("Type") == "AWS::IAM::Role")
    assert explicit == [_ROLE]  # 既存の 11 本の起動用 Role は SAM の自動生成。明示はこの 1 本だけ


# =================================================================================================
# 6 Errors Alarm
# =================================================================================================


def test_the_errors_alarm_has_the_standard_shape_and_is_not_breaching_when_idle() -> None:
    alarm = resources()[_ALARM]["Properties"]
    reference = resources()["HoldingsWatchlistFunctionErrorsAlarm"]["Properties"]

    for key in ("Namespace", "MetricName", "Statistic", "Period", "EvaluationPeriods", "Threshold"):
        assert alarm[key] == reference[key], key
    assert alarm["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
    assert alarm["TreatMissingData"] == "notBreaching"  # 出荷 DISABLED の間も ALARM にならない
    assert alarm["Dimensions"] == [{"Name": "FunctionName", "Value": {"Fn::Ref": _FUNCTION}}]
    assert alarm["AlarmActions"] == [{"Fn::Ref": "IncidentNotificationTopic"}]  # 既存の Topic


# =================================================================================================
# 7 ChangeSet の想定差分
# =================================================================================================


def test_p_adds_exactly_the_three_resources_and_nothing_else_of_p() -> None:
    p_named = {name for name in resources() if "RotationObservation" in name}

    assert p_named == _P_RESOURCES  # Schedule 2 本は SAM が関数の Events から生成する


def test_existing_functions_change_only_through_the_config_layer() -> None:
    """★ 既存 15 関数の Modify = Layers だけ ★(ConfigLayer の LayerVersion の差し替え)。

    各関数に Layers の個別指定が無く(Globals の `[DependenciesLayer, ConfigLayer]` だけ)、
    ConfigLayer は設定ディレクトリ全体を ContentUri に取る = 新しい設定ファイルを足すと LayerVersion
    が 1 つ置き換わり、全関数の Layers が変わる。それ以外の変更が無いことを、この 3 点で固定する。
    """
    template = load_template()
    assert template["Globals"]["Function"]["Layers"] == [
        {"Fn::Ref": "DependenciesLayer"},
        {"Fn::Ref": "ConfigLayer"},
    ]
    assert resources()["ConfigLayer"]["Properties"]["ContentUri"] == "../config/"
    assert (_REPO / "config" / "rotation_observation.yaml").exists()  # 新しい LayerVersion の中身
    per_function_layers = [
        name
        for name, r in resources().items()
        if r.get("Type") == "AWS::Serverless::Function" and "Layers" in r["Properties"]
    ]
    assert per_function_layers == []


def test_the_function_count_is_sixteen() -> None:
    functions = [n for n, r in resources().items() if r.get("Type") == "AWS::Serverless::Function"]

    assert len(functions) == 16  # 既存 15 + P


def test_the_expected_diff_is_written_in_the_runbook() -> None:
    text = (_REPO / "docs" / "operations_manual.md").read_text(encoding="utf-8")
    section = text[text.index("## 42. ") :]  # 42 節の全体(42.2 に資源の一覧・42.3 に想定差分)

    for needle in (
        "Layers",  # 既存 15 関数の Modify の理由(src を含むリリースでは Code も)
        "Code",
        "26 → 28",  # Role の増分は P の Role だけ
        "予約同時実行の合計",  # CREATE 後・EXECUTE 前の確認
        "15",
        "DISABLED",
        "想定外",
        "EXECUTE しない",
        "Schedule",
        "RotationObservationScheduleState",
        "TreatMissingData",
    ):
        assert needle in section, needle
    for unexpected in (
        "Environment",
        "Role",
        "Policy",
        "Handler",
        "Timeout",
        "MemorySize",
        "Events",
        "Replacement",
    ):
        assert unexpected in section, unexpected  # 『想定外』の列挙に含めている


@pytest.mark.parametrize("name", sorted(_P_RESOURCES))
def test_no_p_resource_is_deleted_by_a_rollback_of_data(name: str) -> None:
    """P の資源はデータを持たない(表を新設しない)。DeletionPolicy を要する資源は無い。"""
    assert resources()[name]["Type"] in {
        "AWS::Serverless::Function",
        "AWS::IAM::Role",
        "AWS::CloudWatch::Alarm",
    }
    assert not [
        n
        for n, r in resources().items()
        if "RotationObservation" in n and r["Type"] == "AWS::DynamoDB::Table"
    ]


def test_the_alarm_notice_uses_the_existing_generic_job_name_and_adds_no_new_wording() -> None:
    """Errors Alarm の通知の本文の名称は、既存の『その他の処理』(新しい文言を足さない)。

    専用の名称(利用者に見える文言)を作るかは外部仕様 = USER の確認が要る別の判断。
    """
    assert resolve_incident_job("rotation-observation") is IncidentJob.OTHER
    assert resolve_incident_job("jstock-rotation-observation") is IncidentJob.OTHER
    assert [j.value for j in IncidentJob].count("その他の処理") == 1
    assert len(list(IncidentJob)) == 13  # 新しいメンバー(= 本文に出る新しい文言)を足していない
