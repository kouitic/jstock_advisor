"""判定・価格整合性検証(要求仕様11節)。

通知生成前に実行し、判定(recommendation_type)と価格フィールドの間に
論理的な矛盾が無いかを検証する。矛盾を検出した場合、呼び出し側は通常の
売買推奨通知の代わりにDATA_QUALITY_ALERTを送信すること。

データそのものの信頼性チェック(分割整合性・異常値検知)はservices/
data_quality_service.pyが担当し、本モジュールは「判定結果の論理が
一貫しているか」のみを検証する(責務分離)。
"""

from __future__ import annotations

from dataclasses import dataclass

from jstock_advisor.config.models import ConsistencyValidationConfig
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    PriceFieldBasis,
    RecommendationType,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.valuation import FairValueRange


@dataclass(frozen=True)
class ConsistencyViolation:
    check_name: str
    description: str
    manual_review_required: bool = False


@dataclass(frozen=True)
class ConsistencyCheckResult:
    passed: bool
    violations: list[ConsistencyViolation]

    @property
    def requires_manual_review(self) -> bool:
        return any(v.manual_review_required for v in self.violations)


_SELL_LIKE_TYPES = (RecommendationType.SELL, RecommendationType.URGENT_REVIEW)


def _check_sell_single_evidence(r: Recommendation) -> ConsistencyViolation | None:
    """SELL/URGENT_REVIEWが独立根拠グループ2件未満に基づいていないか(要求仕様§15、
    レビュー対応でTRIGGERED件数ではなくindependent_evidence_group_countを使用)。

    ただし、一次情報確認済みの即時性criticalが存在する場合は、単一グループでも
    URGENT_REVIEWを許可する(要求仕様レビュー対応の例外)。
    """
    if r.recommendation_type not in _SELL_LIKE_TYPES:
        return None

    has_confirmed_immediate_critical = any(
        e.get("status") == "TRIGGERED"
        and e.get("is_immediate_critical")
        and e.get("primary_source_confirmed")
        for e in r.evidence_details
    )
    if (
        r.recommendation_type == RecommendationType.URGENT_REVIEW
        and has_confirmed_immediate_critical
    ):
        return None

    if r.independent_evidence_group_count is None:
        return None
    if r.independent_evidence_group_count < 2:
        return ConsistencyViolation(
            "sell_based_on_single_evidence",
            f"{r.recommendation_type.value}の独立根拠グループが"
            f"{r.independent_evidence_group_count}件しかない(独立した複数の根拠が必要)",
            manual_review_required=True,
        )
    return None


def _check_high_confidence_insufficient_groups(r: Recommendation) -> ConsistencyViolation | None:
    """HIGH信頼度なのに独立根拠グループが2件未満でないか(要求仕様§6・§15)。"""
    if r.confidence != ConfidenceLevel.HIGH:
        return None
    if r.independent_evidence_group_count is None:
        return None
    if r.independent_evidence_group_count < 2:
        return ConsistencyViolation(
            "high_confidence_insufficient_evidence_groups",
            f"信頼度HIGHだが独立根拠グループが{r.independent_evidence_group_count}件しかない",
            manual_review_required=True,
        )
    return None


def _check_sell_based_on_yfinance_only(r: Recommendation) -> ConsistencyViolation | None:
    """SELL/URGENT_REVIEWの根拠がyfinance等の二次情報のみでないか(要求仕様§12・§15)。"""
    if r.recommendation_type not in _SELL_LIKE_TYPES:
        return None
    triggered = [e for e in r.evidence_details if e.get("status") == "TRIGGERED"]
    if not triggered:
        return None
    if all(not e.get("primary_source_confirmed") for e in triggered):
        return ConsistencyViolation(
            "sell_based_on_secondary_source_only",
            f"{r.recommendation_type.value}の根拠がすべて一次情報未確認(yfinance等の二次情報)のみ",
            manual_review_required=True,
        )
    return None


def _check_sell_price_equals_current_as_future_condition(
    r: Recommendation,
) -> ConsistencyViolation | None:
    """成立済みの現在値がそのまま将来の再判断条件として提示されていないか(要求仕様§8)。"""
    if r.sell_prices is None:
        return None
    for name, field in (
        ("stop_review_price", r.sell_prices.stop_review_price),
        ("reevaluation_price_upside", r.sell_prices.reevaluation_price_upside),
        ("reevaluation_price_downside", r.sell_prices.reevaluation_price_downside),
    ):
        if field is not None and field.price == r.price_at_recommendation:
            return ConsistencyViolation(
                "future_condition_equals_current_price",
                f"{name}が現在値と同一だが、将来の再判断条件として提示されている"
                "(すでに成立している条件を将来条件として表示しない)",
                manual_review_required=True,
            )
    return None


