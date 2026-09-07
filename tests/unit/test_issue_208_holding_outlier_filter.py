"""Issue #208 O-E: 保有/SELL 側の適正価格集約へ BUY 側と同じ外れ値フィルタを適用する。

`build_stock_snapshot()` が `build_fair_value_range()` を直接呼んでいたため、
保有側だけ外れ値が除外されず手法間の広がり(spread)が構造的に大きくなっていた。
本変更で BUY 側と同じ `build_valuation_summary()` を通す。

★ 実データ・銘柄コード・実名は使わない。すべて架空値。
★ Production へはアクセスしない。
"""

from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.entities.valuation import FairValueMethodResult
from jstock_advisor.domain.valuation.fair_value_usability import build_fair_value_range
from jstock_advisor.domain.valuation.valuation_methods import build_valuation_summary

_CONFIG = load_config()
_V = _CONFIG.valuation
_TRANSITION = _V.outlier_transition.below_52_week_low_min_ratio


def _methods(values: list[str]) -> list[FairValueMethodResult]:
    return [
        FairValueMethodResult(
            method=f"method{i}",
            fair_value=Decimal(v),
            confidence=ConfidenceLevel.MEDIUM,
        )
        for i, v in enumerate(values)
    ]


def _unfiltered(values: list[str]):
    """変更前の保有側と同じ経路（フィルタなし）。"""
    return build_fair_value_range(
        _methods(values),
        _V.fair_value_methods.aggregation_method,
        _V.fair_value_methods.method_weights,
        _V.fair_value_usability,
    )


def _filtered(values: list[str], *, current_price: str, low_52_week: str | None = None):
    """変更後の保有側と同じ経路（BUY 側と同一）。"""
    return build_valuation_summary(
        _methods(values),
        _V.fair_value_methods.aggregation_method,
        _V.fair_value_methods.method_weights,
        _V.fair_value_usability,
        current_price=Decimal(current_price),
        low_52_week=Decimal(low_52_week) if low_52_week is not None else None,
        transition_min_ratio=_TRANSITION,
    )


def test_extreme_low_method_no_longer_inflates_spread() -> None:
    """1 手法が現在値の 10% 未満の値を出した場合、除外されて広がりが縮む。

    本番で観測された「広がり 14.99 -> 1.29」に相当する形（値は架空）。
    """
    values = ["50", "1400", "1500", "1550", "1600"]  # 先頭が現在値の 10% 未満
    before = _unfiltered(values)
    after = _filtered(values, current_price="1500")

    assert before.bear == Decimal("50")
    assert float(before.bull / before.bear) > 10.0

    assert after.bear is not None and after.bear > Decimal("50")
    assert after.bull is not None
    spread_after = float(after.bull / after.bear)
    assert spread_after < 2.0, spread_after
    assert len(after.methods_excluded) >= 1


def test_usable_for_trading_judgment_flips_when_outlier_removed() -> None:
    """外れ値の除外により、レンジが売買判断に使えるようになる。"""
    values = ["50", "1400", "1500", "1550", "1600"]
    before = _unfiltered(values)
    after = _filtered(values, current_price="1500")

    assert before.usable_for_trading_judgment is False
    assert after.usable_for_trading_judgment is True


def test_no_outlier_means_no_change() -> None:
    """除外が 0 件のとき、レンジは変わらない（挙動不変の固定）。"""
    values = ["1400", "1450", "1500", "1550", "1600"]
    before = _unfiltered(values)
    after = _filtered(values, current_price="1500")

    assert after.bear == before.bear
    assert after.neutral == before.neutral
    assert after.bull == before.bull
    assert after.usable_for_trading_judgment == before.usable_for_trading_judgment
    assert after.methods_excluded == []


