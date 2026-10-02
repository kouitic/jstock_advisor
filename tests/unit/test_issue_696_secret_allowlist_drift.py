"""Issue #696: github-app secret の identity policy 層(UNIT1)と resource policy 層(UNIT2)の
allow-list が、片方だけ更新されて食い違う drift を検知する。

## 背景

github-app secret へ実際に `secretsmanager:GetSecretValue` を行う関数は、2 つの層で最小権限が
固定されている。

    UNIT1  identity policy 層   各関数の `Policies`
           (test_infra_issue_133_secretsmanager_least_privilege.py)
    UNIT2  resource policy 層   GithubAppSecretResourcePolicy の allow-list
           (test_issue_680_secret_resource_policy.py)

両者の期待集合は、従来それぞれ独立にハードコードされていた。#680 の toggle
(`SecretResourcePolicyEnabled`)が有効になった後に片方だけ更新されると、新しい Lambda が
secretsmanager の AccessDenied になり、日次バッチが止まりうる。

## 本テストが固定すること(2 段)

段 1(共有の定義)
    tests/support/secret_access_registry.py の 1 箇所から、UNIT1 / UNIT2 の期待値を導出する。
    意図した変更は registry の 1 箇所の編集で済み、両層へ同時に効く。
段 2(実態の突き合わせ)
    ★ registry に依存しない。template の identity policy から導いた「GetSecretValue を持つ関数」の
    集合と、resource policy の allow-list(ADMIN / DEPLOY を除く)が指す runtime role の集合が
    一致することを直接検査する。registry を更新し忘れても、2 つの層が食い違えば落ちる。

## 変えていないこと

期待集合の値は従来と同一(IncidentNotifierFunction / WeeklyReviewFunction)。infra/template.yaml・
IAM policy・resource policy の内容・セキュリティ方針は変更しない。テンプレートの静的検証のみで、
AWS へのアクセスは行わない。

## 反証(合成入力)

template を deep copy して一部だけを変えた入力で、drift が検出されることを確認する
(on-disk の template.yaml は変更しない)。
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any

import pytest

from tests.support.iam_contract_helpers import load_template
from tests.support.secret_access_registry import (
    SECRETSMANAGER_RUNTIME_FUNCTIONS,
    runtime_role_logical_id,
)
from tests.unit import test_infra_issue_133_secretsmanager_least_privilege as unit1_module
from tests.unit import test_issue_680_secret_resource_policy as unit2_module

_GET_SECRET_VALUE = "secretsmanager:GetSecretValue"
_GITHUB_APP_RESOURCE_POLICY = "GithubAppSecretResourcePolicy"
_COMMON_ALLOWLIST = [{"Fn::Ref": "AdminPrincipalArn"}, {"Fn::Ref": "DeployPrincipalArn"}]
_ROLE_ARN = re.compile(r"^(?P<function>[A-Za-z0-9]+)Role\.Arn$")


def _lambda_functions(template: dict[str, Any]) -> list[str]:
    return [
        name
        for name, resource in template["Resources"].items()
        if isinstance(resource, dict) and resource.get("Type") == "AWS::Serverless::Function"
    ]


def _grants_get_secret_value(action: str) -> bool:
    """actionが `secretsmanager:GetSecretValue` に到達しうるか(wildcard action を含む)。"""
    return action == "*" or fnmatch.fnmatch(_GET_SECRET_VALUE, action)


def _identity_policy_holders(template: dict[str, Any]) -> set[str]:
    """identity policy(関数の Policies の raw Statement)で GetSecretValue を持つ関数の論理 ID。"""
    holders: set[str] = set()
    for name in _lambda_functions(template):
        for policy in template["Resources"][name]["Properties"].get("Policies", []) or []:
            if not isinstance(policy, dict) or "Statement" not in policy:
                continue
            for statement in policy["Statement"] or []:
                if statement.get("Effect", "Allow") != "Allow":
                    continue
                action = statement.get("Action", [])
                actions = {action} if isinstance(action, str) else set(action)
                if any(_grants_get_secret_value(a) for a in actions):
                    holders.add(name)
    return holders


def _resource_policy_allowlisted_functions(template: dict[str, Any]) -> set[str]:
    """GithubAppSecretResourcePolicy の allow-list(ADMIN / DEPLOY 除く)が指す role の関数の論理 ID。

    `!GetAtt <関数>Role.Arn` の形でない要素は、そのまま「認識できない要素」として集合へ含める
    (黙って無視すると、形の変わった要素が drift として検出されない)。
    """
    [statement] = template["Resources"][_GITHUB_APP_RESOURCE_POLICY]["Properties"][
        "ResourcePolicy"
    ]["Statement"]
    allowlist = statement["Condition"]["StringNotEquals"]["aws:PrincipalArn"]
    functions: set[str] = set()
    for principal in allowlist:
        if principal in _COMMON_ALLOWLIST:
            continue
        match = (
            _ROLE_ARN.match(principal["Fn::GetAtt"])
            if isinstance(principal, dict) and isinstance(principal.get("Fn::GetAtt"), str)
            else None
        )
        functions.add(match["function"] if match else f"<unrecognized: {principal!r}>")
    return functions


def secret_access_drift(
    template: dict[str, Any], registry_functions: tuple[str, ...]
) -> dict[str, set[str]]:
    """2 つの層と registry の食い違いを返す。空 dict なら drift なし。"""
    identity = _identity_policy_holders(template)
    resource = _resource_policy_allowlisted_functions(template)
    registry = set(registry_functions)
    drift = {
        "identity_but_not_resource": identity - resource,
        "resource_but_not_identity": resource - identity,
        "registry_but_not_identity": registry - identity,
        "identity_but_not_registry": identity - registry,
    }
    return {key: value for key, value in drift.items() if value}


# --- 段 1: 共有の定義 ---------------------------------------------------------------


def test_unit1_and_unit2_expectations_equal_the_shared_registry() -> None:
    """★ UNIT1 / UNIT2 の期待値が、共有 registry の定義と一致していること。

    どちらか一方の期待値だけを書き換える(registry から外れて再びハードコードする等)と落ちる。
    ★ 値の一致を見る検査であり、「導出の形で書かれているか」までは見ない(値が同じなら通る)。
    """
    expected_functions = set(SECRETSMANAGER_RUNTIME_FUNCTIONS)
    expected_allowlist = [
        {"Fn::GetAtt": f"{runtime_role_logical_id(function)}.Arn"}
        for function in SECRETSMANAGER_RUNTIME_FUNCTIONS
    ]
    assert expected_functions == unit1_module._EXPECTED_SECRETSMANAGER_PRINCIPALS
    assert expected_allowlist == unit2_module._GITHUB_APP_EXTRA_ALLOWLIST


def test_registry_functions_are_real_lambda_functions_in_the_template() -> None:
    """registry が古くならないこと(template に実在しない関数を期待し続けない)。"""
    template = load_template()
    assert set(SECRETSMANAGER_RUNTIME_FUNCTIONS) <= set(_lambda_functions(template))
    assert len(set(SECRETSMANAGER_RUNTIME_FUNCTIONS)) == len(SECRETSMANAGER_RUNTIME_FUNCTIONS)


def _collect_getatt_values(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Fn::GetAtt" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_collect_getatt_values(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_getatt_values(item))
    return found


def test_runtime_role_naming_convention_matches_every_function_role_reference() -> None:
    """★ `runtime_role_logical_id()` の規約(関数の論理 ID + "Role")が、template 全体の
    `!GetAtt <X>Role.Arn` 参照と一致すること。SAM の暗黙 role は Resources に現れないため、
    参照側から規約を確認する。
    """
    template = load_template()
    functions = set(_lambda_functions(template))
    role_references = [
        value for value in _collect_getatt_values(template) if _ROLE_ARN.match(value)
    ]
    assert role_references, "role 参照が 1 件も見つからない(前提が崩れている)"
    for value in role_references:
        function = _ROLE_ARN.match(value)["function"]  # type: ignore[index]
        # `<X>Role.Arn` の X が Lambda 関数の論理 ID であるものだけが、規約の対象。
        # (IAM::Role 等の通常のリソースを指す参照は対象外)
        if function in functions:
            assert runtime_role_logical_id(function) + ".Arn" == value
    for function in SECRETSMANAGER_RUNTIME_FUNCTIONS:
        assert f"{runtime_role_logical_id(function)}.Arn" in role_references


# --- 段 2: 実態の突き合わせ(registry に依存しない)-------------------------------------


def test_identity_policy_and_resource_policy_agree_on_the_runtime_roles() -> None:
    """★ 現在の template で、drift が無いこと(2 つの層と registry が一致する)。"""
    assert secret_access_drift(load_template(), SECRETSMANAGER_RUNTIME_FUNCTIONS) == {}


# --- 反証: 「片方だけ更新」を模した合成入力で drift が検出されること -------------------------


def _other_function(template: dict[str, Any]) -> str:
    return next(
        name for name in _lambda_functions(template) if name not in SECRETSMANAGER_RUNTIME_FUNCTIONS
    )


def _grant_get_secret_value(template: dict[str, Any], function: str, action: str) -> None:
    template["Resources"][function]["Properties"].setdefault("Policies", []).append(
        {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": action,
                    "Resource": {"Fn::Ref": "GithubAppSecretArn"},
                }
            ]
        }
    )


def _allowlist(template: dict[str, Any]) -> list[Any]:
    [statement] = template["Resources"][_GITHUB_APP_RESOURCE_POLICY]["Properties"][
        "ResourcePolicy"
    ]["Statement"]
    return statement["Condition"]["StringNotEquals"]["aws:PrincipalArn"]


def test_a_new_identity_policy_holder_without_the_resource_policy_is_detected() -> None:
    """(b) template の identity policy へ 1 関数を足し、resource policy は変えない → 検出する。"""
    template = load_template()
    added = _other_function(template)
    _grant_get_secret_value(template, added, _GET_SECRET_VALUE)
    drift = secret_access_drift(template, SECRETSMANAGER_RUNTIME_FUNCTIONS)
    assert drift["identity_but_not_resource"] == {added}
    assert drift["identity_but_not_registry"] == {added}


@pytest.mark.parametrize("action", ["secretsmanager:*", "secretsmanager:Get*", "*"])
def test_wildcard_actions_that_include_get_secret_value_are_detected(action: str) -> None:
    """wildcard action による包含も identity policy の保有者として数える(UNIT1 と同じ観点)。"""
    template = load_template()
    added = _other_function(template)
    _grant_get_secret_value(template, added, action)
    assert secret_access_drift(template, SECRETSMANAGER_RUNTIME_FUNCTIONS)[
        "identity_but_not_resource"
    ] == {added}


@pytest.mark.parametrize("action", ["secretsmanager:PutSecretValue", "secretsmanager:Put*"])
def test_actions_that_do_not_reach_get_secret_value_are_not_holders(action: str) -> None:
    """GetSecretValue に到達しない action は、この突き合わせの対象外(誤検出しない)。"""
    template = load_template()
    _grant_get_secret_value(template, _other_function(template), action)
    assert secret_access_drift(template, SECRETSMANAGER_RUNTIME_FUNCTIONS) == {}


def test_a_runtime_role_removed_from_the_allowlist_is_detected() -> None:
    """(c) allow-list から 1 role を外す(identity policy は変えない)→ 検出する。"""
    template = load_template()
    allowlist = _allowlist(template)
    allowlist.remove({"Fn::GetAtt": f"{runtime_role_logical_id('WeeklyReviewFunction')}.Arn"})
    drift = secret_access_drift(template, SECRETSMANAGER_RUNTIME_FUNCTIONS)
    assert drift == {"identity_but_not_resource": {"WeeklyReviewFunction"}}


def test_a_role_added_to_the_allowlist_without_identity_access_is_detected() -> None:
    """(d) allow-list へ 1 role を足す(identity policy は変えない)→ 検出する。"""
    template = load_template()
    added = _other_function(template)
    _allowlist(template).append({"Fn::GetAtt": f"{runtime_role_logical_id(added)}.Arn"})
    assert secret_access_drift(template, SECRETSMANAGER_RUNTIME_FUNCTIONS) == {
        "resource_but_not_identity": {added}
    }


def test_an_unrecognized_allowlist_element_is_surfaced_not_ignored() -> None:
    """allow-list の要素が `!GetAtt <X>Role.Arn` の形でなくなっても、黙って無視しない。"""
    template = load_template()
    _allowlist(template).append("arn:aws:iam::000000000000:role/placeholder")
    drift = secret_access_drift(template, SECRETSMANAGER_RUNTIME_FUNCTIONS)
    assert set(drift) == {"resource_but_not_identity"}
    assert next(iter(drift["resource_but_not_identity"])).startswith("<unrecognized:")


def test_a_registry_entry_without_identity_access_is_detected() -> None:
    """(a) registry へ関数を 1 つ足す(template は変えない)→ 検出する。"""
    template = load_template()
    added = _other_function(template)
    drift = secret_access_drift(template, (*SECRETSMANAGER_RUNTIME_FUNCTIONS, added))
    assert drift == {"registry_but_not_identity": {added}}
