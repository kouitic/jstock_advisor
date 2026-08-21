"""保有銘柄リストの変化から売買イベントを検知し、クールダウン期限を設定する
オーケストレーション(BUY候補裾野拡大機能2026-08)。

`WatchStateRepository`/`WatchStateService`を一切import・呼び出ししない
(責務分離。売買イベント検知後のWatchState強制終了は、呼び出し元の
ハンドラが本サービスの戻り値(TradeEvent一覧)を`WatchStateService.
end_for_trade_events()`へ明示的に渡す形で連結する)。
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field

from jstock_advisor.config.models import TradeCooldownConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import TransactionType
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holdings_snapshot import HoldingsSnapshotEntry
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.domain.signals.trade_event_detection import TradeEvent, detect_trade_events
from jstock_advisor.infrastructure.aws import trade_detection_lock
from jstock_advisor.infrastructure.local_repository.holdings_snapshot_repository import (
    HoldingsSnapshotRepository,
)

logger = logging.getLogger(__name__)

_DEFAULT_EXECUTION_CONTEXT = ExecutionContext.normal()
_LEASE_SECONDS = 60
_BOUNDED_RETRY_ATTEMPTS = 5
_BOUNDED_RETRY_INTERVAL_SECONDS = 0.2


@dataclass(frozen=True)
class TradeDetectionOutcome:
    # False の場合、当日の検知処理の完了をこの実行では確認できなかった
    # (fail-closed)。呼び出し元はis_critical_risk以外の通常通知を
    # TRADE_DETECTION_IN_PROGRESSとして抑止すること(§5-1・§6)。
    confirmed: bool
    events: list[TradeEvent] = field(default_factory=list)


def _cooldown_business_days(event_type: TransactionType, config: TradeCooldownConfig) -> int:
    if event_type == TransactionType.BUY:
        return config.buy_business_days
    if event_type == TransactionType.FULL_SELL:
        return config.sell_business_days
    return config.partial_trade_business_days  # ADDITIONAL_BUY / PARTIAL_SELL


class TradeCooldownService:
    def __init__(
        self,
        business_calendar: BusinessCalendar,
        config: TradeCooldownConfig,
        repository: HoldingsSnapshotRepository | None = None,
        execution_context: ExecutionContext = _DEFAULT_EXECUTION_CONTEXT,
    ) -> None:
        self._calendar = business_calendar
        self._config = config
        self._execution_context = execution_context
        self._repo = repository or HoldingsSnapshotRepository.for_execution_context(
            execution_context
        )

    def detect_and_apply(
        self, current_holdings: dict[str, Holding], now: dt.datetime
    ) -> TradeDetectionOutcome:
        """§5-1: 冪等性・競合安全性を担保した売買イベント検知。

        先行呼び出し(BUY候補Lambda・保有銘柄Lambdaのどちらでもよい)が
        ロックを獲得して検知処理を実行し、後続呼び出しはCOMPLETEDを
        確認できればスキップ、確認できなければ短いbounded retryの後
        fail-closed(confirmed=False)を返す。

        コードレビュー対応(2026-08): ロックキーはExecutionMode(NORMAL/
        VALIDATION)ごとに名前空間分離する(例: "NORMAL:2026-08-14"/
        "VALIDATION:2026-08-14")。分離しない場合、VALIDATIONが先に実行され
        当日分をCOMPLETEDにしてしまうと、NORMALが同じ日付をCOMPLETED済みと
        誤認して検知処理自体をスキップし、本番のHoldingsSnapshotが更新され
        なくなる(通知検証モードの「本番の永続状態・通常運用へ影響させない」
        という既存方針に反する)。物理テーブルは1つのまま、キー値のみで
        NORMAL/VALIDATIONを分離する(大規模化を避けるため)。

        再コードレビュー対応(2026-08、JST暦日境界修正・指摘1/2): ロックキー
        (「1日1回」の基準日)・検知/HoldingsSnapshot記録の基準日
        (_do_detect_and_apply()のtoday、cooldown_until_dateの算出起点)は、
        いずれもevaluation_date_jst(now)で算出したJST暦日を使う(1回だけ算出し
        使い回す)。line_notification_service.check_trade_cooldown_eligibility()
        側の比較もJST暦日で行うため、生成側(ここ)と比較側の基準日を揃える
        (以前はここがLambdaのUTC now.date()のままで、比較側とのみJST化が
        先行し、生成/比較で基準日が一致しない状態になっていた)。try_acquire/
        mark_completed/get_status自体へ渡すnow(リース時刻の実時間比較用)は
        UTCのまま変更しない(暦日ではなく経過時間の判定のため)。
        """
        evaluation_date = evaluation_date_jst(now)
        business_date = f"{self._execution_context.mode.value}:{evaluation_date.isoformat()}"

        if trade_detection_lock.try_acquire(business_date, now, _LEASE_SECONDS):
            events = self._do_detect_and_apply(current_holdings, evaluation_date)
            trade_detection_lock.mark_completed(business_date, leased_at_iso=now.isoformat())
            return TradeDetectionOutcome(confirmed=True, events=events)

        for _ in range(_BOUNDED_RETRY_ATTEMPTS):
            status, lease_expires_at = trade_detection_lock.get_status(business_date)
            if status == trade_detection_lock.RunLockStatus.COMPLETED.value:
                return TradeDetectionOutcome(confirmed=True, events=[])
            # stale lock: 先行Lambdaが異常終了した可能性。自分がリースを奪取する。
            if (
                status == trade_detection_lock.RunLockStatus.PROCESSING.value
                and lease_expires_at is not None
                and lease_expires_at < now.isoformat()
                and trade_detection_lock.try_acquire(business_date, now, _LEASE_SECONDS)
            ):
                events = self._do_detect_and_apply(current_holdings, evaluation_date)
                trade_detection_lock.mark_completed(business_date, leased_at_iso=now.isoformat())
                return TradeDetectionOutcome(confirmed=True, events=events)
            time.sleep(_BOUNDED_RETRY_INTERVAL_SECONDS)

        logger.warning(
            "trade_detection_not_confirmed business_date=%s: "
            "TRADE_DETECTION_IN_PROGRESSとして通常通知をfail-closedする",
            business_date,
        )
        return TradeDetectionOutcome(confirmed=False, events=[])

    def _do_detect_and_apply(
        self, current_holdings: dict[str, Holding], today: dt.date
    ) -> list[TradeEvent]:
        previous_entries = {e.stock_code: e for e in self._repo.list_all()}
        if not previous_entries:
            # 初回実行(前回スナップショットが皆無): 誤検知防止のためイベント検知
            # 自体をスキップし、現状をベースラインとして書き込むのみに留める。
            self._write_baseline(current_holdings, today)
            return []

        events = detect_trade_events(previous_entries, current_holdings, today)
        for event in events:
            cooldown_until = None
            if self._config.enabled:
                cooldown_days = _cooldown_business_days(event.event_type, self._config)
                cooldown_until = self._calendar.add_business_days(today, cooldown_days)
            self._repo.upsert(
                HoldingsSnapshotEntry(
                    stock_code=event.stock_code,
                    shares=event.shares,
                    average_purchase_price=event.average_purchase_price,
                    recorded_at=today,
                    last_trade_event_type=event.event_type,
                    trade_detected_at=today,
                    cooldown_until_date=cooldown_until,
                    active_holding=event.shares > 0,
                )
            )
        return events

    def _write_baseline(self, current_holdings: dict[str, Holding], today: dt.date) -> None:
        for stock_code, holding in current_holdings.items():
            self._repo.upsert(
                HoldingsSnapshotEntry(
                    stock_code=stock_code,
                    shares=holding.shares,
                    average_purchase_price=holding.average_purchase_price,
                    recorded_at=today,
                    active_holding=holding.shares > 0,
                )
            )

    def is_in_cooldown(self, stock_code: str, today: dt.date) -> bool:
        """§6: 通常の売買推奨通知(BUY/買い増し/SELL/一部売却検討/NEAR BUY/
        WATCH_BEFORE_EARNINGS)を抑止すべきか(重大リスクはこのチェックより
        前段で貫通させること。呼び出し元の責務)。
        """
        if not self._config.enabled:
            return False
        entry = self._repo.get(stock_code)
        if entry is None or entry.cooldown_until_date is None:
            return False
        return today <= entry.cooldown_until_date
