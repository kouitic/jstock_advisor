import datetime as dt

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import (
    EarningsDateStatus,
    EarningsDecisionRelevance,
    EarningsReleaseConfirmationState,
    EarningsWindowStatus,
    FinancialPeriodEndSource,
    RecentPeriodsSource,
    RecommendationType,
)
from jstock_advisor.domain.signals.earnings_window import (
    evaluate_earnings_window,
    recommend_earnings_aware_action,
    resolve_earnings_decision_relevance,
    resolve_earnings_release_confirmation,
    resolve_latest_financial_period_end,
)
from jstock_advisor.interfaces.types import FinancialSummary, QuarterlyFinancials

_TEST_SOURCE = DataSourceReference(
    provider="test-fixture", fetched_at=dt.datetime(2026, 8, 6, tzinfo=dt.UTC)
)


def _financial(
    fiscal_period_end: dt.date | None,
    quarter_ends: list[dt.date],
    recent_periods_source: RecentPeriodsSource = RecentPeriodsSource.UNAVAILABLE,
) -> FinancialSummary:
    return FinancialSummary(
        stock_code="2914",
        fiscal_period_end=fiscal_period_end,
        recent_quarters=[
            QuarterlyFinancials(stock_code="2914", quarter_end=q, source=_TEST_SOURCE)
            for q in quarter_ends
        ],
        recent_periods_source=recent_periods_source,
        source=_TEST_SOURCE,
    )

_CONFIG = load_config().earnings_window
_APP_CONFIG = load_config()
_CALENDAR = BusinessCalendar.from_config(_APP_CONFIG.holiday_calendar)
_AS_OF = dt.date(2026, 7, 27)  # 月曜日


def test_no_earnings_info_returns_none_status() -> None:
    result = evaluate_earnings_window(_AS_OF, _CALENDAR, _CONFIG)
    assert result.status == EarningsWindowStatus.NONE
    assert result.business_days_to_next_earnings is None
    assert result.days_since_latest_quarter_end is None


def test_earnings_within_window_is_approaching() -> None:
    next_earnings = _CALENDAR.add_business_days(_AS_OF, 3)
    result = evaluate_earnings_window(
        _AS_OF, _CALENDAR, _CONFIG, next_earnings_date=next_earnings
    )
    assert result.status == EarningsWindowStatus.APPROACHING_EARNINGS
    assert result.business_days_to_next_earnings == 3


def test_earnings_beyond_window_is_none() -> None:
    next_earnings = _CALENDAR.add_business_days(_AS_OF, 30)
    result = evaluate_earnings_window(
        _AS_OF, _CALENDAR, _CONFIG, next_earnings_date=next_earnings
    )
    assert result.status == EarningsWindowStatus.NONE


def test_recently_ended_quarter_is_recently_reported() -> None:
    latest_quarter_end = _AS_OF - dt.timedelta(days=5)
    result = evaluate_earnings_window(
        _AS_OF, _CALENDAR, _CONFIG, latest_quarter_end=latest_quarter_end
    )
    assert result.status == EarningsWindowStatus.RECENTLY_REPORTED
    assert result.days_since_latest_quarter_end == 5


def test_old_quarter_end_is_none() -> None:
    latest_quarter_end = _AS_OF - dt.timedelta(days=90)
    result = evaluate_earnings_window(
        _AS_OF, _CALENDAR, _CONFIG, latest_quarter_end=latest_quarter_end
    )
    assert result.status == EarningsWindowStatus.NONE


def test_future_quarter_end_is_ignored() -> None:
    latest_quarter_end = _AS_OF + dt.timedelta(days=5)
    result = evaluate_earnings_window(
        _AS_OF, _CALENDAR, _CONFIG, latest_quarter_end=latest_quarter_end
    )
    assert result.days_since_latest_quarter_end is None


def test_approaching_earnings_takes_priority_over_recently_reported() -> None:
    next_earnings = _CALENDAR.add_business_days(_AS_OF, 2)
    latest_quarter_end = _AS_OF - dt.timedelta(days=2)
    result = evaluate_earnings_window(
        _AS_OF,
        _CALENDAR,
        _CONFIG,
        next_earnings_date=next_earnings,
        latest_quarter_end=latest_quarter_end,
    )
    assert result.status == EarningsWindowStatus.APPROACHING_EARNINGS


def _window(status: EarningsWindowStatus) -> object:
    from jstock_advisor.domain.signals.earnings_window import EarningsWindowEvaluation

    return EarningsWindowEvaluation(
        status=status, business_days_to_next_earnings=None, days_since_latest_quarter_end=None
    )


def test_buy_before_earnings_becomes_watch_before_earnings() -> None:
    result = recommend_earnings_aware_action(
        RecommendationType.BUY, _window(EarningsWindowStatus.APPROACHING_EARNINGS)
    )
    assert result == RecommendationType.WATCH_BEFORE_EARNINGS


