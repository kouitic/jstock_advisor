"""候補ユニバース本格対応(第6版修正プラン)のbatch_tracker.py新規関数群のテスト。

実際のDynamoDBのConditionExpression/TransactWriteItems意味論を検証する必要が
あるため、手組みフェイクではなくmoto(mock_aws)で実テーブルを作成して検証する
(既存tests/unit/test_dynamodb_store.pyと同じパターン)。
"""

from __future__ import annotations

import datetime as dt
import json

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from moto import mock_aws

from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.infrastructure.aws.batch_tracker import (
    WatchlistBatchStatus,
    WatchlistProgressStatus,
)

_REGION = "ap-northeast-1"
_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)
_BATCH_TABLE = "jstock-batch_runs"
_PROGRESS_TABLE = "jstock-watchlist_candidate_progress"


@pytest.fixture
def dynamo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_BATCH_TABLE,
            KeySchema=[{"AttributeName": "batch_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "batch_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName=_PROGRESS_TABLE,
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
        yield client


# --- 1節/18節: dispatch lease --------------------------------------------------


def test_try_acquire_dispatch_lease_succeeds_on_first_call(dynamo) -> None:
    ok = batch_tracker.try_acquire_dispatch_lease("batch-1", "owner-a", _NOW, 360, 72)
    assert ok is True
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["status"] == WatchlistBatchStatus.DISPATCHING.value
    assert item["dispatch_owner_id"] == "owner-a"


def test_try_acquire_dispatch_lease_rejects_concurrent_owner_while_valid(dynamo) -> None:
    batch_tracker.try_acquire_dispatch_lease("batch-1", "owner-a", _NOW, 360, 72)
    ok = batch_tracker.try_acquire_dispatch_lease("batch-1", "owner-b", _NOW, 360, 72)
    assert ok is False
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["dispatch_owner_id"] == "owner-a"


def test_try_acquire_dispatch_lease_allows_reclaim_after_expiry(dynamo) -> None:
    batch_tracker.try_acquire_dispatch_lease("batch-1", "owner-a", _NOW, 360, 72)
    later = _NOW + dt.timedelta(seconds=361)
    ok = batch_tracker.try_acquire_dispatch_lease("batch-1", "owner-b", later, 360, 72)
    assert ok is True
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["dispatch_owner_id"] == "owner-b"


def test_try_acquire_dispatch_lease_preserves_started_at_across_reacquire(dynamo) -> None:
    """18節: 再開時にタイムアウト判定の起点(started_at)がリセットされないこと。"""
    batch_tracker.try_acquire_dispatch_lease("batch-1", "owner-a", _NOW, 360, 72)
    later = _NOW + dt.timedelta(seconds=361)
    batch_tracker.try_acquire_dispatch_lease("batch-1", "owner-b", later, 360, 72)
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["started_at"] == _NOW.isoformat()


def test_try_acquire_dispatch_lease_rejects_once_status_moved_past_dispatching(dynamo) -> None:
    batch_tracker.try_acquire_dispatch_lease("batch-1", "owner-a", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 3, 72, _NOW)
    batch_tracker.mark_dispatch_completed("batch-1", _NOW)
    ok = batch_tracker.try_acquire_dispatch_lease("batch-1", "owner-c", _NOW, 360, 72)
    assert ok is False


# --- 13/18節: 進捗行の差分作成 --------------------------------------------------


def test_create_missing_candidate_progress_rows_creates_only_missing(dynamo) -> None:
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111", "2222"], _NOW, 72)
    rows = batch_tracker.query_all_candidate_progress("batch-1", consistent_read=True)
    assert {r.stock_code for r in rows} == {"1111", "2222"}
    assert all(r.status == WatchlistProgressStatus.PENDING.value for r in rows)

    # 既存行(1111)がPROCESSINGへ進んでいても、同じコードを含む再実行で上書きされない。
    batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-a", _NOW, 240)
    batch_tracker.create_missing_candidate_progress_rows(
        "batch-1", ["1111", "2222", "3333"], _NOW, 72
    )
    rows_after = {
        r.stock_code: r
        for r in batch_tracker.query_all_candidate_progress("batch-1", consistent_read=True)
    }
    assert set(rows_after) == {"1111", "2222", "3333"}
    assert rows_after["1111"].status == WatchlistProgressStatus.PROCESSING.value
    assert rows_after["3333"].status == WatchlistProgressStatus.PENDING.value


# --- 12節: dispatched更新の競合解消 ---------------------------------------------


def test_mark_candidate_dispatched_succeeds_even_if_status_already_processing(dynamo) -> None:
    """12節: Workerが先にPROCESSINGへ遷移していてもdispatched=trueを記録できること。"""
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111"], _NOW, 72)
    batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-a", _NOW, 240)

    batch_tracker.mark_candidate_dispatched("batch-1", "1111", _NOW)

    rows = batch_tracker.query_all_candidate_progress("batch-1", consistent_read=True)
    row = next(r for r in rows if r.stock_code == "1111")
    assert row.dispatched is True
    assert row.status == WatchlistProgressStatus.PROCESSING.value  # 上書きされていない


def test_mark_candidate_dispatched_is_idempotent(dynamo) -> None:
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111"], _NOW, 72)
    batch_tracker.mark_candidate_dispatched("batch-1", "1111", _NOW)
    batch_tracker.mark_candidate_dispatched("batch-1", "1111", _NOW)  # 例外を出さない
    rows = batch_tracker.query_all_candidate_progress("batch-1", consistent_read=True)
    assert rows[0].dispatched is True


# --- 7節: PROCESSINGリース ------------------------------------------------------


def test_claim_candidate_lease_rejects_while_another_lease_is_valid(dynamo) -> None:
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111"], _NOW, 72)
    assert batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-a", _NOW, 240) is True
    assert batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-b", _NOW, 240) is False


def test_claim_candidate_lease_allows_reclaim_after_expiry(dynamo) -> None:
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111"], _NOW, 72)
    batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-a", _NOW, 240)
    later = _NOW + dt.timedelta(seconds=241)
    assert batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-b", later, 240) is True


# --- 7/11節: 通常完了経路(TransactWriteItems) -----------------------------------


def test_complete_candidate_increments_completed_and_sets_terminal_fields(dynamo) -> None:
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 1, 72, _NOW)
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111"], _NOW, 72)
    batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-a", _NOW, 240)

    ok = batch_tracker.complete_candidate(
        "batch-1",
        "1111",
        "owner-a",
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result="PASSED",
        ranking_entry='{"stock_code": "1111"}',
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=1500,
        now=_NOW,
    )
    assert ok is True

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert int(batch["completed"]) == 1

    rows = batch_tracker.query_all_candidate_progress("batch-1", consistent_read=True)
    row = rows[0]
    assert row.status == WatchlistProgressStatus.COMPLETED.value
    assert row.evaluation_result == "PASSED"
    assert row.ranking_entry == '{"stock_code": "1111"}'
    assert row.total_processing_duration_ms == 1500
    assert row.lease_owner_id is None  # REMOVEされている


def test_complete_candidate_fails_when_owner_does_not_match(dynamo) -> None:
    """7節: リース失効後に別Workerが再クレームしていた場合、失効前のWorkerが
    完了させようとしても条件不成立で失敗すること(completedの二重加算防止)。"""
    batch_tracker.set_watchlist_batch_total("batch-1", 1, 72, _NOW)
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111"], _NOW, 72)
    batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-a", _NOW, 240)
    later = _NOW + dt.timedelta(seconds=241)
    batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-b", later, 240)

    ok = batch_tracker.complete_candidate(
        "batch-1",
        "1111",
        "owner-a",  # 失効前のオーナー
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result="PASSED",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=1000,
        now=later,
    )
    assert ok is False
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert int(batch.get("completed", 0)) == 0  # 加算されていない


# --- 1節: Dispatcher送信失敗/4節: SQS終端失敗 -----------------------------------


def test_record_dispatch_send_failure_marks_failed_and_increments_completed(dynamo) -> None:
    batch_tracker.set_watchlist_batch_total("batch-1", 1, 72, _NOW)
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111"], _NOW, 72)

    ok = batch_tracker.record_dispatch_send_failure("batch-1", "1111", _NOW)
    assert ok is True

    rows = batch_tracker.query_all_candidate_progress("batch-1", consistent_read=True)
    assert rows[0].status == WatchlistProgressStatus.FAILED.value
    assert rows[0].evaluation_result == batch_tracker.EVALUATION_RESULT_DISPATCH_SEND_FAILED
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert int(batch["completed"]) == 1


def test_record_terminal_failure_marks_failed_from_processing(dynamo) -> None:
    batch_tracker.set_watchlist_batch_total("batch-1", 1, 72, _NOW)
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111"], _NOW, 72)
    batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-a", _NOW, 240)

    ok = batch_tracker.record_terminal_failure("batch-1", "1111", _NOW)
    assert ok is True
    rows = batch_tracker.query_all_candidate_progress("batch-1", consistent_read=True)
    assert rows[0].status == WatchlistProgressStatus.FAILED.value
    assert rows[0].evaluation_result == batch_tracker.EVALUATION_RESULT_SQS_MAX_RECEIVE_EXCEEDED


def test_record_terminal_failure_is_idempotent_when_already_terminal(dynamo) -> None:
    batch_tracker.set_watchlist_batch_total("batch-1", 1, 72, _NOW)
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111"], _NOW, 72)
    batch_tracker.record_dispatch_send_failure("batch-1", "1111", _NOW)

    ok = batch_tracker.record_terminal_failure("batch-1", "1111", _NOW)
    assert ok is False
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert int(batch["completed"]) == 1  # 二重加算されていない


# --- 11節: finalize起動の共通化 -------------------------------------------------


def test_try_finalize_if_ready_false_until_dispatch_completed_and_all_done(dynamo) -> None:
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 1, 72, _NOW)
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111"], _NOW, 72)
    batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-a", _NOW, 240)
    batch_tracker.complete_candidate(
        "batch-1",
        "1111",
        "owner-a",
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result="PASSED",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=_NOW,
    )

    # completed>=totalだが、dispatch_completedがまだfalse(status=DISPATCHING)。
    assert batch_tracker.try_finalize_if_ready("batch-1", _NOW) is False

    batch_tracker.mark_dispatch_completed("batch-1", _NOW)
    assert batch_tracker.try_finalize_if_ready("batch-1", _NOW) is True

    # 一度FINALIZINGへ遷移した後は、再度呼んでもFalse(排他制御)。
    assert batch_tracker.try_finalize_if_ready("batch-1", _NOW) is False


