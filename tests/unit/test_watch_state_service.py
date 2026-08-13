import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import NearBuyConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import BuyAction, WatchTransitionType, WatchType
from jstock_advisor.infrastructure.local_repository.watch_state_repository import (
    WatchStateRepository,
)
from jstock_advisor.services.watch_state_service import (
    END_REASON_PRICE_OUT_OF_RANGE,
    END_REASON_PROMOTED_TO_BUY,
    END_REASON_STALE,
    WatchStateService,
)

_CALENDAR = BusinessCalendar.from_config(load_config().holiday_calendar)
_CONFIG = NearBuyConfig(
    start_required_decline_pct=10.0,
    continue_required_decline_pct=12.0,
    min_company_quality_score=60.0,
    daily_max_notifications=5,
    max_stale_business_days=5,
)

# 2026-08-17は月曜日
_MON = dt.date(2026, 8, 17)
_TUE = dt.date(2026, 8, 18)
_WED = dt.date(2026, 8, 19)


def _service(tmp_path: Path) -> WatchStateService:
    repo = WatchStateRepository(store_dir=tmp_path)
    return WatchStateService(business_calendar=_CALENDAR, repository=repo)


def test_starts_new_watch_state_on_first_qualifying_day(tmp_path: Path) -> None:
    service = _service(tmp_path)
    result = service.evaluate_and_update(
        stock_code="9432",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        company_quality_score=65.0,
        required_decline_to_entry_pct=Decimal("8.0"),
        current_price=Decimal("158"),
        entry_price=Decimal("150"),
        today=_MON,
        config=_CONFIG,
    )
    assert result.watch_type == WatchType.NEAR_BUY
    assert result.consecutive_business_days == 1
    assert result.transition_type == WatchTransitionType.STARTED
    assert result.previous_consecutive_business_days is None


def test_does_not_start_when_quality_score_below_threshold(tmp_path: Path) -> None:
    service = _service(tmp_path)
    result = service.evaluate_and_update(
        stock_code="9432",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        company_quality_score=59.9,
        required_decline_to_entry_pct=Decimal("5.0"),
        current_price=Decimal("158"),
        entry_price=Decimal("150"),
        today=_MON,
        config=_CONFIG,
    )
    assert result.watch_type is None
    assert result.consecutive_business_days is None
    assert result.transition_type == WatchTransitionType.NONE


