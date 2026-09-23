"""infra/template.yamlのIAMがIssue #537の最小権限どおりであることの回帰テスト。

EvaluationFunction が WeeklyEvaluationAggregateTable に対して行うのは、評価の保存
Transaction が使う `dynamodb:UpdateItem` だけである(EvaluationResult 自体は
既存の EvaluationResultsTable への DynamoDBCrudPolicy 経由)。

WeeklyReviewFunction が行うのは、週ごとの集計行の取得(Query)・週の状態や一覧の
取得(GetItem)・marker の解消(UpdateItem)だけである。**Scan は付与しない**
(通常の週次レビューが Aggregate 全件を Scan しないことの、IAM 側の担保)。
`PutItem` / `DeleteItem` も付与しない(書き込みは EvaluationFunction 側の Transaction
と、ローカル専用の CLI[Lambda では動かさない]だけが行う)。

テンプレートの静的検証のみで、AWSへのアクセスは行わない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

_EVAL_SID = "UpdateWeeklyEvaluationAggregate"
_REVIEW_SID = "ReadAndResolveWeeklyEvaluationAggregate"
_TABLE_LOGICAL_ID = "WeeklyEvaluationAggregateTable"


def _load_template() -> dict[str, Any]:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda _l, suffix, node: {f"Fn::{suffix}": node.value})
    return yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)


def _resources() -> dict[str, Any]:
    return _load_template()["Resources"]


def _aggregate_statements(function_name: str) -> list[dict[str, Any]]:
    policies = _resources()[function_name]["Properties"].get("Policies", [])
    found: list[dict[str, Any]] = []
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        for statement in policy.get("Statement", []) or []:
            if _TABLE_LOGICAL_ID in str(statement.get("Resource", "")):
                found.append(statement)
    return found


def _actions(statement: dict[str, Any]) -> set[str]:
    action = statement["Action"]
    return {action} if isinstance(action, str) else set(action)


def test_table_is_retained_and_recoverable_like_other_history_tables() -> None:
    table = _resources()[_TABLE_LOGICAL_ID]
    assert table["Type"] == "AWS::DynamoDB::Table"
    assert table["DeletionPolicy"] == "Retain"
    assert table["UpdateReplacePolicy"] == "Retain"
    props = table["Properties"]
    assert props["DeletionProtectionEnabled"] is True
    assert props["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True
    assert "TimeToLiveSpecification" not in props  # raw と同様、TTL は付けない


def test_table_key_schema_is_review_week_and_item_key() -> None:
    props = _resources()[_TABLE_LOGICAL_ID]["Properties"]
    key_names = {k["AttributeName"]: k["KeyType"] for k in props["KeySchema"]}
    assert key_names == {"review_week": "HASH", "item_key": "RANGE"}


def test_evaluation_function_can_only_update_item_on_the_aggregate_table() -> None:
    statements = _aggregate_statements("EvaluationFunction")
    assert len(statements) == 1, "Aggregateへの権限は1つのStatementに集約する"

    statement = statements[0]
    assert statement["Sid"] == _EVAL_SID
    assert statement["Effect"] == "Allow"
    assert _actions(statement) == {"dynamodb:UpdateItem"}
    assert "*" not in str(statement["Resource"]), "ワイルドカードを使わない"


def test_weekly_review_function_cannot_scan_or_write_the_aggregate_table() -> None:
    statements = _aggregate_statements("WeeklyReviewFunction")
    assert len(statements) == 1, "Aggregateへの権限は1つのStatementに集約する"

    statement = statements[0]
    assert statement["Sid"] == _REVIEW_SID
    assert statement["Effect"] == "Allow"
    actions = _actions(statement)
    assert actions == {"dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem"}
    assert "dynamodb:Scan" not in actions, "全件Scanをしないことの担保(AC1)"
    assert "dynamodb:PutItem" not in actions
    assert "dynamodb:DeleteItem" not in actions
    assert "*" not in str(statement["Resource"])


def test_evaluation_function_still_has_full_crud_on_evaluation_results() -> None:
    """既存のEvaluationResultsTableへの権限(#537で変更しない)が残っていることの確認。"""
    policies = _resources()["EvaluationFunction"]["Properties"]["Policies"]
    assert any(
        isinstance(p, dict)
        and p.get("DynamoDBCrudPolicy", {}).get("TableName")
        == {"Fn::Ref": "EvaluationResultsTable"}
        for p in policies
    )


def test_environment_variables_are_wired_and_default_to_disabled() -> None:
    parameters = _load_template()["Parameters"]
    assert parameters["WeeklyAggregateWriteEnabled"]["Default"] == "false"
    assert parameters["WeeklyAggregateReadEnabled"]["Default"] == "false"

    eval_env = _resources()["EvaluationFunction"]["Properties"]["Environment"]["Variables"]
    assert eval_env["WEEKLY_AGGREGATE_WRITE_ENABLED"] == {"Fn::Ref": "WeeklyAggregateWriteEnabled"}
    assert eval_env["WEEKLY_EVALUATION_AGGREGATE_TABLE"] == {"Fn::Ref": _TABLE_LOGICAL_ID}

    review_env = _resources()["WeeklyReviewFunction"]["Properties"]["Environment"]["Variables"]
    assert review_env["WEEKLY_AGGREGATE_READ_ENABLED"] == {"Fn::Ref": "WeeklyAggregateReadEnabled"}
    assert review_env["WEEKLY_EVALUATION_AGGREGATE_TABLE"] == {"Fn::Ref": _TABLE_LOGICAL_ID}
