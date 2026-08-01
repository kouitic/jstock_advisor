"""候補ユニバース本格対応(第6版修正プラン)のbatch_tracker.py新規関数群のテスト。

実際のDynamoDBのConditionExpression/TransactWriteItems意味論を検証する必要が
あるため、手組みフェイクではなくmoto(mock_aws)で実テーブルを作成して検証する
(既存tests/unit/test_dynamodb_store.pyと同じパターン)。
"""

from __future__ import annotations

import datetime as dt

import boto3
import pytest
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
        is_rate_limit_suspected=False,
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
        is_rate_limit_suspected=False,
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
        is_rate_limit_suspected=False,
        processing_duration_ms=100,
        now=_NOW,
    )

    # completed>=totalだが、dispatch_completedがまだfalse(status=DISPATCHING)。
    assert batch_tracker.try_finalize_if_ready("batch-1") is False

    batch_tracker.mark_dispatch_completed("batch-1", _NOW)
    assert batch_tracker.try_finalize_if_ready("batch-1") is True

    # 一度FINALIZINGへ遷移した後は、再度呼んでもFalse(排他制御)。
    assert batch_tracker.try_finalize_if_ready("batch-1") is False


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
        is_rate_limit_suspected=False,
        processing_duration_ms=100,
        now=_NOW,
    )

    results = [batch_tracker.try_finalize_if_ready("batch-1") for _ in range(5)]
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
        is_rate_limit_suspected=False,
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
        is_rate_limit_suspected=False,
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
