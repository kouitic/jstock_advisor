"""52週安値フィルタの境界帯(Issue #179)のテスト。

従来 BELOW_52_WEEK_LOW は、除外閾値(直近52週安値 x 0.50)を 1 円でも下回った
算出値を、桁違いに低い値と同じように完全に切り捨てていた。閾値の直前と直後で
採否が反転するため、算出値をわずかに動かすだけで valuation_anchor が跳ぶ
(第 1 成分 = OUTLIER_MEMBERSHIP_DISCONTINUITY)。

本モジュールは次を固定する。

- 除外閾値(B)の境界で採否も anchor も跳ばないこと
- 境界帯(T_rel = 0.90 以上 1.0 未満)は除外せず他方式中央値へ線形補間すること
- T_rel 未満は従来どおり完全除外され、bad input protection が薄まらないこと
- u を掃引したとき採否が変わるのは T_rel の 1 点だけであること
- 採用値が必ず raw と他方式中央値の間に入り、u -> 1.0 で raw へ収束すること
- #186 の guard(anchor_block = 50.0)と併用しても anchor が消えないこと
- 他の 3 フィルタと DCF 上方乖離フィルタの発動が変わらないこと
- 旧レコード(transition_detail を持たない)が読めること
- SELL / 保有判断が本経路を通らないこと
- reliability が本修正だけで LOW へ倒れないこと

`tests/unit/test_cross_pipeline_invariants.py` は Issue #109 の未 merge branch と
重なるため、影響範囲の不変条件も本モジュールへ置く
(TARO-20260906-023 の DOMAIN_WIP_DECLARATION 参照)。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import OutlierTransition
from jstock_advisor.domain.entities.enums import BuyPriceReliability, ConfidenceLevel
from jstock_advisor.domain.entities.valuation import FairValueMethodResult
from jstock_advisor.domain.valuation.buy_price_reliability import (
    determine_buy_price_reliability,
)
from jstock_advisor.domain.valuation.margin_of_safety import MarginOfSafetyResult
from jstock_advisor.domain.valuation.valuation_confidence import (
    determine_valuation_confidence,
)
from jstock_advisor.domain.valuation.valuation_methods import (
    CODE_BELOW_52_WEEK_LOW,
    CODE_BORDERLINE_INTERPOLATED_TO_MEDIAN,
    apply_outlier_filters,
)

T_REL = 0.90
# 52週安値。除外閾値 = LOW_52W x 0.50 = 1,000 円。
LOW_52W = Decimal("2000")
THRESHOLD = Decimal("1000")
# 他方式(3件以上ないと外れ値検知そのものが走らない)。中央値は 1,500 円。
# 中央値 x 0.40 = 600 円 < 除外閾値 1,000 円 とし、EXTREME_LOW_RELATIVE_TO_MEDIAN が
# 先に発火しない配置にしている(52週安値フィルタ単体を観測するため)。
_OTHERS = {"per": Decimal("1400"), "pbr": Decimal("1500"), "dcf": Decimal("1600")}
_MEDIAN_OTHERS = Decimal("1500")


def _method(name: str, value: Decimal | None) -> FairValueMethodResult:
    return FairValueMethodResult(
        method=name, fair_value=value, confidence=ConfidenceLevel.MEDIUM, applicable=True
    )


def _results(target: Decimal) -> list[FairValueMethodResult]:
    return [_method("target_yield", target)] + [_method(k, v) for k, v in _OTHERS.items()]


def _filter(target: Decimal, *, transition: float | None = T_REL, current_price=None):
    return apply_outlier_filters(
        _results(target),
        current_price,
        LOW_52W,
        transition,
    )


def _target_of(result) -> FairValueMethodResult:
    return next(r for r in result.results if r.method == "target_yield")


# --- config 契約 -----------------------------------------------------------


def test_config_default_is_0_90() -> None:
    assert load_config().valuation.outlier_transition.below_52_week_low_min_ratio == T_REL


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.0, 1.5])
def test_config_rejects_ratio_outside_open_unit_interval(bad: float) -> None:
    """1.0 以上だと境界帯が空になり、0 以下だと「閾値の何倍か」が成立しない。"""
    with pytest.raises(ValueError, match="below_52_week_low_min_ratio"):
        OutlierTransition(below_52_week_low_min_ratio=bad)


# --- B(除外閾値)の境界で跳ばないこと ---------------------------------------


def test_value_at_or_above_threshold_is_never_excluded() -> None:
    """u >= 1.0 はそもそも除外対象にならない(現行どおり)。"""
    for value in (THRESHOLD, THRESHOLD + Decimal("1"), Decimal("1500")):
        result = _filter(value)
        target = _target_of(result)
        assert target.applicable is True
        assert target.fair_value == value
        assert target.transition_detail is None
        assert result.excluded_count == 0
        assert result.transition_count == 0


def test_just_below_threshold_is_interpolated_not_excluded() -> None:
    """B の 1 円下でも捨てず、採用値は raw とほぼ同じになる(段差が無い)。"""
    value = THRESHOLD - Decimal("1")  # u = 0.999
    result = _filter(value)
    target = _target_of(result)
    assert target.applicable is True
    assert result.excluded_count == 0
    assert result.transition_count == 1
    assert target.transition_detail is not None
    assert target.transition_detail.code == CODE_BORDERLINE_INTERPOLATED_TO_MEDIAN
    # raw を残す(#20 O-C の下方シナリオと同じ粒度で元の値が追える)
    assert target.transition_detail.actual_value == value
    assert target.transition_detail.reference_value == THRESHOLD
    # s = (0.999 - 0.90)/0.10 = 0.99 -> raw との差は他方式中央値との差の 1%
    assert target.fair_value is not None
    assert abs(target.fair_value - value) < Decimal("6")


def test_no_membership_flip_across_the_threshold() -> None:
    """B を跨いでも採否が反転せず、採用値が跳ばない。

    修正前は 999 円が除外 / 1,000 円が採用となり、1 円の差で anchor が動いていた。
    """
    below = _target_of(_filter(THRESHOLD - Decimal("1")))
    above = _target_of(_filter(THRESHOLD + Decimal("1")))
    assert below.applicable is above.applicable is True
    assert below.fair_value is not None
    assert above.fair_value is not None
    # 2 円しか違わない入力に対する採用値の差が 10 円未満(修正前は 999 円が
    # 除外され、他方式だけで anchor が決まっていたため段差が桁で違った)
    assert abs(below.fair_value - above.fair_value) < Decimal("10")


# --- T(境界帯の下限)の挙動 -------------------------------------------------


def test_at_transition_lower_bound_the_value_collapses_to_median() -> None:
    """u = T_rel ちょうどでは s = 0 となり、他方式中央値そのものを採用する。"""
    value = THRESHOLD * Decimal("0.90")  # 900 円
    target = _target_of(_filter(value))
    assert target.applicable is True
    assert target.fair_value == _MEDIAN_OTHERS


def test_below_transition_lower_bound_is_hard_rejected() -> None:
    """T_rel 未満は従来どおり完全除外(bad input protection を維持)。"""
    value = THRESHOLD * Decimal("0.899")
    result = _filter(value)
    target = _target_of(result)
    assert target.applicable is False
    assert target.fair_value is None
    assert result.excluded_count == 1
    assert result.transition_count == 0
    assert target.exclusion_detail is not None
    assert target.exclusion_detail.code == CODE_BELOW_52_WEEK_LOW


def test_interpolation_is_linear_in_u() -> None:
    """補間値 = 中央値 + (raw - 中央値) x (u - T)/(1 - T)。"""
    median = _MEDIAN_OTHERS
    for u, share in ((Decimal("0.90"), Decimal("0")), (Decimal("0.95"), Decimal("0.5"))):
        value = THRESHOLD * u
        expected = median + (value - median) * share
        assert _target_of(_filter(value)).fair_value == expected


# --- 単調性 ---------------------------------------------------------------


def test_membership_flips_exactly_once_and_only_at_the_transition_bound() -> None:
    """u を掃引したとき採否が変わるのは T_rel の 1 点だけ。

    修正前は B(u = 1.0)で採否が反転し、そこで anchor が跳んでいた。
    G-2 は不連続を B から T へ移す(消しはしない)。
    """
    accepted: list[bool] = []
    for step in range(880, 1060, 5):  # u = 0.880 .. 1.055
        u = Decimal(step) / Decimal("1000")
        accepted.append(_target_of(_filter(THRESHOLD * u)).fair_value is not None)
    flips = sum(1 for a, b in zip(accepted[:-1], accepted[1:], strict=True) if a != b)
    assert flips == 1
    assert accepted[0] is False and accepted[-1] is True


def test_adopted_value_stays_between_raw_and_median_and_converges_to_raw() -> None:
    """採用値は必ず raw と他方式中央値の間に入り、u -> 1.0 で raw へ収束する。

    ★ 採用値は u に対して単調ではない(設計上の既知の性質)。
      adopted = m + (uR - m) x (u - T)/(1 - T) であり、他方式中央値 m が除外閾値 R を
      十分上回る通常のケースでは du が負になる。すなわち **raw が低いほど採用値は
      高くなる**(捨てないかわりに中央値へ強く寄せるため)。
      これは「信用できない値ほど中央値へ寄せる」という G-2 の意図そのものであり、
      本 Issue が解消の対象とした「採否の反転による anchor の跳び」とは別の性質である。
      将来この向きを変える場合は設計判断が要るため、ここで性質を固定しておく。
    """
    previous: Decimal | None = None
    for step in range(900, 1000, 5):
        u = Decimal(step) / Decimal("1000")
        raw = THRESHOLD * u
        adopted = _target_of(_filter(raw)).fair_value
        assert adopted is not None
        assert min(raw, _MEDIAN_OTHERS) <= adopted <= max(raw, _MEDIAN_OTHERS)
        if previous is not None:
            # 既知の性質: 中央値が閾値を上回る配置では採用値は単調非増加
            assert adopted <= previous
        previous = adopted

    # u -> 1.0 で raw へ収束する(B での連続性)
    near_one = _target_of(_filter(THRESHOLD - Decimal("1"))).fair_value
    assert near_one is not None
    assert abs(near_one - (THRESHOLD - Decimal("1"))) < Decimal("6")


# --- 他フィルタの回帰 -------------------------------------------------------


def test_other_filters_are_not_interpolated() -> None:
    """境界帯の対象は BELOW_52_WEEK_LOW のみ。他の 3 フィルタは従来どおり除外。"""
    # 現在値 12,000 円の 10% 未満 -> EXTREME_LOW_RELATIVE_TO_CURRENT_PRICE
    result = _filter(Decimal("950"), current_price=Decimal("12000"))
    target = _target_of(result)
    assert target.applicable is False
    assert result.transition_count == 0
    assert target.exclusion_detail is not None
    assert target.exclusion_detail.code == "EXTREME_LOW_RELATIVE_TO_CURRENT_PRICE"


def test_extreme_low_relative_to_median_still_excluded() -> None:
    """他方式中央値の 40% 未満は従来どおり完全除外される(境界帯の対象外)。"""
    # 中央値 1,500 の 40% = 600 未満
    result = _filter(Decimal("500"))
    target = _target_of(result)
    assert target.applicable is False
    assert target.exclusion_detail is not None
    assert target.exclusion_detail.code == "EXTREME_LOW_RELATIVE_TO_MEDIAN"
    assert result.transition_count == 0


def test_transition_disabled_reproduces_previous_behaviour() -> None:
    """transition_min_ratio を渡さない呼び出しは従来と完全に同じ。"""
    value = THRESHOLD - Decimal("1")
    result = _filter(value, transition=None)
    target = _target_of(result)
    assert target.applicable is False
    assert result.excluded_count == 1
    assert result.transition_count == 0


def test_outlier_detection_still_skipped_below_three_methods() -> None:
    """有効方式が 3 件未満なら外れ値検知そのものを行わない(現行の前提を維持)。"""
    results = [_method("target_yield", Decimal("500")), _method("per", Decimal("3000"))]
    out = apply_outlier_filters(results, None, LOW_52W, T_REL)
    assert out.excluded_count == 0
    assert out.transition_count == 0
    assert all(r.applicable for r in out.results)


# --- #186 guard との併用 ----------------------------------------------------


def test_interpolation_never_reaches_the_186_anchor_block() -> None:
    """補間は dispersion を押し上げるが anchor_block(50.0)には届かない。

    #186 は dispersion > 50.0 で confidence を LOW にし anchor を消す。
    境界帯の補間値は必ず他方式中央値と raw の間に入るため、dispersion が
    補間前より悪化することはあっても、除外していた raw をそのまま採用した
    場合の値を超えない。
    """
    for step in range(900, 1000, 10):
        value = THRESHOLD * Decimal(step) / Decimal("1000")
        used = [r.fair_value for r in _filter(value).results if r.fair_value is not None]
        dispersion = float(max(used) / min(used))
        confidence = determine_valuation_confidence(
            methods_used_count=len(used),
            dispersion_ratio=dispersion,
            dispersion_medium_max=1.60,
            dispersion_anchor_block=50.0,
            industry_model_applied=False,
            uses_simplified_dcf=True,
            normalized_eps_confidence=None,
        )
        assert dispersion <= 50.0
        assert confidence.level is not ConfidenceLevel.LOW


# --- reliability（U3）------------------------------------------------------


def _margin() -> MarginOfSafetyResult:
    return MarginOfSafetyResult(
        entry_margin=Decimal("0.10"),
        standard_margin=Decimal("0.15"),
        strong_margin=Decimal("0.20"),
        allowed=True,
        entry_margin_before_cap=Decimal("0.10"),
    )


def _reliability(*, excluded: int, interpolated: int, dispersion: float | None = 1.2):
    return determine_buy_price_reliability(
        margin_result=_margin(),
        maximum_entry_margin=0.40,
        valuation_dispersion_ratio=dispersion,
        dispersion_medium_max=1.60,
        methods_used_count=4,
        data_quality_warning=False,
        earnings_date_status=None,
        excluded_outlier_count=excluded,
        borderline_interpolated_count=interpolated,
    )


def test_borderline_interpolation_is_reported_as_a_concern() -> None:
    result = _reliability(excluded=0, interpolated=1)
    assert "BORDERLINE_OUTLIER_INTERPOLATION" in result.concerns
    assert result.reliability is BuyPriceReliability.OK


def test_outlier_concerns_are_counted_as_one_for_the_low_gate() -> None:
    """除外 1 件 + 境界帯 1 件でも LOW にしない。

    修正前は「除外 1 件」で懸念 1 件だった。境界帯を別枠で数えると同じ状況が
    懸念 2 件になり、本 Issue の修正だけで reliability が LOW へ落ちてしまう。
    """
    both = _reliability(excluded=1, interpolated=1)
    assert both.concerns.count("VALUATION_OUTLIER_EXCLUDED") == 1
    assert both.concerns.count("BORDERLINE_OUTLIER_INTERPOLATION") == 1
    assert both.reliability is BuyPriceReliability.OK


def test_low_gate_still_fires_with_an_independent_second_concern() -> None:
    """外れ値以外の懸念が加われば従来どおり LOW になる(緩めていない)。"""
    result = _reliability(excluded=0, interpolated=1, dispersion=2.5)
    assert "HIGH_VALUATION_DISPERSION" in result.concerns
    assert result.reliability is BuyPriceReliability.LOW


# --- 影響範囲の不変条件 -----------------------------------------------------


def test_old_records_without_transition_detail_are_readable() -> None:
    """transition_detail を持たない既存レコードが例外なく読めること。"""
    restored = FairValueMethodResult.model_validate(
        {
            "method": "per",
            "fair_value": "1234",
            "confidence": "MEDIUM",
            "applicable": True,
        }
    )
    assert restored.transition_detail is None
    assert restored.fair_value == Decimal("1234")


def test_outlier_filter_path_importers_are_fixed() -> None:
    """外れ値フィルタ経路を使う module を固定する。

    apply_outlier_filters / build_valuation_summary を import しているモジュールを
    AST で実測して固定する(docstring 内の言及は対象にしない)。

    Issue #179 の時点では BUY 側(buy_signal_service)だけが本経路を通り、
    「SELL・保有判断は build_fair_value_range を直接呼び本経路を通らない」ことを
    不変条件として固定していた。Issue #208 の O-E でその判断は意図的に反転し、
    保有/SELL 側(stock_snapshot_service)も本経路を通るようになった
    (保有側だけ外れ値が除外されず、手法間の広がりが構造的に大きくなっていたため)。

    したがって本テストの役割は「保有側が通らないことの固定」ではなく、
    **どの module が本経路を使うかを明示的に固定すること**である。
    ここに挙げていない module が増えた場合は、その変更が意図的かどうかを
    必ず確認する(集約経路の非対称は #208 のように後から気付きにくい)。
    """
    import ast
    import pathlib

    src = pathlib.Path("src/jstock_advisor")
    importers: list[str] = []
    for path in src.rglob("*.py"):
        if path.name == "valuation_methods.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(
                alias.name in {"apply_outlier_filters", "build_valuation_summary"}
                for alias in node.names
            ):
                importers.append(path.as_posix())
                break
    assert sorted(importers) == [
        "src/jstock_advisor/services/buy_signal_service.py",
        "src/jstock_advisor/services/stock_snapshot_service.py",
    ]
