import datetime as dt
from dataclasses import replace
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.classification import StockTypeClassification
from jstock_advisor.domain.entities.enums import ConfidenceLevel, StockType
from jstock_advisor.domain.signals.watchlist_screening import (
    ExclusionReason,
    HardExclusionCode,
    HighDividendFinancialHealthPolicy,
    MatchedCriterion,
    MultiStyleMonitoringPolicy,
    categorize_exclusion_reasons,
)
from jstock_advisor.services.screening_data_provider import WatchlistScreeningInput

_APP_CONFIG = load_config()
_CONFIG = _APP_CONFIG.watchlist_screening
_POLICY = HighDividendFinancialHealthPolicy()


def _classification(
    *types: StockType, basis: list[str] | None = None
) -> StockTypeClassification:
    return StockTypeClassification(
        stock_code="1234",
        classified_at=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        types=list(types),
        primary_type=types[0] if types else None,
        confidence=ConfidenceLevel.MEDIUM,
        classification_basis=basis or [f"{t.value}該当" for t in types],
        data_sources=[],
    )


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
        stock_type_classification=_classification(StockType.INCOME),
        avg_trading_value=Decimal("100000000"),
        disclosure_risk_keywords_found=[],
        severe_earnings_decline=False,
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


# ============================================================================
# MultiStyleMonitoringPolicy(ウォッチリスト自動追加基準の再設計、2026-08)
# 「高配当だけでなく、連続増配・成長・割安・優良株を対象とし、重大リスク以外は
# 過度にハード除外しない」という下流(BUY候補判定・保有銘柄の売却基準)と同じ
# 方針をウォッチリスト自動追加へも適用する。
# ============================================================================

_MS_POLICY = MultiStyleMonitoringPolicy(_APP_CONFIG.screening)


def test_a_income_only_passes() -> None:
    """A: 高配当株。INCOMEのみ該当→PASS。"""
    result = _MS_POLICY.evaluate(
        _good_input(stock_type_classification=_classification(StockType.INCOME)), _CONFIG
    )
    assert result.passed is True
    assert result.matched_criteria == [MatchedCriterion.TARGET_INCOME]


def test_b_growth_only_passes_even_with_zero_dividend() -> None:
    """B: 成長株。dividend_yield=0%でもGROWTH該当→PASS(低配当だから不合格にならない)。"""
    result = _MS_POLICY.evaluate(
        _good_input(
            dividend_yield_pct=0.0,
            stock_type_classification=_classification(StockType.GROWTH),
        ),
        _CONFIG,
    )
    assert result.passed is True
    assert result.matched_criteria == [MatchedCriterion.TARGET_GROWTH]


def test_c_value_only_passes_with_low_dividend() -> None:
    """C: 割安株。低配当・PBR/PER条件でVALUE→PASS。"""
    result = _MS_POLICY.evaluate(
        _good_input(
            dividend_yield_pct=0.5,
            stock_type_classification=_classification(StockType.VALUE),
        ),
        _CONFIG,
    )
    assert result.passed is True
    assert result.matched_criteria == [MatchedCriterion.TARGET_VALUE]


def test_d_dividend_growth_only_passes() -> None:
    """D: 連続増配株。DIVIDEND_GROWTH該当→PASS。"""
    result = _MS_POLICY.evaluate(
        _good_input(stock_type_classification=_classification(StockType.DIVIDEND_GROWTH)),
        _CONFIG,
    )
    assert result.passed is True
    assert result.matched_criteria == [MatchedCriterion.TARGET_DIVIDEND_GROWTH]


def test_e_quality_only_passes() -> None:
    """E: 優良株。QUALITY該当→PASS。"""
    result = _MS_POLICY.evaluate(
        _good_input(stock_type_classification=_classification(StockType.QUALITY)), _CONFIG
    )
    assert result.passed is True
    assert result.matched_criteria == [MatchedCriterion.TARGET_QUALITY]


