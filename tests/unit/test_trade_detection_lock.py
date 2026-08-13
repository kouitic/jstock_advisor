"""TradeDetectionRunLockの状態管理(PROCESSING/COMPLETED)のテスト(BUY候補裾野拡大機能2026-08)。

実際のDynamoDBのConditionExpression意味論(attribute_not_exists・比較演算子)を
最小限模倣したフェイクテーブルを使う(test_batch_tracker.pyと同じ方針)。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from botocore.exceptions import ClientError

from jstock_advisor.infrastructure.aws import trade_detection_lock

_NOW = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)  # 月曜


class _FakeTable:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]["business_date"]
        condition = kwargs.get("ConditionExpression")
        values = kwargs["ExpressionAttributeValues"]
        item = self.items.get(key, {})

        if condition == (
            "attribute_not_exists(#status) OR "
            "(#status = :processing AND lease_expires_at < :now)"
        ):
            ok = "status" not in item or (
                item.get("status") == values[":processing"]
                and item.get("lease_expires_at", "") < values[":now"]
            )
            if not ok:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
                    "UpdateItem",
                )
            item.update(
                {
                    "status": values[":processing"],
                    "leased_at": values[":now"],
                    "lease_expires_at": values[":expires"],
                    "ttl": values[":ttl"],
                }
            )
        elif condition == "leased_at = :leased_at":
            if item.get("leased_at") != values[":leased_at"]:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
                    "UpdateItem",
                )
            item["status"] = values[":completed"]
        else:
            raise AssertionError(f"unexpected condition: {condition}")

        self.items[key] = item
        return {"Attributes": dict(item)}

    def get_item(self, Key: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        item = self.items.get(Key["business_date"])
        return {"Item": item} if item is not None else {}


class _FakeResource:
    def __init__(self, table: _FakeTable) -> None:
        self._table = table

    def Table(self, name: str) -> _FakeTable:  # noqa: N802
        return self._table


@pytest.fixture
def fake_table_on_lambda(monkeypatch: pytest.MonkeyPatch) -> _FakeTable:
    monkeypatch.setattr(trade_detection_lock, "running_on_lambda", lambda: True)
    table = _FakeTable()
    resource_factory = lambda *a, **kw: _FakeResource(table)  # noqa: E731
    monkeypatch.setattr(trade_detection_lock.boto3, "resource", resource_factory)
    return table


def test_local_env_always_acquires(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trade_detection_lock, "running_on_lambda", lambda: False)
    assert trade_detection_lock.try_acquire("2026-08-17", _NOW, 60) is True


def test_first_acquire_succeeds_on_lambda(fake_table_on_lambda: _FakeTable) -> None:
    assert trade_detection_lock.try_acquire("2026-08-17", _NOW, 60) is True


def test_second_acquire_fails_while_processing_and_not_expired(
    fake_table_on_lambda: _FakeTable,
) -> None:
    assert trade_detection_lock.try_acquire("2026-08-17", _NOW, 60) is True
    later = _NOW + dt.timedelta(seconds=10)
    assert trade_detection_lock.try_acquire("2026-08-17", later, 60) is False


def test_stale_lock_can_be_recovered_after_lease_expires(
    fake_table_on_lambda: _FakeTable,
) -> None:
    assert trade_detection_lock.try_acquire("2026-08-17", _NOW, 60) is True
    much_later = _NOW + dt.timedelta(seconds=120)  # lease(60秒)失効後
    assert trade_detection_lock.try_acquire("2026-08-17", much_later, 60) is True


def test_mark_completed_succeeds_with_matching_lease(fake_table_on_lambda: _FakeTable) -> None:
    trade_detection_lock.try_acquire("2026-08-17", _NOW, 60)
    trade_detection_lock.mark_completed("2026-08-17", leased_at_iso=_NOW.isoformat())
    status, _ = trade_detection_lock.get_status("2026-08-17")
    assert status == trade_detection_lock.RunLockStatus.COMPLETED.value


def test_mark_completed_noop_when_lease_was_taken_over(fake_table_on_lambda: _FakeTable) -> None:
    """自分が取得したリース(leased_at)と一致しない場合は上書きしない
    (先行Lambdaが異常終了しstale lockが別のLambdaに奪取された後のケース)。"""
    trade_detection_lock.try_acquire("2026-08-17", _NOW, 60)
    # 別の(架空の)leased_atでmark_completedを試みる → 一致しないため例外を吸収し何もしない
    trade_detection_lock.mark_completed("2026-08-17", leased_at_iso="1999-01-01T00:00:00")
    status, _ = trade_detection_lock.get_status("2026-08-17")
    assert status == trade_detection_lock.RunLockStatus.PROCESSING.value


def test_get_status_returns_none_when_no_entry(fake_table_on_lambda: _FakeTable) -> None:
    status, expires = trade_detection_lock.get_status("2026-08-17")
    assert status is None
    assert expires is None
