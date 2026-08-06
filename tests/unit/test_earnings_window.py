import datetime as dt

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import (
    EarningsDateStatus,
    EarningsReleaseConfirmationState,
    EarningsWindowStatus,
    RecommendationType,
)
from jstock_advisor.domain.signals.earnings_window import (
    evaluate_earnings_window,
    recommend_earnings_aware_action,
    resolve_earnings_release_confirmation,
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


def test_release_confirmation_not_applicable_when_confirmed_future_date() -> None:
    result = resolve_earnings_release_confirmation(
        EarningsDateStatus.CONFIRMED, dt.date(2026, 9, 1), dt.date(2026, 3, 31), _NOW, _CONFIG
    )
    assert result == EarningsReleaseConfirmationState.NOT_APPLICABLE


def test_release_confirmation_not_applicable_when_unavailable() -> None:
    result = resolve_earnings_release_confirmation(
        EarningsDateStatus.UNAVAILABLE, None, dt.date(2026, 3, 31), _NOW, _CONFIG
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
        _NOW,  # 8/5の翌日
        _CONFIG,
    )
    assert result == EarningsReleaseConfirmationState.AWAITING_CONFIRMATION


def test_release_confirmation_data_updated_when_fiscal_period_end_recent() -> None:
    """明治HD回帰: fiscal_period_endが6/30(8/5の想定報告ラグ60日以内)まで
    進んでいれば、決算発表が財務データへ反映されたとみなしDATA_UPDATED。"""
    result = resolve_earnings_release_confirmation(
        EarningsDateStatus.STALE_PAST_DATE,
        dt.date(2026, 8, 5),
        dt.date(2026, 6, 30),
        _NOW,
        _CONFIG,
    )
    assert result == EarningsReleaseConfirmationState.DATA_UPDATED


def test_release_confirmation_delayed_after_maximum_wait_hours() -> None:
    """maximum_data_reflection_wait_hours(既定48時間)を超えてもfiscal_period_end
    が更新されない場合はDELAYEDへ遷移する。"""
    later = dt.datetime(2026, 8, 8, 1, 0, tzinfo=dt.UTC)  # 8/5 00:00から49時間後
    result = resolve_earnings_release_confirmation(
        EarningsDateStatus.STALE_PAST_DATE,
        dt.date(2026, 8, 5),
        dt.date(2026, 3, 31),
        later,
        _CONFIG,
    )
    assert result == EarningsReleaseConfirmationState.DELAYED


def test_release_confirmation_not_yet_delayed_just_before_max_wait_hours() -> None:
    just_before = dt.datetime(2026, 8, 6, 23, 0, tzinfo=dt.UTC)  # 8/5 00:00から47時間後
    result = resolve_earnings_release_confirmation(
        EarningsDateStatus.STALE_PAST_DATE,
        dt.date(2026, 8, 5),
        dt.date(2026, 3, 31),
        just_before,
        _CONFIG,
    )
    assert result == EarningsReleaseConfirmationState.AWAITING_CONFIRMATION
