from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import ConfidenceLevel, JudgmentStrength
from jstock_advisor.domain.signals.judgment_safety_ladder import (
    JudgmentSafetyInputs,
    cap_judgment_strength,
    max_allowed_strength,
)

_CONFIG = load_config().confidence


def _clean_inputs(**overrides: object) -> JudgmentSafetyInputs:
    base: dict[str, object] = {
        "data_quality_score": 100.0,
        "corporate_action_unresolved": False,
        "key_data_missing": False,
        "consistency_error": False,
        "primary_source_confirmed_material_fact": True,
        "latest_earnings_age_days": 30,
        "days_to_next_earnings_business_days": 60,
        "fair_value_confidence": ConfidenceLevel.HIGH,
        "single_rule_only": False,
    }
    base.update(overrides)
    return JudgmentSafetyInputs(**base)  # type: ignore[arg-type]


def test_clean_inputs_allow_urgent_review() -> None:
    allowed, reasons = max_allowed_strength(_clean_inputs(), _CONFIG)
    assert allowed == JudgmentStrength.URGENT_REVIEW
    assert reasons == []


def test_low_data_quality_score_blocks_strong_action() -> None:
    allowed, reasons = max_allowed_strength(
        _clean_inputs(data_quality_score=50.0), _CONFIG
    )
    assert allowed == JudgmentStrength.PARTIAL_ACTION
    assert reasons


def test_near_earnings_blocks_strong_action() -> None:
    allowed, reasons = max_allowed_strength(
        _clean_inputs(days_to_next_earnings_business_days=2), _CONFIG
    )
    assert allowed == JudgmentStrength.PARTIAL_ACTION


def test_low_fair_value_confidence_blocks_strong_action() -> None:
    allowed, reasons = max_allowed_strength(
        _clean_inputs(fair_value_confidence=ConfidenceLevel.LOW), _CONFIG
    )
    assert allowed == JudgmentStrength.PARTIAL_ACTION


def test_single_rule_only_blocks_strong_action() -> None:
    allowed, reasons = max_allowed_strength(_clean_inputs(single_rule_only=True), _CONFIG)
    assert allowed == JudgmentStrength.PARTIAL_ACTION


def test_cap_judgment_strength_downgrades_full_action_when_unsafe() -> None:
    capped, reasons = cap_judgment_strength(
        JudgmentStrength.FULL_ACTION, _clean_inputs(key_data_missing=True), _CONFIG
    )
    assert capped == JudgmentStrength.PARTIAL_ACTION
    assert reasons


def test_cap_judgment_strength_keeps_full_action_when_safe() -> None:
    capped, reasons = cap_judgment_strength(
        JudgmentStrength.FULL_ACTION, _clean_inputs(), _CONFIG
    )
    assert capped == JudgmentStrength.FULL_ACTION
    assert reasons == []


def test_cap_judgment_strength_leaves_weak_levels_unaffected() -> None:
    capped, reasons = cap_judgment_strength(
        JudgmentStrength.WATCH, _clean_inputs(key_data_missing=True), _CONFIG
    )
    assert capped == JudgmentStrength.WATCH
    assert reasons == []
