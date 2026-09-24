"""infra/template.yamlのIAM・環境変数がIssue #507(S-6 queue backlog / throttle_rate
観測)の最小権限どおりであることの回帰テスト。

`cloudwatch:GetMetricData`はCloudWatch側がリソースレベル権限自体をサポートして
いない(メトリクスはARNを持たない。AWSの既知の制約)ため`Resource: "*"`が必須。

★ #507レビューF2是正: 実装(`_fetch_watchlist_worker_metrics`)はCloudWatch
GetMetricDataのみでOldestMessageAge/Throttles/Invocationsをすべて取得しており、
`boto3.client("sqs")`を一度も構築しない。当初付与していた
`sqs:GetQueueAttributes`は未使用の権限だったため削除した(最小権限の原則。
使わない権限は持たせない)。本ファイルはその削除を固定し、将来の再追加を
検知する(実際に使うようになった場合のみ、使用箇所とともに再度追加すること)。

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


def test_reconciler_has_no_sqs_permission_at_all() -> None:
    """★ #507レビューF2の直接固定: S-6はCloudWatch GetMetricData経由で
    OldestMessageAgeを観測するため、SQSへの直接API権限(GetQueueAttributes
    含む)を一切必要としない。未使用の権限を持たせない(最小権限の原則。
    hidden writeが無いことの確認と同種。CLAUDE.md §3)。
    """
    policies = _resources()[_RECONCILER_FUNCTION]["Properties"].get("Policies", [])
    all_actions: set[str] = set()
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        for statement in policy.get("Statement", []) or []:
            all_actions |= _actions(statement)
    sqs_actions = {action for action in all_actions if action.startswith("sqs:")}
    assert sqs_actions == set()


def test_reconciler_environment_has_queue_and_worker_function_names() -> None:
    env = _resources()[_RECONCILER_FUNCTION]["Properties"]["Environment"]["Variables"]
    assert env["WATCHLIST_SCREENING_QUEUE_NAME"] == {"Fn::GetAtt": f"{_QUEUE_LOGICAL_ID}.QueueName"}
    assert "watchlist-worker" in env["WATCHLIST_WORKER_FUNCTION_NAME"]["Fn::Sub"]