def test_profit_take_before_earnings_becomes_partial_risk_reduction() -> None:
    result = recommend_earnings_aware_action(
        RecommendationType.FULL_PROFIT_TAKE, _window(EarningsWindowStatus.APPROACHING_EARNINGS)
    )
    assert result == RecommendationType.PARTIAL_RISK_REDUCTION


def test_sell_before_earnings_is_not_suppressed() -> None:
    result = recommend_earnings_aware_action(
        RecommendationType.SELL, _window(EarningsWindowStatus.APPROACHING_EARNINGS)
    )
    assert result == RecommendationType.SELL


def test_urgent_review_before_earnings_is_not_suppressed() -> None:
    result = recommend_earnings_aware_action(
        RecommendationType.URGENT_REVIEW, _window(EarningsWindowStatus.APPROACHING_EARNINGS)
    )
    assert result == RecommendationType.URGENT_REVIEW


def test_hold_after_recent_earnings_becomes_review_after_earnings() -> None:
    result = recommend_earnings_aware_action(
        RecommendationType.HOLD, _window(EarningsWindowStatus.RECENTLY_REPORTED)
    )
    assert result == RecommendationType.REVIEW_AFTER_EARNINGS


def test_no_window_status_passes_through_unchanged() -> None:
    result = recommend_earnings_aware_action(
        RecommendationType.HOLD, _window(EarningsWindowStatus.NONE)
    )
    assert result == RecommendationType.HOLD


# ===== resolve_earnings_release_confirmation(コードレビュー対応: 明治HD事例) =====

_NOW = dt.datetime(2026, 8, 6, tzinfo=dt.UTC)
# 48時間起点(デプロイ前対応): 決算予定日(2026-08-05)の翌日JST 00:00
# = UTC 2026-08-05T15:00。
_AWAITING_STARTED_AT_UTC = dt.datetime(2026, 8, 5, 15, 0, tzinfo=dt.UTC)


def test_release_confirmation_not_applicable_when_confirmed_future_date() -> None:
    result = resolve_earnings_release_confirmation(
        EarningsDateStatus.CONFIRMED,
        dt.date(2026, 9, 1),
        dt.date(2026, 3, 31),
        _NOW,
        _NOW,
        _CONFIG,
    )
    assert result == EarningsReleaseConfirmationState.NOT_APPLICABLE


def test_release_confirmation_not_applicable_when_unavailable() -> None:
    result = resolve_earnings_release_confirmation(
        EarningsDateStatus.UNAVAILABLE, None, dt.date(2026, 3, 31), _NOW, _NOW, _CONFIG
    )
    assert result == EarningsReleaseConfirmationState.NOT_APPLICABLE


def test_release_confirmation_awaiting_when_stale_and_fiscal_period_not_updated() -> None:
    """明治HD回帰: 8/5決算予定日が経過したが、fiscal_period_endが3/31のまま
    (想定報告ラグ60日より前)であれば、財務データ未更新とみなしAWAITING_CONFIRMATION。
    """
    result = resolve_earnings_release_confirmation(
        EarningsDateStatus.STALE_PAST_DATE,
        dt.date(2026, 8, 5),
        dt.date(2026, 3, 31),
        _NOW,  # financial_fetched_at(決算予定日以後だが期末日が古いため無関係)
        _NOW,  # 8/5の翌日
        _CONFIG,
    )
    assert result == EarningsReleaseConfirmationState.AWAITING_CONFIRMATION


def test_release_confirmation_data_updated_when_fiscal_period_end_recent() -> None:
    """明治HD回帰: fiscal_period_endが6/30(8/5の想定報告ラグ60日以内)まで
    進んでおり、かつ財務データの取得時刻も決算予定日以後であれば、決算発表が
    財務データへ反映されたとみなしDATA_UPDATED。"""
    result = resolve_earnings_release_confirmation(
        EarningsDateStatus.STALE_PAST_DATE,
        dt.date(2026, 8, 5),
        dt.date(2026, 6, 30),
        _NOW,  # financial_fetched_at: 8/5以後
        _NOW,
        _CONFIG,
    )
    assert result == EarningsReleaseConfirmationState.DATA_UPDATED


