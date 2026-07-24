from jstock_advisor.config.models import EvaluationRulesConfig, ExitEvaluationThresholds
from jstock_advisor.domain.entities.enums import EvaluationLabel, RecommendationType
from jstock_advisor.domain.evaluation_rules import determine_evaluation_label

_CONFIG = EvaluationRulesConfig(
    version=1,
    severe_decline_after_buy_pct=-15.0,
    exit_evaluation=ExitEvaluationThresholds(
        decline_confirms_good_call_pct=-5.0,
        rally_flags_too_early_or_too_sensitive_pct=10.0,
    ),
)


def test_data_issue_when_price_return_missing() -> None:
    label, _ = determine_evaluation_label(RecommendationType.BUY, None, None, None, _CONFIG)
    assert label == EvaluationLabel.DATA_ISSUE


def test_buy_success_when_positive_return_and_excess() -> None:
    label, _ = determine_evaluation_label(RecommendationType.BUY, 10.0, 3.0, -2.0, _CONFIG)
    assert label == EvaluationLabel.SUCCESS


def test_buy_acceptable_when_positive_return_no_excess() -> None:
    label, _ = determine_evaluation_label(RecommendationType.BUY, 10.0, -1.0, -2.0, _CONFIG)
    assert label == EvaluationLabel.ACCEPTABLE


def test_buy_acceptable_when_excess_return_unknown() -> None:
    label, _ = determine_evaluation_label(RecommendationType.BUY, 10.0, None, -2.0, _CONFIG)
    assert label == EvaluationLabel.ACCEPTABLE


def test_buy_price_too_high_when_negative_return() -> None:
    label, _ = determine_evaluation_label(RecommendationType.BUY, -3.0, None, -3.0, _CONFIG)
    assert label == EvaluationLabel.PRICE_TOO_HIGH


def test_buy_risk_underestimated_when_severe_drawdown() -> None:
    label, _ = determine_evaluation_label(RecommendationType.BUY, 5.0, 2.0, -20.0, _CONFIG)
    assert label == EvaluationLabel.RISK_UNDERESTIMATED


def test_hold_uses_entry_logic_too() -> None:
    label, _ = determine_evaluation_label(RecommendationType.HOLD, 5.0, 1.0, -2.0, _CONFIG)
    assert label == EvaluationLabel.SUCCESS


def test_profit_take_success_when_price_declines() -> None:
    label, _ = determine_evaluation_label(
        RecommendationType.PARTIAL_PROFIT_TAKE, -8.0, None, None, _CONFIG
    )
    assert label == EvaluationLabel.SUCCESS


def test_profit_take_too_early_when_price_rallies() -> None:
    label, _ = determine_evaluation_label(
        RecommendationType.FULL_PROFIT_TAKE, 15.0, None, None, _CONFIG
    )
    assert label == EvaluationLabel.PROFIT_TAKE_TOO_EARLY


def test_sell_too_sensitive_when_price_recovers() -> None:
    label, _ = determine_evaluation_label(RecommendationType.SELL, 15.0, None, None, _CONFIG)
    assert label == EvaluationLabel.SELL_TOO_SENSITIVE


def test_urgent_review_too_sensitive_when_price_recovers() -> None:
    label, _ = determine_evaluation_label(
        RecommendationType.URGENT_REVIEW, 15.0, None, None, _CONFIG
    )
    assert label == EvaluationLabel.SELL_TOO_SENSITIVE


def test_exit_acceptable_when_flat() -> None:
    label, _ = determine_evaluation_label(RecommendationType.SELL, 0.0, None, None, _CONFIG)
    assert label == EvaluationLabel.ACCEPTABLE


def test_inconclusive_for_watch_type() -> None:
    label, _ = determine_evaluation_label(RecommendationType.WATCH, 5.0, None, None, _CONFIG)
    assert label == EvaluationLabel.INCONCLUSIVE
