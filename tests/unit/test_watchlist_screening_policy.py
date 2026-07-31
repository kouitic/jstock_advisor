from dataclasses import replace
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.signals.watchlist_screening import (
    ExclusionReason,
    HighDividendFinancialHealthPolicy,
    MatchedCriterion,
    categorize_exclusion_reasons,
)
from jstock_advisor.services.screening_data_provider import WatchlistScreeningInput

_CONFIG = load_config().watchlist_screening
_POLICY = HighDividendFinancialHealthPolicy()


def _good_input(**overrides: object) -> WatchlistScreeningInput:
    defaults = WatchlistScreeningInput(
        stock_code="1234",
        stock_name="テスト株式会社",
        security_type="STOCK",
        sector="Consumer",
        industry="Retail",
        current_price=Decimal("3000"),
        shares_outstanding=Decimal("40000000"),  # 時価総額1200億円(閾値500億円以上)
        market_cap=Decimal("40000000") * Decimal("3000"),
        forecast_eps=Decimal("150"),
        forecast_bps=Decimal("2000"),
        current_per=Decimal("20"),
        current_pbr=Decimal("1.5"),
        equity_ratio_pct=60.0,
        operating_cashflow=Decimal("1000000000"),
        payout_ratio_pct=40.0,
        consecutive_dividend_increase_years=5,
        dividend_yield_pct=5.0,
        shareholder_benefit_exists=True,
        shareholder_benefit_yield_pct=1.0,
        is_dividend_cut_announced=False,
        is_dividend_omission_announced=False,
        is_debt_excess=False,
        is_deficit=False,
        is_going_concern_doubt=False,
        next_earnings_date=None,
        missing_required_fields=[],
        missing_scoring_fields=[],
    )
    return replace(defaults, **overrides)  # type: ignore[arg-type]


def test_good_stock_passes_with_score_above_threshold() -> None:
    result = _POLICY.evaluate(_good_input(), _CONFIG)
    assert result.passed is True
    assert result.score >= _CONFIG.scoring.minimum_total_score
    assert result.exclusion_reasons == []


def test_missing_required_fields_short_circuits_to_data_insufficient() -> None:
    result = _POLICY.evaluate(
        _good_input(missing_required_fields=["shares_outstanding"]), _CONFIG
    )
    assert result.passed is False
    assert result.exclusion_reasons == [ExclusionReason.DATA_INSUFFICIENT]
    assert result.score == 0.0
    assert result.matched_criteria == []


def test_market_cap_below_threshold_fails_required() -> None:
    below_threshold = Decimal(_CONFIG.thresholds.minimum_market_cap_yen - 1)
    result = _POLICY.evaluate(_good_input(market_cap=below_threshold), _CONFIG)
    assert result.passed is False
    assert ExclusionReason.MARKET_CAP_BELOW_THRESHOLD in result.exclusion_reasons


def test_negative_operating_cashflow_fails_required() -> None:
    result = _POLICY.evaluate(
        _good_input(operating_cashflow=Decimal("-1000")), _CONFIG
    )
    assert result.passed is False
    assert ExclusionReason.NEGATIVE_OPERATING_CASHFLOW in result.exclusion_reasons


def test_dividend_cut_announced_fails_required() -> None:
    result = _POLICY.evaluate(_good_input(is_dividend_cut_announced=True), _CONFIG)
    assert result.passed is False
    assert ExclusionReason.SEVERE_DIVIDEND_CUT in result.exclusion_reasons


def test_dividend_omission_announced_fails_required() -> None:
    result = _POLICY.evaluate(_good_input(is_dividend_omission_announced=True), _CONFIG)
    assert result.passed is False
    assert ExclusionReason.SEVERE_DIVIDEND_CUT in result.exclusion_reasons


def test_debt_excess_fails_required() -> None:
    result = _POLICY.evaluate(_good_input(is_debt_excess=True), _CONFIG)
    assert result.passed is False
    assert ExclusionReason.DEBT_EXCESS in result.exclusion_reasons


def test_deficit_fails_required() -> None:
    result = _POLICY.evaluate(_good_input(is_deficit=True), _CONFIG)
    assert result.passed is False
    assert ExclusionReason.DEFICIT in result.exclusion_reasons


def test_going_concern_doubt_fails_required() -> None:
    result = _POLICY.evaluate(_good_input(is_going_concern_doubt=True), _CONFIG)
    assert result.passed is False
    assert ExclusionReason.GOING_CONCERN_DOUBT in result.exclusion_reasons


def test_etf_security_type_is_excluded() -> None:
    result = _POLICY.evaluate(_good_input(security_type="ETF"), _CONFIG)
    assert result.passed is False
    assert ExclusionReason.EXCLUDED_SECURITY_TYPE in result.exclusion_reasons


def test_reit_security_type_is_excluded() -> None:
    result = _POLICY.evaluate(_good_input(security_type="REIT"), _CONFIG)
    assert result.passed is False
    assert ExclusionReason.EXCLUDED_SECURITY_TYPE in result.exclusion_reasons


