import datetime as dt

import pytest

from jstock_advisor.infrastructure.aws import batch_tracker

_NOW = dt.datetime(2026, 7, 28, 7, 0, tzinfo=dt.UTC)


class _FakeTable:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, object]] = {}

    def put_item(self, Item: dict[str, object]) -> None:  # noqa: N803 - boto3のAPI引数名に合わせる
        self.items[Item["batch_id"]] = dict(Item)

    def update_item(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]["batch_id"]  # type: ignore[index]
        expr = kwargs["UpdateExpression"]  # type: ignore[index]
        item = self.items[key]
        if "succeeded" in expr:  # type: ignore[operator]
            item["succeeded"] = int(item["succeeded"]) + 1
        else:
            item["failed"] = int(item["failed"]) + 1
        item["completed"] = int(item["completed"]) + 1
        return {"Attributes": dict(item)}


class _FakeResource:
    def __init__(self, table: _FakeTable) -> None:
        self._table = table

    def Table(self, name: str) -> _FakeTable:  # noqa: N802 - boto3のAPI名に合わせる
        return self._table


def test_start_batch_and_record_result_no_op_when_not_on_lambda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(batch_tracker, "running_on_lambda", lambda: False)
    batch_tracker.start_batch("batch-1", 3, _NOW)  # 例外を出さずに何もしない
    assert batch_tracker.record_result("batch-1", succeeded=True) is None


def test_start_batch_with_zero_total_is_noop_even_on_lambda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(batch_tracker, "running_on_lambda", lambda: True)
    calls = []
    monkeypatch.setattr(batch_tracker.boto3, "resource", lambda *a, **kw: calls.append(1))
    batch_tracker.start_batch("batch-1", 0, _NOW)
    assert calls == []


def test_record_result_tracks_progress_and_detects_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(batch_tracker, "running_on_lambda", lambda: True)
    table = _FakeTable()
    monkeypatch.setattr(batch_tracker.boto3, "resource", lambda *a, **kw: _FakeResource(table))

    batch_tracker.start_batch("batch-1", 3, _NOW)

    p1 = batch_tracker.record_result("batch-1", succeeded=True)
    assert p1 is not None
    assert p1.total == 3
    assert p1.succeeded == 1
    assert p1.failed == 0
    assert p1.completed == 1
    assert p1.is_complete is False

    batch_tracker.record_result("batch-1", succeeded=False)
    p3 = batch_tracker.record_result("batch-1", succeeded=True)
    assert p3 is not None
    assert p3.succeeded == 2
    assert p3.failed == 1
    assert p3.completed == 3
    assert p3.is_complete is True
