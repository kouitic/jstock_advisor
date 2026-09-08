"""Issue #286(#70 F-B4 / F-B8): watchlist系handlerのexecution_mode契約。

## 何を固定するのか

```
F-B4  4 handlerが `execution_mode` / `notification_mode` を**黙殺しない**
      -> 対応はせず、指定されたら例外で止める(REJECTS_EXPLICITLY)
      -> 指定が無いとき(EventBridge Schedulerの自動実行)は**従来どおり**

F-B8  batch auditの `execution_mode` が実際の起動経路を反映する
      -> 修正前は8か所すべてが "scheduled" のハードコードだった
```

★ 「対応する(VALIDATIONを受け付ける)」を選ばなかった理由は
  `_watchlist_execution_mode` のdocstringにある。watchlistには検証用の
  隔離が存在せず、受け付けると**検証のつもりで本番のwatchlistと
  rotation cursorを変えてしまう**ためである。

★ Productionへのfailure injectionは行わない。ローカルでhandlerを直接呼ぶ。
  銘柄コードは実在しない "0000" 系の架空値のみを使う。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.lambda_handlers import (
    watchlist_batch_reconciler_handler,
    watchlist_dispatcher_handler,
    watchlist_terminal_failure_handler,
    watchlist_worker_handler,
)
from jstock_advisor.lambda_handlers._watchlist_execution_mode import (
    REJECTED_KEYS,
    WatchlistExecutionModeNotSupportedError,
    reject_execution_mode,
)
from jstock_advisor.services.watchlist_screening_audit import (
    EXECUTION_MODE_MANUAL,
    EXECUTION_MODE_SCHEDULED,
    EXECUTION_MODE_TRIGGERED,
    resolve_batch_execution_mode,
    resolve_dispatch_execution_mode,
)

_HANDLER_MODULES = (
    watchlist_dispatcher_handler,
    watchlist_worker_handler,
    watchlist_batch_reconciler_handler,
    watchlist_terminal_failure_handler,
)


class _ReachedHandlerBodyError(Exception):
    """拒否ガードを素通りして本体まで到達したことを表す番兵。"""


# =============================================================================
# F-B4 (1) 4 handlerが execution_mode / notification_mode を拒否する
# =============================================================================


@pytest.mark.parametrize("module", _HANDLER_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
@pytest.mark.parametrize(
    "event",
    [
        {"execution_mode": "VALIDATION"},
        {"execution_mode": "NORMAL"},
        {"notification_mode": "SUPPRESS"},
        {"execution_mode": "VALIDATION", "notification_mode": "SUPPRESS"},
    ],
    ids=["validation", "normal", "notification_mode_only", "both"],
)
def test_handlers_refuse_to_run_when_execution_mode_is_specified(
    module: Any, event: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """指定されたら**本体へ入る前に**例外で止まること。

    ★ `load_config` を番兵へ差し替えているため、もし拒否が効いていなければ
      `_ReachedHandlerBodyError` が上がる。「たまたま別の理由で落ちた」ことを
      成功と誤認しないための仕掛けである。
    """
    monkeypatch.setattr(module, "load_config", _raise_reached)

    with pytest.raises(WatchlistExecutionModeNotSupportedError):
        module.handler(event, object())


@pytest.mark.parametrize("module", _HANDLER_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_handlers_are_unchanged_when_no_mode_is_specified(
    module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空event(EventBridge Schedulerの自動実行)は素通りすること。

    Scheduler(ScheduleV2)はどのScheduleもInputを持たないため、自動実行の
    eventにこれらのキーは現れない。**自動実行の挙動が1つも変わらない**ことが
    本Issueの受入条件の1つである。
    """
    monkeypatch.setattr(module, "load_config", _raise_reached)

    with pytest.raises(_ReachedHandlerBodyError):
        module.handler({}, object())


