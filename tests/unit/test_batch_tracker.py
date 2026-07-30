import datetime as dt

import pytest

from jstock_advisor.infrastructure.aws import batch_tracker

_NOW = dt.datetime(2026, 7, 28, 7, 0, tzinfo=dt.UTC)


class _FakeTable:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, object]] = {}

    def put_item(self, Item: dict[str, object]) -> None:  # noqa: N803 - boto3のAPI引数名に合わせる
        self.items[Item["batch_id"]] = dict(Item)  # type: ignore[index]

    def update_item(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]["batch_id"]  # type: ignore[index]
        expr = kwargs["UpdateExpression"]  # type: ignore[index]
        values = kwargs["ExpressionAttributeValues"]  # type: ignore[index]
        names = kwargs.get("ExpressionAttributeNames") or {}  # type: ignore[union-attr]
        item = self.items[key]
        # "ADD #field1 :val1, #field2 :val2, ..." を簡易パースし、DynamoDBのADD意味論
        # (数値は加算、文字列セットは和集合)を模倣する。#で始まる名前は
        # ExpressionAttributeNames経由で実際の属性名に解決する(予約語対策の検証用)。
        assignments = expr.split("ADD ", 1)[1]  # type: ignore[union-attr]
        for pair in assignments.split(","):
            field_ref, placeholder = pair.strip().split()
            field = names[field_ref] if field_ref.startswith("#") else field_ref  # type: ignore[index]
            value = values[placeholder]  # type: ignore[index]
            if isinstance(value, set):
                current = item.get(field, set())
                item[field] = set(current) | value  # type: ignore[arg-type]
            else:
                item[field] = int(item.get(field, 0)) + value  # type: ignore[operator]
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
    assert batch_tracker.record_result("batch-1", "sent") is None


def test_start_batch_with_zero_total_is_noop_even_on_lambda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(batch_tracker, "running_on_lambda", lambda: True)
    calls = []
    monkeypatch.setattr(batch_tracker.boto3, "resource", lambda *a, **kw: calls.append(1))
    batch_tracker.start_batch("batch-1", 0, _NOW)
    assert calls == []


def test_record_result_rejects_unknown_category(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(batch_tracker, "running_on_lambda", lambda: True)
    with pytest.raises(ValueError, match="unknown batch result category"):
        batch_tracker.record_result("batch-1", "not_a_real_category")


def test_record_result_tracks_category_breakdown_and_detects_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(batch_tracker, "running_on_lambda", lambda: True)
    table = _FakeTable()
    monkeypatch.setattr(batch_tracker.boto3, "resource", lambda *a, **kw: _FakeResource(table))

    batch_tracker.start_batch("batch-1", 3, _NOW)

    p1 = batch_tracker.record_result("batch-1", "sent")
    assert p1 is not None
    assert p1.total == 3
    assert p1.category_counts["sent"] == 1
    assert p1.completed == 1
    assert p1.is_complete is False

    batch_tracker.record_result("batch-1", "hold")
    p3 = batch_tracker.record_result("batch-1", "failed", stock_code="1234")
    assert p3 is not None
    assert p3.category_counts["sent"] == 1
    assert p3.category_counts["hold"] == 1
    assert p3.category_counts["failed"] == 1
    assert p3.completed == 3
    assert p3.is_complete is True
    assert p3.failed_stock_codes == ["1234"]
    assert p3.data_insufficient_stock_codes == []


def test_record_result_never_uses_raw_category_name_in_update_expression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DynamoDBの予約語(例: "hold")対策として、UpdateExpressionには常に
    ExpressionAttributeNamesのプレースホルダ(#...)のみを使い、カテゴリ名を
    直書きしないことを保証する回帰テスト(hold単体を通すだけでは、たまたま
    "hold"以外の予約語が将来増えても検知できないため、全カテゴリを検証する)。
    """
    monkeypatch.setattr(batch_tracker, "running_on_lambda", lambda: True)
    captured_exprs: list[str] = []

    class _RecordingTable(_FakeTable):
        def update_item(self, **kwargs: object) -> dict[str, object]:
            captured_exprs.append(kwargs["UpdateExpression"])  # type: ignore[arg-type]
            return super().update_item(**kwargs)

    table = _RecordingTable()
    monkeypatch.setattr(batch_tracker.boto3, "resource", lambda *a, **kw: _FakeResource(table))

    from jstock_advisor.domain.entities.evaluation_audit import SUMMARY_CATEGORIES

    batch_tracker.start_batch("batch-1", len(SUMMARY_CATEGORIES), _NOW)
    for category in SUMMARY_CATEGORIES:
        batch_tracker.record_result("batch-1", category, stock_code="1234")

    for category in SUMMARY_CATEGORIES:
        for expr in captured_exprs:
            assert category not in expr.replace(f"#{category}", ""), (
                f"category '{category}' appears unaliased in UpdateExpression: {expr}"
            )


def test_record_result_accumulates_stock_codes_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(batch_tracker, "running_on_lambda", lambda: True)
    table = _FakeTable()
    monkeypatch.setattr(batch_tracker.boto3, "resource", lambda *a, **kw: _FakeResource(table))

    batch_tracker.start_batch("batch-1", 2, _NOW)
    batch_tracker.record_result("batch-1", "data_insufficient", stock_code="1111")
    progress = batch_tracker.record_result("batch-1", "data_insufficient", stock_code="2222")

    assert progress is not None
    assert progress.category_counts["data_insufficient"] == 2
    assert sorted(progress.data_insufficient_stock_codes) == ["1111", "2222"]
