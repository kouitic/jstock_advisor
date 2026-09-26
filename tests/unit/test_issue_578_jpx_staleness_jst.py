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

from jstock_advisor.interfaces.candidate_universe import CandidateUniverseError
from jstock_advisor.providers.candidate_universe import jpx_impl as jpx_impl_module
from jstock_advisor.providers.candidate_universe.jpx_impl import JpxCandidateUniverseProvider

_SOURCE_DATE = dt.date(2026, 8, 1)
_MAX_STALE_HOURS = 24

# サブちゃんレビュー対応D2: 2026-08-01のJST 00:00 = 2026-07-31 15:00 UTC
# (JST = UTC+9)という事実を、`domain.jst.JST`を経由せずリテラルの瞬間として
# 固定する。production側とtest側が同じ`JST`定数を参照すると、その定数自体が
# 誤っていても両者が同時に狂って一致してしまう自己参照になる(反証W4:
# `JST`を+9→+8へ変異させても本ファイルの新テストがSURVIVEしたことで実証済み)。
_SOURCE_DATE_JST_MIDNIGHT_UTC = dt.datetime(2026, 7, 31, 15, 0, tzinfo=dt.UTC)


def _make_provider(
    now: dt.datetime, max_stale_hours: int = _MAX_STALE_HOURS
) -> JpxCandidateUniverseProvider:
    return JpxCandidateUniverseProvider(
        target_market_segments=None,
        listed_issues_max_stale_hours=max_stale_hours,
        jpx400_max_stale_hours=max_stale_hours,
        now=now,
    )


# --- 9時間の食い違い window: 旧基準では「新鮮」、正しいJST基準では「stale」 -------------


def test_check_staleness_raises_within_the_9_hour_discrepancy_window() -> None:
    """旧基準(UTC 00:00起点)ならageは18h(<=24h、新鮮と誤判定)だが、
    正しいJST基準では27h(>24h、stale)になる時点で、実際にstaleと判定されること。
    """
    now = _SOURCE_DATE_JST_MIDNIGHT_UTC + dt.timedelta(hours=27)
    provider = _make_provider(now)
    with pytest.raises(CandidateUniverseError):
        provider._check_staleness("listed_issues", _SOURCE_DATE, max_stale_hours=_MAX_STALE_HOURS)


def test_check_staleness_does_not_raise_just_before_the_jst_basis_boundary() -> None:
    """JST基準の境界(24h)の直前ではstaleにならないこと(境界の連続性)。"""
    now = _SOURCE_DATE_JST_MIDNIGHT_UTC + dt.timedelta(hours=23, minutes=59)
    provider = _make_provider(now)
    provider._check_staleness("listed_issues", _SOURCE_DATE, max_stale_hours=_MAX_STALE_HOURS)


def test_check_staleness_raises_just_after_the_jst_basis_boundary() -> None:
    """JST基準の境界(24h)を過ぎた直後にstaleとなること(境界の連続性)。"""
    now = _SOURCE_DATE_JST_MIDNIGHT_UTC + dt.timedelta(hours=24, minutes=1)
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

    now = _SOURCE_DATE_JST_MIDNIGHT_UTC + dt.timedelta(hours=27)
    provider = _make_provider(now)
    # 修正前の実装(UTC起点)ではageが18hにしか見えず、staleを検出できない。
    provider._check_staleness("listed_issues", _SOURCE_DATE, max_stale_hours=_MAX_STALE_HOURS)


# --- 回帰: 閾値から十分離れた場合の判定は変わらない -------------------------------------


def test_check_staleness_does_not_raise_for_a_clearly_fresh_date() -> None:
    now = _SOURCE_DATE_JST_MIDNIGHT_UTC + dt.timedelta(hours=1)
    provider = _make_provider(now)
    provider._check_staleness("listed_issues", _SOURCE_DATE, max_stale_hours=_MAX_STALE_HOURS)


def test_check_staleness_raises_for_a_clearly_stale_date() -> None:
    now = _SOURCE_DATE_JST_MIDNIGHT_UTC + dt.timedelta(days=10)
    provider = _make_provider(now)
    with pytest.raises(CandidateUniverseError):
        provider._check_staleness("listed_issues", _SOURCE_DATE, max_stale_hours=_MAX_STALE_HOURS)


# --- F2: get_candidate_universe()の公開API側(cache_age_hours)の契約テスト ---------


def test_get_candidate_universe_cache_age_hours_uses_jst_basis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """サブちゃんレビュー対応F2: `_check_staleness()`(private)だけでなく、
    `get_candidate_universe()`(公開API)が返す`cache_age_hours`自体もJST基準で
    算出されていることを固定する(2箇所目のsiteが変異W1で生き残っていた)。

    パース処理(xls/xlsxの実バイト列が必要)自体は本テストの対象外のため、
    `parse_listed_issues_xls`/`parse_jpx400_weight_csv`をスタブへ差し替え、
    キャッシュ読み取り(`CandidateUniverseCacheIO.read_current`)も同様に
    差し替える(いずれもjpx_impl module内のグローバル名として呼ばれるため、
    module越しにmonkeypatchする)。
    """
    from jstock_advisor.services.candidate_universe_downloader import CacheMetadata

    now = _SOURCE_DATE_JST_MIDNIGHT_UTC + dt.timedelta(hours=27)
    metadata = CacheMetadata(
        source_date=_SOURCE_DATE,
        downloaded_at=now,
        validated_at=now,
        promoted_at=now,
        raw_row_count=1,
        selected_count=1,
        invalid_code_count=0,
    )

    def _fake_read_current(self: object, source: str) -> tuple[bytes, CacheMetadata]:
        return b"stub", metadata

    monkeypatch.setattr(
        "jstock_advisor.services.candidate_universe_downloader.CandidateUniverseCacheIO"
        ".read_current",
        _fake_read_current,
    )
    monkeypatch.setattr(
        jpx_impl_module,
        "parse_listed_issues_xls",
        lambda data, target_market_segments: jpx_impl_module.ParsedListedIssues(
            items=[], raw_row_count=0, invalid_code_count=0, duplicate_count=0,
            unknown_market_segment_count=0, source_date=_SOURCE_DATE,
        ),
    )
    monkeypatch.setattr(
        jpx_impl_module,
        "parse_jpx400_weight_csv",
        lambda data: jpx_impl_module.ParsedJpx400Membership(
            member_codes=set(), raw_row_count=0, invalid_code_count=0,
            duplicate_count=0, source_date=_SOURCE_DATE,
        ),
    )

    provider = JpxCandidateUniverseProvider(
        target_market_segments=None,
        listed_issues_max_stale_hours=1_000_000,
        jpx400_max_stale_hours=1_000_000,
        now=now,
    )
    result = provider.get_candidate_universe()

    assert result.cache_age_hours is not None
    # JST基準(27h)で算出される。UTC基準(修正前)なら18hになっていたはず。
    assert result.cache_age_hours == pytest.approx(27.0, abs=0.01)