def test_try_finalize_if_ready_only_one_winner_among_concurrent_callers(dynamo) -> None:
    """11節: 複数の完了主体が同時にtry_finalize_if_readyを呼んでも1回だけ成功すること。"""
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 1, 72, _NOW)
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111"], _NOW, 72)
    batch_tracker.mark_dispatch_completed("batch-1", _NOW)
    batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-a", _NOW, 240)
    batch_tracker.complete_candidate(
        "batch-1",
        "1111",
        "owner-a",
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result="PASSED",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=_NOW,
    )

    results = [batch_tracker.try_finalize_if_ready("batch-1", _NOW) for _ in range(5)]
    assert results.count(True) == 1


# --- 17節: タイムアウト確定処理(TIMEOUT_FINALIZING、案C) -----------------------


def test_try_acquire_timeout_finalization_from_running_and_failed_states(dynamo) -> None:
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 1, 72, _NOW)
    batch_tracker.mark_dispatch_completed("batch-1", _NOW)

    assert batch_tracker.try_acquire_timeout_finalization("batch-1") is True
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["status"] == WatchlistBatchStatus.TIMEOUT_FINALIZING.value

    # RUNNINGでなくなった後は再取得できない。
    assert batch_tracker.try_acquire_timeout_finalization("batch-1") is False

    # TIMEOUT_FINALIZE_FAILEDからは再取得できる。
    batch_tracker.transition_timeout_finalizing_to_failed("batch-1", _NOW, "boom")
    assert batch_tracker.try_acquire_timeout_finalization("batch-1") is True


def test_run_timeout_finalization_pass_preserves_completed_rows_and_fails_incomplete(
    dynamo,
) -> None:
    """17節: 既にCOMPLETED/FAILEDの行は変更せず、PENDING/PROCESSINGのみFAILED確定する。"""
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 3, 72, _NOW)
    batch_tracker.create_missing_candidate_progress_rows(
        "batch-1", ["1111", "2222", "3333"], _NOW, 72
    )
    batch_tracker.mark_dispatch_completed("batch-1", _NOW)

    # 1111は既に合格・完了済み(ranking_entryとattempt_countを保持していることを確認する)。
    batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-a", _NOW, 240)
    batch_tracker.complete_candidate(
        "batch-1",
        "1111",
        "owner-a",
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result="PASSED",
        ranking_entry='{"stock_code": "1111"}',
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=2000,
        now=_NOW,
    )
    # 2222はPROCESSING中のまま(リース未失効)。3333は未着手(PENDING)。
    batch_tracker.claim_candidate_lease("batch-1", "2222", "owner-b", _NOW, 240)

    batch_tracker.try_acquire_timeout_finalization("batch-1")
    result = batch_tracker.run_timeout_finalization_pass("batch-1", _NOW, max_rows_per_run=1000)

    assert result.total == 3
    assert result.terminal_count == 3
    assert result.newly_failed_count == 2

    rows = {
        r.stock_code: r
        for r in batch_tracker.query_all_candidate_progress("batch-1", consistent_read=True)
    }
    assert rows["1111"].status == WatchlistProgressStatus.COMPLETED.value
    assert rows["1111"].evaluation_result == "PASSED"
    assert rows["1111"].ranking_entry == '{"stock_code": "1111"}'  # 削除されない
    assert rows["1111"].total_processing_duration_ms == 2000  # 保持される

    for code in ("2222", "3333"):
        assert rows[code].status == WatchlistProgressStatus.FAILED.value
        assert rows[code].evaluation_result == batch_tracker.EVALUATION_RESULT_BATCH_TIMED_OUT
        assert rows[code].lease_owner_id is None


