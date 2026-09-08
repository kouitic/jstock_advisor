"""バリュエーションの不変条件テスト(Issue #253 / 打ち手 D-2 前半)。

## なぜ値ではなく性質を固定するか

バリュエーション(S-05)の欠陥は、個別の値ではなく**性質**として繰り返し
現れている(#179 / #186 / #187)。1件ずつ回帰テストを足しても、次に別の閾値で
同じことが起きる。ここでは満たすべき性質そのものを固定する。

## ★ 本ファイルが固定していない不変条件(意図的)

`#253` は3つの不変条件を挙げるが、**現行実装と矛盾するものは固定しない**
(受入条件5: テストを通すために実装を変えると、何が仕様で何が回帰かが混ざる)。
実測の詳細は #253 の設計コメント参照。

    1.60 の境界連続性   全4分布・両confidenceで anchor が跳ぶ(差 4〜124)
                        -> #187(status:設計済 / **未修正**)
    1.30 の境界連続性   confidence HIGH のとき跳ぶ(差 124)。MEDIUM では
                        LOW/MEDIUM がともに min(wm, tm) へ落ちるため連続に
                        見えるが、それは #207 のいう「あるのに効かない境界」で
                        あって連続だからではない。ここで固定すると #207 の修正が
                        回帰に見える -> **固定しない**
    ばらつき方向の単調性 percentile_40 > min(wm, tm) となる分布があり、
                        ばらつきが改善(1.61 -> 1.59)すると anchor が下がる
                        (1180 -> 1076)。3つの集約器が保守度の順に並んでいない
                        -> #253 の設計コメントで Issue 候補として報告済み

いずれも `#187` の完了後に追補する。

## fixture の方針

**架空値のみ**を使う(受入条件4)。実在の銘柄コード・保有数量・取得単価は
一切使わない。分布は「weighted_median と trimmed_mean と percentile_40 が
互いに分かれる」ことだけを条件に構成している。
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
from jstock_advisor.domain.valuation.valuation_confidence import (
    determine_valuation_confidence,
)
from jstock_advisor.domain.valuation.valuation_methods import (
    apply_outlier_filters,
    compute_valuation_anchor,
    determine_dispersion_band,
)

_CFG = load_config()
_DISPERSION = _CFG.buy_decision.valuation_dispersion
_TRANSITION_MIN_RATIO = _CFG.valuation.outlier_transition.below_52_week_low_min_ratio

# 架空の適正価格。5方式ぶん。
_METHOD_NAMES = ("target_yield", "per", "pbr", "historical_range", "dcf")


def _method(name: str, value: str | None) -> FairValueMethodResult:
    return FairValueMethodResult(
        method=name,
        fair_value=None if value is None else Decimal(value),
        confidence=ConfidenceLevel.MEDIUM,
        applicable=value is not None,
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


# 集約器が互いに分かれる架空の分布(#253 設計コメントの実測で使用したもの)。
_DISTRIBUTIONS = {
    "right_tail": ["1000", "1100", "1200", "1900", "2400"],
    "left_tail": ["600", "1150", "1200", "1210", "1220"],
    "near_uniform": ["1180", "1190", "1200", "1210", "1220"],
    "dense_low": ["900", "950", "1000", "1800", "2600"],
}


def _anchor(values: list[str], ratio: float | None, confidence: ConfidenceLevel):
    band = determine_dispersion_band(ratio, _DISPERSION)
    return compute_valuation_anchor(_range(values), confidence, band).anchor


# --- I-1: #186 の回帰 - anchor 生成の単調性 -----------------------------------


@pytest.mark.parametrize("dist", sorted(_DISTRIBUTIONS))
@pytest.mark.parametrize(
    "ratio",
    [1.99, 2.00, 2.01, 5.00, 20.00, 49.99, 50.00],
)
def test_anchor_is_generated_below_anchor_block(dist: str, ratio: float) -> None:
    """Issue #186: ばらつきが auto_buy_block(2.00)を超えても anchor は生成される。

    修正前は dispersion > 2.00 で confidence を LOW へ倒しており、その結果
    anchor が一切生成されなかった。「自動で買わない」は decide_buy_action()と
    validate_buy_recommendation()が同じ2.00で既に担っており、ここで LOW へ
    倒すのは同じ判断の重複だった。副作用として、方式値を上げると anchor が
    有 -> 無 -> 有 と非単調に反転していた。

    ★ #186 の修正(LOW 判定を anchor_block へ移す)を戻すと落ちる。
    """
    result = determine_valuation_confidence(
        methods_used_count=5,
        dispersion_ratio=ratio,
        dispersion_medium_max=_DISPERSION.medium_max,
        dispersion_anchor_block=_DISPERSION.anchor_block,
        industry_model_applied=False,
        uses_simplified_dcf=False,
        normalized_eps_confidence=ConfidenceLevel.MEDIUM,
    )
    assert result.level is not ConfidenceLevel.LOW, (
        f"dispersion={ratio} は anchor_block({_DISPERSION.anchor_block})以下であり、"
        "confidence を LOW へ倒してはならない(#186)"
    )
    assert _anchor(_DISTRIBUTIONS[dist], ratio, result.level) is not None


@pytest.mark.parametrize("ratio", [50.01, 60.00, 100.00])
def test_anchor_is_blocked_above_anchor_block(ratio: float) -> None:
    """anchor_block(50.0)を超えたときだけ anchor を生成しない(#186)。

    「比較対象として成立していない極端な入力」の保護であり、
    自動購入の禁止(2.00)とは目的が異なる。
    """
    result = determine_valuation_confidence(
        methods_used_count=5,
        dispersion_ratio=ratio,
        dispersion_medium_max=_DISPERSION.medium_max,
        dispersion_anchor_block=_DISPERSION.anchor_block,
        industry_model_applied=False,
        uses_simplified_dcf=False,
        normalized_eps_confidence=ConfidenceLevel.MEDIUM,
    )
    assert result.level is ConfidenceLevel.LOW
    assert _anchor(_DISTRIBUTIONS["right_tail"], ratio, result.level) is None


def test_anchor_presence_does_not_flip_back_and_forth() -> None:
    """★ #186 の本質: ばらつきを単調に増やしたとき、anchor の有無が
    有 -> 無 -> 有 と反転しない。

    「値ではなく性質」を固定する。anchor の有無は dispersion に対して
    **一度だけ**切り替わる(anchor_block を跨ぐ 1 点のみ)。
    """
    ratios = [0.5, 1.0, 1.29, 1.31, 1.59, 1.61, 1.99, 2.01, 10.0, 49.99, 50.01, 80.0]
    presence: list[bool] = []
    for ratio in ratios:
        result = determine_valuation_confidence(
            methods_used_count=5,
            dispersion_ratio=ratio,
            dispersion_medium_max=_DISPERSION.medium_max,
            dispersion_anchor_block=_DISPERSION.anchor_block,
            industry_model_applied=False,
            uses_simplified_dcf=False,
            normalized_eps_confidence=ConfidenceLevel.MEDIUM,
        )
        presence.append(_anchor(_DISTRIBUTIONS["right_tail"], ratio, result.level) is not None)

    # 隣接ペアを比べるため、意図的に長さが1つ違う zip を使う(strict は付けない)。
    transitions = sum(1 for a, b in zip(presence, presence[1:], strict=False) if a != b)
    assert transitions == 1, (
        f"anchor の有無が {transitions} 回切り替わった(期待は 1 回)。"
        f"presence={presence} ratios={ratios}"
    )
    assert presence[0] is True and presence[-1] is False


# --- I-2a: #179 の回帰 - 0.50 境界の連続性 ------------------------------------


def _filtered_value(raw: str, low_52_week: str, transition: float | None) -> Decimal | None:
    """外れ値フィルタを通した後の target_yield の適正価格を返す。"""
    # 他方式は 52週安値フィルタに掛からない水準に置く(架空値)。
    methods = [
        _method("target_yield", raw),
        _method("per", "1000"),
        _method("pbr", "1050"),
        _method("historical_range", "1100"),
        _method("dcf", "1150"),
    ]
    result = apply_outlier_filters(
        methods,
        current_price=None,
        low_52_week=Decimal(low_52_week),
        transition_min_ratio=transition,
    )
    target = next(r for r in result.results if r.method == "target_yield")
    return target.fair_value


def test_below_52_week_low_threshold_is_continuous() -> None:
    """Issue #179: 除外閾値(52週安値 x 0.50)の**境界で値が跳ばない**。

    u = 算出値 ÷ 除外閾値 が 1.0 へ近づくとき、補間の採用比率
    s = (u - T) / (1 - T) が 1 へ近づき、補間値が算出値そのものへ収束する。

    ★ #179 の修正(境界帯)を戻す(transition_min_ratio=None)と、
      閾値の直前で fair_value が None になり不連続になる。
    """
    low_52_week = "2000"  # 除外閾値 = 1000
    just_above = _filtered_value("1000", low_52_week, _TRANSITION_MIN_RATIO)
    just_below = _filtered_value("999", low_52_week, _TRANSITION_MIN_RATIO)

    assert just_above is not None
    assert just_below is not None
    # 閾値の 0.1% 下でしかないので、補間後の差も同程度に収まる。
    assert abs(just_above - just_below) < Decimal("5"), (
        f"閾値の直前直後で {just_above} -> {just_below} と跳んでいる(#179)"
    )


def test_below_52_week_low_threshold_is_discontinuous_without_transition_band() -> None:
    """★ 対比: 境界帯を渡さないと、同じ入力が閾値の直前で完全に除外される。

    このテストは #179 の修正が「効いていること」を裏側から固定する。
    """
    low_52_week = "2000"
    assert _filtered_value("1000", low_52_week, None) is not None
    assert _filtered_value("999", low_52_week, None) is None


# --- I-2b: #179 の設計の明示 - 段差は 0.90 へ移した ---------------------------


def test_transition_band_lower_edge_keeps_a_step_by_design() -> None:
    """★ 段差は消えたのではなく u = transition_min_ratio(0.90)へ**移した**。

    #179 の docstring が明記するとおり、この点の段差は帯を狭くするほど
    小さくなるが**消えはしない**(順位ベースの集約器では要素の増減で
    統計量が動くため)。

    「連続であるべき」ではなく「設計上ここに段差がある」ことを固定する。
    帯の下限を動かしたときにこのテストが落ち、意図した変更かを確認できる。
    """
    low_52_week = "2000"  # 除外閾値 = 1000 / 帯の下限 = 900
    inside_band = _filtered_value("901", low_52_week, _TRANSITION_MIN_RATIO)
    below_band = _filtered_value("899", low_52_week, _TRANSITION_MIN_RATIO)

    assert inside_band is not None, "帯の内側は補間して採用される"
    assert below_band is None, "帯の外側は従来どおり完全に除外される"


# --- I-3: 構造的な不変条件(閾値に依存しない) ---------------------------------


@pytest.mark.parametrize("dist", sorted(_DISTRIBUTIONS))
@pytest.mark.parametrize("ratio", [None, 0.5, 1.29, 1.31, 1.59, 1.61, 3.0])
def test_confidence_low_never_produces_anchor(dist: str, ratio: float | None) -> None:
    """信頼度 LOW では、ばらつきや分布によらず anchor を生成しない。"""
    assert _anchor(_DISTRIBUTIONS[dist], ratio, ConfidenceLevel.LOW) is None


@pytest.mark.parametrize("confidence", [ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM])
def test_no_usable_method_produces_no_anchor(confidence: ConfidenceLevel) -> None:
    """採用できる方式が 0 件なら anchor を生成しない(推測で埋めない)。"""
    empty = FairValueRange(
        bear=None,
        neutral=None,
        bull=None,
        overall_confidence=ConfidenceLevel.MEDIUM,
        methods_used=[],
        methods_excluded=[],
        usable_for_trading_judgment=False,
    )
    band = determine_dispersion_band(1.0, _DISPERSION)
    assert compute_valuation_anchor(empty, confidence, band).anchor is None


@pytest.mark.parametrize("dist", sorted(_DISTRIBUTIONS))
def test_unknown_dispersion_does_not_break_anchor(dist: str) -> None:
    """ばらつきが不明(None)でも anchor の算出が落ちない。

    band が None のとき determine_dispersion_band は None を返す。
    「不明だから止める」ではなく、信頼度側の判断へ委ねる。
    """
    assert determine_dispersion_band(None, _DISPERSION) is None
    assert _anchor(_DISTRIBUTIONS[dist], None, ConfidenceLevel.HIGH) is not None


# --- I-4: 閾値スナップショット -------------------------------------------------


def test_dispersion_threshold_snapshot() -> None:
    """使用している閾値を固定し、設定変更が気づかれずに入ることを防ぐ。

    ## 意図した変更のときの更新手順

    1. 閾値を変える Issue を立て、変更の根拠(実測・影響範囲)を記録する
    2. config を変更する
    3. **本テストの期待値を同じ PR で更新する**
    4. 変更履歴(docs/functional_spec.md)へ利用者向けの説明を1行足す

    期待値だけを先に更新して config を後から変えることはしない
    (テストが「意図しない変更の検出器」として機能しなくなるため)。

    ★ 判定記録への閾値の記録(Issue #189)は本テストの対象外である。
      #189 は status:未着手 であり記録する経路がまだ無いため、
      期待値を先に作らない。
    """
    assert _DISPERSION.low_max == 1.30
    assert _DISPERSION.medium_max == 1.60
    assert _DISPERSION.auto_buy_block == 2.00
    assert _DISPERSION.anchor_block == 50.0
    assert _TRANSITION_MIN_RATIO == 0.90


def test_dispersion_thresholds_keep_their_order() -> None:
    """閾値の順序が壊れていない。

    値そのものより順序のほうが本質であり、config の validator が
    守っている不変条件をテスト側でも明示する。
    """
    assert 0 < _DISPERSION.low_max < _DISPERSION.medium_max
    assert _DISPERSION.medium_max < _DISPERSION.auto_buy_block
    assert _DISPERSION.auto_buy_block < _DISPERSION.anchor_block
    assert 0 < _TRANSITION_MIN_RATIO < 1.0
