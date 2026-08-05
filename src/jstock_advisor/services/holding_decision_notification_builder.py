"""HoldingDecisionResultからRecommendationを構築する(実装プラン11節・16節)。

should_notify=trueの場合のみ呼ばれる。売却価格候補の算出(現在株価が必要)は
ここで初めて行い、保有判断スコア自体の算出には一切混ぜない(15節)。
"""

from __future__ import annotations

import uuid

from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    HoldingDecisionCategory,
    HoldingDecisionConfidenceLevel,
    RecommendationType,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holding_decision import HoldingDecisionResult, ReasonImpact
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.services.sell_price_recommendation_service import recommend_sell_prices
from jstock_advisor.services.stock_snapshot_service import StockSnapshot

_REASON_CODE_LABELS: dict[str, str] = {
    "financial_health_equity_ratio": "自己資本比率の水準",
    "financial_health_debt_excess": "債務超過の有無",
    "cash_generation_cf_income_ratio": "営業CFと営業利益の対応",
    "cash_generation_cf_streak": "営業CFの黒字継続性",
    "profitability_roe": "簡易予想ROEの水準",
    "profitability_eps_stability": "EPSの安定性",
    "stability_operating_income": "営業利益の安定性",
    "stability_deficit": "赤字の有無",
    "governance_going_concern": "継続企業の前提",
    "governance_listing_risk": "上場維持リスク",
    "dividend_policy": "配当方針の維持",
    "total_yield": "配当+優待の総合利回り",
    "benefit_condition": "株主優待条件の維持",
    "profit_cf_premise": "中長期的な利益・CF前提の維持",
    "financial_premise": "財務健全性に関する投資前提の維持",
    "custom_conditions": "個別に登録した銘柄固有の投資理由",
    "business_cashflow_deterioration": "業績・キャッシュフローの悪化",
    "shareholder_return_deterioration": "株主還元の悪化",
    "financial_crisis": "財務危機の兆候",
    "governance_and_listing_risk": "不祥事・法規制・上場維持リスク",
    "structural_change": "大型希薄化・買収・構造変化",
}

_CATEGORY_TO_RECOMMENDATION_TYPE: dict[HoldingDecisionCategory, RecommendationType] = {
    HoldingDecisionCategory.SELL_CONSIDERATION: RecommendationType.SELL_CONSIDERATION,
    HoldingDecisionCategory.STRONG_SELL_CONSIDERATION: RecommendationType.STRONG_SELL_CONSIDERATION,
}

_CONFIDENCE_MAP: dict[HoldingDecisionConfidenceLevel, ConfidenceLevel] = {
    HoldingDecisionConfidenceLevel.HIGH: ConfidenceLevel.HIGH,
    HoldingDecisionConfidenceLevel.MEDIUM: ConfidenceLevel.MEDIUM,
    HoldingDecisionConfidenceLevel.LOW: ConfidenceLevel.LOW,
}


def _reason_label(reason: ReasonImpact) -> str:
    return _REASON_CODE_LABELS.get(reason.reason_code, reason.reason_code)


def build_holding_decision_recommendation(
    holding: Holding,
    result: HoldingDecisionResult,
    snapshot: StockSnapshot,
    rule_version: str,
    recommendation_id: str | None = None,
) -> Recommendation:
    """should_notify=true(=このHoldingDecisionResultは通知対象)の場合にのみ呼ぶ。"""
    if result.hard_gate.triggered:
        recommendation_type = RecommendationType.URGENT_HOLDING_REVIEW
    else:
        recommendation_type = _CATEGORY_TO_RECOMMENDATION_TYPE.get(
            result.category, RecommendationType.SELL_CONSIDERATION
        )

    sell_prices = recommend_sell_prices(
        current_price=snapshot.current_price,
        category=result.category,
        hard_gate_triggered=result.hard_gate.triggered,
        fair_value_range=snapshot.fair_value_range,
    )

    reasons = [_reason_label(r) for r in result.negative_reasons]
    counter_factors = [_reason_label(r) for r in result.positive_reasons]

    action_summary = (
        "重大条件のため保有判断スコアへ上限補正を適用しています。速やかに内容を確認してください。"
        if result.hard_gate.triggered
        else (
            f"保有判断スコア{result.display_value}点。"
            "投資前提の悪化が疑われるため売却を検討してください。"
        )
    )

    next_review_conditions = ["次回決算発表後に再評価する"]

    return Recommendation(
        recommendation_id=recommendation_id or str(uuid.uuid4()),
        stock_code=holding.stock_code,
        stock_name=snapshot.financial.stock_name or holding.stock_name,
        recommended_at=result.evaluated_at,
        recommendation_type=recommendation_type,
        raw_recommendation_type=recommendation_type,
        sell_prices=sell_prices,
        price_at_recommendation=snapshot.current_price,
        average_purchase_price_at_recommendation=holding.average_purchase_price,
        shares_at_recommendation=holding.shares,
        dividend_yield_pct_at_recommendation=snapshot.dividend_yield_pct,
        shareholder_benefit_yield_pct_at_recommendation=snapshot.benefit_yield_pct,
        total_yield_pct_at_recommendation=snapshot.total_yield_pct,
        fair_value_at_recommendation=snapshot.fair_value,
        reasons=reasons,
        counter_factors=counter_factors,
        confidence=_CONFIDENCE_MAP.get(result.confidence, ConfidenceLevel.LOW),
        next_earnings_date=snapshot.next_earnings_date,
        rule_version=rule_version,
        config_values_used={
            "holding_decision_result_id": result.holding_decision_result_id,
            "base_score": result.base_score,
            "final_score": result.final_score,
            "category": result.category.value,
            "hard_gate_triggered": result.hard_gate.triggered,
            "hard_gate_adjustment_applied": result.hard_gate.adjustment_applied,
            "company_quality_score": result.company_quality.score,
            "investment_thesis_score": result.investment_thesis.score,
            "risk_deduction_score": result.risk_deduction.score,
        },
        data_sources=list(snapshot.data_sources),
        recommended_action_summary=action_summary,
        next_review_conditions=next_review_conditions,
        holding_risks=reasons,
    )
