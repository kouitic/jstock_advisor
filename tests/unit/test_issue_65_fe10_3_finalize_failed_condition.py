"""Issue #65 F-E10(3): finalize再試行予算の加算を条件付きにする。

`mark_watchlist_finalize_failed()`の`ADD finalize_attempt_count :one`は**無条件**
だった。2つのfinalizerが同時に走ると予算が倍速で減り、本来の半分の試行回数で
Reconcilerが自動再試行を打ち切る(= 復旧できたはずのバッチが復旧しない)。

★ F-E10(2)(`record_notification_failed`)との違いは、遷移元が**単一に確定しない**
  こと。呼び出し元4箇所はいずれも「条件付き遷移に成功した後のexcept Exception」で、
  例外がfinalizeのどの段階で起きたかによってその時点のstatusが変わる。
  そのため条件は**集合**になる。

★ 許容集合を狭くしすぎると、正当な試行が計上されずReconcilerの再試行上限が
  **永久に発火しない**(= 無限リトライ)という、二重計上より重い逆方向の失敗になる。
  そのためT-7で集合の網羅性を機械的に固定する。

★ 既知の限界(Issue #213 (g) / #65 issuecomment-5618908669で開示済み):
  `mark_watchlist_batch_completed()`が無条件のため、
  FINALIZE_FAILED→COMPLETEDへ「蘇生」してから再び失敗する経路では二重計上が残る。
  本テストはその**現状を明示的に固定**する(黙って残さない。T-9)。

★ 実在の銘柄コード・銘柄名は使用しない。
"""

from __future__ import annotations

import datetime as dt
import logging

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.infrastructure.aws.batch_tracker import (
    EXECUTION_RESULT_NORMAL,
    WatchlistBatchStatus,
    get_watchlist_batch,
    mark_watchlist_batch_completed,
    mark_watchlist_finalize_failed,
    resolve_watchlist_batch_completion_status,
    try_retry_finalize,
)

_NOW = dt.datetime(2026, 9, 10, 7, 0, tzinfo=dt.UTC)
_TABLE = "jstock-batch_runs"

_IN_PROGRESS = [
    WatchlistBatchStatus.FINALIZE_PREPARING,
    WatchlistBatchStatus.WATCHLIST_WRITE_COMPLETED,
    WatchlistBatchStatus.NOTIFICATION_PENDING,
    WatchlistBatchStatus.NOTIFICATION_SENT,
]
_TERMINAL = [
    WatchlistBatchStatus.COMPLETED,
    WatchlistBatchStatus.COMPLETED_WITH_NOTIFICATION_FAILURE,
    WatchlistBatchStatus.ABORTED,
]


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


def _seed(
    batch_id: str, status: WatchlistBatchStatus, attempt_count: int | None = None
) -> None:
    item: dict = {"batch_id": {"S": batch_id}, "status": {"S": status.value}}
    if attempt_count is not None:
        item["finalize_attempt_count"] = {"N": str(attempt_count)}
    boto3.client("dynamodb", region_name="ap-northeast-1").put_item(
        TableName=_TABLE, Item=item
    )


def _attempt_count(batch_id: str) -> int:
    item = get_watchlist_batch(batch_id)
    assert item is not None
    return int(item.get("finalize_attempt_count", 0) or 0)


# --- T-1 / T-1b 条件成立: 正当な試行を取りこぼさない -------------------------------------


@pytest.mark.parametrize("status", _IN_PROGRESS)
def test_every_in_progress_phase_is_counted(dynamo, status: WatchlistBatchStatus) -> None:
    """★ finalizeの4段階の**どこで例外が起きても**+1されること。

    例外の発生位置によってstatusが変わるため、1つでも取りこぼすと
    Reconcilerの再試行上限が発火せず再試行が続く(二重計上より重い)。
    """
    _seed("b-1", status)

    mark_watchlist_finalize_failed("b-1", _NOW, "boom")

    item = get_watchlist_batch("b-1")
    assert item is not None
    assert item["status"] == WatchlistBatchStatus.FINALIZE_FAILED.value
    assert _attempt_count("b-1") == 1


@pytest.mark.parametrize("status", _TERMINAL)
def test_every_completion_status_is_counted(dynamo, status: WatchlistBatchStatus) -> None:
    """★ 完了遷移の**後**の失敗も、現状どおりFINALIZE_FAILEDへ遷移し+1されること。

    `mark_watchlist_batch_completed()`の後にも`_maybe_commit_rotation`/
    `maybe_trigger_maintenance`が残っており、そこで失敗すると現状は
    FINALIZE_FAILEDへ落ちてReconcilerが再試行する。本修正はその挙動を変えない
    (管理者判断 #65 issuecomment-5618875590)。
    """
    _seed("b-2", status)

    mark_watchlist_finalize_failed("b-2", _NOW, "boom")

    item = get_watchlist_batch("b-2")
    assert item is not None
    assert item["status"] == WatchlistBatchStatus.FINALIZE_FAILED.value
    assert _attempt_count("b-2") == 1


# --- T-2 / T-3 条件不成立: 予算を消費しない ---------------------------------------------


