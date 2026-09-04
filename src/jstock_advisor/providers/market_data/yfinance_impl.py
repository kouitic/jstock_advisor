"""market_data_provider の yfinance(Yahoo! Finance非公式ライブラリ)実装。

APIキー不要・無料で利用できるが非公式ライブラリのため、Yahoo側の仕様変更で
予告なく動作しなくなるリスクがある(要求仕様12節: 取得できない場合は推測で
補完しない、に従いすべての例外は握りつぶさずNone/空リストとして扱う)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

import yfinance as yf

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.interfaces.provider_errors import (
    ProviderDataError,
    ProviderFailureCategory,
)
from jstock_advisor.interfaces.types import PriceBar, PriceHistory, PriceSnapshot
from jstock_advisor.providers._failure import raise_provider_data_error
from jstock_advisor.providers.market_data._yfinance_log_filter import (
    install_yfinance_expected_missing_log_filter,
)

# yfinanceは既定で例外を隠し、恒久missing銘柄でもライブラリ自身のloggerへERRORを
# 出力して空データを返す(Issue #125)。モジュール初期化時に一度だけフィルタを登録し、
# 期待される恒久missingのみWARNINGへ降格する。真の障害はERRORのまま素通しする。
install_yfinance_expected_missing_log_filter()

_PROVIDER_NAME = "yfinance"
_TICKER_SUFFIX = ".T"

# yfinance(Yahoo! Finance)側のベンチマーク指数ティッカーシンボル対応表。
# Yahoo! FinanceはTOPIX指数そのものは配信していないため、TOPIX連動型上場投資信託
# (1306.T、野村アセットマネジメント)を代替指標として使用する。ETFのため信託報酬分の
# 差異(年率0.05%程度)がわずかに生じるが、リターン比較の近似としては十分実用的。
_BENCHMARK_TICKERS: dict[str, str] = {
    "TOPIX": "1306.T",  # TOPIX連動型ETF(TOPIX指数そのものではない点に注意)
    "NIKKEI225": "^N225",  # 日経平均は指数そのものが取得可能
}


def _to_decimal(value: float) -> Decimal | None:
    try:
        f = float(value)
    except (ValueError, TypeError):
        return None
    if f != f:  # NaN(欠測日・取引停止日等でyfinanceがNaNを返すことがある)
        return None
    try:
        return Decimal(str(round(f, 2)))
    except InvalidOperation:
        return None


def _reject_non_positive_close(stock_code: str, close: Decimal, *, operation: str) -> None:
    """終値が0以下ならProviderDataError(retryable=False)を送出する(Issue #52 Phase B2)。

    ゼロ/負値の株価は現実の市場価格として成立しない。これを正常なPriceSnapshotとして
    domainへ流入させると、適正価格比較・含み損益・損切り・利確の全経路が
    無意味な値で動く(F-J13。`data_quality_service`の`price > 0`ガードは
    ゼロ価格で**検査自体をskipする**ため、下流では検知できない)。

    `None`へ変換しない。`None`は「取得できたがデータが無い(genuine missing)」を
    意味するのに対し、ゼロ/負値は「取得できたがデータが壊れている」であり別物である。
    両者を混同すると、欠測として静かに素通りする経路が再びできる。

    再試行しても壊れた値は変わらないため`retryable=False`。

    値そのものは例外へ載せない(`ProviderDataError`の既存契約に従い、
    メッセージには当方が構築した安全要約のみを載せる)。
    """
    if close > 0:
        return
    raise ProviderDataError(
        provider_name=_PROVIDER_NAME,
        operation=operation,
        retryable=False,
        failure_category=ProviderFailureCategory.NON_RETRYABLE_PROVIDER_FAILURE,
        error_type="NonPositiveClosePrice",
        error_summary=(
            f"close price must be positive (stock_code={stock_code}, sign="
            f"{'zero' if close == 0 else 'negative'})"
        ),
    )


class YFinanceMarketDataProvider:
    def __init__(self, now: dt.datetime | None = None) -> None:
        self._now = now or dt.datetime.now(dt.UTC)

    def _source(self) -> DataSourceReference:
        return DataSourceReference(provider=_PROVIDER_NAME, fetched_at=self._now)

    def _fetch_history(
        self, ticker_symbol: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        try:
            ticker = yf.Ticker(ticker_symbol)
            df = ticker.history(
                start=start,
                end=end + dt.timedelta(days=1),
                interval="1d",
                auto_adjust=False,
            )
        except Exception as exc:  # noqa: BLE001 - 非公式ライブラリのため例外種別を限定できない
            # Issue #59 Phase B2: 取得失敗を「データ無し(None)」へ潰さない。
            # 潰すと再試行・障害率の安全弁が発火せず、一過性障害が
            # 「そのとき株価が取得できなかった銘柄」として恒久的に記録される。
            raise_provider_data_error(
                exc, provider_name=_PROVIDER_NAME, operation="history"
            )

        if df is None or df.empty:
            # 応答は成立したが対象期間にバーが無い(SUCCESS + empty)。
            return None

        bars: list[PriceBar] = []
        for index, row in df.iterrows():
            open_ = _to_decimal(row["Open"])
            high = _to_decimal(row["High"])
            low = _to_decimal(row["Low"])
            close = _to_decimal(row["Close"])
            if open_ is None or high is None or low is None or close is None:
                continue
            bar_date = index.date() if hasattr(index, "date") else index
            volume = int(row["Volume"]) if row["Volume"] == row["Volume"] else 0  # NaN対策
            bars.append(
                PriceBar(date=bar_date, open=open_, high=high, low=low, close=close, volume=volume)
            )

        if not bars:
            return None
        return PriceHistory(symbol=ticker_symbol, bars=bars, source=self._source())

    def get_latest_price(self, stock_code: str) -> PriceSnapshot | None:
        history = self._fetch_history(
            f"{stock_code}{_TICKER_SUFFIX}",
            self._now.date() - dt.timedelta(days=14),
            self._now.date(),
        )
        if history is None or not history.bars:
            return None
        latest = history.bars[-1]
        # Issue #52 Phase B2: ゼロ/負値の終値をdomainへ流入させない。
        # 「データが無い」(None)ではなく「データはあるが壊れている」ため、
        # missingではなくfailureとして扱う(Issue #59 の failure/missing 分離契約)。
        _reject_non_positive_close(stock_code, latest.close, operation="get_latest_price")
        return PriceSnapshot(
            stock_code=stock_code,
            as_of_date=latest.date,
            close_price=latest.close,
            high_price=latest.high,
            low_price=latest.low,
            volume=latest.volume,
            source=self._source(),
        )

    def get_price_history(
        self, stock_code: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        history = self._fetch_history(f"{stock_code}{_TICKER_SUFFIX}", start, end)
        if history is None:
            return None
        return PriceHistory(symbol=stock_code, bars=history.bars, source=history.source)

    def get_average_trading_value(self, stock_code: str, business_days: int) -> Decimal | None:
        # 土日祝を考慮し、必要営業日数の2倍強のカレンダー日数をさかのぼって取得する
        lookback_days = business_days * 2 + 10
        end = self._now.date()
        start = end - dt.timedelta(days=lookback_days)
        history = self._fetch_history(f"{stock_code}{_TICKER_SUFFIX}", start, end)
        if history is None or not history.bars:
            return None
        recent = history.bars[-business_days:]
        if not recent:
            return None
        values = [bar.close * bar.volume for bar in recent]
        return sum(values, Decimal("0")) / len(values)

    def get_benchmark_price_history(
        self, symbol: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        ticker_symbol = _BENCHMARK_TICKERS.get(symbol, symbol)
        history = self._fetch_history(ticker_symbol, start, end)
        if history is None:
            return None
        return PriceHistory(symbol=symbol, bars=history.bars, source=history.source)
