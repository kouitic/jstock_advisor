"""JpxIndustrySourceのテスト(Issue #54 Phase B-1)。

固定する契約:

- 既存のJPXキャッシュ(`CandidateUniverseCacheIO.read_current("listed_issues")`)を
  読むだけで、新規ダウンロード・再パース規則・推測を行わない
- **「一覧に無い(NOT_FOUND)」と「一覧を読めない(SOURCE_UNAVAILABLE)」を潰さない**
  (#59 の FAILURE ≠ SUCCESS + missing と同じ区別。潰すとJPX解決率を評価できない)
- 値の推測を行わない(RESOLVED以外では entry を返さない)
- 読み取り・パース失敗でも例外を送出しない(観測のためにBUY判定を止めない)
- 失敗を永久キャッシュしない(negative cache TTL経過後に再試行する)
- 成功後はプロセス内で使い回し、呼び出しごとに再パースしない
- **共有インスタンスのresetで成功マップ・negative cache timestampが同時に消え、
  テスト間へ漏れない**(実行順序に依存しない)
"""

from __future__ import annotations

import datetime as dt

import pytest

from jstock_advisor.domain.classification.canonical_industry import JpxLookupStatus
from jstock_advisor.interfaces.candidate_universe import CandidateUniverseItem
from jstock_advisor.providers.candidate_universe.jpx_impl import ParsedListedIssues
from jstock_advisor.services import jpx_industry_source as module
from jstock_advisor.services.jpx_industry_source import (
    JpxIndustrySource,
    get_default_jpx_industry_source,
    reset_default_jpx_industry_source,
)

_PRIME = "プライム（内国株式）"
_REIT_SEGMENT = "REIT・ベンチャーファンド・カントリーファンド・インフラファンド"


def _item(stock_code: str, code: str, name: str, segment: str) -> CandidateUniverseItem:
    return CandidateUniverseItem(
        stock_code=stock_code,
        stock_name=f"テスト{stock_code}",
        market_segment=segment,
        industry_33_code=code,
        industry_33_name=name,
    )


def _parsed(items: list[CandidateUniverseItem]) -> ParsedListedIssues:
    return ParsedListedIssues(
        items=items,
        raw_row_count=len(items),
        invalid_code_count=0,
        duplicate_count=0,
        unknown_market_segment_count=0,
        source_date=dt.date(2026, 8, 28),
    )


class _FakeCacheIO:
    """`CandidateUniverseCacheIO` の差し替え。read_currentの呼び出し回数を数える。"""

    instantiations = 0
    read_calls = 0
    result: object = (b"xls-bytes", object())

    def __init__(self) -> None:
        type(self).instantiations += 1

    def read_current(self, source: str) -> object:
        type(self).read_calls += 1
        assert source == "listed_issues"
        if isinstance(type(self).result, Exception):
            raise type(self).result
        return type(self).result


# 本モジュールはローダ自体を検証するため、conftestのautouse fixture
# (`_isolated_jpx_industry_source`が`_load_jpx_industry_map`を空マップへ差し替える)
# を実装関数へ戻したうえで、その内側のキャッシュIO / parseだけを差し替える。
_REAL_LOAD_JPX_INDUSTRY_MAP = module._load_jpx_industry_map


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "_load_jpx_industry_map", _REAL_LOAD_JPX_INDUSTRY_MAP)
    _FakeCacheIO.instantiations = 0
    _FakeCacheIO.read_calls = 0
    _FakeCacheIO.result = (b"xls-bytes", object())
    reset_default_jpx_industry_source()


def _install(
    monkeypatch: pytest.MonkeyPatch, items: list[CandidateUniverseItem] | None = None
) -> list[set[str] | None]:
    """FakeCacheIOとparse関数を差し込み、parseへ渡された市場区分フィルタを記録する。"""
    seen_filters: list[set[str] | None] = []
    entries = items if items is not None else [_item("1234", "3050", "医薬品", _PRIME)]

    def _fake_parse(data: bytes, target_market_segments: set[str] | None) -> ParsedListedIssues:
        seen_filters.append(target_market_segments)
        return _parsed(entries)

    monkeypatch.setattr(module, "CandidateUniverseCacheIO", _FakeCacheIO)
    monkeypatch.setattr(module, "parse_listed_issues_xls", _fake_parse)
    return seen_filters


def test_returns_entry_for_known_stock_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)

    result = JpxIndustrySource().lookup("1234")

    assert result.status is JpxLookupStatus.RESOLVED
    assert result.entry is not None
    assert result.entry.industry_33_code == "3050"
    assert result.entry.industry_33_name == "医薬品"
    assert result.entry.market_segment == _PRIME


