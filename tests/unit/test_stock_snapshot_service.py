"""build_stock_snapshot()の決算日検証ロジックのテスト(コードレビュー対応:
明治ホールディングス(2269)事例)。

データ提供元(yfinance等)の更新遅延により、評価日より過去の日付が「次回決算
予定日」として返ってくることがある。過去日をそのまま次回決算日として使わず、
buy/sell/profit_takingの3消費者すべてが一元的に検証された値のみを使うことを
build_stock_snapshot()の出力で直接確認する。
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import EarningsDateStatus
from jstock_advisor.interfaces.types import Disclosure
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

_CFG = load_config()
_NOW = dt.datetime(2026, 8, 6, tzinfo=dt.UTC)
_STOCK_CODE = "2914"


class _FixedEarningsDateDisclosureProvider:
    """次回決算予定日を固定値(または欠損)で返すフェイク。get_disclosuresは
    委譲元のモックProviderへそのまま委譲する(決算日検証以外は変更しない)。
    """

    def __init__(self, delegate: object, next_earnings_date: dt.date | None) -> None:
        self._delegate = delegate
        self._next_earnings_date = next_earnings_date

    def get_disclosures(self, stock_code: str, since: dt.date) -> list[Disclosure]:
        return self._delegate.get_disclosures(stock_code, since)  # type: ignore[attr-defined]

    def get_next_earnings_date(self, stock_code: str) -> dt.date | None:
        return self._next_earnings_date


def _providers_with_fixed_earnings_date(next_earnings_date: dt.date | None):
    base = build_mock_provider_bundle(_NOW)
    fake_disclosure = _FixedEarningsDateDisclosureProvider(base.disclosure, next_earnings_date)
    return dataclasses.replace(base, disclosure=fake_disclosure)


def test_past_earnings_date_is_rejected_as_stale() -> None:
    """明治HD事例の回帰: 過去の決算予定日をそのままnext_earnings_dateとして
    使わない。"""
    providers = _providers_with_fixed_earnings_date(dt.date(2026, 8, 5))
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.earnings_date_status == EarningsDateStatus.STALE_PAST_DATE
    assert snapshot.earnings_date_raw == dt.date(2026, 8, 5)
    assert snapshot.next_earnings_date is None


def test_today_earnings_date_is_confirmed() -> None:
    """予定日当日はCONFIRMEDとして扱う(過去日として除外しない)。"""
    providers = _providers_with_fixed_earnings_date(_NOW.date())
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.earnings_date_status == EarningsDateStatus.CONFIRMED
    assert snapshot.next_earnings_date == _NOW.date()


def test_future_earnings_date_is_confirmed() -> None:
    future = _NOW.date() + dt.timedelta(days=90)
    providers = _providers_with_fixed_earnings_date(future)
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.earnings_date_status == EarningsDateStatus.CONFIRMED
    assert snapshot.earnings_date_raw == future
    assert snapshot.next_earnings_date == future


def test_missing_earnings_date_is_unavailable() -> None:
    providers = _providers_with_fixed_earnings_date(None)
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.earnings_date_status == EarningsDateStatus.UNAVAILABLE
    assert snapshot.earnings_date_raw is None
    assert snapshot.next_earnings_date is None
