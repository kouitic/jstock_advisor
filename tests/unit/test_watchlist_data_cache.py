"""運用ハードニング第2弾1節: quality_status付きキャッシュ(None/DEGRADEDの
長期キャッシュ禁止)のテスト。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.infrastructure.collection_store import build_collection_store
from jstock_advisor.interfaces.types import (
    DataSourceReference,
    DividendInfo,
    FinancialSummary,
    PriceSnapshot,
)
from jstock_advisor.services.watchlist_data_cache import (
    CacheEntry,
    CacheQualityStatus,
    _CachingDividendDataProvider,
    _CachingMarketDataProvider,
    _classify_financial_summary,
    _classify_optional,
    get_or_fetch,
)

_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)
_TTL_HOURS = 168  # 7日
_NEGATIVE_TTL_MINUTES = 15


@pytest.fixture
def repo(tmp_path: Path):
    return build_collection_store(CacheEntry, "test_cache.json", "cache_key", tmp_path)


def _decimal_adapter():
    from decimal import Decimal

    from pydantic import TypeAdapter

    return TypeAdapter(Decimal | None)


def test_none_is_cached_as_negative_with_short_ttl(repo) -> None:
    adapter = _decimal_adapter()
    calls = {"n": 0}

    def fetch() -> None:
        calls["n"] += 1
        return None

    value = get_or_fetch(
        repo, "k1", _TTL_HOURS, _NEGATIVE_TTL_MINUTES, _NOW, fetch, adapter, _classify_optional,
        "test",
    )
    assert value is None
    assert calls["n"] == 1

    stored = repo.get("k1")
    assert stored is not None
    assert stored.quality_status == CacheQualityStatus.NEGATIVE


def test_none_does_not_survive_seven_days(repo) -> None:
    """Noneがfinancial_cache_ttl_hours(7日)相当の期間キャッシュされ続けないこと。"""
    adapter = _decimal_adapter()
    calls = {"n": 0}

    def fetch() -> None:
        calls["n"] += 1
        return None

    get_or_fetch(
        repo, "k1", _TTL_HOURS, _NEGATIVE_TTL_MINUTES, _NOW, fetch, adapter, _classify_optional,
        "test",
    )
    assert calls["n"] == 1

    # negative_cache_ttl_minutes(15分)は超過しているが、財務系の7日TTLには遠く
    # 満たない経過時間(1時間後)でも既にmiss扱いになり再取得されること。
    later = _NOW + dt.timedelta(hours=1)
    get_or_fetch(
        repo, "k1", _TTL_HOURS, _NEGATIVE_TTL_MINUTES, later, fetch, adapter, _classify_optional,
        "test",
    )
    assert calls["n"] == 2  # 再取得された(7日間キャッシュされ続けていない)


def test_transient_failure_then_success_is_cached_long_term(repo) -> None:
    """一時的な取得失敗(1回目None)後、次回呼び出しで再取得され、
    今度は正常値ならVALID・長期TTLでキャッシュされること。"""
    from decimal import Decimal

    adapter = _decimal_adapter()
    results = [None, Decimal("1000")]
    calls = {"n": 0}

    def fetch() -> Decimal | None:
        value = results[calls["n"]]
        calls["n"] += 1
        return value

    first = get_or_fetch(
        repo, "k1", _TTL_HOURS, _NEGATIVE_TTL_MINUTES, _NOW, fetch, adapter, _classify_optional,
        "test",
    )
    assert first is None
    assert calls["n"] == 1

    after_negative_ttl = _NOW + dt.timedelta(minutes=_NEGATIVE_TTL_MINUTES + 1)
    second = get_or_fetch(
        repo,
        "k1",
        _TTL_HOURS,
        _NEGATIVE_TTL_MINUTES,
        after_negative_ttl,
        fetch,
        adapter,
        _classify_optional,
        "test",
    )
    assert second == Decimal("1000")
    assert calls["n"] == 2

    stored = repo.get("k1")
    assert stored is not None
    assert stored.quality_status == CacheQualityStatus.VALID

    # VALIDなので7日TTL内なら再取得されない(1時間後でもキャッシュヒット)。
    much_later = after_negative_ttl + dt.timedelta(hours=1)
    third = get_or_fetch(
        repo,
        "k1",
        _TTL_HOURS,
        _NEGATIVE_TTL_MINUTES,
        much_later,
        fetch,
        adapter,
        _classify_optional,
        "test",
    )
    assert third == Decimal("1000")
    assert calls["n"] == 2  # 追加のfetchは発生していない


def _financial_summary(**overrides: object) -> FinancialSummary:
    from decimal import Decimal

    defaults: dict[str, object] = dict(
        stock_code="1234",
        fiscal_period_end=dt.date(2026, 3, 31),
        shares_outstanding=Decimal("1000000"),
        operating_cashflow=Decimal("500000"),
        source=DataSourceReference(provider="yfinance", fetched_at=_NOW),
    )
    defaults.update(overrides)
    return FinancialSummary(**defaults)  # type: ignore[arg-type]


def test_financial_summary_missing_required_fields_is_degraded_with_short_ttl(repo) -> None:
    from pydantic import TypeAdapter

    adapter = TypeAdapter(FinancialSummary | None)
    degraded = _financial_summary(shares_outstanding=None)

    get_or_fetch(
        repo,
        "fs1",
        _TTL_HOURS,
        _NEGATIVE_TTL_MINUTES,
        _NOW,
        lambda: degraded,
        adapter,
        _classify_financial_summary,
        "test",
    )
    stored = repo.get("fs1")
    assert stored is not None
    assert stored.quality_status == CacheQualityStatus.DEGRADED

    # DEGRADEDはnegative_cache_ttl_minutes基準のため、1時間後(7日には遠く満たない)
    # には既にmiss扱いとなり、再取得されること。
    calls = {"n": 0}

    def fetch_again() -> FinancialSummary:
        calls["n"] += 1
        return degraded

    get_or_fetch(
        repo,
        "fs1",
        _TTL_HOURS,
        _NEGATIVE_TTL_MINUTES,
        _NOW + dt.timedelta(hours=1),
        fetch_again,
        adapter,
        _classify_financial_summary,
        "test",
    )
    assert calls["n"] == 1


def test_valid_financial_summary_is_cached_long_term(repo) -> None:
    from pydantic import TypeAdapter

    adapter = TypeAdapter(FinancialSummary | None)
    valid = _financial_summary()
    calls = {"n": 0}

    def fetch() -> FinancialSummary:
        calls["n"] += 1
        return valid

    get_or_fetch(
        repo, "fs1", _TTL_HOURS, _NEGATIVE_TTL_MINUTES, _NOW, fetch, adapter,
        _classify_financial_summary, "test",
    )
    stored = repo.get("fs1")
    assert stored is not None
    assert stored.quality_status == CacheQualityStatus.VALID

    # VALIDなので1時間後でもまだキャッシュヒット(再取得されない)。
    get_or_fetch(
        repo,
        "fs1",
        _TTL_HOURS,
        _NEGATIVE_TTL_MINUTES,
        _NOW + dt.timedelta(hours=1),
        fetch,
        adapter,
        _classify_financial_summary,
        "test",
    )
    assert calls["n"] == 1


def test_cache_hit_log_includes_quality_status(repo, caplog: pytest.LogCaptureFixture) -> None:
    adapter = _decimal_adapter()
    from decimal import Decimal

    get_or_fetch(
        repo,
        "k1",
        _TTL_HOURS,
        _NEGATIVE_TTL_MINUTES,
        _NOW,
        lambda: Decimal("1"),
        adapter,
        _classify_optional,
        "test",
    )
    with caplog.at_level("INFO"):
        get_or_fetch(
            repo,
            "k1",
            _TTL_HOURS,
            _NEGATIVE_TTL_MINUTES,
            _NOW,
            lambda: Decimal("1"),
            adapter,
            _classify_optional,
            "test",
        )
    assert any("quality_status=VALID" in record.message for record in caplog.records)


class _RecordingDividendDataProvider:
    """呼び出しごとに異なるDividendInfoを返し、fiscal_year_end_month引数を記録する。"""

    def __init__(self) -> None:
        self.calls: list[int | None] = []

    def get_dividend_info(
        self, stock_code: str, fiscal_year_end_month: int | None = None
    ) -> DividendInfo | None:
        self.calls.append(fiscal_year_end_month)
        return DividendInfo(
            stock_code=stock_code,
            fiscal_year=str(fiscal_year_end_month),
            actual_annual_dividend_per_share=Decimal(str(len(self.calls))),
            source=DataSourceReference(provider="test", fetched_at=_NOW),
        )


def test_dividend_cache_key_does_not_collide_across_fiscal_year_end_month(repo) -> None:
    """fiscal_year_end_month=3と=12でキャッシュキーが衝突しないこと(結果が
    この引数に依存するため、引数を握りつぶさずキャッシュキーへ反映する)。"""
    inner = _RecordingDividendDataProvider()
    caching = _CachingDividendDataProvider(
        inner=inner,
        repo=repo,
        ttl_hours=_TTL_HOURS,
        negative_ttl_minutes=_NEGATIVE_TTL_MINUTES,
        now=_NOW,
    )

    result_3 = caching.get_dividend_info("2914", fiscal_year_end_month=3)
    result_12 = caching.get_dividend_info("2914", fiscal_year_end_month=12)

    assert inner.calls == [3, 12]  # 両方ともキャッシュミスで実際にfetchされる(キー衝突なし)
    assert result_3 is not None
    assert result_12 is not None
    assert result_3.fiscal_year == "3"
    assert result_12.fiscal_year == "12"

    # 同じ引数での再取得はキャッシュヒットし、innerを再度呼ばない
    result_3_again = caching.get_dividend_info("2914", fiscal_year_end_month=3)
    assert inner.calls == [3, 12]
    assert result_3_again is not None
    assert result_3_again.fiscal_year == "3"


# --- 横断整合性レビュー対応(2026-08、指摘2・High): 平日毎日06:00実行に伴う ---
# --- price/average_trading_valueキャッシュのJST日付境界テスト --------------

_JST = dt.timezone(dt.timedelta(hours=9))
# 前日06:00 JST(前営業日を想定)。実際の曜日は問わない(祝日判定は導入しない
# 要件のため、テストも曜日そのものには依存させない)。
_DAY1_0600_JST = dt.datetime(2026, 8, 3, 6, 0, tzinfo=_JST)
# ちょうど24時間後(=旧ロジックのprice_cache_ttl_hoursの境界と一致)の翌日06:00 JST。
_DAY2_0600_JST = _DAY1_0600_JST + dt.timedelta(hours=24)
# 同一営業日内の再実行(1時間後)を想定。
_DAY1_0700_JST = _DAY1_0600_JST + dt.timedelta(hours=1)


class _RecordingMarketDataProvider:
    """呼び出しごとに異なるPriceSnapshot/平均売買代金を返し、呼び出し回数を記録する。"""

    def __init__(self) -> None:
        self.latest_price_calls = 0
        self.avg_trading_value_calls = 0

    def get_latest_price(self, stock_code: str) -> PriceSnapshot | None:
        self.latest_price_calls += 1
        return PriceSnapshot(
            stock_code=stock_code,
            as_of_date=dt.date(2026, 8, 3),
            close_price=Decimal(str(1000 + self.latest_price_calls)),
            source=DataSourceReference(provider="test", fetched_at=_DAY1_0600_JST),
        )

    def get_price_history(self, stock_code, start, end):  # noqa: ANN001, ANN201
        raise NotImplementedError

    def get_average_trading_value(self, stock_code: str, business_days: int) -> Decimal | None:
        self.avg_trading_value_calls += 1
        return Decimal(str(500_000 * self.avg_trading_value_calls))

    def get_benchmark_price_history(self, symbol, start, end):  # noqa: ANN001, ANN201
        raise NotImplementedError


def _market_caching(repo, inner: _RecordingMarketDataProvider, now: dt.datetime):
    return _CachingMarketDataProvider(
        inner=inner,
        repo=repo,
        ttl_hours=24,
        negative_ttl_minutes=_NEGATIVE_TTL_MINUTES,
        now=now,
    )


def test_latest_price_cached_on_day1_is_not_reused_on_day2_even_within_24h_ttl(repo) -> None:
    """前営業日06:00に作成したlatest_priceキャッシュを、ちょうど24時間後
    (=旧TTLの境界内)の翌営業日06:00評価で誤って再利用しないこと(指摘2の
    主眼)。JST暦日がキャッシュキーに含まれるため、24時間TTL自体は満たして
    いても日が変われば必ずcache missとなり再取得されること。"""
    inner = _RecordingMarketDataProvider()

    day1 = _market_caching(repo, inner, _DAY1_0600_JST)
    first = day1.get_latest_price("1111")
    assert inner.latest_price_calls == 1
    assert first is not None
    assert first.close_price == Decimal("1001")

    day2 = _market_caching(repo, inner, _DAY2_0600_JST)
    second = day2.get_latest_price("1111")

    # 経過時間はちょうど24時間(旧ロジックではage_hours<=24でヒットしていた
    # 境界)だが、日付が変わっているため再取得されること。
    assert inner.latest_price_calls == 2
    assert second is not None
    assert second.close_price == Decimal("1002")


def test_latest_price_cache_is_reused_within_the_same_jst_day(repo) -> None:
    """同一JST営業日内の再実行(例: Reconcilerによる再試行)ではキャッシュを
    利用できること(日付境界を導入しても同日内の再利用は妨げない)。"""
    inner = _RecordingMarketDataProvider()

    first_run = _market_caching(repo, inner, _DAY1_0600_JST)
    first_run.get_latest_price("1111")
    assert inner.latest_price_calls == 1

    same_day_rerun = _market_caching(repo, inner, _DAY1_0700_JST)
    same_day_rerun.get_latest_price("1111")

    assert inner.latest_price_calls == 1  # 再取得されていない(キャッシュヒット)


def test_average_trading_value_cache_is_date_partitioned_across_days(repo) -> None:
    """get_average_trading_valueもget_latest_priceと同じ「評価対象日を基準と
    した値」であるため、同じJST日付境界ルールが効くこと(日が変われば
    再取得される)。"""
    inner = _RecordingMarketDataProvider()

    day1 = _market_caching(repo, inner, _DAY1_0600_JST)
    day1.get_average_trading_value("1111", business_days=20)
    assert inner.avg_trading_value_calls == 1

    day2 = _market_caching(repo, inner, _DAY2_0600_JST)
    day2.get_average_trading_value("1111", business_days=20)
    assert inner.avg_trading_value_calls == 2  # 日が変わったため再取得される


def test_average_trading_value_cache_is_reused_within_the_same_jst_day(repo) -> None:
    """get_average_trading_valueも同一JST営業日内の再実行ではキャッシュを
    利用できること。"""
    inner = _RecordingMarketDataProvider()

    first_run = _market_caching(repo, inner, _DAY1_0600_JST)
    first_run.get_average_trading_value("2222", business_days=20)
    assert inner.avg_trading_value_calls == 1

    same_day_rerun = _market_caching(repo, inner, _DAY1_0700_JST)
    same_day_rerun.get_average_trading_value("2222", business_days=20)

    assert inner.avg_trading_value_calls == 1  # 同日内は再取得されない


def test_latest_price_negative_cache_15min_ttl_still_applies_within_same_day(repo) -> None:
    """指摘2の日付境界導入後も、既存のnegative cache(15分)仕様を壊さない
    こと。Noneが返る場合は同一JST日内でも15分でmiss扱いになること。"""

    class _NoneReturningProvider:
        def __init__(self) -> None:
            self.calls = 0

        def get_latest_price(self, stock_code: str) -> PriceSnapshot | None:
            self.calls += 1
            return None

        def get_price_history(self, stock_code, start, end):  # noqa: ANN001, ANN201
            raise NotImplementedError

        def get_average_trading_value(self, stock_code, business_days):  # noqa: ANN001, ANN201
            raise NotImplementedError

        def get_benchmark_price_history(self, symbol, start, end):  # noqa: ANN001, ANN201
            raise NotImplementedError

    inner = _NoneReturningProvider()
    first = _market_caching(repo, inner, _DAY1_0600_JST)
    first.get_latest_price("9999")
    assert inner.calls == 1

    # 15分未満(同日・negative TTL内)は依然としてキャッシュヒットのまま。
    within_negative_ttl = _market_caching(
        repo, inner, _DAY1_0600_JST + dt.timedelta(minutes=10)
    )
    within_negative_ttl.get_latest_price("9999")
    assert inner.calls == 1

    # 15分超過(同日内)はmiss扱いで再取得される。
    after_negative_ttl = _market_caching(
        repo, inner, _DAY1_0600_JST + dt.timedelta(minutes=16)
    )
    after_negative_ttl.get_latest_price("9999")
    assert inner.calls == 2
