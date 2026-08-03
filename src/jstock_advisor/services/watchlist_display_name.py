"""ウォッチリスト銘柄の日本語表示名解決(LINE通知品質改善、2026-08)。

優先順位: 1.JPX上場銘柄一覧 2.手動オーバーライド(StockNameOverrideRepository)
3.既存ウォッチリスト登録名称 4.fallback_name/fallback_name_provider 5.stock_code。

各名称ソースは前段が未解決(None・空文字・空白のみ・例外)の場合にのみ呼ばれる
真の遅延評価であり、いずれも最大1回だけ呼ばれる。JPXで解決できた場合、
Override/Watchlist/fallback_name_providerは一切呼ばれない。いずれかのソースが
例外を送出しても個別に隔離し、次のソースへフォールスルーする(名称取得の失敗を
理由にウォッチリスト登録・通知全体を失敗させない)。

ウォッチリスト通知に限らず、保有銘柄・BUY・SELL等の他の通知からも再利用できる
汎用コンポーネントとして設計する(本ラウンドでは実際の適用はウォッチリスト
自動追加通知のみ)。CandidateUniverseProvider(スクリーニング対象銘柄集合の決定が
責務)には一切依存しない。市場区分フィルタ・staged_rollout・保有/既登録銘柄の
除外も行わない。
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

from jstock_advisor.infrastructure.local_repository.stock_name_override_repository import (
    StockNameOverrideRepository,
)
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
)
from jstock_advisor.providers.candidate_universe.jpx_impl import parse_listed_issues_xls
from jstock_advisor.services.candidate_universe_downloader import CandidateUniverseCacheIO

logger = logging.getLogger(__name__)

_LISTED_ISSUES_SOURCE = "listed_issues"


def _default_clock() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _load_jpx_stock_name_map() -> dict[str, str] | None:
    """JPXキャッシュ(S3/ローカル、CandidateUniverseCacheIO経由)から
    stock_code -> 日本語銘柄名 の全件マップを読み込む。市場区分による絞り込みは
    行わない(target_market_segments=None、全市場区分の名称を解決対象とするため)。
    取得・パースいずれかが失敗した場合は例外を送出せずNoneを返す(呼び出し側が
    negative cacheの起点として扱う)。
    """
    try:
        result = CandidateUniverseCacheIO().read_current(_LISTED_ISSUES_SOURCE)
        if result is None:
            logger.warning(
                "JPX stock name map load failed: no cached listed_issues data "
                "(retryable=true, used_previous_success_cache=false, cached_count=0)"
            )
            return None
        data, _metadata = result
        parsed = parse_listed_issues_xls(data, target_market_segments=None)
    except Exception as exc:  # noqa: BLE001 - 名称解決は失敗してもstock_codeへ安全にフォールバックする
        logger.warning(
            "JPX stock name map load failed error_type=%s error_summary=%s "
            "(retryable=true, used_previous_success_cache=false, cached_count=0)",
            type(exc).__name__,
            str(exc)[:200],
        )
        return None
    return {item.stock_code: item.stock_name for item in parsed.items if item.stock_name}


class JpxStockNameSource:
    """JPX上場銘柄一覧キャッシュから stock_code -> 日本語銘柄名 を読み取る、
    読み取り専用ソース。「未ロード/成功キャッシュ/直近失敗(negative cache)」の
    3状態を持ち、失敗を永久キャッシュしない(一時的なS3/パースエラー1回で
    コンテナ生存期間中ずっと名称解決できなくなることを避ける)。

    negative cache TTL・時刻取得(clock)はいずれもコンストラクタ引数で受け取り、
    既定値はStockDisplayNameConfig側のみが持つ(このクラス自体に既定値を
    重複定義しない)。
    """

    def __init__(
        self,
        negative_cache_ttl_seconds: int,
        clock: Callable[[], dt.datetime] = _default_clock,
    ) -> None:
        self._negative_cache_ttl_seconds = negative_cache_ttl_seconds
        self._clock = clock
        self._success_cache: dict[str, str] | None = None
        self._last_failure_at: dt.datetime | None = None

    def get(self, stock_code: str) -> str | None:
        cache = self._get_or_refresh_success_cache(self._clock())
        return cache.get(stock_code) if cache is not None else None

    def _get_or_refresh_success_cache(self, now: dt.datetime) -> dict[str, str] | None:
        if self._success_cache is not None:
            # 既に成功キャッシュがあれば維持する(再取得は試みない、コンテナ
            # 生存期間中のコスト最小化)。
            return self._success_cache
        if (
            self._last_failure_at is not None
            and (now - self._last_failure_at).total_seconds() < self._negative_cache_ttl_seconds
        ):
            return None  # 直近失敗のnegative cache期間中
        loaded = _load_jpx_stock_name_map()
        if loaded is not None:
            self._success_cache = loaded
            logger.info("JPX stock name map loaded count=%d", len(loaded))
            return self._success_cache
        self._last_failure_at = now
        return None


_shared_jpx_stock_name_source: JpxStockNameSource | None = None


def get_shared_jpx_stock_name_source(negative_cache_ttl_seconds: int) -> JpxStockNameSource:
    """Lambdaコンテナ生存期間中、成功キャッシュを再利用するためのモジュール
    レベル共有インスタンス(呼び出しごとに新規インスタンス化すると、
    コンテナ内での成功キャッシュ再利用の効果が失われるため)。テストでは
    JpxStockNameSourceを直接構築し、この関数は使わないこと。
    """
    global _shared_jpx_stock_name_source
    if _shared_jpx_stock_name_source is None:
        _shared_jpx_stock_name_source = JpxStockNameSource(negative_cache_ttl_seconds)
    return _shared_jpx_stock_name_source


def _normalize_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized if normalized else None


class StockDisplayNameResolver:
    """優先順位: 1.JPX 2.StockNameOverrideRepository 3.WatchlistRepository
    既存登録名称 4.fallback_name/fallback_name_provider 5.stock_code。
    各ソースは前段が未解決の場合にのみ呼ばれる真の遅延評価であり、最大1回だけ
    呼ばれる。1〜4いずれも空文字・空白のみを「未解決」として扱う。1〜4の
    いずれかが例外を送出した場合、WARNINGログを残して次のソースへ
    フォールスルーし、例外を`resolve()`の外へ再送出しない。全ソース失敗時
    (例外の有無を問わない)はstock_codeを返す直前に必ず専用の最終フォール
    バックWARNINGログを出力する。ウォッチリスト通知に限らず、保有銘柄・BUY・
    SELL等の他の通知からも再利用できる汎用コンポーネントとして命名する。
    """

    def __init__(
        self,
        jpx_name_source: JpxStockNameSource,
        override_repository: StockNameOverrideRepository,
        watchlist_repository: WatchlistRepository,
    ) -> None:
        self._jpx_name_source = jpx_name_source
        self._override_repository = override_repository
        self._watchlist_repository = watchlist_repository

    def _safe_get_name(
        self, source_name: str, stock_code: str, provider: Callable[[], str | None]
    ) -> tuple[str | None, bool]:
        """(解決した名称 or None, このソースで例外が発生したか)を返す。
        例外はここで握りつぶし、呼び出し元(resolve())へは再送出しない。
        """
        try:
            value = provider()
        except Exception as exc:  # noqa: BLE001 - 1ソースの失敗で名称解決全体を止めない
            logger.warning(
                "stock display name source failed stock_code=%s source_name=%s "
                "error_type=%s error_summary=%s continue_to_next_source=true",
                stock_code,
                source_name,
                type(exc).__name__,
                str(exc)[:200],
            )
            return None, True
        return _normalize_name(value), False

    def _watchlist_name(self, stock_code: str) -> str | None:
        item = self._watchlist_repository.get(stock_code)
        return item.stock_name if item is not None else None

    def resolve(
        self,
        stock_code: str,
        fallback_name: str | None = None,
        fallback_name_provider: Callable[[], str | None] | None = None,
    ) -> str:
        source_error_occurred = False
        fallback_provider_called = False

        for source_name, provider in (
            ("jpx", lambda: self._jpx_name_source.get(stock_code)),
            ("override", lambda: self._override_repository.get(stock_code)),
            ("watchlist", lambda: self._watchlist_name(stock_code)),
        ):
            candidate, errored = self._safe_get_name(source_name, stock_code, provider)
            source_error_occurred = source_error_occurred or errored
            if candidate is not None:
                return candidate

        candidate = _normalize_name(fallback_name)
        if candidate is not None:
            return candidate

        if fallback_name_provider is not None:
            fallback_provider_called = True
            candidate, errored = self._safe_get_name(
                "external_fallback", stock_code, fallback_name_provider
            )
            source_error_occurred = source_error_occurred or errored
            if candidate is not None:
                return candidate

        logger.warning(
            "stock display name unresolved; falling back to stock code "
            "stock_code=%s fallback_to_stock_code=true resolution_result=unresolved "
            "source_error_occurred=%s fallback_provider_called=%s",
            stock_code,
            source_error_occurred,
            fallback_provider_called,
        )
        return stock_code


def build_stock_display_name_resolver(negative_cache_ttl_seconds: int) -> StockDisplayNameResolver:
    """CLI・Lambda(finalizer)双方の呼び出し箇所で使う標準的な構築方法。
    JpxStockNameSourceはコンテナ/プロセス内で共有インスタンスを再利用する。
    """
    return StockDisplayNameResolver(
        jpx_name_source=get_shared_jpx_stock_name_source(negative_cache_ttl_seconds),
        override_repository=StockNameOverrideRepository(),
        watchlist_repository=WatchlistRepository(),
    )
