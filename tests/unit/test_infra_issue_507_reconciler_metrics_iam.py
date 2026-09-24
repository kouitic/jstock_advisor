"""infra/template.yamlのIAM・環境変数がIssue #507(S-6 queue backlog / throttle_rate
観測)の最小権限どおりであることの回帰テスト。

`cloudwatch:GetMetricData`はCloudWatch側がリソースレベル権限自体をサポートして
いない(メトリクスはARNを持たない。AWSの既知の制約)ため`Resource: "*"`が必須だが、
`sqs:GetQueueAttributes`はSQSキュー単位でリソースレベル権限をサポートするため
厳密にscopeされていることを固定する。

テンプレートの静的検証のみで、AWSへのアクセスは行わない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

_QUEUE_LOGICAL_ID = "WatchlistScreeningQueue"
_RECONCILER_FUNCTION = "WatchlistBatchReconcilerFunction"


def _load_template() -> dict[str, Any]:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda _l, suffix, node: {f"Fn::{suffix}": node.value})
    return yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)


def _resources() -> dict[str, Any]:
    return _load_template()["Resources"]


def _statements_by_sid(function_name: str, sid: str) -> list[dict[str, Any]]:
    policies = _resources()[function_name]["Properties"].get("Policies", [])
    found: list[dict[str, Any]] = []
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        for statement in policy.get("Statement", []) or []:
            if statement.get("Sid") == sid:
                found.append(statement)
    return found


def _actions(statement: dict[str, Any]) -> set[str]:
    action = statement["Action"]
    return {action} if isinstance(action, str) else set(action)


def test_cloudwatch_get_metric_data_requires_wildcard_resource() -> None:
    """CloudWatchはメトリクス単位のリソースレベル権限を持たないため、
    Resource="*"が唯一の選択肢である(AWSの既知の制約)。ワイルドカードが
    許容される稀な例外であることをテストの存在自体で明示する。
    """
    [statement] = _statements_by_sid(_RECONCILER_FUNCTION, "ObserveWatchlistWorkerMetrics")
    assert statement["Effect"] == "Allow"
    assert _actions(statement) == {"cloudwatch:GetMetricData"}
    assert statement["Resource"] == "*"


def test_sqs_get_queue_attributes_is_scoped_to_the_screening_queue_without_wildcard() -> None:
    [statement] = _statements_by_sid(_RECONCILER_FUNCTION, "ObserveWatchlistScreeningQueueDepth")
    assert statement["Effect"] == "Allow"
    assert _actions(statement) == {"sqs:GetQueueAttributes"}
    resources = statement["Resource"]
    resources = resources if isinstance(resources, list) else [resources]
    assert resources == [{"Fn::GetAtt": f"{_QUEUE_LOGICAL_ID}.Arn"}]
    assert "*" not in str(resources)


def test_reconciler_has_no_write_or_delete_permission_on_the_screening_queue() -> None:
    """S-6は読み取り専用の観測であり、SQSメッセージの送信・削除・変更権限を
    一切持たない(hidden writeが無いことのIAM側の担保。CLAUDE.md §3)。
    """
    statement = _statements_by_sid(_RECONCILER_FUNCTION, "ObserveWatchlistScreeningQueueDepth")[0]
    actions = _actions(statement)
    forbidden = {
        "sqs:SendMessage",
        "sqs:SendMessageBatch",
        "sqs:DeleteMessage",
        "sqs:DeleteMessageBatch",
        "sqs:PurgeQueue",
        "sqs:ChangeMessageVisibility",
    }
    assert not (actions & forbidden)


def test_reconciler_environment_has_queue_and_worker_function_names() -> None:
    env = _resources()[_RECONCILER_FUNCTION]["Properties"]["Environment"]["Variables"]
    assert env["WATCHLIST_SCREENING_QUEUE_NAME"] == {"Fn::GetAtt": f"{_QUEUE_LOGICAL_ID}.QueueName"}
    assert "watchlist-worker" in env["WATCHLIST_WORKER_FUNCTION_NAME"]["Fn::Sub"]
