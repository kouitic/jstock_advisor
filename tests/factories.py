"""本番Protocolへ準拠する共有test double(Issue #646。#275 Child A)。

## なぜ必要か

テストのfake providerが本番Protocol(`MarketDataProvider`)の一部メソッドを
欠いたまま、各テストファイルで個別に(重複して)定義されていた。fakeが
Protocol全体を実装しなくても構文上は「動く」ため、本来検証されるべき
経路(Protocolの4メソッドすべてを本物同様に呼べること)が一度も本物の
契約に対してテストされないまま「テストが通る」という誤った安心感を生んで
いた。

`_typecheck_market_data: MarketDataProvider = FakeMarketDataProvider()`の
1行により、mypy(`strict = true`)がこのfakeとMarketDataProvider Protocolの
構造的な不一致を将来のProtocol変更時にも検出する。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from decimal import Decimal

from jstock_advisor.interfaces.market_data import MarketDataProvider
from jstock_advisor.interfaces.types import PriceHistory, PriceSnapshot


class FakeMarketDataProvider:
    """MarketDataProvider Protocolに準拠する共有fake(#646)。

    `latest_price`は単一の`PriceSnapshot`(全stock_codeへ同じ値を返す)、
    または`stock_code`をキーとする`Mapping`(銘柄ごとに異なる値・Noneを
    返す)のいずれかを受け取る。`raise_for_stock_codes`に含まれる
    stock_codeで`get_latest_price()`を呼ぶと、そのstock_codeを`calls`へ
    記録したうえで`raise_error`(既定`RuntimeError`)を送出する
    (1銘柄目のエラーで処理全体を落とさない、という既存テストの意図を
    再現するため)。
    """

    def __init__(
        self,
        latest_price: PriceSnapshot | Mapping[str, PriceSnapshot | None] | None = None,
        price_history: PriceHistory | None = None,
        average_trading_value: Decimal | None = None,
        benchmark_price_history: PriceHistory | None = None,
        raise_for_stock_codes: frozenset[str] | None = None,
        raise_error: Exception | None = None,
    ) -> None:
        self._latest_price = latest_price
        self._price_history = price_history
        self._average_trading_value = average_trading_value
        self._benchmark_price_history = benchmark_price_history
        self._raise_for_stock_codes = raise_for_stock_codes or frozenset()
        self._raise_error = raise_error
        self.calls: list[str] = []

    def get_latest_price(self, stock_code: str) -> PriceSnapshot | None:
        self.calls.append(stock_code)
        if stock_code in self._raise_for_stock_codes:
            raise (
                self._raise_error
                if self._raise_error is not None
                else RuntimeError(
                    "FakeMarketDataProvider: get_latest_price configured to raise "
                    f"for stock_code={stock_code}"
                )
            )
        if isinstance(self._latest_price, Mapping):
            return self._latest_price.get(stock_code)
        if self._latest_price is not None and self._latest_price.stock_code != stock_code:
            return self._latest_price.model_copy(update={"stock_code": stock_code})
        return self._latest_price

    def get_price_history(
        self, stock_code: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        return self._price_history

    def get_average_trading_value(self, stock_code: str, business_days: int) -> Decimal | None:
        return self._average_trading_value

    def get_benchmark_price_history(
        self, symbol: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        return self._benchmark_price_history


_typecheck_market_data: MarketDataProvider = FakeMarketDataProvider()
