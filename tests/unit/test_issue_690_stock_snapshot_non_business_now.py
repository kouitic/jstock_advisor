"""Issue #690: `build_stock_snapshot()` を非営業日の `now` で呼んだときの end-to-end 検証。

## 何を固定するか

`services/stock_snapshot_service.py::build_stock_snapshot()` の時刻まわりは次の3点である。

```
evaluation_date           = evaluation_date_jst(now)       UTC-aware now → JST 暦日
price_as_of_date          = snap.as_of_date                provider の値の素通し(now から計算しない)
business_days_to_earnings = business_days_between(evaluation_date, next_earnings_date)
earnings_date_status      = 決算日なし=UNAVAILABLE / evaluation_date より前=STALE_PAST_DATE /
                            それ以外=CONFIRMED
```

`BusinessCalendar` 単体(`test_business_calendar.py`)は非営業日を検証済みだが、
`build_stock_snapshot()` を土日・祝日・連休・年末年始の `now` で通した配線は直接検証されていなかった
(coverage gap。実装の誤りは確認されていない: Issue #690 の Phase A / 設計コメント)。

## 検証方針(docs/development_workflow.md 3.5。TIME_SEMANTICS_IMPACT = YES〔T4 のみ〕)

- C-BS: 非営業日(土・日・祝日・5連休・GW・年末年始)と UTC/JST 暦日の境界を、固定 clock で網羅する。
- C-MD: **期待値はすべて literal**。mock provider の `as_of_date` は
  `latest_plausible_bar_date()` で決まるが、その helper から期待値を導出して比較すると、
  mock が helper ちょうどで打ち切る実装なら恒真になる。
  ここでは「金曜が直近営業日」のように、局面ごとに日付を直接書く。
- 本モジュールは provider の契約(実 provider が非営業日に何を返すか)を検証しない
  (T2 / #52 の領域)。検証するのは「provider が返した値と JST 暦日が snapshot へ
  欠落・変形なく載る」配線である。
- wall clock(`datetime.now` 等)は使わない。すべて固定時刻。

## 日付の事実(2026年。jpholiday + config の休場日)

```
2026-10-03(土)・10-04(日)                 週末
2026-10-12(月)                             スポーツの日
2026-09-19(土)〜09-23(水)                  5連休(9/21 敬老の日・9/22 国民の休日・9/23 秋分の日)
2026-05-02(土)〜05-06(水)                  GW(5/3 日・5/4 みどりの日・5/5 こどもの日・5/6 振替休日)
2026-12-31(木)・2027-01-01(金)〜01-03(日)  JPX 休場
                                           (config recurring_market_closures: 12-31 / 01-01〜01-03)
```
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import EarningsDateStatus
from jstock_advisor.interfaces.types import Disclosure
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

_CFG = load_config()
_STOCK_CODE = "2914"
_JST = dt.timezone(dt.timedelta(hours=9))


class _FixedEarningsDateDisclosureProvider:
    """次回決算予定日だけを固定するフェイク。それ以外は mock provider へ委譲する。"""

    def __init__(self, delegate: object, next_earnings_date: dt.date | None) -> None:
        self._delegate = delegate
        self._next_earnings_date = next_earnings_date

    def get_disclosures(self, stock_code: str, since: dt.date) -> list[Disclosure]:
        return self._delegate.get_disclosures(stock_code, since)  # type: ignore[attr-defined, no-any-return]

    def get_next_earnings_date(self, stock_code: str) -> dt.date | None:
        return self._next_earnings_date


def _jst_0800(year: int, month: int, day: int) -> dt.datetime:
    """JST 08:00(寄り付き前)の固定時刻を aware datetime として返す。"""
    return dt.datetime(year, month, day, 8, 0, tzinfo=_JST)


def _build(now: dt.datetime, next_earnings_date: dt.date | None):
    base = build_mock_provider_bundle(now)
    providers = dataclasses.replace(
        base, disclosure=_FixedEarningsDateDisclosureProvider(base.disclosure, next_earnings_date)
    )
    return build_stock_snapshot(providers, _STOCK_CODE, now, _CFG)


# (局面, now[JST 08:00], 決算日, 期待する price_as_of_date, 期待する business_days_to_earnings)
# 期待値は literal。as_of は「now 時点で存在しうる直近の営業日」、
# 営業日数は evaluation_date の翌日から決算日まで。
_NON_BUSINESS_CASES = [
    pytest.param(
        "土曜", _jst_0800(2026, 10, 3), dt.date(2026, 10, 5), dt.date(2026, 10, 2), 1, id="saturday"
    ),
    pytest.param(
        "日曜", _jst_0800(2026, 10, 4), dt.date(2026, 10, 5), dt.date(2026, 10, 2), 1, id="sunday"
    ),
    pytest.param(
        "祝日(スポーツの日)",
        _jst_0800(2026, 10, 12),
        dt.date(2026, 10, 13),
        dt.date(2026, 10, 9),
        1,
        id="national-holiday-monday",
    ),
    pytest.param(
        "5連休の中日(国民の休日)",
        _jst_0800(2026, 9, 22),
        dt.date(2026, 9, 24),
        dt.date(2026, 9, 18),
        1,
        id="five-day-streak-middle",
    ),
    pytest.param(
        "5連休の初日(土)",
        _jst_0800(2026, 9, 19),
        dt.date(2026, 9, 24),
        dt.date(2026, 9, 18),
        1,
        id="five-day-streak-first-day",
    ),
    pytest.param(
        "GW(こどもの日)",
        _jst_0800(2026, 5, 5),
        dt.date(2026, 5, 7),
        dt.date(2026, 5, 1),
        1,
        id="golden-week-childrens-day",
    ),
    pytest.param(
        "GW 初日(土)",
        _jst_0800(2026, 5, 2),
        dt.date(2026, 5, 7),
        dt.date(2026, 5, 1),
        1,
        id="golden-week-first-saturday",
    ),
    pytest.param(
        "年末(12/31 は JPX 休場)",
        _jst_0800(2026, 12, 31),
        dt.date(2027, 1, 5),
        dt.date(2026, 12, 30),
        2,
        id="year-end-closure",
    ),
    pytest.param(
        "年始(1/2)",
        _jst_0800(2027, 1, 2),
        dt.date(2027, 1, 5),
        dt.date(2026, 12, 30),
        2,
        id="new-year-closure",
    ),
    # 対照: 営業日の now でも as_of が「直前の営業日」になること
    # (非営業日だけの挙動ではないことの確認)
    pytest.param(
        "対照: 年末直前の営業日(水)",
        _jst_0800(2026, 12, 30),
        dt.date(2027, 1, 5),
        dt.date(2026, 12, 29),
        2,
        id="control-business-day-before-year-end",
    ),
    pytest.param(
        "対照: 月曜の寄り付き前",
        _jst_0800(2026, 10, 5),
        dt.date(2026, 10, 6),
        dt.date(2026, 10, 2),
        1,
        id="control-monday-before-open",
    ),
]


@pytest.mark.parametrize(
    ("situation", "now", "earnings", "expected_as_of", "expected_business_days"),
    _NON_BUSINESS_CASES,
)
def test_snapshot_wires_the_previous_business_day_and_business_days_to_earnings(
    situation: str,
    now: dt.datetime,
    earnings: dt.date,
    expected_as_of: dt.date,
    expected_business_days: int,
) -> None:
    snapshot, error = _build(now, earnings)
    assert error is None, situation
    assert snapshot is not None
    # price_as_of_date: provider が返した「直前の営業日」がそのまま載る
    # (now の暦日にも、翌営業日にもならない)
    assert snapshot.price_as_of_date == expected_as_of, situation
    # 決算日は営業日・非営業日を問わず evaluation_date 以降なので CONFIRMED のまま
    assert snapshot.earnings_date_status == EarningsDateStatus.CONFIRMED, situation
    assert snapshot.next_earnings_date == earnings, situation
    # 営業日数: evaluation_date(JST 暦日)の翌日から決算日までの営業日だけを数える
    assert snapshot.business_days_to_earnings == expected_business_days, situation


# UTC と JST で暦日が異なる境界(金曜 → 土曜)。
# UTC 2026-10-02 14:59 = 金曜 23:59 JST(営業日の評価)/ UTC 15:00 = 土曜 00:00 JST(非営業日の評価)。
# どちらも provider の as_of は 2026-10-02(金)。
_UTC = dt.UTC
_FRI_2359_JST = dt.datetime(2026, 10, 2, 14, 59, tzinfo=_UTC)
_SAT_0000_JST = dt.datetime(2026, 10, 2, 15, 0, tzinfo=_UTC)

_DAY_BOUNDARY_CASES = [
    # 決算日 = 金曜(10/02)
    pytest.param(
        _FRI_2359_JST,
        dt.date(2026, 10, 2),
        EarningsDateStatus.CONFIRMED,
        0,
        id="fri-2359-earnings-fri",
    ),
    pytest.param(
        _SAT_0000_JST,
        dt.date(2026, 10, 2),
        EarningsDateStatus.STALE_PAST_DATE,
        None,
        id="sat-0000-earnings-fri",
    ),
    # 決算日 = 土曜(10/03。非営業日。評価日が金曜でも土曜でも CONFIRMED)
    pytest.param(
        _FRI_2359_JST,
        dt.date(2026, 10, 3),
        EarningsDateStatus.CONFIRMED,
        0,
        id="fri-2359-earnings-sat",
    ),
    pytest.param(
        _SAT_0000_JST,
        dt.date(2026, 10, 3),
        EarningsDateStatus.CONFIRMED,
        0,
        id="sat-0000-earnings-sat",
    ),
    # 決算日 = 月曜(10/05)。金曜 23:59 でも土曜 00:00 でも土日を挟んで 1 営業日
    pytest.param(
        _FRI_2359_JST,
        dt.date(2026, 10, 5),
        EarningsDateStatus.CONFIRMED,
        1,
        id="fri-2359-earnings-mon",
    ),
    pytest.param(
        _SAT_0000_JST,
        dt.date(2026, 10, 5),
        EarningsDateStatus.CONFIRMED,
        1,
        id="sat-0000-earnings-mon",
    ),
]


@pytest.mark.parametrize(
    ("now", "earnings", "expected_status", "expected_business_days"), _DAY_BOUNDARY_CASES
)
def test_utc_jst_day_boundary_uses_the_jst_calendar_date(
    now: dt.datetime,
    earnings: dt.date,
    expected_status: EarningsDateStatus,
    expected_business_days: int | None,
) -> None:
    snapshot, error = _build(now, earnings)
    assert error is None
    assert snapshot is not None
    # 境界の前後どちらでも provider の直近営業日は金曜(10/02)
    assert snapshot.price_as_of_date == dt.date(2026, 10, 2)
    assert snapshot.earnings_date_status == expected_status
    assert snapshot.business_days_to_earnings == expected_business_days
    if expected_status == EarningsDateStatus.STALE_PAST_DATE:
        # 過去日は next_earnings_date へ格納されない(STALE の決算日を消費者が使わない)
        assert snapshot.next_earnings_date is None
    else:
        assert snapshot.next_earnings_date == earnings


def test_missing_earnings_date_leaves_business_days_unset_on_a_non_business_day() -> None:
    """決算日が無い(UNAVAILABLE)なら営業日数は None。非営業日 now でも as_of は直前の営業日。"""
    snapshot, error = _build(_jst_0800(2026, 10, 3), None)
    assert error is None
    assert snapshot is not None
    assert snapshot.earnings_date_status == EarningsDateStatus.UNAVAILABLE
    assert snapshot.next_earnings_date is None
    assert snapshot.business_days_to_earnings is None
    assert snapshot.price_as_of_date == dt.date(2026, 10, 2)
