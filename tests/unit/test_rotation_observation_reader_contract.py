"""資金入替の観測の read-only reader の契約テスト(Issue #604 P-b2。dormant)。

対象: `infrastructure/aws/rotation_observation_reader`。

設計: #604 P-b 設計 rev6(…6169320796)・rev8(…6169691749)・設計レビュー PASS(…6169650442)。
★ 実際の書き手(`batch_tracker`・`HoldingEvaluationRecordRepository`・
`RecommendationRepository`)で moto に書いた項目を読む ★(fake の形を信じない)。

  1  allowlist: get_item・batch_get_item・query だけ。scan・書込・describe は拒否
  2  BatchRuns: 開始・完了確認の時刻 / 行なし・壊れた行・通信の失敗 = 読めない
  3  ★ 保有の記録の範囲指定 Query ★: UTC の境界・両端を含む・1 マイクロ秒外・窓外・UTC でない行
  4  Recommendation の BatchGetItem: 100 件の chunk・UnprocessedKeys の再送と上限
     (失敗は『存在しない』と混同しない)
  5  実際の書き手 → reader → 入力の組み立て → P-b1 の観測(通し)
  6  構造(AST): scan・書込を呼ばない・boto3 は遅延 import・環境変数と時計を読まない・dormant
"""

from __future__ import annotations

