"""Issue #529(#71 F-C11 Phase 2): 売買検知の部分適用クラッシュ時のtakeover
再検知漏れを解消するconsumption stepのテスト(USER/MANAGER判断のT1〜T8)。

Phase 1(#527)は検知した売買イベントを`TradeEventRecord`
(`pending_marker=PENDING`)として永続化するが、`trade_detection_lock`の
COMPLETED遷移が`WatchStateService.end_for_trade_events()`実行より先に発生する
ため、両者の間でクラッシュするとWatchState終了が永久に行われないギャップが
あった(JIRO Phase A報告)。本ファイルは、そのギャップをpending-marker-index
経由で回収するconsumption step(`reconcile_pending_trade_events()`)を固定する。

架空の保有データのみを使用する(実在の銘柄コード・所有者名・保有数量は含まない)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import TransactionType, WatchType
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.entities.trade_event_record import TradeEventRecord, build_trade_event_id
from jstock_advisor.domain.entities.watch_state import WatchState, build_watch_id
from jstock_advisor.infrastructure.local_repository.trade_event_record_repository import (
    PENDING_INDEX_NAME,
    TradeEventRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.watch_state_repository import (
    WatchStateRepository,
)
from jstock_advisor.services.trade_event_reconciliation_service import (
    reconcile_pending_trade_events,
)
from jstock_advisor.services.watch_state_service import END_REASON_TRADE_EVENT, WatchStateService

_NOW = dt.datetime(2026, 8, 20, 23, 30, tzinfo=dt.UTC)  # 2026-08-21 08:30 JST(金)
_TODAY = dt.date(2026, 8, 21)
_STOCK = "2914"
_HID = build_holding_id(DEFAULT_OWNER, _STOCK)


def _calendar() -> BusinessCalendar:
    return BusinessCalendar.from_config(load_config().holiday_calendar)


def _pending_record(
    event_id: str | None = None, detected_at: dt.date = _TODAY, stock_code: str = _STOCK
) -> TradeEventRecord:
    return TradeEventRecord(
        event_id=event_id or build_trade_event_id(_HID, detected_at),
        holding_id=_HID,
        owner=DEFAULT_OWNER,
        stock_code=stock_code,
        event_type=TransactionType.PARTIAL_SELL,
        detected_at=detected_at,
        shares=50,
        average_purchase_price=Decimal("1000"),
        created_at=_NOW,
    )


def _active_watch_state(stock_code: str = _STOCK) -> WatchState:
    return WatchState(
        watch_id=build_watch_id(stock_code, WatchType.NEAR_BUY),
        stock_code=stock_code,
        watch_type=WatchType.NEAR_BUY,
        started_at=dt.date(2026, 8, 10),
        last_matched_at=dt.date(2026, 8, 20),
        last_evaluated_at=dt.date(2026, 8, 20),
        consecutive_business_days=5,
    )


def _get_watch_state(repo: WatchStateRepository, stock_code: str = _STOCK) -> WatchState | None:
    """`WatchStateRepository`は`get()`を持たない(`get_active`/`get_with_raw`
    のみ)ため、終了済みも含めて取得するテスト用ヘルパー。"""
    fetched = repo.get_with_raw(build_watch_id(stock_code, WatchType.NEAR_BUY))
    return fetched[0] if fetched is not None else None


def _reconcile(
    trade_event_repo: TradeEventRecordRepository,
    watch_state_repo: WatchStateRepository,
    max_records_per_run: int = 200,
) -> Any:
    watch_state_service = WatchStateService(
        business_calendar=_calendar(), repository=watch_state_repo
    )
    return reconcile_pending_trade_events(
        _NOW, max_records_per_run, trade_event_repo, watch_state_service
    )


# --- T1: mark_completed後・end_for_trade_events前にcrash → 次reconcilerで回収 --


def test_t1_recovers_watch_state_end_from_pending_record(tmp_path: Path) -> None:
    """C4/C5相当(JIRO Phase A報告): pending_marker=PENDINGのTradeEventRecordが
    永続化されているが、end_for_trade_events()が一度も呼ばれなかった状態から、
    reconcile_pending_trade_events()がWatchStateを終了させること。"""
    trade_event_repo = TradeEventRecordRepository(store_dir=tmp_path)
    trade_event_repo.create_pending(_pending_record())
    watch_state_repo = WatchStateRepository(store_dir=tmp_path)
    watch_state_repo.upsert(_active_watch_state())

    outcome = _reconcile(trade_event_repo, watch_state_repo)

    assert outcome.processed == 1
    assert outcome.already_consumed_by_other_run == 0
    assert outcome.remaining == 0
    state = watch_state_repo.get_active(_STOCK, WatchType.NEAR_BUY)
    assert state is None  # 終了済み(get_activeはended_at is Noneのみ返す)
    ended = _get_watch_state(watch_state_repo)
    assert ended is not None
    assert ended.ended_at == _TODAY
    assert ended.end_reason == END_REASON_TRADE_EVENT


def test_t1_succeeds_even_when_no_active_watch_state_exists(tmp_path: Path) -> None:
    """★ 反証: 対象銘柄にアクティブなWatchStateが無い場合(通常の大半のケース)
    でも、consumptionはエラーにならず正常に完了する。"""
    trade_event_repo = TradeEventRecordRepository(store_dir=tmp_path)
    trade_event_repo.create_pending(_pending_record())
    watch_state_repo = WatchStateRepository(store_dir=tmp_path)

    outcome = _reconcile(trade_event_repo, watch_state_repo)

    assert outcome.processed == 1
    assert outcome.remaining == 0


# --- T2: replay成功後 → consumed_at設定・pending_marker削除・GSI検索対象外 -----


def test_t2_marks_consumed_and_removes_from_pending_index(tmp_path: Path) -> None:
    trade_event_repo = TradeEventRecordRepository(store_dir=tmp_path)
    record = _pending_record()
    trade_event_repo.create_pending(record)
    watch_state_repo = WatchStateRepository(store_dir=tmp_path)
    watch_state_repo.upsert(_active_watch_state())

    _reconcile(trade_event_repo, watch_state_repo)

    stored = trade_event_repo.get(record.event_id)
    assert stored is not None
    assert stored.consumed_at == _NOW
    assert stored.pending_marker is None
    assert trade_event_repo.list_pending_with_raw() == []


# --- T3: 同じeventを2回reconcile → 2回目no-op ---------------------------------


def test_t3_second_reconcile_run_is_a_no_op(tmp_path: Path) -> None:
    trade_event_repo = TradeEventRecordRepository(store_dir=tmp_path)
    trade_event_repo.create_pending(_pending_record())
    watch_state_repo = WatchStateRepository(store_dir=tmp_path)
    watch_state_repo.upsert(_active_watch_state())

    first = _reconcile(trade_event_repo, watch_state_repo)
    second = _reconcile(trade_event_repo, watch_state_repo)

    assert first.processed == 1
    assert second.processed == 0
    assert second.already_consumed_by_other_run == 0
    assert second.remaining == 0
    # WatchStateの終了理由・終了日は1回目のまま変化しない(二重適用されていない)。
    ended = _get_watch_state(watch_state_repo)
    assert ended is not None
    assert ended.ended_at == _TODAY


# --- T4: 同一eventを2 worker/reconcilerが並行処理 → 最終状態1回分へ収束 --------


def test_t4_concurrent_reconcilers_converge_to_single_consumption(tmp_path: Path) -> None:
    """2つのreconciler実行が同じpending recordを同時に読み、片方が先に
    consumeした場合、もう一方のmark_consumed()はCAS不成立(False)となり、
    エラーにはならず`already_consumed_by_other_run`としてカウントされる。"""
    trade_event_repo = TradeEventRecordRepository(store_dir=tmp_path)
    record = _pending_record()
    trade_event_repo.create_pending(record)
    watch_state_repo = WatchStateRepository(store_dir=tmp_path)
    watch_state_repo.upsert(_active_watch_state())

    # 両方の実行が同じ時点のraw値を読んだ状態を模擬する。
    pending = trade_event_repo.list_pending_with_raw()
    assert len(pending) == 1
    stored_record, raw = pending[0]

    # reconciler Aが先にconsumeする。
    assert trade_event_repo.mark_consumed(stored_record, raw, _NOW) is True
    # reconciler B(同じ古いrawを持ったまま)は競合してFalseになる。
    assert trade_event_repo.mark_consumed(stored_record, raw, _NOW) is False

    # 最終状態は1回分の消費(pending_markerが外れ、consumed_atが設定済み)。
    final = trade_event_repo.get(record.event_id)
    assert final is not None
    assert final.pending_marker is None
    assert final.consumed_at == _NOW


# --- T5: 一部が既に完了していても残りを終了できる(部分完了への耐性) -----------


def test_t5_reconcile_is_idempotent_when_watch_state_already_ended(tmp_path: Path) -> None:
    """WatchStateが既に終了済み(前回の部分的な実行で完了していた)状態でも、
    reconcileはエラーにならず、consumptionを正常に完了させる
    (WatchState._end()の終了済みno-opにより二重終了しない)。"""
    trade_event_repo = TradeEventRecordRepository(store_dir=tmp_path)
    trade_event_repo.create_pending(_pending_record())
    watch_state_repo = WatchStateRepository(store_dir=tmp_path)
    already_ended = _active_watch_state().model_copy(
        update={"ended_at": dt.date(2026, 8, 19), "end_reason": "PRICE_OUT_OF_RANGE"}
    )
    watch_state_repo.upsert(already_ended)

    outcome = _reconcile(trade_event_repo, watch_state_repo)

    assert outcome.processed == 1
    # 既存の終了理由・終了日は上書きされない(_end()の終了済みno-op)。
    stored = _get_watch_state(watch_state_repo)
    assert stored is not None
    assert stored.ended_at == dt.date(2026, 8, 19)
    assert stored.end_reason == "PRICE_OUT_OF_RANGE"


# --- T6: 古い孤立PENDING record → TTL無しのProductionでは回収可能 --------------


def test_t6_old_orphaned_pending_record_is_still_recoverable(tmp_path: Path) -> None:
    """detected_atが古い(数週間前)のpending recordでも、年齢による除外は
    行わず回収できる(Production tableにTTLが無い設計と対称。#527参照)。"""
    trade_event_repo = TradeEventRecordRepository(store_dir=tmp_path)
    old_date = dt.date(2026, 7, 1)
    trade_event_repo.create_pending(_pending_record(detected_at=old_date))
    watch_state_repo = WatchStateRepository(store_dir=tmp_path)
    watch_state_repo.upsert(_active_watch_state())

    outcome = _reconcile(trade_event_repo, watch_state_repo)

    assert outcome.processed == 1
    ended = _get_watch_state(watch_state_repo)
    assert ended is not None
    # WatchState終了日は「今回のreconcile実行日」(evaluation_date_jst(now))であり、
    # detected_at(過去)ではない(#529実装: todayはreconcile実行時点のJST暦日)。
    assert ended.ended_at == _TODAY


# --- bounded processing: 上限件数を超えた分はremainingとして次回へ持ち越す -----


def test_bounded_processing_leaves_remainder_for_next_run(tmp_path: Path) -> None:
    trade_event_repo = TradeEventRecordRepository(store_dir=tmp_path)
    for i in range(3):
        trade_event_repo.create_pending(
            _pending_record(event_id=f"event-{i}", stock_code=f"100{i}")
        )
    watch_state_repo = WatchStateRepository(store_dir=tmp_path)

    outcome = _reconcile(trade_event_repo, watch_state_repo, max_records_per_run=2)

    assert outcome.processed == 2
    assert outcome.remaining == 1
    assert len(trade_event_repo.list_pending_with_raw()) == 1


# --- T7/T8: handler()レベルのfailure isolation --------------------------------
# 既存のtest_watchlist_batch_reconciler_handler.pyのhandler()呼び出しは、fake
# config(SimpleNamespaceでconfig.notification自体を持たない)を使っており、
# 本stepはAttributeErrorとしてtry/exceptに捕捉され、38件の既存テストは1件も
# 失敗しない(既存のwatchlist batch reconciliationが本stepの失敗の影響を
# 受けないことを、より厳しい欠落状況で反証済み)。本ファイルではさらに、
# reconcile_pending_trade_events()自体が例外を送出するケースを明示的に固定する。


class _RaisingReconcile:
    def __call__(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated trade_event_reconciliation failure")


_HANDLER_REGION = "ap-northeast-1"
_BATCH_TABLE = "jstock-batch_runs"
_PROGRESS_TABLE = "jstock-watchlist_candidate_progress"


def test_t7_trade_event_reconciliation_failure_does_not_break_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T7: trade replayが例外 → existing batch reconcileは継続する。

    `handler()`本体はDynamoDB(batch_runs/watchlist_candidate_progress)を
    複数箇所で参照するため、`test_watchlist_batch_reconciler_handler.py`の
    `dynamo`フィクスチャと同じ手法(moto + 2テーブル作成)で環境を用意する
    (CIランナーにはAWS_DEFAULT_REGIONが無く、region未設定だとboto3の
    クライアント構築自体がNoRegionErrorになるため)。
    """
    from jstock_advisor.lambda_handlers import watchlist_batch_reconciler_handler as handler_module

    monkeypatch.setenv("AWS_DEFAULT_REGION", _HANDLER_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setattr(handler_module, "reconcile_pending_trade_events", _RaisingReconcile())
    # 既存テストファイルのautouse fixture相当を最小限で再現する。
    monkeypatch.setattr(handler_module, "load_config", lambda: _minimal_handler_config())
    monkeypatch.setattr(handler_module, "build_real_provider_bundle", lambda now, config: object())
    monkeypatch.setattr(
        handler_module,
        "build_cached_provider_bundle",
        lambda providers, config, now: providers,
    )
    monkeypatch.setattr(handler_module, "_build_reconciler_line_client", lambda: _NoopLineClient())
    monkeypatch.setattr(
        handler_module, "_build_notification_service", lambda config, client: object()
    )
    monkeypatch.setattr(handler_module, "record_batch_audit", lambda **kw: None)
    monkeypatch.setattr(handler_module, "_publish_incident_envelope", lambda envelope: None)
    monkeypatch.setattr(handler_module, "_fetch_watchlist_worker_metrics", lambda now: {})

    with mock_aws():
        client = boto3.client("dynamodb", region_name=_HANDLER_REGION)
        client.create_table(
            TableName=_BATCH_TABLE,
            KeySchema=[{"AttributeName": "batch_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "batch_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName=_PROGRESS_TABLE,
            KeySchema=[
                {"AttributeName": "batch_id", "KeyType": "HASH"},
                {"AttributeName": "stock_code", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "batch_id", "AttributeType": "S"},
                {"AttributeName": "stock_code", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        result = handler_module.handler({}, object())

    assert result["candidates"] == 0  # 既存のwatchlist batch reconciliationは正常完了


def _minimal_handler_config() -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        watchlist_screening=SimpleNamespace(
            enabled=True,
            scheduled_run_enabled=True,
            batch_processing_timeout_hours=24,
            finalizing_stuck_threshold_minutes=15,
            max_finalize_retry_attempts=3,
            max_notification_retry_attempts=3,
            max_timeout_finalize_rows_per_run=500,
            auto_removal=SimpleNamespace(
                enabled=True,
                readd_cooldown_days=30,
                minimum_age_days=90,
                consecutive_not_qualified_required=3,
                minimum_not_qualified_span_days=28,
                stale_recheck_days=30,
                maximum_unconfirmed_days=180,
            ),
        ),
        holiday_calendar=SimpleNamespace(
            recurring_market_closures=SimpleNamespace(dates_mm_dd=[]),
            additional_closures=SimpleNamespace(dates=[]),
        ),
        notification=SimpleNamespace(
            trade_event_reconciliation=SimpleNamespace(max_records_per_run=200)
        ),
    )


class _NoopLineClient:
    def push_message(self, text: str) -> None:
        pass


def test_t8_existing_reconciliation_failure_does_not_prevent_prior_trade_event_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T8: existing batch reconcileが例外 → trade replay側を不必要に巻き込まない。

    本Issueの実装は、trade-event consumptionをwatchlist batch reconciliationの
    メインループより**前**に置いているため、メインループ側が後で例外を送出しても、
    trade-event側は既に完了している(呼び出し順序による保証)。"""
    from jstock_advisor.lambda_handlers import watchlist_batch_reconciler_handler as handler_module

    trade_event_repo = TradeEventRecordRepository(store_dir=tmp_path)
    trade_event_repo.create_pending(_pending_record())
    watch_state_repo = WatchStateRepository(store_dir=tmp_path)
    watch_state_repo.upsert(_active_watch_state())

    monkeypatch.setattr(handler_module, "TradeEventRecordRepository", lambda: trade_event_repo)
    monkeypatch.setattr(
        handler_module,
        "WatchStateService",
        lambda business_calendar: WatchStateService(
            business_calendar=business_calendar, repository=watch_state_repo
        ),
    )
    monkeypatch.setattr(handler_module, "load_config", lambda: _minimal_handler_config())
    monkeypatch.setattr(handler_module, "build_real_provider_bundle", lambda now, config: object())
    monkeypatch.setattr(
        handler_module,
        "build_cached_provider_bundle",
        lambda providers, config, now: providers,
    )
    monkeypatch.setattr(handler_module, "_build_reconciler_line_client", lambda: _NoopLineClient())
    monkeypatch.setattr(
        handler_module, "_build_notification_service", lambda config, client: object()
    )

    def _boom(statuses: object) -> list[Any]:
        raise RuntimeError("simulated existing batch reconciliation failure")

    monkeypatch.setattr(handler_module, "list_watchlist_batches_by_status", _boom)

    with pytest.raises(RuntimeError, match="simulated existing batch reconciliation failure"):
        handler_module.handler({}, object())

    # メインループ側の例外伝播より前に、trade-event consumptionは完了している。
    # handler()は実時刻(dt.datetime.now(dt.UTC))を使うため、ended_atは
    # 固定値ではなく「Noneではないこと」で完了を確認する。
    ended = _get_watch_state(watch_state_repo)
    assert ended is not None
    assert ended.ended_at is not None
    assert ended.end_reason == END_REASON_TRADE_EVENT
    assert trade_event_repo.list_pending_with_raw() == []


# --- DynamoDB実装: mark_consumed()がsparse GSIから正しく除外すること -----------

_REGION = "ap-northeast-1"
_DYNAMO_TABLE_NAME = "jstock-trade_event_records"


@pytest.fixture
def dynamo_lambda_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "holdings-watchlist")
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_DYNAMO_TABLE_NAME,
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "pending_marker", "AttributeType": "S"},
                {"AttributeName": "detected_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "pending-marker-index",
                    "KeySchema": [
                        {"AttributeName": "pending_marker", "KeyType": "HASH"},
                        {"AttributeName": "detected_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def test_mark_consumed_removes_pending_marker_from_dynamodb_gsi(dynamo_lambda_env: Any) -> None:
    """mark_consumed()適用後、DynamoDB実装上でpending_marker属性自体が
    トップレベルから消え、sparse GSI(pending-marker-index)から外れること
    (put_itemによる全体置換のため、Itemに含めなければ自動的に削除される)。"""
    repo = TradeEventRecordRepository()
    record = _pending_record()
    repo.create_pending(record)

    raw = repo._store.get_raw_data(record.event_id)
    assert raw is not None
    assert repo.mark_consumed(record, raw, _NOW) is True

    raw_item = dynamo_lambda_env.get_item(
        TableName=_DYNAMO_TABLE_NAME, Key={"event_id": {"S": record.event_id}}
    )["Item"]
    assert "pending_marker" not in raw_item
    found = repo._store.query_by_index(PENDING_INDEX_NAME, "pending_marker", "PENDING")
    assert found == []
    stored = repo.get(record.event_id)
    assert stored is not None
    assert stored.consumed_at == _NOW
    assert stored.pending_marker is None
