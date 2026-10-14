"""infra/template.yaml の owner の符号の鍵(kv1)の Secret・IAM・観測の配線の契約テスト(Issue #925)。

固定するもの
  (1) Secret: 論理 ID・Name・Retain・GenerateSecretString(値を template に持たない)・十分な強度
  (2) identity policy: 実行 role 2 本に、その Secret 1 つの GetSecretValue だけ(他の操作・資源なし)
  (3) 環境変数: 値は Secret への Ref だけ(固定の文字列・Parameter・{{resolve}} でない)
  (4) リソースポリシー: 既存の Condition に相乗り(既定では作られない)・許可する principal の完全一致
  (5) 観測: 2 系統(設定の不備・異常)の MetricFilter と Alarm。通知先は既存の
      IncidentNotificationTopic だけ。対象の log group は存在する関数のものだけ
実装コードの実行・AWS へのアクセスは一切行わない。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "infra" / "template.yaml"

_SECRET_ID = "AllocationShadowOwnerRefKeyV1"
_POLICY_ID = "AllocationShadowOwnerRefKeyResourcePolicy"
_ENV_NAME = "ALLOCATION_SHADOW_REF_KEY_SECRET_ARN"
_READ_SID = "OwnerRefKeyReadOnly"
_FUNCTIONS = ("BuyCandidatesFunction", "BuyCandidateWorkerFunction")
_NAMESPACE = {"Sub": "${AWS::StackName}/AllocationShadow"}
_FILTERS = {
    "AllocationShadowKeyConfigIssueFilter": (
        "ALLOCATION_SHADOW_KEY_UNAVAILABLE_CONFIG",
        "KeyConfigIssue",
    ),
    "AllocationShadowKeyAnomalyFilter": (
        "ALLOCATION_SHADOW_KEY_UNAVAILABLE_ANOMALY",
        "KeyAnomaly",
    ),
}
_ALARMS = {
    "AllocationShadowKeyConfigIssueAlarm": ("KeyConfigIssue", 1, 3600),
    "AllocationShadowKeyAnomalyAlarm": ("KeyAnomaly", 3, 86400),
}


class _CfnLoader(yaml.SafeLoader):
    """!Ref / !GetAtt / !Sub を {"Ref": ...} 等の素朴な dict へ変えるだけの構文解析専用 loader。"""


def _construct(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return {tag_suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {tag_suffix: loader.construct_sequence(node)}
    assert isinstance(node, yaml.MappingNode)  # noqa: S101
    return {tag_suffix: loader.construct_mapping(node)}


_CfnLoader.add_multi_constructor("!", _construct)  # type: ignore[no-untyped-call]


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    loaded = yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_CfnLoader)  # noqa: S506
    assert isinstance(loaded, dict)
    return loaded


def _resource(template: dict[str, Any], logical_id: str) -> dict[str, Any]:
    return template["Resources"][logical_id]  # type: ignore[no-any-return]


# (1) Secret ------------------------------------------------------------------------------------


def test_the_secret_is_retained_and_has_a_fixed_logical_id_and_name(
    template: dict[str, Any],
) -> None:
    secret = _resource(template, _SECRET_ID)
    assert secret["Type"] == "AWS::SecretsManager::Secret"
    # 置換・削除で鍵が失われないこと(論理 ID を変えると置換になる = 鍵の紛失)
    assert secret["DeletionPolicy"] == "Retain"
    assert secret["UpdateReplacePolicy"] == "Retain"
    assert secret["Properties"]["Name"] == {
        "Sub": "${AWS::StackName}/allocation-shadow-owner-ref-key-v1"
    }


def test_the_secret_value_is_generated_and_never_written_in_the_template(
    template: dict[str, Any],
) -> None:
    properties = _resource(template, _SECRET_ID)["Properties"]
    generate = properties["GenerateSecretString"]
    assert "SecretString" not in properties
    assert json.loads(generate["SecretStringTemplate"]) == {"key_version": 1}
    assert generate["GenerateStringKey"] == "key"
    assert generate["ExcludePunctuation"] is True
    # 英数字 62 種 × 64 文字 = 約 381 ビット(256 ビット以上 = 43 文字以上を下回らせない)
    assert generate["PasswordLength"] == 64


def test_the_secret_is_never_read_through_a_dynamic_reference() -> None:
    # {{resolve:secretsmanager:...}} は値を CloudFormation が解決し、Lambda の環境変数に値が入る
    text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "{{resolve" in line:
            assert _SECRET_ID not in line


# (2) identity policy ---------------------------------------------------------------------------


def _read_statements(template: dict[str, Any], function_id: str) -> list[dict[str, Any]]:
    policies = _resource(template, function_id)["Properties"]["Policies"]
    return [
        statement
        for policy in policies
        if isinstance(policy, dict) and "Statement" in policy
        for statement in policy["Statement"]
        if statement.get("Sid") == _READ_SID
    ]


@pytest.mark.parametrize("function_id", _FUNCTIONS)
def test_the_runtime_roles_can_only_get_the_one_secret(
    template: dict[str, Any], function_id: str
) -> None:
    statements = _read_statements(template, function_id)
    assert len(statements) == 1
    (statement,) = statements
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == ["secretsmanager:GetSecretValue"]
    # 資源は Secret の ARN(Ref)だけ。ワイルドカード・他の secret・名前の文字列を付けない
    assert statement["Resource"] == [{"Ref": _SECRET_ID}]


def test_no_other_function_can_reach_the_secret(template: dict[str, Any]) -> None:
    holders = [
        logical_id
        for logical_id, resource in template["Resources"].items()
        if resource.get("Type") == "AWS::Serverless::Function"
        and _SECRET_ID in json.dumps(resource, ensure_ascii=False)
    ]
    assert sorted(holders) == sorted(_FUNCTIONS)


# (3) 環境変数 ----------------------------------------------------------------------------------


@pytest.mark.parametrize("function_id", _FUNCTIONS)
def test_the_environment_variable_is_only_a_ref_to_the_secret(
    template: dict[str, Any], function_id: str
) -> None:
    variables = _resource(template, function_id)["Properties"]["Environment"]["Variables"]
    assert variables[_ENV_NAME] == {"Ref": _SECRET_ID}


def test_no_parameter_carries_the_secret(template: dict[str, Any]) -> None:
    assert [n for n in template["Parameters"] if "RefKey" in n or "OwnerRef" in n] == []


# (4) リソースポリシー --------------------------------------------------------------------------


def test_the_resource_policy_rides_the_existing_condition_and_allows_only_the_intended_roles(
    template: dict[str, Any],
) -> None:
    policy = _resource(template, _POLICY_ID)
    assert policy["Condition"] == "SecretResourcePolicyIsEnabled"  # 既定(false)では作られない
    assert policy["Properties"]["SecretId"] == {"Ref": _SECRET_ID}
    (statement,) = policy["Properties"]["ResourcePolicy"]["Statement"]
    assert statement["Effect"] == "Deny" and statement["Principal"] == "*"
    assert statement["Resource"] == {"Ref": _SECRET_ID}
    allowed = statement["Condition"]["StringNotEquals"]["aws:PrincipalArn"]
    assert allowed == [
        {"Ref": "AdminPrincipalArn"},
        {"Ref": "DeployPrincipalArn"},
        {"GetAtt": "BuyCandidatesFunctionRole.Arn"},
        {"GetAtt": "BuyCandidateWorkerFunctionRole.Arn"},
    ]


# (5) 観測 --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("logical_id", "expected"), list(_FILTERS.items()))
def test_each_metric_filter_counts_one_fixed_marker_on_the_buy_log_group(
    template: dict[str, Any], logical_id: str, expected: tuple[str, str]
) -> None:
    marker, metric = expected
    resource = _resource(template, logical_id)
    assert resource["Type"] == "AWS::Logs::MetricFilter"
    properties = resource["Properties"]
    # ★ BuyCandidateWorkerFunction の log group は(fan-out が無効の間)存在しない。付けると
    # ChangeSet の EXECUTE が失敗する(2026-10-14 に実測)★
    assert properties["LogGroupName"] == {"Sub": "/aws/lambda/${BuyCandidatesFunction}"}
    assert properties["FilterPattern"] == f'"{marker}"'
    (transformation,) = properties["MetricTransformations"]
    assert transformation["MetricName"] == metric
    assert transformation["MetricValue"] == "1"
    assert transformation["MetricNamespace"] == _NAMESPACE


def test_the_two_markers_are_distinct_and_neither_contains_the_other() -> None:
    markers = [marker for marker, _ in _FILTERS.values()]
    assert len(set(markers)) == 2
    assert not any(a in b for a in markers for b in markers if a != b)


@pytest.mark.parametrize(("logical_id", "expected"), list(_ALARMS.items()))
def test_each_alarm_notifies_only_the_existing_incident_topic(
    template: dict[str, Any], logical_id: str, expected: tuple[str, int, int]
) -> None:
    metric, threshold, period = expected
    resource = _resource(template, logical_id)
    assert resource["Type"] == "AWS::CloudWatch::Alarm"
    properties = resource["Properties"]
    assert properties["MetricName"] == metric
    assert properties["Namespace"] == _NAMESPACE
    assert properties["Threshold"] == threshold and properties["Period"] == period
    assert properties["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
    assert properties["TreatMissingData"] == "notBreaching"  # 動かない日を異常にしない
    assert properties["AlarmActions"] == [{"Ref": "IncidentNotificationTopic"}]


def test_the_alarm_names_are_unique(template: dict[str, Any]) -> None:
    names = [
        json.dumps(r["Properties"].get("AlarmName"), sort_keys=True)
        for r in template["Resources"].values()
        if r.get("Type") == "AWS::CloudWatch::Alarm"
    ]
    assert len(names) == len(set(names))
