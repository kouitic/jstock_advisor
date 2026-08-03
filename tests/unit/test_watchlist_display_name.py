"""services/watchlist_display_name.py: JpxStockNameSource/StockDisplayNameResolverの
テスト(LINE通知品質改善)。

- JpxStockNameSource: 成功キャッシュ・negative cache(TTL)・失敗の永久キャッシュ
  禁止をテスト用clockでsleep無しに検証する。
- StockDisplayNameResolver: JPX→Override→既存Watchlist→外部fallbackの
  真の遅延評価(前段で解決できれば後段を一切呼ばない)、各ソースの例外隔離、
  全ソース未解決時の最終フォールバックWARNINGログを検証する。
"""

from __future__ import annotations

import datetime as dt
import logging

import pytest

from jstock_advisor.services.watchlist_display_name import (
    JpxStockNameSource,
    StockDisplayNameResolver,
)

# --- JpxStockNameSource(negative cache) -----------------------------------------


class _FakeClock:
    def __init__(self, start: dt.datetime) -> None:
        self._now = start

    def __call__(self) -> dt.datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += dt.timedelta(seconds=seconds)


def _patch_loader(monkeypatch: pytest.MonkeyPatch, results: list[dict[str, str] | None]) -> list:
    """呼び出しごとにresultsを1つずつpopして返すフェイクローダー。呼び出し回数を
    calls配列で記録する(popできなくなったら最後の値を使い続ける)。"""
    calls: list[None] = []
    remaining = list(results)

    def _fake_loader() -> dict[str, str] | None:
        calls.append(None)
        if remaining:
            return remaining.pop(0)
        return None

    monkeypatch.setattr(
        "jstock_advisor.services.watchlist_display_name._load_jpx_stock_name_map", _fake_loader
    )
    return calls


