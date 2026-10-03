"""Issue #573(#65 F-E10(1)): watchlist batch の完了遷移を条件付きにする(fencing)。

`mark_watchlist_batch_completed()`は無条件のSETだった。2つ目のfinalizer実行が完了確定
まで到達すると、1つ目が既に完了確定させた回に対して、後片付け(`_maybe_commit_rotation`
= rotation lease解放+cursor前進、`maybe_trigger_maintenance` = 後続バッチ起動)が
**二重に**走りうる。

確認すること:

    T1  許容する6状態(DISPATCHING + 進行中4段階 + NOTIFICATION_FAILED)から完了でき、
        status / execution_resultが期待どおりに書かれる(True)
    T2  許容外(終端3状態・FINALIZE_FAILED・RUNNING等)からは何も変えず、例外も送出せずFalse
        (B-1: 例外にすると2人目の外側のexceptがFINALIZE_FAILEDへ巻き戻す)
    T3  許容集合の網羅性: 全statusが「許容」か「レビュー済みの拒否」のどちらかに分類される
        (新しいstatusが増えたら、このテストが落ちてレビューを強いる)
    T4  2人目の`_finish_batch`は後片付けを走らせない(B-2)/ 1人目は従来どおり走る
    T5  2人目が完了済みバッチをFINALIZE_FAILEDへ巻き戻さない(B-1)
    T6  FINALIZE_FAILED→COMPLETEDの蘇生が起きない / Reconciler経由の再試行は成立する
    T7  失敗の可視性(WARNINGの有無・可変文字列を出さない)
    T8  構造(条件とSETが同一のUpdateItem・例外を送出しない)

★ 同時実行そのものはunitで固定する(Productionへのfailure injectionは行わない)。
★ 実在の銘柄コード・銘柄名は使用しない。
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.infrastructure.aws.batch_tracker import (
    EXECUTION_RESULT_NORMAL,
    WatchlistBatchStatus,
    get_watchlist_batch,
    mark_watchlist_batch_completed,
    try_retry_finalize,
)
from jstock_advisor.services import watchlist_batch_finalizer as finalizer_module

_NOW = dt.datetime(2026, 9, 10, 7, 0, tzinfo=dt.UTC)
_LATER = dt.datetime(2026, 9, 10, 7, 5, tzinfo=dt.UTC)
_TABLE = "jstock-batch_runs"
_S = WatchlistBatchStatus

# 人がレビューした許容集合(テスト内のリテラル。実装の定数を写さない)。
_REVIEWED_ALLOWED = frozenset(
    {
        _S.DISPATCHING,
        _S.FINALIZE_PREPARING,
        _S.WATCHLIST_WRITE_COMPLETED,
        _S.NOTIFICATION_PENDING,
        _S.NOTIFICATION_SENT,
        _S.NOTIFICATION_FAILED,
    }
)
# 人がレビューした拒否の集合(許容集合の補集合。理由は各行)。
_REVIEWED_REJECTED = {
    _S.COMPLETED: "既に完了確定済み(二重確定の防止)",
    _S.COMPLETED_WITH_NOTIFICATION_FAILURE: "既に完了確定済み",
    _S.ABORTED: "既に完了確定済み",
    _S.FINALIZE_FAILED: "蘇生の防止(正当な再試行はFINALIZE_PREPARINGへ戻してから走る)",
    _S.RUNNING: "別のライフサイクル(まだfinalizeに入っていない)",
    _S.DISPATCH_FAILED: "別のライフサイクル",
}


@pytest.fixture
def dynamo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-northeast-1")
        client.create_table(
            TableName=_TABLE,
            KeySchema=[{"AttributeName": "batch_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "batch_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def _seed(batch_id: str, status: WatchlistBatchStatus, **extra: Any) -> None:
    item: dict[str, Any] = {
        "batch_id": {"S": batch_id},
        "status": {"S": status.value},
        "updated_at": {"S": _NOW.isoformat()},
    }
    for key, value in extra.items():
        item[key] = {"S": value}
    boto3.client("dynamodb", region_name="ap-northeast-1").put_item(TableName=_TABLE, Item=item)


def _item(batch_id: str) -> dict[str, Any]:
    item = get_watchlist_batch(batch_id)
    assert item is not None
    return item


# --- T1 許容する状態から完了できる ------------------------------------------------------------


@pytest.mark.parametrize("status", sorted(_REVIEWED_ALLOWED, key=lambda s: s.value))
def test_t1_every_allowed_status_can_complete(dynamo, status: WatchlistBatchStatus) -> None:
    """★ 取りこぼすと「正当な完了が弾かれて日が終端しない」。DISPATCHINGは候補0件の日(F-E5)。"""
    _seed("b-1", status)

    assert mark_watchlist_batch_completed("b-1", EXECUTION_RESULT_NORMAL, _LATER) is True

    item = _item("b-1")
    assert item["status"] == WatchlistBatchStatus.COMPLETED.value
    assert item["execution_result"] == EXECUTION_RESULT_NORMAL
    assert item["updated_at"] == _LATER.isoformat()


def test_t1b_the_status_written_is_still_decided_by_the_result_and_the_failure_flag(
    dynamo,
) -> None:
    aborted_result = sorted(batch_tracker._ABORTED_EXECUTION_RESULTS)[0]
    _seed("b-aborted", _S.FINALIZE_PREPARING)
    _seed("b-notification-failed", _S.NOTIFICATION_FAILED)

    assert mark_watchlist_batch_completed("b-aborted", aborted_result, _LATER) is True
    assert (
        mark_watchlist_batch_completed(
            "b-notification-failed",
            EXECUTION_RESULT_NORMAL,
            _LATER,
            notification_permanently_failed=True,
        )
        is True
    )

    assert _item("b-aborted")["status"] == _S.ABORTED.value
    assert _item("b-aborted")["execution_result"] == aborted_result
    assert _item("b-notification-failed")["status"] == _S.COMPLETED_WITH_NOTIFICATION_FAILURE.value


# --- T2 許容外は何も変えず False(例外なし) ----------------------------------------------------


@pytest.mark.parametrize("status", sorted(_REVIEWED_REJECTED, key=lambda s: s.value))
def test_t2_a_rejected_status_changes_nothing_and_returns_false(
    dynamo, status: WatchlistBatchStatus
) -> None:
    _seed("b-2", status, execution_result="previous-result")

    assert mark_watchlist_batch_completed("b-2", EXECUTION_RESULT_NORMAL, _LATER) is False

    item = _item("b-2")
    assert item["status"] == status.value
    assert item["execution_result"] == "previous-result"
    assert item["updated_at"] == _NOW.isoformat()


def test_t2b_a_missing_item_is_not_created(dynamo) -> None:
    """「定常でない1回目」: 存在しないbatch_idでも項目を作らない(従来は幽霊項目ができた)。"""
    assert mark_watchlist_batch_completed("b-missing", EXECUTION_RESULT_NORMAL, _LATER) is False
    assert get_watchlist_batch("b-missing") is None


# --- T3 許容集合の網羅性 ---------------------------------------------------------------------


def test_t3_the_allowed_set_is_exactly_the_reviewed_set() -> None:
    assert set(batch_tracker._COMPLETION_ALLOWED_FROM_STATUSES) == _REVIEWED_ALLOWED
    # 重複なく列挙されている(同じ値を二重に書いていない)
    assert len(batch_tracker._COMPLETION_ALLOWED_FROM_STATUSES) == len(_REVIEWED_ALLOWED)


def test_t3b_every_status_is_either_allowed_or_a_reviewed_rejection() -> None:
    """★ 新しいstatusが増えたとき、許容か拒否かのレビューを強いる(黙って拒否側に落とさない)。

    ここに無い値(TIMEOUT系)は、finalizeの完了遷移に到達しない別ライフサイクル。
    """
    unclassified = (
        set(WatchlistBatchStatus)
        - _REVIEWED_ALLOWED
        - set(_REVIEWED_REJECTED)
        - {
            _S.TIMEOUT_FINALIZING,
            _S.TIMED_OUT,
            _S.TIMEOUT_FINALIZE_FAILED,
        }
    )
    assert unclassified == set(), f"未分類のstatus: {sorted(s.value for s in unclassified)}"


def test_t3c_the_allowed_set_reuses_the_shared_in_progress_constant() -> None:
    """★ 進行中4段階を書き写した「2つ目の真実の源」を作らない(F-E10(3)と同じ)。"""
    for status in batch_tracker._FINALIZE_IN_PROGRESS_STATUSES:
        assert status in batch_tracker._COMPLETION_ALLOWED_FROM_STATUSES


def test_t3d_a_rejected_status_is_really_absent_from_the_allowed_set() -> None:
    """肯定形だけでは「集合が全statusを含む」ケースを見逃すため、弾くべき値が入っていないことも固定する。"""
    for status in _REVIEWED_REJECTED:
        assert status not in batch_tracker._COMPLETION_ALLOWED_FROM_STATUSES


def test_t3e_the_terminal_statuses_the_completion_can_write_are_all_rejected() -> None:
    """完了遷移が書きうる終端3状態(`resolve_watchlist_batch_completion_status()`の全出力)は、
    すべて「完了確定済み」として拒否側にある(2人目をここで弾く)。"""
    aborted_result = sorted(batch_tracker._ABORTED_EXECUTION_RESULTS)[0]
    reachable = {
        batch_tracker.resolve_watchlist_batch_completion_status(result, failed)
        for result in (EXECUTION_RESULT_NORMAL, aborted_result)
        for failed in (False, True)
    }
    assert reachable == {_S.COMPLETED, _S.COMPLETED_WITH_NOTIFICATION_FAILURE, _S.ABORTED}
    for status in reachable:
        assert status not in batch_tracker._COMPLETION_ALLOWED_FROM_STATUSES


# --- T4 / T5 2人目のfinalizer ---------------------------------------------------------------


class _Spy:
    def __init__(self) -> None:
        self.rotation_commits: list[str] = []
        self.maintenance_triggers: list[str] = []


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    recorder = _Spy()
    monkeypatch.setattr(
        finalizer_module,
        "_maybe_commit_rotation",
        lambda batch_id, batch_item, records, now: recorder.rotation_commits.append(batch_id),
    )
    monkeypatch.setattr(
        finalizer_module,
        "maybe_trigger_maintenance",
        lambda batch_id, batch_item, now, config, final_status: (
            recorder.maintenance_triggers.append(batch_id)
        ),
    )
    return recorder


def _finish(batch_id: str) -> None:
    finalizer_module._finish_batch(
        batch_id,
        _LATER,
        _NOW,
        {"finalize_batch_audit_recorded": True},  # 監査の記録は別経路(本テストの対象外)
        [],
        {},
        object(),  # type: ignore[arg-type]
        EXECUTION_RESULT_NORMAL,
        [],
        [],
        False,
        False,
    )


def test_t4_the_second_finisher_skips_the_post_completion_work_and_the_first_does_not(
    dynamo, spy: _Spy
) -> None:
    """★ 1人目は従来どおり後片付けを行い、2人目(完了済みの回へ到達)は行わない(B-2)。"""
    _seed("b-4", _S.NOTIFICATION_SENT)

    _finish("b-4")  # 1人目
    assert spy.rotation_commits == ["b-4"]
    assert spy.maintenance_triggers == ["b-4"]

    _finish("b-4")  # 2人目: 既にCOMPLETED
    assert spy.rotation_commits == ["b-4"]  # ★ 増えていない
    assert spy.maintenance_triggers == ["b-4"]  # ★ 増えていない


def test_t5_the_second_finisher_does_not_roll_the_completed_batch_back(dynamo, spy: _Spy) -> None:
    """★ B-1: 2人目の条件不成立は例外にならず、完了済みバッチはFINALIZE_FAILEDへ巻き戻らない。"""
    _seed("b-5", _S.NOTIFICATION_SENT)

    _finish("b-5")
    _finish("b-5")  # 例外が出ればpytestがここで失敗する

    assert _item("b-5")["status"] == _S.COMPLETED.value
    assert "finalize_attempt_count" not in _item("b-5")


def test_t4b_a_run_that_completes_the_batch_runs_the_work_for_every_entry_status(
    dynamo, spy: _Spy
) -> None:
    """許容する6状態のどこから完了しても、1人目の後片付けは従来どおり走る(正当な完了を弾かない)。"""
    for index, status in enumerate(sorted(_REVIEWED_ALLOWED, key=lambda s: s.value)):
        _seed(f"b-{index}", status)
        _finish(f"b-{index}")
    assert len(spy.rotation_commits) == len(_REVIEWED_ALLOWED)
    assert len(spy.maintenance_triggers) == len(_REVIEWED_ALLOWED)


# --- T6 蘇生の防止と、Reconciler経由の再試行 ----------------------------------------------------


def test_t6_finalize_failed_is_not_revived_and_the_retry_round_trip_still_completes(
    dynamo,
) -> None:
    _seed("b-6", _S.FINALIZE_FAILED)

    # 蘇生しない
    assert mark_watchlist_batch_completed("b-6", EXECUTION_RESULT_NORMAL, _LATER) is False
    assert _item("b-6")["status"] == _S.FINALIZE_FAILED.value

    # 正当な再試行(FINALIZE_FAILED→FINALIZE_PREPARING)を経由すれば完了できる
    assert try_retry_finalize("b-6") is True
    assert mark_watchlist_batch_completed("b-6", EXECUTION_RESULT_NORMAL, _LATER) is True
    assert _item("b-6")["status"] == _S.COMPLETED.value


# --- T7 失敗の可視性 -------------------------------------------------------------------------


def test_t7_a_rejected_completion_is_warned_with_the_batch_id_only(
    dynamo, caplog: pytest.LogCaptureFixture
) -> None:
    _seed("b-7", _S.COMPLETED)

    with caplog.at_level(logging.WARNING):
        mark_watchlist_batch_completed("b-7", "sensitive-result-detail", _LATER)

    assert any("watchlist batch completion not recorded" in r.getMessage() for r in caplog.records)
    assert "batch_id=b-7" in caplog.text
    assert "sensitive-result-detail" not in caplog.text  # Issue #135: 可変文字列を出さない


def test_t7b_the_normal_path_does_not_warn(dynamo, caplog: pytest.LogCaptureFixture) -> None:
    _seed("b-8", _S.NOTIFICATION_SENT)

    with caplog.at_level(logging.WARNING):
        mark_watchlist_batch_completed("b-8", EXECUTION_RESULT_NORMAL, _LATER)

    assert "completion not recorded" not in caplog.text


def test_t7c_the_second_finisher_is_warned(
    dynamo, spy: _Spy, caplog: pytest.LogCaptureFixture
) -> None:
    _seed("b-9", _S.NOTIFICATION_SENT)
    _finish("b-9")

    with caplog.at_level(logging.WARNING):
        _finish("b-9")

    assert "skipping post-completion work batch_id=b-9" in caplog.text


# --- T8 構造 -----------------------------------------------------------------------------------


def _function_source(name: str) -> str:
    source = Path("src/jstock_advisor/infrastructure/aws/batch_tracker.py").read_text(
        encoding="utf-8"
    )
    block = source.split(f"def {name}(", 1)[1]
    return block.split("\ndef ", 1)[0]


def test_t8_the_condition_and_the_set_live_in_the_same_update_item() -> None:
    """★ 条件とSETを別writeへ分けていない(分けると「遷移していないのに書いた」中間状態ができる)。"""
    block = _function_source("mark_watchlist_batch_completed")
    update_item_at = block.index("update_item(")
    assert update_item_at < block.index("ConditionExpression=allowed_condition")
    assert update_item_at < block.index('"SET #status = :status, execution_result = :result')
    assert block.count("update_item(") == 1


def test_t8b_a_condition_failure_returns_false_and_other_errors_are_reraised() -> None:
    block = _function_source("mark_watchlist_batch_completed")
    assert "_TRANSACTION_CONDITION_FAILURE_CODES" in block
    assert "return False" in block
    assert "raise" in block


# --- T9 maintenance の完了(戻り値を使うのは WARNING とログのみ) -------------------------------


def _run_maintenance_finalize(monkeypatch: pytest.MonkeyPatch, completed: bool) -> None:
    from tests.unit import test_issue_141_224_watchlist_maintenance_observability as obs

    item = obs._item("1111", created_at=obs._NOW - dt.timedelta(days=120))
    monkeypatch.setattr(
        finalizer_module,
        "query_all_candidate_progress",
        lambda *a, **k: [obs._record("1111", None)],
    )
    monkeypatch.setattr(
        finalizer_module, "WatchlistRepository", lambda: obs._FakeWatchlistRepository([item])
    )
    monkeypatch.setattr(
        finalizer_module,
        "WatchlistRemovalHistoryRepository",
        lambda *a, **k: obs._FakeWatchlistRemovalHistoryRepository(),
    )
    monkeypatch.setattr(
        finalizer_module, "get_watchlist_batch", lambda _b: {"batch_id": "watchlist-maint-1"}
    )
    monkeypatch.setattr(finalizer_module, "record_batch_audit", lambda **kw: None)
    monkeypatch.setattr(
        finalizer_module, "mark_watchlist_batch_completed", lambda *a, **k: completed
    )
    finalizer_module._finalize_maintenance_completed("watchlist-maint-1", obs._NOW, obs._CONFIG)


def test_t9_a_maintenance_completion_that_was_not_recorded_is_not_logged_as_finalized(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        _run_maintenance_finalize(monkeypatch, completed=False)

    assert "watchlist_maintenance finalized" not in caplog.text
    assert (
        "watchlist_maintenance completion was not recorded by this run batch_id=watchlist-maint-1"
        in caplog.text
    )


def test_t9b_a_recorded_maintenance_completion_is_logged_as_finalized(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        _run_maintenance_finalize(monkeypatch, completed=True)

    assert "watchlist_maintenance finalized batch_id=watchlist-maint-1" in caplog.text
    assert "completion was not recorded" not in caplog.text
