from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import BUY_FAMILY_ACTIONS, BuyAction, ConfidenceLevel
from jstock_advisor.domain.screening.rules import ScreeningResult
from jstock_advisor.domain.signals.buy_decision import (
    compute_purchase_attractiveness_score,
    decide_buy_action,
    screen_investment_universe,
)

_CONFIG = load_config().buy_decision


def _levels(entry: str, standard: str, strong: str) -> BuyPriceLevels:
    return BuyPriceLevels(
        entry=PriceWithRationale(price=Decimal(entry), rationale="x"),
        standard=PriceWithRationale(price=Decimal(standard), rationale="x"),
        strong=PriceWithRationale(price=Decimal(strong), rationale="x"),
    )


_LEVELS = _levels("1000", "900", "800")


def test_screen_investment_universe_passes_when_no_exclusions() -> None:
    result = screen_investment_universe(
        ScreeningResult(passed=True, exclusion_reasons=[], warnings=[]), False, None
    )
    assert result.passed is True


def test_screen_investment_universe_excludes_on_severe_earnings_decline() -> None:
    result = screen_investment_universe(
        ScreeningResult(passed=True, exclusion_reasons=[], warnings=[]), True, None
    )
    assert result.passed is False
    assert result.exclusion_reasons


def test_current_price_above_entry_price_never_buy_family() -> None:
    decision = decide_buy_action(
        current_price=Decimal("1001"),
        buy_price_levels=_LEVELS,
        company_quality_score=90.0,
        business_days_to_earnings=30,
        valuation_dispersion_ratio=1.1,
        config=_CONFIG,
    )
    assert decision.action not in BUY_FAMILY_ACTIONS
    assert decision.action == BuyAction.WATCH_FOR_PRICE


def test_price_at_or_below_strong_price_is_strong_buy_candidate() -> None:
    decision = decide_buy_action(
        current_price=Decimal("800"),
        buy_price_levels=_LEVELS,
        company_quality_score=90.0,
        business_days_to_earnings=30,
        valuation_dispersion_ratio=1.1,
        config=_CONFIG,
    )
    assert decision.action == BuyAction.STRONG_BUY


def test_price_at_standard_is_buy_candidate() -> None:
    decision = decide_buy_action(
        current_price=Decimal("900"),
        buy_price_levels=_LEVELS,
        company_quality_score=90.0,
        business_days_to_earnings=30,
        valuation_dispersion_ratio=1.1,
        config=_CONFIG,
    )
    assert decision.action == BuyAction.BUY


def test_price_at_entry_is_small_entry_candidate() -> None:
    decision = decide_buy_action(
        current_price=Decimal("1000"),
        buy_price_levels=_LEVELS,
        company_quality_score=90.0,
        business_days_to_earnings=30,
        valuation_dispersion_ratio=1.1,
        config=_CONFIG,
    )
    assert decision.action == BuyAction.SMALL_ENTRY


def test_score_alone_never_upgrades_action() -> None:
    """価格条件を満たさない(WATCH_FOR_PRICE)場合、スコアが満点でも昇格しない。"""
    decision = decide_buy_action(
        current_price=Decimal("2000"),
        buy_price_levels=_LEVELS,
        company_quality_score=100.0,
        business_days_to_earnings=30,
        valuation_dispersion_ratio=1.1,
        config=_CONFIG,
    )
    assert decision.action == BuyAction.WATCH_FOR_PRICE


def test_price_condition_met_but_low_score_downgrades_to_watch() -> None:
    decision = decide_buy_action(
        current_price=Decimal("800"),  # STRONG_BUY相当の価格
        buy_price_levels=_LEVELS,
        company_quality_score=50.0,  # small_entry閾値(55)未満
        business_days_to_earnings=30,
        valuation_dispersion_ratio=1.1,
        config=_CONFIG,
    )
    assert decision.action == BuyAction.WATCH_FOR_PRICE
    assert decision.raw_action == BuyAction.STRONG_BUY