def test_release_confirmation_not_data_updated_when_fetched_before_earnings_date() -> None:
    """デプロイ前対応の回帰: fiscal_period_endが報告ラグ以内でも、財務データの
    取得時刻(fetched_at)が決算予定日より前であれば、決算発表前から保持していた
    データの可能性があるためDATA_UPDATEDにしない。
    """
    fetched_before_earnings_date = dt.datetime(2026, 8, 4, tzinfo=dt.UTC)
    result = resolve_earnings_release_confirmation(
        EarningsDateStatus.STALE_PAST_DATE,
        dt.date(2026, 8, 5),
        dt.date(2026, 6, 30),  # 報告ラグ条件自体は満たす
        fetched_before_earnings_date,
        _NOW,
        _CONFIG,
    )
    assert result != EarningsReleaseConfirmationState.DATA_UPDATED
    assert result == EarningsReleaseConfirmationState.AWAITING_CONFIRMATION


def test_release_confirmation_delayed_after_maximum_wait_hours() -> None:
    """maximum_data_reflection_wait_hours(既定48時間)を超えてもfiscal_period_end
    が更新されない場合はDELAYEDへ遷移する(起点は決算予定日翌日JST 00:00)。"""
    later = _AWAITING_STARTED_AT_UTC + dt.timedelta(hours=49)
    result = resolve_earnings_release_confirmation(
        EarningsDateStatus.STALE_PAST_DATE,
        dt.date(2026, 8, 5),
        dt.date(2026, 3, 31),
        later,
        later,
        _CONFIG,
    )
    assert result == EarningsReleaseConfirmationState.DELAYED


def test_release_confirmation_not_yet_delayed_just_before_max_wait_hours() -> None:
    just_before = _AWAITING_STARTED_AT_UTC + dt.timedelta(hours=47)
    result = resolve_earnings_release_confirmation(
        EarningsDateStatus.STALE_PAST_DATE,
        dt.date(2026, 8, 5),
        dt.date(2026, 3, 31),
        just_before,
        just_before,
        _CONFIG,
    )
    assert result == EarningsReleaseConfirmationState.AWAITING_CONFIRMATION


# ===== resolve_latest_financial_period_end(デプロイ前対応) =====

_EVAL_DATE = dt.date(2026, 8, 6)


def test_resolve_latest_period_prefers_recent_quarter_within_evaluation_date() -> None:
    financial = _financial(
        dt.date(2026, 3, 31),
        [dt.date(2026, 3, 31), dt.date(2026, 6, 30)],
        recent_periods_source=RecentPeriodsSource.QUARTERLY,
    )
    result = resolve_latest_financial_period_end(financial, _EVAL_DATE)
    assert result.period_end == dt.date(2026, 6, 30)
    assert result.source == FinancialPeriodEndSource.RECENT_QUARTERLY_PERIOD


def test_resolve_latest_period_ignores_future_quarter_end() -> None:
    """recent_quartersに評価日より未来(2026-09-30)の期末日が混入しても、
    有効な最大値(2026-06-30)が採用される(未来日をProvider異常等で
    決算反映済みの証拠として採用しないための必須テスト)。
    """
    financial = _financial(
        dt.date(2026, 3, 31),
        [dt.date(2026, 3, 31), dt.date(2026, 6, 30), dt.date(2026, 9, 30)],
        recent_periods_source=RecentPeriodsSource.QUARTERLY,
    )
    result = resolve_latest_financial_period_end(financial, _EVAL_DATE)
    assert result.period_end == dt.date(2026, 6, 30)
    assert result.source == FinancialPeriodEndSource.RECENT_QUARTERLY_PERIOD


def test_resolve_latest_period_annual_fallback_source_is_labeled_distinctly() -> None:
    """由来精緻化対応: recent_quartersが実際には年次データへのフォールバック
    由来(Provider側でquarterly取得不能)だった場合、RECENT_QUARTERLY_PERIODと
    誤表示せず、RECENT_ANNUAL_FALLBACKとして区別する。
    """
    financial = _financial(
        dt.date(2026, 3, 31),
        [dt.date(2025, 3, 31), dt.date(2026, 3, 31)],
        recent_periods_source=RecentPeriodsSource.ANNUAL_FALLBACK,
    )
    result = resolve_latest_financial_period_end(financial, _EVAL_DATE)
    assert result.period_end == dt.date(2026, 3, 31)
    assert result.source == FinancialPeriodEndSource.RECENT_ANNUAL_FALLBACK


def test_resolve_latest_period_inconsistent_source_becomes_unknown() -> None:
    """由来精緻化対応: recent_quartersに有効な期間末があるのに
    recent_periods_source=UNAVAILABLEというデータ不整合が起きた場合、
    RECENT_QUARTERLY_PERIOD/RECENT_ANNUAL_FALLBACKのいずれとも誤認せずUNKNOWNと
    する(period_end自体は引き続き有効な最大値を返す。DATA_UPDATED判定条件は
    変更しないため、この状態でもDATA_UPDATEDになりうる)。
    """
    financial = _financial(
        dt.date(2026, 3, 31),
        [dt.date(2026, 6, 30)],
        recent_periods_source=RecentPeriodsSource.UNAVAILABLE,
    )
    result = resolve_latest_financial_period_end(financial, _EVAL_DATE)
    assert result.period_end == dt.date(2026, 6, 30)
    assert result.source == FinancialPeriodEndSource.UNKNOWN


