import datetime as dt

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.infrastructure.aws import batch_tracker

_NOW = dt.datetime(2026, 7, 28, 7, 0, tzinfo=dt.UTC)
_REGION = "ap-northeast-1"


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


def test_record_result_accumulates_ranking_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """買い候補の優先度付け通知(2026-07仕様追加)向け: ranking_entryを渡した銘柄が
    文字列セットへ原子的に蓄積され、record_resultの戻り値から取得できることを確認する。
    """
    monkeypatch.setattr(batch_tracker, "running_on_lambda", lambda: True)
    table = _FakeTable()
    monkeypatch.setattr(batch_tracker.boto3, "resource", lambda *a, **kw: _FakeResource(table))

    batch_tracker.start_batch("batch-1", 3, _NOW)
    batch_tracker.record_result(
        "batch-1", "candidate_not_ranked", ranking_entry="72.5|1234|rec-a"
    )
    batch_tracker.record_result(
        "batch-1", "candidate_not_ranked", ranking_entry="90.0|5678|rec-b"
    )
    progress = batch_tracker.record_result("batch-1", "hold")

    assert progress is not None
    assert progress.category_counts["candidate_not_ranked"] == 2
    assert sorted(progress.ranking_entries) == ["72.5|1234|rec-a", "90.0|5678|rec-b"]


def test_record_result_without_ranking_entry_leaves_ranking_entries_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(batch_tracker, "running_on_lambda", lambda: True)
    table = _FakeTable()
    monkeypatch.setattr(batch_tracker.boto3, "resource", lambda *a, **kw: _FakeResource(table))

    batch_tracker.start_batch("batch-1", 1, _NOW)
    progress = batch_tracker.record_result("batch-1", "hold")

    assert progress is not None
    assert progress.ranking_entries == []


# --- try_acquire_finalize / mark_finalize_complete(ウォッチリスト自動追加機能) ---
# ConditionExpressionの実際の意味論を検証する必要があるため、_FakeTableの簡易ADD
# パーサではなくmoto(実DynamoDB相当の挙動)を使う。