def test_unknown_stock_code_is_not_found_not_source_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一覧は読めたが行が無い場合は NOT_FOUND(推測値も返さない)。"""
    _install(monkeypatch)

    result = JpxIndustrySource().lookup("9999")

    assert result.status is JpxLookupStatus.NOT_FOUND
    assert result.entry is None


def test_does_not_filter_by_market_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    """ETF・REITも読み込む(区分の判定はcanonical_industry側の責務)。"""
    seen_filters = _install(
        monkeypatch,
        items=[
            _item("1234", "3050", "医薬品", _PRIME),
            _item("8951", "8050", "不動産業", _REIT_SEGMENT),
        ],
    )
    source = JpxIndustrySource()

    assert seen_filters == []
    reit = source.lookup("8951")

    assert seen_filters == [None]
    assert reit.entry is not None
    assert reit.entry.market_segment == _REIT_SEGMENT


def test_missing_cache_is_source_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)
    _FakeCacheIO.result = None

    result = JpxIndustrySource().lookup("1234")

    assert result.status is JpxLookupStatus.SOURCE_UNAVAILABLE
    assert result.entry is None


def test_read_failure_is_source_unavailable_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """キャッシュ読み取りが例外を投げても観測側へ伝播させない。"""
    _install(monkeypatch)
    _FakeCacheIO.result = RuntimeError("s3 unavailable")

    assert JpxIndustrySource().lookup("1234").status is JpxLookupStatus.SOURCE_UNAVAILABLE


def test_parse_failure_is_source_unavailable_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(data: bytes, target_market_segments: set[str] | None) -> ParsedListedIssues:
        raise ValueError("broken xls")

    monkeypatch.setattr(module, "CandidateUniverseCacheIO", _FakeCacheIO)
    monkeypatch.setattr(module, "parse_listed_issues_xls", _boom)

    assert JpxIndustrySource().lookup("1234").status is JpxLookupStatus.SOURCE_UNAVAILABLE


def test_source_failure_and_missing_row_are_not_collapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**同一の観測値へ潰れないこと**を1つのテストで直接固定する。

    ここが潰れると、JPX解決率の低さが銘柄側の事情なのかキャッシュ障害なのかを
    区別できず、Phase B-2 の実施可否を判断できなくなる。
    """
    _install(monkeypatch)
    not_found = JpxIndustrySource().lookup("9999").status

    _FakeCacheIO.result = RuntimeError("s3 unavailable")
    unavailable = JpxIndustrySource().lookup("9999").status

    assert not_found is JpxLookupStatus.NOT_FOUND
    assert unavailable is JpxLookupStatus.SOURCE_UNAVAILABLE
    assert not_found != unavailable


def test_successful_load_is_reused_without_reparsing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)
    source = JpxIndustrySource()

    source.lookup("1234")
    source.lookup("1234")
    source.lookup("9999")

    assert _FakeCacheIO.read_calls == 1


def test_failure_is_not_cached_forever_and_retries_after_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """negative cacheはTTL内だけ再試行を抑止し、TTL経過後は再試行する。

    一時的なS3/パースエラー1回でコンテナ生存期間中ずっと解決できなくなるのを避ける。
    """
    _install(monkeypatch)
    _FakeCacheIO.result = RuntimeError("transient")
    now = dt.datetime(2026, 8, 29, 0, 0, tzinfo=dt.UTC)
    source = JpxIndustrySource(negative_cache_ttl_seconds=60, clock=lambda: now)

    assert source.lookup("1234").status is JpxLookupStatus.SOURCE_UNAVAILABLE
    assert _FakeCacheIO.read_calls == 1

    # TTL内は再試行しない。
    assert source.lookup("1234").status is JpxLookupStatus.SOURCE_UNAVAILABLE
    assert _FakeCacheIO.read_calls == 1

    # TTL経過後は再試行し、復旧していれば解決できる。
    now = now + dt.timedelta(seconds=61)
    _FakeCacheIO.result = (b"xls-bytes", object())
    result = source.lookup("1234")

    assert _FakeCacheIO.read_calls == 2
    assert result.entry is not None
    assert result.entry.industry_33_code == "3050"


def test_default_source_is_shared_within_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """fan-out(Lambda 1実行=1銘柄)でも、同一プロセス内では再パースしない。"""
    _install(monkeypatch)

    first = get_default_jpx_industry_source()
    second = get_default_jpx_industry_source()

    assert first is second
    first.lookup("1234")
    second.lookup("1234")
    assert _FakeCacheIO.read_calls == 1