def test_very_low_score_forces_not_attractive_regardless_of_price() -> None:
    decision = decide_buy_action(
        current_price=Decimal("800"),
        buy_price_levels=_LEVELS,
        company_quality_score=40.0,  # watch閾値(45)未満
        business_days_to_earnings=30,
        valuation_dispersion_ratio=1.1,
        config=_CONFIG,
    )
    assert decision.action == BuyAction.NOT_ATTRACTIVE


def test_earnings_within_3_business_days_forces_watch_before_earnings() -> None:
    decision = decide_buy_action(
        current_price=Decimal("800"),
        buy_price_levels=_LEVELS,
        company_quality_score=90.0,
        business_days_to_earnings=2,
        valuation_dispersion_ratio=1.1,
        config=_CONFIG,
    )
    assert decision.action == BuyAction.WATCH_BEFORE_EARNINGS


def test_earnings_within_7_business_days_does_not_block_action() -> None:
    """4〜7営業日は安全余裕率加算(margin_of_safety側)のみで、actionはブロックしない。"""
    decision = decide_buy_action(
        current_price=Decimal("800"),
        buy_price_levels=_LEVELS,
        company_quality_score=90.0,
        business_days_to_earnings=6,
        valuation_dispersion_ratio=1.1,
        config=_CONFIG,
    )
    assert decision.action == BuyAction.STRONG_BUY


def test_dispersion_above_2_00_forces_manual_review_when_would_be_buy() -> None:
    decision = decide_buy_action(
        current_price=Decimal("800"),
        buy_price_levels=_LEVELS,
        company_quality_score=90.0,
        business_days_to_earnings=30,
        valuation_dispersion_ratio=2.5,
        config=_CONFIG,
    )
    assert decision.action == BuyAction.MANUAL_REVIEW


def test_dispersion_above_2_00_does_not_escalate_watch_to_manual_review() -> None:
    decision = decide_buy_action(
        current_price=Decimal("2000"),  # WATCH_FOR_PRICE相当
        buy_price_levels=_LEVELS,
        company_quality_score=90.0,
        business_days_to_earnings=30,
        valuation_dispersion_ratio=2.5,
        config=_CONFIG,
    )
    assert decision.action == BuyAction.WATCH_FOR_PRICE


def test_no_valuation_anchor_defaults_to_watch_for_price() -> None:
    decision = decide_buy_action(
        current_price=Decimal("800"),
        buy_price_levels=BuyPriceLevels(),
        company_quality_score=90.0,
        business_days_to_earnings=30,
        valuation_dispersion_ratio=None,
        config=_CONFIG,
    )
    assert decision.action == BuyAction.WATCH_FOR_PRICE


def test_purchase_attractiveness_score_higher_when_price_lower() -> None:
    high_score = compute_purchase_attractiveness_score(
        current_price=Decimal("800"),
        buy_price_levels=_LEVELS,
        valuation_confidence=ConfidenceLevel.HIGH,
        dispersion_band="LOW",
        business_days_to_earnings=30,
        recent_price_change_pct=None,
        industry_model_applied=True,
        data_quality_warning=False,
        config=_CONFIG,
    )
    low_score = compute_purchase_attractiveness_score(
        current_price=Decimal("1200"),
        buy_price_levels=_LEVELS,
        valuation_confidence=ConfidenceLevel.HIGH,
        dispersion_band="LOW",
        business_days_to_earnings=30,
        recent_price_change_pct=None,
        industry_model_applied=True,
        data_quality_warning=False,
        config=_CONFIG,
    )
    assert high_score > low_score


def test_purchase_attractiveness_score_lower_when_high_company_quality_but_price_high() -> None:
    """企業魅力度が高くても、現在値が高い場合はpurchase_attractiveness_scoreを低くする
    (このスコア自体はcompany_quality_scoreを引数に取らないため、価格位置だけで
    独立して低くなることを確認する)。
    """
    score = compute_purchase_attractiveness_score(
        current_price=Decimal("1200"),  # entry(1000)を大きく上回る
        buy_price_levels=_LEVELS,
        valuation_confidence=ConfidenceLevel.HIGH,
        dispersion_band="LOW",
        business_days_to_earnings=30,
        recent_price_change_pct=None,
        industry_model_applied=True,
        data_quality_warning=False,
        config=_CONFIG,
    )
    assert score < 50.0