def test_run_timeout_finalization_pass_respects_row_cap_and_resumes_next_run(dynamo) -> None:
    """17節: 件数上限に達した場合はTIMEOUT_FINALIZINGのまま継続し、次回実行で再開する。"""
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 3, 72, _NOW)
    batch_tracker.create_missing_candidate_progress_rows(
        "batch-1", ["1111", "2222", "3333"], _NOW, 72
    )
    batch_tracker.mark_dispatch_completed("batch-1", _NOW)
    batch_tracker.try_acquire_timeout_finalization("batch-1")

    first_pass = batch_tracker.run_timeout_finalization_pass("batch-1", _NOW, max_rows_per_run=1)
    assert first_pass.newly_failed_count == 1
    assert first_pass.terminal_count == 1
    assert first_pass.total == 3

    second_pass = batch_tracker.run_timeout_finalization_pass(
        "batch-1", _NOW, max_rows_per_run=1000
    )
    assert second_pass.newly_failed_count == 2
    assert second_pass.terminal_count == 3


def test_set_timeout_finalize_completed_count_does_not_double_count_on_rerun(dynamo) -> None:
    """案C: SET補正のため、同じterminal_countを何度書き込んでも二重加算されない。"""
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 2, 72, _NOW)
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111", "2222"], _NOW, 72)
    batch_tracker.mark_dispatch_completed("batch-1", _NOW)
    batch_tracker.try_acquire_timeout_finalization("batch-1")

    result = batch_tracker.run_timeout_finalization_pass("batch-1", _NOW, max_rows_per_run=1000)
    batch_tracker.set_timeout_finalize_completed_count("batch-1", result.terminal_count, _NOW)
    batch_tracker.set_timeout_finalize_completed_count("batch-1", result.terminal_count, _NOW)

    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert int(batch["completed"]) == 2


def test_transition_timeout_finalizing_to_timed_out(dynamo) -> None:
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 1, 72, _NOW)
    batch_tracker.mark_dispatch_completed("batch-1", _NOW)
    batch_tracker.try_acquire_timeout_finalization("batch-1")

    assert batch_tracker.transition_timeout_finalizing_to_timed_out("batch-1", _NOW) is True
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["status"] == WatchlistBatchStatus.TIMED_OUT.value


def test_worker_and_reconciler_race_only_one_terminal_state_wins(dynamo) -> None:
    """17節「WorkerとReconcilerの競合」: Reconcilerが先にFAILED確定した場合、
    Workerの完了更新(complete_candidate)は条件不成立で失敗すること。"""
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 1, 72, _NOW)
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111"], _NOW, 72)
    batch_tracker.mark_dispatch_completed("batch-1", _NOW)
    batch_tracker.claim_candidate_lease("batch-1", "1111", "worker-a", _NOW, 240)

    # Reconcilerが先にタイムアウト確定(ranking_entryは無いが、事前にセットしておいて
    # 削除されないことも確認する)。
    batch_tracker.try_acquire_timeout_finalization("batch-1")
    result = batch_tracker.run_timeout_finalization_pass("batch-1", _NOW, max_rows_per_run=1000)
    assert result.newly_failed_count == 1

    # Workerが遅れて完了させようとしても条件不成立で失敗する。
    ok = batch_tracker.complete_candidate(
        "batch-1",
        "1111",
        "worker-a",
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result="PASSED",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=_NOW,
    )
    assert ok is False

    rows = batch_tracker.query_all_candidate_progress("batch-1", consistent_read=True)
    assert rows[0].status == WatchlistProgressStatus.FAILED.value
    assert rows[0].evaluation_result == batch_tracker.EVALUATION_RESULT_BATCH_TIMED_OUT


# --- 2節: DISPATCHINGタイムアウト -----------------------------------------------


def test_mark_dispatch_failed_only_from_dispatching_status(dynamo) -> None:
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    assert batch_tracker.mark_dispatch_failed("batch-1", _NOW) is True
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["status"] == WatchlistBatchStatus.DISPATCH_FAILED.value

    assert batch_tracker.mark_dispatch_failed("batch-1", _NOW) is False


def test_list_watchlist_batches_by_status_filters_correctly(dynamo) -> None:
    batch_tracker.try_acquire_dispatch_lease("batch-a", "dispatcher", _NOW, 360, 72)
    batch_tracker.try_acquire_dispatch_lease("batch-b", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-b", 1, 72, _NOW)
    batch_tracker.mark_dispatch_completed("batch-b", _NOW)

    dispatching = batch_tracker.list_watchlist_batches_by_status(
        [WatchlistBatchStatus.DISPATCHING]
    )
    running = batch_tracker.list_watchlist_batches_by_status([WatchlistBatchStatus.RUNNING])
    assert {b["batch_id"] for b in dispatching} == {"batch-a"}
    assert {b["batch_id"] for b in running} == {"batch-b"}


# --- 20節: status/execution_resultの責務分離 ------------------------------------


def test_mark_watchlist_batch_completed_normal_vs_high_throttle(dynamo) -> None:
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.mark_watchlist_batch_completed(
        "batch-1", batch_tracker.EXECUTION_RESULT_NORMAL, _NOW
    )
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["status"] == WatchlistBatchStatus.COMPLETED.value
    assert item["execution_result"] == batch_tracker.EXECUTION_RESULT_NORMAL

    batch_tracker.try_acquire_dispatch_lease("batch-2", "dispatcher", _NOW, 360, 72)
    batch_tracker.mark_watchlist_batch_completed(
        "batch-2", batch_tracker.EXECUTION_RESULT_HIGH_THROTTLE_RATE, _NOW
    )
    item2 = batch_tracker.get_watchlist_batch("batch-2")
    assert item2 is not None
    assert item2["status"] == WatchlistBatchStatus.ABORTED.value
    assert item2["execution_result"] == batch_tracker.EXECUTION_RESULT_HIGH_THROTTLE_RATE


def test_mark_watchlist_batch_completed_scoring_data_quality_degraded_is_aborted(
    dynamo,
) -> None:
    """運用ハードニング3節: 主要項目欠損率によるABORTEDもHIGH_THROTTLE_RATEと
    同じくstatus=ABORTEDへ揃うこと(20節のstatus/execution_result分離パターン)。"""
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.mark_watchlist_batch_completed(
        "batch-1", batch_tracker.EXECUTION_RESULT_SCORING_DATA_QUALITY_DEGRADED, _NOW
    )
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["status"] == WatchlistBatchStatus.ABORTED.value
    assert item["execution_result"] == (
        batch_tracker.EXECUTION_RESULT_SCORING_DATA_QUALITY_DEGRADED
    )


def test_mark_watchlist_batch_completed_composite_execution_result_is_aborted(dynamo) -> None:
    """運用ハードニング第2弾5節: 複数の安全弁に同時該当した場合、execution_resultが
    "|"区切りの複合文字列になってもstatus=ABORTEDと判定されること。"""
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    composite = (
        f"{batch_tracker.EXECUTION_RESULT_HIGH_THROTTLE_RATE}|"
        f"{batch_tracker.EXECUTION_RESULT_EXCESSIVE_DATA_ERRORS}"
    )
    batch_tracker.mark_watchlist_batch_completed("batch-1", composite, _NOW)
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["status"] == WatchlistBatchStatus.ABORTED.value
    assert item["execution_result"] == composite


# --- 運用ハードニング5節: finalize再実行性(FINALIZING/FINALIZE_FAILED) ----------


def test_try_retry_finalize_succeeds_from_finalize_failed(dynamo) -> None:
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 1, 72, _NOW)
    batch_tracker.mark_dispatch_completed("batch-1", _NOW)
    batch_tracker.try_finalize_if_ready("batch-1", _NOW)
    batch_tracker.mark_watchlist_finalize_failed("batch-1", _NOW, "boom")

    ok = batch_tracker.try_retry_finalize("batch-1")
    assert ok is True
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["status"] == WatchlistBatchStatus.FINALIZE_PREPARING.value


