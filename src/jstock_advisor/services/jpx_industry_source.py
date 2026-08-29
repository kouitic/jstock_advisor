"""JPX上場銘柄一覧から canonical 業種・市場区分を引く読み取り専用ソース
(Issue #54 Phase B-1)。

`providers/candidate_universe/jpx_impl.py` は data_j.xls の
`33業種コード` / `33業種区分` / `市場・商品区分` を必須列として検証つきでパース済みだが、
`services/watchlist_candidate_collector.py` が銘柄コードのみを返すため**消費者がゼロ**だった。
本モジュールはその既存パース結果を、銘柄コード単位で引けるようにするだけであり、
新たなダウンロード・再パース規則・推測は一切行わない。

キャッシュの読み取り方(`CandidateUniverseCacheIO.read_current`)と、
「未ロード / 成功キャッシュ / 直近失敗(negative cache)」の3状態設計は
`services/watchlist_display_name.py` の `JpxStockNameSource` と同一方針である
(一時的なS3/パースエラー1回でコンテナ生存期間中ずっと解決できなくなるのを避ける)。

**本モジュールは観測(shadow)専用**であり、現時点でいかなる投資判断にも使われない。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from jstock_advisor.domain.classification.canonical_industry import JpxLookupStatus
from jstock_advisor.providers.candidate_universe.jpx_impl import parse_listed_issues_xls
from jstock_advisor.services.candidate_universe_downloader import CandidateUniverseCacheIO

logger = logging.getLogger(__name__)

_LISTED_ISSUES_SOURCE = "listed_issues"

# 直近の読み取り失敗を再試行するまでの間隔(秒)。`watchlist_display_name.py` の
# negative cache と同じ考え方(失敗を永久キャッシュしない)。
DEFAULT_NEGATIVE_CACHE_TTL_SECONDS = 60


@dataclass(frozen=True)
class JpxIndustryEntry:
    """1銘柄分のJPXメタデータ(canonical業種の入力)。"""

    industry_33_code: str | None
    industry_33_name: str | None
    market_segment: str | None


@dataclass(frozen=True)
class JpxIndustryLookup:
    """1銘柄の引き当て結果。

    **`None` 1つで「一覧に無い」と「一覧を読めない」を潰さない**(#59 の
    FAILURE ≠ SUCCESS + missing と同じ区別)。`entry` は `RESOLVED` のときのみ非None。
    """

    status: JpxLookupStatus
    entry: JpxIndustryEntry | None = None


def _default_clock() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _load_jpx_industry_map() -> dict[str, JpxIndustryEntry] | None:
    """JPXキャッシュから stock_code -> JpxIndustryEntry の全件マップを読み込む。

    市場区分による絞り込みは行わない(`target_market_segments=None`)。ETF・REITも
    含めて読み込み、区分の判定は呼び出し側(canonical_industry)へ委ねる。
    取得・パースいずれかが失敗した場合は例外を送出せずNoneを返す。
    """
    try:
        result = CandidateUniverseCacheIO().read_current(_LISTED_ISSUES_SOURCE)
        if result is None:
            logger.warning(
                "JPX industry map load failed: no cached listed_issues data "
                "(retryable=true, used_previous_success_cache=false, cached_count=0)"
            )
            return None
        data, _metadata = result
        parsed = parse_listed_issues_xls(data, target_market_segments=None)
    except Exception as exc:  # noqa: BLE001 - 観測用のため失敗しても判定を止めない
        logger.warning(
            "JPX industry map load failed error_type=%s error_summary=%s "
            "(retryable=true, used_previous_success_cache=false, cached_count=0)",
            type(exc).__name__,
            str(exc)[:200],
        )
        return None
    return {
        item.stock_code: JpxIndustryEntry(
            industry_33_code=item.industry_33_code,
            industry_33_name=item.industry_33_name,
            market_segment=item.market_segment,
        )
        for item in parsed.items
    }


class JpxIndustrySource:
    """JPX上場銘柄一覧キャッシュから銘柄単位のメタデータを引く読み取り専用ソース。

    3状態(未ロード / 成功キャッシュ / 直近失敗)を持ち、失敗を永久キャッシュしない。
    `get()` は「JPXで解決できなかった」ことを **None** で表し、推測値を返さない。
    """

    def __init__(
        self,
        negative_cache_ttl_seconds: int = DEFAULT_NEGATIVE_CACHE_TTL_SECONDS,
        clock: object | None = None,
    ) -> None:
        self._map: dict[str, JpxIndustryEntry] | None = None
        self._last_failed_at: dt.datetime | None = None
        self._negative_cache_ttl_seconds = negative_cache_ttl_seconds
        self._clock = clock or _default_clock

    def _now(self) -> dt.datetime:
        clock = self._clock
        return clock() if callable(clock) else _default_clock()

    def _ensure_loaded(self) -> dict[str, JpxIndustryEntry] | None:
        if self._map is not None:
            return self._map
        if self._last_failed_at is not None:
            elapsed = (self._now() - self._last_failed_at).total_seconds()
            if elapsed < self._negative_cache_ttl_seconds:
                return None
        loaded = _load_jpx_industry_map()
        if loaded is None:
            self._last_failed_at = self._now()
            return None
        self._map = loaded
        self._last_failed_at = None
        return self._map

    def lookup(self, stock_code: str) -> JpxIndustryLookup:
        """銘柄のJPXメタデータを引く。値の推測は行わない。

        - 一覧を読めて行がある → `RESOLVED`(entry付き)
        - **一覧は読めたが行が無い** → `NOT_FOUND`
        - **一覧そのものを読めない** → `SOURCE_UNAVAILABLE`

        いずれの場合も例外は送出しない(観測のためにBUY判定を止めない)。
        """
        entries = self._ensure_loaded()
        if entries is None:
            return JpxIndustryLookup(status=JpxLookupStatus.SOURCE_UNAVAILABLE)
        entry = entries.get(stock_code)
        if entry is None:
            return JpxIndustryLookup(status=JpxLookupStatus.NOT_FOUND)
        return JpxIndustryLookup(status=JpxLookupStatus.RESOLVED, entry=entry)


# プロセス内共有インスタンス。BUYはfan-out(Lambda 1実行 = 1銘柄)であり、
# 呼び出しごとにインスタンスを作ると warm container でも data_j.xls の
# ダウンロード済みキャッシュ読み取りと約4,000行のパースを毎回繰り返す。
# 読み取り専用・同日中は不変のデータであるため、プロセス内で使い回す。
_DEFAULT_SOURCE: JpxIndustrySource | None = None


def get_default_jpx_industry_source() -> JpxIndustrySource:
    """プロセス内で共有するJpxIndustrySourceを返す(テストでは注入して差し替える)。"""
    global _DEFAULT_SOURCE
    if _DEFAULT_SOURCE is None:
        _DEFAULT_SOURCE = JpxIndustrySource()
    return _DEFAULT_SOURCE


def reset_default_jpx_industry_source() -> None:
    """共有インスタンスを破棄する(テスト用。Production経路からは呼ばない)。"""
    global _DEFAULT_SOURCE
    _DEFAULT_SOURCE = None
