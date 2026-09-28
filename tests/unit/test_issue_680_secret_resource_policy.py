"""Issue #680(#133 UNIT2): Secrets Managerシークレットへのresource policy
(identity policyとは独立した第二の防壁)を、IaC(infra/template.yaml)上で
固定する回帰テスト。

★ 本テストはIaC(テンプレート定義)の静的検証のみを行う。AWSへのアクセス・
実際のresource policy適用は一切行わない。

設計の骨子(template.yamlのコメント参照):
    - `SecretResourcePolicyEnabled`(既定"false")がtrueのときのみ、
      5つの`AWS::SecretsManager::ResourcePolicy`リソースが作成される
      (Conditionでgateされており、本PRのmerge単独ではAWS上の状態は
      変更されない)。
    - 各resource policyは、`Principal: "*"`へのexplicit Denyを、
      `Condition.StringNotEquals.aws:PrincipalArn`でintended
      principal(ADMIN/DEPLOY、およびGithubAppSecretのみ実際に
      GetSecretValueを行う2 runtime role)を除外する形で書く
      (「許可したい相手を除外する」設計。将来の新規principalは
      明示的にallow-listへ追加しない限り自動的にDeny対象となる)。

本テストは、この設計が意図どおりの形でtemplate.yamlに書かれていること、
および allow-list からintended principalのいずれか1つでも欠けると
検知できることを固定する(counter-evidence: 実際に1件ずつ欠落させて
FAILすることを確認したうえで本ファイルを完成させた)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

_RESOURCE_POLICY_TYPE = "AWS::SecretsManager::ResourcePolicy"

_SENSITIVE_ACTIONS = frozenset(
    {
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        "secretsmanager:DeleteSecret",
        "secretsmanager:UpdateSecret",
        "secretsmanager:RotateSecret",
        "secretsmanager:PutResourcePolicy",
        "secretsmanager:DeleteResourcePolicy",
    }
)

#: 各resource policyが保護するsecret(論理名)と、対象secretのARN parameter名。
_SECRET_RESOURCE_TO_ARN_PARAM = {
    "GithubAppSecretResourcePolicy": "GithubAppSecretArn",
    "EdinetApiKeySecretResourcePolicy": "EdinetApiKeySecretArn",
    "LineChannelAccessTokenSecretResourcePolicy": "LineChannelAccessTokenSecretArn",
    "LineUserIdSecretResourcePolicy": "LineUserIdSecretArn",
    "LineChannelSecretSecretResourcePolicy": "LineChannelSecretSecretArn",
}

#: 全resource policy共通のexplicit Deny除外principal(ADMIN/DEPLOY)。
#: dictはhashableでないためsetではなくlistで表現する(順序はtemplate.yaml中の
#: 記載順と一致させる)。
_COMMON_ALLOWLIST: list[Any] = [
    {"Fn::Ref": "AdminPrincipalArn"},
    {"Fn::Ref": "DeployPrincipalArn"},
]

#: GithubAppSecretのみ、実際にsecretsmanager:GetSecretValueを行う2 runtime role
#: (Issue #133/#661で確認済み)を追加でallow-listへ含める。
#: `!GetAtt Foo.Bar`(scalar短縮形)はFn::GetAttの値が"Foo.Bar"というドット
#: 結合の文字列になる(`Fn::GetAtt: [Foo, Bar]`という長形式とは異なる表現。
#: 意味は同一)。
_GITHUB_APP_EXTRA_ALLOWLIST = [
    {"Fn::GetAtt": "IncidentNotifierFunctionRole.Arn"},
    {"Fn::GetAtt": "WeeklyReviewFunctionRole.Arn"},
]


def _construct_intrinsic(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> dict[str, Any]:
    """`!Tag`短縮形をCloudFormationの`Fn::Tag`長形式相当のPython値へ変換する。

    scalar(`!Ref foo`/`!GetAtt foo.bar`)・sequence(`!Equals [a, b]`)・
    mapping のいずれのnode種別も再帰的に解決する(scalarのみを想定した
    単純な`node.value`実装では、`!Equals [...]`のようなsequence形式で
    子nodeが未解決のまま[ScalarNodeオブジェクト自体]残ってしまう)。
    """
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{suffix}": loader.construct_mapping(node, deep=True)}


def _load_template() -> dict[str, Any]:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", _construct_intrinsic)
    return yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)


def _template() -> dict[str, Any]:
    return _load_template()


def _resources() -> dict[str, Any]:
    return _template()["Resources"]


def _parameters() -> dict[str, Any]:
    return _template()["Parameters"]


def _conditions() -> dict[str, Any]:
    return _template()["Conditions"]


def _resource_policy_names() -> list[str]:
    return [
        name
        for name, resource in _resources().items()
        if isinstance(resource, dict) and resource.get("Type") == _RESOURCE_POLICY_TYPE
    ]


def _statement(resource_name: str) -> dict[str, Any]:
    policy = _resources()[resource_name]["Properties"]["ResourcePolicy"]
    [statement] = policy["Statement"]
    return statement


def _actions(statement: dict[str, Any]) -> set[str]:
    action = statement["Action"]
    return {action} if isinstance(action, str) else set(action)


def _allowlisted_principals(statement: dict[str, Any]) -> list[Any]:
    return statement["Condition"]["StringNotEquals"]["aws:PrincipalArn"]


# --- トグル・既定値 ----------------------------------------------------------


def test_secret_resource_policy_enabled_defaults_to_false() -> None:
    """既定ではresource policyを一切作成しない(merge単独でAWS状態が変わらない)。"""
    param = _parameters()["SecretResourcePolicyEnabled"]
    assert param["Default"] == "false"
    assert set(param["AllowedValues"]) == {"true", "false"}


def test_admin_and_deploy_principal_arn_have_placeholder_defaults() -> None:
    """実際のARNをpublic repositoryへ書かない(PUBLIC_MINIMAL方針)。既定値は
    明らかなダミーであり、実在のaccount ID・principal名を含まない。
    """
    admin_default = _parameters()["AdminPrincipalArn"]["Default"]
    deploy_default = _parameters()["DeployPrincipalArn"]["Default"]
    assert "000000000000" in admin_default
    assert "000000000000" in deploy_default
    assert "not-configured" in admin_default
    assert "not-configured" in deploy_default


def test_all_resource_policies_are_gated_by_the_same_condition() -> None:
    """全5つのresource policyが同一Condition(SecretResourcePolicyIsEnabled)で
    gateされている(1つでも無条件作成なら検知する)。
    """
    names = _resource_policy_names()
    assert set(names) == set(_SECRET_RESOURCE_TO_ARN_PARAM)
    for name in names:
        assert _resources()[name]["Condition"] == "SecretResourcePolicyIsEnabled"


def test_the_condition_is_derived_from_the_toggle_parameter() -> None:
    condition = _conditions()["SecretResourcePolicyIsEnabled"]
    assert condition == {"Fn::Equals": [{"Fn::Ref": "SecretResourcePolicyEnabled"}, "true"]}


# --- 各resource policyの内容 --------------------------------------------------


def test_every_resource_policy_targets_its_own_secret_and_denies_by_default() -> None:
    """各resource policyが、対応するsecret ARN parameterのみをResourceとして
    参照し、Effect=Deny / Principal="*" であることを固定する。
    """
    for name, arn_param in _SECRET_RESOURCE_TO_ARN_PARAM.items():
        statement = _statement(name)
        assert statement["Effect"] == "Deny"
        assert statement["Principal"] == "*"
        assert statement["Resource"] == {"Fn::Ref": arn_param}
        assert _resources()[name]["Properties"]["SecretId"] == {"Fn::Ref": arn_param}


def test_every_resource_policy_denies_exactly_the_sensitive_actions() -> None:
    """全5つとも、想定した7 actionちょうどを対象とする(増減があれば検知する)。"""
    for name in _SECRET_RESOURCE_TO_ARN_PARAM:
        assert _actions(_statement(name)) == set(_SENSITIVE_ACTIONS)


def test_github_app_secret_allowlist_includes_admin_deploy_and_both_runtime_roles() -> None:
    """GithubAppSecretResourcePolicyのallow-listは、ADMIN/DEPLOYに加え、
    実際にGetSecretValueを行う2 runtime role(IncidentNotifier/WeeklyReview)を
    含む(Issue #133/#661で確認済みの実態と一致させる)。
    """
    allowlist = _allowlisted_principals(_statement("GithubAppSecretResourcePolicy"))
    expected = _COMMON_ALLOWLIST + _GITHUB_APP_EXTRA_ALLOWLIST
    assert allowlist == expected


def test_the_other_four_secrets_allowlist_only_admin_and_deploy() -> None:
    """github-app以外の4 secretは、実際にsecretsmanager APIで読むruntime
    principalが存在しない(deploy時のCFN dynamic reference解決のみ)ため、
    allow-listはADMIN/DEPLOYのみとする。
    """
    for name in _SECRET_RESOURCE_TO_ARN_PARAM:
        if name == "GithubAppSecretResourcePolicy":
            continue
        allowlist = _allowlisted_principals(_statement(name))
        assert allowlist == _COMMON_ALLOWLIST


# --- counter-evidence: allow-listの欠落を検知できることの確認 -------------------


def test_missing_admin_principal_from_the_allowlist_is_detected() -> None:
    """★ 検査そのものの確認: allow-listからAdminPrincipalArnが1件でも
    欠けていれば、上記の等価比較テストが検知する(空振りしない)ことを、
    意図的に欠落させた比較で確認する。
    """
    allowlist = _allowlisted_principals(_statement("EdinetApiKeySecretResourcePolicy"))
    mutated = [p for p in allowlist if p != {"Fn::Ref": "AdminPrincipalArn"}]
    assert mutated != _COMMON_ALLOWLIST
    assert len(mutated) == len(_COMMON_ALLOWLIST) - 1


def test_missing_a_runtime_role_from_the_github_app_allowlist_is_detected() -> None:
    """★ 同上: GithubAppSecretのallow-listからruntime roleが1件でも
    欠けていれば検知できることを確認する。
    """
    allowlist = _allowlisted_principals(_statement("GithubAppSecretResourcePolicy"))
    mutated = [p for p in allowlist if p != {"Fn::GetAtt": "IncidentNotifierFunctionRole.Arn"}]
    assert mutated != allowlist
    assert len(mutated) == len(allowlist) - 1


def test_no_resource_policy_statement_has_a_wildcard_resource_string() -> None:
    """★ 網羅的な反証: 全5つのStatementのResourceが、必ず対応するsecret ARN
    parameterへの参照であり、リテラルの`"*"`(全secret対象)になっていない
    ことを固定する。
    """
    for name in _SECRET_RESOURCE_TO_ARN_PARAM:
        resource = _statement(name)["Resource"]
        assert resource != "*"
        assert isinstance(resource, dict) and "Fn::Ref" in resource
