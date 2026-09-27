"""infra/template.yaml全体で、secretsmanager権限がwildcard Resourceを
持たないことを固定する回帰テスト(Issue #133 UNIT1)。

Issue #133(同一AWSアカウント内の別workloadからJstock credentialへ到達できる
問題)のPhase A調査(2026-09-04)・fresh再調査(2026-09-27)のいずれでも、
IaC(本テンプレート)上でsecretsmanager権限を持つLambda関数は
IncidentNotifierFunction / WeeklyReviewFunctionの2つのみであり、いずれも
GithubAppSecretArn(exact ARN)へ限定されていることを確認済みである。

本moduleは、既存の個別関数向けテスト(test_infra_issue_503_incident_notifier_iam.py
のtest_incident_notifier_does_not_have_any_wildcard_resource_statement等)が
関数単位でしか検知しない構造的な穴を塞ぐため、**テンプレート内の全Lambda関数**を
対象に、secretsmanager関連actionのwildcard Resource禁止を横断的に固定する。
新しいLambda関数がsecretsmanager権限を持つよう追加された場合、本テストの
_EXPECTED_SECRETSMANAGER_PRINCIPALSを意図的に更新しない限りテストが失敗する
(将来の同種principalの無自覚な追加を検知する)。

テンプレートの静的検証のみで、AWSへのアクセスは行わない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

# secretsmanagerのうち、値の取得・書き換え・resource policy操作に相当するaction。
# List/Describe等の metadata 系は対象外(値そのものへの到達力を持つものだけを
# wildcard禁止の対象とする)。
_SENSITIVE_SECRETSMANAGER_ACTIONS = {
    "secretsmanager:GetSecretValue",
    "secretsmanager:PutSecretValue",
    "secretsmanager:DeleteSecret",
    "secretsmanager:UpdateSecret",
    "secretsmanager:RotateSecret",
    "secretsmanager:PutResourcePolicy",
    "secretsmanager:DeleteResourcePolicy",
}

# 2026-09-27時点でsecretsmanager権限を持つことが確認済みの関数。
# 新しい関数をここへ追加する場合は、Resourceがexact ARN(wildcardでない)
# であることを別途確認したうえで追加すること。
_EXPECTED_SECRETSMANAGER_PRINCIPALS = {
    "IncidentNotifierFunction",
    "WeeklyReviewFunction",
}


def _load_template() -> dict[str, Any]:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda _l, suffix, node: {f"Fn::{suffix}": node.value})
    return yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)


def _resources() -> dict[str, Any]:
    return _load_template()["Resources"]


def _lambda_function_logical_ids() -> list[str]:
    return [
        name
        for name, resource in _resources().items()
        if isinstance(resource, dict) and resource.get("Type") == "AWS::Serverless::Function"
    ]


def _actions(statement: dict[str, Any]) -> set[str]:
    action = statement.get("Action", [])
    return {action} if isinstance(action, str) else set(action)


def _secretsmanager_statements(function_name: str) -> list[dict[str, Any]]:
    policies = _resources()[function_name]["Properties"].get("Policies", [])
    found: list[dict[str, Any]] = []
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        for statement in policy.get("Statement", []) or []:
            if _actions(statement) & _SENSITIVE_SECRETSMANAGER_ACTIONS:
                found.append(statement)
    return found


def test_template_defines_exactly_fifteen_lambda_functions() -> None:
    """関数総数を固定する。増減があれば、本module以下のテストの前提
    (全関数を横断確認している、という主張)が崩れていないかを見直す契機にする。
    """
    assert len(_lambda_function_logical_ids()) == 15


def test_only_expected_functions_have_secretsmanager_sensitive_actions() -> None:
    """secretsmanager(値の取得・書き換え・resource policy操作)権限を持つ
    関数の集合が、既知の2関数からいっさい増減していないことを固定する。
    """
    actual = {
        name for name in _lambda_function_logical_ids() if _secretsmanager_statements(name)
    }
    assert actual == _EXPECTED_SECRETSMANAGER_PRINCIPALS


def test_no_lambda_function_has_wildcard_resource_for_secretsmanager_actions() -> None:
    """★ 網羅的な反証: 全13関数のPoliciesのうち、secretsmanagerの
    sensitive actionを持つStatementが1つでもResource="*"を含めば検知する。
    """
    violations: list[str] = []
    for name in _lambda_function_logical_ids():
        for statement in _secretsmanager_statements(name):
            resource = statement.get("Resource")
            resources = resource if isinstance(resource, list) else [resource]
            if "*" in resources:
                violations.append(f"{name}: {statement}")
    assert violations == [], f"wildcard Resourceを検出: {violations}"


def test_expected_secretsmanager_functions_are_scoped_to_the_shared_github_app_secret() -> None:
    """既知の2関数(IncidentNotifier/WeeklyReview)が、いずれもGithubAppSecretArn
    (exact ARNパラメータ)のみを参照し、新規secretを作っていないことを固定する。
    """
    for name in _EXPECTED_SECRETSMANAGER_PRINCIPALS:
        statements = _secretsmanager_statements(name)
        assert len(statements) == 1, name
        statement = statements[0]
        assert statement["Effect"] == "Allow"
        assert statement["Resource"] == {"Fn::Ref": "GithubAppSecretArn"}
        assert _actions(statement) == {"secretsmanager:GetSecretValue"}
