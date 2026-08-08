"""domain/signals/historical_valuation.pyのテスト(判定精度向上機能Phase B)。

銘柄自身の過去PER/PBR水準に対する現在値のランクベーススコア(-100〜+100)を
検証する。同業他社・市場平均との比較は行わない(自己過去比較のみ)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.signals.historical_valuation import (
    compute_historical_valuation_score,
)
from jstock_advisor.interfaces.types import DataSourceReference, HistoricalValuation

_CONFIG = load_config().historical_valuation
_SOURCE = DataSourceReference(provider="test", fetched_at=dt.datetime(2026, 8, 9, tzinfo=dt.UTC))


def _historical(
    pers: list[Decimal | None], pbrs: list[Decimal | None]
) -> list[HistoricalValuation]:
    assert len(pers) == len(pbrs)
    return [
        HistoricalValuation(
            stock_code="2914",
            date=dt.date(2020 + i, 3, 31),
            per=per,
            pbr=pbr,
            source=_SOURCE,
        )
        for i, (per, pbr) in enumerate(zip(pers, pbrs, strict=True))
    ]


def test_no_data_and_no_current_values_returns_none() -> None:
    score = compute_historical_valuation_score([], None, None, _CONFIG)
    assert score is None


def test_current_values_present_but_no_historical_data_returns_none() -> None:
    score = compute_historical_valuation_score([], Decimal("15"), Decimal("1.2"), _CONFIG)
    assert score is None


def test_per_only_available_uses_per_component_alone() -> None:
    """PBR側のcurrent値が無い場合、PERコンポーネントのみでスコアを算出する
    (0埋めせず、片方だけの重みで正規化する)。"""
    historical = _historical(
        pers=[Decimal("10"), Decimal("15"), Decimal("20")],
        pbrs=[Decimal("1.0"), Decimal("1.5"), Decimal("2.0")],
    )
    score = compute_historical_valuation_score(historical, Decimal("10"), None, _CONFIG)
    assert score is not None
    # current_per(10)は過去3件すべて以上(10自身含む) -> p=1.0 -> (1.0-0.5)*200=100
    assert score == 100.0


def test_pbr_only_available_uses_pbr_component_alone() -> None:
    historical = _historical(
        pers=[Decimal("10"), Decimal("15"), Decimal("20")],
        pbrs=[Decimal("1.0"), Decimal("1.5"), Decimal("2.0")],
    )
    score = compute_historical_valuation_score(historical, None, Decimal("2.0"), _CONFIG)
    assert score is not None
    # current_pbr(2.0)は過去3件のうち自分自身のみ以上 -> p=1/3 -> (1/3-0.5)*200 = -33.33...
    assert round(score, 2) == round((1 / 3 - 0.5) * 200, 2)


def test_both_available_combines_with_configured_weights() -> None:
    historical = _historical(
        pers=[Decimal("10"), Decimal("20"), Decimal("30"), Decimal("40")],
        pbrs=[Decimal("1.0"), Decimal("2.0"), Decimal("3.0"), Decimal("4.0")],
    )
    # PER=10(最安、過去4件すべて以上) -> p=1.0 -> +100
    # PBR=4.0(最高、過去4件のうち自分のみ以上) -> p=1/4 -> (0.25-0.5)*200=-50
    score = compute_historical_valuation_score(
        historical, Decimal("10"), Decimal("4.0"), _CONFIG
    )
    assert score is not None
    expected = 100.0 * _CONFIG.per_weight + (-50.0) * _CONFIG.pbr_weight
    expected /= _CONFIG.per_weight + _CONFIG.pbr_weight
    assert round(score, 6) == round(expected, 6)


def test_current_value_cheapest_in_history_scores_near_positive_100() -> None:
    historical = _historical(
        pers=[Decimal("20"), Decimal("25"), Decimal("30")], pbrs=[None, None, None]
    )
    score = compute_historical_valuation_score(historical, Decimal("5"), None, _CONFIG)
    assert score == 100.0


def test_current_value_most_expensive_in_history_scores_near_negative_100() -> None:
    historical = _historical(
        pers=[Decimal("10"), Decimal("15"), Decimal("20")], pbrs=[None, None, None]
    )
    score = compute_historical_valuation_score(historical, Decimal("100"), None, _CONFIG)
    # current(100)以上の過去値は0件 -> p=0.0 -> (0.0-0.5)*200=-100
    assert score == -100.0


def test_current_value_at_median_scores_near_zero() -> None:
    historical = _historical(
        pers=[Decimal("10"), Decimal("15"), Decimal("20")], pbrs=[None, None, None]
    )
    # current(15)以上の過去値は{15,20}の2件 -> p=2/3
    score = compute_historical_valuation_score(historical, Decimal("15"), None, _CONFIG)
    assert score is not None
    assert round(score, 2) == round((2 / 3 - 0.5) * 200, 2)


def test_insufficient_data_points_excludes_that_component() -> None:
    """過去データ点数がmin_data_points_required未満の指標は、その指標だけを
    スコア対象から除外する(0埋めしない)。既定min_data_points_required=2の
    ため、PER側が1件しかない場合はPERを除外し、PBR側のみで算出する。"""
    historical = _historical(
        pers=[Decimal("10")],
        pbrs=[Decimal("1.0")],
    )
    assert _CONFIG.min_data_points_required >= 2
    score_per_only_data = compute_historical_valuation_score(
        historical, Decimal("5"), None, _CONFIG
    )
    assert score_per_only_data is None  # PER側データが1件のみ(閾値未満)のため算出不可

    historical_two_points = _historical(
        pers=[Decimal("10"), Decimal("20")], pbrs=[Decimal("1.0"), None]
    )
    score = compute_historical_valuation_score(
        historical_two_points, Decimal("5"), Decimal("2.0"), _CONFIG
    )
    assert score is not None
    # PER側(2件、閾値以上)のみ採用され、PBR側(1件、閾値未満)は除外される。
    assert score == 100.0  # PER=5は過去{10,20}すべて以上 -> p=1.0 -> +100


def test_zero_or_negative_or_missing_historical_values_are_excluded() -> None:
    """過去データにPER/PBRが0以下・Noneの行が混在する場合、それらを除外してから
    計算する(fair_value.py::median_historical_per/pbrと同じ除外方針)。"""
    historical = _historical(
        pers=[Decimal("10"), Decimal("-5"), None, Decimal("30")],
        pbrs=[None, None, None, None],
    )
    # 有効な過去PERは{10, 30}の2件(閾値2件と一致)。
    score = compute_historical_valuation_score(historical, Decimal("10"), None, _CONFIG)
    assert score is not None
    # current(10)以上の有効な過去値は{10,30}の2件 -> p=1.0 -> +100
    assert score == 100.0