def test_finalize_failed_does_not_consume_the_budget(dynamo) -> None:
    """★ 本findingの中心。既にFINALIZE_FAILEDなら**加算されない**こと。"""
    _seed("b-3", WatchlistBatchStatus.FINALIZE_FAILED, attempt_count=1)

    mark_watchlist_finalize_failed("b-3", _NOW, "boom")

    assert _attempt_count("b-3") == 1  # ★ 増えていない
    item = get_watchlist_batch("b-3")
    assert item is not None
    assert item["status"] == WatchlistBatchStatus.FINALIZE_FAILED.value


def test_two_concurrent_finalizers_consume_only_one_attempt(dynamo) -> None:
    """★ 2つのfinalizerを模して2回呼んでも**予算が1しか減らない**こと。

    = 本findingの主張(予算が倍速で減る)の直接検証。
    """
    _seed("b-4", WatchlistBatchStatus.FINALIZE_PREPARING)

    mark_watchlist_finalize_failed("b-4", _NOW, "boom")
    mark_watchlist_finalize_failed("b-4", _NOW, "boom")

    assert _attempt_count("b-4") == 1


@pytest.mark.parametrize(
    "status",
    [WatchlistBatchStatus.RUNNING, WatchlistBatchStatus.DISPATCH_FAILED],
)
def test_unrelated_statuses_are_not_transitioned(
    dynamo, status: WatchlistBatchStatus
) -> None:
    """finalizeに入っていない状態を、勝手にFINALIZE_FAILEDへ落とさないこと。"""
    _seed("b-5", status)

    mark_watchlist_finalize_failed("b-5", _NOW, "boom")

    item = get_watchlist_batch("b-5")
    assert item is not None
    assert item["status"] == status.value  # ★ 状態も書き換えない
    assert _attempt_count("b-5") == 0


def test_missing_item_is_not_created(dynamo) -> None:
    """★「定常でない1回目」: 項目が存在しない場合に**項目を作らない**こと。

    従来は条件が無いため、存在しないbatch_idでも項目が新規作成されていた
    (finalize_attempt_count=1の幽霊項目)。fail-closedで作らない方が安全。
    """
    mark_watchlist_finalize_failed("b-missing", _NOW, "boom")

    assert get_watchlist_batch("b-missing") is None


# --- T-4 / T-5 失敗の可視性 --------------------------------------------------------------


def test_condition_failure_does_not_raise(dynamo) -> None:
    """★ 条件不成立でも例外を送出しないこと。

    送出すると「後片付けの失敗でfinalize全体も落ちる」という別の欠陥になる
    (呼び出し側4箇所はいずれも記録後に**元の例外**を再送出する形で、
    本関数自体が失敗することを前提にしていない)。
    """
    _seed("b-6", WatchlistBatchStatus.FINALIZE_FAILED)

    # 例外が出ればpytestがここで失敗する。
    mark_watchlist_finalize_failed("b-6", _NOW, "boom")


def test_condition_failure_is_warned_not_silently_accepted(
    dynamo, caplog: pytest.LogCaptureFixture
) -> None:
    """記録できなかったことをWARNINGで残すこと。"""
    _seed("b-7", WatchlistBatchStatus.FINALIZE_FAILED, attempt_count=2)

    with caplog.at_level(logging.WARNING):
        mark_watchlist_finalize_failed("b-7", _NOW, "boom")

    assert any(
        "finalize failure not recorded" in r.getMessage() for r in caplog.records
    )


def test_the_normal_path_does_not_warn(dynamo, caplog: pytest.LogCaptureFixture) -> None:
    """★ 通常時はWARNINGを出さないこと(ノイズを増やさない)。"""
    _seed("b-8", WatchlistBatchStatus.FINALIZE_PREPARING)

    with caplog.at_level(logging.WARNING):
        mark_watchlist_finalize_failed("b-8", _NOW, "boom")

    assert "finalize failure not recorded" not in caplog.text


def test_the_warning_does_not_leak_the_error_message(
    dynamo, caplog: pytest.LogCaptureFixture
) -> None:
    """★ Issue #135: batch_id以外の可変文字列をログへ出さないこと。"""
    _seed("b-9", WatchlistBatchStatus.FINALIZE_FAILED)

    with caplog.at_level(logging.WARNING):
        mark_watchlist_finalize_failed("b-9", _NOW, "sensitive-error-detail")

    assert "sensitive-error-detail" not in caplog.text


# --- T-6 再試行の往復が壊れないこと ------------------------------------------------------


def test_the_retry_round_trip_still_counts_each_attempt(dynamo) -> None:
    """★ 実運用の再試行(FAILED→try_retry_finalize→PREPARING→失敗)を3周すると3になること。

    Reconcilerの上限判定(max_finalize_retry_attempts=3)が従来どおり発火する、
    という**平常時の不変**の確認。
    """
    _seed("b-10", WatchlistBatchStatus.FINALIZE_PREPARING)

    for _ in range(3):
        mark_watchlist_finalize_failed("b-10", _NOW, "boom")
        # try_retry_finalizeがFINALIZE_FAILED→FINALIZE_PREPARINGへ戻す(既存の遷移)。
        assert try_retry_finalize("b-10") is True

    assert _attempt_count("b-10") == 3


