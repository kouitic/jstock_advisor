"""購入側 Shadow の記録が『条件付き書込』であることの契約テスト(Issue #603)。

AuditLog の表は moto(mocked DynamoDB)で、本番と同じ Lambda 上の経路(`lambda_runtime_env`)を通す。
観測のループ(`allocation_shadow_runner`)の記録を、実際の `AuditService` で保存して固定するもの:

  1. 4 スレッドが同じ日・同じ owner を同時に記録しても、各 audit_id の保存は 1 件
  2. 既存の audit_id への 2 回目は ConditionalCheckFailed になり、既存の項目を変えない
  3. 記録の間に呼ぶ DynamoDB の操作は PutItem だけ
     (GetItem / Scan / Query / Update / Delete を呼ばない)
  4. その PutItem はすべて `attribute_not_exists` の条件つき(無条件の書込がない)
  5. 保存された項目に owner の実名・金額・株数が無く、owner は kv1 の符号だけ
  6. 鍵が取れない日は、日単位の skip が 1 件だけ(owner 単位の記録は 0 件)
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from botocore.client import BaseClient
from moto import mock_aws

from jstock_advisor.domain.entities.identifier_ref import KeyUnavailable, KeyUnavailableReason
from jstock_advisor.domain.signals.allocation_shadow_record import (
    day_skip_audit_id,
    index_audit_id,
    items_audit_id,
)
from jstock_advisor.infrastructure.collection_store import resolve_table_name
from jstock_advisor.services.allocation_shadow_runner import make_runner
from jstock_advisor.services.allocation_shadow_service import Deadline, ShadowRunContext
from jstock_advisor.services.audit_service import AuditService
from tests.unit.test_allocation_shadow_runner_contract import (
    _BATCH,
    _DATE,
    _NOW,
    _SENTINEL,
    _Compute,
    _owners,
    _prepare,
    _Provider,
    _ref,
)

_REGION = "ap-northeast-1"


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


def _run(owners: list[str] | None = None, provider: Any = None) -> bool:
    # スレッドごとに自前の AuditService(自前の boto3 resource)を作る
    runner = make_runner(
        prepare=_prepare(owners if owners is not None else _owners(2)),
        compute_owner=_Compute(),
        key_provider=provider if provider is not None else _Provider(),
    )
    return runner(
        ShadowRunContext(
            batch_id=_BATCH,
            now=_NOW,
            audit_service=AuditService(),
            deadline=Deadline(30.0),
            io_timeout_seconds=5.0,
            shadow_mode="SHADOW",
            precheck=lambda: None,
        )
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


def _expected_ids(owners: list[str]) -> set[str]:
    ids = {index_audit_id(_DATE)}
    for owner in owners:
        ref = _ref(owner)
        ids.add(items_audit_id(_DATE, ref))
        ids.add(f"allocation_shadow_skip:{_BATCH}:{ref}")  # 枠の記録(この fake は SKIPPED の結果)
    return ids


def test_a_single_run_stores_the_deterministic_ids(audit_table: Any) -> None:
    assert _run() is True

    ids = {item["audit_id"] for item in _items(audit_table)}
    assert ids == _expected_ids(_owners(2))


def test_four_concurrent_runs_store_each_record_exactly_once(audit_table: Any) -> None:
    results: list[bool] = []
    barrier = threading.Barrier(4)
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()  # 同時に開始する
        recorded = _run()
        with lock:
            results.append(recorded)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert len(results) == 4
    assert results.count(True) >= 1  # 少なくとも 1 実行が新しい記録を書いた
    ids = [item["audit_id"] for item in _items(audit_table)]
    assert sorted(ids) == sorted(_expected_ids(_owners(2)))  # 各 ID はちょうど 1 件


def test_second_run_hits_conditional_check_failed_and_keeps_every_item(audit_table: Any) -> None:
    assert _run() is True
    before = sorted(_items(audit_table), key=lambda item: item["audit_id"])

    assert _run() is False  # すべて既存 -> 何も変えない

    assert sorted(_items(audit_table), key=lambda item: item["audit_id"]) == before


def test_only_conditional_put_item_is_issued(
    audit_table: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _OperationSpy(monkeypatch)

    _run()  # 1 回目: 新規
    _run()  # 2 回目: すべて ConditionalCheckFailed

    operations = [name for name, _ in spy.calls]
    assert operations, "DynamoDB 呼び出しが観測されていない(spy が効いていない)"
    assert set(operations) == {"PutItem"}  # GetItem / Scan / Query / UpdateItem / DeleteItem なし
    assert operations.count("PutItem") == 2 * len(_expected_ids(_owners(2)))
    for _, params in spy.calls:
        assert "attribute_not_exists" in params["ConditionExpression"]


def test_stored_items_contain_no_plain_owner_and_use_kv1_references_only(
    audit_table: Any,
) -> None:
    _run([_SENTINEL, "owner-01"])

    stored = _items(audit_table)
    text = repr(stored)
    assert _SENTINEL not in text  # 実名は保存しない
    assert "owner-01" not in text
    assert _ref(_SENTINEL) in text  # kv1 の符号は保存する
    assert "kv0" not in text


def test_a_key_failure_day_stores_one_day_skip_and_no_owner_record(audit_table: Any) -> None:
    provider = _Provider(KeyUnavailable(KeyUnavailableReason.ACCESS_DENIED))

    assert _run(provider=provider) is True

    [item] = _items(audit_table)
    assert item["audit_id"] == day_skip_audit_id(_DATE)  # 業務日だけが key
    assert "ACCESS_DENIED" in repr(item)
    assert provider.calls == 1
