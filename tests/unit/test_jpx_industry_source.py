"""JpxIndustrySourceのテスト(Issue #54 Phase B-1)。

固定する契約:

- 既存のJPXキャッシュ(`CandidateUniverseCacheIO.read_current("listed_issues")`)を
  読むだけで、新規ダウンロード・再パース規則・推測を行わない
- 解決できない銘柄は **None**(推測値を返さない)
- 読み取り・パース失敗は例外を送出せず None(観測のためにBUY判定を止めない)
- 失敗を永久キャッシュしない(negative cache TTL経過後に再試行する)
- 成功後はプロセス内で使い回し、呼び出しごとに再パースしない
"""

from __future__ import annotations

import datetime as dt

import pytest

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

    entry = JpxIndustrySource().get("1234")

    assert entry is not None
    assert entry.industry_33_code == "3050"
    assert entry.industry_33_name == "医薬品"
    assert entry.market_segment == _PRIME


def test_returns_none_for_unknown_stock_code_without_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch)

    assert JpxIndustrySource().get("9999") is None


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
    reit = source.get("8951")

    assert seen_filters == [None]
    assert reit is not None
    assert reit.market_segment == _REIT_SEGMENT


def test_missing_cache_returns_none_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)
    _FakeCacheIO.result = None

    assert JpxIndustrySource().get("1234") is None


def test_read_failure_returns_none_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """キャッシュ読み取りが例外を投げても観測側へ伝播させない。"""
    _install(monkeypatch)
    _FakeCacheIO.result = RuntimeError("s3 unavailable")

    assert JpxIndustrySource().get("1234") is None


def test_parse_failure_returns_none_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(data: bytes, target_market_segments: set[str] | None) -> ParsedListedIssues:
        raise ValueError("broken xls")

    monkeypatch.setattr(module, "CandidateUniverseCacheIO", _FakeCacheIO)
    monkeypatch.setattr(module, "parse_listed_issues_xls", _boom)

    assert JpxIndustrySource().get("1234") is None


def test_successful_load_is_reused_without_reparsing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)
    source = JpxIndustrySource()

    source.get("1234")
    source.get("1234")
    source.get("9999")

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

    assert source.get("1234") is None
    assert _FakeCacheIO.read_calls == 1

    # TTL内は再試行しない。
    assert source.get("1234") is None
    assert _FakeCacheIO.read_calls == 1

    # TTL経過後は再試行し、復旧していれば解決できる。
    now = now + dt.timedelta(seconds=61)
    _FakeCacheIO.result = (b"xls-bytes", object())
    entry = source.get("1234")

    assert _FakeCacheIO.read_calls == 2
    assert entry is not None
    assert entry.industry_33_code == "3050"


def test_default_source_is_shared_within_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """fan-out(Lambda 1実行=1銘柄)でも、同一プロセス内では再パースしない。"""
    _install(monkeypatch)

    first = get_default_jpx_industry_source()
    second = get_default_jpx_industry_source()

    assert first is second
    first.get("1234")
    second.get("1234")
    assert _FakeCacheIO.read_calls == 1


def test_reset_default_source_discards_shared_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)
    first = get_default_jpx_industry_source()

    reset_default_jpx_industry_source()

    assert get_default_jpx_industry_source() is not first