@pytest.fixture
def moto_dynamodb(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(batch_tracker, "running_on_lambda", lambda: True)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "jstock")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName="jstock-batch_runs",
            KeySchema=[{"AttributeName": "batch_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "batch_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def test_try_acquire_finalize_succeeds_after_start_batch(moto_dynamodb: None) -> None:
    batch_tracker.start_batch("batch-1", 3, _NOW)
    assert batch_tracker.try_acquire_finalize("batch-1") is True


def test_try_acquire_finalize_only_succeeds_once_for_concurrent_workers(
    moto_dynamodb: None,
) -> None:
    """複数ワーカーが同時にis_complete==Trueを観測しても、1ワーカーだけが
    finalize権限を取得できることを検証する(実装プラン§7)。
    """
    batch_tracker.start_batch("batch-1", 3, _NOW)

    first = batch_tracker.try_acquire_finalize("batch-1")
    second = batch_tracker.try_acquire_finalize("batch-1")
    third = batch_tracker.try_acquire_finalize("batch-1")

    assert [first, second, third] == [True, False, False]


def test_mark_finalize_complete_transitions_to_completed(moto_dynamodb: None) -> None:
    batch_tracker.start_batch("batch-1", 3, _NOW)
    batch_tracker.try_acquire_finalize("batch-1")

    batch_tracker.mark_finalize_complete("batch-1")

    table = boto3.resource("dynamodb", region_name=_REGION).Table("jstock-batch_runs")
    item = table.get_item(Key={"batch_id": "batch-1"})["Item"]
    assert item["status"] == "COMPLETED"


def test_try_acquire_finalize_returns_true_locally_without_dynamodb_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(batch_tracker, "running_on_lambda", lambda: False)
    assert batch_tracker.try_acquire_finalize("batch-1") is True


def test_mark_finalize_complete_is_noop_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(batch_tracker, "running_on_lambda", lambda: False)
    batch_tracker.mark_finalize_complete("batch-1")  # 例外を出さずに何もしない


# --- mark_finalize_failed(レビュー対応: finalize失敗時にCOMPLETEDへ遷移させない) ---


def test_mark_finalize_failed_transitions_to_finalize_failed(moto_dynamodb: None) -> None:
    batch_tracker.start_batch("batch-1", 3, _NOW)
    batch_tracker.try_acquire_finalize("batch-1")

    batch_tracker.mark_finalize_failed("batch-1", "boom")

    table = boto3.resource("dynamodb", region_name=_REGION).Table("jstock-batch_runs")
    item = table.get_item(Key={"batch_id": "batch-1"})["Item"]
    assert item["status"] == "FINALIZE_FAILED"
    assert item["finalize_error_message"] == "boom"
    assert "finalize_failed_at" in item
    assert "updated_at" in item


def test_mark_finalize_failed_truncates_long_error_message(moto_dynamodb: None) -> None:
    batch_tracker.start_batch("batch-1", 3, _NOW)
    batch_tracker.try_acquire_finalize("batch-1")
    long_message = "x" * 10_000

    batch_tracker.mark_finalize_failed("batch-1", long_message)

    table = boto3.resource("dynamodb", region_name=_REGION).Table("jstock-batch_runs")
    item = table.get_item(Key={"batch_id": "batch-1"})["Item"]
    assert len(item["finalize_error_message"]) == batch_tracker.MAX_FINALIZE_ERROR_MESSAGE_LENGTH


def test_mark_finalize_failed_is_noop_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(batch_tracker, "running_on_lambda", lambda: False)
    batch_tracker.mark_finalize_failed("batch-1", "boom")  # 例外を出さずに何もしない


def test_try_acquire_finalize_does_not_allow_retry_after_finalize_failed(
    moto_dynamodb: None,
) -> None:
    """通常の重複ワーカーはFINALIZE_FAILEDから再取得できない(終端状態として扱う)。"""
    batch_tracker.start_batch("batch-1", 3, _NOW)
    batch_tracker.try_acquire_finalize("batch-1")
    batch_tracker.mark_finalize_failed("batch-1", "boom")

    assert batch_tracker.try_acquire_finalize("batch-1") is False


def test_max_ranking_entries_capacity_is_documented_and_positive() -> None:
    """MAX_RANKING_ENTRY_BYTES x MAX_RANKING_ENTRIESがDynamoDB項目上限400KBに対し
    十分な余裕を持つことを回帰的に確認する(レビュー対応の算出根拠)。
    """
    assert batch_tracker.MAX_RANKING_ENTRIES > 0
    total_budget_bytes = batch_tracker.MAX_RANKING_ENTRY_BYTES * batch_tracker.MAX_RANKING_ENTRIES
    dynamodb_item_limit_bytes = 400_000
    assert total_budget_bytes < dynamodb_item_limit_bytes * 0.5


# --- complete_candidate: total_score/notification_detail(LINE通知品質改善) ------


@pytest.fixture
def moto_progress_dynamodb(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(batch_tracker, "running_on_lambda", lambda: True)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "jstock")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName="jstock-batch_runs",
            KeySchema=[{"AttributeName": "batch_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "batch_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="jstock-watchlist_candidate_progress",
            KeySchema=[
                {"AttributeName": "batch_id", "KeyType": "HASH"},
                {"AttributeName": "stock_code", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "batch_id", "AttributeType": "S"},
                {"AttributeName": "stock_code", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def _prepare_pending_row(batch_id: str, stock_code: str, owner_id: str) -> None:
    batch_tracker.try_acquire_dispatch_lease(batch_id, "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total(batch_id, 1, 72, _NOW)
    batch_tracker.create_missing_candidate_progress_rows(batch_id, [stock_code], _NOW, 72)
    batch_tracker.mark_dispatch_completed(batch_id, _NOW)
    batch_tracker.claim_candidate_lease(batch_id, stock_code, owner_id, _NOW, 240)


def test_complete_candidate_persists_total_score_for_non_passed_category(
    moto_progress_dynamodb: None,
) -> None:
    """total_scoreはPASSED以外(FAILED_SCORE等)でも保存される(修正①、
    evaluate()が実行された全銘柄で保存する設計)。"""
    _prepare_pending_row("batch-1", "1111", "owner-a")

    batch_tracker.complete_candidate(
        "batch-1",
        "1111",
        "owner-a",
        terminal_status=batch_tracker.WatchlistProgressStatus.COMPLETED,
        evaluation_result="FAILED_SCORE",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=_NOW,
        total_score=45.5,
    )

    records = batch_tracker.query_all_candidate_progress("batch-1", consistent_read=True)
    assert records[0].total_score == pytest.approx(45.5)
    assert records[0].notification_detail is None


def test_complete_candidate_persists_notification_detail_as_model(
    moto_progress_dynamodb: None,
) -> None:
    """notification_detailはWatchlistScoreDetailのまま渡し、内部でJSON化・
    復元される(呼び出し元はJSON文字列を一切扱わない)。"""
    from jstock_advisor.domain.signals.watchlist_screening import (
        ScoreCriterionValue,
        WatchlistScoreDetail,
    )

    _prepare_pending_row("batch-1", "1111", "owner-a")
    detail = WatchlistScoreDetail(
        stock_code="1111",
        criteria=[
            ScoreCriterionValue(
                criterion_key="dividend_yield", label="配当利回り", score=30.0, metric_value="6.6%"
            )
        ],
    )

    batch_tracker.complete_candidate(
        "batch-1",
        "1111",
        "owner-a",
        terminal_status=batch_tracker.WatchlistProgressStatus.COMPLETED,
        evaluation_result="PASSED",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=_NOW,
        total_score=87.0,
        notification_detail=detail,
    )

    records = batch_tracker.query_all_candidate_progress("batch-1", consistent_read=True)
    restored = records[0].notification_detail
    assert restored is not None
    assert restored.stock_code == "1111"
    assert restored.criteria[0].criterion_key == "dividend_yield"
    assert restored.criteria[0].metric_value == "6.6%"


def test_complete_candidate_without_total_score_leaves_it_none(
    moto_progress_dynamodb: None,
) -> None:
    """total_scoreを渡さない場合(NOT_FOUND/DATA_ERROR等、evaluate()が
    実行されなかった銘柄)はNoneのまま保存される。"""
    _prepare_pending_row("batch-1", "1111", "owner-a")

    batch_tracker.complete_candidate(
        "batch-1",
        "1111",
        "owner-a",
        terminal_status=batch_tracker.WatchlistProgressStatus.COMPLETED,
        evaluation_result="NOT_FOUND",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=_NOW,
    )

    records = batch_tracker.query_all_candidate_progress("batch-1", consistent_read=True)
    assert records[0].total_score is None
