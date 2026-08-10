"""HoldingDecisionResultからRecommendationを構築する(実装プラン11節・16節)。

should_notify=trueの場合のみ呼ばれる。売却価格候補の算出(現在株価が必要)は
ここで初めて行い、保有判断スコア自体の算出には一切混ぜない(15節)。
"""

from __future__ import annotations

import uuid

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    HoldingDecisionCategory,
    HoldingDecisionConfidenceLevel,
    RecommendationType,
)
from jstock_advisor.domain.entities.exit_price_range import ExitPriceRangeResult
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holding_decision import HoldingDecisionResult, ReasonImpact
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.earnings_surprise import (
    earnings_surprise_config_values,
    earnings_surprise_result_to_metrics,
)
from jstock_advisor.domain.signals.earnings_trend import (
    earnings_trend_config_values,
    earnings_trend_result_to_metrics,
)
from jstock_advisor.domain.signals.entry_price_range import (
    entry_price_range_config_values,
    entry_price_range_result_to_metrics,
)
from jstock_advisor.domain.signals.exit_price_range import (
    exit_price_range_config_values,
    exit_price_range_result_to_metrics,
)
from jstock_advisor.domain.signals.historical_valuation import (
    historical_valuation_config_values,
    historical_valuation_result_to_metrics,
)
from jstock_advisor.domain.signals.timing_score import (
    timing_score_config_values,
    timing_score_result_to_metrics,
)
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
    config: AppConfig,
    exit_price_range: ExitPriceRangeResult,
    recommendation_id: str | None = None,
) -> Recommendation:
    """should_notify=true(=このHoldingDecisionResultは通知対象)の場合にのみ呼ぶ。

    configは判定精度向上機能Phase B(Historical Valuation Score)のconfig_values_
    used記録専用(コードレビュー対応)。保有判断スコア自体の算出には一切使わない
    (15節の既存原則を維持)。

    exit_price_range(判定精度向上機能次フェーズSTEP2)は呼び出し元
    (lambda_handlers/holdings_watchlist_handler.py)が既に算出済みの値を
    そのまま受け取る。本関数自身はEntry/Exit Price Rangeを一切算出しない
    (Shadow計測の算出責務は呼び出し元に一元化する設計原則)。
    """
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
            "historical_valuation": historical_valuation_config_values(
                config.historical_valuation
            ),
            "timing_score": timing_score_config_values(config.timing_score),
            "earnings_surprise": earnings_surprise_config_values(config.earnings_surprise),
            "earnings_trend": earnings_trend_config_values(config.earnings_trend),
            "entry_price_range": entry_price_range_config_values(config.entry_exit_price.entry),
            "exit_price_range": exit_price_range_config_values(config.entry_exit_price.exit),
        },
        data_sources=list(snapshot.data_sources),
        recommended_action_summary=action_summary,
        next_review_conditions=next_review_conditions,
        holding_risks=reasons,
        # 判定精度向上機能Phase B: DecisionSnapshot記録専用(Shadow計測)。
        historical_valuation_score=snapshot.historical_valuation.score,
        historical_valuation_confidence=snapshot.historical_valuation.confidence,
        historical_valuation_coverage=snapshot.historical_valuation.coverage,
        historical_valuation_reason_codes=snapshot.historical_valuation.reason_codes,
        historical_valuation_metrics=historical_valuation_result_to_metrics(
            snapshot.historical_valuation
        ),
        # 判定精度向上機能Phase B第二弾: DecisionSnapshot記録専用(Shadow計測)。
        timing_score=snapshot.timing.score,
        timing_confidence=snapshot.timing.confidence,
        timing_coverage=snapshot.timing.coverage,
        timing_reason_codes=snapshot.timing.reason_codes,
        timing_metrics=timing_score_result_to_metrics(
            snapshot.timing, snapshot.momentum, snapshot.current_price
        ),
        # 判定精度向上機能Phase C: DecisionSnapshot記録専用(Shadow計測)。
        earnings_surprise_score=snapshot.earnings_surprise.score,
        earnings_surprise_confidence=snapshot.earnings_surprise.confidence,
        earnings_surprise_coverage=snapshot.earnings_surprise.coverage,
        earnings_surprise_reason_codes=snapshot.earnings_surprise.reason_codes,
        earnings_surprise_metrics=earnings_surprise_result_to_metrics(
            snapshot.earnings_surprise
        ),
        earnings_trend_score=snapshot.earnings_trend.score,
        earnings_trend_confidence=snapshot.earnings_trend.confidence,
        earnings_trend_coverage=snapshot.earnings_trend.coverage,
        earnings_trend_reason_codes=snapshot.earnings_trend.reason_codes,
        earnings_trend_metrics=earnings_trend_result_to_metrics(snapshot.earnings_trend),
        # 判定精度向上機能次フェーズSTEP2: DecisionSnapshot記録専用
        # (Shadow計測)。Entryはsnapshot算出済みの値をそのままコピー、
        # Exitは呼び出し元が算出済みのexit_price_rangeをコピーする
        # (本関数自身は算出しない)。
        entry_price_range_state=snapshot.entry_price_range.state,
        entry_price_range_confidence=snapshot.entry_price_range.confidence,
        entry_price_range_coverage=snapshot.entry_price_range.coverage,
        entry_price_range_reason_codes=snapshot.entry_price_range.reason_codes,
        entry_price_range_metrics=entry_price_range_result_to_metrics(
            snapshot.entry_price_range,
            snapshot.fair_value_range,
            snapshot.historical_valuation,
            snapshot.timing,
            snapshot.momentum,
            config.entry_exit_price.entry,
        ),
        entry_price_range_starter_price=snapshot.entry_price_range.starter_entry_price,
        entry_price_range_preferred_price=snapshot.entry_price_range.preferred_entry_price,
        entry_price_range_strong_price=snapshot.entry_price_range.strong_entry_price,
        entry_price_range_max_price=snapshot.entry_price_range.max_entry_price,
        entry_price_range_stop_review_price=snapshot.entry_price_range.stop_review_price,
        exit_price_range_state=exit_price_range.state,
        exit_price_range_confidence=exit_price_range.confidence,
        exit_price_range_coverage=exit_price_range.coverage,
        exit_price_range_reason_codes=exit_price_range.reason_codes,
        exit_price_range_metrics=exit_price_range_result_to_metrics(
            exit_price_range,
            snapshot.fair_value_range,
            snapshot.historical_valuation,
            snapshot.timing,
            holding.average_purchase_price,
            config.entry_exit_price.exit,
        ),
        exit_price_range_partial_low_price=exit_price_range.partial_profit_take_low_price,
        exit_price_range_partial_high_price=exit_price_range.partial_profit_take_high_price,
        exit_price_range_strong_price=exit_price_range.strong_profit_take_price,
        exit_price_range_downside_review_price=exit_price_range.downside_review_price,
        exit_price_range_exit_review_price=exit_price_range.exit_review_price,
    )
