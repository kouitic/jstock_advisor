"""Issue #166: 連続営業日カウンタが営業日でしか進まないことの回帰テスト。

`near_buy_consecutive_business_days`(WatchState 側は `consecutive_business_days`)は
連続「評価回数」ではなく連続「営業日数」を表す。Issue #166 以前は
「前回一致日からの営業日数 <= 1」でインクリメントしていたため、営業日が
1 日も経過していない評価(値 0)でも加算されていた。

値 0 に含まれるのは次の 3 つで、いずれも営業日は進んでいない。

- 同一営業日の再評価(リトライ・再実行を含む)
- 週末の実行
- 平日に当たる祝日の実行(scheduler は MON-FRI で発火するが東証は休場)

本テストが固定する契約。

    経過営業日数 == 0   据え置き
    経過営業日数 == 1   +1
    経過営業日数 >= 2   1 へリセット

    非営業日の一致では last_matched_at を据え置き、last_evaluated_at のみ更新する。

カウンタは監視終了通知の送信ゲート(閾値 5)と通知本文の「N日連続」表示に
使われるため、過剰加算は「本来送られない通知」と「誤った本文」に直結する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.signals.near_buy import compute_consecutive_business_days

# 2026-09 の実カレンダー上の位置づけ(祝日は jpholiday 由来)。
_THU = dt.date(2026, 9, 3)  # 営業日
_FRI = dt.date(2026, 9, 4)  # 営業日
_SAT = dt.date(2026, 9, 5)  # 週末
_SUN = dt.date(2026, 9, 6)  # 週末
_MON = dt.date(2026, 9, 7)  # 営業日
_HOLIDAY_MON = dt.date(2026, 9, 21)  # 敬老の日
_HOLIDAY_TUE = dt.date(2026, 9, 22)  # 国民の休日
_HOLIDAY_WED = dt.date(2026, 9, 23)  # 秋分の日
_HOLIDAY_NEXT_BUSINESS = dt.date(2026, 9, 24)  # 3 連休明けの営業日

_CALENDAR = BusinessCalendar(extra_closure_mm_dd=frozenset(), additional_closure_dates=frozenset())


def _gap(last_matched: dt.date, today: dt.date) -> int:
    return _CALENDAR.business_days_between(last_matched, today)


# --- 前提: カレンダーが祝日・週末を非営業日として扱うこと ---------------------


@pytest.mark.parametrize("day", [_SAT, _SUN, _HOLIDAY_MON, _HOLIDAY_TUE, _HOLIDAY_WED])
def test_non_business_days_are_recognised(day: dt.date) -> None:
    assert _CALENDAR.is_business_day(day) is False


@pytest.mark.parametrize("day", [_THU, _FRI, _MON, _HOLIDAY_NEXT_BUSINESS])
def test_business_days_are_recognised(day: dt.date) -> None:
    assert _CALENDAR.is_business_day(day) is True


# --- Case 1 / 7: 通常の営業日はちょうど +1 -----------------------------------


def test_case1_consecutive_business_day_increments_once() -> None:
    gap = _gap(_THU, _FRI)

    assert gap == 1
    assert compute_consecutive_business_days(gap, 2) == 3


def test_case7_weekday_sequence_increments_exactly_once_per_business_day() -> None:
    """営業日が 1 日進むごとに、ちょうど 1 ずつ増える。"""
    counter = 1
    last_matched = _THU
    for today in (_FRI, _MON):
        counter = compute_consecutive_business_days(_gap(last_matched, today), counter)
        last_matched = today

    assert counter == 3


# --- Case 2 / 10: 同一営業日の再評価は冪等 -----------------------------------


def test_case2_same_business_date_repeat_does_not_increment() -> None:
    gap = _gap(_FRI, _FRI)

    assert gap == 0
    assert compute_consecutive_business_days(gap, 2) == 2


def test_case10_same_day_replay_is_idempotent_for_the_counter() -> None:
    """同じ評価日で何度処理しても、カウンタは増え続けない。"""
    counter = 2
    for _ in range(5):
        counter = compute_consecutive_business_days(_gap(_FRI, _FRI), counter)

    assert counter == 2


# --- Case 3: 週末 -------------------------------------------------------------


@pytest.mark.parametrize("today", [_SAT, _SUN])
def test_case3_weekend_evaluation_does_not_increment(today: dt.date) -> None:
    gap = _gap(_FRI, today)

    assert gap == 0
    assert compute_consecutive_business_days(gap, 2) == 2


def test_case3_repeated_weekend_runs_do_not_accumulate() -> None:
    """Production で実測された 2 -> 3 -> 4 -> 5 が起きないこと。"""
    counter = 2
    for _ in range(3):
        counter = compute_consecutive_business_days(_gap(_FRI, _SAT), counter)

    assert counter == 2


# --- Case 4 / 5: 平日に当たる祝日 ---------------------------------------------


def test_case4_weekday_holiday_does_not_increment() -> None:
    """scheduler は MON-FRI で発火するが、祝日は東証休場のため加算しない。"""
    gap = _gap(dt.date(2026, 9, 18), _HOLIDAY_MON)  # 前営業日は 9/18(金)

    assert gap == 0
    assert compute_consecutive_business_days(gap, 4) == 4


def test_case5_consecutive_holidays_do_not_increment() -> None:
    """2026-09-21/22/23 は 3 日連続の平日祝日。手動実行が無くても発火する。"""
    counter = 4
    last_matched = dt.date(2026, 9, 18)
    for today in (_HOLIDAY_MON, _HOLIDAY_TUE, _HOLIDAY_WED):
        assert _gap(last_matched, today) == 0
        counter = compute_consecutive_business_days(_gap(last_matched, today), counter)

    assert counter == 4

    # 連休明けの営業日で、ちょうど 1 だけ進む。
    counter = compute_consecutive_business_days(
        _gap(last_matched, _HOLIDAY_NEXT_BUSINESS), counter
    )
    assert counter == 5


# --- Case 6: 営業日が 2 日以上空いたらリセット --------------------------------


def test_case6_gap_of_two_or_more_business_days_resets_to_one() -> None:
    gap = _gap(dt.date(2026, 9, 1), _THU)

    assert gap >= 2
    assert compute_consecutive_business_days(gap, 9) == 1


# --- 週末を挟んでも営業日として連続していれば +1 -------------------------------


def test_weekend_in_between_still_counts_as_consecutive_business_days() -> None:
    """金 -> 月 は営業日として連続しているため +1 になる。"""
    gap = _gap(_FRI, _MON)

    assert gap == 1
    assert compute_consecutive_business_days(gap, 2) == 3


# --- Case 8: 通知ゲート(閾値 5)へ誤って到達しないこと --------------------------


def test_case8_weekend_and_replay_do_not_reach_notification_threshold() -> None:
    """実営業日が 2 日しか無いのに、週末実行と再実行で閾値 5 へ到達しない。

    Production で実際に起きた系列(金曜時点 2 -> 土曜に 3 回実行)を再現する。
    """
    threshold = 5
    counter = 2
    for _ in range(3):
        counter = compute_consecutive_business_days(_gap(_FRI, _SAT), counter)

    assert counter == 2
    assert counter < threshold, "非営業日の実行だけで通知ゲートへ到達してはならない"


def test_case8_threshold_is_reached_only_by_real_business_days() -> None:
    """真に 5 営業日連続した場合は、従来どおり閾値へ到達する。"""
    threshold = 5
    days = [
        dt.date(2026, 9, 14),
        dt.date(2026, 9, 15),
        dt.date(2026, 9, 16),
        dt.date(2026, 9, 17),
        dt.date(2026, 9, 18),
    ]
    counter = 1
    last_matched = days[0]
    for today in days[1:]:
        assert _CALENDAR.is_business_day(today)
        counter = compute_consecutive_business_days(_gap(last_matched, today), counter)
        last_matched = today

    assert counter == threshold


# --- WatchStateService 経由の項目更新契約 --------------------------------------


class _InMemoryWatchStateRepository:
    def __init__(self, state=None) -> None:
        self.saved = state

    def get_active(self, stock_code: str, watch_type):  # noqa: ANN001, ARG002
        return self.saved

    def upsert(self, state) -> None:  # noqa: ANN001
        self.saved = state


def _build_state(*, last_matched: dt.date, last_evaluated: dt.date, counter: int):
    from jstock_advisor.domain.entities.enums import WatchType
    from jstock_advisor.domain.entities.watch_state import WatchState

    return WatchState(
        watch_id="9999:NEAR_BUY",
        stock_code="9999",
        watch_type=WatchType.NEAR_BUY,
        started_at=dt.date(2026, 9, 1),
        last_matched_at=last_matched,
        last_evaluated_at=last_evaluated,
        consecutive_business_days=counter,
        last_current_price=Decimal("1000"),
        last_entry_price=Decimal("900"),
        best_distance_pct=Decimal("10"),
    )


def _near_buy_config():
    from jstock_advisor.config.models import NearBuyConfig

    return NearBuyConfig(
        start_required_decline_pct=5.0,
        continue_required_decline_pct=10.0,
        min_company_quality_score=50.0,
        daily_max_notifications=5,
        max_stale_business_days=5,
    )


def _evaluate(repo, today: dt.date):
    from jstock_advisor.domain.entities.enums import BuyAction
    from jstock_advisor.services.watch_state_service import WatchStateService

    service = WatchStateService(business_calendar=_CALENDAR, repository=repo)
    return service.evaluate_and_update(
        stock_code="9999",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        company_quality_score=80.0,
        required_decline_to_entry_pct=Decimal("3"),
        current_price=Decimal("1000"),
        entry_price=Decimal("970"),
        today=today,
        config=_near_buy_config(),
    )


def test_non_business_day_holds_last_matched_at_and_updates_last_evaluated_at() -> None:
    """承認された項目契約: 非営業日では last_matched_at を据え置き、評価日のみ更新。"""
    repo = _InMemoryWatchStateRepository(
        _build_state(last_matched=_FRI, last_evaluated=_FRI, counter=2)
    )

    _evaluate(repo, _SAT)

    assert repo.saved.consecutive_business_days == 2, "非営業日で加算してはならない"
    assert repo.saved.last_matched_at == _FRI, "営業日の錨を非営業日で上書きしない"
    assert repo.saved.last_evaluated_at == _SAT, "評価した事実は記録する"


def test_business_day_updates_both_dates_and_increments() -> None:
    repo = _InMemoryWatchStateRepository(
        _build_state(last_matched=_FRI, last_evaluated=_SAT, counter=2)
    )

    _evaluate(repo, _MON)

    assert repo.saved.consecutive_business_days == 3
    assert repo.saved.last_matched_at == _MON
    assert repo.saved.last_evaluated_at == _MON


def test_same_business_day_rerun_does_not_double_count_via_service() -> None:
    repo = _InMemoryWatchStateRepository(
        _build_state(last_matched=_MON, last_evaluated=_MON, counter=3)
    )

    _evaluate(repo, _MON)
    _evaluate(repo, _MON)

    assert repo.saved.consecutive_business_days == 3
    assert repo.saved.last_matched_at == _MON


def test_repeated_non_business_day_runs_do_not_accumulate_via_service() -> None:
    """Production で観測された系列(土曜に複数回 NORMAL 実行)を再現する。"""
    repo = _InMemoryWatchStateRepository(
        _build_state(last_matched=_FRI, last_evaluated=_FRI, counter=2)
    )

    for _ in range(3):
        _evaluate(repo, _SAT)

    assert repo.saved.consecutive_business_days == 2
    assert repo.saved.last_matched_at == _FRI
    assert repo.saved.last_evaluated_at == _SAT
