"""Style Attractiveness(投資スタイルとしての魅力度)のshadow算出(Issue #22 Phase B4)。

Layer 2に相当する。企業品質(Layer 1)でも価格評価(Layer 3)でもなく、
「そのStockTypeとして、どの程度 魅力的か」だけを表す。

設計(Issue #22 issuecomment-5541944110 の H-1)が採用した方式は
CLASSIFICATION_THRESHOLD_DISTANCE である。すなわち、

  そのStockTypeのmembershipを成立させたclassification featureについて、
  classification thresholdからの距離をstyle degreeとして使う

ALLOWED / PROHIBITED の境界(設計原文)

  ALLOWED_IN_STYLE_ATTRACTIVENESS
      membershipを成立させたclassification featureの、閾値からの距離
  PROHIBITED_IN_STYLE_ATTRACTIVENESS
      valuation anchor / fair value target / entry price / target buy price /
      margin of safety / 現在価格と行動可能な買い閾値の比較 /
      Layer 3のprice gate結果 / Layer 3のvaluation判定出力

本モジュールはvaluation(domain/valuation/)を一切importしない。PER/PBRは
分類条件そのものであり、Layer 3のanchorとは別concept である
(ACTIONABLE_VALUATION_PRICE_GATE = LAYER_3_ONLY)。

★ 本モジュールはshadow専用である。
  STYLE_ATTRACTIVENESS = SHADOW_ONLY / NON_BLOCKING
  現行のBUY判定・通知・価格判定へは接続しない
  (V2_SHADOW_DECISION = CURRENT_PRODUCTION_DECISION_UNCHANGED)。

★ 本モジュールはqualified stylesを決めない。
  「閾値を満たしたstyleの集合」を作るには qualification threshold が要るが、
  その値はshadow evidenceを見てから決める(設計の「閾値を先に決めない」)。
  ここではdegreeだけを記録し、qualificationはSHADOW_CALIBRATION_REQUIREDとする。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.config.models import StockClassificationRulesConfig
from jstock_advisor.domain.entities.enums import StockType

# degreeの正規化上限。閾値をちょうど満たした場合を0.0、閾値の2倍だけ
# 上回った(または下回った)場合を1.0として頭打ちにする。
#
# ★★ これはProduction採用値ではない。★★
# shadowでdegreeの分布を観測するために必要な、算術上の正規化定数である。
# 本番の判定に使う閾値・上限は shadow evidence を見てから人間が決める
# (設計の「閾値を先に決めない」/ H-3 の STYLE_ATTRACTIVENESS = SHADOW_ONLY)。
# この値をBUY判定・通知・価格判定へ接続してはならない。
SHADOW_DEGREE_NORMALIZATION_RATIO = 1.0

# stateの値。company_quality側のEvidenceCoverageStatusと同じ語を使い、
# 新しい状態体系を作らない(設計の「新しいstate体系は作りません」)。
STATE_EVALUATED = "EVALUATED"
STATE_NOT_EVALUATED = "NOT_EVALUATED"
STATE_NOT_APPLICABLE = "NOT_APPLICABLE"

# qualified styles を決めない理由を、値としてそのまま残す。
QUALIFICATION_STATE = "SHADOW_CALIBRATION_REQUIRED"


@dataclass(frozen=True)
class StyleAttractivenessInputs:
    """SAの算出に使う判定時点の入力。

    valuation anchor / fair value / entry price は受け取らない(設計のPROHIBITED)。
    """

    matched_styles: tuple[StockType, ...]
    dividend_yield_pct: float | None
    consecutive_dividend_increase_years: int | None
    dividend_growth_pct: float | None
    quarterly_operating_incomes: list[Decimal]
    current_per: Decimal | None
    current_pbr: Decimal | None


@dataclass(frozen=True)
class StyleFeatureDetail:
    """degreeの根拠となった1 feature分の内訳。"""

    feature: str
    value: float | None
    threshold: float
    direction: str  # "AT_LEAST" / "BELOW"
    degree: float | None


@dataclass(frozen=True)
class StyleAttractivenessDetail:
    """1 style分のSA。"""

    style: str
    state: str
    degree: float | None
    features: tuple[StyleFeatureDetail, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class StyleAttractivenessResult:
    """matched styleごとのSAの集合。

    primary_typeは作らない。最大値も「代表値」として持たない(要件7)。
    """

    style_layer_state: str
    details: tuple[StyleAttractivenessDetail, ...] = ()
    qualification_state: str = QUALIFICATION_STATE


def _degree_at_least(value: float | None, threshold: float) -> float | None:
    """「閾値以上」で成立するfeatureの、閾値からの距離。

    閾値ちょうどで0.0、閾値をSHADOW_DEGREE_NORMALIZATION_RATIO倍だけ上回ると1.0。
    """
    if value is None or threshold <= 0:
        return None
    excess_ratio = (value - threshold) / threshold
    return _clamp(excess_ratio / SHADOW_DEGREE_NORMALIZATION_RATIO)


def _degree_below(value: float | None, threshold: float) -> float | None:
    """「閾値未満」で成立するfeatureの、閾値からの距離。"""
    if value is None or threshold <= 0:
        return None
    excess_ratio = (threshold - value) / threshold
    return _clamp(excess_ratio / SHADOW_DEGREE_NORMALIZATION_RATIO)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _combine(degrees: list[float | None]) -> float | None:
    """複数featureのdegreeを1つにまとめる。

    評価できたfeatureの平均とする。評価できたものが無ければNone
    (0.0へ潰さない。「魅力が無い」と「測れなかった」を混ぜない)。
    """
    available = [d for d in degrees if d is not None]
    if not available:
        return None
    return sum(available) / len(available)


def _growth_magnitude_pct(quarterly_operating_incomes: list[Decimal]) -> float | None:
    """営業利益の成長の程度(%)。

    分類条件は「非減少トレンド」という真偽であり、程度を持たない。SAでは
    同じ系列の最初と最後の比で程度を表す(新しいデータを持ち込まない)。
    起点が0または負の場合は比が意味を持たないためNoneとする(推測しない)。
    """
    if len(quarterly_operating_incomes) < 2:
        return None
    first = quarterly_operating_incomes[0]
    last = quarterly_operating_incomes[-1]
    if first <= 0:
        return None
    return float((last - first) / first * 100)


def _improvement_quarters(quarterly_operating_incomes: list[Decimal]) -> int | None:
    """直近から連続して改善している期数。

    分類条件(min_consecutive_improvement_quarters以上)と同じ向きの量であり、
    SAではその期数そのものを程度として使う。
    """
    if len(quarterly_operating_incomes) < 2:
        return None
    streak = 0
    for i in range(len(quarterly_operating_incomes) - 1, 0, -1):
        if quarterly_operating_incomes[i] > quarterly_operating_incomes[i - 1]:
            streak += 1
        else:
            break
    return streak


def score_style_attractiveness(
    inputs: StyleAttractivenessInputs,
    config: StockClassificationRulesConfig,
) -> StyleAttractivenessResult:
    """matched styleごとにSAを算出する(shadow専用)。

    StockType該当0件の場合はSTYLE_LAYER自体がNOT_APPLICABLEになる
    (Common Qualityは通常どおり評価される。設計のH-2)。
    """
    if not inputs.matched_styles:
        return StyleAttractivenessResult(style_layer_state=STATE_NOT_APPLICABLE)

    details: list[StyleAttractivenessDetail] = []
    for style in inputs.matched_styles:
        details.append(_score_single_style(style, inputs, config))
    return StyleAttractivenessResult(
        style_layer_state=STATE_EVALUATED,
        details=tuple(details),
    )


def _evaluated_or_missing(
    style: StockType,
    features: list[StyleFeatureDetail],
) -> StyleAttractivenessDetail:
    degree = _combine([f.degree for f in features])
    state = STATE_EVALUATED if degree is not None else STATE_NOT_EVALUATED
    reason = None if degree is not None else "分類featureの判定時点値を取得できなかった"
    return StyleAttractivenessDetail(
        style=style.value,
        state=state,
        degree=degree,
        features=tuple(features),
        reason=reason,
    )


def _not_applicable(style: StockType, reason: str) -> StyleAttractivenessDetail:
    return StyleAttractivenessDetail(
        style=style.value,
        state=STATE_NOT_APPLICABLE,
        degree=None,
        reason=reason,
    )


def _score_single_style(
    style: StockType,
    inputs: StyleAttractivenessInputs,
    config: StockClassificationRulesConfig,
) -> StyleAttractivenessDetail:
    if style is StockType.INCOME:
        threshold = config.income.min_dividend_yield_pct
        return _evaluated_or_missing(
            style,
            [
                StyleFeatureDetail(
                    feature="dividend_yield_pct",
                    value=inputs.dividend_yield_pct,
                    threshold=threshold,
                    direction="AT_LEAST",
                    degree=_degree_at_least(inputs.dividend_yield_pct, threshold),
                )
            ],
        )

    if style is StockType.DIVIDEND_GROWTH:
        years_threshold = float(config.dividend_growth.min_consecutive_dividend_increase_years)
        growth_threshold = config.dividend_growth.min_dividend_growth_pct
        years_value = (
            float(inputs.consecutive_dividend_increase_years)
            if inputs.consecutive_dividend_increase_years is not None
            else None
        )
        return _evaluated_or_missing(
            style,
            [
                StyleFeatureDetail(
                    feature="consecutive_dividend_increase_years",
                    value=years_value,
                    threshold=years_threshold,
                    direction="AT_LEAST",
                    degree=_degree_at_least(years_value, years_threshold),
                ),
                StyleFeatureDetail(
                    feature="dividend_growth_pct",
                    value=inputs.dividend_growth_pct,
                    threshold=growth_threshold,
                    direction="AT_LEAST",
                    degree=_degree_at_least(inputs.dividend_growth_pct, growth_threshold),
                ),
            ],
        )

    if style is StockType.GROWTH:
        # 分類条件は「非減少トレンド」という真偽。SAはその程度を表す。
        # 閾値は分類側に無いため、非減少の境界である0%を基準にする。
        magnitude = _growth_magnitude_pct(inputs.quarterly_operating_incomes)
        degree = None
        if magnitude is not None:
            # 0%で0.0、100%成長で1.0(shadow用の正規化。Production採用値ではない)。
            degree = _clamp(magnitude / 100.0)
        return _evaluated_or_missing(
            style,
            [
                StyleFeatureDetail(
                    feature="operating_income_growth_pct",
                    value=magnitude,
                    threshold=0.0,
                    direction="AT_LEAST",
                    degree=degree,
                )
            ],
        )

    if style is StockType.VALUE:
        per_threshold = config.value.max_per
        pbr_threshold = config.value.max_pbr
        per_value = float(inputs.current_per) if inputs.current_per is not None else None
        pbr_value = float(inputs.current_pbr) if inputs.current_pbr is not None else None
        return _evaluated_or_missing(
            style,
            [
                StyleFeatureDetail(
                    feature="current_per",
                    value=per_value,
                    threshold=per_threshold,
                    direction="BELOW",
                    degree=_degree_below(per_value, per_threshold),
                ),
                StyleFeatureDetail(
                    feature="current_pbr",
                    value=pbr_value,
                    threshold=pbr_threshold,
                    direction="BELOW",
                    degree=_degree_below(pbr_value, pbr_threshold),
                ),
            ],
        )

    if style is StockType.ASSET_PLAY:
        # 設計の制約(1): ASSET_PLAYの分類条件は「PBRが閾値未満」かつ
        # 「自己資本比率が下限以上」だが、後者はCommon Qualityの最大配点
        # componentと同じ入力である。SAへ含めるとLayer 1とLayer 2が同じ情報で
        # 二重に効く(C2の失敗と同型)。したがってPBR側の距離のみを使う。
        pbr_threshold = config.asset_play.max_pbr
        pbr_value = float(inputs.current_pbr) if inputs.current_pbr is not None else None
        return _evaluated_or_missing(
            style,
            [
                StyleFeatureDetail(
                    feature="current_pbr",
                    value=pbr_value,
                    threshold=pbr_threshold,
                    direction="BELOW",
                    degree=_degree_below(pbr_value, pbr_threshold),
                )
            ],
        )

    if style is StockType.TURNAROUND:
        threshold = float(config.turnaround.min_consecutive_improvement_quarters)
        quarters = _improvement_quarters(inputs.quarterly_operating_incomes)
        quarters_value = float(quarters) if quarters is not None else None
        return _evaluated_or_missing(
            style,
            [
                StyleFeatureDetail(
                    feature="consecutive_improvement_quarters",
                    value=quarters_value,
                    threshold=threshold,
                    direction="AT_LEAST",
                    degree=_degree_at_least(quarters_value, threshold),
                )
            ],
        )

    if style is StockType.QUALITY:
        # 設計の確定: QUALITYの分類条件(ROE・自己資本比率・営業CF)はLayer 1と
        # 重複するため、SAとしては評価しない。
        return _not_applicable(style, "分類条件がCommon Quality(Layer 1)と重複するため")

    # CYCLICAL / DEFENSIVE / EVENT_DRIVEN は業種キーワード・開示キーワードで
    # 分類され、数値の閾値を持たない。距離を定義できないためSAは評価しない。
    return _not_applicable(style, "分類条件が数値閾値を持たないため(キーワード一致)")