def test_jpx_source_caches_success_and_avoids_repeated_io(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_loader(monkeypatch, [{"1301": "テスト水産"}])
    clock = _FakeClock(dt.datetime(2026, 8, 1, tzinfo=dt.UTC))
    source = JpxStockNameSource(negative_cache_ttl_seconds=60, clock=clock)

    assert source.get("1301") == "テスト水産"
    assert source.get("1301") == "テスト水産"
    assert len(calls) == 1


def test_jpx_source_does_not_permanently_cache_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_loader(monkeypatch, [None, {"1301": "テスト水産"}])
    clock = _FakeClock(dt.datetime(2026, 8, 1, tzinfo=dt.UTC))
    source = JpxStockNameSource(negative_cache_ttl_seconds=60, clock=clock)

    assert source.get("1301") is None
    clock.advance(61)
    assert source.get("1301") == "テスト水産"
    assert len(calls) == 2


def test_jpx_source_negative_cache_ttl_suppresses_retry_within_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_loader(monkeypatch, [None, {"1301": "テスト水産"}])
    clock = _FakeClock(dt.datetime(2026, 8, 1, tzinfo=dt.UTC))
    source = JpxStockNameSource(negative_cache_ttl_seconds=60, clock=clock)

    assert source.get("1301") is None
    clock.advance(30)  # TTL(60秒)未満
    assert source.get("1301") is None
    assert len(calls) == 1  # 2回目のget()はnegative cache期間中のため再取得しない


def test_jpx_source_retries_after_negative_cache_ttl_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_loader(monkeypatch, [None, {"1301": "テスト水産"}])
    clock = _FakeClock(dt.datetime(2026, 8, 1, tzinfo=dt.UTC))
    source = JpxStockNameSource(negative_cache_ttl_seconds=60, clock=clock)

    assert source.get("1301") is None
    clock.advance(60.01)  # TTLちょうど経過後
    assert source.get("1301") == "テスト水産"
    assert len(calls) == 2


def test_jpx_source_success_cache_is_used_regardless_of_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_loader(monkeypatch, [{"1301": "テスト水産"}])
    clock = _FakeClock(dt.datetime(2026, 8, 1, tzinfo=dt.UTC))
    source = JpxStockNameSource(negative_cache_ttl_seconds=1, clock=clock)

    assert source.get("1301") == "テスト水産"
    clock.advance(1000)  # TTLをはるかに超える経過時間でも再取得しない
    assert source.get("1301") == "テスト水産"
    assert len(calls) == 1


def test_jpx_source_returns_none_for_unknown_stock_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_loader(monkeypatch, [{"1301": "テスト水産"}])
    clock = _FakeClock(dt.datetime(2026, 8, 1, tzinfo=dt.UTC))
    source = JpxStockNameSource(negative_cache_ttl_seconds=60, clock=clock)

    assert source.get("9999") is None


# --- StockDisplayNameResolver(遅延評価・例外隔離) --------------------------------


class _FakeSource:
    def __init__(self, name: str | None = None, raises: Exception | None = None) -> None:
        self.calls = 0
        self._name = name
        self._raises = raises

    def get(self, stock_code: str) -> str | None:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._name


class _FakeWatchlistItem:
    def __init__(self, stock_name: str | None) -> None:
        self.stock_name = stock_name


class _FakeWatchlistRepo:
    def __init__(self, name: str | None = None, raises: Exception | None = None) -> None:
        self.calls = 0
        self._name = name
        self._raises = raises

    def get(self, stock_code: str) -> _FakeWatchlistItem | None:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return _FakeWatchlistItem(self._name) if self._name is not None else None


def _resolver(
    jpx: _FakeSource | None = None,
    override: _FakeSource | None = None,
    watchlist: _FakeWatchlistRepo | None = None,
) -> tuple[StockDisplayNameResolver, _FakeSource, _FakeSource, _FakeWatchlistRepo]:
    jpx = jpx or _FakeSource()
    override = override or _FakeSource()
    watchlist = watchlist or _FakeWatchlistRepo()
    return StockDisplayNameResolver(jpx, override, watchlist), jpx, override, watchlist


def test_resolve_uses_jpx_name_and_does_not_call_later_sources() -> None:
    resolver, jpx, override, watchlist = _resolver(jpx=_FakeSource(name="JPX名称"))

    result = resolver.resolve("1234", fallback_name_provider=lambda: pytest.fail("called"))

    assert result == "JPX名称"
    assert jpx.calls == 1
    assert override.calls == 0
    assert watchlist.calls == 0


def test_resolve_falls_through_to_override_when_jpx_unresolved() -> None:
    resolver, jpx, override, watchlist = _resolver(override=_FakeSource(name="Override名称"))

    result = resolver.resolve("1234")

    assert result == "Override名称"
    assert jpx.calls == 1
    assert override.calls == 1
    assert watchlist.calls == 0


def test_resolve_falls_through_to_watchlist_when_jpx_and_override_unresolved() -> None:
    resolver, _jpx, _override, watchlist = _resolver(
        watchlist=_FakeWatchlistRepo(name="Watchlist名称")
    )

    result = resolver.resolve("1234", fallback_name_provider=lambda: pytest.fail("called"))

    assert result == "Watchlist名称"
    assert watchlist.calls == 1


def test_resolve_fallback_name_used_only_when_all_sources_unresolved() -> None:
    resolver, _jpx, _override, _watchlist = _resolver()

    result = resolver.resolve("1234", fallback_name="即値の名称")

    assert result == "即値の名称"


def test_resolve_fallback_name_provider_called_lazily_at_most_once() -> None:
    resolver, _jpx, _override, _watchlist = _resolver()
    calls = {"n": 0}

    def provider() -> str:
        calls["n"] += 1
        return "遅延解決名称"

    result = resolver.resolve("1234", fallback_name_provider=provider)

    assert result == "遅延解決名称"
    assert calls["n"] == 1


def test_resolve_falls_back_to_stock_code_when_everything_unresolved() -> None:
    resolver, _jpx, _override, _watchlist = _resolver()

    result = resolver.resolve("1234")

    assert result == "1234"


def test_resolve_treats_whitespace_only_names_as_unresolved() -> None:
    resolver, _jpx, _override, _watchlist = _resolver(jpx=_FakeSource(name="   "))

    result = resolver.resolve("1234", fallback_name="フォールバック名")

    assert result == "フォールバック名"


def test_resolve_jpx_exception_falls_through_to_override(caplog: pytest.LogCaptureFixture) -> None:
    resolver, jpx, override, _watchlist = _resolver(
        jpx=_FakeSource(raises=RuntimeError("boom")), override=_FakeSource(name="Override名称")
    )

    with caplog.at_level(logging.WARNING):
        result = resolver.resolve("1234")

    assert result == "Override名称"
    assert jpx.calls == 1
    assert override.calls == 1
    warning_records = [r for r in caplog.records if "continue_to_next_source=true" in r.message]
    assert any("source_name=jpx" in r.message for r in warning_records)


def test_resolve_override_exception_falls_through_to_watchlist() -> None:
    resolver, _jpx, override, watchlist = _resolver(
        override=_FakeSource(raises=ValueError("boom")),
        watchlist=_FakeWatchlistRepo(name="Watchlist名称"),
    )

    result = resolver.resolve("1234")

    assert result == "Watchlist名称"
    assert override.calls == 1


def test_resolve_watchlist_exception_falls_through_to_fallback() -> None:
    resolver, _jpx, _override, watchlist = _resolver(
        watchlist=_FakeWatchlistRepo(raises=RuntimeError("boom"))
    )

    result = resolver.resolve("1234", fallback_name="フォールバック名")

    assert result == "フォールバック名"
    assert watchlist.calls == 1


def test_resolve_all_sources_exception_falls_back_to_stock_code_and_continues() -> None:
    resolver, _jpx, _override, _watchlist = _resolver(
        jpx=_FakeSource(raises=RuntimeError("boom1")),
        override=_FakeSource(raises=RuntimeError("boom2")),
        watchlist=_FakeWatchlistRepo(raises=RuntimeError("boom3")),
    )

    def failing_provider() -> str:
        raise RuntimeError("boom4")

    result = resolver.resolve("1234", fallback_name_provider=failing_provider)

    assert result == "1234"


def test_resolve_all_sources_none_emits_final_fallback_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver, _jpx, _override, _watchlist = _resolver()

    with caplog.at_level(logging.WARNING):
        result = resolver.resolve("1234")

    assert result == "1234"
    final_warnings = [r for r in caplog.records if "resolution_result=unresolved" in r.message]
    assert len(final_warnings) == 1
    message = final_warnings[0].message
    assert "stock_code=1234" in message
    assert "fallback_to_stock_code=true" in message


def test_resolve_all_sources_whitespace_emits_final_fallback_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver, _jpx, _override, _watchlist = _resolver(
        jpx=_FakeSource(name=""), override=_FakeSource(name="   ")
    )

    with caplog.at_level(logging.WARNING):
        result = resolver.resolve("1234")

    assert result == "1234"
    assert any("resolution_result=unresolved" in r.message for r in caplog.records)


def test_resolve_fallback_provider_returning_none_sets_fallback_provider_called_true(
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver, _jpx, _override, _watchlist = _resolver()

    with caplog.at_level(logging.WARNING):
        result = resolver.resolve("1234", fallback_name_provider=lambda: None)

    assert result == "1234"
    final_warnings = [r for r in caplog.records if "resolution_result=unresolved" in r.message]
    assert len(final_warnings) == 1
    assert "fallback_provider_called=True" in final_warnings[0].message


def test_resolve_exception_in_fallback_provider_still_continues_registration() -> None:
    """fallback_name_provider例外時もstock_codeが返り、呼び出し元の登録処理
    (WatchlistRepository.add_if_new()等)を継続できる(例外を再送出しない)。"""
    resolver, _jpx, _override, _watchlist = _resolver()

    def failing_provider() -> str:
        raise RuntimeError("network error")

    result = resolver.resolve("1234", fallback_name_provider=failing_provider)

    assert result == "1234"


def test_resolve_success_case_does_not_emit_final_fallback_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver, _jpx, _override, _watchlist = _resolver(jpx=_FakeSource(name="JPX名称"))

    with caplog.at_level(logging.WARNING):
        resolver.resolve("1234")

    assert not any("resolution_result=unresolved" in r.message for r in caplog.records)


def test_resolve_each_source_called_at_most_once_even_on_success() -> None:
    resolver, jpx, override, watchlist = _resolver(jpx=_FakeSource(name="JPX名称"))

    resolver.resolve("1234")
    resolver.resolve("1234")

    # 同一resolver・同一呼び出しではキャッシュしないため、2回のresolve()呼び出しで
    # jpxは2回呼ばれるが、1回のresolve()内では最大1回のみであることを確認する。
    assert jpx.calls == 2
    assert override.calls == 0
    assert watchlist.calls == 0
