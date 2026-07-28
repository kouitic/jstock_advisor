from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.signals.confidence_scoring import (
    ConfidenceFactors,
    compute_confidence,
)

_CONFIG = load_config().confidence


def test_perfect_factors_yield_high_confidence() -> None:
    factors = ConfidenceFactors(
        data_freshness_days=0,
        primary_source_fetch_rate=1.0,
        corporate_action_adjustment_consistent=True,
        financial_period_comparable=True,
        fair_value_method_spread_ratio=1.1,
        days_to_next_earnings_business_days=30,
        latest_quarter_fetched=True,
        days_since_last_split=None,
        split_adjustment_confirmed=True,
        record_date_known=True,
        key_metric_missing=False,
        primary_secondary_conflict=False,
        one_time_factors_identified=True,
        cross_rule_agreement=True,
    )
    result = compute_confidence(factors, _CONFIG)
    assert result.level == ConfidenceLevel.HIGH
    assert result.reasons_not_high == []


def test_near_earnings_disallows_high_even_with_perfect_score() -> None:
    factors = ConfidenceFactors(
        data_freshness_days=0,
        primary_source_fetch_rate=1.0,
        corporate_action_adjustment_consistent=True,
        financial_period_comparable=True,
        fair_value_method_spread_ratio=1.1,
        days_to_next_earnings_business_days=3,  # 5営業日以内
        latest_quarter_fetched=True,
        record_date_known=True,
        one_time_factors_identified=True,
        cross_rule_agreement=True,
    )
    result = compute_confidence(factors, _CONFIG)
    assert result.level != ConfidenceLevel.HIGH
    assert result.reasons_not_high


def test_unknown_record_date_disallows_high() -> None:
    factors = ConfidenceFactors(
        data_freshness_days=0,
        primary_source_fetch_rate=1.0,
        corporate_action_adjustment_consistent=True,
        financial_period_comparable=True,
        fair_value_method_spread_ratio=1.1,
        latest_quarter_fetched=True,
        record_date_known=False,
        one_time_factors_identified=True,
        cross_rule_agreement=True,
    )
    result = compute_confidence(factors, _CONFIG)
    assert result.level != ConfidenceLevel.HIGH


def test_large_method_spread_disallows_high() -> None:
    factors = ConfidenceFactors(
        data_freshness_days=0,
        primary_source_fetch_rate=1.0,
        fair_value_method_spread_ratio=3.0,  # 2倍以上
        latest_quarter_fetched=True,
        record_date_known=True,
        one_time_factors_identified=True,
        cross_rule_agreement=True,
    )
    result = compute_confidence(factors, _CONFIG)
    assert result.level != ConfidenceLevel.HIGH


def test_unconfirmed_split_adjustment_disallows_high() -> None:
    factors = ConfidenceFactors(
        data_freshness_days=0,
        primary_source_fetch_rate=1.0,
        latest_quarter_fetched=True,
        record_date_known=True,
        days_since_last_split=100,
        split_adjustment_confirmed=False,
        one_time_factors_identified=True,
        cross_rule_agreement=True,
    )
    result = compute_confidence(factors, _CONFIG)
    assert result.level != ConfidenceLevel.HIGH


def test_stale_data_and_missing_metrics_lower_score_to_low() -> None:
    factors = ConfidenceFactors(
        data_freshness_days=30,
        primary_source_fetch_rate=0.1,
        corporate_action_adjustment_consistent=False,
        financial_period_comparable=False,
        key_metric_missing=True,
        one_time_factors_identified=False,
        cross_rule_agreement=False,
    )
    result = compute_confidence(factors, _CONFIG)
    assert result.level == ConfidenceLevel.LOW
    assert len(result.reasons_not_high) >= 3


def test_no_factors_provided_defaults_are_not_high() -> None:
    # デフォルト(全てNone/False)では、判定不能な項目が多く安全側でHIGHにならない
    result = compute_confidence(ConfidenceFactors(), _CONFIG)
    assert result.level != ConfidenceLevel.HIGH