def test_try_retry_finalize_fails_conditional_check_when_not_finalize_failed(dynamo) -> None:
    """ConditionalCheckFailedException相当: FINALIZE_FAILED以外(例: RUNNING)からは
    遷移しないこと。"""
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 1, 72, _NOW)
    batch_tracker.mark_dispatch_completed("batch-1", _NOW)

    ok = batch_tracker.try_retry_finalize("batch-1")
    assert ok is False
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["status"] == WatchlistBatchStatus.RUNNING.value


def test_mark_watchlist_finalize_failed_increments_attempt_count(dynamo) -> None:
    """Reconcilerの再試行回数上限判定に使うfinalize_attempt_countが、
    finalize失敗のたびに加算されること。"""
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 1, 72, _NOW)
    batch_tracker.mark_dispatch_completed("batch-1", _NOW)
    batch_tracker.try_finalize_if_ready("batch-1", _NOW)

    batch_tracker.mark_watchlist_finalize_failed("batch-1", _NOW, "boom-1")
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert int(item["finalize_attempt_count"]) == 1

    batch_tracker.try_retry_finalize("batch-1")
    batch_tracker.mark_watchlist_finalize_failed("batch-1", _NOW, "boom-2")
    item2 = batch_tracker.get_watchlist_batch("batch-1")
    assert item2 is not None
    assert int(item2["finalize_attempt_count"]) == 2


def _drive_batch_to_finalize_preparing(now: dt.datetime) -> None:
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", now, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 1, 72, now)
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111"], now, 72)
    batch_tracker.mark_dispatch_completed("batch-1", now)
    batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-a", now, 240)
    batch_tracker.complete_candidate(
        "batch-1",
        "1111",
        "owner-a",
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result="PASSED",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=now,
    )
    assert batch_tracker.try_finalize_if_ready("batch-1", now) is True


def test_mark_finalizing_stuck_as_failed_transitions_when_past_threshold(dynamo) -> None:
    _drive_batch_to_finalize_preparing(_NOW)  # finalizing_started_at = _NOW

    later = _NOW + dt.timedelta(minutes=16)
    ok = batch_tracker.mark_finalizing_stuck_as_failed("batch-1", later, 15)
    assert ok is True
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["status"] == WatchlistBatchStatus.FINALIZE_FAILED.value


def test_mark_finalizing_stuck_as_failed_no_op_when_within_threshold(dynamo) -> None:
    """ConditionalCheckFailedException相当: 閾値未満(まだ正常に進行中かもしれない)
    場合は遷移しないこと。"""
    _drive_batch_to_finalize_preparing(_NOW)

    soon = _NOW + dt.timedelta(minutes=5)
    ok = batch_tracker.mark_finalizing_stuck_as_failed("batch-1", soon, 15)
    assert ok is False
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["status"] == WatchlistBatchStatus.FINALIZE_PREPARING.value


# --- 運用ハードニング6節: 運用者によるバッチ中断(CLI abort) --------------------


def test_try_operator_abort_succeeds_from_non_terminal_status(dynamo) -> None:
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 1, 72, _NOW)
    batch_tracker.mark_dispatch_completed("batch-1", _NOW)

    ok = batch_tracker.try_operator_abort("batch-1", "運用者判断による中断", _NOW)
    assert ok is True
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["status"] == WatchlistBatchStatus.ABORTED.value
    assert "運用者判断による中断" in item["execution_result"]


def test_try_operator_abort_fails_conditional_check_when_already_terminal(dynamo) -> None:
    """ConditionalCheckFailedException相当: 既に終端状態(COMPLETED等)からは
    遷移しないこと。"""
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.mark_watchlist_batch_completed(
        "batch-1", batch_tracker.EXECUTION_RESULT_NORMAL, _NOW
    )

    ok = batch_tracker.try_operator_abort("batch-1", "後から中断しようとした", _NOW)
    assert ok is False
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["status"] == WatchlistBatchStatus.COMPLETED.value


# --- SQS再配信・Lambda途中終了の模擬テスト ---------------------------------------


def test_same_sqs_message_processed_twice_only_reflected_once(dynamo) -> None:
    """同一SQSメッセージ(同一batch_id/stock_code)がWorkerで2回処理されても
    (可視性タイムアウト経過前の重複配信を想定、ownerは同一)、completedが
    1回しか加算されないこと。"""
    batch_tracker.set_watchlist_batch_total("batch-1", 1, 72, _NOW)
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111"], _NOW, 72)
    batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-a", _NOW, 240)

    first = batch_tracker.complete_candidate(
        "batch-1",
        "1111",
        "owner-a",
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result="PASSED",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=_NOW,
    )
    second = batch_tracker.complete_candidate(
        "batch-1",
        "1111",
        "owner-a",
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result="PASSED",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=_NOW,
    )
    assert first is True
    assert second is False  # 既にstatus=COMPLETEDのため条件不成立
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert int(batch["completed"]) == 1


def test_lambda_terminated_after_lease_before_completion_allows_reclaim(dynamo) -> None:
    """Worker Lambdaがリース取得直後(complete_candidate呼び出し前)に打ち切られた
    場合、リース期限切れ後に別のWorker実行が再クレームできること。"""
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111"], _NOW, 72)
    batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-a", _NOW, 240)
    # ここでLambdaが打ち切られたと想定(complete_candidateが一度も呼ばれない)。

    later = _NOW + dt.timedelta(seconds=241)
    reclaimed = batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-b", later, 240)
    assert reclaimed is True
    rows = batch_tracker.query_all_candidate_progress("batch-1", consistent_read=True)
    assert rows[0].lease_owner_id == "owner-b"
    assert rows[0].attempt_count == 2


