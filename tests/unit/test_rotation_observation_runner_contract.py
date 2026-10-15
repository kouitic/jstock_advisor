"""資金入替の観測 P の runner の契約テスト(Issue #604 P-b3。dormant)。

対象: `services/rotation_observation_runner`。設計: #604 P-b 設計 rev7(…6169348315)・
rev8(…6169691749。総予算)・USER 決定(#122 …6174293468 の 7。mode は OFF / SHADOW のみ)。

  1  ★ OFF ★: ports を 1 回も呼ばない・now を検査しない(明示の OFF で測る。既定に依存させない)。
     tripwire の対照 = 同じ ports でも SHADOW なら呼ばれる(OFF のテストが空振りで通らない)
  2  mode: ACTIVE は存在しない・未知の値は契約エラー(暗黙変換しない)
  3  流れ: 営業日でない / 途中の回 DEFERRED / 最後の回の gap / OBSERVED の WRITTEN /
     2 回目 ALREADY_PRESENT /
     最後の回の前後の境界 / naive の now
  4  ★ 先勝ち ★: 件数でなく内容。実際の `AuditService.record_if_absent`(moto)の
     条件付き書込で内容が保たれる
  5  隔離・失敗: port の None・書込の例外・想定外の例外 → 結果で返る(例外が漏れない)・failed
  6  ★ 総予算 ★: 残り ≦ 予約なら読取をせず読めない日 / 読取の port へ残り時間と予約を渡す / 定数
  7  構築時の検証: 最後の回が TTL の安全な上限の内側
  8  純粋性(AST)・漏れ(sentinel)・dormant
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import logging
import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.signals import rotation_observation_schedule as schedule
from jstock_advisor.domain.signals.rotation_observation import (
    MAX_HOLDINGS,
    BatchFacts,
    DayStatus,
    HoldingInput,
    observe_day,
)
from jstock_advisor.domain.signals.rotation_observation_record import (
    CONTRACT_VERSION,
    GAP_ID_PREFIX,
    OBSERVATION_ID_PREFIX,
    audit_id_for,
    to_output_values,
)
from jstock_advisor.domain.signals.rotation_source_eligibility import EligibilityWindow
from jstock_advisor.infrastructure.aws import rotation_observation_reader as reader
from jstock_advisor.infrastructure.collection_store import resolve_table_name
from jstock_advisor.services import rotation_observation_runner as module
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.rotation_observation_runner import (
    TIME_BUDGET_SECONDS,
    TOTAL_BUDGET_SECONDS,
    WRITE_RESERVE_SECONDS,
    RotationObserverMode,
    RotationObserverPorts,
    RotationRunnerContractError,
    RunKind,
    make_rotation_observer,
    observe_rotation,
)
from tests.unit.test_rotation_observation_contract import _importers_of

_REPO = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO / "src" / "jstock_advisor"
_MODULE_PATH = _PACKAGE / "services" / "rotation_observation_runner.py"

_JST = dt.timezone(dt.timedelta(hours=9))
_DAY = dt.date(2026, 10, 15)  # 木曜(営業日)
_START = dt.datetime(2026, 10, 15, 8, 0, 47, tzinfo=_JST)
_END = dt.datetime(2026, 10, 15, 8, 2, 41, tzinfo=_JST)
_LAST_RUN_TIME = dt.time(9, 0)
_LAST_RUN_AT = dt.datetime(2026, 10, 15, 9, 0, tzinfo=_JST)
_MIDDLE = dt.datetime(2026, 10, 15, 8, 30, tzinfo=_JST)
_SENTINEL = "SENTINEL-OWNER-HOLDING-777"
_REGION = "ap-northeast-1"


# --- fakes ---------------------------------------------------------------------------------------


class _Batch:
    def __init__(self, result: BatchFacts | None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    def get(self, batch_id: str) -> BatchFacts | None:
        self.calls.append(batch_id)
        if self.error is not None:
            raise self.error
        return self.result


class _Holdings:
    def __init__(
        self,
        result: Sequence[HoldingInput] | None,
        *,
        error: Exception | None = None,
        drain: Callable[[], None] | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.drain = drain
        self.calls: list[tuple[EligibilityWindow, float]] = []
        self.remaining_seen: list[float] = []

    def inputs_for(
        self,
        window: EligibilityWindow,
        *,
        remaining_seconds: Callable[[], float],
        reserve_seconds: float,
    ) -> Sequence[HoldingInput] | None:
        self.calls.append((window, reserve_seconds))
        self.remaining_seen.append(remaining_seconds())
        if self.error is not None:
            raise self.error
        if self.drain is not None:
            self.drain()
            if remaining_seconds() <= reserve_seconds:
                return None  # 予算が尽きたら部分結果にせず None(P-b2 の契約)
        return self.result


class _Sink:
    """最初の 1 件が勝つ(AuditService.record_if_absent と同じ意味)。"""

    def __init__(self, error: Exception | None = None, written: bool | None = None) -> None:
        self.stored: dict[str, Mapping[str, Any]] = {}
        self.calls: list[str] = []
        self.error = error
        self.forced = written

    def record_if_absent(self, audit_id: str, output_values: Mapping[str, Any]) -> bool:
        self.calls.append(audit_id)
        if self.error is not None:
            raise self.error
        if self.forced is not None:
            return self.forced
        if audit_id in self.stored:
            return False
        self.stored[audit_id] = dict(output_values)
        return True


class _Calendar:
    def __init__(self, business: bool = True, error: Exception | None = None) -> None:
        self.business = business
        self.error = error
        self.calls: list[dt.date] = []

    def is_business_day(self, day: dt.date) -> bool:
        self.calls.append(day)
        if self.error is not None:
            raise self.error
        return self.business


def _remaining(value: float = TOTAL_BUDGET_SECONDS) -> Callable[[], float]:
    return lambda: value


def _complete() -> BatchFacts:
    return BatchFacts(_START, _END)


def _incomplete() -> BatchFacts:
    return BatchFacts(_START, None)


def _inputs(count: int = 2) -> tuple[HoldingInput, ...]:
    return tuple(HoldingInput(None, {}) for _ in range(count))


def _ports(
    *,
    batch: _Batch | None = None,
    holdings: _Holdings | None = None,
    sink: _Sink | None = None,
    calendar: _Calendar | None = None,
    remaining: Callable[[], float] | None = None,
) -> tuple[RotationObserverPorts, _Batch, _Holdings, _Sink, _Calendar]:
    batch = batch or _Batch(_complete())
    holdings = holdings or _Holdings(_inputs())
    sink = sink or _Sink()
    calendar = calendar or _Calendar()
    ports = RotationObserverPorts(batch, holdings, sink, calendar, remaining or _remaining())
    return ports, batch, holdings, sink, calendar


def _observer(ports: RotationObserverPorts) -> module.RotationObserver:
    return make_rotation_observer(ports, last_run_time=_LAST_RUN_TIME)


SHADOW = RotationObserverMode.SHADOW
OFF = RotationObserverMode.OFF


# --- 1 OFF ---------------------------------------------------------------------------------------


def test_off_calls_no_port_and_does_not_even_look_at_now() -> None:
    ports, batch, holdings, sink, calendar = _ports()
    observer = _observer(ports)
    result = observe_rotation(OFF, object(), observer)  # type: ignore[arg-type]  # now を検査しない
    assert result.kind is RunKind.NOT_RUN_OFF and not result.failed
    assert (batch.calls, holdings.calls, sink.calls, calendar.calls) == ([], [], [], [])


def test_off_is_measured_with_an_explicit_off_and_the_same_ports_run_under_shadow() -> None:
    """対照: 同じ ports は SHADOW なら呼ばれる(OFF のテストが空振りで通っていない証拠)。"""
    ports, batch, _, sink, calendar = _ports()
    observer = _observer(ports)
    assert observe_rotation(OFF, _MIDDLE, observer).kind is RunKind.NOT_RUN_OFF
    assert (batch.calls, sink.calls, calendar.calls) == ([], [], [])
    result = observe_rotation(SHADOW, _LAST_RUN_AT, observer)
    assert result.kind is RunKind.WRITTEN
    assert calendar.calls and batch.calls and sink.calls


def test_the_mode_has_only_off_and_shadow_and_no_active() -> None:
    assert [m.value for m in RotationObserverMode] == ["OFF", "SHADOW"]  # ACTIVE は存在しない
    assert not hasattr(RotationObserverMode, "ACTIVE")


# --- 2 mode ------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, "OFF", "SHADOW", "ACTIVE", 0, 1, True, object()])
def test_a_mode_that_is_not_a_member_is_a_contract_error_never_converted(bad: Any) -> None:
    ports, batch, _, sink, _ = _ports()
    with pytest.raises(RotationRunnerContractError):
        observe_rotation(bad, _MIDDLE, _observer(ports))
    assert batch.calls == [] and sink.calls == []


# --- 3 流れ --------------------------------------------------------------------------------------


def test_a_naive_now_is_a_contract_error() -> None:
    ports, batch, _, _, _ = _ports()
    with pytest.raises(RotationRunnerContractError):
        observe_rotation(SHADOW, dt.datetime(2026, 10, 15, 9, 0), _observer(ports))
    assert batch.calls == []


def test_not_a_business_day_writes_nothing_and_reads_nothing() -> None:
    ports, batch, holdings, sink, calendar = _ports(calendar=_Calendar(business=False))
    result = observe_rotation(SHADOW, _LAST_RUN_AT, _observer(ports))
    assert result.kind is RunKind.NOT_RUN_NOT_BUSINESS_DAY
    assert calendar.calls == [_DAY]
    assert (batch.calls, holdings.calls, sink.calls) == ([], [], [])  # 最後の回でも gap を作らない


def test_the_target_day_is_the_jst_calendar_day_of_now() -> None:
    ports, batch, _, _, calendar = _ports()
    utc_now = dt.datetime(2026, 10, 15, 0, 30, tzinfo=dt.UTC)  # = 09:30 JST の同じ日
    observe_rotation(SHADOW, utc_now, _observer(ports))
    assert calendar.calls == [_DAY]
    assert batch.calls == [schedule.holdings_batch_id(_DAY)]
    late = dt.datetime(2026, 10, 14, 15, 30, tzinfo=dt.UTC)  # = 10/15 00:30 JST(日付が進む)
    observe_rotation(SHADOW, late, _observer(_ports(calendar=calendar)[0]))
    assert calendar.calls[-1] == _DAY


def test_observed_is_written_with_the_deterministic_id_and_the_p_b1_values() -> None:
    ports, _, holdings, sink, _ = _ports()
    result = observe_rotation(SHADOW, _MIDDLE, _observer(ports))
    assert result.kind is RunKind.WRITTEN and result.day_status is DayStatus.OBSERVED
    expected = observe_day(target_date=_DAY, batch=_complete(), holdings=_inputs())
    assert list(sink.stored) == [f"{OBSERVATION_ID_PREFIX}:{_DAY.isoformat()}"]
    assert sink.stored[audit_id_for(expected)] == to_output_values(expected)
    assert holdings.calls[0][0] == EligibilityWindow(_START, _END)  # 完了した窓を渡す


def test_the_second_run_is_already_present_and_keeps_the_first_content() -> None:
    first_inputs = _inputs(2)
    second_inputs = _inputs(5)
    sink = _Sink()
    for inputs, expected in (
        (first_inputs, RunKind.WRITTEN),
        (second_inputs, RunKind.ALREADY_PRESENT),
    ):
        ports, *_ = _ports(holdings=_Holdings(inputs), sink=sink)
        assert observe_rotation(SHADOW, _MIDDLE, _observer(ports)).kind is expected
    (stored,) = sink.stored.values()
    assert stored["holdings_total"] == 2  # ★ 件数でなく内容: 先に書いた回の内容が残る ★


def test_an_incomplete_batch_before_the_last_run_is_deferred_and_writes_nothing() -> None:
    ports, _, holdings, sink, _ = _ports(batch=_Batch(_incomplete()))
    result = observe_rotation(SHADOW, _MIDDLE, _observer(ports))
    assert result.kind is RunKind.DEFERRED
    assert result.day_status is DayStatus.BATCH_NOT_COMPLETED_IN_WINDOW
    assert sink.calls == [] and holdings.calls == []  # 完了していなければ保有を読まない


def test_an_incomplete_batch_in_the_last_run_writes_the_gap() -> None:
    ports, _, _, sink, _ = _ports(batch=_Batch(_incomplete()))
    result = observe_rotation(SHADOW, _LAST_RUN_AT, _observer(ports))
    assert result.kind is RunKind.GAP_WRITTEN
    assert list(sink.stored) == [f"{GAP_ID_PREFIX}:{_DAY.isoformat()}"]


def test_a_second_gap_write_is_gap_already_present() -> None:
    sink = _Sink()
    kinds = []
    for _ in range(2):
        ports, *_ = _ports(batch=_Batch(None), sink=sink)
        kinds.append(observe_rotation(SHADOW, _LAST_RUN_AT, _observer(ports)).kind)
    assert kinds == [RunKind.GAP_WRITTEN, RunKind.GAP_ALREADY_PRESENT]


@pytest.mark.parametrize(
    ("offset_seconds", "expected"),
    [(-1, RunKind.DEFERRED), (0, RunKind.GAP_WRITTEN), (1, RunKind.GAP_WRITTEN)],
)
def test_the_last_run_boundary_is_now_greater_than_or_equal(
    offset_seconds: int, expected: RunKind
) -> None:
    ports, *_ = _ports(batch=_Batch(_incomplete()))
    moment = _LAST_RUN_AT + dt.timedelta(seconds=offset_seconds)
    assert observe_rotation(SHADOW, moment, _observer(ports)).kind is expected


def test_observed_in_the_last_run_is_an_observation_not_a_gap() -> None:
    ports, *_ = _ports()
    result = observe_rotation(SHADOW, _LAST_RUN_AT, _observer(ports))
    assert result.kind is RunKind.WRITTEN


def test_a_holdings_failure_is_a_read_failure_deferred_then_a_gap() -> None:
    for moment, expected in ((_MIDDLE, RunKind.DEFERRED), (_LAST_RUN_AT, RunKind.GAP_WRITTEN)):
        ports, *_ = _ports(holdings=_Holdings(None))
        result = observe_rotation(SHADOW, moment, _observer(ports))
        assert (result.kind, result.day_status) == (expected, DayStatus.READ_FAILED)


def test_over_the_holding_limit_is_not_a_partial_result() -> None:
    over = tuple(HoldingInput(None, {}) for _ in range(MAX_HOLDINGS + 1))
    ports, *_ = _ports(holdings=_Holdings(over))
    result = observe_rotation(SHADOW, _LAST_RUN_AT, _observer(ports))
    assert (result.kind, result.day_status) == (RunKind.GAP_WRITTEN, DayStatus.INPUT_LIMIT_EXCEEDED)


# --- 5 隔離・失敗 ---------------------------------------------------------------------------------


def test_a_sink_exception_is_write_failed_and_failed_without_leaking() -> None:
    ports, *_ = _ports(sink=_Sink(error=RuntimeError(_SENTINEL)))
    result = observe_rotation(SHADOW, _MIDDLE, _observer(ports))
    assert result.kind is RunKind.WRITE_FAILED and result.failed
    assert result.error_type == "RuntimeError"
    assert _SENTINEL not in repr(result)


@pytest.mark.parametrize(
    "ports_kwargs",
    [
        {"batch": _Batch(None, error=ValueError(_SENTINEL))},
        {"holdings": _Holdings(None, error=KeyError(_SENTINEL))},
        {"calendar": _Calendar(error=OSError(_SENTINEL))},
    ],
    ids=["batch", "holdings", "calendar"],
)
def test_a_port_that_raises_is_an_unexpected_error_that_fails_the_run(
    ports_kwargs: dict[str, Any],
) -> None:
    """port は例外にしない契約。違反は UNEXPECTED_ERROR(型名だけ)。runner から例外は漏れない。"""
    ports, *_ = _ports(**ports_kwargs)
    result = observe_rotation(SHADOW, _LAST_RUN_AT, _observer(ports))
    assert result.kind is RunKind.UNEXPECTED_ERROR and result.failed
    assert result.error_type in {"ValueError", "KeyError", "OSError"}
    assert _SENTINEL not in repr(result)


def test_only_write_failed_and_unexpected_error_are_failed() -> None:
    failed = {kind for kind in RunKind if module.RotationRunResult(kind).failed}
    assert failed == {RunKind.WRITE_FAILED, RunKind.UNEXPECTED_ERROR}


def test_a_failure_leaves_no_trace_of_the_message_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ports, *_ = _ports(sink=_Sink(error=RuntimeError(_SENTINEL)))
    with caplog.at_level(logging.INFO, logger=module.logger.name):
        observe_rotation(SHADOW, _MIDDLE, _observer(ports))
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert module.MARKER_FAILED in text and "RuntimeError" in text
    assert _SENTINEL not in text


def test_logs_carry_only_markers_and_codes(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=module.logger.name):
        ports, *_ = _ports()
        observe_rotation(SHADOW, _MIDDLE, _observer(ports))
        ports, *_ = _ports(batch=_Batch(None))
        observe_rotation(SHADOW, _LAST_RUN_AT, _observer(ports))
    messages = [record.getMessage() for record in caplog.records]
    assert any(m.startswith(module.MARKER_RUN) for m in messages)
    assert any(m.startswith(module.MARKER_GAP) for m in messages)
    levels = {
        record.levelno for record in caplog.records if module.MARKER_GAP in record.getMessage()
    }
    assert levels == {logging.WARNING}  # gap は WARNING


def test_the_result_has_no_value_or_identifier_fields() -> None:
    assert [f for f in module.RotationRunResult.__dataclass_fields__] == [
        "kind",
        "day_status",
        "error_type",
    ]


def test_sentinels_in_a_batch_id_resolver_never_reach_the_sink_values() -> None:
    sink = _Sink()
    ports, *_ = _ports(sink=sink)
    observer = make_rotation_observer(
        ports, last_run_time=_LAST_RUN_TIME, batch_id_for=lambda day: f"holdings-{_SENTINEL}-{day}"
    )
    observe_rotation(SHADOW, _MIDDLE, observer)
    assert _SENTINEL not in repr(sink.stored) and _SENTINEL not in repr(sink.calls)


# --- 6 総予算 ------------------------------------------------------------------------------------


def test_the_budget_constants_are_named_and_the_total_is_their_sum() -> None:
    assert TOTAL_BUDGET_SECONDS == TIME_BUDGET_SECONDS + WRITE_RESERVE_SECONDS
    assert TIME_BUDGET_SECONDS > WRITE_RESERVE_SECONDS > 0


def test_the_total_budget_fits_inside_ninety_percent_of_the_assumed_lambda_timeout() -> None:
    """P-b4 の契約(template の Timeout から読む)の先取り。

    想定の 60 秒に対し 総予算 ≦ 0.9 × Timeout。
    """
    assumed_lambda_timeout_seconds = 60
    assert assumed_lambda_timeout_seconds * 0.9 >= TOTAL_BUDGET_SECONDS


def test_the_worst_case_call_count_justifies_having_a_total_budget() -> None:
    """設計 rev8 SHOULD-3 の算術(reader の定数から計算)。

    1 回の待ちを 1 秒としても予算を超える = 予算が要る。
    """
    chunks = math.ceil(MAX_HOLDINGS * 3 / reader.BATCH_GET_CHUNK)  # 保有あたり推奨 id は最大 3
    worst_calls = (
        MAX_HOLDINGS * reader.MAX_QUERY_PAGES + chunks * (1 + reader.MAX_BATCH_RETRIES) + 1 + 1
    )
    assert worst_calls * 1 > TOTAL_BUDGET_SECONDS  # 待ち 1 秒 × 最悪の回数 ≫ 総予算
    assert worst_calls >= 600  # 設計の見積り(≈ 638)の桁


def test_an_exhausted_budget_at_the_start_reads_nothing_and_is_a_read_failure() -> None:
    for moment, expected in ((_MIDDLE, RunKind.DEFERRED), (_LAST_RUN_AT, RunKind.GAP_WRITTEN)):
        ports, batch, holdings, _, _ = _ports(remaining=_remaining(WRITE_RESERVE_SECONDS))
        result = observe_rotation(SHADOW, moment, _observer(ports))
        assert (result.kind, result.day_status) == (expected, DayStatus.READ_FAILED)
        assert batch.calls == [] and holdings.calls == []  # 境界: 残り = 予約 → 読まない


def test_one_second_more_than_the_reserve_still_reads() -> None:
    ports, batch, holdings, _, _ = _ports(remaining=_remaining(WRITE_RESERVE_SECONDS + 1))
    assert observe_rotation(SHADOW, _MIDDLE, _observer(ports)).kind is RunKind.WRITTEN
    assert batch.calls and holdings.calls


def test_the_holdings_port_receives_the_remaining_clock_and_the_write_reserve() -> None:
    ports, _, holdings, _, _ = _ports()
    observe_rotation(SHADOW, _MIDDLE, _observer(ports))
    assert holdings.calls[0][1] == WRITE_RESERVE_SECONDS
    assert holdings.remaining_seen == [TOTAL_BUDGET_SECONDS]


def test_a_budget_exhausted_inside_the_read_is_a_read_failure_and_last_run_writes_a_gap() -> None:
    clock = {"left": float(TOTAL_BUDGET_SECONDS)}

    def drain() -> None:
        clock["left"] = float(WRITE_RESERVE_SECONDS)

    holdings = _Holdings(_inputs(), drain=drain)
    ports, _, _, sink, _ = _ports(holdings=holdings, remaining=lambda: clock["left"])
    result = observe_rotation(SHADOW, _LAST_RUN_AT, _observer(ports))
    assert (result.kind, result.day_status) == (RunKind.GAP_WRITTEN, DayStatus.READ_FAILED)
    assert list(sink.stored) == [f"{GAP_ID_PREFIX}:{_DAY.isoformat()}"]  # 書込の予約が残っていた


# --- 7 構築時の検証 -------------------------------------------------------------------------------


def _build(last: dt.time, scheduled: dt.time = schedule.DEFAULT_SCHEDULED_TIME) -> Any:
    ports, *_ = _ports()
    return make_rotation_observer(ports, last_run_time=last, scheduled_time=scheduled)


def test_the_last_run_must_be_inside_the_ttl_safe_limit() -> None:
    assert _build(dt.time(13, 0))  # 08:00 + 6 時間 − 1 時間 = 13:00(上限。含む)
    assert _build(dt.time(9, 0))
    for beyond in (dt.time(13, 0, 1), dt.time(14, 0), dt.time(18, 0)):
        with pytest.raises(RotationRunnerContractError):
            _build(beyond)


def test_the_last_run_must_not_be_before_the_scheduled_time() -> None:
    assert _build(dt.time(8, 0))
    with pytest.raises(RotationRunnerContractError):
        _build(dt.time(7, 59, 59))


def test_the_safe_limit_follows_the_injected_scheduled_time() -> None:
    assert _build(dt.time(14, 0), scheduled=dt.time(9, 0))
    with pytest.raises(RotationRunnerContractError):
        _build(dt.time(14, 0, 1), scheduled=dt.time(9, 0))


def test_the_safe_limit_is_the_p_b2_constant_not_a_second_definition() -> None:
    limit = schedule.latest_safe_run_at(_DAY, schedule.DEFAULT_SCHEDULED_TIME)
    assert limit.time().replace(tzinfo=None) == dt.time(13, 0)
    assert _build(limit.time().replace(tzinfo=None))


@pytest.mark.parametrize(
    "bad", [None, "09:00", dt.datetime(2026, 10, 15, 9, 0), dt.time(9, 0, tzinfo=_JST)]
)
def test_the_times_must_be_naive_times_of_day(bad: Any) -> None:
    ports, *_ = _ports()
    with pytest.raises(RotationRunnerContractError):
        make_rotation_observer(ports, last_run_time=bad)
    with pytest.raises(RotationRunnerContractError):
        make_rotation_observer(ports, last_run_time=_LAST_RUN_TIME, scheduled_time=bad)


def test_ports_must_be_the_ports_bundle() -> None:
    with pytest.raises(RotationRunnerContractError):
        make_rotation_observer(object(), last_run_time=_LAST_RUN_TIME)  # type: ignore[arg-type]
    ports, *_ = _ports()
    with pytest.raises(RotationRunnerContractError):
        observe_rotation(SHADOW, _MIDDLE, object())  # type: ignore[arg-type]
    assert ports


def test_the_default_batch_id_follows_the_injected_scheduled_time() -> None:
    """予定時刻を変えたら、導出する batch_id も変わる(08:00 固定の写しを持たない)。"""
    ports, batch, *_ = _ports()
    observer = make_rotation_observer(
        ports, last_run_time=dt.time(10, 0), scheduled_time=dt.time(9, 0)
    )
    observe_rotation(SHADOW, dt.datetime(2026, 10, 15, 9, 30, tzinfo=_JST), observer)
    assert batch.calls == [schedule.holdings_batch_id(_DAY, dt.time(9, 0))]
    assert batch.calls == ["holdings-watchlist-20261015T090000"]


def test_the_default_batch_id_is_the_scheduler_derived_one() -> None:
    ports, batch, *_ = _ports()
    observe_rotation(SHADOW, _MIDDLE, _observer(ports))
    assert batch.calls == ["holdings-watchlist-20261015T080000"]


# --- 4 先勝ち(実際の AuditService.record_if_absent。moto の条件付き書込)----------------------


class _AuditSink:
    """テスト用 adapter(P-b4 の adapter と同じ形): AuditService.record_if_absent へ繋ぐ。"""

    def __init__(self, service: AuditService) -> None:
        self._service = service

    def record_if_absent(self, audit_id: str, output_values: Mapping[str, Any]) -> bool:
        entry = self._service.record_if_absent(
            audit_id=audit_id,
            decision_type="capital_rotation_observation",
            stock_code=None,
            input_values={},
            calculation_formulas={},
            output_values=dict(output_values),
            data_sources=[],
            rule_version=CONTRACT_VERSION,
            timestamp=_MIDDLE,
        )
        return entry is not None


@pytest.fixture
def audit_table(
    monkeypatch: pytest.MonkeyPatch,
    lambda_runtime_env: None,
    create_collection_table: Callable[..., None],
) -> Iterator[Any]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        create_collection_table("audit_log.json", "audit_id")
        yield boto3.resource("dynamodb", region_name=_REGION).Table(
            resolve_table_name("audit_log.json")
        )


def test_first_write_wins_with_the_real_conditional_write(audit_table: Any) -> None:
    """★ 実際の AuditService.record_if_absent(moto)で、CAS が最初の内容を保つ ★。

    fake の形を信じない(R1 で実測した罠)。
    """
    sink = _AuditSink(AuditService())
    kinds = []
    for count in (2, 5):
        ports, *_ = _ports(holdings=_Holdings(_inputs(count)), sink=sink)  # type: ignore[arg-type]
        kinds.append(observe_rotation(SHADOW, _MIDDLE, _observer(ports)).kind)
    assert kinds == [RunKind.WRITTEN, RunKind.ALREADY_PRESENT]
    items = list(audit_table.scan()["Items"])
    assert len(items) == 1
    assert items[0]["audit_id"] == f"{OBSERVATION_ID_PREFIX}:{_DAY.isoformat()}"
    content = json.loads(items[0]["data"])["output_values"]
    assert content["holdings_total"] == 2  # ★ 先に書いた回の内容が残る(2 回目の 5 件ではない)★


def test_the_gap_and_the_observation_can_both_exist_for_one_day_with_different_ids(
    audit_table: Any,
) -> None:
    sink = _AuditSink(AuditService())
    ports, *_ = _ports(holdings=_Holdings(_inputs(2)), sink=sink)  # type: ignore[arg-type]
    assert observe_rotation(SHADOW, _MIDDLE, _observer(ports)).kind is RunKind.WRITTEN
    ports, *_ = _ports(batch=_Batch(None), sink=sink)  # type: ignore[arg-type]
    assert observe_rotation(SHADOW, _LAST_RUN_AT, _observer(ports)).kind is RunKind.GAP_WRITTEN
    ids = {item["audit_id"] for item in audit_table.scan()["Items"]}
    assert ids == {
        f"{OBSERVATION_ID_PREFIX}:{_DAY.isoformat()}",
        f"{GAP_ID_PREFIX}:{_DAY.isoformat()}",
    }  # 読み手の規則 = OBSERVED 優先(P-b1 の docstring)


# --- 8 構造(AST)・dormant ----------------------------------------------------------------------


def _tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def _imported_modules() -> set[str]:
    found: set[str] = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_it_imports_only_the_standard_library_and_domain_modules() -> None:
    allowed_std = {
        "__future__",
        "datetime",
        "logging",
        "collections.abc",
        "dataclasses",
        "enum",
        "typing",
    }
    for name in _imported_modules():
        if name.startswith("jstock_advisor."):
            assert name.startswith("jstock_advisor.domain."), name
        else:
            assert name in allowed_std, name


def test_it_has_exactly_one_except_exception_and_no_bare_except() -> None:
    handlers = [n for n in ast.walk(_tree()) if isinstance(n, ast.ExceptHandler)]
    assert len(handlers) == 1
    (handler,) = handlers
    assert isinstance(handler.type, ast.Name) and handler.type.id == "Exception"


def test_it_does_not_read_a_clock_environment_or_use_scan_or_put() -> None:
    banned_attrs = {
        "now",
        "today",
        "utcnow",
        "getenv",
        "environ",
        "scan",
        "put_item",
        "update_item",
    }
    banned_names = {"print", "open", "input", "exec", "eval"}
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Attribute):
            assert node.attr not in banned_attrs, node.attr
        if isinstance(node, ast.Name):
            assert node.id not in banned_names, node.id


def test_it_has_no_new_numbers_beyond_the_named_budget_constants() -> None:
    values = [
        n.value
        for n in ast.walk(_tree())
        if isinstance(n, ast.Constant)
        and isinstance(n.value, int | float)
        and not isinstance(n.value, bool)
    ]
    assert sorted(values) == sorted([TIME_BUDGET_SECONDS, WRITE_RESERVE_SECONDS])


def test_the_mode_enum_has_no_active_and_no_other_value_is_referenced() -> None:
    names = {n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)}
    assert "ACTIVE" not in names


def test_nothing_in_src_imports_the_runner_so_it_is_dormant() -> None:
    assert _importers_of("rotation_observation_runner", _MODULE_PATH) == []
