"""NEAR BUY継続監視状態(WatchState)のオーケストレーション(BUY候補裾野拡大機能2026-08)。

`TradeCooldownService`とは`HoldingsSnapshotEntry.cooldown_until_date`
(クールダウン判定はbuy_signal_service.py側で行い、クールダウン中はそもそも
このサービスを呼ばない)・`TradeEvent`一覧(売買イベント検知後のWatchState
強制終了)を介してのみ連携し、互いを直接import・呼び出ししない(責務分離)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.config.models import NearBuyConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import (
    BUY_FAMILY_ACTIONS,
    BuyAction,
    WatchTransitionType,
    WatchType,
)
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

# WATCH終了通知の対象となり得る終了理由(TRADE_EVENTは対象外。§3コードレビュー対応)。
WATCH_END_NOTIFIABLE_REASONS = frozenset(
    {END_REASON_PRICE_OUT_OF_RANGE, END_REASON_NOT_ATTRACTIVE, END_REASON_STALE}
)

_NONE_TRANSITION = WatchTransitionType.NONE


@dataclass(frozen=True)
class WatchTransitionResult:
    """evaluate_and_update()の戻り値(コードレビュー対応2026-08: 単なる
    tupleではなく、通知層が「4日監視後にBUY到達」「PAUSED後の監視再開」
    「6日継続してPRICE_OUT_OF_RANGEで終了」を表現できるだけの情報を返す)。

    watch_typeはNONE以外の遷移(STARTED/CONTINUED/RESUMED/PROMOTED_TO_BUY/
    ENDED)ではどの監視種別に関する遷移かを示すため常に設定される
    (WATCH終了通知の文言生成に必要なため)。「現在アクティブに監視中か」を
    示すRecommendation.watch_type相当の値は、呼び出し元がtransition_typeが
    STARTED/CONTINUED/RESUMEDのいずれかであるかどうかで別途判定すること
    (PROMOTED_TO_BUY/ENDEDでは監視は終了済みのため、watch_typeがNEAR_BUYで
    あっても「現在アクティブ」を意味しない)。
    previous_consecutive_business_daysは、終了/昇格時点で「それまで何営業日
    連続で監視していたか」を保持する(consecutive_business_daysは終了/昇格後の
    値が無いためNoneになるが、previous_consecutive_business_daysには残る)。
    """

    watch_type: WatchType | None
    transition_type: WatchTransitionType
    consecutive_business_days: int | None = None
    previous_consecutive_business_days: int | None = None
    started_at: dt.date | None = None
    end_reason: str | None = None
    best_distance_pct: Decimal | None = None


_NO_TRANSITION = WatchTransitionResult(watch_type=None, transition_type=_NONE_TRANSITION)


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
    ) -> WatchTransitionResult:
        """当日の評価結果からWatchStateを開始/継続/終了させ、通知層が必要と
        する遷移情報を`WatchTransitionResult`として返す。

        呼び出し側(BuySignalService)は、クールダウン中(HoldingsSnapshotEntry.
        cooldown_until_date >= today)の銘柄についてはこのメソッド自体を
        呼ばないこと(§5-2)。
        """
        existing = self._repo.get_active(stock_code, WatchType.NEAR_BUY)

        # コードレビュー対応(2026-08、指摘1): 「今日実際に買い水準へ到達した」
        # という最新の有効な評価結果を、stale終了より優先する。数営業日の
        # 評価不能を挟んだ後に評価が再開し、その当日にbuy_actionがBUY家族
        # だった場合、stale終了を先に判定してしまうと「監視終了」と
        # 「BUY到達」の二重通知が発生してしまうため、判定順序を入れ替えた
        # (PROMOTED_TO_BUYの判定にstale・gapは一切関与しない)。
        if existing is not None and buy_action in BUY_FAMILY_ACTIONS:
            previous_days = existing.consecutive_business_days
            started_at = existing.started_at
            best_distance = existing.best_distance_pct
            self._end(existing, today, END_REASON_PROMOTED_TO_BUY)
            return WatchTransitionResult(
                watch_type=WatchType.NEAR_BUY,
                transition_type=WatchTransitionType.PROMOTED_TO_BUY,
                previous_consecutive_business_days=previous_days,
                started_at=started_at,
                end_reason=END_REASON_PROMOTED_TO_BUY,
                best_distance_pct=best_distance,
            )

        if existing is not None:
            gap = self._calendar.business_days_between(existing.last_matched_at, today)
            if evaluate_stale(gap, config.max_stale_business_days):
                previous_days = existing.consecutive_business_days
                started_at = existing.started_at
                best_distance = existing.best_distance_pct
                self._end(existing, today, END_REASON_STALE)
                # コードレビュー対応: STALE終了直後に同一営業日内で新規監視を
                # 再開させない(終了通知の意味を保つため、既存の「即日再開」
                # 挙動は廃止し、次回評価から改めて開始条件を満たすか判定する)。
                return WatchTransitionResult(
                    watch_type=WatchType.NEAR_BUY,
                    transition_type=WatchTransitionType.ENDED,
                    previous_consecutive_business_days=previous_days,
                    started_at=started_at,
                    end_reason=END_REASON_STALE,
                    best_distance_pct=best_distance,
                )

        if existing is not None:
            if not meets_near_buy_continue_conditions(
                buy_action, required_decline_to_entry_pct, config
            ):
                end_reason = (
                    END_REASON_PRICE_OUT_OF_RANGE
                    if buy_action == BuyAction.WATCH_FOR_PRICE
                    else END_REASON_NOT_ATTRACTIVE
                )
                previous_days = existing.consecutive_business_days
                started_at = existing.started_at
                best_distance = existing.best_distance_pct
                self._end(existing, today, end_reason)
                return WatchTransitionResult(
                    watch_type=WatchType.NEAR_BUY,
                    transition_type=WatchTransitionType.ENDED,
                    previous_consecutive_business_days=previous_days,
                    started_at=started_at,
                    end_reason=end_reason,
                    best_distance_pct=best_distance,
                )

            assert required_decline_to_entry_pct is not None  # noqa: S101 - continue条件で保証済み
            gap = self._calendar.business_days_between(existing.last_matched_at, today)
            # Issue #166: 非営業日(週末・平日に当たる祝日)の評価では、営業日ベースの
            # stateを一切進めない。last_matched_atは「連続営業日数へ寄与した最後の
            # 一致営業日」であり、非営業日で上書きすると営業日計算の起点そのものが
            # 非営業日になってしまう(エンティティ側の定義とも矛盾する)。
            # gapが2以上でも同様で、非営業日にリセットを確定させず次の営業日へ委ねる
            # (次の営業日でもgapは2以上のままなので、そこで正しくリセットされる)。
            # last_evaluated_atは「最後に評価処理を行った日」なので常に更新する。
            today_is_business_day = self._calendar.is_business_day(today)
            is_resumed = today_is_business_day and gap >= 2
            consecutive = (
                compute_consecutive_business_days(gap, existing.consecutive_business_days)
                if today_is_business_day
                else existing.consecutive_business_days
            )
            # 同一営業日の再評価(gap == 0)でも起点は動かさない(二重更新しない)。
            advances_anchor = today_is_business_day and gap >= 1
            updated = existing.model_copy(
                update={
                    "last_matched_at": today if advances_anchor else existing.last_matched_at,
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
            return WatchTransitionResult(
                watch_type=WatchType.NEAR_BUY,
                transition_type=(
                    WatchTransitionType.RESUMED if is_resumed else WatchTransitionType.CONTINUED
                ),
                consecutive_business_days=consecutive,
                previous_consecutive_business_days=existing.consecutive_business_days,
                started_at=existing.started_at,
                best_distance_pct=updated.best_distance_pct,
            )

        if meets_near_buy_start_conditions(
            buy_action, company_quality_score, required_decline_to_entry_pct, config
        ):
            # Issue #166: 非営業日には監視を開始しない。
            # consecutive_business_days=1は「1営業日連続で条件を満たした」ことを
            # 意味し、last_matched_atは「連続営業日数へ寄与した一致営業日」である。
            # 非営業日に開始すると、まだ1営業日も成立していないのにcounter=1となり、
            # 営業日計算の起点も非営業日になってしまう(継続側の契約と矛盾する)。
            # counterを0で作る・過去の営業日を起点として詐称する、といった回避は
            # いずれも採らず、**永続stateを作らずに次の営業日へ委ねる**。
            # 次の営業日に改めて評価し、その日に開始条件を満たしていれば
            # その営業日を起点として通常どおり開始する。
            if not self._calendar.is_business_day(today):
                return _NO_TRANSITION
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
            return WatchTransitionResult(
                watch_type=WatchType.NEAR_BUY,
                transition_type=WatchTransitionType.STARTED,
                consecutive_business_days=1,
                previous_consecutive_business_days=None,
                started_at=today,
                best_distance_pct=required_decline_to_entry_pct,
            )

        return _NO_TRANSITION

    def end_for_trade_events(self, events: list[TradeEvent], today: dt.date) -> None:
        """売買イベントを検知した銘柄について、既存のアクティブなWatchStateを
        すべて終了する(TradeCooldownServiceからは直接呼ばれない。呼び出し順序は
        オーケストレーション層(handler)がTradeCooldownService.detect_and_apply()
        の戻り値をこのメソッドへ明示的に渡す形で連結する、責務分離のため)。

        TRADE_EVENTによる終了はWATCH終了通知の対象外(§3)であり、この
        メソッドはRecommendationを一切生成しない(呼び出し元のハンドラは
        該当銘柄をその日の通常評価から除外済みのため、watch_transition_type
        フィールドが誤って設定されることもない)。
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
