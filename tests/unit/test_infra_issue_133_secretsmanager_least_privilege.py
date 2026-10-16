"""infra/template.yaml全体で、secretsmanager権限がwildcard Resourceを
持たないことを固定する回帰テスト(Issue #133 UNIT1)。

Issue #133(同一AWSアカウント内の別workloadからJstock credentialへ到達できる
問題)のPhase A調査(2026-09-04)・fresh再調査(2026-09-27)のいずれでも、
IaC(本テンプレート)上でraw IAM Statementとしてsecretsmanager権限を持つ
Lambda関数はIncidentNotifierFunction / WeeklyReviewFunctionの2つのみであり、
いずれもGithubAppSecretArn(exact ARN)へ限定されていることを確認済みである。

本moduleは、既存の個別関数向けテスト(test_infra_issue_503_incident_notifier_iam.py
のtest_incident_notifier_does_not_have_any_wildcard_resource_statement等)が
関数単位でしか検知しない構造的な穴を塞ぐため、**テンプレート内の全Lambda関数**を
対象に、secretsmanager関連actionのwildcard禁止を横断的に固定する。

★ レビュー指摘F1(PR #661、サブちゃん)の是正: 初版はraw Statement形式の
完全一致actionしか見ておらず、以下がいずれも検出を素通りした。
  - SAM policy template shorthand(例: `SecretsManagerReadWrite`,
    `AWSSecretsManagerGetSecretValuePolicy`。1キーのdict、またはbare文字列で
    Statementを持たない形)
  - wildcard action(`secretsmanager:*` や `Action: "*"`)
本moduleはこれらも検出対象に含める。新しいLambda関数がsecretsmanager権限を
持つよう追加された場合(raw Statement・SAM policy template shorthandの
いずれの形でも)、本テストの_EXPECTED_SECRETSMANAGER_PRINCIPALSを意図的に
更新しない限りテストが失敗する(将来の同種principalの無自覚な追加を検知する)。

テンプレートの静的検証のみで、AWSへのアクセスは行わない。
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

import yaml

from tests.support.secret_access_registry import SECRETSMANAGER_RUNTIME_FUNCTIONS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

# secretsmanagerのうち、値の取得・書き換え・resource policy操作に相当するaction。
# List/Describe等の metadata 系は対象外(値そのものへの到達力を持つものだけを
# wildcard禁止の対象とする)。ここに列挙した具体的なactionのいずれかへ
# fnmatchするaction文字列(例 "secretsmanager:*"、"secretsmanager:Get*")も
# 併せてsensitiveとみなす(_action_grants_sensitive_access参照)。
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
# ★ Issue #696: この集合(identity policy層)と、#680(resource policy層、
# tests/unit/test_issue_680_secret_resource_policy.pyのGithubApp用
# allow-list)は、共有のregistry(tests/support/secret_access_registry.py)
# から導出する。期待集合の定義はregistryの1箇所だけで、更新は両層へ同時に効く。
# さらに tests/unit/test_issue_696_secret_allowlist_drift.py が、registryに
# 依存せず2つの層を直接突き合わせる。
_EXPECTED_SECRETSMANAGER_PRINCIPALS = set(SECRETSMANAGER_RUNTIME_FUNCTIONS)

# ★ Issue #925(kv1): 鍵の Secret の GetSecretValue だけを持つ 2 関数。github-app secret の
# registry とは『別の Secret』であり、registry へは足さない
# 付与の形(その Secret の ARN 1 つ・GetSecretValue だけ)は下の専用テストで固定する。
# 鍵の Secret のリソースポリシー側の allow-list は test_infra_owner_ref_key_secret.py が固定する
_KEY_SECRET_LOGICAL_ID = "AllocationShadowOwnerRefKeyV1"
_KEY_SECRET_READERS = {"BuyCandidatesFunction", "BuyCandidateWorkerFunction"}
# secretsmanager の sensitive action を持つ関数の全体(github-app の 2 関数 + 鍵の Secret の 2 関数)
_EXPECTED_SENSITIVE_FUNCTIONS = _EXPECTED_SECRETSMANAGER_PRINCIPALS | _KEY_SECRET_READERS


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


def _action_grants_sensitive_access(action: str) -> bool:
    """actionが#133の観点でsensitive(secret値の取得・書き換え・resource
    policy操作に到達しうる)かどうかを判定する。完全一致のほか、
    action文字列自体をglobパターンとして扱い、既知のsensitive actionの
    いずれかにマッチするか(= wildcard actionによる包含)も見る。
    """
    if action == "*":
        return True
    if action in _SENSITIVE_SECRETSMANAGER_ACTIONS:
        return True
    return any(
        fnmatch.fnmatch(sensitive, action) for sensitive in _SENSITIVE_SECRETSMANAGER_ACTIONS
    )


def _policy_entries(function_name: str) -> list[Any]:
    return _resources()[function_name]["Properties"].get("Policies", []) or []


def _sam_policy_template_name(entry: Any) -> str | None:
    """SAM policy template shorthand(bare文字列、または{テンプレート名: params}の
    1キーdict)であれば、そのテンプレート名を返す。raw Statement形式(Statement
    キーを持つdict)であればNoneを返す。
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and "Statement" not in entry and len(entry) == 1:
        return next(iter(entry))
    return None