# --- T-7 許容集合の網羅性(ドリフト防止) -------------------------------------------------


def test_the_allowed_set_reuses_the_shared_in_progress_constant() -> None:
    """★ 4段階を書き写した「2つ目の真実の源」を作っていないこと。

    `mark_finalizing_stuck_as_failed()`が依存している定数と同じものを使う。
    片方だけ更新されると、stuck検知と加算の対象がずれる。
    """
    for status in batch_tracker._FINALIZE_IN_PROGRESS_STATUSES:
        assert status in batch_tracker._FINALIZE_FAILURE_RECORDABLE_STATUSES


def test_the_allowed_set_covers_every_completion_status() -> None:
    """★ 完了遷移が書きうるstatusを**1つも取りこぼしていない**こと。

    `resolve_watchlist_batch_completion_status()`が唯一の判定箇所なので、
    そこから返りうる値を全て列挙して包含を確認する。将来ここへ4つ目が増えたとき、
    許容集合の更新漏れを**このテストが落として教える**
    (取りこぼすと再試行上限が発火せず、無限リトライになる)。
    """
    aborted_result = next(iter(batch_tracker._ABORTED_EXECUTION_RESULTS))
    reachable = {
        resolve_watchlist_batch_completion_status(execution_result, permanently_failed)
        for execution_result in (EXECUTION_RESULT_NORMAL, aborted_result)
        for permanently_failed in (False, True)
    }

    assert reachable  # 空集合で素通りしないこと
    assert reachable <= set(batch_tracker._FINALIZE_FAILURE_RECORDABLE_STATUSES)


def test_finalize_failed_itself_is_not_in_the_allowed_set() -> None:
    """★ 肯定形だけでは「集合が全statusを含む」ケースを見逃すため、
    弾くべき値が実際に**入っていない**ことも固定する(Issue #254 P-1)。"""
    assert (
        WatchlistBatchStatus.FINALIZE_FAILED
        not in batch_tracker._FINALIZE_FAILURE_RECORDABLE_STATUSES
    )


# --- T-8 構造(ADDと条件が同一UpdateItemであること) ---------------------------------------


def test_the_add_and_the_condition_live_in_the_same_update_item() -> None:
    """★ 加算と条件を**別writeへ分けていない**こと。

    分けると「加算したのに遷移していない / 遷移したのに加算されていない」中間状態が
    生まれ、予算管理が破綻する(`try_acquire_completion_finalize()` = Issue #57 B2と
    同じ理由。F-E10(2)で同型の判定を置いたのと同じ形)。
    """
    from pathlib import Path

    source = Path("src/jstock_advisor/infrastructure/aws/batch_tracker.py").read_text(
        encoding="utf-8"
    )
    block = source.split("def mark_watchlist_finalize_failed(", 1)[1]
    block = block.split("\ndef ", 1)[0]
    add_at = block.index("ADD finalize_attempt_count :one")
    condition_at = block.index("ConditionExpression=status_condition")
    update_item_at = block.index("update_item(")
    assert update_item_at < add_at
    assert update_item_at < condition_at
    # ★ 2回目のupdate_itemが無いこと(= 別writeへ分けていない)。
    assert block.count("update_item(") == 1


# --- T-9 既知の限界(Issue #213 (g))を明示的に固定する -------------------------------------


def test_known_limitation_completion_resurrection_still_double_counts(dynamo) -> None:
    """★ **残存する二重計上**を、既知の限界として明示的に固定する。

    `mark_watchlist_batch_completed()`は無条件のため、1人目がFINALIZE_FAILEDにした
    後でも2人目がCOMPLETEDへ「蘇生」でき、そこから再び失敗すると条件が再び成立して
    2回目の加算が起きる。根は完了遷移が無条件であることで、その条件付けは別の設計
    判断を要するため本単位では直さない(Issue #213 (g)。
    #65 issuecomment-5618908669で開示し、issuecomment-5618875590の方針で承認済み)。

    ★ このテストは「望ましい仕様」ではなく**現状の記録**である。#213 (g)を直す際は
      期待値を1へ変えること。
    """
    _seed("b-11", WatchlistBatchStatus.FINALIZE_PREPARING)
    mark_watchlist_batch_completed("b-11", EXECUTION_RESULT_NORMAL, _NOW)

    # 1人目: 後片付けが失敗 -> COMPLETEDは許容集合内なので成立
    mark_watchlist_finalize_failed("b-11", _NOW, "cleanup failed")
    assert _attempt_count("b-11") == 1

    # 2人目: 無条件の完了遷移がFINALIZE_FAILEDをCOMPLETEDへ蘇生させる
    mark_watchlist_batch_completed("b-11", EXECUTION_RESULT_NORMAL, _NOW)
    mark_watchlist_finalize_failed("b-11", _NOW, "cleanup failed")

    assert _attempt_count("b-11") == 2  # ★ 残存する二重計上(#213 (g))
