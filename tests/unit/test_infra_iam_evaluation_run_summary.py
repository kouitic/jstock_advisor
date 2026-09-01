"""infra/template.yamlのIAMがIssue #114 Phase B1の最小権限どおりであることの回帰テスト。

B1でEvaluationFunctionへ追加するのは、run summaryを`jstock-audit_log`へ書き込む
`dynamodb:PutItem`だけである(`AuditLogRepository.save_if_absent()`の条件付き
PutItemしか使わないため)。`DynamoDBCrudPolicy`では
GetItem/Scan/Query/DeleteItem等まで付与されてしまうので使わない。

週次改善レビュー側の読み取り権限は#114 Phase B3で初めて必要になるため、
**B1では追加しない**ことも固定する(最小権限)。

テンプレートの静的検証のみで、AWSへのアクセスは行わない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

_AUDIT_WRITE_SID = "PutEvaluationRunSummaryAudit"


def _load_template() -> dict[str, Any]:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda _l, suffix, node: {f"Fn::{suffix}": node.value})
    return yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)


def _audit_log_statements(function_name: str) -> list[dict[str, Any]]:
    resources = _load_template()["Resources"]
    policies = resources[function_name]["Properties"].get("Policies", [])
    found: list[dict[str, Any]] = []
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        for statement in policy.get("Statement", []) or []:
            if "AuditLogTable" in str(statement.get("Resource", "")):
                found.append(statement)
    return found


def test_evaluation_function_can_write_audit_log() -> None:
    statements = _audit_log_statements("EvaluationFunction")
    assert len(statements) == 1, "AuditLogTableへの権限は1つのStatementに集約する"

    statement = statements[0]
    assert statement["Sid"] == _AUDIT_WRITE_SID
    assert statement["Effect"] == "Allow"
    # 実際に使うのはsave_if_absent()の条件付きPutItemのみ。
    assert statement["Action"] == "dynamodb:PutItem"
    assert "*" not in str(statement["Resource"]), "ワイルドカードを使わない"
    # indexは使わないため index/* を含めない。
    assert "index/*" not in str(statement["Resource"])


def test_evaluation_function_keeps_existing_policies() -> None:
    """既存のRecommendations read / EvaluationResults crudを壊していない。"""
    policies = _load_template()["Resources"]["EvaluationFunction"]["Properties"]["Policies"]
    rendered = str(policies)
    assert "DynamoDBReadPolicy" in rendered
    assert "RecommendationsTable" in rendered
    assert "DynamoDBCrudPolicy" in rendered
    assert "EvaluationResultsTable" in rendered


def test_evaluation_function_does_not_use_crud_policy_for_audit_log() -> None:
    """AuditLogへはCrudPolicy(過剰権限)を使わない。"""
    policies = _load_template()["Resources"]["EvaluationFunction"]["Properties"]["Policies"]
    for policy in policies:
        if isinstance(policy, dict) and "DynamoDBCrudPolicy" in policy:
            assert "AuditLog" not in str(policy["DynamoDBCrudPolicy"])


def test_run_summary_audit_write_is_scoped_to_evaluation_function() -> None:
    """B1のwrite Statementを他のFunctionへ広げていない。"""
    resources = _load_template()["Resources"]
    holders = []
    for name, resource in resources.items():
        if resource.get("Type") != "AWS::Serverless::Function":
            continue
        for policy in resource["Properties"].get("Policies", []) or []:
            if not isinstance(policy, dict):
                continue
            for statement in policy.get("Statement", []) or []:
                if statement.get("Sid") == _AUDIT_WRITE_SID:
                    holders.append(name)
    assert holders == ["EvaluationFunction"]


def test_weekly_review_function_read_access_is_deferred_to_b3() -> None:
    """B1では週次改善レビュー側の読み取り権限を追加しない(最小権限)。

    WeeklyReviewFunctionは以前からAuditLog権限を持つが、B1で
    **新しいStatementを足していない**ことを固定する。
    """
    for statement in _audit_log_statements("WeeklyReviewFunction"):
        assert statement.get("Sid") != _AUDIT_WRITE_SID
