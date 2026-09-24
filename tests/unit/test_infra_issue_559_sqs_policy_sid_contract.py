"""Issue #559(W9根本原因): AWS::SQS::QueuePolicyへの予防的Sid契約テスト。

Issue #557(直接原因)は`IncidentNotificationTopicPolicy`(AWS::SNS::TopicPolicy)の
複数StatementにSidが無く、SNSの`SetTopicAttributes` APIが
"Invalid parameter: Every policy statement must have a unique ID"で拒否した
事象だった。`tests/unit/test_infra_issue_557_topic_policy_sid.py`が、この契約を
`AWS::SNS::TopicPolicy`というresource type全体へ汎用化した回帰テストを既に持つ
(LogicalIdに依存せず将来のSNS TopicPolicy追加にも効く)。

本テストはIssue #559(release検証層の欠落の根本原因分析)のPhase B PR-1として、
`AWS::SQS::QueuePolicy`への**予防的**カバレッジを追加する。

**確認済みの制約(SNS)と予防的な備え(SQS)の違いを明確にしておく**:
SNS側はProduction実測エラーで制約の実在が確定している。SQS側は、AWS公式
ドキュメント(IAM JSON policy elements: Sid)が「Some AWS services (for example,
Amazon SQS or Amazon SNS) might require this element and have uniqueness
requirements for it.」と例示するに留まり、SQSの`SetQueueAttributes` APIが
実際に同じ制約を課すかは本Issueの調査時点で確認できていない(実エラーの再現・
一次情報の確定のいずれも無い)。

そのため本テストは「SQSも同じ制約を持つと確認された」という前提には立たず、
「AWS公式が制約の可能性を示唆しているサービスについては、現在0件でも
将来追加時に個別対応漏れが起きないよう、機械的なガードを先に用意しておく」
という予防目的のみで存在する。現在このtemplate.yamlにはAWS::SQS::QueuePolicy
resourceは1件も無いため、`test_all_sqs_queue_policy_statements_have_unique_sids`は
現時点では走査対象0件で自明にpassする。ロジック自体が正しく機能することは、
合成データを使った`test_*_synthetic_*`系のテストで別途固定する(Production
templateへ偽のresourceを追加する必要を避けるため)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

_SQS_QUEUE_POLICY_TYPE = "AWS::SQS::QueuePolicy"


def _load_template() -> dict[str, Any]:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda _l, suffix, node: {f"Fn::{suffix}": node.value})
    return yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)


def _resources() -> dict[str, Any]:
    return _load_template()["Resources"]


def _policy_statements_of_type(
    resources: dict[str, Any], resource_type: str
) -> list[tuple[str, list[Any]]]:
    """(logical_id, Statementリスト)のタプルを、指定resource typeの
    `Properties.PolicyDocument.Statement`からのみ収集する。

    test_infra_issue_557_topic_policy_sid.pyの`_sns_topic_policy_statements`と
    同じ形の走査を、resource type引数で汎用化したもの(#557のfileは変更しない。
    重複実装を避けつつ、独立したIssueスコープのfileとして自己完結させるため、
    このfile内でのみ定義する)。
    """
    found: list[tuple[str, list[Any]]] = []
    for logical_id, resource in resources.items():
        if not isinstance(resource, dict) or resource.get("Type") != resource_type:
            continue
        statement = resource.get("Properties", {}).get("PolicyDocument", {}).get("Statement")
        if isinstance(statement, list):
            found.append((logical_id, statement))
    return found


def _sid_violations(entries: list[tuple[str, list[Any]]]) -> list[str]:
    """複数StatementのPolicyDocumentについて、Sid欠落・重複を検出する。
    単一StatementのPolicyDocumentはSid省略がAWS API上も問題にならないため対象外。
    """
    violations: list[str] = []
    for logical_id, statement in entries:
        if len(statement) < 2:
            continue
        sids = [entry.get("Sid") for entry in statement]
        if any(sid is None for sid in sids):
            violations.append(f"{logical_id}: Sid未設定のStatementがある(Sid一覧: {sids})")
        elif len(sids) != len(set(sids)):
            violations.append(f"{logical_id}: Sidが重複している(Sid一覧: {sids})")
    return violations


# --- 現在のtemplate.yamlに対する走査(予防的。現時点では0件でvacuously pass) ------


def test_all_sqs_queue_policy_statements_have_unique_sids() -> None:
    """AWS::SQS::QueuePolicyが複数Statementを持つ場合、全StatementがSidを持ち
    一意であることを固定する(予防的。#559)。現在このtemplate.yamlに
    AWS::SQS::QueuePolicyは存在しないため、本テストは現時点で走査対象0件で
    自明にpassする。将来SQS QueuePolicyが追加された場合に、Sid欠落・重複を
    個別対応なしに検知するために存在する。
    """
    resources = _resources()
    entries = _policy_statements_of_type(resources, _SQS_QUEUE_POLICY_TYPE)
    violations = _sid_violations(entries)

    assert not violations, (
        "AWS::SQS::QueuePolicyの複数Statementで、Sid欠落または重複が検出された"
        "(AWS公式ドキュメントはSQS/SNSがSidを要求し一意性要件を持つ場合があると"
        "明記している。Issue #559参照):\n" + "\n".join(violations)
    )


def test_no_sqs_queue_policy_currently_exists_in_the_template() -> None:
    """前提の記録: このtemplate.yamlには現在AWS::SQS::QueuePolicyが1件も無い
    (#559調査時点の実測)。この前提が崩れた場合(SQS QueuePolicyが追加された
    場合)、`test_all_sqs_queue_policy_statements_have_unique_sids`が初めて
    実質的な走査対象を持つことになる。この事実を明示的に固定しておく
    (前提が静かに変わることを防ぐ)。
    """
    resources = _resources()
    entries = _policy_statements_of_type(resources, _SQS_QUEUE_POLICY_TYPE)
    assert entries == []


# --- 合成データによるロジック検証(negative verificationを兼ねる) -----------------


def test_synthetic_sqs_queue_policy_with_missing_sid_is_detected() -> None:
    """`_sid_violations`が、Sid欠落を持つ複数StatementのSQS QueuePolicyを
    実際に検知できることを、合成データで固定する(#559 T1相当。Production
    templateへ偽のresourceを追加せずにロジックを検証する)。
    """
    resources = {
        "FakeQueuePolicy": {
            "Type": _SQS_QUEUE_POLICY_TYPE,
            "Properties": {
                "PolicyDocument": {
                    "Statement": [
                        {"Sid": "AllowA", "Effect": "Allow"},
                        {"Effect": "Allow"},  # Sid欠落
                    ]
                }
            },
        }
    }
    entries = _policy_statements_of_type(resources, _SQS_QUEUE_POLICY_TYPE)
    violations = _sid_violations(entries)

    assert violations == ["FakeQueuePolicy: Sid未設定のStatementがある(Sid一覧: ['AllowA', None])"]


def test_synthetic_sqs_queue_policy_with_duplicate_sid_is_detected() -> None:
    """#559 T4相当: Sid重複を検知する。"""
    resources = {
        "FakeQueuePolicy": {
            "Type": _SQS_QUEUE_POLICY_TYPE,
            "Properties": {
                "PolicyDocument": {
                    "Statement": [
                        {"Sid": "AllowA", "Effect": "Allow"},
                        {"Sid": "AllowA", "Effect": "Allow"},  # 重複
                    ]
                }
            },
        }
    }
    entries = _policy_statements_of_type(resources, _SQS_QUEUE_POLICY_TYPE)
    violations = _sid_violations(entries)

    assert violations == ["FakeQueuePolicy: Sidが重複している(Sid一覧: ['AllowA', 'AllowA'])"]


def test_synthetic_sqs_queue_policy_with_valid_sids_passes() -> None:
    """#559 T2相当: 正常なSQS QueuePolicy(全StatementにSid付き・重複無し)は
    violationにならない。"""
    resources = {
        "FakeQueuePolicy": {
            "Type": _SQS_QUEUE_POLICY_TYPE,
            "Properties": {
                "PolicyDocument": {
                    "Statement": [
                        {"Sid": "AllowA", "Effect": "Allow"},
                        {"Sid": "AllowB", "Effect": "Allow"},
                    ]
                }
            },
        }
    }
    entries = _policy_statements_of_type(resources, _SQS_QUEUE_POLICY_TYPE)
    assert _sid_violations(entries) == []


def test_synthetic_single_statement_sqs_queue_policy_without_sid_is_not_flagged() -> None:
    """#559 T5相当: Statementが1件のみの場合、Sid省略はAWS API上も問題にならない
    ため、falseに検知しない(scope discipline)。"""
    resources = {
        "FakeQueuePolicy": {
            "Type": _SQS_QUEUE_POLICY_TYPE,
            "Properties": {
                "PolicyDocument": {"Statement": [{"Effect": "Allow"}]}  # Sid無し・単一
            },
        }
    }
    entries = _policy_statements_of_type(resources, _SQS_QUEUE_POLICY_TYPE)
    assert _sid_violations(entries) == []


def test_synthetic_unrelated_policy_document_type_is_not_scanned() -> None:
    """#559 T5相当(汎用化の誤爆防止): AWS::SQS::QueuePolicy以外のresource type
    (例: AWS::S3::BucketPolicy)は、Sid欠落があっても`_policy_statements_of_type`
    がAWS::SQS::QueuePolicyを指定した場合には収集されない(誤って他typeへ
    制約を広げない)。
    """
    resources = {
        "FakeBucketPolicy": {
            "Type": "AWS::S3::BucketPolicy",
            "Properties": {
                "PolicyDocument": {
                    "Statement": [
                        {"Effect": "Deny"},
                        {"Effect": "Allow"},
                    ]
                }
            },
        }
    }
    entries = _policy_statements_of_type(resources, _SQS_QUEUE_POLICY_TYPE)
    assert entries == []