def _check_review_retains_immediate_execution_price(
    r: Recommendation,
) -> ConsistencyViolation | None:
    """REVIEW判定(自動的な売却判断を行わない)なのに、即時執行を意味する価格が
    残っていないか(要求仕様レビュー対応: SELL/URGENT_REVIEWからの格下げ後、
    元の強い行動提案の価格だけが残る矛盾を防ぐ)。
    """
    if r.recommendation_type != RecommendationType.REVIEW or r.sell_prices is None:
        return None
    if r.sell_prices.immediate_execution_price is not None:
        return ConsistencyViolation(
            "review_retains_immediate_execution_price",
            "REVIEW判定(自動売却判断を行わない)なのに、即時執行目安価格が"
            "設定されたままになっている",
            manual_review_required=True,
        )
    for name, field in (
        ("recommended_limit_price", r.sell_prices.recommended_limit_price),
        ("stop_review_price", r.sell_prices.stop_review_price),
    ):
        if field is not None and field.basis == PriceFieldBasis.IMMEDIATE_EXECUTION_REFERENCE:
            return ConsistencyViolation(
                "review_retains_immediate_execution_price",
                f"REVIEW判定なのに、{name}が即時執行目安(IMMEDIATE_EXECUTION_REFERENCE)"
                "のまま残っている",
                manual_review_required=True,
            )
    return None


def _check_full_take_extreme_margin(
    r: Recommendation, config: ConsistencyValidationConfig
) -> ConsistencyViolation | None:
    if r.recommendation_type != RecommendationType.FULL_PROFIT_TAKE or r.sell_prices is None:
        return None
    full_take = r.sell_prices.full_profit_consideration_price
    if full_take is None or r.price_at_recommendation <= 0:
        return None
    margin_pct = float(full_take.price / r.price_at_recommendation - 1) * 100
    if margin_pct >= config.full_take_extreme_margin_pct:
        return ConsistencyViolation(
            "full_take_extreme_margin",
            f"全株利確検討価格({full_take.price}円)が現在値"
            f"({r.price_at_recommendation}円)より{margin_pct:.0f}%も高く、極端に乖離している",
        )
    return None


def _check_full_take_missing_all_price_guidance(r: Recommendation) -> ConsistencyViolation | None:
    if r.recommendation_type != RecommendationType.FULL_PROFIT_TAKE or r.sell_prices is None:
        return None
    sp = r.sell_prices
    if sp.recommended_limit_price is None and sp.full_profit_consideration_price is None:
        return ConsistencyViolation(
            "full_take_no_price_guidance",
            "FULL_PROFIT_TAKEなのに、指値候補も全株利確検討価格も算出されていない",
        )
    return None


def _check_watch_immediate_execution(r: Recommendation) -> ConsistencyViolation | None:
    """WATCH(監視)判定に、即時売却・即時利確を意味する価格が残っていないか確認する
    (要求仕様レビュー対応: recommended_limit_priceだけでなく、partial_profit_start_price・
    immediate_execution_price・stop_review_priceも確認する)。
    """
    if r.recommendation_type != RecommendationType.WATCH or r.sell_prices is None:
        return None
    sp = r.sell_prices
    if sp.immediate_execution_price is not None:
        return ConsistencyViolation(
            "watch_recommends_immediate_sell",
            "WATCH(監視)判定なのに、即時執行価格が設定されている",
        )
    for name, field in (
        ("partial_profit_start_price", sp.partial_profit_start_price),
        ("recommended_limit_price", sp.recommended_limit_price),
        ("stop_review_price", sp.stop_review_price),
    ):
        if field is not None and field.basis == PriceFieldBasis.IMMEDIATE_EXECUTION_REFERENCE:
            return ConsistencyViolation(
                "watch_recommends_immediate_sell",
                f"WATCH(監視)判定なのに、{name}が即時執行目安として提示されている",
            )
    return None


def _check_three_or_more_equal_prices(r: Recommendation) -> ConsistencyViolation | None:
    if r.sell_prices is None:
        return None
    sp = r.sell_prices
    fields = [
        sp.partial_profit_start_price,
        sp.recommended_limit_price,
        sp.full_profit_consideration_price,
        sp.reevaluation_price_upside,
    ]
    values = [f.price for f in fields if f is not None]
    if len(values) < 3:
        return None
    most_common = max(values.count(v) for v in set(values))
    if most_common >= 3:
        return ConsistencyViolation(
            "three_or_more_equal_prices",
            "理由なく3つ以上の価格フィールドが同一の値になっている",
        )
    return None


