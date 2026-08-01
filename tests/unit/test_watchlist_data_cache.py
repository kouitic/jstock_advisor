"""運用ハードニング第2弾1節: quality_status付きキャッシュ(None/DEGRADEDの
長期キャッシュ禁止)のテスト。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from jstock_advisor.infrastructure.collection_store import build_collection_store
from jstock_advisor.interfaces.types import DataSourceReference, FinancialSummary
from jstock_advisor.services.watchlist_data_cache import (
    CacheEntry,
    CacheQualityStatus,
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