def test_lambda_terminated_after_completion_before_finalize_call_is_safe_to_resume(
    dynamo,
) -> None:
    """complete_candidate成功直後、maybe_finalize呼び出し前にLambdaが打ち切られた
    場合を想定。completedは既に加算済みのため、後続の実行(次のWorker呼び出しや
    Reconciler)がtry_finalize_if_readyを呼べば正しく進行できること(二重加算なし)。"""
    batch_tracker.try_acquire_dispatch_lease("batch-1", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("batch-1", 1, 72, _NOW)
    batch_tracker.mark_dispatch_completed("batch-1", _NOW)
    batch_tracker.create_missing_candidate_progress_rows("batch-1", ["1111"], _NOW, 72)
    batch_tracker.claim_candidate_lease("batch-1", "1111", "owner-a", _NOW, 240)
    batch_tracker.complete_candidate(
        "batch-1",
        "1111",
        "owner-a",
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result="PASSED",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=_NOW,
    )
    # ここでLambdaが打ち切られたと想定(maybe_finalizeが一度も呼ばれない)。

    # 別実行(次のイベント、またはReconciler)がfinalizeを試みる。
    ok = batch_tracker.try_finalize_if_ready("batch-1", _NOW)
    assert ok is True
    batch = batch_tracker.get_watchlist_batch("batch-1")
    assert batch is not None
    assert int(batch["completed"]) == 1  # 二重加算されていない


# --- 運用ハードニング8節: DynamoDBアイテムサイズの回帰確認 -----------------------


def test_create_missing_candidate_progress_rows_scales_to_full_market_without_400kb_risk(
    dynamo,
) -> None:
    """東証プライム+スタンダード全銘柄相当(約3,200件)を投入しても、DynamoDBの
    単一アイテム400KB上限に抵触しないこと(銘柄単位の行設計で構造的に解消済み、
    旧設計(単一アイテムへのranking_entries集約)にあったリスクの回帰確認)。"""
    stock_codes = [f"{1000 + i:04d}" for i in range(3200)]
    batch_tracker.set_watchlist_batch_total("batch-1", len(stock_codes), 72, _NOW)
    batch_tracker.create_missing_candidate_progress_rows("batch-1", stock_codes, _NOW, 72)

    rows = batch_tracker.query_all_candidate_progress("batch-1", consistent_read=True)
    assert len(rows) == 3200

    # BatchRunsTableのアイテム自体も小さい(件数のみを持つ設計のため)。
    batch_item = batch_tracker.get_watchlist_batch("batch-1")
    assert batch_item is not None
    import json

    assert len(json.dumps(batch_item, default=str).encode("utf-8")) < 10_000


# --- 本番運用ハードニング(2026-08-07): TransactionConflictException対応 -----------
# 本番のウォッチリスト自動追加パイプラインで、並行Worker実行下でcomplete_candidateの
# TransactWriteItemsとtry_finalize_if_ready等の単純UpdateItemが同一BatchRunsTable
# 項目へほぼ同時にアクセスし、TransactionConflictExceptionが未捕捉のままLambda
# 呼び出し自体が失敗する事象が発生した(SQS再送により最終的にバッチ自体は正常完了)。
# ConditionalCheckFailedExceptionのみを捕捉していた全箇所を
# _TRANSACTION_CONDITION_FAILURE_CODES経由の判定へ統一したため、その回帰を確認する。
# 実DynamoDB(moto)では真の並行競合を再現できないため、_table/_progress_table/
# boto3.clientをスタブ化してClientErrorを注入する。


class _ConflictRaisingTable:
    """update_itemを呼ぶと常に指定コードのClientErrorを送出するスタブ。"""

    def __init__(self, code: str) -> None:
        self._code = code

    def update_item(self, **_kwargs):
        raise ClientError({"Error": {"Code": self._code, "Message": "conflict"}}, "UpdateItem")


class _ConflictRaisingDynamoClient:
    """transact_write_itemsを呼ぶと常に指定コードのClientErrorを送出するスタブ。"""

    def __init__(self, code: str) -> None:
        self._code = code

    def transact_write_items(self, **_kwargs):
        raise ClientError(
            {"Error": {"Code": self._code, "Message": "conflict"}}, "TransactWriteItems"
        )


# batch_id配下のBatchRunsTable項目に対しUpdateItemを行う排他制御関数群
# (try_finalize_if_readyが実際に本番でTransactionConflictExceptionを observed した箇所)。
_BATCH_RUNS_TABLE_CALLS: list[tuple[str, tuple]] = [
    ("try_acquire_dispatch_lease", ("batch-1", "owner-a", _NOW, 360, 72)),
    ("mark_dispatch_completed", ("batch-1", _NOW)),
    ("mark_dispatch_failed", ("batch-1", _NOW)),
    ("try_finalize_if_ready", ("batch-1", _NOW)),
    ("try_retry_finalize", ("batch-1",)),
    ("mark_finalizing_stuck_as_failed", ("batch-1", _NOW, 30)),
    ("record_finalize_target", ("batch-1", _NOW, ["1301"], "[]")),
    ("mark_watchlist_write_completed", ("batch-1", _NOW)),
    ("record_notification_pending", ("batch-1", _NOW, "hash")),
    ("record_notification_resolved", ("batch-1", _NOW, [], "SENT")),
    ("try_retry_notification", ("batch-1", _NOW)),
    ("try_operator_abort", ("batch-1", "reason", _NOW)),
    ("try_acquire_timeout_finalization", ("batch-1",)),
    ("set_timeout_finalize_completed_count", ("batch-1", 1, _NOW)),
    ("transition_timeout_finalizing_to_timed_out", ("batch-1", _NOW)),
    ("transition_timeout_finalizing_to_failed", ("batch-1", _NOW, "reason")),
]

# WatchlistCandidateProgressTable項目に対しUpdateItemを行う排他制御関数群。
_PROGRESS_TABLE_CALLS: list[tuple[str, tuple]] = [
    ("mark_candidate_dispatched", ("batch-1", "1301", _NOW)),
    ("claim_candidate_lease", ("batch-1", "1301", "owner-a", _NOW, 180)),
    ("_try_mark_row_timed_out", ("batch-1", "1301", _NOW)),
]


@pytest.mark.parametrize("func_name, args", _BATCH_RUNS_TABLE_CALLS)
def test_batch_runs_table_functions_treat_transaction_conflict_as_expected_contention(
    monkeypatch: pytest.MonkeyPatch, func_name: str, args: tuple
) -> None:
    """TransactionConflictExceptionはConditionalCheckFailedExceptionと同様、想定内の
    競合として扱われ(再送出せず)呼び出し元へ制御を返すこと(本番incidentの回帰確認)。"""
    monkeypatch.setattr(
        batch_tracker, "_table", lambda: _ConflictRaisingTable("TransactionConflictException")
    )
    func = getattr(batch_tracker, func_name)
    result = func(*args)
    assert result in (False, None)


@pytest.mark.parametrize("func_name, args", _PROGRESS_TABLE_CALLS)
def test_progress_table_functions_treat_transaction_conflict_as_expected_contention(
    monkeypatch: pytest.MonkeyPatch, func_name: str, args: tuple
) -> None:
    monkeypatch.setattr(
        batch_tracker,
        "_progress_table",
        lambda: _ConflictRaisingTable("TransactionConflictException"),
    )
    func = getattr(batch_tracker, func_name)
    result = func(*args)
    assert result in (False, None)


def test_complete_candidate_treats_transaction_conflict_as_expected_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        batch_tracker.boto3,
        "client",
        lambda *_a, **_k: _ConflictRaisingDynamoClient("TransactionConflictException"),
    )
    ok = batch_tracker.complete_candidate(
        "batch-1",
        "1301",
        "owner-a",
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result="PASSED",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=_NOW,
    )
    assert ok is False


def test_record_terminal_failure_treats_transaction_conflict_as_expected_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        batch_tracker.boto3,
        "client",
        lambda *_a, **_k: _ConflictRaisingDynamoClient("TransactionConflictException"),
    )
    ok = batch_tracker.record_terminal_failure("batch-1", "1301", _NOW)
    assert ok is False


def test_unrelated_client_error_still_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """本修正が競合系コード以外(例: ValidationException)まで握りつぶさないことの回帰。"""
    monkeypatch.setattr(
        batch_tracker, "_table", lambda: _ConflictRaisingTable("ValidationException")
    )
    with pytest.raises(ClientError):
        batch_tracker.try_finalize_if_ready("batch-1", _NOW)


# --- 平日毎日起動化(2026-08)対応: WATCHLIST_MAINTENANCE後続起動トリガー -----------


def test_try_acquire_maintenance_trigger_succeeds_on_first_call(dynamo) -> None:
    ok = batch_tracker.try_acquire_maintenance_trigger(
        "batch-1", "watchlist-maint-batch-1", "owner-a", _NOW
    )
    assert ok is True
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["maintenance_trigger_status"] == batch_tracker.MAINTENANCE_TRIGGER_STATUS_TRIGGERING
    assert item["maintenance_batch_id"] == "watchlist-maint-batch-1"