def _secretsmanager_statements(function_name: str) -> list[dict[str, Any]]:
    """raw Statement形式のPoliciesエントリのうち、sensitiveなsecretsmanager
    actionを含むStatementを返す(wildcard actionを含む)。
    """
    found: list[dict[str, Any]] = []
    for policy in _policy_entries(function_name):
        if not isinstance(policy, dict) or "Statement" not in policy:
            continue
        for statement in policy.get("Statement", []) or []:
            if any(_action_grants_sensitive_access(a) for a in _actions(statement)):
                found.append(statement)
    return found


def _secretsmanager_related_sam_policy_templates(function_name: str) -> list[str]:
    """SAM policy template shorthand(1キーdictまたはbare文字列)のうち、
    テンプレート名にsecretを想起させる文字列を含むものを返す
    (`SecretsManagerReadWrite`・`AWSSecretsManagerGetSecretValuePolicy`等を
    名前ベースで検出する。個々のtemplateのscopeをすべて実装知識として
    モデル化する代わりに、「secret関連の名前を持つtemplateは現時点で
    1つも使われていない」というclosed-world前提を固定する)。
    """
    found: list[str] = []
    for entry in _policy_entries(function_name):
        name = _sam_policy_template_name(entry)
        if name is not None and "secret" in name.lower():
            found.append(name)
    return found


def test_template_defines_exactly_sixteen_lambda_functions() -> None:
    """関数総数を固定する。増減があれば、本module以下のテストの前提
    (全関数を横断確認している、という主張)が崩れていないかを見直す契機にする。
    """
    # 15 + #604 P-b4 A2 の資金入替の観測 P(RotationObservationFunction。Secrets Manager 権限なし)
    assert len(_lambda_function_logical_ids()) == 16


def test_no_lambda_function_uses_a_secretsmanager_related_sam_policy_template() -> None:
    """★ レビュー指摘F1の是正。SAM policy template shorthand(bare文字列・
    1キーdict)経由でsecretsmanager権限を付与している関数が無いことを固定する。
    このshorthand形式はraw Statementを持たないため、_secretsmanager_statements
    (Statementベースの検査)からは原理的に見えない。現時点でこの形式による
    secretsmanager権限付与は0件であるという事実そのものをclosed-worldとして
    固定し、将来追加される場合は本テストの意図的な見直しを要求する。
    """
    violations = {
        name: templates
        for name in _lambda_function_logical_ids()
        if (templates := _secretsmanager_related_sam_policy_templates(name))
    }
    assert violations == {}, f"secret関連のSAM policy templateを検出: {violations}"


def test_only_expected_functions_have_secretsmanager_sensitive_actions() -> None:
    """secretsmanager(値の取得・書き換え・resource policy操作)権限を持つ
    関数の集合が、既知の2関数からいっさい増減していないことを固定する。
    wildcard action(`secretsmanager:*`・`*`)による包含も対象に含む。
    """
    actual = {name for name in _lambda_function_logical_ids() if _secretsmanager_statements(name)}
    assert actual == _EXPECTED_SENSITIVE_FUNCTIONS


def test_no_lambda_function_has_wildcard_resource_for_secretsmanager_actions() -> None:
    """★ 網羅的な反証: 全関数のPoliciesのうち、secretsmanagerのsensitive
    action(wildcard actionによる包含を含む)を持つStatementが1つでも
    Resource="*"を含めば検知する。
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


def test_the_key_secret_readers_can_only_get_that_one_secret() -> None:
    """#925 kv1: 鍵の Secret を読む 2 関数は、その Secret 1 つの GetSecretValue だけを持つ。

    他の secret(github-app 等)・他の操作・ワイルドカードを同じ関数へ足せない。
    """
    for name in sorted(_KEY_SECRET_READERS):
        statements = _secretsmanager_statements(name)
        assert len(statements) == 1, f"{name}: {statements}"
        (statement,) = statements
        assert statement.get("Action") == ["secretsmanager:GetSecretValue"], name
        assert statement.get("Resource") == [{"Fn::Ref": _KEY_SECRET_LOGICAL_ID}], name


def test_the_github_app_secret_readers_are_not_widened_by_the_key_secret() -> None:
    """github-app secret を読む 2 関数(registry)に、鍵の Secret を足していない。"""
    for name in sorted(_EXPECTED_SECRETSMANAGER_PRINCIPALS):
        for statement in _secretsmanager_statements(name):
            assert _KEY_SECRET_LOGICAL_ID not in str(statement.get("Resource")), name
