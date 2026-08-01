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

運用ハードニング第2弾1節: yfinance系Provider実装は内部で例外を広く捕捉して
`None`を返す実装になっているため(共有Provider実装は変更しない前提)、
「本当にデータが無い」場合と「一時的なProvider障害」を`get_or_fetch()`側で
区別できない。そのため`None`(および必須項目が大幅欠損した`FinancialSummary`等)
は`quality_status`をVALID以外に分類し、`negative_cache_ttl_minutes`
(既定15分)という短いTTLでのみキャッシュする。これにより一時的な取得失敗が
最長7日間(`financial_cache_ttl_hours`)キャッシュされ続けることを防ぐ。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

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

# 財務データのうち、これらが欠けているとscreening_data_provider.REQUIRED_FIELD_NAMES
# (必須項目)そのものが欠損することになるため、DEGRADED(=長期キャッシュ不可)とする。
_FINANCIAL_SUMMARY_REQUIRED_ATTRS = ("shares_outstanding", "operating_cashflow")

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


class CacheQualityStatus(StrEnum):
    """運用ハードニング第2弾1節: キャッシュされた値の信頼性区分。

    VALID以外はfinancial_cache_ttl_hours等の長期TTLを適用せず、
    negative_cache_ttl_minutesの短期TTLのみを使う。
    """

    VALID = "VALID"
    NEGATIVE = "NEGATIVE"  # Noneまたは空(取得できなかった)
    DEGRADED = "DEGRADED"  # 値は返ったが必須項目が大幅に欠損している


class CacheEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cache_key: str
    cached_at: dt.datetime
    payload_json: str
    quality_status: CacheQualityStatus


def _classify_optional(value: object) -> CacheQualityStatus:
    return CacheQualityStatus.NEGATIVE if value is None else CacheQualityStatus.VALID


def _classify_price_history(value: PriceHistory | None) -> CacheQualityStatus:
    if value is None or not value.bars:
        return CacheQualityStatus.NEGATIVE
    return CacheQualityStatus.VALID


def _classify_historical_valuation_list(value: list[HistoricalValuation]) -> CacheQualityStatus:
    return CacheQualityStatus.NEGATIVE if not value else CacheQualityStatus.VALID


def _classify_financial_summary(value: FinancialSummary | None) -> CacheQualityStatus:
    if value is None:
        return CacheQualityStatus.NEGATIVE
    if any(getattr(value, attr) is None for attr in _FINANCIAL_SUMMARY_REQUIRED_ATTRS):
        return CacheQualityStatus.DEGRADED
    return CacheQualityStatus.VALID


def get_or_fetch[T](
    repo: CollectionStore[CacheEntry],
    cache_key: str,
    ttl_hours: int,
    negative_ttl_minutes: int,
    now: dt.datetime,
    fetch_fn: Callable[[], T],
    type_adapter: TypeAdapter[T],
    classify_quality: Callable[[T], CacheQualityStatus],
    log_label: str,
) -> T:
    """キャッシュヒット時はCloudWatch Logsへ`cache hit`、ミス/期限切れ時は
    `cache miss`を出力する(運用ハードニング4節、quality_statusも出力する)。
    DynamoDBの物理TTLは設定しない(キー空間が銘柄コード数で自然に有界なため、
    EdinetFilingCacheTable等の既存キャッシュテーブルと同じ方針)。鮮度判定
    (論理TTL)はcached_atとの比較のみで行う。

    運用ハードニング第2弾1節: 保存済みエントリのquality_statusがVALIDなら
    ttl_hours、それ以外(NEGATIVE/DEGRADED)ならnegative_ttl_minutesを鮮度
    判定に使う。新規保存時も同様にfetch_fnの戻り値をclassify_qualityで
    分類し、VALID以外は長期キャッシュしない。
    """
    cached = repo.get(cache_key)
    if cached is not None:
        threshold_hours = (
            ttl_hours
            if cached.quality_status == CacheQualityStatus.VALID
            else negative_ttl_minutes / 60
        )
        age_hours = (now - cached.cached_at).total_seconds() / 3600
        if age_hours <= threshold_hours:
            logger.info(
                "watchlist cache hit %s cache_key=%s age_hours=%.2f quality_status=%s",
                log_label,
                cache_key,
                age_hours,
                cached.quality_status,
            )
            return type_adapter.validate_json(cached.payload_json)
        logger.info(
            "watchlist cache miss(expired) %s cache_key=%s age_hours=%.2f quality_status=%s",
            log_label,
            cache_key,
            age_hours,
            cached.quality_status,
        )
    else:
        logger.info("watchlist cache miss(absent) %s cache_key=%s", log_label, cache_key)

    value = fetch_fn()
    quality_status = classify_quality(value)
    payload_json = type_adapter.dump_json(value).decode("utf-8")
    repo.upsert(
        CacheEntry(
            cache_key=cache_key,
            cached_at=now,
            payload_json=payload_json,
            quality_status=quality_status,
        )
    )
    return value


