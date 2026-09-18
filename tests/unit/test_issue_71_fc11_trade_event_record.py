"""Issue #71 F-C11 Phase 1: TradeEventRecord永続化契約のcrash-point A/B。

Phase 1のscope(USER承認済みスコープ分割、TARO-20260918-045)は
TradeEventRecordの永続化契約・deterministic event identity・
insert_if_absent・persist-event-before-snapshot順序・crash-point A/Bの
regression testに限定される。pending-event consumption(crash-point C/D/E)は
Phase 2のscopeであり、本ファイルでは対象としない。

架空の保有データのみを使用する(実在の銘柄コード・所有者名・保有数量は
含まない)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import TradeCooldownConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import AccountType, TransactionType
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holdings_snapshot import HoldingsSnapshotEntry
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.entities.trade_event_record import (
    PENDING_MARKER_VALUE,
    TradeEventRecord,
    build_trade_event_id,
)
from jstock_advisor.infrastructure.local_repository.holdings_snapshot_repository import (
    HoldingsSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.trade_event_record_repository import (
    PENDING_INDEX_NAME,
    TradeEventRecordRepository,
)
from jstock_advisor.services.trade_cooldown_service import TradeCooldownService

_NOW = dt.datetime(2026, 8, 20, 23, 30, tzinfo=dt.UTC)  # 2026-08-21 08:30 JST(金)
_HID = build_holding_id(DEFAULT_OWNER, "2914")


# --- event_id構成: deterministic + holding_idを平文で含まない ------------------


def test_build_trade_event_id_is_deterministic() -> None:
    first = build_trade_event_id(_HID, dt.date(2026, 8, 21))
    second = build_trade_event_id(_HID, dt.date(2026, 8, 21))
    assert first == second


def test_build_trade_event_id_does_not_contain_plaintext_holding_id() -> None:
    event_id = build_trade_event_id(_HID, dt.date(2026, 8, 21))
    assert _HID not in event_id
    assert DEFAULT_OWNER not in event_id


def test_build_trade_event_id_differs_by_holding_id() -> None:
    other_hid = build_holding_id(DEFAULT_OWNER, "9999")
    assert build_trade_event_id(_HID, dt.date(2026, 8, 21)) != build_trade_event_id(
        other_hid, dt.date(2026, 8, 21)
    )


def test_build_trade_event_id_differs_by_detected_at() -> None:
    assert build_trade_event_id(_HID, dt.date(2026, 8, 21)) != build_trade_event_id(
        _HID, dt.date(2026, 8, 22)
    )


# --- TradeEventRecordRepository.create_pending(): 冪等性とGSI属性 --------------


def _sample_record(event_id: str | None = None, consumed_at: dt.datetime | None = None) -> Any:
    return TradeEventRecord(
        event_id=event_id or build_trade_event_id(_HID, dt.date(2026, 8, 21)),
        holding_id=_HID,
        owner=DEFAULT_OWNER,
        stock_code="2914",
        event_type=TransactionType.PARTIAL_SELL,
        detected_at=dt.date(2026, 8, 21),
        shares=50,
        average_purchase_price=Decimal("1000"),
        created_at=_NOW,
        consumed_at=consumed_at,
    )


def test_create_pending_returns_true_on_first_call(tmp_path: Path) -> None:
    repo = TradeEventRecordRepository(store_dir=tmp_path)
    assert repo.create_pending(_sample_record()) is True


def test_create_pending_returns_false_on_duplicate_call(tmp_path: Path) -> None:
    repo = TradeEventRecordRepository(store_dir=tmp_path)
    record = _sample_record()
    repo.create_pending(record)
    assert repo.create_pending(record) is False


def test_create_pending_does_not_overwrite_already_consumed_record(tmp_path: Path) -> None:
    """★ リトライで誤って消費済み状態を巻き戻さないこと(insert_if_absent()の
    存在チェックにより、2回目以降のcreate_pending()は既存項目に一切触れない)。
    """
    repo = TradeEventRecordRepository(store_dir=tmp_path)
    event_id = build_trade_event_id(_HID, dt.date(2026, 8, 21))
    repo.create_pending(_sample_record(event_id=event_id))

    # Phase 2相当の消費(consumed_at設定)をシミュレートする(直接store経由)。
    consumed_at = _NOW + dt.timedelta(hours=1)
    stored = repo.get(event_id)
    assert stored is not None
    repo._store.upsert(stored.model_copy(update={"consumed_at": consumed_at}))

    # 同一event_idで再度create_pending()を呼んでも(リトライ相当)、
    # 既存の消費済み状態(consumed_at)は保持されること。
    created_again = repo.create_pending(_sample_record(event_id=event_id))
    assert created_again is False
    after = repo.get(event_id)
    assert after is not None
    assert after.consumed_at == consumed_at


def test_create_pending_sets_pending_marker_queryable_via_gsi(tmp_path: Path) -> None:
    """新規作成時、pending_marker属性がGSI相当のquery_by_index()で引けること
    (ローカルJSON実装はGSIを持たないため、find()相当のフィルタとして動作するが、
    インターフェース契約としてquery_by_index()が使えることを確認する)。
    """
    repo = TradeEventRecordRepository(store_dir=tmp_path)
    record = _sample_record()
    repo.create_pending(record)

    pending = repo._store.query_by_index(PENDING_INDEX_NAME, "pending_marker", PENDING_MARKER_VALUE)
    assert [r.event_id for r in pending] == [record.event_id]


# --- crash-point A/B: TradeCooldownService._do_detect_and_apply()経由 ----------


class _RaisingBeforePersistTradeEventRepository:
    """crash-point A: event永続化(insert_if_absent)前にcrashする状況を模倣する。

    create_pending()自体を呼ぶ前に例外を送出する(=insert_if_absentへ一切
    到達しない)。
    """

    def create_pending(self, record: TradeEventRecord) -> bool:
        raise RuntimeError("simulated crash before event persisted (crash-point A)")


class _RaisingSnapshotRepository:
    """crash-point B: event永続化後・snapshot更新前にcrashする状況を模倣する。

    list_all()は実体へ委譲し(previous_entriesの読み取りは正常に行う)、
    upsert()呼び出し時にのみ例外を送出する。
    """

    def __init__(self, real: HoldingsSnapshotRepository) -> None:
        self._real = real

    def list_all(self) -> list[HoldingsSnapshotEntry]:
        return self._real.list_all()

    def upsert(self, entry: HoldingsSnapshotEntry) -> None:
        raise RuntimeError(
            "simulated crash after event persisted, before snapshot updated (crash-point B)"
        )

    def get(self, holding_id: str) -> HoldingsSnapshotEntry | None:
        return self._real.get(holding_id)


def _config() -> TradeCooldownConfig:
    return TradeCooldownConfig(
        enabled=True, buy_business_days=5, sell_business_days=5, partial_trade_business_days=3
    )


def _calendar() -> BusinessCalendar:
    return BusinessCalendar.from_config(load_config().holiday_calendar)


def _seed_baseline(repo: HoldingsSnapshotRepository, shares: int) -> None:
    repo.upsert(
        HoldingsSnapshotEntry(
            owner=DEFAULT_OWNER,
            holding_id=_HID,
            stock_code="2914",
            shares=shares,
            average_purchase_price=Decimal("1000") if shares > 0 else None,
            recorded_at=dt.date(2026, 8, 14),
            active_holding=shares > 0,
        )
    )


def _current_holdings(shares: int) -> dict[str, Holding]:
    return {
        _HID: Holding(
            owner=DEFAULT_OWNER,
            holding_id=_HID,
            stock_code="2914",
            stock_name="架空銘柄",
            shares=shares,
            average_purchase_price=Decimal("1000"),
            total_purchase_amount=Decimal("1000") * shares,
            first_purchase_date=_NOW.date(),
            last_purchase_date=_NOW.date(),
            account_type=AccountType.SPECIFIC,
            created_at=_NOW,
            updated_at=_NOW,
        )
    }


def test_crash_point_a_recovers_on_next_execution(tmp_path: Path) -> None:
    """A: event永続化前にcrash。次回実行時、snapshotが未更新のままのため
    detect_trade_events()が同じイベントを再検知し、event永続化+snapshot更新が
    完遂すること。"""
    snapshot_repo = HoldingsSnapshotRepository(store_dir=tmp_path)
    _seed_baseline(snapshot_repo, shares=100)
    current_holdings = _current_holdings(shares=50)  # 一部売却

    crashing_service = TradeCooldownService(
        business_calendar=_calendar(),
        config=_config(),
        repository=snapshot_repo,
        execution_context=ExecutionContext.normal(),
        trade_event_repository=_RaisingBeforePersistTradeEventRepository(),  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match="crash-point A"):
        crashing_service.detect_and_apply(current_holdings, _NOW)

    # snapshotは未更新のまま(検知処理自体がevent永続化の前でcrashしたため)。
    entry_after_crash_a = snapshot_repo.get(_HID)
    assert entry_after_crash_a is not None
    assert entry_after_crash_a.shares == 100

    # 次回実行(正常な依存関係で再構築): 同じイベントが再検知され完遂すること。
    trade_event_repo = TradeEventRecordRepository(store_dir=tmp_path)
    recovered_service = TradeCooldownService(
        business_calendar=_calendar(),
        config=_config(),
        repository=snapshot_repo,
        execution_context=ExecutionContext.normal(),
        trade_event_repository=trade_event_repo,
    )
    outcome = recovered_service.detect_and_apply(current_holdings, _NOW + dt.timedelta(minutes=1))

    assert outcome.confirmed is True
    assert len(outcome.events) == 1
    entry_after_recovery = snapshot_repo.get(_HID)
    assert entry_after_recovery is not None
    assert entry_after_recovery.shares == 50
    event_id = build_trade_event_id(_HID, dt.date(2026, 8, 21))
    assert trade_event_repo.get(event_id) is not None


def test_crash_point_b_recovers_without_duplicating_the_event_record(tmp_path: Path) -> None:
    """B: event永続化後・snapshot更新前にcrash。次回実行時、snapshotが依然
    未更新のため同じイベントが再検知され、insert_if_absent()はFalseを返すが
    snapshot更新(step2)は実行され完遂すること。TradeEventRecordの重複作成が
    無いこと(1件のみ存在)。"""
    snapshot_repo = HoldingsSnapshotRepository(store_dir=tmp_path)
    _seed_baseline(snapshot_repo, shares=100)
    trade_event_repo = TradeEventRecordRepository(store_dir=tmp_path)
    current_holdings = _current_holdings(shares=50)  # 一部売却

    crashing_service = TradeCooldownService(
        business_calendar=_calendar(),
        config=_config(),
        repository=_RaisingSnapshotRepository(snapshot_repo),  # type: ignore[arg-type]
        execution_context=ExecutionContext.normal(),
        trade_event_repository=trade_event_repo,
    )
    with pytest.raises(RuntimeError, match="crash-point B"):
        crashing_service.detect_and_apply(current_holdings, _NOW)

    # eventは永続化済み、snapshotは未更新のまま。
    event_id = build_trade_event_id(_HID, dt.date(2026, 8, 21))
    assert trade_event_repo.get(event_id) is not None
    entry_after_crash_b = snapshot_repo.get(_HID)
    assert entry_after_crash_b is not None
    assert entry_after_crash_b.shares == 100

    # 次回実行(正常な依存関係で再構築): snapshotが未更新のため同じイベントが
    # 再検知され、insert_if_absent()はFalseだがsnapshot更新は完遂すること。
    recovered_service = TradeCooldownService(
        business_calendar=_calendar(),
        config=_config(),
        repository=snapshot_repo,
        execution_context=ExecutionContext.normal(),
        trade_event_repository=trade_event_repo,
    )
    outcome = recovered_service.detect_and_apply(current_holdings, _NOW + dt.timedelta(minutes=1))

    assert outcome.confirmed is True
    assert len(outcome.events) == 1
    entry_after_recovery = snapshot_repo.get(_HID)
    assert entry_after_recovery is not None
    assert entry_after_recovery.shares == 50

    # TradeEventRecordの重複作成が無いこと(1件のみ存在)。
    all_records = trade_event_repo._store.list_all()
    assert len(all_records) == 1
    assert all_records[0].event_id == event_id


def test_existing_responsibility_boundary_is_preserved() -> None:
    """★ Phase 1完了後もtrade_cooldown_service.pyがWatchStateService/
    WatchStateRepositoryを一切import・呼び出ししないこと(TARO-20260918-045の
    禁止範囲: 既存の責務境界の変更をしないこと)。docstring/コメントで責務境界を
    説明すること自体は許容するため、import文の有無だけを見る(文字列
    "WatchState"の有無ではない。コメントでの言及まで禁止すると本Issueの
    説明自体が書けなくなる)。"""
    import ast
    import inspect

    from jstock_advisor.services import trade_cooldown_service

    tree = ast.parse(inspect.getsource(trade_cooldown_service))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert not any("WatchState" in name for name in imported_names)