@pytest.mark.parametrize("module", _HANDLER_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_handlers_are_unchanged_for_events_that_do_not_carry_a_mode(
    module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既存の正当なevent(SQS Records / job_type付きの連鎖起動)も素通りすること。"""
    monkeypatch.setattr(module, "load_config", _raise_reached)

    for event in (
        {"Records": []},
        {"job_type": "WATCHLIST_MAINTENANCE", "batch_id": "watchlist-maint-00000000"},
        {"execution_mode": None, "notification_mode": None},
    ):
        with pytest.raises(_ReachedHandlerBodyError):
            module.handler(event, object())


# =============================================================================
# F-B4 (2) 拒否関数そのものの契約
# =============================================================================


def test_reject_execution_mode_names_every_specified_key_in_the_message() -> None:
    """どのキーが原因かをメッセージから追えること(黙って落ちない)。"""
    with pytest.raises(WatchlistExecutionModeNotSupportedError) as excinfo:
        reject_execution_mode(
            {"execution_mode": "VALIDATION", "notification_mode": "SUPPRESS"},
            handler_name="watchlist dispatcher",
        )

    message = str(excinfo.value)
    assert "watchlist dispatcher" in message
    assert "execution_mode" in message
    assert "notification_mode" in message


def test_reject_execution_mode_logs_before_raising(caplog: pytest.LogCaptureFixture) -> None:
    """例外だけでなくERRORログも残ること(CloudWatchから理由を追えるようにする)。"""
    with caplog.at_level("ERROR"), pytest.raises(WatchlistExecutionModeNotSupportedError):
        reject_execution_mode({"execution_mode": "VALIDATION"}, handler_name="watchlist worker")

    assert any("watchlist worker" in record.getMessage() for record in caplog.records)


def test_reject_execution_mode_rejects_unknown_values_too() -> None:
    """既知の値かどうかで扱いを変えないこと。

    watchlist系はどの値も受け付けないため、typoだけを通す・止めるといった
    区別に意味が無い。「未知だから無視」という黙殺経路を作らない。
    """
    with pytest.raises(WatchlistExecutionModeNotSupportedError):
        reject_execution_mode({"execution_mode": "NOT_A_MODE"}, handler_name="x")


def test_reject_execution_mode_covers_the_same_keys_as_the_shared_resolver() -> None:
    """`_execution_mode.resolve_execution_context()` が読むキーを取りこぼさないこと。

    片方だけ増えると「新しいキーだけ黙殺される」状態が復活する。
    定数の突き合わせではなく、**共有resolverのソースから実際に読まれている
    `event.get(...)` を機械的に抽出**して比べる。
    """
    import inspect
    import re

    from jstock_advisor.lambda_handlers import _execution_mode

    source = inspect.getsource(_execution_mode.resolve_execution_context)
    resolver_keys = set(re.findall(r'event\.get\("([^"]+)"', source))

    assert resolver_keys, "共有resolverがeventから読むキーを抽出できなかった"
    assert resolver_keys <= set(REJECTED_KEYS), (
        f"共有resolverが読むキーのうち拒否対象に無いものがある: "
        f"{sorted(resolver_keys - set(REJECTED_KEYS))}"
    )


def test_rejection_error_is_a_value_error() -> None:
    """既存の不正mode指定と同じ型で受けられること(呼び出し側が分岐せずに済む)。"""
    assert issubclass(WatchlistExecutionModeNotSupportedError, ValueError)


# =============================================================================
# F-B8 (1) 起動経路の解決そのもの
# =============================================================================


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({}, EXECUTION_MODE_SCHEDULED),
        ({"unrelated": "x"}, EXECUTION_MODE_SCHEDULED),
        ({"job_type": "NEW_CANDIDATE_SCREENING"}, EXECUTION_MODE_MANUAL),
        ({"batch_id": "watchlist-00000000"}, EXECUTION_MODE_MANUAL),
        (
            {
                "job_type": "WATCHLIST_MAINTENANCE",
                "batch_id": "watchlist-maint-00000000",
                "triggered_by_batch_id": "watchlist-00000000",
                "trigger_type": "POST_NEW_CANDIDATE_SCREENING",
            },
            EXECUTION_MODE_TRIGGERED,
        ),
    ],
    ids=["empty_schedule", "unknown_keys_only", "manual_job_type", "manual_batch_id", "triggered"],
)
def test_resolve_dispatch_execution_mode(event: dict[str, Any], expected: str) -> None:
    """eventのキーの有無だけで起動経路を決めること。

    ★ 空eventが "scheduled" であることは、自動実行の監査記録が
      **修正前と同じ値のまま**であることの固定でもある。
    """
    assert resolve_dispatch_execution_mode(event) == expected


@pytest.mark.parametrize(
    ("batch_item", "expected"),
    [
        ({"job_type": "NEW_CANDIDATE_SCREENING"}, EXECUTION_MODE_SCHEDULED),
        ({}, EXECUTION_MODE_SCHEDULED),
        (
            {
                "job_type": "WATCHLIST_MAINTENANCE",
                "triggered_by_batch_id": "watchlist-00000000",
                "trigger_type": "POST_NEW_CANDIDATE_SCREENING",
            },
            EXECUTION_MODE_TRIGGERED,
        ),
    ],
    ids=["scheduled_batch", "missing_row", "triggered_batch"],
)
def test_resolve_batch_execution_mode(batch_item: dict[str, Any], expected: str) -> None:
    """finalize / reconcileはdispatchのeventを持たないため、batch行から復元する。"""
    assert resolve_batch_execution_mode(batch_item) == expected


def test_resolve_batch_execution_mode_cannot_see_a_manual_dispatch() -> None:
    """★ 既知の限界を**明示的に固定**する(Issue #286では解消しない)。

    dispatch時に解決した経路そのものはBatchRunsTableへ永続化していない
    (列の追加になるため)。よって手動でdispatchしたbatchのfinalize監査は
    "scheduled" と記録される。**この挙動を知らずに監査を読むと誤読する**ため、
    暗黙にせずテストとして残す。

    dispatcher自身の監査5か所は "manual" になるため、経路の判別は
    少なくとも1か所では残る。
    """
    manual_dispatch_event = {"job_type": "NEW_CANDIDATE_SCREENING"}
    assert resolve_dispatch_execution_mode(manual_dispatch_event) == EXECUTION_MODE_MANUAL

    # 同じbatchがBatchRunsTableへ残す行(trigger情報を持たない)
    persisted_row = {"job_type": "NEW_CANDIDATE_SCREENING", "batch_id": "watchlist-00000000"}
    assert resolve_batch_execution_mode(persisted_row) == EXECUTION_MODE_SCHEDULED


# =============================================================================
# F-B8 (2) dispatcherが実際に記録する値
# =============================================================================


def _fake_config() -> SimpleNamespace:
    """`full_market_screening_blocked` へ到達させる最小のconfig。

    この経路を選ぶのは、**dispatch lease取得・SQS投入・LINE通知より前**に
    batch auditを1件だけ記録して戻るためである(副作用を持ち込まずに
    execution_modeの実測値を観測できる)。
    """
    watchlist_screening = SimpleNamespace(
        enabled=True,
        scheduled_run_enabled=True,
        candidate_universe=SimpleNamespace(provider="csv"),
        screening_policy="high_dividend_financial_health",
        staged_rollout=SimpleNamespace(candidate_limit=None, market_segment_filter=None),
        batch_record_ttl_hours=72,
        rotation=SimpleNamespace(enabled=True),
        batch_processing_timeout_hours=24,
    )
    return SimpleNamespace(watchlist_screening=watchlist_screening)


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({}, EXECUTION_MODE_SCHEDULED),
        ({"job_type": "NEW_CANDIDATE_SCREENING"}, EXECUTION_MODE_MANUAL),
        (
            {
                "job_type": "NEW_CANDIDATE_SCREENING",
                "triggered_by_batch_id": "watchlist-00000000",
                "trigger_type": "POST_NEW_CANDIDATE_SCREENING",
            },
            EXECUTION_MODE_TRIGGERED,
        ),
    ],
    ids=["schedule", "manual", "triggered"],
)
def test_dispatcher_audit_records_the_actual_invocation_route(
    event: dict[str, Any], expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ F-B8の本体。修正前はこの3ケースがすべて "scheduled" だった。"""
    monkeypatch.delenv("ALLOW_FULL_MARKET_SCREENING", raising=False)
    monkeypatch.setattr(watchlist_dispatcher_handler, "load_config", _fake_config)
    audit_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        watchlist_dispatcher_handler, "record_batch_audit", lambda **kw: audit_calls.append(kw)
    )
    monkeypatch.setattr(
        watchlist_dispatcher_handler, "try_acquire_dispatch_lease", _fail_if_called
    )

    result = watchlist_dispatcher_handler.handler(event, object())

    assert result == {"error": "full_market_screening_blocked"}
    assert len(audit_calls) == 1
    assert audit_calls[0]["execution_mode"] == expected


def test_dispatcher_no_longer_hardcodes_scheduled() -> None:
    """ソース上にハードコードが1つも残っていないこと(8か所すべて)。

    ★ 呼び出し箇所は将来増えるため、**値ではなく形**で探す。
      新しい保存箇所が `execution_mode="scheduled"` で追加されたら落ちる。
    """
    import inspect
    from pathlib import Path

    from jstock_advisor.services import watchlist_batch_finalizer

    for module in (
        watchlist_dispatcher_handler,
        watchlist_batch_reconciler_handler,
        watchlist_batch_finalizer,
    ):
        source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
        assert 'execution_mode="scheduled"' not in source, (
            f"{module.__name__}: 起動経路のハードコードが残っている(#286 F-B8)"
        )


@pytest.mark.parametrize(
    ("batch_row", "expected"),
    [
        (
            {
                "batch_id": "watchlist-maint-00000000",
                "triggered_by_batch_id": "watchlist-00000000",
                "trigger_type": "POST_NEW_CANDIDATE_SCREENING",
            },
            EXECUTION_MODE_TRIGGERED,
        ),
        ({"batch_id": "watchlist-maint-00000000"}, EXECUTION_MODE_SCHEDULED),
    ],
    ids=["triggered_by_previous_batch", "no_trigger_info"],
)
def test_maintenance_finalize_audit_reflects_the_batch_row(
    batch_row: dict[str, Any], expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """finalize側の呼び出し箇所も起動経路を載せること(修正前は "scheduled" 固定)。

    ★ 2つ目のケースが本Issueで**解消していない限界**である。
      手動dispatchでもbatch行にtrigger情報が残らないため "scheduled" になる。
    """
    from jstock_advisor.services import watchlist_batch_finalizer

    audits: list[dict[str, Any]] = []
    monkeypatch.setattr(
        watchlist_batch_finalizer, "query_all_candidate_progress", lambda *a, **k: []
    )
    monkeypatch.setattr(watchlist_batch_finalizer, "WatchlistRepository", lambda *a, **k: object())
    monkeypatch.setattr(
        watchlist_batch_finalizer, "WatchlistRemovalHistoryRepository", lambda *a, **k: object()
    )
    monkeypatch.setattr(watchlist_batch_finalizer, "get_watchlist_batch", lambda _b: batch_row)
    monkeypatch.setattr(
        watchlist_batch_finalizer, "record_batch_audit", lambda **kw: audits.append(kw)
    )
    monkeypatch.setattr(
        watchlist_batch_finalizer, "mark_watchlist_batch_completed", lambda *a, **k: True
    )

    watchlist_batch_finalizer._finalize_maintenance_completed(
        "watchlist-maint-00000000", _NOW, _fake_maintenance_config()
    )

    assert [audit["execution_mode"] for audit in audits] == [expected]


# --- helpers -----------------------------------------------------------------


def _raise_reached(*args: Any, **kwargs: Any) -> Any:
    raise _ReachedHandlerBodyError


def _fail_if_called(*args: Any, **kwargs: Any) -> bool:
    pytest.fail("拒否ガードの後でdispatch leaseまで進んではいけない")

_NOW = dt.datetime(2026, 9, 8, 3, 0, tzinfo=dt.UTC)


def _fake_maintenance_config() -> SimpleNamespace:
    """`_finalize_maintenance_completed` が読む設定だけを持つ最小のconfig。"""
    return SimpleNamespace(
        watchlist_screening=SimpleNamespace(
            candidate_universe=SimpleNamespace(provider="csv"),
            auto_removal=SimpleNamespace(readd_cooldown_days=30),
            screening_policy="multi_style_monitoring",
        )
    )
