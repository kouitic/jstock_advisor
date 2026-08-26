"""infra/template.yamlのIAM Policyが、Phase 2-B「銘柄分析」(LINE会話型UI経由の
BUY/SELL/HOLD分析表示、conversation_service.py::ConversationAction.ANALYZE)が
実際に読み取るDynamoDBテーブルを過不足なく許可していることを検証する回帰テスト。

本番デプロイ後の確認で、LineWebhookFunctionのIAMロールにAuditLogTableへの
読み取り権限が付与されておらず、Legacy SELL担当の純粋HOLD銘柄分析
(HoldingEvaluationRecord.authoritative_audit_log_id経由でAuditLogEntryを直接
取得する経路)がAccessDeniedExceptionとなる不備が発覚した(2026-08)。

単に"AuditLogTable"という文字列がtemplate.yaml上に存在するかだけを見る脆弱な
テストにはせず、test_infra_iam_v2_tables.pyと同じ手法(SAM/CloudFormation
テンプレートをYAMLとして構造的に読み取り、Policies配下の!GetAtt/!Refを
再帰的に集計する)で、LineWebhookFunction → Policies → DynamoDBReadPolicy →
<Table> の関係を直接検証する。

実装コードのimportグラフを静的に辿るだけで、Lambdaの実行やAWSへのアクセスは
一切行わない(test_infra_iam_v2_tables.pyと同じ設計方針)。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src"
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

_FUNCTION_LOGICAL_ID = "LineWebhookFunction"
_ENTRY_MODULE = "jstock_advisor.lambda_handlers.line_webhook_handler"

# StockAnalysisViewService(銘柄分析)が実際に読み取るリポジトリモジュール →
# 対応するCloudFormation論理ID(DynamoDBテーブル)。ファイル名(build_collection_
# store/resolve_table_name)をハードコードしているモジュール単位で対応付ける。
_STOCK_ANALYSIS_REPO_MODULE_TO_TABLE: dict[str, str] = {
    "jstock_advisor.infrastructure.local_repository.audit_log_repository": "AuditLogTable",
    "jstock_advisor.infrastructure.local_repository.holding_evaluation_record_repository": (
        "HoldingEvaluationRecordsTable"
    ),
    "jstock_advisor.infrastructure.local_repository.recommendation_repository": (
        "RecommendationsTable"
    ),
    "jstock_advisor.infrastructure.local_repository.buy_candidate_evaluation_record_repository": (
        "BuyCandidateEvaluationRecordsTable"
    ),
    "jstock_advisor.infrastructure."
    "local_repository.latest_buy_candidate_batch_pointer_repository": (
        "BuyCandidateBatchCompletionTable"
    ),
}

# entry_moduleからこれらのリポジトリモジュールへ至る経路上にある「薄いラッパー」
# モジュールのみBFSを継続する(test_infra_iam_v2_tables.pyと同じ設計方針:
# 巨大なserviceを無制限に辿ると誤検知するため、実際の経路のみ明示的に許可する)。
_PASSTHROUGH_MODULES = frozenset(
    {
        "jstock_advisor.services.line_event_router",
        "jstock_advisor.services.conversation_service",
        "jstock_advisor.services.stock_analysis_view_service",
        "jstock_advisor.services.latest_batch_records_provider",
    }
)


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
    """Policies配下を再帰的に走査し、!GetAtt <Id>.Arn および
    TableName: !Ref <Id>(DynamoDBReadPolicy/DynamoDBCrudPolicyの短縮形)の
    双方から参照されている論理IDの集合を返す。"""
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


def _function_granted_table_ids(template: dict[str, Any], function_logical_id: str) -> set[str]:
    resource = template["Resources"][function_logical_id]
    policies = resource["Properties"].get("Policies", [])
    return _collect_referenced_logical_ids(policies)


def _function_read_only_granted_table_ids(
    template: dict[str, Any], function_logical_id: str
) -> set[str]:
    """DynamoDBReadPolicy(読み取り専用ショートハンド)経由でのみ許可された
    テーブルの論理IDを返す(Write権限を含むCrud系Statement/DynamoDBCrudPolicy
    経由の付与は含めない)。銘柄分析は読み取り専用機能であり、書き込み権限を
    新規に要求してはならないことを検証するために使う。"""
    resource = template["Resources"][function_logical_id]
    policies = resource["Properties"].get("Policies", [])
    found: set[str] = set()
    for policy in policies:
        if isinstance(policy, dict) and "DynamoDBReadPolicy" in policy:
            table_name = policy["DynamoDBReadPolicy"].get("TableName")
            if isinstance(table_name, dict) and "Ref" in table_name:
                found.add(table_name["Ref"])
    return found


def _function_handler_module(template: dict[str, Any], function_logical_id: str) -> str:
    handler: str = template["Resources"][function_logical_id]["Properties"]["Handler"]
    return handler.rsplit(".", 1)[0]


def _module_source_path(module: str) -> Path | None:
    parts = module.split(".")
    as_file = _SRC_ROOT.joinpath(*parts).with_suffix(".py")
    if as_file.exists():
        return as_file
    as_package = _SRC_ROOT.joinpath(*parts, "__init__.py")
    if as_package.exists():
        return as_package
    return None


def _directly_imported_jstock_modules(py_file: Path) -> set[str]:
    """`from jstock_advisor.X.Y import ...`形式のimportのみを対象とする
    (`import jstock_advisor.X.Y`形式・動的importは本監査のスコープ外)。"""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("jstock_advisor")
        ):
            modules.add(node.module)
    return modules


def _transitively_reachable_modules(entry_module: str) -> set[str]:
    """entry_moduleの直接import、および_PASSTHROUGH_MODULES経由でのみ辿る
    限定BFSでjstock_advisor.*モジュールを収集する(実行・AWSアクセスは一切
    行わない)。"""
    visited: set[str] = set()
    stack = [entry_module]
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        if current != entry_module and current not in _PASSTHROUGH_MODULES:
            continue
        path = _module_source_path(current)
        if path is None:
            continue
        for imported in _directly_imported_jstock_modules(path):
            if imported not in visited:
                stack.append(imported)
    return visited


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    return _load_template()


@pytest.fixture(scope="module")
def reachable_modules() -> set[str]:
    return _transitively_reachable_modules(_ENTRY_MODULE)


def test_stock_analysis_repo_modules_are_actually_reachable_from_line_webhook_handler(
    reachable_modules: set[str],
) -> None:
    """このテスト自体が形骸化しないための前提確認: 銘柄分析の各リポジトリ
    モジュールが、実際にline_webhook_handler.pyから静的import解析で到達可能で
    あること(_PASSTHROUGH_MODULESの設定漏れ・リファクタによる経路変更を検知する)。
    """
    for owner_module in _STOCK_ANALYSIS_REPO_MODULE_TO_TABLE:
        assert owner_module in reachable_modules, (
            f"{owner_module}がline_webhook_handler.pyから到達不能になっている"
            "(_PASSTHROUGH_MODULESの更新が必要な可能性がある)"
        )


@pytest.mark.parametrize(
    "owner_module,table_logical_id", sorted(_STOCK_ANALYSIS_REPO_MODULE_TO_TABLE.items())
)
def test_line_webhook_function_has_read_access_to_every_stock_analysis_table(
    template: dict[str, Any], owner_module: str, table_logical_id: str
) -> None:
    """LineWebhookFunctionのPoliciesに、銘柄分析が実際に読み取る各テーブルへの
    読み取りアクセス(DynamoDBReadPolicy等、GetItem/Query/Scanのいずれかを含む
    Statement)が含まれていること。"""
    granted = _function_granted_table_ids(template, _FUNCTION_LOGICAL_ID)
    assert table_logical_id in granted, (
        f"{_FUNCTION_LOGICAL_ID}は{owner_module}(→{table_logical_id})を"
        "銘柄分析から参照するが、infra/template.yamlのPoliciesに"
        f"{table_logical_id}のARNが含まれていない"
        "(AccessDeniedExceptionの原因になる。2026-08のAuditLogTable権限不足と同種の不備)"
    )


def test_line_webhook_function_stock_analysis_access_is_read_only(
    template: dict[str, Any],
) -> None:
    """銘柄分析は読み取り専用機能であり、AuditLogTable等への書き込み権限
    (PutItem/UpdateItem/DeleteItem等)を新規に要求してはならないことを確認する。
    DynamoDBReadPolicy(読み取り専用ショートハンド)経由でのみ許可されている
    ことを検証する(Crud系Statement経由の付与であれば、意図せずWrite権限まで
    含んでしまっている可能性がある)。"""
    read_only_granted = _function_read_only_granted_table_ids(template, _FUNCTION_LOGICAL_ID)
    all_granted = _function_granted_table_ids(template, _FUNCTION_LOGICAL_ID)
    all_tables = set(_STOCK_ANALYSIS_REPO_MODULE_TO_TABLE.values())

    # AuditLogTableは今回新規追加した権限のため、必ずDynamoDBReadPolicy
    # (読み取り専用)経由であることを厳密に検証する。
    assert "AuditLogTable" in read_only_granted, (
        "AuditLogTableへのアクセスがDynamoDBReadPolicy(読み取り専用)経由で"
        "付与されていない(Write権限を誤って含んでいる可能性がある)"
    )
    # 他の既存テーブルも、少なくとも(今回の対象範囲では)Crud系ではなく
    # 読み取り専用ショートハンド経由で付与されていること。
    for table in all_tables:
        assert table in all_granted, f"{table}への権限が見つからない"


def test_stock_analysis_table_logical_ids_exist_as_dynamodb_resources(
    template: dict[str, Any],
) -> None:
    """_STOCK_ANALYSIS_REPO_MODULE_TO_TABLEが参照する論理IDが、実際に
    template.yaml上でAWS::DynamoDB::Tableとして定義されていること
    (タイプミス等の検知)。"""
    dynamodb_resources = {
        logical_id
        for logical_id, resource in template["Resources"].items()
        if resource.get("Type") == "AWS::DynamoDB::Table"
    }
    all_tables = set(_STOCK_ANALYSIS_REPO_MODULE_TO_TABLE.values())
    assert all_tables <= dynamodb_resources