def test_f_composite_type_scores_higher_than_single_type() -> None:
    """F: 複合型。INCOME+DIVIDEND_GROWTH+QUALITY→単一タイプよりMonitoringScoreが高い。"""
    single = _MS_POLICY.evaluate(
        _good_input(stock_type_classification=_classification(StockType.INCOME)), _CONFIG
    )
    composite = _MS_POLICY.evaluate(
        _good_input(
            stock_type_classification=_classification(
                StockType.INCOME, StockType.DIVIDEND_GROWTH, StockType.QUALITY
            )
        ),
        _CONFIG,
    )
    assert composite.passed is True
    assert composite.score > single.score
    assert set(composite.matched_criteria) == {
        MatchedCriterion.TARGET_INCOME,
        MatchedCriterion.TARGET_DIVIDEND_GROWTH,
        MatchedCriterion.TARGET_QUALITY,
    }


def test_g_single_year_deficit_growth_is_not_hard_excluded() -> None:
    """G: 単年度赤字のGROWTH。債務超過なし・継続企業疑義なし・GROWTH該当
    →赤字だけではハード除外されない。"""
    result = _MS_POLICY.evaluate(
        _good_input(
            is_deficit=True,
            is_debt_excess=False,
            is_going_concern_doubt=False,
            stock_type_classification=_classification(StockType.GROWTH),
        ),
        _CONFIG,
    )
    assert result.passed is True
    assert "no_deficit_bonus" not in result.score_breakdown


def test_h_negative_operating_cashflow_value_is_not_hard_excluded() -> None:
    """H: 営業CFマイナスのVALUE。重大リスクなし・VALUE該当→CFマイナスだけでは
    全タイプ共通除外されない。"""
    result = _MS_POLICY.evaluate(
        _good_input(
            operating_cashflow=Decimal("-1"),
            stock_type_classification=_classification(StockType.VALUE),
        ),
        _CONFIG,
    )
    assert result.passed is True
    assert "cashflow_bonus" not in result.score_breakdown


def test_i_recent_dividend_cut_value_growth_still_passes() -> None:
    """I: 直近減配のVALUE/GROWTH→配当タイプとしては弱くても、VALUE/GROWTHなら
    追加候補になり得る。"""
    result = _MS_POLICY.evaluate(
        _good_input(
            is_dividend_cut_announced=True,
            stock_type_classification=_classification(StockType.VALUE, StockType.GROWTH),
        ),
        _CONFIG,
    )
    assert result.passed is True
    assert "no_dividend_cut_bonus" not in result.score_breakdown


def test_j_debt_excess_fails_even_if_type_matched() -> None:
    """J: 債務超過→StockTypeに該当してもFAIL。"""
    result = _MS_POLICY.evaluate(
        _good_input(
            is_debt_excess=True,
            stock_type_classification=_classification(StockType.QUALITY),
        ),
        _CONFIG,
    )
    assert result.passed is False
    assert result.exclusion_reasons == [ExclusionReason.HARD_EXCLUDED]
    assert "債務超過" in result.hard_exclusion_reasons
    assert result.hard_exclusion_codes == [HardExclusionCode.NEGATIVE_EQUITY]


def test_k_going_concern_doubt_fails() -> None:
    """K: 継続企業疑義→FAIL。"""
    result = _MS_POLICY.evaluate(
        _good_input(
            is_going_concern_doubt=True,
            stock_type_classification=_classification(StockType.INCOME),
        ),
        _CONFIG,
    )
    assert result.passed is False
    assert any("継続企業" in reason for reason in result.hard_exclusion_reasons)
    assert result.hard_exclusion_codes == [HardExclusionCode.GOING_CONCERN_DOUBT]


def test_l_disclosure_risk_keyword_fails() -> None:
    """L: 重大不祥事/上場廃止リスク→FAIL。"""
    result = _MS_POLICY.evaluate(
        _good_input(
            disclosure_risk_keywords_found=["不適切会計"],
            stock_type_classification=_classification(StockType.INCOME),
        ),
        _CONFIG,
    )
    assert result.passed is False
    assert any("リスクキーワード" in reason for reason in result.hard_exclusion_reasons)
    assert result.hard_exclusion_codes == [HardExclusionCode.DISCLOSURE_RISK]