def _check_reevaluation_unreasonably_above_full_take(
    r: Recommendation, config: ConsistencyValidationConfig
) -> ConsistencyViolation | None:
    if r.sell_prices is None:
        return None
    reeval = r.sell_prices.reevaluation_price_upside
    full_take = r.sell_prices.full_profit_consideration_price
    if reeval is None or full_take is None or full_take.price <= 0:
        return None
    margin_pct = float(reeval.price / full_take.price - 1) * 100
    if margin_pct >= config.reevaluation_vs_full_take_max_margin_pct:
        return ConsistencyViolation(
            "reevaluation_unreasonably_above_full_take",
            f"上昇時再評価価格({reeval.price}円)が全株利確検討価格"
            f"({full_take.price}円)より{margin_pct:.0f}%も高く、不合理な水準",
        )
    return None


def _check_low_fair_value_confidence_full_take(
    r: Recommendation, fair_value_range: FairValueRange | None
) -> ConsistencyViolation | None:
    if r.recommendation_type != RecommendationType.FULL_PROFIT_TAKE:
        return None
    if fair_value_range is None:
        return None
    if fair_value_range.overall_confidence == ConfidenceLevel.LOW and any(
        "適正価格" in reason for reason in r.reasons
    ):
        return ConsistencyViolation(
            "low_fair_value_confidence_full_take",
            "適正価格の信頼度がLOWなのに、適正価格を根拠にFULL_PROFIT_TAKEを出している",
        )
    return None


def _check_gain_below_threshold_full_take(
    r: Recommendation, config: ConsistencyValidationConfig, gain_full_threshold_pct: float
) -> ConsistencyViolation | None:
    if r.recommendation_type != RecommendationType.FULL_PROFIT_TAKE:
        return None
    avg = r.average_purchase_price_at_recommendation
    if avg is None or avg <= 0:
        return None
    gain_pct = float(r.price_at_recommendation / avg - 1) * 100
    min_reasons = config.min_reasons_for_full_take_on_gain_alone
    if gain_pct < gain_full_threshold_pct and len(r.reasons) < min_reasons:
        return ConsistencyViolation(
            "full_take_with_insufficient_gain_and_reasons",
            f"含み益率({gain_pct:.1f}%)が全株利確閾値未満なのに、根拠が{len(r.reasons)}件しかない",
        )
    return None


def _check_yield_sufficient_full_take_on_yield_alone(
    r: Recommendation, min_yield_pct: float
) -> ConsistencyViolation | None:
    if r.recommendation_type != RecommendationType.FULL_PROFIT_TAKE:
        return None
    yield_pct = r.total_yield_pct_at_recommendation
    if yield_pct is None or yield_pct < min_yield_pct:
        return None
    yield_reasons = [reason for reason in r.reasons if "利回り" in reason]
    if yield_reasons and len(yield_reasons) == len(r.reasons):
        return ConsistencyViolation(
            "sufficient_yield_full_take_on_yield_alone",
            f"総合利回り({yield_pct:.2f}%)が最低基準以上なのに、"
            "利回り低下のみを根拠にFULL_PROFIT_TAKEを出している",
        )
    return None


def _check_price_equals_current_with_wrong_basis(
    r: Recommendation,
) -> ConsistencyViolation | None:
    if r.sell_prices is None:
        return None
    for name, field in (
        ("partial_profit_start_price", r.sell_prices.partial_profit_start_price),
        ("recommended_limit_price", r.sell_prices.recommended_limit_price),
    ):
        if (
            field is not None
            and field.price == r.price_at_recommendation
            and field.basis == PriceFieldBasis.TARGET_PRICE
        ):
            return ConsistencyViolation(
                "price_equals_current_with_target_basis",
                f"{name}が現在値と一致しているが、根拠(basis)が"
                "「目標価格」のままになっている(即時執行目安等に区分すべき)",
            )
    return None


def validate_recommendation(
    recommendation: Recommendation,
    config: ConsistencyValidationConfig,
    fair_value_range: FairValueRange | None = None,
    gain_full_threshold_pct: float = 50.0,
    min_yield_pct: float = 2.5,
) -> ConsistencyCheckResult:
    checks = [
        _check_full_take_extreme_margin(recommendation, config),
        _check_full_take_missing_all_price_guidance(recommendation),
        _check_watch_immediate_execution(recommendation),
        _check_three_or_more_equal_prices(recommendation),
        _check_reevaluation_unreasonably_above_full_take(recommendation, config),
        _check_low_fair_value_confidence_full_take(recommendation, fair_value_range),
        _check_gain_below_threshold_full_take(recommendation, config, gain_full_threshold_pct),
        _check_yield_sufficient_full_take_on_yield_alone(recommendation, min_yield_pct),
        _check_price_equals_current_with_wrong_basis(recommendation),
        _check_sell_single_evidence(recommendation),
        _check_high_confidence_insufficient_groups(recommendation),
        _check_sell_based_on_yfinance_only(recommendation),
        _check_sell_price_equals_current_as_future_condition(recommendation),
        _check_review_retains_immediate_execution_price(recommendation),
    ]
    violations = [c for c in checks if c is not None]
    return ConsistencyCheckResult(passed=not violations, violations=violations)