def test_resolve_latest_period_falls_back_to_annual_when_all_quarters_future() -> None:
    financial = _financial(dt.date(2026, 3, 31), [dt.date(2026, 9, 30)])
    result = resolve_latest_financial_period_end(financial, _EVAL_DATE)
    assert result.period_end == dt.date(2026, 3, 31)
    assert result.source == FinancialPeriodEndSource.ANNUAL_FISCAL_PERIOD_END


def test_resolve_latest_period_unavailable_when_annual_also_future() -> None:
    financial = _financial(dt.date(2026, 9, 30), [dt.date(2026, 9, 30)])
    result = resolve_latest_financial_period_end(financial, _EVAL_DATE)
    assert result.period_end is None
    assert result.source == FinancialPeriodEndSource.UNAVAILABLE


def test_resolve_latest_period_falls_back_to_annual_when_no_quarters() -> None:
    financial = _financial(dt.date(2026, 3, 31), [])
    result = resolve_latest_financial_period_end(financial, _EVAL_DATE)
    assert result.period_end == dt.date(2026, 3, 31)
    assert result.source == FinancialPeriodEndSource.ANNUAL_FISCAL_PERIOD_END


def test_resolve_latest_period_unavailable_when_no_data_at_all() -> None:
    financial = _financial(None, [])
    result = resolve_latest_financial_period_end(financial, _EVAL_DATE)
    assert result.period_end is None
    assert result.source == FinancialPeriodEndSource.UNAVAILABLE


def test_release_confirmation_not_data_updated_when_period_end_unavailable() -> None:
    """latest_financial_period_end=None(取得不能)の場合、DATA_UPDATEDにならない
    (取得不能を取得日で代替しない)。"""
    result = resolve_earnings_release_confirmation(
        EarningsDateStatus.STALE_PAST_DATE,
        dt.date(2026, 8, 5),
        None,
        _NOW,
        _NOW,
        _CONFIG,
    )
    assert result != EarningsReleaseConfirmationState.DATA_UPDATED
    assert result == EarningsReleaseConfirmationState.AWAITING_CONFIRMATION


# ===== resolve_earnings_decision_relevance(デプロイ前対応: 無期限抑制の防止) =====


def test_decision_relevance_not_relevant_when_not_stale() -> None:
    result = resolve_earnings_decision_relevance(
        EarningsDateStatus.CONFIRMED,
        dt.date(2026, 9, 1),
        EarningsReleaseConfirmationState.NOT_APPLICABLE,
        dt.date(2026, 8, 6),
        _CONFIG,
    )
    assert result == EarningsDecisionRelevance.NOT_RELEVANT


def test_decision_relevance_not_relevant_when_data_updated() -> None:
    """財務データが既に決算後まで進んでいる場合は、経過日数に関わらずNOT_RELEVANT
    (通常判定へ復帰してよい)。"""
    result = resolve_earnings_decision_relevance(
        EarningsDateStatus.STALE_PAST_DATE,
        dt.date(2026, 8, 5),
        EarningsReleaseConfirmationState.DATA_UPDATED,
        dt.date(2026, 8, 6),
        _CONFIG,
    )
    assert result == EarningsDecisionRelevance.NOT_RELEVANT


def test_decision_relevance_relevant_when_recent_and_unconfirmed() -> None:
    """直近過去日(翌日)で財務更新未確認ならRELEVANT(通常利確提案を保留)。"""
    result = resolve_earnings_decision_relevance(
        EarningsDateStatus.STALE_PAST_DATE,
        dt.date(2026, 8, 5),
        EarningsReleaseConfirmationState.AWAITING_CONFIRMATION,
        dt.date(2026, 8, 6),
        _CONFIG,
    )
    assert result == EarningsDecisionRelevance.RELEVANT


def test_decision_relevance_unknown_when_far_past_and_still_unconfirmed() -> None:
    """デプロイ前対応の回帰: 6か月前の過去日をProviderが返し続け、財務データも
    更新されないまま(AWAITING/DELAYED)の場合、stale_earnings_relevance_days
    (既定10日)を大きく超えているためUNKNOWNとし、通常判定を無期限に止めない。
    """
    result = resolve_earnings_decision_relevance(
        EarningsDateStatus.STALE_PAST_DATE,
        dt.date(2026, 2, 5),  # 評価日の6か月前
        EarningsReleaseConfirmationState.DELAYED,
        dt.date(2026, 8, 6),
        _CONFIG,
    )
    assert result == EarningsDecisionRelevance.UNKNOWN