def test_try_acquire_maintenance_trigger_rejects_second_attempt_while_lease_valid(dynamo) -> None:
    """同じparent finalizerが2回実行されても、leaseが有効な間は再取得できない
    (exactly-onceの中核)。"""
    batch_tracker.try_acquire_maintenance_trigger(
        "batch-1", "watchlist-maint-batch-1", "owner-a", _NOW
    )
    ok = batch_tracker.try_acquire_maintenance_trigger(
        "batch-1", "watchlist-maint-batch-1", "owner-b", _NOW
    )
    assert ok is False


def test_try_acquire_maintenance_trigger_allows_reclaim_after_lease_expiry(dynamo) -> None:
    """invoke失敗時、lease失効後はReconcilerが再取得できる(処理を消失させない)。"""
    batch_tracker.try_acquire_maintenance_trigger(
        "batch-1", "watchlist-maint-batch-1", "owner-a", _NOW, lease_seconds=120
    )
    later = _NOW + dt.timedelta(seconds=121)
    ok = batch_tracker.try_acquire_maintenance_trigger(
        "batch-1", "watchlist-maint-batch-1", "owner-b", later
    )
    assert ok is True
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["maintenance_trigger_owner_id"] == "owner-b"
    assert item["maintenance_trigger_attempt_count"] == 2


def test_mark_maintenance_triggered_is_permanently_final(dynamo) -> None:
    """invoke成功後にTRIGGEREDへ確定すると、以後は(leaseが失効していても)
    二度と再取得できない(exactly-once)。"""
    batch_tracker.try_acquire_maintenance_trigger(
        "batch-1", "watchlist-maint-batch-1", "owner-a", _NOW
    )
    batch_tracker.mark_maintenance_triggered("batch-1", _NOW)
    item = batch_tracker.get_watchlist_batch("batch-1")
    assert item is not None
    assert item["maintenance_trigger_status"] == batch_tracker.MAINTENANCE_TRIGGER_STATUS_TRIGGERED
    assert item["maintenance_triggered_at"] == _NOW.isoformat()

    much_later = _NOW + dt.timedelta(days=1)
    ok = batch_tracker.try_acquire_maintenance_trigger(
        "batch-1", "watchlist-maint-batch-1", "owner-b", much_later
    )
    assert ok is False


def test_list_stale_maintenance_triggers_finds_expired_triggering_only(dynamo) -> None:
    batch_tracker.try_acquire_maintenance_trigger(
        "batch-1", "watchlist-maint-batch-1", "owner-a", _NOW, lease_seconds=120
    )
    batch_tracker.try_acquire_maintenance_trigger(
        "batch-2", "watchlist-maint-batch-2", "owner-a", _NOW, lease_seconds=120
    )
    batch_tracker.mark_maintenance_triggered("batch-2", _NOW)  # batch-2は成功済み(対象外)

    just_after_expiry = _NOW + dt.timedelta(seconds=121)
    stale = batch_tracker.list_stale_maintenance_triggers(just_after_expiry)
    assert {item["batch_id"] for item in stale} == {"batch-1"}


def test_list_stale_maintenance_triggers_empty_before_lease_expires(dynamo) -> None:
    batch_tracker.try_acquire_maintenance_trigger(
        "batch-1", "watchlist-maint-batch-1", "owner-a", _NOW, lease_seconds=120
    )
    still_within_lease = _NOW + dt.timedelta(seconds=60)
    assert batch_tracker.list_stale_maintenance_triggers(still_within_lease) == []


def test_set_watchlist_batch_total_records_triggered_by_batch_id(dynamo) -> None:
    """平日毎日起動化(2026-08)対応: parent-child監査用フィールドがchild batchの
    BatchRunsTableへ正しく記録されること。"""
    batch_tracker.set_watchlist_batch_total(
        "watchlist-maint-batch-1",
        5,
        72,
        _NOW,
        job_type=batch_tracker.WatchlistJobType.WATCHLIST_MAINTENANCE,
        triggered_by_batch_id="batch-1",
        trigger_type="POST_NEW_CANDIDATE_SCREENING",
    )
    item = batch_tracker.get_watchlist_batch("watchlist-maint-batch-1")
    assert item is not None
    assert item["triggered_by_batch_id"] == "batch-1"
    assert item["trigger_type"] == "POST_NEW_CANDIDATE_SCREENING"


# --- Issue #31: holdings/buy完了処理(finalize)の排他制御 -----------------------
# completion_finalize_token/started_at/completed_atによるacquire/complete/
# stale takeoverのDynamoDB意味論をmotoで検証する。running_on_lambda()ガードを
# 通すため各テストでAWS_LAMBDA_FUNCTION_NAMEを設定する。

_I31_NOW = dt.datetime(2026, 8, 28, 23, 0, tzinfo=dt.UTC)


def _i31_put_batch(batch_id: str = "cb-1", total: int = 3, completed: int = 3) -> None:
    boto3.resource("dynamodb", region_name=_REGION).Table(_BATCH_TABLE).put_item(
        Item={"batch_id": batch_id, "total": total, "completed": completed}
    )


def _i31_get_batch(batch_id: str = "cb-1") -> dict:
    return (
        boto3.resource("dynamodb", region_name=_REGION)
        .Table(_BATCH_TABLE)
        .get_item(Key={"batch_id": batch_id})["Item"]
    )


def test_issue31_acquire_first_true_then_false(dynamo, monkeypatch) -> None:
    """A/B/D: 初回acquireはtokenを返し、直後(stale閾値未満)の2回目はNone。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    _i31_put_batch()

    token = batch_tracker.try_acquire_completion_finalize("cb-1", _I31_NOW)
    assert token is not None

    again = batch_tracker.try_acquire_completion_finalize(
        "cb-1", _I31_NOW + dt.timedelta(seconds=600)
    )
    assert again is None
    item = _i31_get_batch()
    assert item["completion_finalize_token"] == token  # 所有権は初回のまま


def test_issue31_acquire_rejected_when_batch_incomplete(dynamo, monkeypatch) -> None:
    """C: completed < totalの間はfinalize lockを取得できない。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    _i31_put_batch(total=3, completed=2)

    assert batch_tracker.try_acquire_completion_finalize("cb-1", _I31_NOW) is None


def test_issue31_stale_takeover_updates_token(dynamo, monkeypatch) -> None:
    """E: started_atが1200秒以上古くcompleted_at未記録なら、takeoverが成功し
    token・started_atが新ownerの値へ更新される。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    _i31_put_batch()
    old_token = batch_tracker.try_acquire_completion_finalize("cb-1", _I31_NOW)
    assert old_token is not None

    later = _I31_NOW + dt.timedelta(seconds=1300)
    new_token = batch_tracker.try_acquire_completion_finalize("cb-1", later)

    assert new_token is not None
    assert new_token != old_token
    item = _i31_get_batch()
    assert item["completion_finalize_token"] == new_token
    assert item["completion_finalize_started_at"] == later.isoformat()


def test_issue31_old_owner_cannot_complete_after_takeover(dynamo, monkeypatch) -> None:
    """F/G: takeover後、旧owner tokenによるcompleted記録は必ず失敗し、
    新owner tokenのみ記録できる。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    _i31_put_batch()
    old_token = batch_tracker.try_acquire_completion_finalize("cb-1", _I31_NOW)
    later = _I31_NOW + dt.timedelta(seconds=1300)
    new_token = batch_tracker.try_acquire_completion_finalize("cb-1", later)
    assert old_token is not None and new_token is not None

    assert (
        batch_tracker.mark_completion_finalize_completed("cb-1", old_token, later)
        is False
    )
    item = _i31_get_batch()
    assert "completion_finalize_completed_at" not in item

    assert (
        batch_tracker.mark_completion_finalize_completed("cb-1", new_token, later)
        is True
    )
    item = _i31_get_batch()
    assert item["completion_finalize_completed_at"] == later.isoformat()