import ast
import datetime as dt
import json
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holding_evaluation_record import (
    HoldingEvaluationRecord,
    build_holding_evaluation_id,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.rotation_observation import (
    BatchCompletionState,
    BatchFacts,
    DayStatus,
    judge_batch_completion,
    observe_day,
)
from jstock_advisor.domain.signals.rotation_observation_reads import RecordStatus
from jstock_advisor.domain.signals.rotation_observation_schedule import holdings_batch_id
from jstock_advisor.domain.signals.rotation_source_eligibility import (
    EligibilityWindow,
    RecommendationFacts,
    facts_from_record,
)
from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.infrastructure.aws import rotation_observation_reader as module
from jstock_advisor.infrastructure.aws.audit_shadow_reader import ReadOnlyViolationError
from jstock_advisor.infrastructure.aws.rotation_observation_reader import (
    ALLOWED_OPERATIONS,
    BATCH_GET_CHUNK,
    MAX_BATCH_RETRIES,
    MAX_QUERY_PAGES,
    HoldingRecordReader,
    RecommendationReader,
    RotationReadOnlyClient,
    read_batch_facts,
)
from jstock_advisor.infrastructure.collection_store import resolve_table_name
from jstock_advisor.infrastructure.local_repository.holding_evaluation_record_repository import (
    HoldingEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.rotation_observation_inputs import assemble_holding_inputs
from tests.unit import test_allocation_shadow_compute_contract as compute_tests
from tests.unit.test_rotation_observation_contract import _importers_of

_REPO = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO / "src" / "jstock_advisor"
_MODULE_PATH = _PACKAGE / "infrastructure" / "aws" / "rotation_observation_reader.py"

_REGION = "ap-northeast-1"
_JST = dt.timezone(dt.timedelta(hours=9))
_DAY = dt.date(2026, 10, 14)
_BATCH_ID = holdings_batch_id(_DAY)
_START = dt.datetime(2026, 10, 14, 8, 0, 41, 500000, tzinfo=_JST)
_END = _START + dt.timedelta(minutes=2)
_WINDOW = EligibilityWindow(_START, _END)
_INSIDE = _START + dt.timedelta(seconds=30)
_SENTINEL = "SENTINEL-OWNER-CODE-4242"

_BATCH_FILE = "batch_runs.json"
_RECORDS_FILE = "holding_evaluation_records.json"
_RECS_FILE = "recommendations.json"


@pytest.fixture
def tables(
    monkeypatch: pytest.MonkeyPatch,
    lambda_runtime_env: None,
    create_collection_table: Callable[..., None],
) -> Iterator[Any]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        create_collection_table(_BATCH_FILE, "batch_id")
        create_collection_table(_RECS_FILE, "recommendation_id")
        boto3.client("dynamodb", region_name=_REGION).create_table(
            TableName=resolve_table_name(_RECORDS_FILE),
            KeySchema=[{"AttributeName": "holding_evaluation_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "holding_evaluation_id", "AttributeType": "S"},
                {"AttributeName": "holding_id", "AttributeType": "S"},
                {"AttributeName": "evaluated_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "holding_id-index",
                    "KeySchema": [
                        {"AttributeName": "holding_id", "KeyType": "HASH"},
                        {"AttributeName": "evaluated_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.client("dynamodb", region_name=_REGION)


def _client() -> RotationReadOnlyClient:
    return RotationReadOnlyClient(boto3.client("dynamodb", region_name=_REGION))


def _records_reader() -> HoldingRecordReader:
    return HoldingRecordReader(_client(), resolve_table_name(_RECORDS_FILE))


def _recs_reader(
    resolver: Callable[[Recommendation], str | None] = lambda _r: None,
    sleep: Callable[[float], None] = lambda _s: None,
) -> RecommendationReader:
    return RecommendationReader(_client(), resolve_table_name(_RECS_FILE), resolver, sleep)


# --- 実際の書き手 ------------------------------------------------------------------------------


def _write_batch(batch_id: str, started: dt.datetime, completed: dt.datetime | None) -> None:
    started_utc = started.astimezone(dt.UTC)
    assert batch_tracker.start_batch(
        batch_id,
        1,
        started_utc,
        batch_tracker.BatchFamily.HOLDINGS_WATCHLIST,
        ExecutionContext.normal(),
        holding_count=1,
    )
    if completed is None:
        return
    batch_tracker.record_result(batch_id, "hold", stock_code="owner-a#0001")
    done = completed.astimezone(dt.UTC)
    token = batch_tracker.try_acquire_completion_finalize(batch_id, done)
    assert token is not None
    assert batch_tracker.mark_completion_finalize_completed(batch_id, token, done)


def _save_record(
    holding_id: str, evaluated_at: dt.datetime, **fields: Any
) -> HoldingEvaluationRecord:
    owner, _, code = holding_id.partition("#")
    values: dict[str, Any] = {
        "holding_evaluation_id": build_holding_evaluation_id(holding_id, evaluated_at),
        "holding_id": holding_id,
        "owner": owner,
        "stock_code": code,
        "evaluated_at": evaluated_at,
        "rule_version": "test",
        "authoritative_engine": "PROFIT_TAKING",
        "authoritative_outcome_category": "hold",
        "legacy_sell_ran": True,
        "profit_taking_ran": True,
    }
    values.update(fields)
    record = HoldingEvaluationRecord(**values)
    HoldingEvaluationRecordRepository().save(record)
    return record


def _recommendation(code: str, kind: RecommendationType) -> Recommendation:
    return compute_tests.rec(code, recommendation_type=kind, buy_action=None)


def _save_recommendation(code: str, kind: RecommendationType) -> Recommendation:
    recommendation = _recommendation(code, kind)
    RecommendationRepository().save(recommendation)
    return recommendation


_UTC = dt.UTC


def _utc(moment: dt.datetime) -> dt.datetime:
    return moment.astimezone(_UTC)


# =================================================================================================
# 1 allowlist
# =================================================================================================


@pytest.mark.parametrize(
    "operation",
    [
        "scan",
        "put_item",
        "update_item",
        "delete_item",
        "batch_write_item",
        "transact_write_items",
        "transact_get_items",
        "describe_table",
        "list_tables",
        "create_table",
        "delete_table",
        "invoke",
        "get_paginator",
        "meta",
    ],
)
def test_every_operation_outside_the_allowlist_is_refused(operation: str, tables: Any) -> None:
    client = _client()

    with pytest.raises(ReadOnlyViolationError):
        getattr(client, operation)


def test_the_allowlist_is_exactly_three_read_operations() -> None:
    assert frozenset({"get_item", "batch_get_item", "query"}) == ALLOWED_OPERATIONS
    assert "scan" not in ALLOWED_OPERATIONS


def test_the_allowed_operations_pass_through_to_the_real_client(tables: Any) -> None:
    client = _client()

    response = client.get_item(
        TableName=resolve_table_name(_BATCH_FILE), Key={"batch_id": {"S": "none"}}
    )

    assert "Item" not in response


# =================================================================================================
# 2 BatchRuns
# =================================================================================================


def _read_batch(batch_id: str = _BATCH_ID) -> BatchFacts | None:
    return read_batch_facts(_client(), resolve_table_name(_BATCH_FILE), batch_id)


def test_a_complete_batch_row_is_read_with_both_times_in_utc(tables: Any) -> None:
    _write_batch(_BATCH_ID, _START, _END)

    facts = _read_batch()

    assert facts is not None
    assert facts.started_at == _START and facts.completed_at == _END
    assert facts.started_at is not None and facts.started_at.utcoffset() == dt.timedelta(0)


def test_an_incomplete_batch_row_has_no_completion_time(tables: Any) -> None:
    _write_batch(_BATCH_ID, _START, None)

    facts = _read_batch()

    assert facts == BatchFacts(_START, None)
    assert judge_batch_completion(facts, _DAY).state is BatchCompletionState.NOT_COMPLETE


def test_a_complete_row_is_judged_complete_with_the_window_the_writer_wrote(tables: Any) -> None:
    _write_batch(_BATCH_ID, _START, _END)

    judgement = judge_batch_completion(_read_batch(), _DAY)

    assert judgement.state is BatchCompletionState.COMPLETE
    assert judgement.window == _WINDOW


def test_a_missing_row_is_unreadable(tables: Any) -> None:
    assert _read_batch("holdings-watchlist-20250101T080000") is None
    assert judge_batch_completion(_read_batch(), _DAY).state is BatchCompletionState.UNREADABLE


def test_a_manual_run_row_with_a_random_suffix_is_never_fetched(tables: Any) -> None:
    _write_batch(f"{_BATCH_ID}-3fa9c2d1", _START, _END)

    assert _read_batch() is None


@pytest.mark.parametrize(
    "item",
    [
        {"batch_id": {"S": _BATCH_ID}, "started_at": {"S": "not a time"}},
        {"batch_id": {"S": _BATCH_ID}, "started_at": {"S": "2026-10-14T08:00:00"}},  # offset なし
        {
            "batch_id": {"S": _BATCH_ID},
            "started_at": {"S": "2026-10-14T08:00:00+09:00"},
            "completion_finalize_completed_at": {"S": "garbage"},
        },
        {"batch_id": {"S": _BATCH_ID}, "started_at": {"N": "1"}},  # 型が違う = 欠落と同じ
    ],
)
def test_a_garbled_row_is_unreadable_never_complete(item: dict[str, Any], tables: Any) -> None:
    tables.put_item(TableName=resolve_table_name(_BATCH_FILE), Item=item)

    facts = _read_batch()

    assert judge_batch_completion(facts, _DAY).state is BatchCompletionState.UNREADABLE


def test_a_client_failure_is_unreadable_and_leaks_nothing() -> None:
    class _Broken:
        def get_item(self, **_kwargs: Any) -> Any:
            raise RuntimeError(_SENTINEL)

    assert read_batch_facts(_Broken(), "t", _SENTINEL) is None  # type: ignore[arg-type]


# =================================================================================================
# 3 保有の記録の範囲指定 Query
# =================================================================================================

_HOLDING = "owner-a#0001"


def _read(holding_id: str = _HOLDING, window: EligibilityWindow = _WINDOW) -> Any:
    return _records_reader().latest_in_window(holding_id, window)


def test_a_record_inside_the_window_is_found_with_the_facts_the_writer_wrote(tables: Any) -> None:
    saved = _save_record(_HOLDING, _utc(_INSIDE))

    read = _read()

    assert read.status is RecordStatus.FOUND
    assert read.facts == facts_from_record(saved)


def test_the_window_includes_both_ends(tables: Any) -> None:
    _save_record("owner-a#0001", _utc(_START))
    _save_record("owner-a#0002", _utc(_END))

    assert _read("owner-a#0001").status is RecordStatus.FOUND
    assert _read("owner-a#0002").status is RecordStatus.FOUND


def test_one_microsecond_outside_either_end_is_not_found(tables: Any) -> None:
    micro = dt.timedelta(microseconds=1)
    _save_record("owner-a#0001", _utc(_START) - micro)
    _save_record("owner-a#0002", _utc(_END) + micro)

    assert _read("owner-a#0001").status is RecordStatus.NOT_FOUND
    assert _read("owner-a#0002").status is RecordStatus.NOT_FOUND


def test_a_same_day_manual_run_outside_the_window_is_not_picked_up(tables: Any) -> None:
    manual = dt.datetime(2026, 10, 14, 12, 30, tzinfo=_JST)
    _save_record(_HOLDING, _utc(manual))

    assert _read().status is RecordStatus.NOT_FOUND


def test_the_latest_record_inside_the_window_wins(tables: Any) -> None:
    older = _save_record(_HOLDING, _utc(_START + dt.timedelta(seconds=10)))
    newer = _save_record(
        _HOLDING,
        _utc(_START + dt.timedelta(seconds=90)),
        authoritative_outcome_category="suppressed",
    )

    read = _read()

    assert read.facts == facts_from_record(newer) != facts_from_record(older)


def test_another_holdings_record_is_never_returned(tables: Any) -> None:
    _save_record("owner-b#0002", _utc(_INSIDE))

    assert _read(_HOLDING).status is RecordStatus.NOT_FOUND


def test_the_query_agrees_with_the_window_contains_over_a_grid_of_times(tables: Any) -> None:
    """★ 範囲指定 Query と P-a の `window.contains` が、すべての時刻で同じ結論 ★。

    端・マイクロ秒の有無を含む。
    """
    offsets = [
        dt.timedelta(seconds=s) + dt.timedelta(microseconds=u)
        for s in (-2, -1, 0, 1, 60, 119, 120, 121, 122)
        for u in (-1, 0, 1, 500000)
    ]
    for index, offset in enumerate(offsets):
        moment = _utc(_START) + offset
        _save_record(f"owner-g#{index:04d}", moment)

    for index, offset in enumerate(offsets):
        moment = _utc(_START) + offset
        expected = RecordStatus.FOUND if _WINDOW.contains(moment) else RecordStatus.NOT_FOUND
        assert _read(f"owner-g#{index:04d}").status is expected, offset


def test_a_window_given_in_jst_is_sent_as_utc_text(tables: Any) -> None:
    """窓は JST の aware。BETWEEN へは UTC の isoformat で渡る(JST の文字列だと辞書順が壊れる)。"""
    calls: list[dict[str, Any]] = []

    class _Spy:
        def query(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return {"Items": []}

    reader = HoldingRecordReader(_Spy(), "t")  # type: ignore[arg-type]
    reader.latest_in_window(_HOLDING, _WINDOW)

    values = calls[0]["ExpressionAttributeValues"]
    assert values[":s"] == {"S": _START.astimezone(dt.UTC).isoformat()}
    assert values[":e"] == {"S": _END.astimezone(dt.UTC).isoformat()}
    assert values[":s"]["S"].endswith("+00:00") and values[":e"]["S"].endswith("+00:00")
    assert calls[0]["IndexName"] == "holding_id-index"
    assert calls[0]["KeyConditionExpression"] == (
        "holding_id = :h AND evaluated_at BETWEEN :s AND :e"
    )


def _put_raw(holding_id: str, stamp: str, body_stamp: str | None = None, **overrides: Any) -> None:
    """local の書き方(値をそのまま書く)を再現する。本体の evaluated_at を別に指定できる。"""
    moment = dt.datetime.fromisoformat(body_stamp or stamp)
    record = HoldingEvaluationRecord(
        holding_evaluation_id=f"{holding_id}:{stamp}",
        holding_id=holding_id,
        owner=holding_id.partition("#")[0],
        stock_code=holding_id.partition("#")[2],
        evaluated_at=moment,
        rule_version="test",
        authoritative_outcome_category="hold",
    )
    item = {
        "holding_evaluation_id": {"S": record.holding_evaluation_id},
        "data": {"S": overrides.get("data", record.model_dump_json())},
        "holding_id": {"S": holding_id},
        "evaluated_at": {"S": stamp},
    }
    boto3.client("dynamodb", region_name=_REGION).put_item(
        TableName=resolve_table_name(_RECORDS_FILE), Item=item
    )


def test_a_row_not_written_in_utc_that_the_string_range_returns_is_a_read_failure(
    tables: Any,
) -> None:
    """文字列では窓の内側に見えるが UTC でない行(JST の isoformat)= 推測で解釈せず読取の失敗。"""
    _put_raw(_HOLDING, "2026-10-13T23:01:00+09:00")  # 文字列は窓(10/13 23:00〜23:02 UTC)の内側

    assert _read().status is RecordStatus.FAILED


def test_a_jst_row_outside_the_string_range_is_simply_not_returned_a_documented_limit(
    tables: Any,
) -> None:
    """★ 正直な限界 ★: 範囲外に見える JST の行は BETWEEN が返さない(数えられない)。
    AWS の書き手が UTC だけであることが前提で、下の契約テストが固定する。"""
    _put_raw(_HOLDING, "2026-10-14T08:01:00+09:00")  # 実際は窓の内側。文字列では外側

    assert _read().status is RecordStatus.NOT_FOUND


def test_a_body_whose_offset_is_not_zero_is_a_read_failure_even_when_the_attribute_says_utc(
    tables: Any,
) -> None:
    _put_raw(_HOLDING, "2026-10-13T23:01:00+00:00", body_stamp="2026-10-14T08:01:00+09:00")

    assert _read().status is RecordStatus.FAILED


def test_a_body_that_disagrees_with_the_window_is_a_read_failure(tables: Any) -> None:
    _put_raw(_HOLDING, "2026-10-13T23:01:00+00:00", body_stamp="2026-10-13T21:00:00+00:00")

    assert _read().status is RecordStatus.FAILED


@pytest.mark.parametrize("data", ["{not json", "{}", '{"holding_id": 5}', ""])
def test_a_corrupt_record_is_a_read_failure_not_an_exception(data: str, tables: Any) -> None:
    _put_raw(_HOLDING, "2026-10-13T23:01:00+00:00", data=data)

    assert _read().status is RecordStatus.FAILED


def test_one_bad_row_among_good_ones_fails_the_holding_conservatively(tables: Any) -> None:
    _save_record(_HOLDING, _utc(_START) + dt.timedelta(seconds=10))
    _put_raw(_HOLDING, "2026-10-13T23:01:00+09:00")

    assert _read().status is RecordStatus.FAILED


def test_the_production_writer_always_writes_utc_text(tables: Any) -> None:
    """★ 範囲指定が正しい前提の固定 ★: 実際の書き手が書く evaluated_at は UTC(+00:00)の文字列。"""
    _save_record(_HOLDING, _utc(_INSIDE))

    item = tables.query(
        TableName=resolve_table_name(_RECORDS_FILE),
        IndexName="holding_id-index",
        KeyConditionExpression="holding_id = :h",
        ExpressionAttributeValues={":h": {"S": _HOLDING}},
    )["Items"][0]

    assert item["evaluated_at"]["S"].endswith("+00:00")
    handler = (_PACKAGE / "lambda_handlers" / "holdings_watchlist_handler.py").read_text(
        encoding="utf-8"
    )
    assert "now = dt.datetime.now(dt.UTC)" in handler


def test_a_client_failure_is_a_read_failure_and_leaks_nothing() -> None:
    class _Broken:
        def query(self, **_kwargs: Any) -> Any:
            raise RuntimeError(_SENTINEL)

    read = HoldingRecordReader(_Broken(), "t").latest_in_window(_SENTINEL, _WINDOW)  # type: ignore[arg-type]

    assert read.status is RecordStatus.FAILED
    assert _SENTINEL not in repr(read)


def test_pages_are_followed_up_to_the_cap_and_beyond_it_the_read_fails() -> None:
    class _Endless:
        def __init__(self) -> None:
            self.calls = 0

        def query(self, **_kwargs: Any) -> Any:
            self.calls += 1
            return {"Items": [], "LastEvaluatedKey": {"k": {"S": "x"}}}

    class _TwoPages:
        def __init__(self) -> None:
            self.calls = 0

        def query(self, **kwargs: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                assert "ExclusiveStartKey" not in kwargs
                return {"Items": [], "LastEvaluatedKey": {"k": {"S": "x"}}}
            assert kwargs["ExclusiveStartKey"] == {"k": {"S": "x"}}
            return {"Items": []}

    endless, two = _Endless(), _TwoPages()

    assert HoldingRecordReader(endless, "t").latest_in_window("h", _WINDOW).status is (  # type: ignore[arg-type]
        RecordStatus.FAILED
    )
    assert endless.calls == MAX_QUERY_PAGES
    assert HoldingRecordReader(two, "t").latest_in_window("h", _WINDOW).status is (  # type: ignore[arg-type]
        RecordStatus.NOT_FOUND
    )
    assert two.calls == 2


# =================================================================================================
# 4 Recommendation の BatchGetItem
# =================================================================================================


def test_recommendations_are_read_into_facts_with_the_injected_resolver(tables: Any) -> None:
    _save_recommendation("0001", RecommendationType.HOLD)
    _save_recommendation("0002", RecommendationType.SELL)
    seen: list[str] = []

    def resolver(recommendation: Recommendation) -> str | None:
        seen.append(recommendation.recommendation_id)
        return "PROFIT_PROTECTION_CANDIDATE" if recommendation.stock_code == "0002" else None

    ids = [
        _recommendation("0001", RecommendationType.HOLD).recommendation_id,
        _recommendation("0002", RecommendationType.SELL).recommendation_id,
    ]

    result = _recs_reader(resolver).get_many(ids)

    assert result.found[ids[0]] == RecommendationFacts("HOLD", None)
    assert result.found[ids[1]] == RecommendationFacts("SELL", "PROFIT_PROTECTION_CANDIDATE")
    assert sorted(seen) == sorted(ids) and not result.failed


def test_an_id_that_does_not_exist_is_neither_found_nor_failed(tables: Any) -> None:
    result = _recs_reader().get_many(["no-such-id"])

    assert not result.found and not result.failed


class _CountingClient:
    """実際の moto の client を包み、batch_get_item の呼び出しの鍵の数を数える。"""

    def __init__(self) -> None:
        self._real = boto3.client("dynamodb", region_name=_REGION)
        self.key_counts: list[int] = []

    def batch_get_item(self, **kwargs: Any) -> Any:
        (request,) = kwargs["RequestItems"].values()
        self.key_counts.append(len(request["Keys"]))
        return self._real.batch_get_item(**kwargs)


def test_ids_are_read_in_chunks_of_one_hundred(tables: Any) -> None:
    for number in range(250):
        code = f"{number:04d}"
        RecommendationRepository().save(_recommendation(code, RecommendationType.HOLD))
    ids = [
        _recommendation(f"{n:04d}", RecommendationType.HOLD).recommendation_id for n in range(250)
    ]
    counting = _CountingClient()
    reader = RecommendationReader(
        counting,
        resolve_table_name(_RECS_FILE),
        lambda _r: None,
        lambda _s: None,  # type: ignore[arg-type]
    )

    result = reader.get_many(ids)

    assert counting.key_counts == [BATCH_GET_CHUNK, BATCH_GET_CHUNK, 50]
    assert len(result.found) == 250 and not result.failed


@pytest.mark.parametrize("count", [99, 100, 101])
def test_the_chunk_boundary_never_drops_or_duplicates_an_id(count: int, tables: Any) -> None:
    for number in range(count):
        RecommendationRepository().save(_recommendation(f"{number:04d}", RecommendationType.HOLD))
    ids = [
        _recommendation(f"{n:04d}", RecommendationType.HOLD).recommendation_id for n in range(count)
    ]

    result = _recs_reader().get_many(ids)

    assert set(result.found) == set(ids)


def test_duplicate_ids_are_requested_once(tables: Any) -> None:
    _save_recommendation("0001", RecommendationType.HOLD)
    only = _recommendation("0001", RecommendationType.HOLD).recommendation_id
    counting = _CountingClient()
    reader = RecommendationReader(
        counting,
        resolve_table_name(_RECS_FILE),
        lambda _r: None,
        lambda _s: None,  # type: ignore[arg-type]
    )

    reader.get_many([only, only, only])

    assert counting.key_counts == [1]


class _UnprocessedClient:
    """最初の `unprocessed_rounds` 回は全部を UnprocessedKeys で返し、その後は全件を返す。"""

    def __init__(self, table: str, items: dict[str, Any], rounds: int) -> None:
        self._table = table
        self._items = items
        self._rounds = rounds
        self.calls = 0

    def batch_get_item(self, **kwargs: Any) -> Any:
        self.calls += 1
        keys = kwargs["RequestItems"][self._table]["Keys"]
        if self.calls <= self._rounds:
            return {
                "Responses": {self._table: []},
                "UnprocessedKeys": {self._table: {"Keys": keys}},
            }
        found = [
            self._items[k["recommendation_id"]["S"]]
            for k in keys
            if k["recommendation_id"]["S"] in self._items
        ]
        return {"Responses": {self._table: found}, "UnprocessedKeys": {}}


def _item_of(recommendation: Recommendation) -> dict[str, Any]:
    return {
        "recommendation_id": {"S": recommendation.recommendation_id},
        "data": {"S": recommendation.model_dump_json()},
    }


def test_unprocessed_keys_are_retried_with_a_deterministic_backoff() -> None:
    recommendation = _recommendation("0001", RecommendationType.HOLD)
    client = _UnprocessedClient(
        "t", {recommendation.recommendation_id: _item_of(recommendation)}, 3
    )
    sleeps: list[float] = []
    reader = RecommendationReader(client, "t", lambda _r: None, sleeps.append)  # type: ignore[arg-type]

    result = reader.get_many([recommendation.recommendation_id])

    assert recommendation.recommendation_id in result.found
    assert sleeps == [0.5, 1.0, 2.0]


def test_when_the_retries_run_out_the_ids_are_failed_not_missing() -> None:
    """★ 取れなかったことを『存在しない』と混同しない ★。再送の上限は 5 回、遅延の上限は 5 秒。"""
    recommendation = _recommendation("0001", RecommendationType.HOLD)
    client = _UnprocessedClient(
        "t", {recommendation.recommendation_id: _item_of(recommendation)}, 99
    )
    sleeps: list[float] = []
    reader = RecommendationReader(client, "t", lambda _r: None, sleeps.append)  # type: ignore[arg-type]

    result = reader.get_many([recommendation.recommendation_id])

    assert result.failed == frozenset({recommendation.recommendation_id}) and not result.found
    assert client.calls == MAX_BATCH_RETRIES + 1
    assert sleeps == [0.5, 1.0, 2.0, 4.0, 5.0]


def test_a_failing_chunk_fails_only_its_ids() -> None:
    items = {
        f"r{n}": _item_of(_recommendation(f"{n:04d}", RecommendationType.HOLD)) for n in range(150)
    }
    # id を実際の主キーに合わせる
    by_id = {
        _recommendation(f"{n:04d}", RecommendationType.HOLD).recommendation_id: _item_of(
            _recommendation(f"{n:04d}", RecommendationType.HOLD)
        )
        for n in range(150)
    }
    del items

    class _FailsSecond:
        def __init__(self) -> None:
            self.calls = 0

        def batch_get_item(self, **kwargs: Any) -> Any:
            self.calls += 1
            keys = kwargs["RequestItems"]["t"]["Keys"]
            if self.calls == 2:
                raise RuntimeError(_SENTINEL)
            return {"Responses": {"t": [by_id[k["recommendation_id"]["S"]] for k in keys]}}

    ids = list(by_id)
    reader = RecommendationReader(_FailsSecond(), "t", lambda _r: None, lambda _s: None)  # type: ignore[arg-type]

    result = reader.get_many(ids)

    assert set(result.found) == set(ids[:100])
    assert result.failed == frozenset(ids[100:])
    assert _SENTINEL not in repr(result)


@pytest.mark.parametrize("data", ["{not json", "{}", ""])
def test_a_corrupt_recommendation_fails_only_that_id(data: str, tables: Any) -> None:
    good = _save_recommendation("0001", RecommendationType.HOLD)
    tables.put_item(
        TableName=resolve_table_name(_RECS_FILE),
        Item={"recommendation_id": {"S": "bad"}, "data": {"S": data}},
    )

    result = _recs_reader().get_many([good.recommendation_id, "bad"])

    assert good.recommendation_id in result.found
    assert result.failed == frozenset({"bad"})


def test_a_resolver_that_raises_fails_that_id_only(tables: Any) -> None:
    first = _save_recommendation("0001", RecommendationType.HOLD)
    second = _save_recommendation("0002", RecommendationType.HOLD)

    def resolver(recommendation: Recommendation) -> str | None:
        if recommendation.stock_code == "0002":
            raise RuntimeError(_SENTINEL)
        return None

    result = _recs_reader(resolver).get_many([first.recommendation_id, second.recommendation_id])

    assert set(result.found) == {first.recommendation_id}
    assert result.failed == frozenset({second.recommendation_id})
    assert _SENTINEL not in repr(result)


# =================================================================================================
# 5 通し: 実際の書き手 → reader → 入力の組み立て → P-b1 の観測
# =================================================================================================


class _Ids:
    def __init__(self, ids: Sequence[str]) -> None:
        self._ids = ids

    def holding_ids(self) -> Sequence[str]:
        return self._ids


def test_the_whole_path_from_the_real_writers_to_a_day_observation(tables: Any) -> None:
    _write_batch(_BATCH_ID, _START, _END)
    hold = _save_recommendation("0001", RecommendationType.HOLD)
    sell = _save_recommendation("0002", RecommendationType.SELL)
    _save_record(
        "owner-a#0001", _utc(_INSIDE), authoritative_recommendation_id=hold.recommendation_id
    )
    _save_record(
        "owner-a#0002", _utc(_INSIDE), authoritative_recommendation_id=sell.recommendation_id
    )
    _save_record("owner-a#0003", _utc(_INSIDE))
    _save_record("owner-a#0004", _utc(dt.datetime(2026, 10, 14, 12, 30, tzinfo=_JST)))  # 手動実行
    # owner-a#0005 は記録なし
    batch = _read_batch()
    judgement = judge_batch_completion(batch, _DAY)
    assert judgement.window is not None

    inputs = assemble_holding_inputs(
        window=judgement.window,
        holding_ids=_Ids([f"owner-a#000{n}" for n in range(1, 6)]),
        records=_records_reader(),
        recommendations=_recs_reader(),
        remaining_seconds=lambda: 1000.0,
        reserve_seconds=10.0,
    )
    observation = observe_day(target_date=_DAY, batch=batch, holdings=inputs)

    assert observation.day_status is DayStatus.OBSERVED
    assert observation.holdings_total == 5
    assert observation.reasons["NO_EXIT_EVIDENCE"] == 2  # HOLD の推奨 + 推奨なし
    assert observation.reasons["EXIT_RECOMMENDATION"] == 1
    assert observation.reasons["OUTSIDE_WINDOW"] == 0
    assert observation.reasons["RECORD_MISSING"] == 2  # 手動実行(窓の外)+ 記録なし
    assert observation.eligibility == {"ELIGIBLE": 2, "BLOCKED_BY_EXIT": 1, "UNKNOWN": 2}
    assert json.dumps(dict(observation.reasons)).count("owner") == 0


# =================================================================================================
# 6 構造(AST)・dormant
# =================================================================================================


def _tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def _imports() -> list[str]:
    imported: list[str] = []
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
    return imported


def _called_attributes() -> set[str]:
    return {
        node.func.attr
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_the_module_never_calls_scan_or_any_write_or_describe_operation() -> None:
    called = _called_attributes()

    assert not called & {
        "scan",
        "put_item",
        "update_item",
        "delete_item",
        "batch_write_item",
        "transact_write_items",
        "describe_table",
        "invoke",
        "publish",
    }
    assert called & {"get_item", "query", "batch_get_item"} == {
        "get_item",
        "query",
        "batch_get_item",
    }


def test_the_module_does_not_import_a_store_repository_or_service_that_can_write() -> None:
    imported = _imports()

    for forbidden in (
        "collection_store",
        "dynamodb_store",
        "local_repository",
        "audit_service",
        "batch_tracker",
        "line",
        "services",
    ):
        assert not [m for m in imported if forbidden in m], forbidden


def test_boto3_is_imported_lazily_inside_a_function_only() -> None:
    top_level = [node for node in _tree().body if isinstance(node, (ast.Import, ast.ImportFrom))]

    assert not [
        node
        for node in top_level
        if (isinstance(node, ast.Import) and any(a.name.startswith("boto") for a in node.names))
        or (isinstance(node, ast.ImportFrom) and (node.module or "").startswith("boto"))
    ]
    assert any(
        isinstance(inner, ast.Import) and any(a.name == "boto3" for a in inner.names)
        for outer in ast.walk(_tree())
        if isinstance(outer, ast.FunctionDef)
        for inner in ast.walk(outer)
    )


def test_the_module_reads_no_environment_no_clock_and_takes_names_and_sleep_by_argument() -> None:
    names = {node.id for node in ast.walk(_tree()) if isinstance(node, ast.Name)}
    called = _called_attributes()

    assert not names & {"print", "open", "input", "logger", "logging", "environ", "os", "time"}
    assert not called & {"now", "utcnow", "today", "sleep", "getenv", "read_text"}
    assert "time" not in {name.split(".")[0] for name in _imports()}
    assert "os" not in {name.split(".")[0] for name in _imports()}
    source = _MODULE_PATH.read_text(encoding="utf-8")
    assert "TABLE_PREFIX" not in source and "resolve_table_name" not in source


def test_no_module_other_than_the_tests_imports_the_reader_yet() -> None:
    """dormant: P-b3・P-b4 の配線まで、どこからも import されない。"""
    assert _importers_of("rotation_observation_reader", _MODULE_PATH) == []


def test_the_module_is_importable_by_name() -> None:
    assert module.HOLDING_ID_INDEX_NAME == "holding_id-index"
