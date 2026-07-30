import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import PriceWithRationale, SellPriceLevels
from jstock_advisor.domain.entities.enums import (
    BuyAction,
    ConfidenceLevel,
    PriceFieldBasis,
    RecommendationType,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.services.recommendation_consistency_validator import validate_recommendation

_NOW = dt.datetime(2026, 7, 27, tzinfo=dt.UTC)
_CONFIG = load_config().data_validation.consistency_validation


def _recommendation(
    recommendation_type: RecommendationType,
    price: Decimal,
    sell_prices: SellPriceLevels | None = None,
    average_purchase_price: Decimal | None = None,
    total_yield_pct: float | None = None,
    reasons: list[str] | None = None,
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM,
    evidence_details: list[dict] | None = None,
    independent_evidence_group_count: int | None = None,
) -> Recommendation:
    return Recommendation(
        recommendation_id="rec-1",
        stock_code="2914",
        stock_name="JT",
        recommended_at=_NOW,
        recommendation_type=recommendation_type,
        sell_prices=sell_prices,
        price_at_recommendation=price,
        average_purchase_price_at_recommendation=average_purchase_price,
        total_yield_pct_at_recommendation=total_yield_pct,
        reasons=reasons or [],
        confidence=confidence,
        rule_version="v1-mvp",
        evidence_details=evidence_details or [],
        independent_evidence_group_count=independent_evidence_group_count,
    )


def test_clean_recommendation_passes() -> None:
    sell_prices = SellPriceLevels(
        recommended_limit_price=PriceWithRationale(
            price=Decimal("6300"), rationale="x", basis=PriceFieldBasis.TARGET_PRICE
        ),
        full_profit_consideration_price=PriceWithRationale(price=Decimal("6800"), rationale="x"),
    )
    r = _recommendation(
        RecommendationType.FULL_PROFIT_TAKE,
        Decimal("6531"),
        sell_prices=sell_prices,
        average_purchase_price=Decimal("4000"),  # 含み益率63% >= 全利確閾値50%
        reasons=["含み益率63.0%が全株利確閾値に到達", "適正価格レンジ上限を超過"],
    )
    result = validate_recommendation(r, _CONFIG)
    assert result.passed is True


def test_full_take_extreme_margin_flagged() -> None:
    sell_prices = SellPriceLevels(
        full_profit_consideration_price=PriceWithRationale(price=Decimal("15000"), rationale="x")
    )
    r = _recommendation(
        RecommendationType.FULL_PROFIT_TAKE,
        Decimal("6531"),
        sell_prices=sell_prices,
        average_purchase_price=Decimal("4000"),
        reasons=["a", "b"],
    )
    result = validate_recommendation(r, _CONFIG)
    assert result.passed is False
    assert any(v.check_name == "full_take_extreme_margin" for v in result.violations)


def test_full_take_missing_all_price_guidance_flagged() -> None:
    r = _recommendation(
        RecommendationType.FULL_PROFIT_TAKE,
        Decimal("6531"),
        sell_prices=SellPriceLevels(),
        average_purchase_price=Decimal("4000"),
        reasons=["a", "b"],
    )
    result = validate_recommendation(r, _CONFIG)
    assert any(v.check_name == "full_take_no_price_guidance" for v in result.violations)


def test_watch_immediate_execution_flagged() -> None:
    sell_prices = SellPriceLevels(
        recommended_limit_price=PriceWithRationale(
            price=Decimal("6531"),
            rationale="x",
            basis=PriceFieldBasis.IMMEDIATE_EXECUTION_REFERENCE,
        )
    )
    r = _recommendation(RecommendationType.WATCH, Decimal("6531"), sell_prices=sell_prices)
    result = validate_recommendation(r, _CONFIG)
    assert any(v.check_name == "watch_recommends_immediate_sell" for v in result.violations)


def test_three_or_more_equal_prices_flagged() -> None:
    same_price = PriceWithRationale(price=Decimal("6531"), rationale="x")
    sell_prices = SellPriceLevels(
        partial_profit_start_price=same_price,
        recommended_limit_price=same_price,
        full_profit_consideration_price=same_price,
    )
    r = _recommendation(
        RecommendationType.PARTIAL_PROFIT_TAKE, Decimal("6531"), sell_prices=sell_prices
    )
    result = validate_recommendation(r, _CONFIG)
    assert any(v.check_name == "three_or_more_equal_prices" for v in result.violations)


def test_reevaluation_unreasonably_above_full_take_flagged() -> None:
    sell_prices = SellPriceLevels(
        full_profit_consideration_price=PriceWithRationale(price=Decimal("8490"), rationale="x"),
        reevaluation_price_upside=PriceWithRationale(price=Decimal("20000"), rationale="x"),
    )
    r = _recommendation(
        RecommendationType.FULL_PROFIT_TAKE, Decimal("6531"), sell_prices=sell_prices
    )
    result = validate_recommendation(r, _CONFIG)
    assert any(
        v.check_name == "reevaluation_unreasonably_above_full_take" for v in result.violations
    )


def test_low_fair_value_confidence_full_take_flagged() -> None:
    # 通知直前のvalidate_recommendation呼び出し元は永続化されたRecommendationしか
    # 持たないため、fair_value_overall_confidenceはRecommendation自身のフィールド
    # から読む(以前はFairValueRangeを別途渡す設計で、実際の呼び出し元では常に
    # Noneになりチェックが無効化されていたバグの回帰テスト)。
    r = _recommendation(
        RecommendationType.FULL_PROFIT_TAKE,
        Decimal("6531"),
        reasons=["最終適正価格を大幅に超過"],
    ).model_copy(update={"fair_value_overall_confidence": ConfidenceLevel.LOW})
    result = validate_recommendation(r, _CONFIG)
    assert any(
        v.check_name == "low_fair_value_confidence_full_take" for v in result.violations
    )


def test_low_fair_value_confidence_full_take_not_flagged_when_confidence_missing() -> None:
    r = _recommendation(
        RecommendationType.FULL_PROFIT_TAKE,
        Decimal("6531"),
        reasons=["最終適正価格を大幅に超過"],
    )
    result = validate_recommendation(r, _CONFIG)
    assert not any(
        v.check_name == "low_fair_value_confidence_full_take" for v in result.violations
    )


def test_gain_below_threshold_full_take_with_few_reasons_flagged() -> None:
    r = _recommendation(
        RecommendationType.FULL_PROFIT_TAKE,
        Decimal("1100"),
        average_purchase_price=Decimal("1000"),  # 含み益10% << 全利確閾値50%
        reasons=["含み益率10.0%が全株利確閾値に到達"],  # 1件のみ
    )
    result = validate_recommendation(r, _CONFIG, gain_full_threshold_pct=50.0)
    assert any(
        v.check_name == "full_take_with_insufficient_gain_and_reasons" for v in result.violations
    )


def test_sufficient_yield_full_take_on_yield_alone_flagged() -> None:
    r = _recommendation(
        RecommendationType.FULL_PROFIT_TAKE,
        Decimal("1100"),
        total_yield_pct=4.0,  # 最低基準以上
        reasons=["総合利回りが低下"],
    )
    result = validate_recommendation(r, _CONFIG, min_yield_pct=2.5)
    assert any(
        v.check_name == "sufficient_yield_full_take_on_yield_alone" for v in result.violations
    )


def test_price_equals_current_with_target_basis_flagged() -> None:
    sell_prices = SellPriceLevels(
        recommended_limit_price=PriceWithRationale(
            price=Decimal("6531"), rationale="x", basis=PriceFieldBasis.TARGET_PRICE
        )
    )
    r = _recommendation(
        RecommendationType.PARTIAL_PROFIT_TAKE, Decimal("6531"), sell_prices=sell_prices
    )
    result = validate_recommendation(r, _CONFIG)
    assert any(
        v.check_name == "price_equals_current_with_target_basis" for v in result.violations
    )


# --- 独立根拠グループ数ベースの整合性検査(2026-07仕様レビュー対応) ------------


def _evidence(*, immediate: bool = False, primary_confirmed: bool = False) -> dict:
    return {
        "rule_name": "x",
        "status": "TRIGGERED",
        "severity": "critical",
        "evidence_group": "GOVERNANCE",
        "is_immediate_critical": immediate,
        "primary_source_confirmed": primary_confirmed,
        "explanation": "x",
    }


def test_sell_with_single_independent_group_flagged_for_manual_review() -> None:
    r = _recommendation(
        RecommendationType.SELL,
        Decimal("1000"),
        evidence_details=[_evidence()],
        independent_evidence_group_count=1,
    )
    result = validate_recommendation(r, _CONFIG)
    assert result.requires_manual_review
    assert any(v.check_name == "sell_based_on_single_evidence" for v in result.violations)


def test_sell_with_two_independent_groups_not_flagged() -> None:
    r = _recommendation(
        RecommendationType.SELL,
        Decimal("1000"),
        evidence_details=[_evidence(), _evidence()],
        independent_evidence_group_count=2,
    )
    result = validate_recommendation(r, _CONFIG)
    assert not any(v.check_name == "sell_based_on_single_evidence" for v in result.violations)


def test_urgent_review_with_unconfirmed_immediate_critical_flagged() -> None:
    r = _recommendation(
        RecommendationType.URGENT_REVIEW,
        Decimal("1000"),
        evidence_details=[_evidence(immediate=True, primary_confirmed=False)],
        independent_evidence_group_count=1,
    )
    result = validate_recommendation(r, _CONFIG)
    assert any(v.check_name == "sell_based_on_single_evidence" for v in result.violations)


def test_urgent_review_with_confirmed_immediate_critical_allowed_single_group() -> None:
    r = _recommendation(
        RecommendationType.URGENT_REVIEW,
        Decimal("1000"),
        evidence_details=[_evidence(immediate=True, primary_confirmed=True)],
        independent_evidence_group_count=1,
    )
    result = validate_recommendation(r, _CONFIG)
    assert not any(v.check_name == "sell_based_on_single_evidence" for v in result.violations)


def test_review_with_immediate_execution_price_flagged() -> None:
    sell_prices = SellPriceLevels(
        immediate_execution_price=PriceWithRationale(price=Decimal("1000"), rationale="x")
    )
    r = _recommendation(RecommendationType.REVIEW, Decimal("1000"), sell_prices=sell_prices)
    result = validate_recommendation(r, _CONFIG)
    assert result.requires_manual_review
    assert any(
        v.check_name == "review_retains_immediate_execution_price" for v in result.violations
    )


def test_watch_with_immediate_partial_profit_start_price_flagged() -> None:
    # partial_profit_start_priceがIMMEDIATE_EXECUTION_REFERENCEの場合も検出する
    # (recommended_limit_priceのみを確認していた旧実装のギャップ、レビュー対応)。
    sell_prices = SellPriceLevels(
        partial_profit_start_price=PriceWithRationale(
            price=Decimal("637"), rationale="x", basis=PriceFieldBasis.IMMEDIATE_EXECUTION_REFERENCE
        )
    )
    r = _recommendation(RecommendationType.WATCH, Decimal("637"), sell_prices=sell_prices)
    result = validate_recommendation(r, _CONFIG)
    assert any(v.check_name == "watch_recommends_immediate_sell" for v in result.violations)


def test_watch_with_immediate_execution_price_field_flagged() -> None:
    sell_prices = SellPriceLevels(
        immediate_execution_price=PriceWithRationale(price=Decimal("637"), rationale="x")
    )
    r = _recommendation(RecommendationType.WATCH, Decimal("637"), sell_prices=sell_prices)
    result = validate_recommendation(r, _CONFIG)
    assert any(v.check_name == "watch_recommends_immediate_sell" for v in result.violations)


def test_review_without_immediate_execution_price_not_flagged() -> None:
    r = _recommendation(RecommendationType.REVIEW, Decimal("1000"), sell_prices=SellPriceLevels())
    result = validate_recommendation(r, _CONFIG)
    assert not any(
        v.check_name == "review_retains_immediate_execution_price" for v in result.violations
    )


# --- BUYパイプライン整合性チェックの配線(2026-07 BUYパイプライン再設計) --------

_BUY_DECISION_CONFIG = load_config().buy_decision


def _buy_recommendation(
    action: BuyAction, current_price: Decimal, **overrides: object
) -> Recommendation:
    base = dict(
        recommendation_id="rec-buy-1",
        stock_code="4516",
        stock_name="日本新薬",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        price_at_recommendation=current_price,
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
        buy_action=action,
        entry_buy_price=Decimal("1000"),
        standard_buy_price=Decimal("900"),
        strong_buy_price=Decimal("800"),
        business_days_to_earnings=30,
        valuation_dispersion_ratio=Decimal("1.1"),
    )
    base.update(overrides)
    return Recommendation(**base)  # type: ignore[arg-type]


def test_buy_action_not_flagged_when_consistent() -> None:
    r = _buy_recommendation(BuyAction.BUY, Decimal("900"))
    result = validate_recommendation(r, _CONFIG, buy_decision_config=_BUY_DECISION_CONFIG)
    assert not any(v.check_name == "buy_action_consistency_violation" for v in result.violations)


def test_buy_action_flagged_when_current_price_above_entry() -> None:
    r = _buy_recommendation(BuyAction.BUY, Decimal("1200"))
    result = validate_recommendation(r, _CONFIG, buy_decision_config=_BUY_DECISION_CONFIG)
    assert result.requires_manual_review
    assert any(v.check_name == "buy_action_consistency_violation" for v in result.violations)


def test_buy_action_not_checked_without_buy_decision_config() -> None:
    r = _buy_recommendation(BuyAction.BUY, Decimal("1200"))
    result = validate_recommendation(r, _CONFIG)  # buy_decision_config省略
    assert not any(v.check_name == "buy_action_consistency_violation" for v in result.violations)


def test_non_buy_recommendation_skips_buy_check() -> None:
    r = _recommendation(RecommendationType.SELL, Decimal("1000"))
    result = validate_recommendation(r, _CONFIG, buy_decision_config=_BUY_DECISION_CONFIG)
    assert not any(v.check_name == "buy_action_consistency_violation" for v in result.violations)
