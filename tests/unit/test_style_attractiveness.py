"""Style Attractiveness(Issue #22 Phase B4)のshadow算出の単体テスト。

固定するのは設計(Issue #22 issuecomment-5541944110 の H-1 / H-2)が確定した
次の点である。

  ・分類thresholdからの距離だけを使う(valuation anchor等は使わない)
  ・ASSET_PLAYは自己資本比率を使わない(制約(1)。Layer 1との二重計上を避ける)
  ・QUALITYはNOT_APPLICABLE(分類条件がLayer 1と重複)
  ・複数該当時は全styleを独立に保持する。primary_typeを作らない(要件7)
  ・該当0件ではSTYLE_LAYER自体がNOT_APPLICABLE
  ・qualified stylesを決めない(qualification thresholdはshadow calibrationで決める)

★ 本モジュールの正規化定数はProduction採用値ではない。テストでも
  「この値が本番の閾値である」ことは固定しない(分布を観測するための算術)。
"""

from __future__ import annotations

from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import StockType
from jstock_advisor.domain.scoring.style_attractiveness import (
    QUALIFICATION_STATE,
    StyleAttractivenessInputs,
    score_style_attractiveness,
)

_CONFIG = load_config()
_RULES = _CONFIG.stock_classification


def _inputs(**kwargs: object) -> StyleAttractivenessInputs:
    base: dict[str, object] = {
        "matched_styles": (),
        "dividend_yield_pct": None,
        "consecutive_dividend_increase_years": None,
        "dividend_growth_pct": None,
        "quarterly_operating_incomes": [],
        "current_per": None,
        "current_pbr": None,
    }
    base.update(kwargs)
    return StyleAttractivenessInputs(**base)  # type: ignore[arg-type]


def test_no_matched_style_makes_layer_not_applicable() -> None:
    """StockType該当0件では、Style Layer自体が評価対象外になる(設計H-2)。"""
    result = score_style_attractiveness(_inputs(), _RULES)

    assert result.style_layer_state == "NOT_APPLICABLE"
    assert result.details == ()
    # 該当0件でもqualificationは決めない(閾値を先に決めない)。
    assert result.qualification_state == QUALIFICATION_STATE


def test_income_degree_grows_with_distance_from_threshold() -> None:
    """INCOMEのdegreeは、分類閾値(最低配当利回り)からの距離で増える。"""
    threshold = _RULES.income.min_dividend_yield_pct

    at_threshold = score_style_attractiveness(
        _inputs(matched_styles=(StockType.INCOME,), dividend_yield_pct=threshold),
        _RULES,
    )
    far = score_style_attractiveness(
        _inputs(matched_styles=(StockType.INCOME,), dividend_yield_pct=threshold * 1.5),
        _RULES,
    )

    at_degree = at_threshold.details[0].degree
    far_degree = far.details[0].degree
    assert at_degree == 0.0
    assert far_degree is not None
    assert far_degree > 0.0


def test_missing_feature_value_is_not_evaluated_not_zero() -> None:
    """分類featureの値が取れない場合はNOT_EVALUATEDであり、0.0へ潰さない。

    「魅力が無い」と「測れなかった」を混ぜない(Common Quality側と同じ原則)。
    """
    result = score_style_attractiveness(
        _inputs(matched_styles=(StockType.INCOME,), dividend_yield_pct=None), _RULES
    )

    detail = result.details[0]
    assert detail.state == "NOT_EVALUATED"
    assert detail.degree is None


def test_asset_play_uses_only_pbr_distance() -> None:
    """ASSET_PLAYのSAはPBRの距離だけを使う(制約(1)の回帰防止)。

    分類条件は「PBRが閾値未満」かつ「自己資本比率が下限以上」だが、後者は
    Common Qualityの最大配点componentと同じ入力である。SAへ含めると
    Layer 1とLayer 2が同じ情報で二重に効く。
    """
    result = score_style_attractiveness(
        _inputs(matched_styles=(StockType.ASSET_PLAY,), current_pbr=Decimal("0.5")),
        _RULES,
    )

    detail = result.details[0]
    features = [f.feature for f in detail.features]
    assert features == ["current_pbr"]
    assert "equity_ratio_pct" not in features


