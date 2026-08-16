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
ため、1回のバッチ全体で実質1〜数回しか実際のfetchが走らない(最大の削減効果)。

運用ハードニング第2弾1節: yfinance系Provider実装は内部で例外を広く捕捉して
`None`を返す実装になっているため(共有Provider実装は変更しない前提)、
「本当にデータが無い」場合と「一時的なProvider障害」を`get_or_fetch()`側で
区別できない。そのため`None`(および必須項目が大幅欠損した`FinancialSummary`等)
は`quality_status`をVALID以外に分類し、`negative_cache_ttl_minutes`
(既定15分)という短いTTLでのみキャッシュする。これにより一時的な取得失敗が
最長7日間(`financial_cache_ttl_hours`)キャッシュされ続けることを防ぐ。

横断整合性レビュー対応(2026-08、指摘2・High): 本モジュールのキャッシュTTL
(`price_cache_ttl_hours`=24時間・`financial_cache_ttl_hours`=168時間)は、
元々は週1回(毎週土曜)実行を前提に設計されており、平日毎日06:00実行への
変更(同年08月)後もTTL値自体は据え置いたままだった。時間TTLのみに依存する
設計では、前営業日06:00直後に作成した`get_latest_price`/
`get_average_trading_value`のキャッシュが翌営業日06:00直前でもまだ24時間
未満のため、前々営業日のデータを翌日の評価へ誤って再利用しうる欠陥があった。
これを解消するため、`get_latest_price`/`get_average_trading_value`の
キャッシュキーへ評価時点のJST暦日(`domain.jst.evaluation_date_jst`)を含め、
日をまたいだキャッシュの再利用を構造的に防止した(`get_price_history`/
`get_benchmark_price_history`は元々呼び出し元が指定する`start`/`end`が
キーに含まれるため、この問題の対象外)。`get_financial_summary`等の財務・
配当データ(168時間TTL)は決算発表頻度が低く、この日次境界による副作用が
比較的軽微なため、本対応では時間TTLのみを維持している(残存する鮮度課題は
GitHub Issue「ウォッチリストcache戦略の最適化検討」を参照)。
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
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store
from jstock_advisor.interfaces.dividend_data import DividendDataProvider
from jstock_advisor.interfaces.financial_data import FinancialDataProvider
from jstock_advisor.interfaces.market_data import MarketDataProvider
from jstock_advisor.interfaces.types import (
    CashflowDecomposition,
    DividendInfo,
    EarningsSurpriseRecord,
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
_earnings_surprise_record_list_adapter: TypeAdapter[list[EarningsSurpriseRecord]] = TypeAdapter(
    list[EarningsSurpriseRecord]
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


@dataclass
class CacheStats:
    """計画Part B-1: Before/After比較用のキャッシュhit/miss軽量集計。

    Workerの1 Lambda呼び出し(通常1銘柄、BatchSize=1)単位で共有する
    `build_cached_provider_bundle()`の呼び出し元が生成し、処理後にログへ
    出力する想定。永続化はしない(計測専用、DynamoDBスキーマは変更しない)。
    """

    hit_count: int = 0
    miss_count: int = 0

    def record_hit(self) -> None:
        self.hit_count += 1

    def record_miss(self) -> None:
        self.miss_count += 1


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


def _classify_earnings_surprise_record_list(
    value: list[EarningsSurpriseRecord],
) -> CacheQualityStatus:
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
    stats: CacheStats | None = None,
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
            if stats is not None:
                stats.record_hit()
            return type_adapter.validate_json(cached.payload_json)
        logger.info(
            "watchlist cache miss(expired) %s cache_key=%s age_hours=%.2f quality_status=%s",
            log_label,
            cache_key,
            age_hours,
            cached.quality_status,
        )
        if stats is not None:
            stats.record_miss()
    else:
        logger.info("watchlist cache miss(absent) %s cache_key=%s", log_label, cache_key)
        if stats is not None:
            stats.record_miss()

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
    stats: CacheStats | None = None

    def get_latest_price(self, stock_code: str) -> PriceSnapshot | None:
        # 横断整合性レビュー対応(2026-08、指摘2・High): 平日毎日06:00実行
        # への変更に伴い、キャッシュキーへJST暦日(evaluation_date_jst)を
        # 含めることで、日をまたいだ古いスナップショットの再利用を構造的に
        # 防止する(時間TTLのみに依存しない)。ttl_hours(24時間)は同一JST日
        # 内での取り違え防止・古いキーの自然な世代交代用に維持し、日付境界と
        # 時間TTLの二重の仕組みとする。
        return get_or_fetch(
            self.repo,
            f"latest_price:{stock_code}:{evaluation_date_jst(self.now).isoformat()}",
            self.ttl_hours,
            self.negative_ttl_minutes,
            self.now,
            lambda: self.inner.get_latest_price(stock_code),
            _price_snapshot_adapter,
            _classify_optional,
            "get_latest_price",
            stats=self.stats,
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
            stats=self.stats,
        )

    def get_average_trading_value(self, stock_code: str, business_days: int) -> Decimal | None:
        # 指摘2対応: get_latest_priceと同じ理由でJST暦日をキャッシュキーへ
        # 含める(直近N営業日の平均売買代金も「評価対象日を基準とした値」の
        # ため、日をまたいだ再利用は不適切)。
        return get_or_fetch(
            self.repo,
            f"avg_trading_value:{stock_code}:{business_days}:"
            f"{evaluation_date_jst(self.now).isoformat()}",
            self.ttl_hours,
            self.negative_ttl_minutes,
            self.now,
            lambda: self.inner.get_average_trading_value(stock_code, business_days),
            _decimal_adapter,
            _classify_optional,
            "get_average_trading_value",
            stats=self.stats,
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
            stats=self.stats,
        )


@dataclass
class _CachingFinancialDataProvider:
    inner: FinancialDataProvider
    repo: CollectionStore[CacheEntry]
    ttl_hours: int
    negative_ttl_minutes: int
    now: dt.datetime
    stats: CacheStats | None = None

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
            stats=self.stats,
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
            stats=self.stats,
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
            stats=self.stats,
        )

    def get_earnings_surprise_history(self, stock_code: str) -> list[EarningsSurpriseRecord]:
        # ウォッチリスト自動追加パイプラインはbuild_stock_snapshot()を使わない
        # ため実際には呼ばれないが、FinancialDataProviderプロトコルを満たすため
        # 実装する(モジュールdocstring参照)。
        return get_or_fetch(
            self.repo,
            f"earnings_surprise_history:{stock_code}",
            self.ttl_hours,
            self.negative_ttl_minutes,
            self.now,
            lambda: self.inner.get_earnings_surprise_history(stock_code),
            _earnings_surprise_record_list_adapter,
            _classify_earnings_surprise_record_list,
            "get_earnings_surprise_history",
            stats=self.stats,
        )


@dataclass
class _CachingDividendDataProvider:
    inner: DividendDataProvider
    repo: CollectionStore[CacheEntry]
    ttl_hours: int
    negative_ttl_minutes: int
    now: dt.datetime
    stats: CacheStats | None = None

    def get_dividend_info(
        self, stock_code: str, fiscal_year_end_month: int | None = None
    ) -> DividendInfo | None:
        # fiscal_year_end_monthは結果(決算期単位の集計)に直接影響するため、キャッシュキーへ
        # 含める(配当データクロスバリデーション根本修正: 引数を握りつぶさずキー衝突を防ぐ)。
        return get_or_fetch(
            self.repo,
            f"dividend_info:{stock_code}:{fiscal_year_end_month}",
            self.ttl_hours,
            self.negative_ttl_minutes,
            self.now,
            lambda: self.inner.get_dividend_info(
                stock_code, fiscal_year_end_month=fiscal_year_end_month
            ),
            _dividend_info_adapter,
            _classify_optional,
            "get_dividend_info",
            stats=self.stats,
        )


def _cache_config(config: AppConfig) -> WatchlistDataCacheConfig:
    return config.watchlist_screening.data_cache


def build_cached_provider_bundle(
    base_bundle: ProviderBundle,
    config: AppConfig,
    now: dt.datetime,
    stats: CacheStats | None = None,
) -> ProviderBundle:
    """ウォッチリストの4つのLambdaハンドラのみで使う。`shareholder_benefit`
    (ローカル手動登録データ)・`disclosure`/`corporate_action`(既にEDINET専用
    キャッシュテーブルを持つ)はそのまま素通しする。

    `stats`(計画Part B-1、Before/After比較用)を渡すと、このbundle経由の
    全get_or_fetch()呼び出しのhit/missを集計できる。省略時は計測を行わない
    (既存呼び出し元の挙動は変えない)。
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
            stats=stats,
        ),
        financial_data=_CachingFinancialDataProvider(
            base_bundle.financial_data,
            financial_repo,
            cache_config.financial_cache_ttl_hours,
            cache_config.negative_cache_ttl_minutes,
            now,
            stats=stats,
        ),
        dividend_data=_CachingDividendDataProvider(
            base_bundle.dividend_data,
            financial_repo,
            cache_config.financial_cache_ttl_hours,
            cache_config.negative_cache_ttl_minutes,
            now,
            stats=stats,
        ),
        shareholder_benefit=base_bundle.shareholder_benefit,
        disclosure=base_bundle.disclosure,
        corporate_action=base_bundle.corporate_action,
    )