def test_issue31_completed_batch_never_reacquired(dynamo, monkeypatch) -> None:
    """H/I(Acceptance Criteria): completed_at記録後は、直後でも1200秒以上・
    数時間経過後でも、どれだけ遅いretryでも再acquireできない
    (TTLでbatch item自体が削除されるまでfinalize済み状態を維持)。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    _i31_put_batch()
    token = batch_tracker.try_acquire_completion_finalize("cb-1", _I31_NOW)
    assert token is not None
    assert batch_tracker.mark_completion_finalize_completed("cb-1", token, _I31_NOW) is True

    for delay_seconds in (1, 1300, 2 * 60 * 60, 5 * 60 * 60):
        assert (
            batch_tracker.try_acquire_completion_finalize(
                "cb-1", _I31_NOW + dt.timedelta(seconds=delay_seconds)
            )
            is None
        ), f"delay={delay_seconds}s で再acquireできてはならない"


def test_issue31_complete_is_recorded_only_once(dynamo, monkeypatch) -> None:
    """completed_atは1回だけ記録される(同一tokenでの二重記録もFalse)。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    _i31_put_batch()
    token = batch_tracker.try_acquire_completion_finalize("cb-1", _I31_NOW)
    assert token is not None
    assert batch_tracker.mark_completion_finalize_completed("cb-1", token, _I31_NOW) is True
    assert (
        batch_tracker.mark_completion_finalize_completed(
            "cb-1", token, _I31_NOW + dt.timedelta(seconds=1)
        )
        is False
    )


def test_issue31_local_environment_always_acquires(monkeypatch) -> None:
    """ローカル(非Lambda)は常に取得成功(単一プロセスのため排他不要。
    DynamoDBへはアクセスしない)。"""
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    token = batch_tracker.try_acquire_completion_finalize("cb-local", _I31_NOW)
    assert token is not None
    assert (
        batch_tracker.mark_completion_finalize_completed("cb-local", token, _I31_NOW)
        is True
    )


# --- Issue #57 Phase B1: completion idempotency / early finalize prevention ----
# `completed`カウンタは条件なしADDのため、同一銘柄のLambda非同期retryで二重
# 加算され、未処理銘柄を残したまま`completed >= total`が成立しうる(早期
# finalize)。B1ではDynamoDBの文字列セット`completed_codes`の要素数を
# eligibilityの正本にする。DynamoDBのsize()/Set意味論に依存するためmotoで検証する。

_I57_NOW = dt.datetime(2026, 8, 31, 23, 0, tzinfo=dt.UTC)


def _i57_start(batch_id: str = "b57", total: int = 2) -> None:
    """新形式(B1)のバッチをstart_batch経由で作成する。"""
    batch_tracker.start_batch(batch_id, total, _I57_NOW)


def _i57_item(batch_id: str = "b57") -> dict:
    return (
        boto3.resource("dynamodb", region_name=_REGION)
        .Table(_BATCH_TABLE)
        .get_item(Key={"batch_id": batch_id})["Item"]
    )


def test_i57_duplicate_completion_id_is_idempotent(dynamo, monkeypatch) -> None:
    """T1: 同一識別子を2回report → completed_codesは1件のまま(completedは2)。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    _i57_start(total=2)

    batch_tracker.record_result("b57", "hold", completion_id="7203")
    progress = batch_tracker.record_result("b57", "hold", completion_id="7203")

    assert progress is not None
    assert progress.completed_codes == ["7203"]
    assert progress.unique_completed == 1
    # legacyカウンタは維持する(削除しない。既存monitoring/log互換のため)
    assert progress.completed == 2


def test_i57_duplicate_plus_pending_is_not_complete(dynamo, monkeypatch) -> None:
    """T2: A×2 + B未処理 → completed(2) >= total(2) でもeligibilityはFalse。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    _i57_start(total=2)

    batch_tracker.record_result("b57", "hold", completion_id="7203")
    progress = batch_tracker.record_result("b57", "hold", completion_id="7203")

    assert progress is not None
    assert progress.completed >= progress.total, "旧判定なら完了扱いになる前提条件"
    assert progress.is_complete is False, "早期finalizeを防げていない"
    # DynamoDB側のConditionExpressionも同じ意味論であること
    assert batch_tracker.try_acquire_completion_finalize("b57", _I57_NOW) is None