def test_reset_default_source_discards_shared_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)
    first = get_default_jpx_industry_source()

    reset_default_jpx_industry_source()

    assert get_default_jpx_industry_source() is not first


# --- 共有stateの隔離(Issue #54 Phase B-1 implementation review §2)---------
#
# `JpxIndustrySource` はプロセス内共有インスタンスを持ち、その中に
# 「成功マップ」と「negative cacheのtimestamp」という2つの可変stateを抱える。
# これがテスト間へ漏れると実行順序で結果が変わるため、resetで**両方が同時に**
# 消えることを固定する(片方だけ残る状態を作らない)。


def test_reset_discards_success_map_so_later_failure_is_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """success → reset → failure。成功マップがresetを越えて残らない。"""
    _install(monkeypatch)
    assert get_default_jpx_industry_source().lookup("1234").status is JpxLookupStatus.RESOLVED

    reset_default_jpx_industry_source()
    _FakeCacheIO.result = RuntimeError("s3 unavailable")

    # 古い成功マップを使い回していれば RESOLVED のままになってしまう。
    assert (
        get_default_jpx_industry_source().lookup("1234").status
        is JpxLookupStatus.SOURCE_UNAVAILABLE
    )


def test_reset_discards_negative_cache_so_later_success_is_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """failure → reset → success。negative cacheのtimestampがresetを越えて残らない。"""
    _install(monkeypatch)
    _FakeCacheIO.result = RuntimeError("transient")
    assert (
        get_default_jpx_industry_source().lookup("1234").status
        is JpxLookupStatus.SOURCE_UNAVAILABLE
    )

    reset_default_jpx_industry_source()
    _FakeCacheIO.result = (b"xls-bytes", object())

    # timestampが残っていればTTL(60秒)内は再試行されず失敗のままになる。
    assert get_default_jpx_industry_source().lookup("1234").status is JpxLookupStatus.RESOLVED


def test_reset_allows_immediate_retry_without_waiting_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """negative cache → reset → retry。時刻を進めずに再読み取りが起きる。"""
    _install(monkeypatch)
    _FakeCacheIO.result = RuntimeError("transient")
    frozen = dt.datetime(2026, 8, 29, 0, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(module, "_default_clock", lambda: frozen)

    get_default_jpx_industry_source().lookup("1234")
    assert _FakeCacheIO.read_calls == 1
    # 同一インスタンスではTTL内なので再読み取りしない。
    get_default_jpx_industry_source().lookup("1234")
    assert _FakeCacheIO.read_calls == 1

    reset_default_jpx_industry_source()
    get_default_jpx_industry_source().lookup("1234")

    assert _FakeCacheIO.read_calls == 2


def test_shared_state_does_not_leak_part_a_success_then_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """順序独立性(A)。共有インスタンスは必ず未ロード状態から始まる。

    本testと `..._part_b_...` は**どちらが先に実行されても両方PASS**する。
    先行testが成功マップ/失敗timestampのどちらを残しても検知できるよう、
    冒頭で「まだ一度も読んでいない」ことを read_calls で確認する。
    """
    _install(monkeypatch)
    assert _FakeCacheIO.read_calls == 0

    assert get_default_jpx_industry_source().lookup("1234").status is JpxLookupStatus.RESOLVED
    assert _FakeCacheIO.read_calls == 1


def test_shared_state_does_not_leak_part_b_failure_then_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """順序独立性(B)。`..._part_a_...` と逆順で実行しても同じ結果になる。"""
    _install(monkeypatch)
    assert _FakeCacheIO.read_calls == 0

    _FakeCacheIO.result = RuntimeError("transient")
    assert (
        get_default_jpx_industry_source().lookup("1234").status
        is JpxLookupStatus.SOURCE_UNAVAILABLE
    )
    assert _FakeCacheIO.read_calls == 1


def test_conftest_autouse_fixture_leaves_no_shared_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """conftestのautouse fixtureが、テスト開始時点で共有インスタンスを破棄済みであること。

    productionコードのglobal stateをunit test側から完全にresetできることの確認。
    """
    assert module._DEFAULT_SOURCE is None  # noqa: SLF001 - global stateの検査そのものが目的

    _install(monkeypatch)
    get_default_jpx_industry_source().lookup("1234")

    assert module._DEFAULT_SOURCE is not None  # noqa: SLF001
