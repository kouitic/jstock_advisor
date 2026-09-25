"""売買検知の部分適用クラッシュ時のtakeover再検知漏れを解消する
consumption step(Issue #71 F-C11 Phase 2 = Issue #529)。

## 背景

`TradeCooldownService._do_detect_and_apply()`は、検知した売買イベント
(`TradeEvent`)を`HoldingsSnapshotEntry`更新より前に`TradeEventRecord`
(`pending_marker=PENDING`)として永続化する(Phase 1、Issue #71 F-C11)。
その後`trade_detection_lock`をCOMPLETEDへ遷移させてから、呼び出し元
ハンドラが`WatchStateService.end_for_trade_events(events, today)`を呼ぶ。

しかしロックのCOMPLETED遷移は`end_for_trade_events()`より**先**に発生する
ため、両者の間でLambdaがクラッシュすると、ロックはCOMPLETEDのまま固定され、
以後の`detect_and_apply()`呼び出しはすべて`events=[]`を返す(検知した
`events`はクラッシュしたLambda呼び出しのローカル変数にしか存在しない)。
`TradeEventRecord`(`pending_marker=PENDING`)自体は永続化されているが、
これを読んでWatchState終了を再実行するconsumption stepがPhase 1には
存在しなかった(Phase 2のスコープとして意図的に残されていた)。

## 設計(USER/MANAGER判断。#529 issuecomment参照)

専用batch/新規scheduled Lambdaは作らず、既存の`watchlist_batch_reconciler_
handler.py`(毎時起動)へ独立した関数として相乗りする。既存のwatchlist batch
reconciliationとは処理を完全に分離し、どちらかが失敗してももう一方を
巻き込まない(呼び出し側でtry/except境界を分ける)。

一貫性モデルは**at-least-once-with-idempotent-consumer**(JIRO Phase A報告
の結論)。`TradeEventRecord`(決定的event_id)・`HoldingsSnapshotEntry`
(natural upsert収束)・`WatchState._end()`(CAS+終了済みno-op)はいずれも
既に冪等設計であり、本stepを複数のreconcilerが同時に実行しても、
`TradeEventRecordRepository.mark_consumed()`のCAS(条件付き置換)により
最終的に1回分の消費へ収束する(2回目以降はCAS不成立でFalseが返り、
エラー扱いにはしない)。

## bounded processing

1回のreconciler実行あたりの処理件数を`max_records_per_run`で上限を設ける
(config/notification_rules.yaml `trade_event_reconciliation.
max_records_per_run`)。残件があれば`remaining`として報告し、次回reconciler
実行が継続して処理する(無制限に1回で処理しない)。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from jstock_advisor.domain.entities.trade_event_record import TradeEventRecord
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.domain.signals.trade_event_detection import TradeEvent
from jstock_advisor.infrastructure.local_repository.trade_event_record_repository import (
    TradeEventRecordRepository,
)
from jstock_advisor.services.watch_state_service import WatchStateService

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@dataclass(frozen=True)
class TradeEventReconciliationOutcome:
    """1回のreconciler実行における処理結果(監視・テスト用)。"""

    processed: int
    already_consumed_by_other_run: int
    remaining: int


def _record_to_trade_event(record: TradeEventRecord) -> TradeEvent:
    """`TradeEventRecord`(永続化された事実)から`TradeEvent`(検知結果の
    値オブジェクト)を再構成する。`event_id`以外のフィールド集合は完全に
    一致するため(both entities share the same detection facts)、
    `WatchStateService.end_for_trade_events()`・
    `domain/signals/trade_event_detection.py`のいずれも変更しない。
    """
    return TradeEvent(
        holding_id=record.holding_id,
        owner=record.owner,
        stock_code=record.stock_code,
        event_type=record.event_type,
        detected_at=record.detected_at,
        shares=record.shares,
        average_purchase_price=record.average_purchase_price,
    )


def reconcile_pending_trade_events(
    now: dt.datetime,
    max_records_per_run: int,
    trade_event_repo: TradeEventRecordRepository,
    watch_state_service: WatchStateService,
) -> TradeEventReconciliationOutcome:
    """未消費(`pending_marker=PENDING`)の`TradeEventRecord`を検出し、
    `WatchStateService.end_for_trade_events()`を再実行してから消費済みに
    する(Issue #529)。

    呼び出し元(reconciler handler)は、既存のwatchlist batch reconciliation
    ループとは**別のtry/except境界**でこの関数を呼ぶこと(failure isolation。
    USER/MANAGER判断の必須契約)。
    """
    today = evaluation_date_jst(now)
    pending = trade_event_repo.list_pending_with_raw()
    to_process = pending[:max_records_per_run]

    processed = 0
    already_consumed_by_other_run = 0
    for record, raw in to_process:
        event = _record_to_trade_event(record)
        watch_state_service.end_for_trade_events([event], today)
        if trade_event_repo.mark_consumed(record, raw, now):
            processed += 1
        else:
            # 別のreconciler実行(または並行worker)が先に消費済みにしていた。
            # end_for_trade_events()自体はWatchState._end()のCAS+終了済み
            # no-opにより安全に重複実行できるため、エラーではなくskipとして
            # 扱う(at-least-once-with-idempotent-consumer)。
            already_consumed_by_other_run += 1
            logger.info(
                "trade_event_reconciliation: event already consumed by another run "
                "stock_code=%s detected_at=%s",
                event.stock_code,
                event.detected_at.isoformat(),
            )

    remaining = max(0, len(pending) - len(to_process))
    if processed or already_consumed_by_other_run:
        logger.info(
            "trade_event_reconciliation: processed=%d already_consumed_by_other_run=%d "
            "remaining=%d",
            processed,
            already_consumed_by_other_run,
            remaining,
        )
    return TradeEventReconciliationOutcome(
        processed=processed,
        already_consumed_by_other_run=already_consumed_by_other_run,
        remaining=remaining,
    )
