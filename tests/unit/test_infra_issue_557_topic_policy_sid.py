"""infra/template.yamlの複数StatementポリシーがすべてSidを持つことの回帰テスト
(Issue #557)。

Release W9のChangeCommandSet EXECUTEで、IncidentNotificationTopicPolicyの2
Statementに一意なSidが無く、SNSのSetTopicAttributes APIが
"Invalid parameter: Every policy statement must have a unique ID"で失敗した
(HandlerErrorCode: InvalidRequest)。この制約はCloudFormationテンプレートの
YAML検証(sam validate --lint)では検出できず、実際にAWS APIへ到達して初めて
顕在化する。

本テストは対象resourceを個別に固定するだけでなく、テンプレート全体を走査して
「複数Statementを持つPolicyDocumentは全StatementがSidを持ち、かつそのSidが
同一PolicyDocument内で一意である」ことを汎用的に固定する。#557と同型の
defect(将来別のresourceへ複数StatementのPolicyDocumentが追加され、Sidを
付け忘れるケース)を、resourceの種類やLogicalIdに関わらず検知するため。

テンプレートの静的検証のみで、AWSへのアクセスは行わない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

_TOPIC_POLICY_LOGICAL_ID = "IncidentNotificationTopicPolicy"


def _load_template() -> dict[str, Any]:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda _l, suffix, node: {f"Fn::{suffix}": node.value})
    return yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)


def _resources() -> dict[str, Any]:
    return _load_template()["Resources"]


def _iter_policy_documents(resources: dict[str, Any]) -> list[tuple[str, str, list[Any]]]:
    """(logical_id, 出現箇所のラベル, Statementリスト)のタプルを、テンプレート内の
    全PolicyDocumentから収集する(TopicPolicy/QueuePolicy/Role.Policiesの
    PolicyDocument等、形は問わない)。
    """
    found: list[tuple[str, str, list[Any]]] = []

    def _walk(node: Any, logical_id: str, path: str) -> None:
        if isinstance(node, dict):
            if "PolicyDocument" in node and isinstance(node["PolicyDocument"], dict):
                statement = node["PolicyDocument"].get("Statement")
                if isinstance(statement, list):
                    found.append((logical_id, f"{path}.PolicyDocument", statement))
            for key, value in node.items():
                _walk(value, logical_id, f"{path}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                _walk(item, logical_id, f"{path}[{index}]")

    for logical_id, resource in resources.items():
        _walk(resource.get("Properties", {}), logical_id, "Properties")

    return found


def test_incident_notification_topic_policy_statements_have_unique_sids() -> None:
    resources = _resources()
    statement = resources[_TOPIC_POLICY_LOGICAL_ID]["Properties"]["PolicyDocument"]["Statement"]
    assert len(statement) == 2, "Issue #557当時の2 Statement構成を前提とする"

    sids = [entry.get("Sid") for entry in statement]
    assert all(sid is not None for sid in sids), (
        f"{_TOPIC_POLICY_LOGICAL_ID}の全StatementにSidが必要(Issue #557)。実際: {sids}"
    )
    assert len(sids) == len(set(sids)), f"Sidが重複している: {sids}"

    assert sids == [
        "AllowCloudWatchAlarmPublish",
        "AllowWatchlistBatchReconcilerPublish",
    ]


def test_all_multi_statement_policy_documents_in_template_have_unique_sids() -> None:
    """★ Issue #557と同型のdefectを、resourceの種類・LogicalIdに関わらず汎用的に
    検知する(レビューでの反証: 将来別のresourceへ複数StatementのPolicyDocumentが
    追加され、Sidを付け忘れた場合でも、本テストが個別修正なしに検知できる)。

    Statementが1件のみのPolicyDocument(Sid省略がAWS API上も問題にならない)は
    対象外とする。
    """
    resources = _resources()
    violations: list[str] = []

    for logical_id, path, statement in _iter_policy_documents(resources):
        if len(statement) < 2:
            continue

        sids = [entry.get("Sid") for entry in statement]
        if any(sid is None for sid in sids):
            violations.append(f"{logical_id} ({path}): Sid未設定のStatementがある(Sid一覧: {sids})")
        elif len(sids) != len(set(sids)):
            violations.append(f"{logical_id} ({path}): Sidが重複している(Sid一覧: {sids})")

    assert not violations, (
        "複数StatementのPolicyDocumentで、Sid欠落または重複が検出された"
        "(SNS/SQS等のAPIはSid一意性を要求し、テンプレートのYAML検証だけでは"
        "検出できない。Issue #557参照):\n" + "\n".join(violations)
    )
