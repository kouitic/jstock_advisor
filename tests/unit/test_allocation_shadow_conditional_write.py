"""購入側 Shadow(Q')の記録が『条件付き書込』であることの契約テスト(Issue #603)。

AuditLog の表は moto(mocked DynamoDB)で、本番と同じ Lambda 上の経路(`lambda_runtime_env`)を通す。
固定するもの:

  1. 8 スレッドが同じ (batch_id, owner) を同時に記録しても、保存されるのは 1 件
  2. 既存の audit_id への 2 回目は ConditionalCheckFailed になり、既存の項目を変えない
  3. 記録の間に呼ぶ DynamoDB の操作は PutItem だけ
     (GetItem / Scan / Query / Update / Delete を呼ばない)
  4. その PutItem はすべて `attribute_not_exists` の条件つき(無条件の書込がない)
  5. 保存された項目に owner の実名・金額・株数が無い
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from botocore.client import BaseClient
from moto import mock_aws

from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, log_ref
from jstock_advisor.domain.signals.allocation_shadow_config import (
    AllocationShadowConfig,
    AllocationShadowMode,
)
from jstock_advisor.infrastructure.collection_store import resolve_table_name
from jstock_advisor.services.allocation_shadow_service import (
    DECISION_TYPE,
    SKIP_ID_PREFIX,
    ShadowOutcome,
    ShadowResult,
    observe_allocation_shadow,
    result_audit_id,
    skip_audit_id,
)
from jstock_advisor.services.audit_service import AuditService

_REGION = "ap-northeast-1"
_NOW = dt.datetime(2026, 10, 12, 8, 0, tzinfo=dt.UTC)
_BATCH = "buy-candidates-2026-10-12"
_SHADOW = AllocationShadowConfig(mode=AllocationShadowMode.SHADOW)
_PLENTY_OF_TIME = lambda: 300_000  # noqa: E731 - 残り 300 秒


@pytest.fixture
def audit_table(
    monkeypatch: pytest.MonkeyPatch,
    lambda_runtime_env: None,
    create_collection_table: Callable[..., None],
) -> Iterator[Any]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        create_collection_table("audit_log.json", "audit_id")
        yield boto3.resource("dynamodb", region_name=_REGION).Table(
            resolve_table_name("audit_log.json")
        )


def _observe(
    owners: list[str] | None = None, compute: Any = None, remaining_ms: Any = None
) -> bool:
    # スレッドごとに自前の AuditService(自前の boto3 resource)を作る
    return observe_allocation_shadow(
        batch_id=_BATCH,
        now=_NOW,
        execution_context=ExecutionContext.normal(),
        audit_service=AuditService(),
        remaining_time_ms=remaining_ms or _PLENTY_OF_TIME,
        shadow_config=_SHADOW,
        compute=compute,
        owners=owners,
    )


def _items(table: Any) -> list[dict[str, Any]]:
    return list(table.scan()["Items"])


class _OperationSpy:
    """botocore の API 呼び出し(操作名とパラメータ)を記録する。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        original = BaseClient._make_api_call
        spy = self

        def _wrapped(client: Any, operation_name: str, api_params: dict[str, Any]) -> Any:
            spy.calls.append((operation_name, dict(api_params)))
            return original(client, operation_name, api_params)

        monkeypatch.setattr(BaseClient, "_make_api_call", _wrapped)


def test_a_single_run_stores_one_item_with_the_deterministic_id(audit_table: Any) -> None:
    assert _observe() is True

    [item] = _items(audit_table)
    # 既定の計算は『未実装』= スキップなので、スキップの ID（結果の ID とは別の鍵。Q-B）
    assert item["audit_id"] == skip_audit_id(_BATCH, DEFAULT_OWNER)
    assert item["audit_id"] == f"{SKIP_ID_PREFIX}:{_BATCH}:{log_ref(DEFAULT_OWNER)}"


def test_eight_concurrent_runs_store_exactly_one_item(audit_table: Any) -> None:
    results: list[bool] = []
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()  # 同時に開始する
        recorded = _observe()
        with lock:
            results.append(recorded)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert len(results) == 8
    assert results.count(True) == 1  # 記録できたのは 1 実行だけ
    assert len(_items(audit_table)) == 1


def test_second_run_hits_conditional_check_failed_and_keeps_the_item(audit_table: Any) -> None:
    assert _observe() is True
    [before] = _items(audit_table)

    assert _observe() is False  # 既存 -> 何も変えない

    assert _items(audit_table) == [before]


def test_distinct_owners_are_stored_separately(audit_table: Any) -> None:
    assert _observe(["owner-a", "owner-b"]) is True

    ids = sorted(item["audit_id"] for item in _items(audit_table))
    assert ids == sorted(skip_audit_id(_BATCH, o) for o in ("owner-a", "owner-b"))


def test_only_conditional_put_item_is_issued(
    audit_table: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _OperationSpy(monkeypatch)

    _observe()  # 1 回目: 新規
    _observe()  # 2 回目: ConditionalCheckFailed

    operations = [name for name, _ in spy.calls]
    assert operations, "DynamoDB 呼び出しが観測されていない(spy が効いていない)"
    assert set(operations) == {"PutItem"}  # GetItem / Scan / Query / UpdateItem / DeleteItem なし
    assert operations.count("PutItem") == 2
    for _, params in spy.calls:
        assert "attribute_not_exists" in params["ConditionExpression"]


def test_stored_item_contains_no_owner_name_or_money(audit_table: Any) -> None:
    _observe()

    [item] = _items(audit_table)
    text = repr(item)
    assert DEFAULT_OWNER not in text  # 実名(固定の既定 owner 文字列)は保存しない
    assert log_ref(DEFAULT_OWNER) in text  # 符号は保存する
    assert "COMPUTE_NOT_IMPLEMENTED" in text


def test_late_result_is_stored_next_to_the_time_limit_skip(audit_table: Any) -> None:
    """捨てたワーカーが後から成功した場合（Q-B）: スキップと結果は別の ID で、両方が残る。"""
    assert _observe(remaining_ms=lambda: 119_900) is True  # 残り時間が足りない = スキップを記録
    assert (
        _observe(  # 後から本物の結果が書ける（スキップに塞がれない）
            compute=lambda run: ShadowResult(ShadowOutcome.COMPUTED, facts={"candidates": 2})
        )
        is True
    )

    items = {item["audit_id"]: item for item in _items(audit_table)}
    assert set(items) == {
        skip_audit_id(_BATCH, DEFAULT_OWNER),
        result_audit_id(_BATCH, DEFAULT_OWNER),
    }
    assert result_audit_id(_BATCH, DEFAULT_OWNER).startswith(f"{DECISION_TYPE}:")
    # どちらの ID も、先に書いた方が残る（同じ内容の 2 回目は何もしない）
    assert _observe(remaining_ms=lambda: 119_900) is False
    assert _observe(compute=lambda run: ShadowResult(ShadowOutcome.COMPUTED)) is False
    assert len(_items(audit_table)) == 2