def test_i57_duplicate_plus_pending_completes_after_last_stock(dynamo, monkeypatch) -> None:
    """T3: A×2 + Bが完了 → eligibilityはTrue(正常完了を妨げない)。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    _i57_start(total=2)

    batch_tracker.record_result("b57", "hold", completion_id="7203")
    batch_tracker.record_result("b57", "hold", completion_id="7203")
    progress = batch_tracker.record_result("b57", "hold", completion_id="8306")

    assert progress is not None
    assert progress.unique_completed == 2
    assert progress.is_complete is True
    assert batch_tracker.try_acquire_completion_finalize("b57", _I57_NOW) is not None


def test_i57_holdings_same_stock_different_owner_counts_separately(
    dynamo, monkeypatch
) -> None:
    """T4: holdingsはholding_id(owner#stock_code)を渡すため、同一銘柄でも
    owner違いは別の完了として数える(M3.1の既存方針と整合)。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    _i57_start(total=2)

    batch_tracker.record_result("b57", "hold", completion_id="owner-a#8306")
    progress = batch_tracker.record_result("b57", "hold", completion_id="owner-b#8306")

    assert progress is not None
    assert progress.unique_completed == 2
    assert progress.is_complete is True


def test_i57_legacy_item_without_completed_codes_still_finalizes(
    dynamo, monkeypatch
) -> None:
    """T5(deploy境界): 旧コードが作成した項目にはcompleted_codesが無い。
    属性の不在を「完了0件」と扱うと永久にfinalize不能になるため、legacy
    カウンタへフォールバックする。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    boto3.resource("dynamodb", region_name=_REGION).Table(_BATCH_TABLE).put_item(
        Item={"batch_id": "legacy", "total": 3, "completed": 3}
    )

    progress = batch_tracker.BatchProgress(
        total=3,
        completed=3,
        category_counts={},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[],
        sector_entries=[],
        holding_count=0,
    )
    assert progress.has_completion_ids is False
    assert progress.unique_completed == 3
    assert progress.is_complete is True
    assert batch_tracker.try_acquire_completion_finalize("legacy", _I57_NOW) is not None


def test_i57_new_format_item_uses_set_cardinality(dynamo, monkeypatch) -> None:
    """T6: 新形式項目はcompleted_codesの要素数で判定する
    (legacyカウンタが水増しされていても引きずられない)。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    boto3.resource("dynamodb", region_name=_REGION).Table(_BATCH_TABLE).put_item(
        Item={"batch_id": "mixed", "total": 3, "completed": 99, "completed_codes": {"a", "b"}}
    )
    assert batch_tracker.try_acquire_completion_finalize("mixed", _I57_NOW) is None

    batch_tracker.record_result("mixed", "hold", completion_id="c")
    assert batch_tracker.try_acquire_completion_finalize("mixed", _I57_NOW) is not None


def test_i57_large_batch_item_size_is_within_dynamodb_limit(dynamo, monkeypatch) -> None:
    """T7: 621銘柄規模で、他の大きな属性と併存しても400KB上限に余裕があること
    を実測する(概算ではなくserialized sizeを測る)。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    total = 621
    _i57_start("big", total=total)
    for i in range(total):
        code = f"{1000 + i}"
        batch_tracker.record_result(
            "big",
            "hold",
            completion_id=code,
            # 同時に育つ既存の大きな属性も併せて再現する
            ranking_entry=f"12.3456789|{code}|{'r' * 36}",
            sector_entry=f"BANKING|1234567890.12|{code}",
        )

    item = _i57_item("big")
    assert len(item["completed_codes"]) == total

    serializer = TypeSerializer()
    serialized = {k: serializer.serialize(v) for k, v in item.items()}
    size_bytes = len(json.dumps(serialized, default=str).encode("utf-8"))
    # DynamoDB項目上限は400KB。余裕があること(半分未満)を固定する。
    assert size_bytes < 200_000, f"item size too large: {size_bytes} bytes"


def test_i57_concurrent_duplicate_completions_do_not_inflate_unique_count(
    dynamo, monkeypatch
) -> None:
    """T8: 同一識別子が並行に複数回reportされても、unique完了数はずれない
    (#71のfanout重複が未修正でも#57側で安全であることを固定する)。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    _i57_start("conc", total=3)

    for _ in range(5):
        batch_tracker.record_result("conc", "hold", completion_id="7203")
        batch_tracker.record_result("conc", "hold", completion_id="8306")
    progress = batch_tracker.record_result("conc", "hold", completion_id="9432")

    assert progress is not None
    assert progress.unique_completed == 3
    assert progress.completed == 11
    assert progress.is_complete is True


# --- Issue #31 regression(B1で壊れていないこと) -------------------------------


def test_i57_regression_parallel_acquire_yields_exactly_one_token(
    dynamo, monkeypatch
) -> None:
    """T9(#31回帰): 新形式項目でも、同時acquireで実行権を得るのは1つだけ。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    _i57_start("par", total=1)
    batch_tracker.record_result("par", "hold", completion_id="7203")

    tokens = [
        batch_tracker.try_acquire_completion_finalize("par", _I57_NOW) for _ in range(5)
    ]
    assert sum(1 for t in tokens if t is not None) == 1


def test_i57_regression_completed_marker_blocks_forever(dynamo, monkeypatch) -> None:
    """T10(#31回帰): 正常finalize後はstale閾値を超えても永久にacquire不可。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    _i57_start("once", total=1)
    batch_tracker.record_result("once", "hold", completion_id="7203")

    token = batch_tracker.try_acquire_completion_finalize("once", _I57_NOW)
    assert token is not None
    assert batch_tracker.mark_completion_finalize_completed("once", token, _I57_NOW) is True

    much_later = _I57_NOW + dt.timedelta(seconds=100_000)
    assert batch_tracker.try_acquire_completion_finalize("once", much_later) is None


# --- Issue #57 B1 Track 2前半: finalize failure persistence --------------------


def test_i57_catchable_failure_is_persisted(dynamo, monkeypatch) -> None:
    """T11: 捕捉可能な例外はfailed_at/failure_reasonとして永続化される。
    completed_atは書かないため、gateの意味論は変わらない。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    _i57_start("fail", total=1)
    batch_tracker.record_result("fail", "hold", completion_id="7203")
    token = batch_tracker.try_acquire_completion_finalize("fail", _I57_NOW)
    assert token is not None

    assert (
        batch_tracker.mark_completion_finalize_failed(
            "fail", token, _I57_NOW, RuntimeError("provider secret=abc123 leaked")
        )
        is True
    )

    item = _i57_item("fail")
    assert item["completion_finalize_failed_at"] == _I57_NOW.isoformat()
    # 例外クラス名のみ。メッセージ本文(秘密情報を含みうる)は保存しない。
    assert item["completion_finalize_failure_reason"] == "RuntimeError"
    assert "secret" not in item["completion_finalize_failure_reason"]
    assert "completion_finalize_completed_at" not in item


def test_i57_normal_finalize_leaves_no_failure_marker(dynamo, monkeypatch) -> None:
    """T12: 正常finalizeではfailed_at/failure_reasonが付かない。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    _i57_start("ok", total=1)
    batch_tracker.record_result("ok", "hold", completion_id="7203")
    token = batch_tracker.try_acquire_completion_finalize("ok", _I57_NOW)
    assert token is not None
    assert batch_tracker.mark_completion_finalize_completed("ok", token, _I57_NOW) is True

    item = _i57_item("ok")
    assert item["completion_finalize_completed_at"] == _I57_NOW.isoformat()
    assert "completion_finalize_failed_at" not in item
    assert "completion_finalize_failure_reason" not in item


def test_i57_timeout_like_state_has_no_failed_at_but_is_stale_detectable(
    dynamo, monkeypatch
) -> None:
    """T13(§9の契約): Lambda timeout/強制終了は捕捉できないため
    failed_atは付かない。started_atあり かつ completed_atなし がstale候補の
    第一条件であり、failed_atの不在を「失敗していない」と解釈してはならない。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    _i57_start("to", total=1)
    batch_tracker.record_result("to", "hold", completion_id="7203")
    token = batch_tracker.try_acquire_completion_finalize("to", _I57_NOW)
    assert token is not None
    # ここでプロセスが強制終了した想定(mark_*を一切呼ばない)

    item = _i57_item("to")
    assert "completion_finalize_started_at" in item
    assert "completion_finalize_completed_at" not in item
    assert "completion_finalize_failed_at" not in item, (
        "timeoutでfailed_atが付くと主張してはならない"
    )

    # stale閾値経過後はtakeover可能(=stale候補として識別できる)。
    stale_later = _I57_NOW + dt.timedelta(
        seconds=batch_tracker._COMPLETION_FINALIZE_STALE_AFTER_SECONDS + 1
    )
    assert batch_tracker.try_acquire_completion_finalize("to", stale_later) is not None


def test_i57_failure_marker_requires_token_ownership(dynamo, monkeypatch) -> None:
    """所有権を失った旧ownerはfailure markerを書けない(takeover側を上書きしない)。"""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    _i57_start("own", total=1)
    batch_tracker.record_result("own", "hold", completion_id="7203")
    old_token = batch_tracker.try_acquire_completion_finalize("own", _I57_NOW)
    assert old_token is not None
    later = _I57_NOW + dt.timedelta(
        seconds=batch_tracker._COMPLETION_FINALIZE_STALE_AFTER_SECONDS + 1
    )
    new_token = batch_tracker.try_acquire_completion_finalize("own", later)
    assert new_token is not None and new_token != old_token

    assert (
        batch_tracker.mark_completion_finalize_failed(
            "own", old_token, later, RuntimeError("stale owner")
        )
        is False
    )
    assert "completion_finalize_failed_at" not in _i57_item("own")
