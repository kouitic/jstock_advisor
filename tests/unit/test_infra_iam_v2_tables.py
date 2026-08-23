"""infra/template.yamlのIAM PolicyとM3(保有銘柄オーナー機能)のV2テーブル切替が
整合していることを検証する回帰テスト。

M3/M3.1本番デプロイ後のM4検証で、HoldingsWatchlistFunction/BuyCandidatesFunction
等がholdings_v2等のV2テーブルを実際にはコード上で参照しているにもかかわらず、
infra/template.yaml側のIAM Policy(Resource配列)がV1テーブルのままだったため
AccessDeniedExceptionでLambdaが停止する不具合が発覚した(2026-08)。本テストは、
V2テーブルの読み書きを行うrepository/serviceモジュールを(直接・間接importで)
参照するLambda Functionには、対応するV2テーブルのCloudFormation論理ID
(!GetAtt <Table>.Arn)がPoliciesへ必ず含まれていることを検証する。

逆方向(参照しないFunctionへV2権限を過剰に付与していないこと)もあわせて
検証する(レビュー指摘: 「念のため全部のFunctionに全部のV2テーブル権限を
与える」方式は禁止、必要なFunction×必要なテーブルだけを付与すること)。

実装コード(import文)から静的にimportグラフをたどるだけで、Lambdaの実行や
AWSへのアクセスは一切行わない。
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

# V2テーブルのファイル名(build_collection_store/resolve_table_name)を直接
# ハードコードしているモジュール → 対応するCloudFormation論理ID。
# holdings_snapshot_repository.pyはNORMAL/VALIDATION両方のV2テーブルを
# for_execution_context()で切り替えるため、両方を要求する。
_V2_OWNER_MODULE_TO_TABLES: dict[str, tuple[str, ...]] = {
    "jstock_advisor.infrastructure.local_repository.holding_repository": ("HoldingsTableV2",),
    "jstock_advisor.infrastructure.local_repository.holdings_snapshot_repository": (
        "HoldingsSnapshotTableV2",
        "ValidationHoldingsSnapshotTableV2",
    ),
    "jstock_advisor.infrastructure.aws.baseline_sequence": (
        "InvestmentThesisBaselineSequencesTableV2",
    ),
    "jstock_advisor.infrastructure.aws.baseline_pointer": (
        "InvestmentThesisBaselinePointersTableV2",
    ),
}

# 全Lambda Function(AWS::Serverless::Function論理ID)一覧。テンプレートに
# 新しいFunctionが追加されたのにこのテストが追随し忘れることを防ぐため、
# 実際のリソース一覧との差分をtest_all_lambda_functions_are_covered_by_auditで検証する。
_ALL_FUNCTIONS = frozenset(
    {
        "BuyCandidatesFunction",
        "DisclosureCheckFunction",
        "EvaluationFunction",
        "HoldingsWatchlistFunction",
        "LineWebhookFunction",
        "MonthlyReviewFunction",
        "QuarterlyReviewFunction",
        "WatchlistBatchReconcilerFunction",
        "WatchlistDispatcherFunction",
        "WatchlistTerminalFailureHandlerFunction",
        "WatchlistWorkerFunction",
        "WeeklyReviewFunction",
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


def _function_handler_module(template: dict[str, Any], function_logical_id: str) -> str:
    handler: str = template["Resources"][function_logical_id]["Properties"]["Handler"]
    # "jstock_advisor.lambda_handlers.holdings_watchlist_handler.handler" →
    # モジュール部分だけ("...handler_handler"の末尾の関数名を除く)。
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


#  handler moduleがこれらの「薄いラッパー」serviceを直接importしている場合のみ、
# さらに一段先(そのservice自身が何をimportしているか)まで辿る。LineNotification
# Service/BuySignalService/SellSignalService等の「多目的・巨大な」serviceを
# 経由してBFSを無制限に広げると、実際には呼ばれないメソッド由来のimportまで
# 誤って「参照している」と判定してしまう(例: watchlist_worker_handler.pyは
# 通知送信のためだけにLineNotificationServiceをimportしており、同サービスが
# 内部でHoldingsSnapshotRepositoryを使うcheck_trade_cooldown_eligibility()を
# 呼び出すことはない)。したがって、V2テーブルへの依存を宣言する薄いラッパー
# serviceに限定してBFSを継続する(過検知を防ぐための意図的な設計)。
_PASSTHROUGH_MODULES = frozenset(
    {
        "jstock_advisor.services.portfolio_service",
        "jstock_advisor.services.trade_cooldown_service",
        "jstock_advisor.services.investment_thesis_service",
        "jstock_advisor.services.holding_decision_service",
        "jstock_advisor.services.conversation_service",
        "jstock_advisor.services.disclosure_check_service",
        "jstock_advisor.services.line_event_router",
        "jstock_advisor.services.watchlist_candidate_collector",
    }
)


def _transitively_reachable_modules(entry_module: str) -> set[str]:
    """entry_moduleの直接import、および_PASSTHROUGH_MODULES経由でのみ辿る
    限定BFSでjstock_advisor.*モジュールを収集する(実行・AWSアクセスは一切
    行わない)。entry_module自身は無条件に展開するが、そこから先は
    _PASSTHROUGH_MODULESに含まれるモジュールのみさらに展開する
    (詳細は_PASSTHROUGH_MODULESのコメント参照)。"""
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


def test_all_lambda_functions_are_covered_by_audit(template: dict[str, Any]) -> None:
    """テンプレートの実際のAWS::Serverless::Function一覧が_ALL_FUNCTIONSと
    一致すること(新しいFunctionが追加されたらこのテストファイルの更新が
    必要であることに気づけるようにする)。"""
    actual = {
        logical_id
        for logical_id, resource in template["Resources"].items()
        if resource.get("Type") == "AWS::Serverless::Function"
    }
    assert actual == _ALL_FUNCTIONS


@pytest.mark.parametrize("function_logical_id", sorted(_ALL_FUNCTIONS))
def test_function_has_iam_access_to_every_v2_table_it_transitively_uses(
    template: dict[str, Any], function_logical_id: str
) -> None:
    """このFunctionのハンドラから静的import解析で到達可能なモジュールに、
    V2テーブルをファイル名でハードコードしているrepository/serviceモジュールが
    含まれる場合、対応するV2テーブルの論理IDがPoliciesへ必ず含まれること。"""
    handler_module = _function_handler_module(template, function_logical_id)
    reachable = _transitively_reachable_modules(handler_module)
    granted = _function_granted_table_ids(template, function_logical_id)

    for owner_module, required_tables in _V2_OWNER_MODULE_TO_TABLES.items():
        if owner_module not in reachable:
            continue
        for table in required_tables:
            assert table in granted, (
                f"{function_logical_id}は{owner_module}(→{table})を参照しているが、"
                f"infra/template.yamlのPoliciesに{table}のARNが含まれていない"
                "(AccessDeniedExceptionの原因になる)"
            )


@pytest.mark.parametrize("function_logical_id", sorted(_ALL_FUNCTIONS))
def test_function_does_not_have_unnecessary_v2_table_access(
    template: dict[str, Any], function_logical_id: str
) -> None:
    """逆方向: このFunctionが実際には参照していないV2テーブルの権限を
    「念のため」付与していないこと(最小権限の原則、レビュー指摘)。"""
    handler_module = _function_handler_module(template, function_logical_id)
    reachable = _transitively_reachable_modules(handler_module)
    granted = _function_granted_table_ids(template, function_logical_id)

    required_tables: set[str] = set()
    for owner_module, tables in _V2_OWNER_MODULE_TO_TABLES.items():
        if owner_module in reachable:
            required_tables.update(tables)

    all_v2_tables = {t for tables in _V2_OWNER_MODULE_TO_TABLES.values() for t in tables}
    unnecessary = (granted & all_v2_tables) - required_tables
    assert not unnecessary, (
        f"{function_logical_id}はコード上参照していないV2テーブル{sorted(unnecessary)}への"
        "IAM権限を持っている(最小権限の原則に反する、過剰付与)"
    )


def test_v2_table_logical_ids_exist_as_dynamodb_resources(template: dict[str, Any]) -> None:
    """_V2_OWNER_MODULE_TO_TABLESが参照する論理IDが、実際にtemplate.yaml上で
    AWS::DynamoDB::Tableとして定義されていること(タイプミス等の検知)。"""
    dynamodb_resources = {
        logical_id
        for logical_id, resource in template["Resources"].items()
        if resource.get("Type") == "AWS::DynamoDB::Table"
    }
    all_v2_tables = {t for tables in _V2_OWNER_MODULE_TO_TABLES.values() for t in tables}
    assert all_v2_tables <= dynamodb_resources
