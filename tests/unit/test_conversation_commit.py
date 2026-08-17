"""conversation_commit.pyのテスト(LINEボタン起点会話型UI・実装プランv2 3節・
追加条件1「楽観ロック必須化」・追加条件2「ConversationState単一Delete」)。

moto(実DynamoDB互換バックエンド)で、TransactWriteItemsの実際の
ConditionExpression意味論(#data=:expected_data・attribute_not_exists)を
正確に検証する。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

from jstock_advisor.domain.entities.enums import (
    AccountType,
    ConversationAction,
    TransactionType,
)
from jstock_advisor.domain.entities.holding import Holding, PurchaseLot
from jstock_advisor.infrastructure.aws import conversation_commit, conversation_state_store
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.infrastructure.local_repository.transaction_repository import (
    TransactionRepository,
)
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
)
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.transaction_history_service import TransactionHistoryService
from jstock_advisor.services.watchlist_service import WatchlistService

_REGION = "ap-northeast-1"
_NOW = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)
_USER = "U1"
_STOCK = "8306"


@pytest.fixture
def moto_conversation_tables(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-line-webhook")
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "jstock")
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        for table_name, key in (
            ("jstock-conversation_states", "user_id"),
            ("jstock-transactions", "transaction_id"),
            ("jstock-purchase_lots", "lot_id"),
            ("jstock-holdings", "stock_code"),
            ("jstock-watchlist", "stock_code"),
        ):
            client.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        yield


def _start_buy_confirm(shares: int = 100, price: str = "1500") -> Any:
    conversation_state_store.start_or_replace(_USER, ConversationAction.BUY, _NOW)
    return conversation_state_store.record_input(
        _USER, ConversationAction.BUY, _STOCK, _NOW, shares=shares, price=Decimal(price)
    )


def _start_sell_confirm(shares: int, price: str = "1500") -> Any:
    conversation_state_store.start_or_replace(_USER, ConversationAction.SELL, _NOW)
    return conversation_state_store.record_input(
        _USER, ConversationAction.SELL, _STOCK, _NOW, shares=shares, price=Decimal(price)
    )


def _start_watch_confirm() -> Any:
    conversation_state_store.start_or_replace(_USER, ConversationAction.WATCH, _NOW)
    return conversation_state_store.record_input(_USER, ConversationAction.WATCH, _STOCK, _NOW)


def _seed_holding_with_one_lot(shares: int, price: str = "1000") -> None:
    lot = PurchaseLot(
        lot_id="existing-lot",
        stock_code=_STOCK,
        purchase_date=dt.date(2026, 8, 1),
        shares=shares,
        purchase_price=Decimal(price),
        account_type=AccountType.GENERAL,
    )
    PurchaseLotRepository().upsert(lot)
    holding = Holding(
        stock_code=_STOCK,
        stock_name=_STOCK,
        shares=shares,
        average_purchase_price=Decimal(price),
        total_purchase_amount=Decimal(price) * shares,
        first_purchase_date=dt.date(2026, 8, 1),
        last_purchase_date=dt.date(2026, 8, 1),
        account_type=AccountType.GENERAL,
        created_at=_NOW,
        updated_at=_NOW,
    )
    HoldingRepository().upsert(holding)


# --- commit_buy ---------------------------------------------------------


def test_commit_buy_succeeds_for_new_stock(moto_conversation_tables: None) -> None:
    state = _start_buy_confirm(shares=100, price="1500")
    assert state is not None
    portfolio = PortfolioService()
    plan = portfolio.build_purchase_write_plan(
        stock_code=_STOCK,
        stock_name=None,
        shares=100,
        purchase_price=Decimal("1500"),
        purchase_date=dt.date(2026, 8, 17),
        account_type=AccountType.GENERAL,
        now=_NOW,
    )
    transaction = TransactionHistoryService().build_execution_plan(
        transaction_id=state.operation_id,
        stock_code=_STOCK,
        transaction_type=TransactionType.BUY,
        shares=100,
        execution_price=Decimal("1500"),
        execution_date=dt.date(2026, 8, 17),
        now=_NOW,
    )

    ok = conversation_commit.commit_buy(_USER, state.operation_id, plan, transaction, _NOW)

    assert ok is True
    holding = HoldingRepository().get(_STOCK)
    assert holding is not None
    assert holding.shares == 100
    lots = PurchaseLotRepository().list_by_stock(_STOCK)
    assert len(lots) == 1
    assert TransactionRepository().get(state.operation_id) is not None
    assert conversation_state_store.get(_USER, _NOW) is None


def test_commit_buy_fails_when_existing_holding_changed_after_plan_built(
    moto_conversation_tables: None,
) -> None:
    """追加条件1: 計画構築後にHoldingが別経路で変更された場合、
    トランザクション全体を失敗させ、古い計画のまま書き込まない。"""
    _seed_holding_with_one_lot(shares=100, price="1000")
    state = _start_buy_confirm(shares=50, price="1500")
    assert state is not None
    portfolio = PortfolioService()
    plan = portfolio.build_purchase_write_plan(
        stock_code=_STOCK,
        stock_name=None,
        shares=50,
        purchase_price=Decimal("1500"),
        purchase_date=dt.date(2026, 8, 17),
        account_type=AccountType.GENERAL,
        now=_NOW,
    )
    transaction = TransactionHistoryService().build_execution_plan(
        transaction_id=state.operation_id,
        stock_code=_STOCK,
        transaction_type=TransactionType.ADDITIONAL_BUY,
        shares=50,
        execution_price=Decimal("1500"),
        execution_date=dt.date(2026, 8, 17),
        now=_NOW,
    )

    # 計画構築後、別経路(CSV等)でHoldingが変更されたことを模擬する。
    mutated = HoldingRepository().get(_STOCK)
    assert mutated is not None
    HoldingRepository().upsert(mutated.model_copy(update={"memo": "concurrent edit"}))

    ok = conversation_commit.commit_buy(_USER, state.operation_id, plan, transaction, _NOW)

    assert ok is False
    # ConversationStateは消費されず(まだCONFIRM_WAITING)、Transactionも作られない。
    assert conversation_state_store.get(_USER, _NOW) is not None
    assert TransactionRepository().get(state.operation_id) is None
    # 元の(競合させた側の)変更はそのまま保たれている。
    assert HoldingRepository().get(_STOCK).memo == "concurrent edit"  # type: ignore[union-attr]
    # 新規ロットは追加されない(既存の1件のみ)。
    assert len(PurchaseLotRepository().list_by_stock(_STOCK)) == 1


def test_commit_buy_fails_when_holding_created_concurrently_for_new_stock(
    moto_conversation_tables: None,
) -> None:
    """新規Holding(attribute_not_exists条件)についても、計画構築後に
    別経路で先に作成された場合は失敗すること。"""
    state = _start_buy_confirm(shares=100, price="1500")
    assert state is not None
    portfolio = PortfolioService()
    plan = portfolio.build_purchase_write_plan(
        stock_code=_STOCK,
        stock_name=None,
        shares=100,
        purchase_price=Decimal("1500"),
        purchase_date=dt.date(2026, 8, 17),
        account_type=AccountType.GENERAL,
        now=_NOW,
    )
    transaction = TransactionHistoryService().build_execution_plan(
        transaction_id=state.operation_id,
        stock_code=_STOCK,
        transaction_type=TransactionType.BUY,
        shares=100,
        execution_price=Decimal("1500"),
        execution_date=dt.date(2026, 8, 17),
        now=_NOW,
    )

    # 計画構築後、別経路が先にHoldingを作成した状況を模擬する。
    _seed_holding_with_one_lot(shares=10, price="999")

    ok = conversation_commit.commit_buy(_USER, state.operation_id, plan, transaction, _NOW)

    assert ok is False
    assert conversation_state_store.get(_USER, _NOW) is not None
    # 競合させた側のHoldingが上書きされていないこと。
    assert HoldingRepository().get(_STOCK).shares == 10  # type: ignore[union-attr]


def test_commit_buy_fails_on_operation_id_mismatch(moto_conversation_tables: None) -> None:
    state = _start_buy_confirm(shares=100, price="1500")
    assert state is not None
    portfolio = PortfolioService()
    plan = portfolio.build_purchase_write_plan(
        stock_code=_STOCK,
        stock_name=None,
        shares=100,
        purchase_price=Decimal("1500"),
        purchase_date=dt.date(2026, 8, 17),
        account_type=AccountType.GENERAL,
        now=_NOW,
    )
    transaction = TransactionHistoryService().build_execution_plan(
        transaction_id=state.operation_id,
        stock_code=_STOCK,
        transaction_type=TransactionType.BUY,
        shares=100,
        execution_price=Decimal("1500"),
        execution_date=dt.date(2026, 8, 17),
        now=_NOW,
    )

    ok = conversation_commit.commit_buy(_USER, "wrong-op-id", plan, transaction, _NOW)

    assert ok is False
    assert HoldingRepository().get(_STOCK) is None
    assert TransactionRepository().get(state.operation_id) is None
    assert conversation_state_store.get(_USER, _NOW) is not None


# --- commit_sell ---------------------------------------------------------


def test_commit_sell_full_sell_deletes_lot_and_holding(moto_conversation_tables: None) -> None:
    _seed_holding_with_one_lot(shares=100, price="1000")
    state = _start_sell_confirm(shares=100, price="1500")
    assert state is not None
    portfolio = PortfolioService()
    plan = portfolio.build_sale_write_plan(_STOCK, 100, now=_NOW)
    transaction = TransactionHistoryService().build_execution_plan(
        transaction_id=state.operation_id,
        stock_code=_STOCK,
        transaction_type=TransactionType.FULL_SELL,
        shares=100,
        execution_price=Decimal("1500"),
        execution_date=dt.date(2026, 8, 17),
        now=_NOW,
    )

    ok = conversation_commit.commit_sell(_USER, state.operation_id, plan, transaction, _NOW)

    assert ok is True
    assert HoldingRepository().get(_STOCK) is None
    assert PurchaseLotRepository().list_by_stock(_STOCK) == []
    assert TransactionRepository().get(state.operation_id) is not None
    assert conversation_state_store.get(_USER, _NOW) is None


def test_commit_sell_partial_sell_updates_lot_and_holding(moto_conversation_tables: None) -> None:
    _seed_holding_with_one_lot(shares=100, price="1000")
    state = _start_sell_confirm(shares=30, price="1500")
    assert state is not None
    portfolio = PortfolioService()
    plan = portfolio.build_sale_write_plan(_STOCK, 30, now=_NOW)
    transaction = TransactionHistoryService().build_execution_plan(
        transaction_id=state.operation_id,
        stock_code=_STOCK,
        transaction_type=TransactionType.PARTIAL_SELL,
        shares=30,
        execution_price=Decimal("1500"),
        execution_date=dt.date(2026, 8, 17),
        now=_NOW,
    )

    ok = conversation_commit.commit_sell(_USER, state.operation_id, plan, transaction, _NOW)

    assert ok is True
    holding = HoldingRepository().get(_STOCK)
    assert holding is not None
    assert holding.shares == 70
    lots = PurchaseLotRepository().list_by_stock(_STOCK)
    assert len(lots) == 1
    assert lots[0].shares == 70
    assert conversation_state_store.get(_USER, _NOW) is None


def test_commit_sell_fails_when_lot_changed_after_plan_built(
    moto_conversation_tables: None,
) -> None:
    _seed_holding_with_one_lot(shares=100, price="1000")
    state = _start_sell_confirm(shares=100, price="1500")
    assert state is not None
    portfolio = PortfolioService()
    plan = portfolio.build_sale_write_plan(_STOCK, 100, now=_NOW)
    transaction = TransactionHistoryService().build_execution_plan(
        transaction_id=state.operation_id,
        stock_code=_STOCK,
        transaction_type=TransactionType.FULL_SELL,
        shares=100,
        execution_price=Decimal("1500"),
        execution_date=dt.date(2026, 8, 17),
        now=_NOW,
    )

    # 計画構築後、別経路でロットが変更された状況を模擬する。
    lot = PurchaseLotRepository().get("existing-lot")
    assert lot is not None
    PurchaseLotRepository().upsert(lot.model_copy(update={"shares": 40}))

    ok = conversation_commit.commit_sell(_USER, state.operation_id, plan, transaction, _NOW)

    assert ok is False
    assert conversation_state_store.get(_USER, _NOW) is not None
    assert PurchaseLotRepository().get("existing-lot").shares == 40  # type: ignore[union-attr]


# --- commit_watch ---------------------------------------------------------


def test_commit_watch_creates_item_and_consumes_state(moto_conversation_tables: None) -> None:
    state = _start_watch_confirm()
    assert state is not None
    watchlist_item = WatchlistService().build_add_item_plan(stock_code=_STOCK)

    ok = conversation_commit.commit_watch(_USER, state.operation_id, watchlist_item, _NOW)

    assert ok is True
    item = WatchlistRepository().get(_STOCK)
    assert item is not None
    assert item.stock_code == _STOCK
    assert conversation_state_store.get(_USER, _NOW) is None


def test_commit_watch_fails_on_operation_id_mismatch(moto_conversation_tables: None) -> None:
    state = _start_watch_confirm()
    assert state is not None
    watchlist_item = WatchlistService().build_add_item_plan(stock_code=_STOCK)

    ok = conversation_commit.commit_watch(_USER, "wrong-op-id", watchlist_item, _NOW)

    assert ok is False
    assert WatchlistRepository().get(_STOCK) is None
    assert conversation_state_store.get(_USER, _NOW) is not None


# --- TransactionConflictException リトライ ---------------------------------


class _FlakyTransactClient:
    def __init__(self, real_client: Any, fail_times: int, error_code: str) -> None:
        self._real_client = real_client
        self._fail_times = fail_times
        self._error_code = error_code
        self.call_count = 0

    def transact_write_items(self, **kwargs: Any) -> Any:
        self.call_count += 1
        if self.call_count <= self._fail_times:
            raise ClientError(
                {"Error": {"Code": self._error_code, "Message": "conflict"}},
                "TransactWriteItems",
            )
        return self._real_client.transact_write_items(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real_client, name)


def _patch_flaky_transact_client(
    monkeypatch: pytest.MonkeyPatch, fail_times: int, error_code: str
) -> _FlakyTransactClient:
    real_client = boto3.client("dynamodb", region_name=_REGION)
    flaky = _FlakyTransactClient(real_client, fail_times=fail_times, error_code=error_code)
    monkeypatch.setattr(conversation_commit.boto3, "client", lambda *a, **kw: flaky)
    return flaky


def test_commit_watch_retries_transaction_conflict_then_succeeds(
    moto_conversation_tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _start_watch_confirm()
    assert state is not None
    watchlist_item = WatchlistService().build_add_item_plan(stock_code=_STOCK)
    flaky = _patch_flaky_transact_client(
        monkeypatch, fail_times=1, error_code="TransactionConflictException"
    )

    ok = conversation_commit.commit_watch(_USER, state.operation_id, watchlist_item, _NOW)

    assert ok is True
    assert flaky.call_count == 2
    assert WatchlistRepository().get(_STOCK) is not None


def test_commit_watch_exhausts_retries_and_returns_false(
    moto_conversation_tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _start_watch_confirm()
    assert state is not None
    watchlist_item = WatchlistService().build_add_item_plan(stock_code=_STOCK)
    flaky = _patch_flaky_transact_client(
        monkeypatch,
        fail_times=conversation_commit._MAX_TRANSACTION_CONFLICT_RETRY_ATTEMPTS,
        error_code="TransactionConflictException",
    )

    ok = conversation_commit.commit_watch(_USER, state.operation_id, watchlist_item, _NOW)

    assert ok is False
    assert flaky.call_count == conversation_commit._MAX_TRANSACTION_CONFLICT_RETRY_ATTEMPTS
    # 期限内・条件一致のままなのでConversationStateは消費されていない。
    assert conversation_state_store.get(_USER, _NOW) is not None


def test_commit_watch_does_not_retry_genuine_conditional_check_failure(
    moto_conversation_tables: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _start_watch_confirm()
    assert state is not None
    watchlist_item = WatchlistService().build_add_item_plan(stock_code=_STOCK)
    flaky = _patch_flaky_transact_client(
        monkeypatch, fail_times=99, error_code="ConditionalCheckFailedException"
    )

    ok = conversation_commit.commit_watch(_USER, state.operation_id, watchlist_item, _NOW)

    assert ok is False
    assert flaky.call_count == 1