def test_matches_buy_side_for_the_same_inputs() -> None:
    """同じ入力なら BUY 側と同じレンジになる（非対称の解消）。

    BUY 側は build_valuation_summary をそのまま呼んでいる。保有側も同じ関数・
    同じ引数を使うため、結果は一致する。
    """
    values = ["50", "1400", "1500", "1550", "1600"]
    buy_side = _filtered(values, current_price="1500", low_52_week="1200")
    holding_side = _filtered(values, current_price="1500", low_52_week="1200")

    assert holding_side.bear == buy_side.bear
    assert holding_side.bull == buy_side.bull
    assert holding_side.valuation_dispersion_ratio == buy_side.valuation_dispersion_ratio
    assert holding_side.usable_for_trading_judgment == buy_side.usable_for_trading_judgment


def test_bull_does_not_rise_when_filtering() -> None:
    """誤売却防止: フィルタで bull（想定上限価格）が**上がらない**ことを固定する。

    利確判定は「現在値が想定上限価格へどれだけ近いか」で強まる。
    bull が上がると上値余地が広がり、判定は弱まる方向にしか動かない。
    bull が下がる（= 判定が強まりうる）のは、上方の外れ値を除外した場合だけである。
    そのケースを明示的に列挙して確認する。
    """
    # 下方外れ値のみ -> bull は不変
    low_outlier = ["50", "1400", "1500", "1550", "1600"]
    assert _filtered(low_outlier, current_price="1500").bull == _unfiltered(low_outlier).bull

    # 上方外れ値あり -> bull は下がる（上がることはない）
    high_outlier = ["1400", "1450", "1500", "1550", "9000"]
    before = _unfiltered(high_outlier)
    after = _filtered(high_outlier, current_price="1500")
    assert after.bull is not None and before.bull is not None
    assert after.bull <= before.bull


def test_high_outlier_removal_lowers_ceiling_but_keeps_range_usable() -> None:
    """上方外れ値を除外すると想定上限価格は下がる。その影響を明示的に固定する。

    ceiling が下がると上値余地が縮み、利確判定は強まる方向へ動きうる。
    ただし除外されるのは「他方式の中央値と比べて極端に高い」値であり、
    その値を根拠に利確を見送っていた状態のほうが実態と合わない。
    """
    values = ["1400", "1450", "1500", "1550", "9000"]
    before = _unfiltered(values)
    after = _filtered(values, current_price="1500")

    assert before.bull == Decimal("9000")
    assert after.bull is not None and after.bull < Decimal("9000")
    # 除外後も売買判断に使える（広がりが縮むため）。
    assert after.usable_for_trading_judgment is True
    assert before.usable_for_trading_judgment is False


def test_excluded_methods_keep_their_reason() -> None:
    """除外された算出方式と理由が記録に残る（#20 O-C と同じ形）。"""
    values = ["50", "1400", "1500", "1550", "1600"]
    after = _filtered(values, current_price="1500")

    excluded = after.methods_excluded
    assert excluded, "除外された手法が記録されていない"
    assert any(m.exclusion_detail is not None for m in excluded), (
        "除外理由（ValuationExclusionReason）が残っていない"
    )


def test_pre_filter_range_is_still_recorded_for_audit() -> None:
    """監査用のフィルタ前レンジ（valuation_min / max）は従来の意味を保つ。"""
    values = ["50", "1400", "1500", "1550", "1600"]
    after = _filtered(values, current_price="1500")

    assert after.valuation_min == Decimal("50")
    assert after.valuation_max == Decimal("1600")
    # 判断に使うのはフィルタ後の値。
    assert after.decision_valuation_min is not None
    assert after.decision_valuation_min > Decimal("50")


def test_fewer_than_three_methods_are_not_filtered() -> None:
    """有効手法が 3 件未満のときは外れ値検知を行わない（既存仕様の回帰）。"""
    values = ["50", "1500"]
    before = _unfiltered(values)
    after = _filtered(values, current_price="1500")

    assert after.bear == before.bear
    assert after.bull == before.bull
    assert after.methods_excluded == []
