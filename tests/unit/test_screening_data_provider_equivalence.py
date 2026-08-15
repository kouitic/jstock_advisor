"""計画Part B-3: LightweightScreeningDataProviderとStockSnapshotScreeningDataProvider
の同値性テスト。

同一の下層データ(`build_mock_provider_bundle()`、決定論的な合成データ)を
両Providerへ与えたとき、`WatchlistScreeningInput`が(`next_earnings_date`を
除き)完全一致すること、および`WatchlistScreeningService.evaluate()`経由の
PASS/FAIL・matched_target_types・MonitoringScore・hard_exclusion_reasonsが
一致することを確認する。next_earnings_dateはLightweight側が判定に使わない
ため常にNone固定であり(計画Part B-2)、この差異のみ意図的なため比較から除外する。
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.screening_data_provider import (
    LightweightScreeningDataProvider,
    ScreeningDataStatus,
    StockSnapshotScreeningDataProvider,
    WatchlistScreeningInput,
)
from jstock_advisor.services.watchlist_screening_service import WatchlistScreeningService

_CFG = load_config()
_NOW = dt.datetime(2026, 8, 6, 7, 0, tzinfo=dt.UTC)

# mock_fixtures.py::MOCK_STOCKSに登録済みの銘柄コードのみ実データを持つ
# (プロファイルの異なる4銘柄、様々なStockType判定パターンを網羅する)。
_STOCK_CODES = ["2914", "9861", "8136", "8306"]

_COMPARED_FIELD_NAMES = tuple(
    f.name for f in dataclasses.fields(WatchlistScreeningInput) if f.name != "next_earnings_date"
)


@pytest.mark.parametrize("stock_code", _STOCK_CODES)
def test_screening_input_matches_between_providers_except_next_earnings_date(
    stock_code: str,
) -> None:
    providers = build_mock_provider_bundle(_NOW)
    heavy = StockSnapshotScreeningDataProvider(providers, _CFG)
    light = LightweightScreeningDataProvider(providers, _CFG)

    heavy_result = heavy.get_screening_input(stock_code, _NOW)
    light_result = light.get_screening_input(stock_code, _NOW)

    assert heavy_result.status == light_result.status == ScreeningDataStatus.OK
    assert heavy_result.input is not None
    assert light_result.input is not None

    mismatches = [
        field_name
        for field_name in _COMPARED_FIELD_NAMES
        if getattr(heavy_result.input, field_name) != getattr(light_result.input, field_name)
    ]
    assert mismatches == []

    # next_earnings_dateはLightweight側が意図的に常にNone(判定に使われないため)。
    assert light_result.input.next_earnings_date is None

    assert heavy_result.missing_fields == light_result.missing_fields


@pytest.mark.parametrize("stock_code", _STOCK_CODES)
def test_multi_style_monitoring_evaluation_matches_between_providers(stock_code: str) -> None:
    """PASS/FAIL・matched_target_types・MonitoringScore・hard_exclusion_reasonsが
    WatchlistScreeningService経由で一致することを確認する(計画Part B-3)。
    """
    providers = build_mock_provider_bundle(_NOW)
    heavy = StockSnapshotScreeningDataProvider(providers, _CFG)
    light = LightweightScreeningDataProvider(providers, _CFG)
    screening_service = WatchlistScreeningService(_CFG)

    heavy_input = heavy.get_screening_input(stock_code, _NOW).input
    light_input = light.get_screening_input(stock_code, _NOW).input
    assert heavy_input is not None
    assert light_input is not None

    heavy_eval = screening_service.evaluate(stock_code, heavy_input.stock_name, heavy_input, _NOW)
    light_eval = screening_service.evaluate(stock_code, light_input.stock_name, light_input, _NOW)

    assert heavy_eval.passed == light_eval.passed
    assert heavy_eval.total_score == light_eval.total_score
    assert heavy_eval.matched_criteria == light_eval.matched_criteria
    assert heavy_eval.exclusion_reasons == light_eval.exclusion_reasons
    assert len(heavy_eval.policy_results) == len(light_eval.policy_results)
    for heavy_policy, light_policy in zip(
        heavy_eval.policy_results, light_eval.policy_results, strict=True
    ):
        assert heavy_policy.passed == light_policy.passed
        assert heavy_policy.score == light_policy.score
        assert heavy_policy.hard_exclusion_reasons == light_policy.hard_exclusion_reasons


def test_lightweight_provider_calls_fewer_market_data_methods_than_stock_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """計画Part B-2の「取得しない」項目確認: get_price_history・
    get_benchmark_price_historyがLightweight側では一切呼ばれないこと
    (fakeプロバイダのcall countで確認、計画Part B性能テスト要件)。
    """
    providers = build_mock_provider_bundle(_NOW)
    call_counts = {"get_price_history": 0, "get_benchmark_price_history": 0}
    original_price_history = providers.market_data.get_price_history
    original_benchmark_history = providers.market_data.get_benchmark_price_history

    def _counting_price_history(*args: object, **kwargs: object) -> object:
        call_counts["get_price_history"] += 1
        return original_price_history(*args, **kwargs)  # type: ignore[operator]

    def _counting_benchmark_history(*args: object, **kwargs: object) -> object:
        call_counts["get_benchmark_price_history"] += 1
        return original_benchmark_history(*args, **kwargs)  # type: ignore[operator]

    monkeypatch.setattr(providers.market_data, "get_price_history", _counting_price_history)
    monkeypatch.setattr(
        providers.market_data, "get_benchmark_price_history", _counting_benchmark_history
    )

    light = LightweightScreeningDataProvider(providers, _CFG)
    result = light.get_screening_input("2914", _NOW)

    assert result.status == ScreeningDataStatus.OK
    assert call_counts["get_price_history"] == 0
    assert call_counts["get_benchmark_price_history"] == 0
