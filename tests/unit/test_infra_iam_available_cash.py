"""infra/template.yamlのAvailableCashTable定義・IAM配線を検証する回帰テスト
(Issue #595、#128 A6a)。

owner単位の買付余力(available_cash)を実際に読み書きするLambda関数は、
現時点でLineWebhookFunctionのみである(#592、LINE会話型UI経由。CLI〔#594〕は
running_on_lambda()が常にFalseのためLambda/IAMを一切必要としない)。他の
定期実行Lambda(BuyCandidatesFunction/HoldingsWatchlistFunction等)は
available_cashに一切触れない。

test_infra_iam_v2_tables.py/test_infra_iam_stock_analysis.pyと同じ手法
(SAM/CloudFormationテンプレートをYAMLとして構造的に読み取り、Policies配下の
!GetAtt/!Refを再帰的に集計する)を使うが、本テストは静的import解析による
到達可能性の追跡は行わない(#592のconversation_service.py→
available_cash_service.pyのimportは本テスト実行時点のmainブランチには
まだ反映されていない場合があり、テンプレート側の配線自体が正しいかのみを
独立して検証する。#592マージ後の到達可能性の回帰検証はtest_infra_iam_v2_
tables.py型のテストを別途追加することを妨げない)。

実装コードの実行・AWSへのアクセスは一切行わない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

_TABLE_LOGICAL_ID = "AvailableCashTable"
_AUTHORIZED_FUNCTION = "LineWebhookFunction"
# Issue #602(#128 R1。USER 承認 = #122 issuecomment-6138462304【4】O1 案): 購入側 Shadow(Q')が
# 買付余力を読む 2 関数。GetItem だけ(書込・列挙・index なし)
_SHADOW_READ_FUNCTIONS = ("BuyCandidatesFunction", "BuyCandidateWorkerFunction")
_SHADOW_READ_SID = "AvailableCashReadOnly"


class _CfnLoader(yaml.SafeLoader):
    """CloudFormationの短縮形組み込み関数(!GetAtt/!Ref/!Sub等)を、
    {"GetAtt": "..."}等の素朴なdictへ変換するだけの最小限のYAML Loader。
    値の意味解決(実際のARN計算等)は行わない、構文解析専用。
    """


def _cfn_multi_constructor(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return {tag_suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {tag_suffix: loader.construct_sequence(node)}
    assert isinstance(node, yaml.MappingNode)  # noqa: S101 - CFNタグはこの3種のみ
    return {tag_suffix: loader.construct_mapping(node)}


_CfnLoader.add_multi_constructor("!", _cfn_multi_constructor)  # type: ignore[no-untyped-call]


def _load_template() -> dict[str, Any]:
    loaded = yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_CfnLoader)
    assert isinstance(loaded, dict)
    return loaded


def _collect_referenced_logical_ids(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        if "GetAtt" in node and isinstance(node["GetAtt"], str):
            found.add(node["GetAtt"].split(".", 1)[0])
        if "Ref" in node and isinstance(node["Ref"], str):
            found.add(node["Ref"])
        for value in node.values():
            found |= _collect_referenced_logical_ids(value)
    elif isinstance(node, list):
        for item in node:
            found |= _collect_referenced_logical_ids(item)
    return found


def _collect_actions_for_resource(policies: list[Any], table_logical_id: str) -> set[str]:
    """table_logical_idをResourceに含むStatementのActionを集計する。"""
    actions: set[str] = set()
    for policy in policies:
        if not isinstance(policy, dict) or "Statement" not in policy:
            continue
        for statement in policy["Statement"]:
            resource = statement.get("Resource")
            resources = resource if isinstance(resource, list) else [resource]
            if any(table_logical_id in _collect_referenced_logical_ids(r) for r in resources):
                action = statement.get("Action")
                actions |= set(action) if isinstance(action, list) else {action}
    return actions


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    return _load_template()


def test_available_cash_table_is_defined_with_owner_key(template: dict[str, Any]) -> None:
    table = template["Resources"][_TABLE_LOGICAL_ID]
    assert table["Type"] == "AWS::DynamoDB::Table"
    props = table["Properties"]
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    key_schema = props["KeySchema"]
    assert len(key_schema) == 1
    assert key_schema[0]["AttributeName"] == "owner"
    assert key_schema[0]["KeyType"] == "HASH"


def test_available_cash_table_has_same_data_protection_as_holding_decision_runtime_config(
    template: dict[str, Any],
) -> None:
    """Issue #137(失うと再生成できないデータの保護)と同水準であること。

    利用者の実際の資金管理データであり、HoldingDecisionRuntimeConfigTableと
    同じ保護(Retain・PITR・DeletionProtection)を要求する。
    """
    table = template["Resources"][_TABLE_LOGICAL_ID]
    assert table.get("DeletionPolicy") == "Retain"
    assert table.get("UpdateReplacePolicy") == "Retain"
    props = table["Properties"]
    assert props["DeletionProtectionEnabled"] is True
    assert props["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True


def _functions_referencing_the_table(template: dict[str, Any]) -> list[str]:
    granted = []
    for logical_id, resource in template["Resources"].items():
        if resource.get("Type") != "AWS::Serverless::Function":
            continue
        policies = resource["Properties"].get("Policies", [])
        if _TABLE_LOGICAL_ID in _collect_referenced_logical_ids(policies):
            granted.append(logical_id)
    return granted


def test_only_the_three_intended_functions_are_granted_available_cash_table_access(
    template: dict[str, Any],
) -> None:
    """AvailableCashを実際に扱うのは、LineWebhookFunction(読み書き。#592)と、購入側 Shadow が
    読む BuyCandidatesFunction / BuyCandidateWorkerFunction(GetItem のみ。#602 O1)の 3 つだけ。
    CLI(#594)はLambda不要、他の定期実行Lambdaは一切触れないため、最小権限の原則として
    これ以外への付与を禁止する。
    """
    granted = sorted(_functions_referencing_the_table(template))
    assert granted == sorted([_AUTHORIZED_FUNCTION, *_SHADOW_READ_FUNCTIONS]), (
        f"AvailableCashTableへのIAM権限が想定外のFunctionへ付与されている: {granted}"
    )


def test_available_cash_table_grants_only_get_and_put_item(template: dict[str, Any]) -> None:
    """サブちゃんレビュー指摘(#617)の固定化: action粒度を最小限に保つ。

    DeleteItem(既存レコードの削除で「未登録」契約を壊せる)・Scan(全owner
    列挙)等、実際には使わないアクションを混入させない。call graph実測
    (#592実装のAvailableCashService/conversation_commit.py)により、必要
    なのはGetItem(get/get_raw)とPutItem(conditional_put_transact_item()が
    生成するConditionExpression付きPut)の2つのみ(ConditionCheckは別途の
    ConditionCheck transact item経由でのみ必要になるが、AvailableCashTable
    に対してはそれを行わないためConditionCheckItemも不要)。
    """
    function = template["Resources"][_AUTHORIZED_FUNCTION]
    policies = function["Properties"]["Policies"]
    actions = _collect_actions_for_resource(policies, _TABLE_LOGICAL_ID)
    assert actions == {"dynamodb:GetItem", "dynamodb:PutItem"}, (
        f"AvailableCashTableへ付与されたactionが最小権限でない: {actions}"
    )


# ---- #602 O1: 購入側 Shadow の買付余力の読取(GetItem のみ)----------------------------------


@pytest.mark.parametrize("function_id", _SHADOW_READ_FUNCTIONS)
def test_the_shadow_functions_can_only_get_item_on_the_available_cash_table(
    template: dict[str, Any], function_id: str
) -> None:
    policies = template["Resources"][function_id]["Properties"]["Policies"]
    actions = _collect_actions_for_resource(policies, _TABLE_LOGICAL_ID)
    # PutItem・UpdateItem・DeleteItem・Scan・Query・BatchGetItem・ConditionCheckItem を混入させない
    assert actions == {"dynamodb:GetItem"}, f"{function_id}: {actions}"


@pytest.mark.parametrize("function_id", _SHADOW_READ_FUNCTIONS)
def test_the_shadow_read_grant_is_a_dedicated_exact_statement(
    template: dict[str, Any], function_id: str
) -> None:
    policies = template["Resources"][function_id]["Properties"]["Policies"]
    statements = [
        statement
        for policy in policies
        if isinstance(policy, dict) and "Statement" in policy
        for statement in policy["Statement"]
        if _TABLE_LOGICAL_ID in _collect_referenced_logical_ids(statement.get("Resource"))
    ]
    # 共有 Statement(DynamoDbCrudAccess)へ混ぜず、専用の 1 つだけ
    assert len(statements) == 1, f"{function_id}: {len(statements)} statements"
    (statement,) = statements
    assert statement["Sid"] == _SHADOW_READ_SID
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == ["dynamodb:GetItem"]
    # Resource は表の ARN だけ(GSI の /index/* ・ワイルドカード・他の表を付けない)
    assert statement["Resource"] == [{"GetAtt": f"{_TABLE_LOGICAL_ID}.Arn"}]


@pytest.mark.parametrize("function_id", _SHADOW_READ_FUNCTIONS)
def test_the_shared_crud_statement_of_the_shadow_functions_does_not_include_the_table(
    template: dict[str, Any], function_id: str
) -> None:
    policies = template["Resources"][function_id]["Properties"]["Policies"]
    for policy in policies:
        if not isinstance(policy, dict) or "Statement" not in policy:
            continue
        for statement in policy["Statement"]:
            if statement.get("Sid") == "DynamoDbCrudAccess":
                assert _TABLE_LOGICAL_ID not in _collect_referenced_logical_ids(
                    statement.get("Resource")
                )


def test_the_line_webhook_grant_is_not_weakened_or_widened(template: dict[str, Any]) -> None:
    policies = template["Resources"][_AUTHORIZED_FUNCTION]["Properties"]["Policies"]
    actions = _collect_actions_for_resource(policies, _TABLE_LOGICAL_ID)
    assert actions == {"dynamodb:GetItem", "dynamodb:PutItem"}


def test_the_shadow_read_grant_adds_no_environment_variable_or_parameter(
    template: dict[str, Any],
) -> None:
    # Q' は表名を既存の DYNAMODB_TABLE_PREFIX(Globals)から解決する。新しい環境変数・Parameter なし
    for function_id in _SHADOW_READ_FUNCTIONS:
        env = template["Resources"][function_id]["Properties"].get("Environment", {})
        assert _TABLE_LOGICAL_ID not in _collect_referenced_logical_ids(env)
    assert not [
        name for name in template.get("Parameters", {}) if "AvailableCash" in name or "Cash" in name
    ]


def test_the_inline_policy_statements_of_the_shadow_functions_stay_well_under_the_iam_limit(
    template: dict[str, Any],
) -> None:
    """IAM の inline policy は 1 role あたり 10,240 文字が上限。SAM のポリシーテンプレートの展開後の
    実サイズは template からは測れない(AWS 上の実測は IAM の読取権限が要る)ため、明示した
    Statement だけの保守的な目安(JSON の長さ)で、上限に近づいていないことの警報にする。
    ChangeSet の CREATE 時に CloudFormation が上限超過を検出する(= 最終の防壁)。
    """
    import json

    for function_id in _SHADOW_READ_FUNCTIONS:
        policies = template["Resources"][function_id]["Properties"]["Policies"]
        explicit = [p for p in policies if isinstance(p, dict) and "Statement" in p]
        size = len(json.dumps(explicit, ensure_ascii=False, separators=(",", ":")))
        assert size < 6000, f"{function_id}: explicit statements = {size} chars"
