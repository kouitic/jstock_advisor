"""Issue #440: JPX休場日に、市場依存の3 entryをskipする共通gateのテスト。

対象(USER決定 #440 issuecomment-5741656160):
  BuyCandidates parent / HoldingsWatchlist parent / WatchlistDispatcher NEW_CANDIDATE_SCREENING

## 時間意味論(development_workflow 3.5): TIME_SEMANTICS_IMPACT = YES(T3 + T4)

- C-BS: 実際に分岐する状態(営業日 / 土日 / 祝日 / 国民の休日 / 臨時休業 / JST境界 / bypass)を、
  **固定clock**で検証する(実時刻に依存しない。CIがどの時刻・曜日に走っても同じ結果)。
  固定clockは、handlerでは`handler_module.dt`を差し替える固定clock、helperでは`now`引数で与える。
- C-CS/C-CO: 本モジュールは`real_market_calendar` markerで、tests/unit/conftest.pyの既定の
  「常に営業日」fixtureを実カレンダーへ戻す。既存の親経路テスト(実時刻を使う)は既定fixtureにより
  休場日でも落ちない。影響範囲の分析はPR本文に記録する。

## 検証する順序契約

  validation → 非対象分岐(recovery/child) → bypass検証 → JST日付 → 営業日判定 → skip
  INVALID EVENT + MARKET HOLIDAY = INVALID EVENT(holidayで入力エラーを隠さない)
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import ExecutionMode
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.lambda_handlers import _market_holiday as gate
from jstock_advisor.lambda_handlers import buy_candidates_handler as buy_module
from jstock_advisor.lambda_handlers import holdings_watchlist_handler as holdings_module
from jstock_advisor.lambda_handlers import watchlist_dispatcher_handler as dispatcher_module

pytestmark = pytest.mark.real_market_calendar

_UTC = dt.UTC
# --- 固定clock(すべてUTC。コメントにJSTを併記する) ---
_JST_9_18_FRI_0800 = dt.datetime(2026, 9, 17, 23, 0, tzinfo=_UTC)  # 営業日(金)
_JST_9_19_SAT_0800 = dt.datetime(2026, 9, 18, 23, 0, tzinfo=_UTC)  # 土曜
_JST_9_20_SUN_0800 = dt.datetime(2026, 9, 19, 23, 0, tzinfo=_UTC)  # 日曜
_JST_9_21_MON_0800 = dt.datetime(2026, 9, 20, 23, 0, tzinfo=_UTC)  # 敬老の日
_JST_9_22_TUE_0800 = dt.datetime(2026, 9, 21, 23, 0, tzinfo=_UTC)  # 国民の休日
_JST_9_23_WED_0800 = dt.datetime(2026, 9, 22, 23, 0, tzinfo=_UTC)  # 秋分の日
_JST_9_24_THU_0800 = dt.datetime(2026, 9, 23, 23, 0, tzinfo=_UTC)  # 連休明けの営業日

_NORMAL = ExecutionContext.normal()
_VALIDATION = ExecutionContext(mode=ExecutionMode.VALIDATION)


@pytest.fixture(scope="module")
def config():
    return load_config()


# ============================================================================
# helper(純粋な判定): T1〜T5
# ============================================================================


@pytest.mark.parametrize(
    ("date", "expected_closed"),
    [
        (dt.date(2026, 9, 18), False),  # T1 金曜(営業日)
        (dt.date(2026, 9, 19), True),  # T2 土曜
        (dt.date(2026, 9, 20), True),  # T2 日曜
        (dt.date(2026, 9, 21), True),  # T3 敬老の日
        (dt.date(2026, 9, 22), True),  # T3/T4 国民の休日(祝日と祝日に挟まれた日)
        (dt.date(2026, 9, 23), True),  # T3 秋分の日
        (dt.date(2026, 9, 24), False),  # T10 連休明け
        (dt.date(2026, 9, 25), False),
    ],
)
def test_t1_to_t4_market_closed_follows_the_business_calendar(
    config: Any, date: dt.date, expected_closed: bool
) -> None:
    assert gate.is_market_closed(date, config) is expected_closed


def test_t4_the_decision_is_exactly_the_business_calendar_not_a_new_rule(config: Any) -> None:
    """helperは新しい営業日判定を作らず、BusinessCalendarの結果そのものに従う。"""
    calendar = BusinessCalendar.from_config(config.holiday_calendar)
    for offset in range(-40, 400):
        day = dt.date(2026, 8, 1) + dt.timedelta(days=offset)
        assert gate.is_market_closed(day, config) is (not calendar.is_business_day(day)), day


def test_t4_additional_closure_in_the_config_is_respected(config: Any) -> None:
    """臨時休業(additional_closures)もBusinessCalendarに従って休場になる。"""
    hc = config.holiday_calendar
    extended = hc.model_copy(
        update={
            "additional_closures": hc.additional_closures.model_copy(
                update={"dates": [*hc.additional_closures.dates, "2026-09-30"]}
            )
        }
    )
    cfg = config.model_copy(update={"holiday_calendar": extended})

    assert gate.is_market_closed(dt.date(2026, 9, 30), cfg) is True
    assert gate.is_market_closed(dt.date(2026, 9, 30), config) is False


@pytest.mark.parametrize(
    ("utc_now", "expected_jst_date", "expected_skip"),
    [
        # UTC 9/20 21:30 = JST 9/21 06:30(祝日)。JST日付は9/21
        (dt.datetime(2026, 9, 20, 21, 30, tzinfo=_UTC), dt.date(2026, 9, 21), True),
        # UTC 9/17 23:30 = JST 9/18 08:30(金・営業日)。UTC日付(9/17)ではなくJST日付(9/18)で判定
        (dt.datetime(2026, 9, 17, 23, 30, tzinfo=_UTC), dt.date(2026, 9, 18), False),
        # UTC 9/23 14:59 = JST 9/23 23:59(秋分の日)→ 休場
        (dt.datetime(2026, 9, 23, 14, 59, tzinfo=_UTC), dt.date(2026, 9, 23), True),
        # UTC 9/23 15:00 = JST 9/24 00:00(営業日)→ 実行(JST日跨ぎの境界)
        (dt.datetime(2026, 9, 23, 15, 0, tzinfo=_UTC), dt.date(2026, 9, 24), False),
        # 識別ペア: UTCの日付とJSTの日付で結果が逆になる(UTC/JST取り違えを必ず検出する)
        # UTC 9/18 21:00 = JST 9/19(土) 06:00 → 休場(UTC日付は金曜9/18で営業日)
        (dt.datetime(2026, 9, 18, 21, 0, tzinfo=_UTC), dt.date(2026, 9, 19), True),
        # UTC 9/23 21:00 = JST 9/24(木) 06:00 → 実行(UTC日付は水曜9/23で休場)
        (dt.datetime(2026, 9, 23, 21, 0, tzinfo=_UTC), dt.date(2026, 9, 24), False),
        # UTC 9/18 15:00 = JST 9/19 00:00(土曜)→ 休場(金曜のUTC日付のままなら実行してしまう)
        (dt.datetime(2026, 9, 18, 15, 0, tzinfo=_UTC), dt.date(2026, 9, 19), True),
    ],
)
def test_t5_the_decision_uses_the_jst_date_not_the_utc_date(
    config: Any, utc_now: dt.datetime, expected_jst_date: dt.date, expected_skip: bool
) -> None:
    decision = gate.decide_market_closed(utc_now, config, allow_market_closed=False)

    assert decision.business_date_jst == expected_jst_date
    assert decision.skip is expected_skip


def test_naive_now_is_rejected_not_treated_as_utc(config: Any) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        gate.decide_market_closed(dt.datetime(2026, 9, 21, 8, 0), config, allow_market_closed=False)


# ============================================================================
# bypass(allow_market_closed)の契約(D1): default=false / VALIDATION+trueのみ / 不正値はエラー
# ============================================================================


@pytest.mark.parametrize(
    "event", [{}, {"allow_market_closed": None}, {"allow_market_closed": False}]
)
@pytest.mark.parametrize("ctx", [_NORMAL, _VALIDATION])
def test_bypass_defaults_to_false(event: dict[str, Any], ctx: ExecutionContext) -> None:
    assert gate.resolve_allow_market_closed(event, ctx) is False


def test_bypass_true_is_allowed_only_for_validation() -> None:
    assert gate.resolve_allow_market_closed({"allow_market_closed": True}, _VALIDATION) is True


def test_bypass_true_with_normal_is_an_error() -> None:
    with pytest.raises(gate.MarketClosedBypassError, match="requires execution_mode=VALIDATION"):
        gate.resolve_allow_market_closed({"allow_market_closed": True}, _NORMAL)


@pytest.mark.parametrize("bad", ["true", "false", "yes", 1, 0, [], {}, "True"])
@pytest.mark.parametrize("ctx", [_NORMAL, _VALIDATION])
def test_bypass_non_boolean_value_is_an_error_never_coerced(
    bad: Any, ctx: ExecutionContext
) -> None:
    with pytest.raises(gate.MarketClosedBypassError, match="must be a boolean"):
        gate.resolve_allow_market_closed({"allow_market_closed": bad}, ctx)


def test_bypass_on_a_holiday_runs_only_for_validation_and_is_recorded(
    config: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=gate.__name__)

    skipped = gate.should_skip_for_market_closed(
        {"allow_market_closed": True}, _VALIDATION, _JST_9_21_MON_0800, config, handler="h"
    )

    assert skipped is False
    messages = [r.getMessage() for r in caplog.records if r.name == gate.__name__]
    assert len(messages) == 1
    assert "event=MARKET_CLOSED_BYPASS" in messages[0]
    assert "execution_mode=VALIDATION" in messages[0]


def test_validation_without_the_flag_is_skipped_on_a_holiday_no_implicit_bypass(
    config: Any,
) -> None:
    assert (
        gate.should_skip_for_market_closed({}, _VALIDATION, _JST_9_21_MON_0800, config, handler="h")
        is True
    )


def test_bypass_flag_on_a_business_day_runs_normally_and_is_still_recorded(
    config: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """営業日にVALIDATION+trueを指定しても実行は変わらない(skipされない)が、使用の事実は記録する。"""
    caplog.set_level(logging.INFO, logger=gate.__name__)

    skipped = gate.should_skip_for_market_closed(
        {"allow_market_closed": True}, _VALIDATION, _JST_9_18_FRI_0800, config, handler="h"
    )

    assert skipped is False
    (record,) = [r for r in caplog.records if r.name == gate.__name__]
    assert "event=MARKET_CLOSED_BYPASS" in record.getMessage()
    assert "business_date_jst=2026-09-18" in record.getMessage()


# ============================================================================
# T7(helper): 休場日 + 不正event → holiday skipで隠さずエラー
# ============================================================================


@pytest.mark.parametrize("now", [_JST_9_21_MON_0800, _JST_9_19_SAT_0800, _JST_9_18_FRI_0800])
@pytest.mark.parametrize("event", [{"allow_market_closed": "yes"}, {"allow_market_closed": 1}])
def test_t7_invalid_flag_is_an_error_even_on_a_market_holiday(
    config: Any, now: dt.datetime, event: dict[str, Any]
) -> None:
    with pytest.raises(gate.MarketClosedBypassError):
        gate.should_skip_for_market_closed(event, _NORMAL, now, config, handler="h")


def test_t7_normal_with_true_is_an_error_even_on_a_market_holiday(config: Any) -> None:
    with pytest.raises(gate.MarketClosedBypassError):
        gate.should_skip_for_market_closed(
            {"allow_market_closed": True}, _NORMAL, _JST_9_21_MON_0800, config, handler="h"
        )


# ============================================================================
# skipのログ(D7): 構造化INFOを1 invocationにつき1件・PIIなし
# ============================================================================


def test_skip_logs_exactly_one_structured_info_without_pii(
    config: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=gate.__name__)

    assert gate.should_skip_for_market_closed(
        {},
        _NORMAL,
        _JST_9_21_MON_0800,
        config,
        handler="watchlist_dispatcher",
        job_type="NEW_CANDIDATE_SCREENING",
    )

    records = [r for r in caplog.records if r.name == gate.__name__]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert records[0].getMessage() == (
        "event=MARKET_CLOSED_SKIP handler=watchlist_dispatcher business_date_jst=2026-09-21 "
        "execution_mode=NORMAL job_type=NEW_CANDIDATE_SCREENING"
    )


def test_skip_log_omits_job_type_when_not_needed(
    config: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=gate.__name__)

    gate.should_skip_for_market_closed(
        {}, _NORMAL, _JST_9_22_TUE_0800, config, handler="buy_candidates"
    )

    (record,) = [r for r in caplog.records if r.name == gate.__name__]
    assert "job_type" not in record.getMessage()
    assert record.getMessage().endswith("execution_mode=NORMAL")


def test_no_log_on_a_business_day(config: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=gate.__name__)

    assert not gate.should_skip_for_market_closed(
        {}, _NORMAL, _JST_9_18_FRI_0800, config, handler="buy_candidates"
    )

    assert [r for r in caplog.records if r.name == gate.__name__] == []


# ============================================================================
# handler配線: 固定clockで3 entryを通す
#   skip前に実行してはならないもの(最初の状態変更・市場依存の通知)が、skip時に呼ばれない
# ============================================================================


class _FrozenDatetime(dt.datetime):
    """`dt.datetime.now()`だけを固定する(その他のdatetimeの振る舞いは通常どおり)。"""

    frozen: dt.datetime = _JST_9_18_FRI_0800

    @classmethod
    def now(cls, tz: Any = None) -> dt.datetime:  # type: ignore[override]
        return cls.frozen if tz is None else cls.frozen.astimezone(tz)


class _FrozenDt:
    """handler moduleの`dt`の代わり。`dt.datetime.now`だけ固定し他は委譲する。"""

    datetime = _FrozenDatetime

    def __getattr__(self, name: str) -> Any:
        return getattr(dt, name)


class _ReachedPostGateError(Exception):
    """gateを通過して通常処理の最初の状態変更の直前に到達したことを示す番兵。"""


def _freeze(monkeypatch: pytest.MonkeyPatch, module: Any, now: dt.datetime) -> None:
    monkeypatch.setattr(_FrozenDatetime, "frozen", now)
    monkeypatch.setattr(module, "dt", _FrozenDt())


def _forbid(monkeypatch: pytest.MonkeyPatch, module: Any, names: list[str]) -> list[str]:
    """skip時に呼ばれてはならない関数を、呼ばれたら記録するものへ差し替える。"""
    called: list[str] = []
    for name in names:
        monkeypatch.setattr(
            module,
            name,
            lambda *a, _n=name, **kw: (
                called.append(_n)
                or (_ for _ in ()).throw(
                    AssertionError(f"{_n} must not run before the market-closed skip")
                )
            ),
        )
    return called


def _sentinel(monkeypatch: pytest.MonkeyPatch, module: Any, name: str) -> None:
    def _raise(*a: Any, **kw: Any) -> None:
        raise _ReachedPostGateError

    monkeypatch.setattr(module, name, _raise)


@pytest.fixture(autouse=True)
def _line_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "token-value")
    monkeypatch.setenv("LINE_USER_ID", "user-value")
    monkeypatch.setenv("ALLOW_FULL_MARKET_SCREENING", "true")


def _prepare_buy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(buy_module, "build_real_provider_bundle", lambda now, config: object())
    monkeypatch.setattr(buy_module, "build_line_client_for_run", lambda **kw: object())
    monkeypatch.setattr(buy_module, "LineNotificationService", lambda **kw: object())


def _prepare_holdings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(holdings_module, "build_real_provider_bundle", lambda now, config: object())
    monkeypatch.setattr(holdings_module, "build_line_client_for_run", lambda **kw: object())
    monkeypatch.setattr(holdings_module, "LineNotificationService", lambda **kw: object())


_BUY_FORBIDDEN = ["check_registry_health", "TradeCooldownService", "start_batch", "dispatch_async"]
_HOLDINGS_FORBIDDEN = [
    "check_registry_health",
    "TradeCooldownService",
    "start_batch",
    "dispatch_async",
    "evaluate_household_concentration_and_notify",
]
_DISPATCHER_FORBIDDEN = [
    "try_acquire_dispatch_lease",
    "try_acquire_rotation_dispatch_lease",
    "_build_notification_service",
]


@pytest.mark.parametrize(
    "now",
    [
        _JST_9_19_SAT_0800,
        _JST_9_20_SUN_0800,
        _JST_9_21_MON_0800,
        _JST_9_22_TUE_0800,
        _JST_9_23_WED_0800,
    ],
    ids=["9/19土", "9/20日", "9/21敬老の日", "9/22国民の休日", "9/23秋分の日"],
)
class TestMarketClosedSkip:
    """T2/T3/T6: 休場日 + 正常event → 正常no-op(状態変更0・通知0・返却契約)。"""

    def test_buy_candidates_parent_skips_without_any_state_change(
        self, monkeypatch: pytest.MonkeyPatch, now: dt.datetime
    ) -> None:
        _prepare_buy(monkeypatch)
        _freeze(monkeypatch, buy_module, now)
        forbidden = _forbid(monkeypatch, buy_module, _BUY_FORBIDDEN)

        result = buy_module.handler({}, None)

        assert result == {"dispatched": 0, "skipped": "MARKET_CLOSED"}
        assert forbidden == []

    def test_holdings_watchlist_parent_skips_without_any_state_change_or_notification(
        self, monkeypatch: pytest.MonkeyPatch, now: dt.datetime
    ) -> None:
        _prepare_holdings(monkeypatch)
        _freeze(monkeypatch, holdings_module, now)
        forbidden = _forbid(monkeypatch, holdings_module, _HOLDINGS_FORBIDDEN)

        result = holdings_module.handler({}, None)

        assert result == {"dispatched_holdings": 0, "skipped": "MARKET_CLOSED"}
        assert forbidden == []

    def test_watchlist_dispatcher_skips_before_lease_notification_service_and_sqs(
        self, monkeypatch: pytest.MonkeyPatch, now: dt.datetime
    ) -> None:
        _freeze(monkeypatch, dispatcher_module, now)
        forbidden = _forbid(monkeypatch, dispatcher_module, _DISPATCHER_FORBIDDEN)

        result = dispatcher_module.handler({}, None)

        assert result == {"skipped": "MARKET_CLOSED"}
        assert forbidden == []


class TestBusinessDayRunsAsBefore:
    """T1: 通常営業日 → gateを通過し、従来の処理(最初の状態変更)へ進む。"""

    def test_buy_candidates_parent_reaches_the_first_post_gate_step(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _prepare_buy(monkeypatch)
        _freeze(monkeypatch, buy_module, _JST_9_18_FRI_0800)
        _sentinel(monkeypatch, buy_module, "check_registry_health")

        with pytest.raises(_ReachedPostGateError):
            buy_module.handler({}, None)

    def test_holdings_watchlist_parent_reaches_the_first_post_gate_step(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _prepare_holdings(monkeypatch)
        _freeze(monkeypatch, holdings_module, _JST_9_18_FRI_0800)
        _sentinel(monkeypatch, holdings_module, "check_registry_health")

        with pytest.raises(_ReachedPostGateError):
            holdings_module.handler({}, None)

    def test_watchlist_dispatcher_reaches_the_notification_service_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _freeze(monkeypatch, dispatcher_module, _JST_9_18_FRI_0800)
        _sentinel(monkeypatch, dispatcher_module, "_build_notification_service")

        with pytest.raises(_ReachedPostGateError):
            dispatcher_module.handler({}, None)


class TestNextBusinessDayResumesWithoutPollution:
    """T10: 連休(9/19〜23)の後の営業日(9/24)は、前日までのskipの状態汚染なく通常処理へ復帰する。"""

    def test_after_consecutive_skips_the_next_business_day_reaches_normal_processing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _prepare_buy(monkeypatch)
        forbidden = _forbid(monkeypatch, buy_module, _BUY_FORBIDDEN)
        for holiday in (_JST_9_21_MON_0800, _JST_9_22_TUE_0800, _JST_9_23_WED_0800):
            _freeze(monkeypatch, buy_module, holiday)
            assert buy_module.handler({}, None)["skipped"] == "MARKET_CLOSED"
        assert forbidden == []  # 連休中に何も書き込んでいない

        _sentinel(monkeypatch, buy_module, "check_registry_health")
        _freeze(monkeypatch, buy_module, _JST_9_24_THU_0800)
        with pytest.raises(_ReachedPostGateError):
            buy_module.handler({}, None)


class TestInvalidEventIsNeverHiddenByAHoliday:
    """T7: 休場日 + 不正event → 従来どおり失敗する(休場日で隠さない)。"""

    def test_buy_invalid_execution_mode_fails_on_a_holiday(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _prepare_buy(monkeypatch)
        _freeze(monkeypatch, buy_module, _JST_9_21_MON_0800)

        with pytest.raises(ValueError, match="unknown execution_mode"):
            buy_module.handler({"execution_mode": "BOGUS"}, None)

    def test_holdings_invalid_execution_mode_fails_on_a_holiday(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _prepare_holdings(monkeypatch)
        _freeze(monkeypatch, holdings_module, _JST_9_21_MON_0800)

        with pytest.raises(ValueError, match="unknown execution_mode"):
            holdings_module.handler({"execution_mode": "BOGUS"}, None)

    def test_buy_invalid_bypass_value_fails_on_a_holiday(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _prepare_buy(monkeypatch)
        _freeze(monkeypatch, buy_module, _JST_9_21_MON_0800)
        forbidden = _forbid(monkeypatch, buy_module, _BUY_FORBIDDEN)

        with pytest.raises(gate.MarketClosedBypassError):
            buy_module.handler({"allow_market_closed": "yes"}, None)
        assert forbidden == []

    def test_holdings_normal_with_bypass_true_fails_on_a_holiday(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _prepare_holdings(monkeypatch)
        _freeze(monkeypatch, holdings_module, _JST_9_21_MON_0800)

        with pytest.raises(gate.MarketClosedBypassError):
            holdings_module.handler({"allow_market_closed": True}, None)

    def test_dispatcher_execution_mode_is_still_rejected_on_a_holiday(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jstock_advisor.lambda_handlers._watchlist_execution_mode import (
            WatchlistExecutionModeNotSupportedError,
        )

        _freeze(monkeypatch, dispatcher_module, _JST_9_21_MON_0800)

        with pytest.raises(WatchlistExecutionModeNotSupportedError):
            dispatcher_module.handler({"execution_mode": "VALIDATION"}, None)

    def test_dispatcher_bypass_true_fails_because_the_dispatcher_has_no_validation_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _freeze(monkeypatch, dispatcher_module, _JST_9_21_MON_0800)
        forbidden = _forbid(monkeypatch, dispatcher_module, _DISPATCHER_FORBIDDEN)

        with pytest.raises(gate.MarketClosedBypassError):
            dispatcher_module.handler({"allow_market_closed": True}, None)
        assert forbidden == []

    def test_dispatcher_invalid_bypass_value_fails_even_for_maintenance_on_a_holiday(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _freeze(monkeypatch, dispatcher_module, _JST_9_21_MON_0800)

        with pytest.raises(gate.MarketClosedBypassError):
            dispatcher_module.handler(
                {"job_type": "WATCHLIST_MAINTENANCE", "allow_market_closed": "yes"}, None
            )

    def test_dispatcher_unknown_job_type_still_returns_its_error_on_a_holiday(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _freeze(monkeypatch, dispatcher_module, _JST_9_21_MON_0800)

        result = dispatcher_module.handler({"job_type": "BOGUS"}, None)

        assert result == {"error": "unknown_job_type", "job_type": "BOGUS"}


class TestNonTargetBranchesAreNeverGated:
    """T8: 休場日でも、recovery / child は実行される(前営業日のbatchの回復・継続を止めない)。"""

    def test_buy_recovery_event_is_processed_on_a_holiday(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _prepare_buy(monkeypatch)
        _freeze(monkeypatch, buy_module, _JST_9_21_MON_0800)
        monkeypatch.setattr(buy_module, "resolve_finalize_only_request", lambda *a, **kw: None)

        result = buy_module.handler({"recovery_action": "FINALIZE_ONLY", "batch_id": "b"}, None)

        assert result == {"finalize_recovery": "REJECTED"}  # skipではなくrecovery分岐に入った

    def test_holdings_recovery_event_is_processed_on_a_holiday(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _prepare_holdings(monkeypatch)
        _freeze(monkeypatch, holdings_module, _JST_9_21_MON_0800)
        monkeypatch.setattr(holdings_module, "resolve_finalize_only_request", lambda *a, **kw: None)

        result = holdings_module.handler(
            {"recovery_action": "FINALIZE_ONLY", "batch_id": "b"}, None
        )

        assert result == {"finalize_recovery": "REJECTED"}

    def test_buy_child_task_is_processed_on_a_holiday(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jstock_advisor.domain.entities.enums import CandidateSource

        _prepare_buy(monkeypatch)
        _freeze(monkeypatch, buy_module, _JST_9_21_MON_0800)
        processed: list[str] = []
        monkeypatch.setattr(
            buy_module,
            "_process_single_candidate",
            lambda stock_code, *a, **kw: processed.append(stock_code) or {"stock_code": stock_code},
        )

        result = buy_module.handler(
            {
                "task": "buy_candidate",
                "stock_code": "1111",
                "source": next(iter(CandidateSource)).value,
                "batch_id": "batch-x",
            },
            None,
        )

        assert processed == ["1111"]
        assert result["stock_code"] == "1111"

    def test_holdings_child_task_is_processed_on_a_holiday(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _prepare_holdings(monkeypatch)
        _freeze(monkeypatch, holdings_module, _JST_9_21_MON_0800)
        processed: list[str] = []
        monkeypatch.setattr(
            holdings_module,
            "_process_single_holding",
            lambda holding_id, *a, **kw: processed.append(holding_id) or {"found": True},
        )

        result = holdings_module.handler(
            {"task": "holding", "holding_id": "holding-x", "batch_id": "batch-x"}, None
        )

        assert processed == ["holding-x"]
        assert result["found"] is True


class TestRecoveryAndChildIgnoreTheBypassKey:
    """Q-1: recovery/childはgateを通らず、bypassキーは(不正値でも)無視する(エラーにしない)。"""

    @pytest.mark.parametrize("flag", [True, "yes", 1])
    def test_buy_recovery_ignores_the_flag(
        self, monkeypatch: pytest.MonkeyPatch, flag: Any
    ) -> None:
        _prepare_buy(monkeypatch)
        _freeze(monkeypatch, buy_module, _JST_9_21_MON_0800)
        monkeypatch.setattr(buy_module, "resolve_finalize_only_request", lambda *a, **kw: None)

        result = buy_module.handler(
            {"recovery_action": "FINALIZE_ONLY", "batch_id": "b", "allow_market_closed": flag}, None
        )

        assert result == {"finalize_recovery": "REJECTED"}

    @pytest.mark.parametrize("flag", [True, "yes", 1])
    def test_holdings_child_ignores_the_flag(
        self, monkeypatch: pytest.MonkeyPatch, flag: Any
    ) -> None:
        _prepare_holdings(monkeypatch)
        _freeze(monkeypatch, holdings_module, _JST_9_21_MON_0800)
        monkeypatch.setattr(
            holdings_module, "_process_single_holding", lambda *a, **kw: {"found": True}
        )

        result = holdings_module.handler(
            {"task": "holding", "holding_id": "h", "batch_id": "b", "allow_market_closed": flag},
            None,
        )

        assert result == {"found": True}


def test_provider_bundle_construction_performs_no_network_io(
    monkeypatch: pytest.MonkeyPatch, config: Any
) -> None:
    """gateの前にprovider構築が走る。構築だけでは外部通信をしない(skip時に無駄な通信が出ない)。"""
    import socket

    from jstock_advisor.services.provider_factory import build_real_provider_bundle

    def _no_network(*a: Any, **kw: Any) -> None:
        raise AssertionError("network access during provider construction")

    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(socket.socket, "connect", _no_network)

    build_real_provider_bundle(_JST_9_21_MON_0800, config)


class TestValidationBypassOnAHoliday:
    """D1: VALIDATION + allow_market_closed=true のときだけ、休場日でも通常処理へ進む。"""

    def test_buy_validation_with_bypass_runs_the_normal_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _prepare_buy(monkeypatch)
        _freeze(monkeypatch, buy_module, _JST_9_21_MON_0800)
        _sentinel(monkeypatch, buy_module, "check_registry_health")

        with pytest.raises(_ReachedPostGateError):
            buy_module.handler({"execution_mode": "VALIDATION", "allow_market_closed": True}, None)

    def test_holdings_validation_with_bypass_runs_the_normal_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _prepare_holdings(monkeypatch)
        _freeze(monkeypatch, holdings_module, _JST_9_21_MON_0800)
        _sentinel(monkeypatch, holdings_module, "check_registry_health")

        with pytest.raises(_ReachedPostGateError):
            holdings_module.handler(
                {"execution_mode": "VALIDATION", "allow_market_closed": True}, None
            )

    def test_buy_validation_without_bypass_is_skipped_on_a_holiday(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _prepare_buy(monkeypatch)
        _freeze(monkeypatch, buy_module, _JST_9_21_MON_0800)
        forbidden = _forbid(monkeypatch, buy_module, _BUY_FORBIDDEN)

        result = buy_module.handler({"execution_mode": "VALIDATION"}, None)

        assert result == {"dispatched": 0, "skipped": "MARKET_CLOSED"}
        assert forbidden == []


class TestNonTargetEntriesAreNotTouched:
    """T9: 市場営業日に依存しないentry(disclosure・evaluation・週次等)は、gateを持たない。"""

    @pytest.mark.parametrize(
        "module_name",
        [
            "disclosure_check_handler",
            "evaluation_handler",
            "weekly_review_handler",
            "monthly_review_handler",
            "quarterly_review_handler",
            "line_webhook_handler",
            "watchlist_worker_handler",
            "watchlist_terminal_failure_handler",
            "watchlist_batch_reconciler_handler",
        ],
    )
    def test_handler_does_not_reference_the_market_closed_gate(self, module_name: str) -> None:
        import importlib

        module = importlib.import_module(f"jstock_advisor.lambda_handlers.{module_name}")

        assert not hasattr(module, "should_skip_for_market_closed")
        assert not hasattr(module, "SKIP_REASON")

    def test_dispatcher_does_not_gate_watchlist_maintenance_in_this_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D2=DEFER: WATCHLIST_MAINTENANCEは今回のscope外。休場日でもgateされず従来の処理へ進む。"""
        _freeze(monkeypatch, dispatcher_module, _JST_9_21_MON_0800)
        monkeypatch.setattr(
            dispatcher_module,
            "try_acquire_dispatch_lease",
            lambda *a, **kw: (_ for _ in ()).throw(_ReachedPostGateError()),
        )

        with pytest.raises(_ReachedPostGateError):
            dispatcher_module.handler({"job_type": "WATCHLIST_MAINTENANCE"}, None)