def test_quality_is_not_applicable_because_it_overlaps_layer1() -> None:
    """QUALITYは分類条件がLayer 1と重複するためSAを評価しない(設計の確定)。"""
    result = score_style_attractiveness(_inputs(matched_styles=(StockType.QUALITY,)), _RULES)

    detail = result.details[0]
    assert detail.state == "NOT_APPLICABLE"
    assert detail.degree is None
    assert detail.reason is not None


def test_keyword_based_styles_are_not_applicable() -> None:
    """業種・開示キーワードで分類されるstyleは数値閾値を持たないため評価しない。"""
    result = score_style_attractiveness(
        _inputs(
            matched_styles=(
                StockType.CYCLICAL,
                StockType.DEFENSIVE,
                StockType.EVENT_DRIVEN,
            )
        ),
        _RULES,
    )

    assert [d.state for d in result.details] == [
        "NOT_APPLICABLE",
        "NOT_APPLICABLE",
        "NOT_APPLICABLE",
    ]


def test_multiple_styles_are_kept_independently_without_primary_type() -> None:
    """複数該当時は全styleを独立に保持し、代表値を作らない(要件7)。"""
    result = score_style_attractiveness(
        _inputs(
            matched_styles=(StockType.INCOME, StockType.VALUE),
            dividend_yield_pct=_RULES.income.min_dividend_yield_pct * 1.2,
            current_per=Decimal(str(_RULES.value.max_per * 0.5)),
            current_pbr=Decimal(str(_RULES.value.max_pbr * 0.5)),
        ),
        _RULES,
    )

    assert [d.style for d in result.details] == ["INCOME", "VALUE"]
    # 代表値(primary_type / max score)を持つフィールドは存在しない。
    assert not hasattr(result, "primary_type")
    assert not hasattr(result, "max_degree")


def test_value_uses_classification_thresholds_not_valuation_anchor() -> None:
    """VALUEのSAは分類閾値(max_per / max_pbr)からの距離で表す。

    Layer 3のvaluation anchor / fair value / entry priceは使わない
    (設計のPROHIBITED_IN_STYLE_ATTRACTIVENESS)。入力dataclassにも
    それらのフィールドは存在しない。
    """
    result = score_style_attractiveness(
        _inputs(
            matched_styles=(StockType.VALUE,),
            current_per=Decimal(str(_RULES.value.max_per * 0.5)),
            current_pbr=Decimal(str(_RULES.value.max_pbr * 0.5)),
        ),
        _RULES,
    )

    detail = result.details[0]
    thresholds = {f.feature: f.threshold for f in detail.features}
    assert thresholds["current_per"] == _RULES.value.max_per
    assert thresholds["current_pbr"] == _RULES.value.max_pbr
    # 入力契約にvaluation由来のフィールドが無いことを固定する。
    assert not hasattr(
        StyleAttractivenessInputs(
            matched_styles=(),
            dividend_yield_pct=None,
            consecutive_dividend_increase_years=None,
            dividend_growth_pct=None,
            quarterly_operating_incomes=[],
            current_per=None,
            current_pbr=None,
        ),
        "valuation_anchor",
    )


def test_qualified_styles_are_not_decided_here() -> None:
    """qualified stylesを決めない。qualification thresholdは未確定である。

    設計は「閾値を先に決めない」と定めており、実装都合で仮の本番閾値を
    固定しない。ここでは状態としてSHADOW_CALIBRATION_REQUIREDを残す。
    """
    result = score_style_attractiveness(
        _inputs(
            matched_styles=(StockType.INCOME,),
            dividend_yield_pct=_RULES.income.min_dividend_yield_pct * 3,
        ),
        _RULES,
    )

    assert result.qualification_state == "SHADOW_CALIBRATION_REQUIRED"
    assert not hasattr(result, "qualified_styles")