def test_m_illiquid_stock_fails_with_same_hard_condition_as_buy() -> None:
    """M: 流動性不足→downstream BUYと同じハード条件(config.screening.universe.
    min_avg_trading_value_20d_yen)でFAIL。"""
    min_value = _APP_CONFIG.screening.universe.min_avg_trading_value_20d_yen
    result = _MS_POLICY.evaluate(
        _good_input(
            avg_trading_value=Decimal(min_value - 1),
            stock_type_classification=_classification(StockType.INCOME),
        ),
        _CONFIG,
    )
    assert result.passed is False
    assert any("平均売買代金" in reason for reason in result.hard_exclusion_reasons)
    assert result.hard_exclusion_codes == [HardExclusionCode.INSUFFICIENT_LIQUIDITY]


def test_n_etf_fails() -> None:
    """N: ETF/REIT→FAIL。"""
    result = _MS_POLICY.evaluate(
        _good_input(
            security_type="ETF",
            stock_type_classification=_classification(StockType.INCOME),
        ),
        _CONFIG,
    )
    assert result.passed is False
    assert any("ETF" in reason for reason in result.hard_exclusion_reasons)
    assert result.hard_exclusion_codes == [HardExclusionCode.ETF_EXCLUDED]


def test_n_reit_fails() -> None:
    result = _MS_POLICY.evaluate(
        _good_input(
            security_type="REIT",
            stock_type_classification=_classification(StockType.INCOME),
        ),
        _CONFIG,
    )
    assert result.passed is False
    assert any("REIT" in reason for reason in result.hard_exclusion_reasons)
    assert result.hard_exclusion_codes == [HardExclusionCode.REIT_EXCLUDED]


def test_o_missing_dividend_data_but_value_classifiable_passes_not_data_insufficient() -> None:
    """O: 配当データ一部欠損だがVALUE評価可能→DATA_INSUFFICIENTにせずPASS。"""
    result = _MS_POLICY.evaluate(
        _good_input(
            dividend_yield_pct=None,
            consecutive_dividend_increase_years=None,
            shareholder_benefit_exists=False,
            shareholder_benefit_yield_pct=None,
            missing_scoring_fields=[
                "dividend_yield_pct",
                "consecutive_dividend_increase_years",
                "shareholder_benefit_yield_pct",
            ],
            stock_type_classification=_classification(StockType.VALUE),
        ),
        _CONFIG,
    )
    assert result.passed is True
    assert ExclusionReason.DATA_INSUFFICIENT not in result.exclusion_reasons


def test_p_zero_matched_types_fails_with_explicit_reason() -> None:
    """P: 0タイプ。重大リスク無しでも→FAILED_NO_TARGET_TYPE等の明確な理由でFAIL。"""
    result = _MS_POLICY.evaluate(
        _good_input(stock_type_classification=_classification()), _CONFIG
    )
    assert result.passed is False
    assert result.exclusion_reasons == [ExclusionReason.FAILED_NO_TARGET_TYPE]
    _, evaluation_result = categorize_exclusion_reasons(result.exclusion_reasons)
    assert evaluation_result == "FAILED_NO_TARGET_TYPE"


def test_q_production_default_disables_watchlist_addition_line_notification() -> None:
    """Q: 自動ウォッチリスト追加が発生してもLINE送信0件(本番既定notification_enabled=false)。
    追加内容自体はAudit(record_candidate_audit/record_repository_result_audit)には
    従来どおり残る(このテストではconfig既定値のみ確認する。実際のAudit記録経路は
    test_watchlist_screening_audit.pyで別途検証済み)。
    """
    assert _CONFIG.notification_enabled is False
    assert _CONFIG.screening_policy == "multi_style_monitoring"


