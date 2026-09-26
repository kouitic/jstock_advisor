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


def test_only_line_webhook_function_is_granted_available_cash_table_access(
    template: dict[str, Any],
) -> None:
    """AvailableCashを実際に読み書きするのはLineWebhookFunctionのみ
    (#592)。CLI(#594)はLambda不要、他の定期実行Lambdaは一切触れないため、
    最小権限の原則としてLineWebhookFunction以外への付与を禁止する。
    """
    granted_functions = []
    for logical_id, resource in template["Resources"].items():
        if resource.get("Type") != "AWS::Serverless::Function":
            continue
        policies = resource["Properties"].get("Policies", [])
        if _TABLE_LOGICAL_ID in _collect_referenced_logical_ids(policies):
            granted_functions.append(logical_id)

    assert granted_functions == [_AUTHORIZED_FUNCTION], (
        f"AvailableCashTableへのIAM権限が想定外のFunctionへ付与されている: "
        f"{granted_functions}(想定は{_AUTHORIZED_FUNCTION}のみ)"
    )