def test_continues_and_increments_on_next_business_day(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.evaluate_and_update(
        stock_code="9432",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        company_quality_score=65.0,
        required_decline_to_entry_pct=Decimal("8.0"),
        current_price=Decimal("158"),
        entry_price=Decimal("150"),
        today=_MON,
        config=_CONFIG,
    )
    result = service.evaluate_and_update(
        stock_code="9432",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        company_quality_score=65.0,
        required_decline_to_entry_pct=Decimal("6.0"),
        current_price=Decimal("155"),
        entry_price=Decimal("150"),
        today=_TUE,
        config=_CONFIG,
    )
    assert result.watch_type == WatchType.NEAR_BUY
    assert result.consecutive_business_days == 2
    assert result.transition_type == WatchTransitionType.CONTINUED
    assert result.previous_consecutive_business_days == 1


def test_ends_when_required_decline_exceeds_continue_threshold(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.evaluate_and_update(
        stock_code="9432",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        company_quality_score=65.0,
        required_decline_to_entry_pct=Decimal("8.0"),
        current_price=Decimal("158"),
        entry_price=Decimal("150"),
        today=_MON,
        config=_CONFIG,
    )
    result = service.evaluate_and_update(
        stock_code="9432",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        company_quality_score=65.0,
        required_decline_to_entry_pct=Decimal("12.1"),
        current_price=Decimal("170"),
        entry_price=Decimal("150"),
        today=_TUE,
        config=_CONFIG,
    )
    assert result.watch_type == WatchType.NEAR_BUY  # どの監視種別が終了したかは残る
    assert result.consecutive_business_days is None
    assert result.transition_type == WatchTransitionType.ENDED
    assert result.end_reason == END_REASON_PRICE_OUT_OF_RANGE
    assert result.previous_consecutive_business_days == 1
    state = service._repo.get_active("9432", WatchType.NEAR_BUY)
    assert state is None  # 終了済み


def test_ends_with_promoted_reason_when_buy_family_reached(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.evaluate_and_update(
        stock_code="9432",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        company_quality_score=65.0,
        required_decline_to_entry_pct=Decimal("8.0"),
        current_price=Decimal("158"),
        entry_price=Decimal("150"),
        today=_MON,
        config=_CONFIG,
    )
    result = service.evaluate_and_update(
        stock_code="9432",
        buy_action=BuyAction.BUY,
        company_quality_score=65.0,
        required_decline_to_entry_pct=None,
        current_price=Decimal("148"),
        entry_price=Decimal("150"),
        today=_TUE,
        config=_CONFIG,
    )
    assert result.watch_type == WatchType.NEAR_BUY  # どの監視種別が終了したかは残る
    assert result.consecutive_business_days is None
    assert result.transition_type == WatchTransitionType.PROMOTED_TO_BUY
    assert result.previous_consecutive_business_days == 1
    assert result.end_reason == END_REASON_PROMOTED_TO_BUY
    all_states = service._repo.list_all()
    ended = next(s for s in all_states if s.stock_code == "9432")
    assert ended.end_reason == END_REASON_PROMOTED_TO_BUY


def test_gap_resets_consecutive_days_but_keeps_watch_state(tmp_path: Path) -> None:
    """指摘8のA案: 評価不能を挟んでもWatchState自体は維持し、表示用の連続日数のみ
    1へリセットする(started_atは不変)。ここではMON開始→WED再評価(TUEは評価不能で
    一切呼ばれない)という間隔を再現する。"""
    service = _service(tmp_path)
    service.evaluate_and_update(
        stock_code="9432",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        company_quality_score=65.0,
        required_decline_to_entry_pct=Decimal("8.0"),
        current_price=Decimal("158"),
        entry_price=Decimal("150"),
        today=_MON,
        config=_CONFIG,
    )
    # TUEはDATA_INSUFFICIENTのため評価自体を呼ばない(呼び出し元の既存動作)。
    result = service.evaluate_and_update(
        stock_code="9432",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        company_quality_score=65.0,
        required_decline_to_entry_pct=Decimal("7.0"),
        current_price=Decimal("156"),
        entry_price=Decimal("150"),
        today=_WED,
        config=_CONFIG,
    )
    assert result.watch_type == WatchType.NEAR_BUY
    assert result.consecutive_business_days == 1  # 連続日数はリセットされる
    assert result.transition_type == WatchTransitionType.RESUMED
    assert result.previous_consecutive_business_days == 1
    state = service._repo.get_active("9432", WatchType.NEAR_BUY)
    assert state is not None
    assert state.started_at == _MON  # started_atは不変


def test_price_out_of_range_reason_recorded(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.evaluate_and_update(
        stock_code="9432",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        company_quality_score=65.0,
        required_decline_to_entry_pct=Decimal("8.0"),
        current_price=Decimal("158"),
        entry_price=Decimal("150"),
        today=_MON,
        config=_CONFIG,
    )
    service.evaluate_and_update(
        stock_code="9432",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        company_quality_score=65.0,
        required_decline_to_entry_pct=Decimal("20.0"),
        current_price=Decimal("180"),
        entry_price=Decimal("150"),
        today=_TUE,
        config=_CONFIG,
    )
    ended = next(s for s in service._repo.list_all() if s.stock_code == "9432")
    assert ended.end_reason == END_REASON_PRICE_OUT_OF_RANGE


def test_stale_ends_watch_and_does_not_restart_same_day(tmp_path: Path) -> None:
    """コードレビュー対応2026-08: STALE終了と同一営業日内に開始条件を満たしても
    即座に再開させない(WATCH終了通知の意味を保つため)。"""
    service = _service(tmp_path)
    service.evaluate_and_update(
        stock_code="9432",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        company_quality_score=65.0,
        required_decline_to_entry_pct=Decimal("8.0"),
        current_price=Decimal("158"),
        entry_price=Decimal("150"),
        today=_MON,
        config=_CONFIG,
    )
    far_future = _MON + dt.timedelta(days=60)
    result = service.evaluate_and_update(
        stock_code="9432",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        company_quality_score=65.0,
        required_decline_to_entry_pct=Decimal("8.0"),
        current_price=Decimal("158"),
        entry_price=Decimal("150"),
        today=far_future,
        config=_CONFIG,
    )
    assert result.watch_type == WatchType.NEAR_BUY  # どの監視種別が終了したかは残る
    assert result.consecutive_business_days is None
    assert result.transition_type == WatchTransitionType.ENDED
    assert result.end_reason == END_REASON_STALE


def test_promoted_to_buy_takes_priority_over_stale_after_gap(tmp_path: Path) -> None:
    """コードレビュー対応2026-08(指摘1): NEAR BUYを数営業日継続後、評価不能期間が
    max_staleを超過し、次に評価できた営業日にBUY水準へ到達した場合は、
    STALE終了ではなくPROMOTED_TO_BUYとして扱う(監視終了通知とBUY到達通知の
    二重送信を防ぐため)。
    """
    service = _service(tmp_path)
    service.evaluate_and_update(
        stock_code="9432",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        company_quality_score=65.0,
        required_decline_to_entry_pct=Decimal("8.0"),
        current_price=Decimal("158"),
        entry_price=Decimal("150"),
        today=_MON,
        config=_CONFIG,
    )
    far_future = _MON + dt.timedelta(days=60)  # max_stale_business_daysを確実に超過する間隔
    result = service.evaluate_and_update(
        stock_code="9432",
        buy_action=BuyAction.BUY,  # 評価再開日に買い水準へ到達
        company_quality_score=65.0,
        required_decline_to_entry_pct=None,
        current_price=Decimal("148"),
        entry_price=Decimal("150"),
        today=far_future,
        config=_CONFIG,
    )
    assert result.transition_type == WatchTransitionType.PROMOTED_TO_BUY
    assert result.end_reason == END_REASON_PROMOTED_TO_BUY
    assert result.previous_consecutive_business_days == 1
    state = service._repo.get_active("9432", WatchType.NEAR_BUY)
    assert state is None  # 終了済み(STALEとしては終了していない)
    all_states = service._repo.list_all()
    ended = next(s for s in all_states if s.stock_code == "9432")
    assert ended.end_reason == END_REASON_PROMOTED_TO_BUY
    assert result.previous_consecutive_business_days == 1
    state = service._repo.get_active("9432", WatchType.NEAR_BUY)
    assert state is None
