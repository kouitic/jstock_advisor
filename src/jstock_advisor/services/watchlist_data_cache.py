"""ウォッチリスト専用の株価/財務/配当データキャッシュ(運用ハードニング4節)。

`market_data`/`financial_data`/`dividend_data`のyfinance実装
(`providers/{market_data,financial_data,dividend_data}/yfinance_impl.py`)には
現状キャッシュが一切無く、呼び出しのたびに新規`yf.Ticker()`を生成しネットワーク
アクセスする(1銘柄あたり8〜15件のHTTP通信、15節)。これを共有Provider実装
そのものへ手を入れずに削減するため、`ProviderBundle`をこのモジュールの
`CachingProviderBundle`でラップして使う(既存のinfrastructure.collection_store.
build_collection_store、EdinetFilingCacheRepositoryと同じDynamoDB/ローカルJSON
自動切替パターンを流用)。

適用箇所はウォッチリストの4つのLambdaハンドラ(watchlist_dispatcher/worker/
terminal_failure/batch_reconciler_handler.py)のみで、`buy_candidates_handler.py`・
`holdings_watchlist_handler.py`・共有yfinance Provider実装・
`stock_snapshot_service.py`は一切変更しない。

`get_benchmark_price_history`はシンボル(TOPIX/セクターETF)単位でキャッシュする
ため、週次バッチ全体で実質1〜数回しか実際のfetchが走らない(最大の削減効果)。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from jstock_advisor.config.models import AppConfig, WatchlistDataCacheConfig
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store
from jstock_advisor.interfaces.dividend_data import DividendDataProvider
from jstock_advisor.interfaces.financial_data import FinancialDataProvider
from jstock_advisor.interfaces.market_data import MarketDataProvider
from jstock_advisor.interfaces.types import (
    CashflowDecomposition,
    DividendInfo,
    FinancialSummary,
    HistoricalValuation,
    PriceHistory,
    PriceSnapshot,
)
from jstock_advisor.services.provider_bundle import ProviderBundle

logger = logging.getLogger(__name__)

_PRICE_CACHE_FILE = "watchlist_price_cache.json"
_FINANCIAL_CACHE_FILE = "watchlist_financial_cache.json"

_price_snapshot_adapter: TypeAdapter[PriceSnapshot | None] = TypeAdapter(PriceSnapshot | None)
_price_history_adapter: TypeAdapter[PriceHistory | None] = TypeAdapter(PriceHistory | None)
_decimal_adapter: TypeAdapter[Decimal | None] = TypeAdapter(Decimal | None)
_financial_summary_adapter: TypeAdapter[FinancialSummary | None] = TypeAdapter(
    FinancialSummary | None
)
_historical_valuation_list_adapter: TypeAdapter[list[HistoricalValuation]] = TypeAdapter(
    list[HistoricalValuation]
)
_cashflow_decomposition_adapter: TypeAdapter[CashflowDecomposition | None] = TypeAdapter(
    CashflowDecomposition | None
)
_dividend_info_adapter: TypeAdapter[DividendInfo | None] = TypeAdapter(DividendInfo | None)


class CacheEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cache_key: str
    cached_at: dt.datetime
    payload_json: str


def get_or_fetch[T](
    repo: CollectionStore[CacheEntry],
    cache_key: str,
    ttl_hours: int,
    now: dt.datetime,
    fetch_fn: Callable[[], T],
    type_adapter: TypeAdapter[T],
    log_label: str,
) -> T:
    """キャッシュヒット時はCloudWatch Logsへ`cache hit`、ミス/期限切れ時は
    `cache miss`を出力する(運用ハードニング4節)。DynamoDBの物理TTLは設定しない
    (キー空間が銘柄コード数で自然に有界なため、EdinetFilingCacheTable等の既存
    キャッシュテーブルと同じ方針)。鮮度判定(論理TTL)はcached_atとの比較のみで行う。
    """
    cached = repo.get(cache_key)
    if cached is not None:
        age_hours = (now - cached.cached_at).total_seconds() / 3600
        if age_hours <= ttl_hours:
            logger.info(
                "watchlist cache hit %s cache_key=%s age_hours=%.1f",
                log_label,
                cache_key,
                age_hours,
            )
            return type_adapter.validate_json(cached.payload_json)
        logger.info(
            "watchlist cache miss(expired) %s cache_key=%s age_hours=%.1f",
            log_label,
            cache_key,
            age_hours,
        )
    else:
        logger.info("watchlist cache miss(absent) %s cache_key=%s", log_label, cache_key)

    value = fetch_fn()
    payload_json = type_adapter.dump_json(value).decode("utf-8")
    repo.upsert(CacheEntry(cache_key=cache_key, cached_at=now, payload_json=payload_json))
    return value


@dataclass
class _CachingMarketDataProvider:
    inner: MarketDataProvider
    repo: CollectionStore[CacheEntry]
    ttl_hours: int
    now: dt.datetime

    def get_latest_price(self, stock_code: str) -> PriceSnapshot | None:
        return get_or_fetch(
            self.repo,
            f"latest_price:{stock_code}",
            self.ttl_hours,
            self.now,
            lambda: self.inner.get_latest_price(stock_code),
            _price_snapshot_adapter,
            "get_latest_price",
        )

    def get_price_history(
        self, stock_code: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        return get_or_fetch(
            self.repo,
            f"price_history:{stock_code}:{start.isoformat()}:{end.isoformat()}",
            self.ttl_hours,
            self.now,
            lambda: self.inner.get_price_history(stock_code, start, end),
            _price_history_adapter,
            "get_price_history",
        )

    def get_average_trading_value(self, stock_code: str, business_days: int) -> Decimal | None:
        return get_or_fetch(
            self.repo,
            f"avg_trading_value:{stock_code}:{business_days}",
            self.ttl_hours,
            self.now,
            lambda: self.inner.get_average_trading_value(stock_code, business_days),
            _decimal_adapter,
            "get_average_trading_value",
        )

    def get_benchmark_price_history(
        self, symbol: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        return get_or_fetch(
            self.repo,
            f"benchmark_history:{symbol}:{start.isoformat()}:{end.isoformat()}",
            self.ttl_hours,
            self.now,
            lambda: self.inner.get_benchmark_price_history(symbol, start, end),
            _price_history_adapter,
            "get_benchmark_price_history",
        )


@dataclass
class _CachingFinancialDataProvider:
    inner: FinancialDataProvider
    repo: CollectionStore[CacheEntry]
    ttl_hours: int
    now: dt.datetime

    def get_financial_summary(self, stock_code: str) -> FinancialSummary | None:
        return get_or_fetch(
            self.repo,
            f"financial_summary:{stock_code}",
            self.ttl_hours,
            self.now,
            lambda: self.inner.get_financial_summary(stock_code),
            _financial_summary_adapter,
            "get_financial_summary",
        )

    def get_historical_valuation(self, stock_code: str, years: int) -> list[HistoricalValuation]:
        return get_or_fetch(
            self.repo,
            f"historical_valuation:{stock_code}:{years}",
            self.ttl_hours,
            self.now,
            lambda: self.inner.get_historical_valuation(stock_code, years),
            _historical_valuation_list_adapter,
            "get_historical_valuation",
        )

    def get_cashflow_decomposition(self, stock_code: str) -> CashflowDecomposition | None:
        return get_or_fetch(
            self.repo,
            f"cashflow_decomposition:{stock_code}",
            self.ttl_hours,
            self.now,
            lambda: self.inner.get_cashflow_decomposition(stock_code),
            _cashflow_decomposition_adapter,
            "get_cashflow_decomposition",
        )


@dataclass
class _CachingDividendDataProvider:
    inner: DividendDataProvider
    repo: CollectionStore[CacheEntry]
    ttl_hours: int
    now: dt.datetime

    def get_dividend_info(self, stock_code: str) -> DividendInfo | None:
        return get_or_fetch(
            self.repo,
            f"dividend_info:{stock_code}",
            self.ttl_hours,
            self.now,
            lambda: self.inner.get_dividend_info(stock_code),
            _dividend_info_adapter,
            "get_dividend_info",
        )


def _cache_config(config: AppConfig) -> WatchlistDataCacheConfig:
    return config.watchlist_screening.data_cache


def build_cached_provider_bundle(
    base_bundle: ProviderBundle, config: AppConfig, now: dt.datetime
) -> ProviderBundle:
    """ウォッチリストの4つのLambdaハンドラのみで使う。`shareholder_benefit`
    (ローカル手動登録データ)・`disclosure`/`corporate_action`(既にEDINET専用
    キャッシュテーブルを持つ)はそのまま素通しする。
    """
    cache_config = _cache_config(config)
    price_repo: CollectionStore[CacheEntry] = build_collection_store(
        CacheEntry, _PRICE_CACHE_FILE, "cache_key"
    )
    financial_repo: CollectionStore[CacheEntry] = build_collection_store(
        CacheEntry, _FINANCIAL_CACHE_FILE, "cache_key"
    )
    return ProviderBundle(
        market_data=_CachingMarketDataProvider(
            base_bundle.market_data, price_repo, cache_config.price_cache_ttl_hours, now
        ),
        financial_data=_CachingFinancialDataProvider(
            base_bundle.financial_data,
            financial_repo,
            cache_config.financial_cache_ttl_hours,
            now,
        ),
        dividend_data=_CachingDividendDataProvider(
            base_bundle.dividend_data,
            financial_repo,
            cache_config.financial_cache_ttl_hours,
            now,
        ),
        shareholder_benefit=base_bundle.shareholder_benefit,
        disclosure=base_bundle.disclosure,
        corporate_action=base_bundle.corporate_action,
    )
