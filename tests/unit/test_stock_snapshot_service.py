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

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    EarningsDateStatus,
    HistoricalValuationEvaluationState,
    ValuationBasis,
)
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


# ===== JST基準の境界テスト(デプロイ前対応) =====


def test_jst_boundary_rejects_date_that_is_today_in_utc_but_past_in_jst() -> None:
    """UTC 2026-08-05T23:00 = JST 2026-08-06T08:00。素の.date()(UTC基準)なら
    8/5になり「当日」と誤判定するが、JST基準では8/6が評価日のため、8/5は
    過去日として正しくSTALE_PAST_DATEになる。
    """
    now_utc_23 = dt.datetime(2026, 8, 5, 23, 0, tzinfo=dt.UTC)
    providers = _providers_with_fixed_earnings_date(dt.date(2026, 8, 5))
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, now_utc_23, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.earnings_date_status == EarningsDateStatus.STALE_PAST_DATE
    assert snapshot.next_earnings_date is None


def test_jst_boundary_treats_date_as_today_when_jst_date_matches() -> None:
    """UTC 2026-08-04T23:00 = JST 2026-08-05T08:00。決算予定日が8/5の場合、
    JST基準では「当日」のためCONFIRMEDのままとなり、営業日数は0になる。
    """
    now_utc_23 = dt.datetime(2026, 8, 4, 23, 0, tzinfo=dt.UTC)
    providers = _providers_with_fixed_earnings_date(dt.date(2026, 8, 5))
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, now_utc_23, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.earnings_date_status == EarningsDateStatus.CONFIRMED
    assert snapshot.next_earnings_date == dt.date(2026, 8, 5)
    assert snapshot.business_days_to_earnings == 0


def test_naive_now_is_rejected() -> None:
    providers = _providers_with_fixed_earnings_date(None)
    naive_now = dt.datetime(2026, 8, 6)
    with pytest.raises(ValueError, match="timezone-aware"):
        build_stock_snapshot(providers, _STOCK_CODE, naive_now, _CFG)


def test_business_days_to_earnings_is_computed_once_on_snapshot() -> None:
    """next_earnings_dateが未来日の場合、business_days_to_earningsがJST暦日
    基準で1回だけ計算されsnapshotへ格納される(buy/sell/profit_takingが
    個別に再計算しないための一元化)。"""
    future = _NOW.date() + dt.timedelta(days=7)
    providers = _providers_with_fixed_earnings_date(future)
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.business_days_to_earnings is not None
    assert snapshot.business_days_to_earnings > 0


def test_business_days_to_earnings_is_none_when_earnings_date_stale() -> None:
    providers = _providers_with_fixed_earnings_date(dt.date(2026, 8, 5))
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.business_days_to_earnings is None


# ===== 判定精度向上機能Phase B: Historical Valuation Score配線確認 =====


def test_historical_valuation_score_is_computed_when_data_available() -> None:
    """モックプロバイダの過去バリュエーションデータ・trailing_eps/forecast_bpsが
    揃っていれば、-100〜+100の範囲でhistorical_valuation.scoreが計算されること
    (配線確認。スコアの計算ロジック自体の詳細はtest_historical_valuation_score.py
    で検証)。"""
    providers = build_mock_provider_bundle(_NOW)
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.historical_valuation.state == HistoricalValuationEvaluationState.EVALUATED
    assert snapshot.historical_valuation.score is not None
    assert -100.0 <= snapshot.historical_valuation.score <= 100.0


def test_historical_valuation_current_per_uses_trailing_basis() -> None:
    """current PERはforecast_eps(forward)ではなくtrailing_epsから算出され、
    TRAILING basisとして記録される(コードレビュー対応: basis整合性)。"""
    providers = build_mock_provider_bundle(_NOW)
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.historical_valuation.current_per_basis == ValuationBasis.TRAILING
    assert snapshot.historical_valuation.current_per == (
        snapshot.current_price / snapshot.financial.trailing_eps
    )


# ===== 判定精度向上機能Phase B第二弾: Timing Score配線確認 =====


def test_timing_score_is_computed_from_momentum_snapshot() -> None:
    """StockSnapshot.timingがmomentumを基に計算され、trend成分は常に利用可能
    なため状態がEVALUATED/NOT_EVALUATEDのいずれであってもtrend_componentが
    設定されること(配線確認。算出式自体の詳細はtest_timing_score.pyで検証)。"""
    providers = build_mock_provider_bundle(_NOW)
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.timing.trend_component is not None
    assert snapshot.timing.model_version == _CFG.timing_score.model_version