# ============================================================================
# D5: 複数日未実行の後の次営業日に、売買イベント(差分)を正常検知できる
# ============================================================================


def test_d5_trade_detection_after_a_multi_day_gap_still_detects_the_difference(
    tmp_path: Any,
) -> None:
    """9/18(金)のスナップショットの後、9/19〜23は実行されず(休場日のskip + 週末)、
    その間に記録された売買(保有の増加)を9/24(木)の実行で検知できる。
    検知は「前回スナップショットと現在の保有の差分」であり、日数の連続性に依存しない。"""
    from decimal import Decimal

    from jstock_advisor.config.models import TradeCooldownConfig
    from jstock_advisor.domain.entities.enums import AccountType, TransactionType
    from jstock_advisor.domain.entities.holding import Holding
    from jstock_advisor.domain.entities.holdings_snapshot import HoldingsSnapshotEntry
    from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
    from jstock_advisor.infrastructure.local_repository.holdings_snapshot_repository import (
        HoldingsSnapshotRepository,
    )
    from jstock_advisor.infrastructure.local_repository.trade_event_record_repository import (
        TradeEventRecordRepository,
    )
    from jstock_advisor.services.trade_cooldown_service import TradeCooldownService

    calendar = BusinessCalendar.from_config(load_config().holiday_calendar)
    repo = HoldingsSnapshotRepository(store_dir=tmp_path, file_name="holdings_snapshots.json")
    holding_id = build_holding_id(DEFAULT_OWNER, "2914")
    repo.upsert(
        HoldingsSnapshotEntry(
            owner=DEFAULT_OWNER,
            holding_id=holding_id,
            stock_code="2914",
            shares=0,
            average_purchase_price=None,
            recorded_at=dt.date(2026, 9, 18),  # 最後に実行された営業日(金)
            active_holding=False,
        )
    )
    service = TradeCooldownService(
        business_calendar=calendar,
        config=TradeCooldownConfig(
            enabled=True, buy_business_days=5, sell_business_days=5, partial_trade_business_days=3
        ),
        repository=repo,
        execution_context=_NORMAL,
        trade_event_repository=TradeEventRecordRepository(store_dir=tmp_path),
    )
    bought_during_the_holidays = {
        holding_id: Holding(
            owner=DEFAULT_OWNER,
            holding_id=holding_id,
            stock_code="2914",
            stock_name="銘柄2914",
            shares=100,
            average_purchase_price=Decimal("1000"),
            total_purchase_amount=Decimal("100000"),
            first_purchase_date=dt.date(2026, 9, 21),
            last_purchase_date=dt.date(2026, 9, 21),
            account_type=AccountType.SPECIFIC,
            created_at=_JST_9_21_MON_0800,
            updated_at=_JST_9_21_MON_0800,
        )
    }

    outcome = service.detect_and_apply(bought_during_the_holidays, _JST_9_24_THU_0800)

    assert outcome.confirmed is True
    assert [(e.holding_id, e.event_type) for e in outcome.events] == [
        (holding_id, TransactionType.BUY)
    ]
    entry = next(e for e in repo.list_all() if e.holding_id == holding_id)
    assert entry.shares == 100
    assert entry.recorded_at == dt.date(2026, 9, 24)  # 検知した日(連休明けの営業日)
    # クールダウンは検知日(営業日)から営業日数で数える(祝日を営業日に数えない)
    assert entry.cooldown_until_date == calendar.add_business_days(dt.date(2026, 9, 24), 5)