@dataclass
class _CachingMarketDataProvider:
    inner: MarketDataProvider
    repo: CollectionStore[CacheEntry]
    ttl_hours: int
    negative_ttl_minutes: int
    now: dt.datetime

    def get_latest_price(self, stock_code: str) -> PriceSnapshot | None:
        return get_or_fetch(
            self.repo,
            f"latest_price:{stock_code}",
            self.ttl_hours,
            self.negative_ttl_minutes,
            self.now,
            lambda: self.inner.get_latest_price(stock_code),
            _price_snapshot_adapter,
            _classify_optional,
            "get_latest_price",
        )

    def get_price_history(
        self, stock_code: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        return get_or_fetch(
            self.repo,
            f"price_history:{stock_code}:{start.isoformat()}:{end.isoformat()}",
            self.ttl_hours,
            self.negative_ttl_minutes,
            self.now,
            lambda: self.inner.get_price_history(stock_code, start, end),
            _price_history_adapter,
            _classify_price_history,
            "get_price_history",
        )

    def get_average_trading_value(self, stock_code: str, business_days: int) -> Decimal | None:
        return get_or_fetch(
            self.repo,
            f"avg_trading_value:{stock_code}:{business_days}",
            self.ttl_hours,
            self.negative_ttl_minutes,
            self.now,
            lambda: self.inner.get_average_trading_value(stock_code, business_days),
            _decimal_adapter,
            _classify_optional,
            "get_average_trading_value",
        )

    def get_benchmark_price_history(
        self, symbol: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        return get_or_fetch(
            self.repo,
            f"benchmark_history:{symbol}:{start.isoformat()}:{end.isoformat()}",
            self.ttl_hours,
            self.negative_ttl_minutes,
            self.now,
            lambda: self.inner.get_benchmark_price_history(symbol, start, end),
            _price_history_adapter,
            _classify_price_history,
            "get_benchmark_price_history",
        )


@dataclass
class _CachingFinancialDataProvider:
    inner: FinancialDataProvider
    repo: CollectionStore[CacheEntry]
    ttl_hours: int
    negative_ttl_minutes: int
    now: dt.datetime

    def get_financial_summary(self, stock_code: str) -> FinancialSummary | None:
        return get_or_fetch(
            self.repo,
            f"financial_summary:{stock_code}",
            self.ttl_hours,
            self.negative_ttl_minutes,
            self.now,
            lambda: self.inner.get_financial_summary(stock_code),
            _financial_summary_adapter,
            _classify_financial_summary,
            "get_financial_summary",
        )

    def get_historical_valuation(self, stock_code: str, years: int) -> list[HistoricalValuation]:
        return get_or_fetch(
            self.repo,
            f"historical_valuation:{stock_code}:{years}",
            self.ttl_hours,
            self.negative_ttl_minutes,
            self.now,
            lambda: self.inner.get_historical_valuation(stock_code, years),
            _historical_valuation_list_adapter,
            _classify_historical_valuation_list,
            "get_historical_valuation",
        )

    def get_cashflow_decomposition(self, stock_code: str) -> CashflowDecomposition | None:
        return get_or_fetch(
            self.repo,
            f"cashflow_decomposition:{stock_code}",
            self.ttl_hours,
            self.negative_ttl_minutes,
            self.now,
            lambda: self.inner.get_cashflow_decomposition(stock_code),
            _cashflow_decomposition_adapter,
            _classify_optional,
            "get_cashflow_decomposition",
        )


@dataclass
class _CachingDividendDataProvider:
    inner: DividendDataProvider
    repo: CollectionStore[CacheEntry]
    ttl_hours: int
    negative_ttl_minutes: int
    now: dt.datetime

    def get_dividend_info(self, stock_code: str) -> DividendInfo | None:
        return get_or_fetch(
            self.repo,
            f"dividend_info:{stock_code}",
            self.ttl_hours,
            self.negative_ttl_minutes,
            self.now,
            lambda: self.inner.get_dividend_info(stock_code),
            _dividend_info_adapter,
            _classify_optional,
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
            base_bundle.market_data,
            price_repo,
            cache_config.price_cache_ttl_hours,
            cache_config.negative_cache_ttl_minutes,
            now,
        ),
        financial_data=_CachingFinancialDataProvider(
            base_bundle.financial_data,
            financial_repo,
            cache_config.financial_cache_ttl_hours,
            cache_config.negative_cache_ttl_minutes,
            now,
        ),
        dividend_data=_CachingDividendDataProvider(
            base_bundle.dividend_data,
            financial_repo,
            cache_config.financial_cache_ttl_hours,
            cache_config.negative_cache_ttl_minutes,
            now,
        ),
        shareholder_benefit=base_bundle.shareholder_benefit,
        disclosure=base_bundle.disclosure,
        corporate_action=base_bundle.corporate_action,
    )
