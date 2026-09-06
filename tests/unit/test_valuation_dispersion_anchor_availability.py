"""dispersion由来のanchor可用性(Issue #186)のテスト。

従来は dispersion > auto_buy_block(2.00) で valuation_confidence を LOW にし、
その結果 valuation_anchor が None になって買付価格が一切生成されなかった。
同じ「ばらつきが大きいので自動で買わない」判断は decide_buy_action() と
validate_buy_recommendation() が同じ 2.00 で既に持っており、重複していた。
重複の副作用として、方式値を上げると anchor が 有 -> 無 -> 有 と非単調に
反転していた。

本モジュールは次を固定する。

- LOW へ倒す閾値が anchor_block(既定 50.0)であること
- 2.00 の境界で anchor の有無が反転しないこと
- 2.00〜50.0 を単調に動かしても anchor の有無が反転しないこと
- 2.00 超で decide_buy_action() が MANUAL_REVIEW を返し続けること(L4 の回帰)
- validate_buy_recommendation() の LOW 判定(L5)が anchor_block 超でのみ到達すること
- SELL 側が本経路を通らないこと

`tests/unit/test_cross_pipeline_invariants.py` は Issue #109 の未 merge branch と
重なるため、「BUY 系が増えない」不変条件も本モジュールへ置く
(TARO-20260906-015 の DOMAIN_WIP_DECLARATION 参照)。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import ValuationDispersionThresholds
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import BuyAction, ConfidenceLevel
from jstock_advisor.domain.entities.valuation import FairValueMethodResult
from jstock_advisor.domain.signals.buy_consistency import validate_buy_recommendation
from jstock_advisor.domain.signals.buy_decision import decide_buy_action
from jstock_advisor.domain.valuation.valuation_confidence import (
    CODE_VALUATION_DISPERSION_TOO_HIGH,
    determine_valuation_confidence,
)
from jstock_advisor.domain.valuation.valuation_methods import (
    compute_valuation_anchor,
    determine_dispersion_band,
)

_BUY_DECISION_CONFIG = load_config().buy_decision

AUTO_BUY_BLOCK = 2.00
ANCHOR_BLOCK = 50.00
MEDIUM_MAX = 1.60


def _confidence(dispersion_ratio: float | None, methods_used_count: int = 4):
    return determine_valuation_confidence(
        methods_used_count=methods_used_count,
        dispersion_ratio=dispersion_ratio,
        dispersion_medium_max=MEDIUM_MAX,
        dispersion_anchor_block=ANCHOR_BLOCK,
        industry_model_applied=False,  # 本番は常に False
        uses_simplified_dcf=True,
        normalized_eps_confidence=None,
    )


def _range(values: list[Decimal]):
    """methods_used だけを持つ最小の FairValueRange 代替。"""
    methods = [
        FairValueMethodResult(
            method=f"m{i}", fair_value=v, confidence=ConfidenceLevel.MEDIUM, applicable=True
        )
        for i, v in enumerate(values)
    ]

    class _Stub:
        methods_used = methods

    return _Stub()


def _anchor_for(values: list[Decimal], dispersion_thresholds: ValuationDispersionThresholds):
    lo, hi = min(values), max(values)
    ratio = float(hi / lo)
    conf = _confidence(ratio)
    band = determine_dispersion_band(ratio, dispersion_thresholds)
    weights = {f"m{i}": 0.2 for i in range(len(values))}
    return compute_valuation_anchor(_range(values), conf.level, band, weights).anchor, conf, ratio


@pytest.fixture
def thresholds() -> ValuationDispersionThresholds:
    return ValuationDispersionThresholds(
        low_max=1.30, medium_max=MEDIUM_MAX, auto_buy_block=AUTO_BUY_BLOCK,
        anchor_block=ANCHOR_BLOCK,
    )


# --- config 契約 -----------------------------------------------------------


def test_config_requires_anchor_block_above_auto_buy_block() -> None:
    """順序検証: low_max < medium_max < auto_buy_block < anchor_block。"""
    with pytest.raises(ValueError, match="anchor_block"):
        ValuationDispersionThresholds(
            low_max=1.30, medium_max=1.60, auto_buy_block=2.00, anchor_block=1.90
        )


def test_config_rejects_anchor_block_equal_to_auto_buy_block() -> None:
    with pytest.raises(ValueError, match="anchor_block"):
        ValuationDispersionThresholds(
            low_max=1.30, medium_max=1.60, auto_buy_block=2.00, anchor_block=2.00
        )


# --- 2.00 境界 -------------------------------------------------------------


@pytest.mark.parametrize("ratio", [1.99, 2.00, 2.01, 2.5, 10.0, 49.99, 50.00])
def test_confidence_is_not_low_at_or_below_anchor_block(ratio: float) -> None:
    """2.00 の境界でも、anchor_block 以下なら LOW にならない。"""
    result = _confidence(ratio)
    assert result.level is not ConfidenceLevel.LOW
    assert result.blocking_reason is None


@pytest.mark.parametrize("ratio", [50.01, 60.0, 13891.72])
def test_confidence_is_low_above_anchor_block(ratio: float) -> None:
    result = _confidence(ratio)
    assert result.level is ConfidenceLevel.LOW
    assert result.blocking_reason is not None
    assert result.blocking_reason.code == CODE_VALUATION_DISPERSION_TOO_HIGH
    assert result.blocking_reason.threshold_value == ANCHOR_BLOCK


def test_anchor_exists_across_the_2_00_boundary(thresholds) -> None:
    """0.01 の差で anchor の有無が反転しない(#186 の中心)。"""
    below, _, ratio_below = _anchor_for(
        [Decimal("1000"), Decimal("1500"), Decimal("1800"), Decimal("1990")], thresholds
    )
    above, _, ratio_above = _anchor_for(
        [Decimal("1000"), Decimal("1500"), Decimal("1800"), Decimal("2010")], thresholds
    )
    assert ratio_below < AUTO_BUY_BLOCK < ratio_above
    assert below is not None
    assert above is not None


# --- 単調性 ---------------------------------------------------------------


def test_anchor_availability_is_monotonic_between_2_00_and_anchor_block(thresholds) -> None:
    """最大値を単調に上げても anchor の有無が反転しない。

    現行実装(修正前)は 2.00 を跨いだ時点で None になり、さらに上げると
    再び算出できるという非単調な挙動を示していた。
    """
    availability: list[bool] = []
    for top in range(1900, 50000, 1700):
        values = [Decimal("1000"), Decimal("1200"), Decimal("1400"), Decimal(str(top))]
        anchor, _, ratio = _anchor_for(values, thresholds)
        assert ratio <= ANCHOR_BLOCK
        availability.append(anchor is not None)
    assert all(availability), "anchor_block 以下では常に anchor が算出されること"


def test_anchor_disappears_only_above_anchor_block(thresholds) -> None:
    values_ok = [Decimal("1000"), Decimal("1200"), Decimal("1400"), Decimal("49000")]
    values_ng = [Decimal("1000"), Decimal("1200"), Decimal("1400"), Decimal("51000")]
    anchor_ok, _, ratio_ok = _anchor_for(values_ok, thresholds)
    anchor_ng, conf_ng, ratio_ng = _anchor_for(values_ng, thresholds)
    assert ratio_ok <= ANCHOR_BLOCK < ratio_ng
    assert anchor_ok is not None
    assert anchor_ng is None
    assert conf_ng.level is ConfidenceLevel.LOW


# --- L4 / L5 の回帰（安全機能を落としていないこと） -------------------------


def _levels(anchor: Decimal) -> BuyPriceLevels:
    return BuyPriceLevels(
        entry=PriceWithRationale(price=anchor * Decimal("0.80"), rationale="x"),
        standard=PriceWithRationale(price=anchor * Decimal("0.75"), rationale="x"),
        strong=PriceWithRationale(price=anchor * Decimal("0.70"), rationale="x"),
    )


def test_l4_still_forces_manual_review_above_auto_buy_block() -> None:
    """L4: dispersion > auto_buy_block なら BUY 系は MANUAL_REVIEW のまま。

    anchor が出るようになっても、自動購入の禁止は落ちない。
    """
    decision = decide_buy_action(
        current_price=Decimal("100"),
        buy_price_levels=_levels(Decimal("1000")),  # 現在値は積極買い価格を下回る
        company_quality_score=100.0,
        business_days_to_earnings=30,
        valuation_dispersion_ratio=2.5,
        buy_price_reliability=None,
        config=_BUY_DECISION_CONFIG,
    )
    assert decision.raw_action == BuyAction.STRONG_BUY
    assert decision.action == BuyAction.MANUAL_REVIEW
    assert any(r.code == "VALUATION_DISPERSION_TOO_HIGH" for r in decision.reasons)


def test_l4_does_not_fire_below_auto_buy_block() -> None:
    decision = decide_buy_action(
        current_price=Decimal("100"),
        buy_price_levels=_levels(Decimal("1000")),
        company_quality_score=100.0,
        business_days_to_earnings=30,
        valuation_dispersion_ratio=1.9,
        buy_price_reliability=None,
        config=_BUY_DECISION_CONFIG,
    )
    assert decision.action == BuyAction.STRONG_BUY


def test_l5_low_confidence_violation_reachable_only_above_anchor_block() -> None:
    """L5: confidence LOW + BUY 系 の違反は anchor_block 超でのみ到達しうる。

    2.00〜50.0 の帯では confidence が MEDIUM になるため発火しない。
    """
    medium = _confidence(2.5).level
    low = _confidence(60.0).level
    assert medium is ConfidenceLevel.MEDIUM
    assert low is ConfidenceLevel.LOW

    violations_medium = validate_buy_recommendation(
        action=BuyAction.STRONG_BUY,
        current_price=Decimal("100"),
        entry_price=Decimal("800"),
        standard_price=Decimal("750"),
        strong_price=Decimal("700"),
        confidence=medium,
        business_days_to_earnings=30,
        valuation_dispersion_ratio=2.5,
        config=_BUY_DECISION_CONFIG,
    )
    assert not any(v.code == "LOW_CONFIDENCE_BUY_ACTION" for v in violations_medium)

    violations_low = validate_buy_recommendation(
        action=BuyAction.STRONG_BUY,
        current_price=Decimal("100"),
        entry_price=Decimal("800"),
        standard_price=Decimal("750"),
        strong_price=Decimal("700"),
        confidence=low,
        business_days_to_earnings=30,
        valuation_dispersion_ratio=60.0,
        config=_BUY_DECISION_CONFIG,
    )
    assert any(v.code == "LOW_CONFIDENCE_BUY_ACTION" for v in violations_low)


# --- 影響範囲の不変条件 -----------------------------------------------------


def test_sell_side_does_not_use_this_path() -> None:
    """SELL・保有判断は build_fair_value_range を直接呼び、本経路を通らない。

    determine_valuation_confidence を import しているモジュールを実測で固定する
    (import 方向の回帰)。docstring 内の言及は対象にしない。
    """
    import ast
    import pathlib

    src = pathlib.Path("src/jstock_advisor")
    importers: list[str] = []
    for path in src.rglob("*.py"):
        if path.name == "valuation_confidence.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(
                alias.name == "determine_valuation_confidence" for alias in node.names
            ):
                importers.append(path.as_posix())
                break
    assert sorted(importers) == ["src/jstock_advisor/services/buy_signal_service.py"]
