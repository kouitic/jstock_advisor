"""Issue #65 F-E7: dispatcher末尾例外後、EventBridge Schedulerのretryが同じ論理実行を
新しいbatch_idで再dispatchしてしまう欠陥の修正テスト。

`_derive_batch_id()`(純粋関数。T1/T2/T4/T5/T6)と、そこから導出したbatch_idを
`try_acquire_dispatch_lease()`(既存・未変更)へ渡した場合の実際の排他挙動
(moto。T3/T7/T8/T9相当)、Schedulerのcontext attribute契約(T10)を確認する。

対象外なテスト: try_acquire_dispatch_lease()自体のConditionExpressionの網羅的な
検証はtest_batch_tracker.pyの責務であり、ここでは再定義しない(#65 F-E7が実際に
その既存機構を正しく起動できることのみを確認する)。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import boto3
import pytest
import yaml
from moto import mock_aws

from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.lambda_handlers import watchlist_dispatcher_handler as handler_module

_REGION = "ap-northeast-1"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_PATH = _REPO_ROOT / "infra" / "template.yaml"

# --- T1/T2/T4/T5/T6: _derive_batch_id()は純粋関数 --------------------------------


def test_t1_scheduled_invocation_derives_a_batch_id() -> None:
    """T1: 通常のSchedule起動(scheduled_time付き)でbatch_idが決定される。"""
    event = {"scheduled_time": "2026-09-24T21:00:00Z"}
    now = dt.datetime(2026, 9, 24, 21, 0, 5, tzinfo=dt.UTC)

    batch_id = handler_module._derive_batch_id(event, "watchlist", now)

    assert batch_id
    assert batch_id.startswith("watchlist-")


def test_t2_retry_of_the_same_logical_execution_yields_the_same_batch_id() -> None:
    """T2: 同一Scheduler論理実行のretry(1回目 scheduled_time=X、retry
    scheduled_time=X)は同じbatch_idになる。実際の実行時刻(now)が異なっても
    scheduled_timeが同じであれば結果は同じ(retry安定性)。"""
    event = {"scheduled_time": "2026-09-24T21:00:00Z"}
    first_attempt_now = dt.datetime(2026, 9, 24, 21, 0, 5, tzinfo=dt.UTC)
    retry_now = dt.datetime(2026, 9, 24, 21, 15, 30, tzinfo=dt.UTC)  # 15分後のretry

    first_batch_id = handler_module._derive_batch_id(event, "watchlist", first_attempt_now)
    retry_batch_id = handler_module._derive_batch_id(event, "watchlist", retry_now)

    assert first_batch_id == retry_batch_id


def test_t4_explicit_batch_id_is_used_unchanged_and_scheduled_time_is_ignored() -> None:
    """T4: event["batch_id"]が明示されている既存経路(手動re-run・
    maybe_trigger_maintenanceの決定論的後続起動)は変更しない。scheduled_time
    が同時にあっても無視される。"""
    event = {
        "batch_id": "watchlist-maint-parent-triggered",
        "scheduled_time": "2026-09-24T21:00:00Z",
    }
    now = dt.datetime(2026, 9, 24, 21, 0, 5, tzinfo=dt.UTC)

    batch_id = handler_module._derive_batch_id(event, "watchlist-maint", now)

    assert batch_id == "watchlist-maint-parent-triggered"


def test_t5_a_different_schedule_slot_yields_a_different_batch_id() -> None:
    """T5: 別の定期実行(翌営業日等)はscheduled_timeが異なるため別batch_idになる。"""
    monday = handler_module._derive_batch_id(
        {"scheduled_time": "2026-09-20T21:00:00Z"}, "watchlist", dt.datetime.now(dt.UTC)
    )
    tuesday = handler_module._derive_batch_id(
        {"scheduled_time": "2026-09-21T21:00:00Z"}, "watchlist", dt.datetime.now(dt.UTC)
    )

    assert monday != tuesday


def test_t6_utc_scheduled_time_is_converted_to_jst_before_formatting() -> None:
    """T6: JST 06:00起動のscheduled_timeはUTC表記だと前日21:00Zになる
    (JST = UTC+9)。batch_idの日付部分は、UTC日付をそのまま使うと前日に
    化けてしまうため、domain/jst.py::to_jst()で変換したJST日付
    (2026-09-25、06:00起動当日)を使うことを固定する。"""
    # cron(0 6 ? * MON-FRI *) with ScheduleExpressionTimezone=Asia/Tokyo が
    # 2026-09-25(金)06:00 JSTに起動する場合、AWSが返すscheduled-timeは
    # 常にUTCのため 2026-09-24T21:00:00Z になる(前日のUTC日付)。
    event = {"scheduled_time": "2026-09-24T21:00:00Z"}
    now = dt.datetime(2026, 9, 24, 21, 0, 5, tzinfo=dt.UTC)

    batch_id = handler_module._derive_batch_id(event, "watchlist", now)

    assert "20260925T060000" in batch_id  # JST日付・時刻(9/25 06:00)
    assert "20260924" not in batch_id  # UTC日付(9/24)がそのまま出ていないこと


def test_t6b_naive_scheduled_time_without_offset_defaults_to_utc() -> None:
    """T6b(レビュー対応: PR #556 F1): scheduled_timeにoffsetが無い(naive)場合の
    既定を固定する。AWS公式ドキュメントには常にUTCで渡るという明記は無く、
    offset無しで渡ってきた場合にどう解釈するかはhandler側の既定次第である。
    現行実装はUTCとみなす(offset付きの場合と同じくto_jst()でJST変換する)。
    この既定が崩れると、値自体は変わらず(冪等性は壊れない)batch_idの日時
    表記だけが9時間ずれるため、人が気づかない限りサイレントに誤り続ける
    (レビューで反証G4「naiveをJSTとみなす」がSURVIVEDし、実測で9時間ずれる
    ことを確認済み)。"""
    event = {"scheduled_time": "2026-09-24T21:00:00"}  # offset無し(naive)
    now = dt.datetime(2026, 9, 24, 21, 0, 5, tzinfo=dt.UTC)

    batch_id = handler_module._derive_batch_id(event, "watchlist", now)

    # UTCとみなした場合: 2026-09-24T21:00:00Z -> JST 2026-09-25T06:00:00
    assert "20260925T060000" in batch_id
    # naiveをJSTとみなす変異が入ると 20260924T210000 になる(9時間ずれ)。
    assert "20260924T210000" not in batch_id


def test_malformed_scheduled_time_falls_back_to_the_random_generator() -> None:
    """scheduled_timeが不正な文字列の場合、handler自体を失敗させず、従来の
    時刻+ランダムサフィックス方式へfallbackする(fail-safe)。"""
    event = {"scheduled_time": "not-a-valid-timestamp"}
    now = dt.datetime(2026, 9, 24, 21, 0, 5, tzinfo=dt.UTC)

    batch_id = handler_module._derive_batch_id(event, "watchlist", now)

    assert batch_id.startswith("watchlist-20260924T210005-")
    assert len(batch_id.rsplit("-", 1)[-1]) == 8  # ランダムサフィックス(hex 8桁)


def test_manual_invocation_without_batch_id_or_scheduled_time_is_unchanged() -> None:
    """Scheduler経由でない起動(手動invoke・ローカルテスト等)は、従来どおり
    時刻+ランダムサフィックスで生成される(既存の挙動を変更しない回帰確認)。"""
    event: dict[str, Any] = {}
    now = dt.datetime(2026, 9, 24, 21, 0, 5, tzinfo=dt.UTC)

    batch_id = handler_module._derive_batch_id(event, "watchlist", now)

    assert batch_id.startswith("watchlist-20260924T210005-")
    assert len(batch_id.rsplit("-", 1)[-1]) == 8


# --- T3/T7/T8/T9: 導出したbatch_idが既存のdispatch leaseを正しく起動させる ---------


@pytest.fixture
def moto_batch_runs_dynamodb(monkeypatch: pytest.MonkeyPatch):
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


def test_t3_retry_with_the_same_batch_id_cannot_reacquire_an_active_lease(
    moto_batch_runs_dynamodb: None,
) -> None:
    """T3: 同一論理実行のretryは同じbatch_idになる(T2)結果、リース有効期間内の
    2回目の試行はtry_acquire_dispatch_lease()(既存・未変更)が拒否し、
    二重dispatchが発生しない。job_type・rotation.enabledに一切依存しない
    (batch_id keyのdispatch leaseのみで完結する)ことがF-E7の核心。"""
    scheduled_time = "2026-09-24T21:00:00Z"
    first_now = dt.datetime(2026, 9, 24, 21, 0, 5, tzinfo=dt.UTC)
    retry_now = dt.datetime(2026, 9, 24, 21, 2, 0, tzinfo=dt.UTC)  # リース有効期間内(360秒)

    first_batch_id = handler_module._derive_batch_id(
        {"scheduled_time": scheduled_time}, "watchlist", first_now
    )
    retry_batch_id = handler_module._derive_batch_id(
        {"scheduled_time": scheduled_time}, "watchlist", retry_now
    )
    assert first_batch_id == retry_batch_id  # 前提(T2の再確認)

    first_acquired = batch_tracker.try_acquire_dispatch_lease(
        first_batch_id, "attempt-1", first_now, 360, 72
    )
    retry_acquired = batch_tracker.try_acquire_dispatch_lease(
        retry_batch_id, "attempt-2", retry_now, 360, 72
    )

    assert first_acquired is True
    assert retry_acquired is False  # 二重dispatchされない


def test_t7_rotation_disabled_path_is_still_protected_by_the_dispatch_lease(
    moto_batch_runs_dynamodb: None,
) -> None:
    """T7: rotation.enabled=falseの経路はrotation dispatch leaseを取得しないが
    (watchlist_dispatcher_handler.py:521)、batch_id自体が安定するようになった
    ため、job_type・rotation設定に一切関知しないdispatch lease(batch_id key)
    だけでretry時の二重dispatchを防止できる。"""
    scheduled_time = "2026-09-24T21:00:00Z"
    first_now = dt.datetime(2026, 9, 24, 21, 0, 5, tzinfo=dt.UTC)
    retry_now = dt.datetime(2026, 9, 24, 21, 2, 0, tzinfo=dt.UTC)

    batch_id = handler_module._derive_batch_id(
        {"scheduled_time": scheduled_time}, "watchlist", first_now
    )

    first_acquired = batch_tracker.try_acquire_dispatch_lease(
        batch_id, "attempt-1", first_now, 360, 72
    )
    retry_acquired = batch_tracker.try_acquire_dispatch_lease(
        batch_id, "attempt-2", retry_now, 360, 72
    )

    assert first_acquired is True
    assert retry_acquired is False


def test_t8_watchlist_maintenance_path_is_still_protected_by_the_dispatch_lease(
    moto_batch_runs_dynamodb: None,
) -> None:
    """T8: WATCHLIST_MAINTENANCE(rotation_lease_held=False固定)も、
    batch_prefix="watchlist-maint"で導出したbatch_idがdispatch leaseにより
    retryでの二重dispatchから保護される。"""
    scheduled_time = "2026-09-24T21:00:00Z"
    first_now = dt.datetime(2026, 9, 24, 21, 0, 5, tzinfo=dt.UTC)
    retry_now = dt.datetime(2026, 9, 24, 21, 2, 0, tzinfo=dt.UTC)

    batch_id = handler_module._derive_batch_id(
        {"scheduled_time": scheduled_time}, "watchlist-maint", first_now
    )
    assert batch_id.startswith("watchlist-maint-")

    first_acquired = batch_tracker.try_acquire_dispatch_lease(
        batch_id, "attempt-1", first_now, 360, 72
    )
    retry_acquired = batch_tracker.try_acquire_dispatch_lease(
        batch_id, "attempt-2", retry_now, 360, 72
    )

    assert first_acquired is True
    assert retry_acquired is False


def test_t9_retry_after_a_late_failure_near_the_end_of_dispatch_does_not_start_a_new_batch(
    moto_batch_runs_dynamodb: None,
) -> None:
    """T9: dispatcher末尾(SQS送信・mark_dispatch_completed・finalize呼び出し)の
    いずれかで未捕捉の例外が起き、Lambda呼び出し自体が失敗した後のretryでも、
    (同じscheduled_timeから同じbatch_idが導出されるため)新規batchとして
    dispatchされない。status=DISPATCHINGのまま・リース有効期間内であっても、
    有効期間が過ぎていても、いずれの場合もretryは「新規batch」としては扱われない
    (有効期間内はリース拒否〔本テスト〕。期間経過後は同一batch_idの再開に
    なり、新規batchにはならない)。"""
    scheduled_time = "2026-09-24T21:00:00Z"
    original_now = dt.datetime(2026, 9, 24, 21, 0, 5, tzinfo=dt.UTC)
    # dispatcher末尾(SQS送信直前〜completed直前)で例外が起きたと仮定し、
    # status=DISPATCHINGのまま終わる(mark_dispatch_completedは呼ばれない)。
    original_batch_id = handler_module._derive_batch_id(
        {"scheduled_time": scheduled_time}, "watchlist", original_now
    )
    assert batch_tracker.try_acquire_dispatch_lease(
        original_batch_id, "original-attempt", original_now, 360, 72
    )

    # Schedulerのretry。同じscheduled_timeのため同一batch_idになる。
    retry_now = dt.datetime(2026, 9, 24, 21, 3, 0, tzinfo=dt.UTC)  # リース有効期間内
    retry_batch_id = handler_module._derive_batch_id(
        {"scheduled_time": scheduled_time}, "watchlist", retry_now
    )

    assert retry_batch_id == original_batch_id  # 新規batchにならない(同一batch_id)
    retry_acquired = batch_tracker.try_acquire_dispatch_lease(
        retry_batch_id, "retry-attempt", retry_now, 360, 72
    )
    assert retry_acquired is False  # 二重dispatchされない


# --- T10: Scheduler Inputの契約をinfraテストで固定 --------------------------------


def _load_template_resources() -> dict[str, Any]:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda _l, suffix, node: {f"Fn::{suffix}": node.value})
    loaded = yaml.load(_TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_Loader)
    return loaded["Resources"]


def test_t10_scheduler_input_contract_is_fixed_in_the_template() -> None:
    """T10: WatchlistDispatcherFunctionのWeekdayMorning ScheduleがInputで
    scheduled_timeを渡すこと、既存のRetryPolicy(#318の契約。MaximumRetryAttempts=2
    / MaximumEventAgeInSeconds=3600)を変更していないことを固定する。"""
    resources = _load_template_resources()
    events = resources["WatchlistDispatcherFunction"]["Properties"]["Events"]
    schedule = events["WeekdayMorning"]["Properties"]

    assert schedule["Input"] == '{"scheduled_time": "<aws.scheduler.scheduled-time>"}'
    # #318の契約(retry回数/event age)は変更しない。
    assert schedule["RetryPolicy"]["MaximumRetryAttempts"] == 2
    assert schedule["RetryPolicy"]["MaximumEventAgeInSeconds"] == 3600
    # DLQは#349/#132のscopeのまま(#318が意図的に未設置)。
    assert "DeadLetterConfig" not in schedule
