"""infra/template.yamlの複数StatementポリシーがすべてSidを持つことの回帰テスト
(Issue #557)。

Release W9のChangeCommandSet EXECUTEで、IncidentNotificationTopicPolicyの2
Statementに一意なSidが無く、SNSのSetTopicAttributes APIが
"Invalid parameter: Every policy statement must have a unique ID"で失敗した
(HandlerErrorCode: InvalidRequest)。この制約はCloudFormationテンプレートの
YAML検証(sam validate --lint)では検出できず、実際にAWS APIへ到達して初めて
顕在化する。

本テストは対象resourceを個別に固定する専用テストに加え、
`AWS::SNS::TopicPolicy`というresource type全体を走査して「複数Statementを
持つPolicyDocumentは全StatementがSidを持ち、かつそのSidが同一
PolicyDocument内で一意である」ことを固定する横断テストを持つ(#557
issuecomment MEDIUM-1レビュー対応)。

この契約はSNS TopicPolicy固有のもの(SNSのSetTopicAttributes APIがSid一意性
を要求する)であり、AWS::S3::BucketPolicyやIAM inline policy、SQS
QueuePolicy等、他のPolicyDocumentへは適用されない(それらがSidなしの複数
Statementを許容するかどうかはサービスごとに異なり、本Issueでは検証していない)。
そのため横断テストの対象はAWS::SNS::TopicPolicyに限定する。

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


def _sns_topic_policy_statements(resources: dict[str, Any]) -> list[tuple[str, list[Any]]]:
    """(logical_id, Statementリスト)のタプルを、`AWS::SNS::TopicPolicy` resourceの
    `Properties.PolicyDocument.Statement`からのみ収集する。

    横断テストの対象をSNS TopicPolicyに限定するため、他のresource type
    (AWS::S3::BucketPolicy、IAM inline policy、AWS::SQS::QueuePolicy等)は
    意図的に対象外とする(#557 issuecomment MEDIUM-1)。
    """
    found: list[tuple[str, list[Any]]] = []
    for logical_id, resource in resources.items():
        if resource.get("Type") != "AWS::SNS::TopicPolicy":
            continue
        statement = resource.get("Properties", {}).get("PolicyDocument", {}).get("Statement")
        if isinstance(statement, list):
            found.append((logical_id, statement))
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


def test_all_sns_topic_policy_statements_have_unique_sids() -> None:
    """★ Issue #557と同型のdefectを、`AWS::SNS::TopicPolicy` resourceであれば
    LogicalIdに関わらず検知する(レビューでの反証: 将来別のAWS::SNS::TopicPolicyが
    追加され、Sidを付け忘れた場合でも、本テストが個別修正なしに検知できる)。

    対象はAWS::SNS::TopicPolicyのみ(#557 issuecomment MEDIUM-1: この契約は
    SNSのSetTopicAttributes API固有の制約であり、AWS::S3::BucketPolicyやIAM
    inline policy等、他のPolicyDocumentへ一般化しない)。
    Statementが1件のみのPolicyDocument(Sid省略がAWS API上も問題にならない)は
    対象外とする。
    """
    resources = _resources()
    violations: list[str] = []

    for logical_id, statement in _sns_topic_policy_statements(resources):
        if len(statement) < 2:
            continue

        sids = [entry.get("Sid") for entry in statement]
        if any(sid is None for sid in sids):
            violations.append(f"{logical_id}: Sid未設定のStatementがある(Sid一覧: {sids})")
        elif len(sids) != len(set(sids)):
            violations.append(f"{logical_id}: Sidが重複している(Sid一覧: {sids})")

    assert not violations, (
        "AWS::SNS::TopicPolicyの複数Statementで、Sid欠落または重複が検出された"
        "(SNSのSetTopicAttributes APIはSid一意性を要求し、テンプレートのYAML"
        "検証だけでは検出できない。Issue #557参照):\n" + "\n".join(violations)
    )
