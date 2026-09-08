"""定点評価の1実行(run)スコープで株価履歴をメモ化するMarketDataProviderデコレータ。

Issue #113: `RecommendationEvaluationService._evaluate_one()`は評価1件ごとに

- 対象銘柄の`get_price_history()`
- ベンチマーク(TOPIX)の`get_benchmark_price_history()`

を呼ぶが、`YFinanceMarketDataProvider`にはキャッシュが一切無いため、
**同一のTOPIX履歴を評価のたびに取り直していた**。DynamoDBのN+1 Scanを除去すると
この外部I/Oが次の支配項になるため、run単位でメモ化する。

## look-ahead biasを作らないこと(最重要)

キャッシュは「取得範囲を広げて1回だけ取り、各評価では**その評価基準日までを
slice して返す**」方式とする。返す`PriceHistory.bars`は常に呼び出し側が要求した
`[start, end]`のみであり、**evaluation_dateより未来のバーは決して混入しない**。
`_evaluate_one()`が使う`_latest_bar_on_or_before(bars, evaluation_date)`と
`period_bars`の絞り込みは、キャッシュ有無で完全に同一の結果になる
(providerは元々`[start, end]`のバーのみを返すため、和集合を取ってから
sliceした結果は個別取得した結果と一致する)。

## run scopeであること

module levelやprocess levelのキャッシュにはしない。Lambdaの実行環境は
再利用されるため、process levelに置くと**次の日の実行が前日のバーを再利用**し、
まさにlook-ahead/stale biasの温床になる。本クラスはrunごとに生成し、
run終了とともに破棄する。

## 取得失敗の扱い

`ProviderDataError`等の例外は**握り潰さず素通しする**(Issue #59 Phase B2:
取得失敗を「データ無し」へ潰すと再試行・障害率の安全弁が発火しなくなる)。
例外時はキャッシュへ何も記録しないため、次の呼び出しで再度providerへ到達する。

「応答は成立したが対象期間にバーが無い(None)」はrun中は安定とみなして
キャッシュする(同一runで同じ銘柄へ何度も無駄な問い合わせをしないため)。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.interfaces.market_data import MarketDataProvider
from jstock_advisor.interfaces.types import PriceBar, PriceHistory, PriceSnapshot

_HistoryFetch = Callable[[dt.date, dt.date], PriceHistory | None]


@dataclass
class _CachedWindow:
    """あるsymbolについて取得済みの範囲と、その範囲内の全バー。"""

    start: dt.date
    end: dt.date
    bars: list[PriceBar]
    source: DataSourceReference | None


class RunScopedMarketDataCache:
    """1 run内で株価・ベンチマーク履歴をメモ化するMarketDataProviderデコレータ。

    `upper_bound`はそのrunで評価対象になり得る最大の評価基準日(通常はJST当日)。
    初回取得時にこの日まで範囲を広げて取ることで、同一銘柄の複数horizonが
    1回の取得で賄えるようにする(取得範囲を広げても、返却時に必ず
    `[start, end]`へsliceするためlook-ahead biasは生じない)。
    """

    def __init__(self, inner: MarketDataProvider, upper_bound: dt.date) -> None:
        self._inner = inner
        self._upper_bound = upper_bound
        self._stock_windows: dict[str, _CachedWindow] = {}
        self._benchmark_windows: dict[str, _CachedWindow] = {}
        self._provider_call_count = 0

    @property
    def provider_call_count(self) -> int:
        """このrunで実際に下位providerへ到達した履歴取得の回数(観測用)。"""
        return self._provider_call_count

    @property
    def cached_symbol_count(self) -> int:
        return len(self._stock_windows) + len(self._benchmark_windows)

    # --- MarketDataProvider ------------------------------------------------

    def get_price_history(
        self, stock_code: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        window = self._resolve_window(
            self._stock_windows,
            stock_code,
            start,
            end,
            lambda s, e: self._inner.get_price_history(stock_code, s, e),
        )
        return self._slice(window, stock_code, start, end)

    def get_benchmark_price_history(
        self, symbol: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        window = self._resolve_window(
            self._benchmark_windows,
            symbol,
            start,
            end,
            lambda s, e: self._inner.get_benchmark_price_history(symbol, s, e),
        )
        return self._slice(window, symbol, start, end)

    def get_latest_price(self, stock_code: str) -> PriceSnapshot | None:
        """定点評価では使わないため素通しする(キャッシュしない)。"""
        return self._inner.get_latest_price(stock_code)

    def get_average_trading_value(self, stock_code: str, business_days: int) -> Decimal | None:
        """定点評価では使わないため素通しする(キャッシュしない)。"""
        return self._inner.get_average_trading_value(stock_code, business_days)

    # --- 内部 --------------------------------------------------------------

    def _resolve_window(
        self,
        cache: dict[str, _CachedWindow],
        key: str,
        start: dt.date,
        end: dt.date,
        fetch: _HistoryFetch,
    ) -> _CachedWindow:
        cached = cache.get(key)
        if cached is not None and cached.start <= start and cached.end >= end:
            return cached

        # 未取得、または要求範囲が既存キャッシュに収まらない場合のみ取り直す。
        # 取得範囲はそのrunの上限日まで広げ、同一銘柄の他horizonを賄えるようにする。
        fetch_start = min(start, cached.start) if cached is not None else start
        fetch_end = max(end, self._upper_bound)
        if cached is not None:
            fetch_end = max(fetch_end, cached.end)

        # 例外はここで握り潰さない(Issue #59)。キャッシュへも記録しないため、
        # 次回呼び出しで再度providerへ到達する。
        history = fetch(fetch_start, fetch_end)
        self._provider_call_count += 1

        window = _CachedWindow(
            start=fetch_start,
            end=fetch_end,
            bars=list(history.bars) if history is not None else [],
            source=history.source if history is not None else None,
        )
        cache[key] = window
        return window

    @staticmethod
    def _slice(
        window: _CachedWindow, symbol: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        """要求された`[start, end]`のバーだけを返す(未来のバーを混入させない)。"""
        bars = [bar for bar in window.bars if start <= bar.date <= end]
        if not bars or window.source is None:
            # バーが無い場合、素のproviderは`None`を返す(取得は成立したが該当期間に
            # データが無い)。その意味論をそのまま維持する。
            return None
        return PriceHistory(symbol=symbol, bars=bars, source=window.source)
