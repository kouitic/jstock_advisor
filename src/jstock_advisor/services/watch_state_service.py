"""NEAR BUY継続監視状態(WatchState)のオーケストレーション(BUY候補裾野拡大機能2026-08)。

`TradeCooldownService`とは`HoldingsSnapshotEntry.cooldown_until_date`
(クールダウン判定はbuy_signal_service.py側で行い、クールダウン中はそもそも
このサービスを呼ばない)・`TradeEvent`一覧(売買イベント検知後のWatchState
強制終了)を介してのみ連携し、互いを直接import・呼び出ししない(責務分離)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.config.models import NearBuyConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import BUY_FAMILY_ACTIONS, BuyAction, WatchType
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.watch_state import WatchState, build_watch_id
from jstock_advisor.domain.signals.near_buy import (
    compute_best_distance_pct,
    compute_consecutive_business_days,
    evaluate_stale,
    meets_near_buy_continue_conditions,
    meets_near_buy_start_conditions,
)
from jstock_advisor.domain.signals.trade_event_detection import TradeEvent
from jstock_advisor.infrastructure.local_repository.watch_state_repository import (
    WatchStateRepository,
)

_DEFAULT_EXECUTION_CONTEXT = ExecutionContext.normal()

# WatchState.end_reasonの値(§5-2の5終了理由のうち、TRADE_EVENTを除く4つを
# このサービスが担当する。TRADE_EVENTはend_for_trade_events()が担当)。
END_REASON_PRICE_OUT_OF_RANGE = "PRICE_OUT_OF_RANGE"
END_REASON_PROMOTED_TO_BUY = "PROMOTED_TO_BUY"
END_REASON_NOT_ATTRACTIVE = "NOT_ATTRACTIVE"
END_REASON_STALE = "STALE"
END_REASON_TRADE_EVENT = "TRADE_EVENT"


class WatchStateService:
    def __init__(
        self,
        business_calendar: BusinessCalendar,
        repository: WatchStateRepository | None = None,
        execution_context: ExecutionContext = _DEFAULT_EXECUTION_CONTEXT,
    ) -> None:
        self._repo = repository or WatchStateRepository.for_execution_context(execution_context)
        self._calendar = business_calendar

    def evaluate_and_update(
        self,
        stock_code: str,
        buy_action: BuyAction,
        company_quality_score: float | None,
        required_decline_to_entry_pct: Decimal | None,
        current_price: Decimal | None,
        entry_price: Decimal | None,
        today: dt.date,
        config: NearBuyConfig,
    ) -> tuple[WatchType | None, int | None]:
        """当日の評価結果からWatchStateを開始/継続/終了させ、Recommendationへ
        設定すべき(watch_type, consecutive_business_days)を返す(いずれも
        NEAR BUY対象外の場合は(None, None))。

        呼び出し側(BuySignalService)は、クールダウン中(HoldingsSnapshotEntry.
        cooldown_until_date >= today)の銘柄についてはこのメソッド自体を
        呼ばないこと(§5-2)。
        """
        existing = self._repo.get_active(stock_code, WatchType.NEAR_BUY)

        if existing is not None:
            gap = self._calendar.business_days_between(existing.last_matched_at, today)
            if evaluate_stale(gap, config.max_stale_business_days):
                self._end(existing, today, END_REASON_STALE)
                existing = None

        if existing is not None and buy_action in BUY_FAMILY_ACTIONS:
            self._end(existing, today, END_REASON_PROMOTED_TO_BUY)
            return None, None

        if existing is not None:
            if not meets_near_buy_continue_conditions(
                buy_action, required_decline_to_entry_pct, config
            ):
                end_reason = (
                    END_REASON_PRICE_OUT_OF_RANGE
                    if buy_action == BuyAction.WATCH_FOR_PRICE
                    else END_REASON_NOT_ATTRACTIVE
                )
                self._end(existing, today, end_reason)
                return None, None

            assert required_decline_to_entry_pct is not None  # noqa: S101 - continue条件で保証済み
            gap = self._calendar.business_days_between(existing.last_matched_at, today)
            consecutive = compute_consecutive_business_days(
                gap, existing.consecutive_business_days
            )
            updated = existing.model_copy(
                update={
                    "last_matched_at": today,
                    "last_evaluated_at": today,
                    "consecutive_business_days": consecutive,
                    "last_current_price": current_price,
                    "last_entry_price": entry_price,
                    "best_distance_pct": compute_best_distance_pct(
                        existing.best_distance_pct, required_decline_to_entry_pct
                    ),
                }
            )
            self._repo.upsert(updated)
            return WatchType.NEAR_BUY, consecutive

        if meets_near_buy_start_conditions(
            buy_action, company_quality_score, required_decline_to_entry_pct, config
        ):
            assert required_decline_to_entry_pct is not None  # noqa: S101 - 開始条件で保証済み
            new_state = WatchState(
                watch_id=build_watch_id(stock_code, WatchType.NEAR_BUY),
                stock_code=stock_code,
                watch_type=WatchType.NEAR_BUY,
                started_at=today,
                last_matched_at=today,
                last_evaluated_at=today,
                consecutive_business_days=1,
                last_current_price=current_price,
                last_entry_price=entry_price,
                best_distance_pct=required_decline_to_entry_pct,
            )
            self._repo.upsert(new_state)
            return WatchType.NEAR_BUY, 1

        return None, None

    def end_for_trade_events(self, events: list[TradeEvent], today: dt.date) -> None:
        """売買イベントを検知した銘柄について、既存のアクティブなWatchStateを
        すべて終了する(TradeCooldownServiceからは直接呼ばれない。呼び出し順序は
        オーケストレーション層(handler)がTradeCooldownService.detect_and_apply()
        の戻り値をこのメソッドへ明示的に渡す形で連結する、責務分離のため)。
        """
        for event in events:
            for watch_type in WatchType:
                state = self._repo.get_active(event.stock_code, watch_type)
                if state is not None:
                    self._end(state, today, END_REASON_TRADE_EVENT)

    def _end(self, state: WatchState, today: dt.date, end_reason: str) -> None:
        updated = state.model_copy(
            update={"ended_at": today, "end_reason": end_reason, "last_evaluated_at": today}
        )
        self._repo.upsert(updated)
