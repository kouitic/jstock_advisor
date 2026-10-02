"""Issue #405 PR-1: BUY(buy_signal_service)の `*_metrics` の hoist が、取り違え・引数落としなく
Recommendation へ載ることを固定する。

S-20 系列(Issue #384 / #403)は、`isolated_shadow_observation()` で隔離した `*_to_metrics` の
戻り値をローカル変数へ hoist し、`Recommendation(...)` の kwarg へ渡す。次の 2 つの誤りは、
例外も出ず、既存のテストでも検出できなかった(Issue #403 のレビューによる mutation 実測)。

    P6 / P9  2 つの変数名の取り違え(market ↔ sector / earnings_surprise ↔ earnings_trend)
    P7       `*_to_metrics` の呼び出しからの引数落とし(timing から `current_price` を落とす等)

方式は MANAGER 判断の選択肢 A1 + A3(共有部品 = tests/support/shadow_metrics_wiring.py):
  A1  サービスが消費した snapshot から `*_to_metrics` を同じ引数列で再計算(oracle)し、
      Recommendation の各 field と完全一致することを assert する。
  A3  oracle 一致が空振りしない前提(取り違えで差が出る・引数を落とすと差が出る)を
      fixture ごとに固定する。

## このテストが検査している範囲 / していない範囲(★ 正確に)

している    buy_signal_service.py の `*_metrics` 8 種(historical_valuation / timing /
            earnings_surprise / earnings_trend / entry_price_range / market / sector /
            environment)の hoist 8 件。mock provider の全 fixture(下記)で、Recommendation の
            field が oracle と一致すること。
していない  ・`isolated_shadow_*` を介さず手書きの try/except で隔離している箇所
            ・src/ の外からの呼び出し
            ・hoist 側で `**dict` の展開や、式(関数呼び出しの結果)を直接渡している形
            ・Recommendation → DecisionSnapshot の複写(decision_snapshot_builder)での取り違え
            ・`*_to_metrics` 自身の算出内容の正しさ(各 domain のテストの責務)
            ・buy_signal_service.py の facts 3 件(common_quality / style_attractiveness /
              canonical_industry)と、sell / 利確 / 保有判断の hoist(Issue #405 の別 PR)
            ・stock_snapshot_service.py 等、Result 型を返す隔離(Issue #405 の範囲外)

fixture は mock provider のみ(架空値)。時刻は固定。Production・AWS へは触れない。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.providers import mock_fixtures
from jstock_advisor.providers.corporate_action.mock_impl import MockCorporateActionProvider
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.dividend_data.mock_impl import MockDividendDataProvider
from jstock_advisor.providers.financial_data.mock_impl import MockFinancialDataProvider
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.shareholder_benefit.mock_impl import MockShareholderBenefitProvider
from jstock_advisor.services.buy_signal_service import BuySignalService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot
from tests.support.shadow_metrics_wiring import (
    MetricsSpec,
    argument_drop_blind_spots,
    common_metrics_specs,
    mismatched_fields,
    swap_blind_pairs,
)

_CONFIG = load_config()
_CALENDAR = BusinessCalendar.from_config(_CONFIG.holiday_calendar)
# 営業日の大引け後(水曜 16:00 JST)に固定する(wall clock を使わない)。
_NOW = dt.datetime(2026, 9, 2, 16, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
_MOCK_STOCK_CODES = tuple(mock_fixtures.MOCK_STOCKS)

_BUY_METRICS_FIELDS = (
    "historical_valuation_metrics",
    "timing_metrics",
    "earnings_surprise_metrics",
    "earnings_trend_metrics",
    "entry_price_range_metrics",
    "market_metrics",
    "sector_metrics",
    "environment_metrics",
)


def _providers() -> ProviderBundle:
    return ProviderBundle(
        market_data=MockMarketDataProvider(now=_NOW),
        financial_data=MockFinancialDataProvider(now=_NOW),
        dividend_data=MockDividendDataProvider(now=_NOW),
        shareholder_benefit=MockShareholderBenefitProvider(now=_NOW),
        disclosure=MockDisclosureProvider(now=_NOW),
        corporate_action=MockCorporateActionProvider(),
    )


def _snapshot_and_recommendation(stock_code: str) -> tuple[Any, Any]:
    providers = _providers()
    snapshot, error = build_stock_snapshot(
        providers, stock_code, _NOW, _CONFIG, business_calendar=_CALENDAR
    )
    assert snapshot is not None, error
    outcome = BuySignalService(
        providers=providers, config=_CONFIG, business_calendar=_CALENDAR
    ).analyze(stock_code, _NOW, RecommendationType.BUY, snapshot=snapshot)
    # Recommendation が作られない fixture では、以下の検査が空振りする(vacuous)。
    assert outcome.recommendation is not None, "この fixture では Recommendation が作られない"
    return snapshot, outcome.recommendation


@pytest.fixture(params=_MOCK_STOCK_CODES, ids=[f"mock-{i}" for i in range(len(_MOCK_STOCK_CODES))])
def wired(request: pytest.FixtureRequest) -> tuple[Any, Any, list[MetricsSpec]]:
    snapshot, recommendation = _snapshot_and_recommendation(request.param)
    return snapshot, recommendation, common_metrics_specs(snapshot, _CONFIG)


def test_the_spec_covers_exactly_the_eight_buy_metrics_fields(
    wired: tuple[Any, Any, list[MetricsSpec]],
) -> None:
    """検査対象の field が、buy_signal_service の hoist 8 件と一致する(黙って見落とさない)。"""
    _, _, specs = wired
    assert tuple(spec.field for spec in specs) == _BUY_METRICS_FIELDS


# --- A3: oracle 一致が空振りしないための前提 ---------------------------------------


def test_a3_swapping_any_two_metrics_changes_the_result(
    wired: tuple[Any, Any, list[MetricsSpec]],
) -> None:
    """★ 前提: 9 種(この PR では 8 種)の oracle が互いに異なる。等しい組があると、その 2 つを
    取り違える変異(P6 / P9)は、Recommendation の値が変わらず生き残る。
    """
    _, _, specs = wired
    assert swap_blind_pairs(specs) == []


def test_a3_dropping_any_argument_changes_the_result(
    wired: tuple[Any, Any, list[MetricsSpec]],
) -> None:
    """★ 前提: `*_to_metrics` の引数を 1 つ None にすると、oracle が変わる(または例外になる)。
    変わらない引数があると、その引数を落とす変異(P7)は生き残る(#403 の P7 がその形だった)。
    """
    _, _, specs = wired
    assert argument_drop_blind_spots(specs) == []


# --- A1: Recommendation の `*_metrics` が oracle と一致する ------------------------


def test_a1_every_buy_metrics_field_matches_the_oracle(
    wired: tuple[Any, Any, list[MetricsSpec]],
) -> None:
    """★ 本体: 8 つの `*_metrics` を、同じ引数列で再計算した値と完全一致で比較する。

    取り違え(別の変数を渡す)・引数落とし(値が静かに変わる)・kwarg の欠落(既定の空 dict)の
    いずれも、この 1 つの比較で落ちる。
    """
    _, recommendation, specs = wired
    assert mismatched_fields(recommendation, specs) == {}


# --- 検査そのものの反証(Recommendation 側へ変異を模して、検出できること) -------------


def test_the_check_detects_a_swap_between_market_and_sector(
    wired: tuple[Any, Any, list[MetricsSpec]],
) -> None:
    """P6 を模す: market_metrics と sector_metrics を入れ替えた Recommendation を、検出する。"""
    _, recommendation, specs = wired
    swapped = recommendation.model_copy(
        update={
            "market_metrics": recommendation.sector_metrics,
            "sector_metrics": recommendation.market_metrics,
        }
    )
    assert set(mismatched_fields(swapped, specs)) == {"market_metrics", "sector_metrics"}


def test_the_check_detects_a_swap_between_earnings_surprise_and_trend(
    wired: tuple[Any, Any, list[MetricsSpec]],
) -> None:
    """P9 を模す: earnings_surprise_metrics と earnings_trend_metrics の入れ替えを検出する。"""
    _, recommendation, specs = wired
    swapped = recommendation.model_copy(
        update={
            "earnings_surprise_metrics": recommendation.earnings_trend_metrics,
            "earnings_trend_metrics": recommendation.earnings_surprise_metrics,
        }
    )
    assert set(mismatched_fields(swapped, specs)) == {
        "earnings_surprise_metrics",
        "earnings_trend_metrics",
    }


def test_the_check_detects_timing_without_current_price(
    wired: tuple[Any, Any, list[MetricsSpec]],
) -> None:
    """P7 を模す: timing の `current_price` を落として算出した値が載った場合を検出する。"""
    _, recommendation, specs = wired
    timing = next(spec for spec in specs if spec.field == "timing_metrics")
    current_price_index = timing.argument_names.index("current_price")
    degraded = recommendation.model_copy(
        update={"timing_metrics": timing.oracle_with_argument_dropped(current_price_index)}
    )
    assert set(mismatched_fields(degraded, specs)) == {"timing_metrics"}


@pytest.mark.parametrize("field", _BUY_METRICS_FIELDS)
def test_the_check_detects_a_missing_hoist_for_every_field(
    wired: tuple[Any, Any, list[MetricsSpec]], field: str
) -> None:
    """kwarg の欠落を模す: その field が既定の空 dict のままの Recommendation を検出する。"""
    _, recommendation, specs = wired
    omitted = recommendation.model_copy(update={field: {}})
    assert set(mismatched_fields(omitted, specs)) == {field}


def test_the_a3_preconditions_themselves_detect_a_blind_fixture() -> None:
    """A3 の検査が空振りしないこと: 同じ値を返す 2 つの spec は swap_blind、引数を無視する
    関数は argument_drop_blind として、それぞれ検出される。
    """

    def constant(_ignored: object) -> dict[str, Any]:
        return {"value": 1}

    specs = [
        MetricsSpec("first_metrics", constant, ("a",)),
        MetricsSpec("second_metrics", constant, ("b",)),
    ]
    assert swap_blind_pairs(specs) == [("first_metrics", "second_metrics")]
    assert argument_drop_blind_spots(specs) == [
        ("first_metrics", "_ignored"),
        ("second_metrics", "_ignored"),
    ]
