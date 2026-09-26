"""Issue #578(#66 16C): JPXキャッシュのstaleness判定基準日をJST基準へ統一する。

`providers/candidate_universe/jpx_impl.py`の`_check_staleness()`/
`get_candidate_universe()`は、JST公表日である`source_date`を誤って
UTC 00:00として扱っており、実際のJST 00:00より9時間遅い時点を起点にしていた
(経過時間が実際より9時間短く出る)。本テストは、この9時間のズレが実際に
判定結果を変える境界(45日・90日といった閾値の直前9時間)で固定する。

架空の日付のみを使う(実在の公表日程・銘柄情報は使わない)。
"""

from __future__ import annotations

import datetime as dt

import pytest

from jstock_advisor.domain.jst import JST
from jstock_advisor.interfaces.candidate_universe import CandidateUniverseError
from jstock_advisor.providers.candidate_universe import jpx_impl as jpx_impl_module
from jstock_advisor.providers.candidate_universe.jpx_impl import JpxCandidateUniverseProvider

_SOURCE_DATE = dt.date(2026, 8, 1)
_MAX_STALE_HOURS = 24


def _make_provider(
    now: dt.datetime, max_stale_hours: int = _MAX_STALE_HOURS
) -> JpxCandidateUniverseProvider:
    return JpxCandidateUniverseProvider(
        target_market_segments=None,
        listed_issues_max_stale_hours=max_stale_hours,
        jpx400_max_stale_hours=max_stale_hours,
        now=now,
    )


def _jst_midnight_utc(source_date: dt.date) -> dt.datetime:
    """source_date(JST暦日)のJST 00:00をUTCで表す(テスト用の期待値計算)。"""
    return dt.datetime.combine(source_date, dt.time(), tzinfo=JST).astimezone(dt.UTC)


# --- 9時間の食い違い window: 旧基準では「新鮮」、正しいJST基準では「stale」 -------------


def test_check_staleness_raises_within_the_9_hour_discrepancy_window() -> None:
    """旧基準(UTC 00:00起点)ならageは18h(<=24h、新鮮と誤判定)だが、
    正しいJST基準では27h(>24h、stale)になる時点で、実際にstaleと判定されること。
    """
    now = _jst_midnight_utc(_SOURCE_DATE) + dt.timedelta(hours=27)
    provider = _make_provider(now)
    with pytest.raises(CandidateUniverseError):
        provider._check_staleness("listed_issues", _SOURCE_DATE, max_stale_hours=_MAX_STALE_HOURS)


def test_check_staleness_does_not_raise_just_before_the_jst_basis_boundary() -> None:
    """JST基準の境界(24h)の直前ではstaleにならないこと(境界の連続性)。"""
    now = _jst_midnight_utc(_SOURCE_DATE) + dt.timedelta(hours=23, minutes=59)
    provider = _make_provider(now)
    provider._check_staleness("listed_issues", _SOURCE_DATE, max_stale_hours=_MAX_STALE_HOURS)


def test_check_staleness_raises_just_after_the_jst_basis_boundary() -> None:
    """JST基準の境界(24h)を過ぎた直後にstaleとなること(境界の連続性)。"""
    now = _jst_midnight_utc(_SOURCE_DATE) + dt.timedelta(hours=24, minutes=1)
    provider = _make_provider(now)
    with pytest.raises(CandidateUniverseError):
        provider._check_staleness("listed_issues", _SOURCE_DATE, max_stale_hours=_MAX_STALE_HOURS)


def test_check_staleness_negative_verification_without_the_jst_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mutation-based negative verification: jpx_impl.py内の`JST`を修正前の
    `dt.UTC`へ一時的に差し替えると、9時間window内でstaleを検出できなくなる
    (=このテストが検出したい修正前の欠陥そのものを再現する)ことを確認する。
    """
    monkeypatch.setattr(jpx_impl_module, "JST", dt.UTC)

    now = _jst_midnight_utc(_SOURCE_DATE) + dt.timedelta(hours=27)
    provider = _make_provider(now)
    # 修正前の実装(UTC起点)ではageが18hにしか見えず、staleを検出できない。
    provider._check_staleness("listed_issues", _SOURCE_DATE, max_stale_hours=_MAX_STALE_HOURS)


# --- 回帰: 閾値から十分離れた場合の判定は変わらない -------------------------------------


def test_check_staleness_does_not_raise_for_a_clearly_fresh_date() -> None:
    now = _jst_midnight_utc(_SOURCE_DATE) + dt.timedelta(hours=1)
    provider = _make_provider(now)
    provider._check_staleness("listed_issues", _SOURCE_DATE, max_stale_hours=_MAX_STALE_HOURS)


def test_check_staleness_raises_for_a_clearly_stale_date() -> None:
    now = _jst_midnight_utc(_SOURCE_DATE) + dt.timedelta(days=10)
    provider = _make_provider(now)
    with pytest.raises(CandidateUniverseError):
        provider._check_staleness("listed_issues", _SOURCE_DATE, max_stale_hours=_MAX_STALE_HOURS)