def test_categorize_exclusion_reasons_hard_excluded_maps_to_required_condition_failed() -> None:
    category, evaluation_result = categorize_exclusion_reasons([ExclusionReason.HARD_EXCLUDED])
    assert category == "required_condition_failed"
    assert evaluation_result == "FAILED_REQUIRED"


def test_r_downstream_buy_screening_hard_exclusion_unaffected() -> None:
    """R: downstream isolation。今回の変更はBUY一次スクリーニング
    (domain/screening/rules.py::evaluate_screening)のコード自体には一切触れて
    いないため、同一の入力に対する挙動が変化しないことを直接確認する
    (BuySignalService.decide_buy_action等の上流には影響しない設計であることの
    裏付け)。"""
    import datetime as _dt

    from jstock_advisor.domain.business_calendar import BusinessCalendar
    from jstock_advisor.domain.entities.common import DataSourceReference
    from jstock_advisor.domain.screening.rules import evaluate_screening
    from jstock_advisor.interfaces.types import FinancialSummary

    calendar = BusinessCalendar.from_config(_APP_CONFIG.holiday_calendar)
    now = _dt.datetime(2026, 8, 1, tzinfo=_dt.UTC)
    financial = FinancialSummary(
        stock_code="1234",
        security_type="STOCK",
        source=DataSourceReference(provider="test", fetched_at=now),
    )

    result = evaluate_screening(
        financial=financial,
        dividend=None,
        average_trading_value_yen=Decimal("100000000"),
        disclosure_risk_keywords_found=[],
        data_fetched_at=now,
        now=now,
        business_calendar=calendar,
        config=_APP_CONFIG.screening,
    )
    assert result.passed is True
    assert result.exclusion_reasons == []


# --- Issue #29: 金融業hard exclusion(classify_industryベース) ----------------
# BUY一次スクリーニング(test_screening.py)と同じ除外方針であることを
# watchlist経路でも固定する(片経路だけ直すと、通常screeningでは除外されるが
# watchlist自動追加では流入する不整合が残るため)。


def test_financial_bank_hard_excluded_like_8306() -> None:
    result = _MS_POLICY.evaluate(
        _good_input(sector="Financial Services", industry="Banks - Diversified"),
        _CONFIG,
    )
    assert result.passed is False
    assert HardExclusionCode.UNSUPPORTED_INDUSTRY in result.hard_exclusion_codes
    assert any("BANKING" in reason for reason in result.hard_exclusion_reasons)


def test_financial_securities_hard_excluded_like_8604() -> None:
    result = _MS_POLICY.evaluate(
        _good_input(sector="Financial Services", industry="Capital Markets"),
        _CONFIG,
    )
    assert result.passed is False
    assert HardExclusionCode.UNSUPPORTED_INDUSTRY in result.hard_exclusion_codes


def test_financial_insurance_hard_excluded_like_8766() -> None:
    result = _MS_POLICY.evaluate(
        _good_input(sector="Financial Services", industry="Insurance - Property & Casualty"),
        _CONFIG,
    )
    assert result.passed is False
    assert HardExclusionCode.UNSUPPORTED_INDUSTRY in result.hard_exclusion_codes


def test_other_financial_lease_not_hard_excluded_by_default() -> None:
    """OTHER_FINANCIAL(リース・Credit Services等)は既定ではhard exclusionしない。"""
    result = _MS_POLICY.evaluate(
        _good_input(sector="Financial Services", industry="Credit Services"),
        _CONFIG,
    )
    assert HardExclusionCode.UNSUPPORTED_INDUSTRY not in result.hard_exclusion_codes


def test_unknown_sector_not_hard_excluded() -> None:
    """UNKNOWN(sector欠損)を金融業と推測してhard exclusionしない。"""
    result = _MS_POLICY.evaluate(
        _good_input(sector=None, industry="銀行業"),
        _CONFIG,
    )
    assert HardExclusionCode.UNSUPPORTED_INDUSTRY not in result.hard_exclusion_codes