def test_score_below_threshold_fails_even_when_required_conditions_pass() -> None:
    weak_input = _good_input(
        dividend_yield_pct=0.0,
        equity_ratio_pct=None,
        payout_ratio_pct=None,
        consecutive_dividend_increase_years=None,
        shareholder_benefit_exists=False,
        shareholder_benefit_yield_pct=None,
        missing_scoring_fields=[
            "equity_ratio_pct",
            "payout_ratio_pct",
            "consecutive_dividend_increase_years",
        ],
    )
    result = _POLICY.evaluate(weak_input, _CONFIG)
    assert result.passed is False
    assert ExclusionReason.SCORE_BELOW_THRESHOLD in result.exclusion_reasons
    assert result.score < _CONFIG.scoring.minimum_total_score


def test_too_many_missing_scoring_fields_marks_data_insufficient_even_if_score_high() -> None:
    """max_missing_fieldsを超える欠損があれば、たまたまスコアが閾値以上でも
    データ不足として不合格にする(根拠データが乏しいまま自動追加しない安全策)。
    """
    many_missing = [
        "dividend_yield_pct",
        "equity_ratio_pct",
        "payout_ratio_pct",
    ]
    assert len(many_missing) > _CONFIG.max_missing_fields
    result = _POLICY.evaluate(
        _good_input(missing_scoring_fields=many_missing), _CONFIG
    )
    assert ExclusionReason.DATA_INSUFFICIENT in result.exclusion_reasons
    assert result.passed is False


def test_high_dividend_yield_matched_criterion_present_when_above_threshold() -> None:
    result = _POLICY.evaluate(_good_input(dividend_yield_pct=5.0), _CONFIG)
    assert MatchedCriterion.HIGH_DIVIDEND_YIELD in result.matched_criteria


def test_dividend_yield_below_threshold_no_matched_criterion_and_zero_score_component() -> None:
    result = _POLICY.evaluate(_good_input(dividend_yield_pct=1.0), _CONFIG)
    assert MatchedCriterion.HIGH_DIVIDEND_YIELD not in result.matched_criteria
    assert result.score_breakdown["dividend_yield"] == 0.0


def test_shareholder_benefit_presence_only_scores_half_of_full_weight() -> None:
    result = _POLICY.evaluate(
        _good_input(shareholder_benefit_exists=True, shareholder_benefit_yield_pct=None),
        _CONFIG,
    )
    full_weight = _CONFIG.scoring.shareholder_benefit.weight
    ratio = _CONFIG.scoring.shareholder_benefit.presence_only_score_ratio
    assert result.score_breakdown["shareholder_benefit"] == full_weight * ratio
    assert MatchedCriterion.SHAREHOLDER_BENEFIT in result.matched_criteria


def test_no_shareholder_benefit_scores_zero_and_no_matched_criterion() -> None:
    result = _POLICY.evaluate(
        _good_input(shareholder_benefit_exists=False, shareholder_benefit_yield_pct=None),
        _CONFIG,
    )
    assert result.score_breakdown["shareholder_benefit"] == 0.0
    assert MatchedCriterion.SHAREHOLDER_BENEFIT not in result.matched_criteria


def test_zero_consecutive_dividend_increase_years_scores_zero() -> None:
    result = _POLICY.evaluate(
        _good_input(consecutive_dividend_increase_years=0), _CONFIG
    )
    assert result.score_breakdown["dividend_growth"] == 0.0
    assert MatchedCriterion.DIVIDEND_GROWTH_TRACK_RECORD not in result.matched_criteria


def test_payout_ratio_within_healthy_range_scores_full_points() -> None:
    result = _POLICY.evaluate(_good_input(payout_ratio_pct=40.0), _CONFIG)
    assert result.score_breakdown["payout_ratio"] == _CONFIG.scoring.payout_ratio.weight
    assert MatchedCriterion.HEALTHY_PAYOUT_RATIO in result.matched_criteria


def test_payout_ratio_far_outside_healthy_range_scores_less_than_full() -> None:
    result = _POLICY.evaluate(_good_input(payout_ratio_pct=95.0), _CONFIG)
    assert result.score_breakdown["payout_ratio"] < _CONFIG.scoring.payout_ratio.weight
    assert MatchedCriterion.HEALTHY_PAYOUT_RATIO not in result.matched_criteria


def test_categorize_exclusion_reasons_prioritizes_data_insufficient() -> None:
    reasons = [ExclusionReason.DATA_INSUFFICIENT, ExclusionReason.MARKET_CAP_BELOW_THRESHOLD]
    category, evaluation_result = categorize_exclusion_reasons(reasons)
    assert category == "data_insufficient"
    assert evaluation_result == "DATA_INSUFFICIENT"


def test_categorize_exclusion_reasons_required_before_score() -> None:
    reasons = [ExclusionReason.DEBT_EXCESS, ExclusionReason.SCORE_BELOW_THRESHOLD]
    category, evaluation_result = categorize_exclusion_reasons(reasons)
    assert category == "required_condition_failed"
    assert evaluation_result == "FAILED_REQUIRED"


def test_categorize_exclusion_reasons_score_only() -> None:
    category, evaluation_result = categorize_exclusion_reasons(
        [ExclusionReason.SCORE_BELOW_THRESHOLD]
    )
    assert category == "score_failed"
    assert evaluation_result == "FAILED_SCORE"


def test_categorize_exclusion_reasons_empty_means_passed() -> None:
    category, evaluation_result = categorize_exclusion_reasons([])
    assert category == "passed"
    assert evaluation_result == "PASSED"
