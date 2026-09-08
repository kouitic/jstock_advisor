"""valuation anchorの単調クランプ(Issue #260 / 是正案 O-1)。

## 何を固定するか

`compute_valuation_anchor()`はばらつき(dispersion band)に応じて集約器を
使い分ける。2026-07-31の設計文書は「バラつき率と信頼度に応じて**保守的に**
決定する」と定めているが、是正前の実装はband HIGHで`percentile_40`を
**単独で**採っており、band MEDIUMの`min(weighted_median, trimmed_mean)`を
上回ることがあった。つまり**ばらつきが悪化したほうが高い買付価格**になる。

ここで固定するのは「bandが悪化してanchorが上がることは無い」という性質
そのものである(#253 のM-3)。値そのものではなく性質を固定するのは、
同じ欠陥が別の閾値・別の集約器で再発しないようにするためである。

## ★ 本ファイルが固定していないもの(意図的)

    1.60 の境界連続性   O-1 は不連続を**消さない**。跳ぶ向きを下方向のみに
                        限定するだけである。完全な連続化は #187 の scope。
                        ここでは「上向きに跳ばない」という弱い形(M-1')だけを
                        固定する。
    1.30 の境界         confidence HIGH でのみ効くが、#207 のとおり本番では
                        HIGH に到達しない。O-1 は 1.30 に触れていない。
    _trimmed_mean の挙動 #263。本Issueでは**変えない**。実態(本番の方式数では
                        単純平均と同一)を回帰として固定するに留める。

## 実測(Production / 2026-09-02〜06)

再構成を検証できた2,100件のうち、band HIGHの765件中**437件(57.1%)**が
`percentile_40 > min(weighted_median, trimmed_mean)`だった。買付価格は
中央値で+2.82%(最大+15.79%)高く出ており、割安判定(`below_fair_value`)が
12件で反転していた。詳細は #260 のPhase Aコメント。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.entities.valuation import (
    FairValueMethodResult,
    FairValueRange,
)
from jstock_advisor.domain.valuation.valuation_methods import (
    _percentile,
    _trimmed_mean,
    _weighted_median,
    compute_valuation_anchor,
    determine_dispersion_band,
)

_CFG = load_config()
_DISPERSION = _CFG.buy_decision.valuation_dispersion

# 架空の適正価格。5方式ぶん(実在の銘柄値は使わない)。
_METHOD_NAMES = ("target_yield", "per", "pbr", "historical_range", "dcf")

# 集約器が互いに分かれる架空の分布(#253 / #260 の設計コメントと同じもの)。
_DISTRIBUTIONS = {
    "right_tail": ["1000", "1100", "1200", "1900", "2400"],
    "left_tail": ["600", "1150", "1200", "1210", "1220"],
    "near_uniform": ["1180", "1190", "1200", "1210", "1220"],
    "dense_low": ["900", "950", "1000", "1800", "2600"],
}

# 1.60(medium_max)を跨ぐ。1.59はMEDIUM、1.61はHIGH。
_RATIO_MEDIUM = 1.59
_RATIO_HIGH = 1.61
_RATIO_LOW = 1.20


def _method(name: str, value: str) -> FairValueMethodResult:
    return FairValueMethodResult(
        method=name,
        fair_value=Decimal(value),
        confidence=ConfidenceLevel.MEDIUM,
        applicable=True,
    )


def _range(values: list[str]) -> FairValueRange:
    return FairValueRange(
        bear=None,
        neutral=None,
        bull=None,
        overall_confidence=ConfidenceLevel.MEDIUM,
        methods_used=[_method(_METHOD_NAMES[i], v) for i, v in enumerate(values)],
        methods_excluded=[],
        usable_for_trading_judgment=True,
    )


def _anchor(values: list[str], ratio: float, confidence: ConfidenceLevel) -> Decimal | None:
    band = determine_dispersion_band(ratio, _DISPERSION)
    return compute_valuation_anchor(_range(values), confidence, band).anchor


def _parts(values: list[str]) -> tuple[Decimal, Decimal, Decimal]:
    """weighted_median / trimmed_mean / percentile_40 を素で計算する。"""
    decs = [Decimal(v) for v in values]
    wm = _weighted_median([(d, 1.0) for d in decs])
    assert wm is not None
    return wm, _trimmed_mean(decs), _percentile(decs, 40)


# --- (a) M-3: ばらつき悪化でanchorが上がらない -------------------------------


@pytest.mark.parametrize("dist", sorted(_DISTRIBUTIONS))
@pytest.mark.parametrize("confidence", [ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH])
def test_anchor_does_not_rise_when_dispersion_worsens(
    dist: str, confidence: ConfidenceLevel
) -> None:
    """Issue #260 本体。1.59 -> 1.61(ばらつき悪化)でanchorが上がらない。

    是正前は left_tail のみ 1076 -> 1180(+9.7%)と**上がって**いた。
    """
    values = _DISTRIBUTIONS[dist]
    before = _anchor(values, _RATIO_MEDIUM, confidence)
    after = _anchor(values, _RATIO_HIGH, confidence)
    assert before is not None
    assert after is not None
    assert after <= before, (
        f"{dist}/{confidence.value}: ばらつきが悪化したのにanchorが上がった "
        f"({before} -> {after})"
    )


@pytest.mark.parametrize("dist", sorted(_DISTRIBUTIONS))
def test_anchor_is_monotone_non_increasing_across_all_bands(dist: str) -> None:
    """band LOW -> MEDIUM -> HIGH でanchorが単調非増加であること。

    2026-07-31の設計意図「ばらつきに応じて保守的に決定する」の直接の表現。
    """
    values = _DISTRIBUTIONS[dist]
    low = _anchor(values, _RATIO_LOW, ConfidenceLevel.HIGH)
    medium = _anchor(values, _RATIO_MEDIUM, ConfidenceLevel.HIGH)
    high = _anchor(values, _RATIO_HIGH, ConfidenceLevel.HIGH)
    assert low is not None and medium is not None and high is not None
    assert medium <= low, f"{dist}: LOW -> MEDIUM で上がった ({low} -> {medium})"
    assert high <= medium, f"{dist}: MEDIUM -> HIGH で上がった ({medium} -> {high})"


# --- (b) band LOW / MEDIUM は是正前と同一 -----------------------------------


@pytest.mark.parametrize("dist", sorted(_DISTRIBUTIONS))
def test_band_low_still_returns_weighted_median(dist: str) -> None:
    """band LOW かつ confidence HIGH は weighted_median のまま(O-1で触れていない)。"""
    values = _DISTRIBUTIONS[dist]
    wm, _, _ = _parts(values)
    assert _anchor(values, _RATIO_LOW, ConfidenceLevel.HIGH) == wm


@pytest.mark.parametrize("dist", sorted(_DISTRIBUTIONS))
@pytest.mark.parametrize("confidence", [ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH])
def test_band_medium_still_returns_min_of_wm_and_trimmed_mean(
    dist: str, confidence: ConfidenceLevel
) -> None:
    """band MEDIUM は min(weighted_median, trimmed_mean) のまま(O-1で触れていない)。

    confidence MEDIUM のときは band LOW でも同じ分岐へ落ちる(#207)。
    """
    values = _DISTRIBUTIONS[dist]
    wm, tm, _ = _parts(values)
    assert _anchor(values, _RATIO_MEDIUM, confidence) == min(wm, tm)


@pytest.mark.parametrize("dist", sorted(_DISTRIBUTIONS))
def test_band_low_with_medium_confidence_is_unchanged(dist: str) -> None:
    """confidence MEDIUM では band LOW も min(wm, tm)。#207 の前提を壊さない。"""
    values = _DISTRIBUTIONS[dist]
    wm, tm, _ = _parts(values)
    assert _anchor(values, _RATIO_LOW, ConfidenceLevel.MEDIUM) == min(wm, tm)


# --- (c) M-1': band HIGH は上向きに跳ばない ---------------------------------


@pytest.mark.parametrize("dist", sorted(_DISTRIBUTIONS))
@pytest.mark.parametrize("confidence", [ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH])
def test_band_high_anchor_is_at_most_min_of_wm_and_trimmed_mean(
    dist: str, confidence: ConfidenceLevel
) -> None:
    """band HIGH のanchorは常に min(weighted_median, trimmed_mean) 以下。

    #187 の完全な連続性は固定しない(O-1では不連続は残る)。ここで固定するのは
    「跳ぶとしても下方向のみ」という弱い形。
    """
    values = _DISTRIBUTIONS[dist]
    wm, tm, _ = _parts(values)
    anchor = _anchor(values, _RATIO_HIGH, confidence)
    assert anchor is not None
    assert anchor <= min(wm, tm)


@pytest.mark.parametrize("dist", sorted(_DISTRIBUTIONS))
def test_band_high_equals_min_of_three_aggregators(dist: str) -> None:
    """band HIGH は3集約器のminそのものであること(実装の形を固定する)。"""
    values = _DISTRIBUTIONS[dist]
    wm, tm, p40 = _parts(values)
    assert _anchor(values, _RATIO_HIGH, ConfidenceLevel.MEDIUM) == min(wm, tm, p40)


# --- (d) 是正を戻すと落ちる negative check -----------------------------------


def test_left_tail_high_band_does_not_use_percentile_40_alone() -> None:
    """★ 是正を戻すと落ちるテスト。

    left_tail は `mean < percentile_40 <= weighted_median` となる分布で、
    是正前は band HIGH が percentile_40(=1180)を単独で返していた。
    是正後は min(wm, tm)(=1076)になる。

    このテストは `min(...)` を `_percentile(values, 40)` へ戻すと必ず落ちる。
    """
    values = _DISTRIBUTIONS["left_tail"]
    wm, tm, p40 = _parts(values)

    # 前提: この分布では percentile_40 のほうが高い(= 是正前は危険側だった)
    assert p40 > min(wm, tm), "分布の前提が崩れている(#260 の再現条件)"

    anchor = _anchor(values, _RATIO_HIGH, ConfidenceLevel.MEDIUM)
    assert anchor == min(wm, tm)
    assert anchor != p40


def test_dispersion_worsening_no_longer_raises_anchor_for_left_tail() -> None:
    """left_tail の 1.59 -> 1.61 が是正前は +9.7% だったことを回帰として固定する。"""
    values = _DISTRIBUTIONS["left_tail"]
    before = _anchor(values, _RATIO_MEDIUM, ConfidenceLevel.MEDIUM)
    after = _anchor(values, _RATIO_HIGH, ConfidenceLevel.MEDIUM)
    assert before is not None and after is not None
    assert after == before, "left_tail では min(wm, tm) が3者の最小のため変化しない"


# --- (e) #263 _trimmed_mean の実態を固定する(挙動は変えない) ------------------


@pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 9])
def test_trimmed_mean_does_not_trim_below_ten_methods(n: int) -> None:
    """Issue #263: trim_count = int(n * 0.1) は n >= 10 でのみ 1 以上になる。

    本番の方式数は最大5(2026-09-02〜06の4,142件で実測)であり、
    `trimmed_mean` は**常に単純平均**である。名前と実態が一致していないが、
    挙動の是正は #263 の scope。ここでは実態を回帰として固定するに留める。

    O-1 はこの性質に依存しない。minの項が増えるだけなので、
    trimmed_mean が単純平均であってもanchorは是正前以下にしかならない。
    """
    values = [Decimal("1"), *(Decimal(str(100 + i)) for i in range(n - 2)), Decimal("900")]
    assert len(values) == n
    plain = sum(values, Decimal("0")) / len(values)
    assert _trimmed_mean(values) == plain


def test_trimmed_mean_starts_trimming_at_ten_methods() -> None:
    """n = 10 で初めて trim が効くことを固定する(#263 の境界)。

    方式が10種類へ増えた日にanchorの算出規則が黙って変わる、という潜在リスクの
    在り処を示すためのテスト。
    """
    values = [Decimal("1"), *(Decimal(str(100 + i)) for i in range(8)), Decimal("900")]
    assert len(values) == 10
    plain = sum(values, Decimal("0")) / len(values)
    assert _trimmed_mean(values) != plain
