"""必要安全余裕率(2026-07 BUYパイプライン再設計、および第2次修正)。

固定95%/90%/85%方式を廃止し、適正価格の信頼度を基準とした安全余裕率に、
リスクに応じた加算を行う。信頼度LOWの場合は自動の買い推奨価格を生成しない。

--- 第2次修正で全面書き換え ---
初期実装は、複数のリスクコードを単純合算したうえでentry/standard/strongの
3段階へ同額加算し、同じ上限(旧maximum)で個別にキャップしていた。この方式では
(a) 実質的に同じリスクを表す複数コードが二重・三重に加算される
(自動車部品業種ではindustry_model_not_applied/cyclical_industry/
major_customer_dependencyが常に同時発生する)、(b) 加算が大きい場合に3段階
すべてが同一の上限へ潰れ、entry/standard/strong価格が同額になる、という
2つの実害があった(タチエス7239の実データで確認済み)。

本修正では、リスクコードをカテゴリ(MarginRiskCategory)へ分類し、カテゴリ内は
最大値のみを採用(カテゴリ間は合算)したうえで、3段階へは同額ではなく
adjustment_multipliersで感応度を変えて反映し、上限もmaximum_marginで
段階別に分離する。上限適用後もminimum_margin_gapでentry<standard<strongの
最小差を保証する。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.config.models import MarginOfSafetyConfig
from jstock_advisor.domain.entities.common import MarginAdjustment
from jstock_advisor.domain.entities.enums import ConfidenceLevel, MarginRiskCategory

_ADJUSTMENT_REASON_LABELS: dict[str, str] = {
    "earnings_within_3_business_days": "次回決算まで3営業日以内",
    "earnings_within_7_business_days": "次回決算まで7営業日以内",
    "high_valuation_dispersion": "適正価格手法間のばらつきが大きい",
    "very_high_valuation_dispersion": "適正価格手法間のばらつきが非常に大きい",
    "industry_model_not_applied": "業種別適正価格モデル未適用",
    "cyclical_industry": "市況の影響を受けやすい業種",
    "small_cap_or_low_liquidity": "時価総額が小さい、または流動性が低い",
    "volatile_earnings": "業績のブレが大きい",
    "temporary_earnings_boost_risk": "一時的な利益上振れの可能性",
    "major_customer_dependency": "主要顧客への依存度が高い",
    "data_quality_warning": "データ品質に懸念がある",
}

# リスクコードの所属カテゴリ(ユーザー提示の対応表をそのまま採用。要求仕様3節)。
# カテゴリ内で複数コードが該当した場合は最大値のみを採用し、カテゴリ間のみ合算する。
_ADJUSTMENT_CATEGORY_MAP: dict[str, MarginRiskCategory] = {
    "high_valuation_dispersion": MarginRiskCategory.VALUATION_UNCERTAINTY,
    "very_high_valuation_dispersion": MarginRiskCategory.VALUATION_UNCERTAINTY,
    "industry_model_not_applied": MarginRiskCategory.VALUATION_UNCERTAINTY,
    "cyclical_industry": MarginRiskCategory.INDUSTRY_AND_BUSINESS,
    "major_customer_dependency": MarginRiskCategory.INDUSTRY_AND_BUSINESS,
    "volatile_earnings": MarginRiskCategory.EARNINGS_QUALITY,
    "temporary_earnings_boost_risk": MarginRiskCategory.EARNINGS_QUALITY,
    "earnings_within_3_business_days": MarginRiskCategory.EVENT_TIMING,
    "earnings_within_7_business_days": MarginRiskCategory.EVENT_TIMING,
    "data_quality_warning": MarginRiskCategory.DATA_QUALITY,
    "small_cap_or_low_liquidity": MarginRiskCategory.LIQUIDITY,
}


@dataclass(frozen=True)
class MarginOfSafetyResult:
    entry_margin: Decimal | None
    standard_margin: Decimal | None
    strong_margin: Decimal | None
    adjustments: tuple[MarginAdjustment, ...] = ()
    # 信頼度LOWの場合はFalse(自動の買い推奨価格を生成しない)。
    allowed: bool = True
    # --- 第2次修正で追加。買付価格信頼性ゲート(buy_price_reliability.py)が
    # 参照する。entry_margin_before_cap > config.maximum_margin.entryの場合、
    # entryの上限適用による頭打ちが発生しており、機械的に算出した価格の
    # 信頼性が低い可能性が高い ---
    entry_margin_before_cap: Decimal | None = None


def compute_margin_of_safety(
    valuation_confidence: ConfidenceLevel,
    adjustment_codes: list[str],
    config: MarginOfSafetyConfig,
) -> MarginOfSafetyResult:
    if valuation_confidence == ConfidenceLevel.LOW:
        return MarginOfSafetyResult(None, None, None, (), allowed=False)

    tier = (
        config.confidence.high
        if valuation_confidence == ConfidenceLevel.HIGH
        else config.confidence.medium
    )

    # --- カテゴリ内は最大値のみ採用、カテゴリ間は合算 ---
    matched: list[tuple[str, Decimal, MarginRiskCategory]] = []
    for code in adjustment_codes:
        amount = getattr(config.adjustments, code, None)
        if amount is None:
            continue
        category = _ADJUSTMENT_CATEGORY_MAP.get(code)
        if category is None:
            continue
        matched.append((code, Decimal(str(amount)), category))

    best_by_category: dict[MarginRiskCategory, tuple[str, Decimal]] = {}
    for code, amount, category in matched:
        current_best = best_by_category.get(category)
        if current_best is None or amount > current_best[1]:
            best_by_category[category] = (code, amount)

    adjustments: list[MarginAdjustment] = []
    for code, amount, category in matched:
        adopted_code, adopted_amount = best_by_category[category]
        superseded_by = None if code == adopted_code else adopted_code
        adjustments.append(
            MarginAdjustment(
                code=code,
                adjustment=amount,
                reason=_ADJUSTMENT_REASON_LABELS.get(code, code),
                category=category,
                superseded_by=superseded_by,
            )
        )

    category_total = sum((amount for _, amount in best_by_category.values()), Decimal("0"))

    multipliers = config.adjustment_multipliers
    entry_adjustment = category_total * Decimal(str(multipliers.entry))
    standard_adjustment = category_total * Decimal(str(multipliers.standard))
    strong_adjustment = category_total * Decimal(str(multipliers.strong))

    max_entry = Decimal(str(config.maximum_margin.entry))
    max_standard = Decimal(str(config.maximum_margin.standard))
    max_strong = Decimal(str(config.maximum_margin.strong))
    min_gap = Decimal(str(config.minimum_margin_gap))

    entry_before_cap = Decimal(str(tier.entry)) + entry_adjustment
    entry_margin = min(entry_before_cap, max_entry)
    standard_margin = min(Decimal(str(tier.standard)) + standard_adjustment, max_standard)
    strong_margin = min(Decimal(str(tier.strong)) + strong_adjustment, max_strong)

    # --- 上限適用後もentry < standard < strongの最小差を保証する。下位段階を
    # 下げるのではなく、上位段階を「下位+最小差」まで引き上げる(ただし
    # 各段階自身の上限は超えない)。それでも差が確保できない極端なケースは、
    # buy_price_reliability.pyがentry_margin_before_capとの比較で検知する ---
    if standard_margin < entry_margin + min_gap:
        standard_margin = min(entry_margin + min_gap, max_standard)
    if strong_margin < standard_margin + min_gap:
        strong_margin = min(standard_margin + min_gap, max_strong)

    return MarginOfSafetyResult(
        entry_margin=entry_margin,
        standard_margin=standard_margin,
        strong_margin=strong_margin,
        adjustments=tuple(adjustments),
        allowed=True,
        entry_margin_before_cap=entry_before_cap,
    )
