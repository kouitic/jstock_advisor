"""Issue #57 Phase B2: finalize-only recovery の契約テスト。

DynamoDB の ConditionExpression / Set 意味論に依存する部分は手組みフェイクでは
検証できないため moto(mock_aws)で実テーブルを作成する
(tests/unit/test_watchlist_batch_tracker.py と同じパターン)。
"""

from __future__ import annotations

import datetime as dt

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.entities.enums import ExecutionMode, NotificationMode
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.infrastructure.aws.batch_tracker import BatchFamily
from jstock_advisor.lambda_handlers import _finalize_recovery as recovery

_REGION = "ap-northeast-1"
_BATCH_TABLE = "jstock-batch_runs"
_NOW = dt.datetime(2026, 8, 31, 23, 0, tzinfo=dt.UTC)


@pytest.fixture
def dynamo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-fn")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_BATCH_TABLE,
            KeySchema=[{"AttributeName": "batch_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "batch_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def _start(
    batch_id: str,
    total: int = 1,
    family: BatchFamily = BatchFamily.BUY_CANDIDATES,
    context: ExecutionContext | None = None,
) -> None:
    batch_tracker.start_batch(
        batch_id, total, _NOW, family, context or ExecutionContext.normal()
    )


def _complete_all(batch_id: str, codes: list[str]) -> None:
    for code in codes:
        batch_tracker.record_result(batch_id, "hold", completion_id=code)


def _item(batch_id: str) -> dict:
    return (
        boto3.resource("dynamodb", region_name=_REGION)
        .Table(_BATCH_TABLE)
        .get_item(Key={"batch_id": batch_id})["Item"]
    )


def _payload(batch_id: str, family: BatchFamily, mode: str = "NORMAL") -> dict:
    return {
        recovery.RECOVERY_ACTION_KEY: recovery.FINALIZE_ONLY_ACTION,
        "batch_id": batch_id,
        "batch_family": family.value,
        "execution_mode": mode,
    }


# --- marker / context の永続化 -------------------------------------------------


def test_b2_buy_family_marker_is_persisted(dynamo) -> None:
    """T1: buyのstart_batchでfamily markerが保存される。"""
    _start("b-buy", family=BatchFamily.BUY_CANDIDATES)
    assert _item("b-buy")["batch_family"] == "BUY_CANDIDATES"


def test_b2_holdings_family_marker_is_persisted(dynamo) -> None:
    """T2: holdingsのstart_batchでfamily markerが保存される。"""
    _start("b-hold", family=BatchFamily.HOLDINGS_WATCHLIST)
    assert _item("b-hold")["batch_family"] == "HOLDINGS_WATCHLIST"


def test_b2_execution_context_round_trip(dynamo) -> None:
    """T3: 実行文脈が保存され、同じ値として復元される。"""
    _start("b-normal", context=ExecutionContext.normal())
    _start(
        "b-validation",
        context=ExecutionContext(
            mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.DRY_RUN
        ),
    )

    normal = batch_tracker.get_completion_batch("b-normal")
    validation = batch_tracker.get_completion_batch("b-validation")
    assert normal is not None and validation is not None
    assert normal.execution_context == ExecutionContext.normal()
    assert validation.execution_context is not None
    assert validation.execution_context.mode == ExecutionMode.VALIDATION
    assert validation.execution_context.notification_mode == NotificationMode.DRY_RUN


def test_b2_unknown_family_value_is_not_resolved(dynamo) -> None:
    """T4: 未知のfamily値はNoneへ落とす(fail-closeの前提)。"""
    boto3.resource("dynamodb", region_name=_REGION).Table(_BATCH_TABLE).put_item(
        Item={"batch_id": "b-unknown", "total": 1, "completed": 1, "batch_family": "NOPE"}
    )
    record = batch_tracker.get_completion_batch("b-unknown")
    assert record is not None
    assert record.family is None


def test_b2_inconsistent_context_is_rejected(dynamo) -> None:
    """NORMAL + DRY_RUN は resolve_execution_context() が禁止する組み合わせ。
    壊れた記録として拒否し、NORMAL+SENDへ推測補正しない。"""
    boto3.resource("dynamodb", region_name=_REGION).Table(_BATCH_TABLE).put_item(
        Item={
            "batch_id": "b-bad",
            "total": 1,
            "completed": 1,
            "batch_family": "BUY_CANDIDATES",
            "execution_mode": "NORMAL",
            "notification_mode": "DRY_RUN",
        }
    )
    record = batch_tracker.get_completion_batch("b-bad")
    assert record is not None
    assert record.execution_context is None


# --- payload validation(fail-close) -------------------------------------------


def test_b2_unknown_action_is_rejected(dynamo) -> None:
    """T15: 未知のrecovery_actionは拒否する。"""
    _start("b1")
    _complete_all("b1", ["7203"])
    event = _payload("b1", BatchFamily.BUY_CANDIDATES) | {"recovery_action": "SOMETHING"}
    assert (
        recovery.resolve_finalize_only_request(
            event, BatchFamily.BUY_CANDIDATES, ExecutionContext.normal()
        )
        is None
    )


@pytest.mark.parametrize("bad_batch_id", [None, "", 123])
def test_b2_malformed_batch_id_is_rejected(dynamo, bad_batch_id: object) -> None:
    """T15: batch_idが無い/型不正なpayloadは拒否する。"""
    event = _payload("b1", BatchFamily.BUY_CANDIDATES) | {"batch_id": bad_batch_id}
    assert (
        recovery.resolve_finalize_only_request(
            event, BatchFamily.BUY_CANDIDATES, ExecutionContext.normal()
        )
        is None
    )


def test_b2_cross_family_payload_is_rejected(dynamo) -> None:
    """T16: buy Lambdaへholdingsのバッチを渡しても実行しない。"""
    _start("b-hold", family=BatchFamily.HOLDINGS_WATCHLIST)
    _complete_all("b-hold", ["owner-a#8306"])
    event = _payload("b-hold", BatchFamily.HOLDINGS_WATCHLIST)
    assert (
        recovery.resolve_finalize_only_request(
            event, BatchFamily.BUY_CANDIDATES, ExecutionContext.normal()
        )
        is None
    )


def test_b2_persisted_family_mismatch_is_rejected(dynamo) -> None:
    """payloadだけを書き換えても、永続化されたfamilyと一致しなければ拒否する。"""
    _start("b-hold", family=BatchFamily.HOLDINGS_WATCHLIST)
    _complete_all("b-hold", ["owner-a#8306"])
    event = _payload("b-hold", BatchFamily.BUY_CANDIDATES)
    assert (
        recovery.resolve_finalize_only_request(
            event, BatchFamily.BUY_CANDIDATES, ExecutionContext.normal()
        )
        is None
    )


def test_b2_validation_batch_is_not_auto_redriven(dynamo) -> None:
    """T17: VALIDATIONバッチは自動re-driveの対象にしない(NORMALへ昇格しない)。"""
    _start(
        "b-val",
        context=ExecutionContext(
            mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.DRY_RUN
        ),
    )
    _complete_all("b-val", ["7203"])
    assert (
        recovery.resolve_finalize_only_request(
            _payload("b-val", BatchFamily.BUY_CANDIDATES),
            BatchFamily.BUY_CANDIDATES,
            ExecutionContext.normal(),
        )
        is None
    )


def test_b2_missing_batch_record_is_rejected(dynamo) -> None:
    """TTL経過等でbatch項目が無ければ何もしない。"""
    assert (
        recovery.resolve_finalize_only_request(
            _payload("gone", BatchFamily.BUY_CANDIDATES),
            BatchFamily.BUY_CANDIDATES,
            ExecutionContext.normal(),
        )
        is None
    )


def test_b2_already_finalized_is_noop(dynamo) -> None:
    """T24: 既にfinalize済みならno-op。"""
    _start("b-done")
    _complete_all("b-done", ["7203"])
    token = batch_tracker.try_acquire_completion_finalize("b-done", _NOW)
    assert token is not None
    batch_tracker.mark_completion_finalize_completed("b-done", token, _NOW)

    assert (
        recovery.resolve_finalize_only_request(
            _payload("b-done", BatchFamily.BUY_CANDIDATES),
            BatchFamily.BUY_CANDIDATES,
            ExecutionContext.normal(),
        )
        is None
    )


def test_b2_valid_request_is_accepted(dynamo) -> None:
    """正当なpayloadは受理され、復元されたBatchProgressを伴う。"""
    _start("b-ok", total=2)
    _complete_all("b-ok", ["7203", "8306"])
    record = recovery.resolve_finalize_only_request(
        _payload("b-ok", BatchFamily.BUY_CANDIDATES),
        BatchFamily.BUY_CANDIDATES,
        ExecutionContext.normal(),
    )
    assert record is not None
    assert record.progress.total == 2
    assert record.progress.is_complete is True
    assert record.progress.completed_codes == ["7203", "8306"]


def test_b2_payload_builder_matches_persisted_record(dynamo) -> None:
    """reconcilerが送るpayloadは、永続化された値からのみ構築される。"""
    _start("b-pl", family=BatchFamily.HOLDINGS_WATCHLIST)
    _complete_all("b-pl", ["owner-a#8306"])
    record = batch_tracker.get_completion_batch("b-pl")
    assert record is not None
    assert recovery.build_finalize_only_payload(record) == {
        "recovery_action": "FINALIZE_ONLY",
        "batch_id": "b-pl",
        "batch_family": "HOLDINGS_WATCHLIST",
        "execution_mode": "NORMAL",
    }


# --- retry budget(gate取得回数) ------------------------------------------------


def test_b2_attempt_count_increments_with_gate_acquisition(dynamo) -> None:
    """T18/19/20/21: attempt_countは**gate取得回数**。初回finalizeで1、
    recoveryのたびに増え、MAX(3)到達後は取得できない。"""
    _start("b-att")
    _complete_all("b-att", ["7203"])

    stale = dt.timedelta(
        seconds=batch_tracker._COMPLETION_FINALIZE_STALE_AFTER_SECONDS + 1
    )
    assert batch_tracker.try_acquire_completion_finalize("b-att", _NOW) is not None
    assert int(_item("b-att")["completion_finalize_attempt_count"]) == 1

    assert batch_tracker.try_acquire_completion_finalize("b-att", _NOW + stale) is not None
    assert int(_item("b-att")["completion_finalize_attempt_count"]) == 2

    assert (
        batch_tracker.try_acquire_completion_finalize("b-att", _NOW + stale * 2) is not None
    )
    assert int(_item("b-att")["completion_finalize_attempt_count"]) == 3

    # 4回目は上限で取得不可(無限re-driveしない)
    assert batch_tracker.try_acquire_completion_finalize("b-att", _NOW + stale * 3) is None
    assert int(_item("b-att")["completion_finalize_attempt_count"]) == 3


def test_b2_concurrent_acquisitions_do_not_exceed_max(dynamo) -> None:
    """T22: 並行取得でも上限を超えない(条件と加算が同一UpdateItemのため)。"""
    _start("b-conc")
    _complete_all("b-conc", ["7203"])
    stale = dt.timedelta(
        seconds=batch_tracker._COMPLETION_FINALIZE_STALE_AFTER_SECONDS + 1
    )
    acquired = 0
    for i in range(10):
        if batch_tracker.try_acquire_completion_finalize("b-conc", _NOW + stale * i):
            acquired += 1
    assert acquired == batch_tracker.MAX_COMPLETION_FINALIZE_ATTEMPTS
    assert (
        int(_item("b-conc")["completion_finalize_attempt_count"])
        == batch_tracker.MAX_COMPLETION_FINALIZE_ATTEMPTS
    )


def test_b2_attempts_exhausted_flag(dynamo) -> None:
    """T5相当: 上限到達はrecord側からも判定できる(reconcilerの事前skip用)。"""
    _start("b-ex")
    _complete_all("b-ex", ["7203"])
    stale = dt.timedelta(
        seconds=batch_tracker._COMPLETION_FINALIZE_STALE_AFTER_SECONDS + 1
    )
    for i in range(batch_tracker.MAX_COMPLETION_FINALIZE_ATTEMPTS):
        batch_tracker.try_acquire_completion_finalize("b-ex", _NOW + stale * i)
    record = batch_tracker.get_completion_batch("b-ex")
    assert record is not None
    assert record.attempts_exhausted is True


def test_b2_stale_threshold_is_unchanged(dynamo) -> None:
    """T9: catchable failureでもstale閾値は短縮しない(#31の契約を維持)。"""
    _start("b-stale")
    _complete_all("b-stale", ["7203"])
    token = batch_tracker.try_acquire_completion_finalize("b-stale", _NOW)
    assert token is not None
    batch_tracker.mark_completion_finalize_failed(
        "b-stale", token, _NOW, RuntimeError("boom")
    )
    assert "completion_finalize_failed_at" in _item("b-stale")

    # failure markerがあってもstale経過前は取得できない
    assert (
        batch_tracker.try_acquire_completion_finalize(
            "b-stale", _NOW + dt.timedelta(seconds=600)
        )
        is None
    )
    # 経過後は取得できる(T8)
    assert (
        batch_tracker.try_acquire_completion_finalize(
            "b-stale",
            _NOW
            + dt.timedelta(seconds=batch_tracker._COMPLETION_FINALIZE_STALE_AFTER_SECONDS + 1),
        )
        is not None
    )
