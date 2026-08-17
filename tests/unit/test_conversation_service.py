"""ConversationServiceの結合的なテスト(LINEボタン起点会話型UI・実装プランv2)。

moto(実DynamoDB互換バックエンド)上で、postback起点の状態遷移
(start→入力→確認→confirm/retry/cancel)をエンドツーエンドに検証する。
StockDisplayNameResolverは全ソース未接続のため最終的にstock_codeへ
フォールバックする(JpxStockNameSource等が例外を握りつぶす既存設計により、
テスト用インフラなしでも安全に動作する)。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.entities.enums import ConversationStateName
from jstock_advisor.infrastructure.aws import conversation_state_store
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
)
from jstock_advisor.services.conversation_service import ConversationService

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


@pytest.fixture
def service() -> ConversationService:
    return ConversationService()


# --- BUY: start → 入力 → confirm ------------------------------------------


def test_buy_flow_start_input_confirm(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    start_reply = service.handle_postback(_USER, "start_buy", None, _NOW)
    assert "銘柄コード" in start_reply.text
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    assert state.state == ConversationStateName.INPUT_WAITING

    input_reply = service.handle_text_input(_USER, state, "8306,100,1500", _NOW)
    assert "登録します" in input_reply.text
    assert input_reply.quick_reply is not None
    assert {b.postback_data.split("&")[0] for b in input_reply.quick_reply} == {
        "action=confirm",
        "action=retry",
        "action=cancel",
    }
    confirm_state = conversation_state_store.get(_USER, _NOW)
    assert confirm_state is not None
    assert confirm_state.state == ConversationStateName.CONFIRM_WAITING
    op = confirm_state.operation_id

    confirm_reply = service.handle_postback(_USER, "confirm", op, _NOW)
    assert "登録しました" in confirm_reply.text
    holding = HoldingRepository().get(_STOCK)
    assert holding is not None
    assert holding.shares == 100
    assert PurchaseLotRepository().list_by_stock(_STOCK)[0].purchase_price == Decimal("1500")
    assert conversation_state_store.get(_USER, _NOW) is None


def test_buy_input_invalid_stock_code_stays_input_waiting(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    service.handle_postback(_USER, "start_buy", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    reply = service.handle_text_input(_USER, state, "ABC,100,1500", _NOW)
    assert "銘柄コードが不正" in reply.text
    unchanged = conversation_state_store.get(_USER, _NOW)
    assert unchanged is not None
    assert unchanged.state == ConversationStateName.INPUT_WAITING


def test_buy_input_wrong_field_count_reprompts(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    service.handle_postback(_USER, "start_buy", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    reply = service.handle_text_input(_USER, state, "8306,100", _NOW)
    assert "銘柄コード" in reply.text
    assert conversation_state_store.get(_USER, _NOW).state == ConversationStateName.INPUT_WAITING  # type: ignore[union-attr]


# --- SELL: 保有チェック ------------------------------------------------


def _seed_holding(shares: int = 100) -> None:
    from jstock_advisor.domain.entities.enums import AccountType
    from jstock_advisor.domain.entities.holding import Holding, PurchaseLot

    PurchaseLotRepository().upsert(
        PurchaseLot(
            lot_id="lot-1",
            stock_code=_STOCK,
            purchase_date=dt.date(2026, 8, 1),
            shares=shares,
            purchase_price=Decimal("1000"),
            account_type=AccountType.GENERAL,
        )
    )
    HoldingRepository().upsert(
        Holding(
            stock_code=_STOCK,
            stock_name=_STOCK,
            shares=shares,
            average_purchase_price=Decimal("1000"),
            total_purchase_amount=Decimal("1000") * shares,
            first_purchase_date=dt.date(2026, 8, 1),
            last_purchase_date=dt.date(2026, 8, 1),
            account_type=AccountType.GENERAL,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )


def test_sell_flow_full_sell(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    _seed_holding(shares=100)
    service.handle_postback(_USER, "start_sell", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    input_reply = service.handle_text_input(_USER, state, "8306,100,1800", _NOW)
    assert "売却" in input_reply.text
    confirm_state = conversation_state_store.get(_USER, _NOW)
    assert confirm_state is not None

    confirm_reply = service.handle_postback(_USER, "confirm", confirm_state.operation_id, _NOW)

    assert "登録しました" in confirm_reply.text
    assert HoldingRepository().get(_STOCK) is None
    assert conversation_state_store.get(_USER, _NOW) is None


def test_sell_input_rejects_shares_exceeding_holding(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    _seed_holding(shares=50)
    service.handle_postback(_USER, "start_sell", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    reply = service.handle_text_input(_USER, state, "8306,100,1800", _NOW)
    assert "保有株数" in reply.text
    assert conversation_state_store.get(_USER, _NOW).state == ConversationStateName.INPUT_WAITING  # type: ignore[union-attr]


def test_sell_input_rejects_when_not_holding(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    service.handle_postback(_USER, "start_sell", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    reply = service.handle_text_input(_USER, state, "8306,100,1800", _NOW)
    assert "保有銘柄として登録されていません" in reply.text


# --- WATCH ---------------------------------------------------------------


def test_watch_flow(moto_conversation_tables: None, service: ConversationService) -> None:
    service.handle_postback(_USER, "start_watch", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    input_reply = service.handle_text_input(_USER, state, "8306", _NOW)
    assert "ウォッチリストに追加します" in input_reply.text
    confirm_state = conversation_state_store.get(_USER, _NOW)
    assert confirm_state is not None

    confirm_reply = service.handle_postback(_USER, "confirm", confirm_state.operation_id, _NOW)

    assert "ウォッチリストに追加しました" in confirm_reply.text
    assert WatchlistRepository().get(_STOCK) is not None
    assert conversation_state_store.get(_USER, _NOW) is None


# --- retry / cancel --------------------------------------------------------


def test_retry_returns_to_input_waiting(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    service.handle_postback(_USER, "start_buy", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    service.handle_text_input(_USER, state, "8306,100,1500", _NOW)
    confirm_state = conversation_state_store.get(_USER, _NOW)
    assert confirm_state is not None

    retry_reply = service.handle_postback(_USER, "retry", confirm_state.operation_id, _NOW)

    assert "購入記録を開始します" in retry_reply.text
    after = conversation_state_store.get(_USER, _NOW)
    assert after is not None
    assert after.state == ConversationStateName.INPUT_WAITING
    assert after.stock_code is None


def test_cancel_deletes_state(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    service.handle_postback(_USER, "start_buy", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    service.handle_text_input(_USER, state, "8306,100,1500", _NOW)
    confirm_state = conversation_state_store.get(_USER, _NOW)
    assert confirm_state is not None

    cancel_reply = service.handle_postback(_USER, "cancel", confirm_state.operation_id, _NOW)

    assert "キャンセルしました" in cancel_reply.text
    assert conversation_state_store.get(_USER, _NOW) is None


def test_confirm_with_stale_operation_id_returns_no_active_operation(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    service.handle_postback(_USER, "start_buy", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    service.handle_text_input(_USER, state, "8306,100,1500", _NOW)

    reply = service.handle_postback(_USER, "confirm", "stale-op-id", _NOW)

    assert "有効な操作がありません" in reply.text
    # 何も変更されない(まだCONFIRM_WAITINGのまま)。
    assert conversation_state_store.get(_USER, _NOW).state == (  # type: ignore[union-attr]
        ConversationStateName.CONFIRM_WAITING
    )


def test_confirm_without_any_state_returns_no_active_operation(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    reply = service.handle_postback(_USER, "confirm", "anything", _NOW)
    assert "有効な操作がありません" in reply.text


# --- CONFIRM_WAITING中の想定外テキスト -------------------------------------


def test_unexpected_text_during_confirm_waiting_does_not_change_state(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    service.handle_postback(_USER, "start_buy", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    service.handle_text_input(_USER, state, "8306,100,1500", _NOW)
    confirm_state = conversation_state_store.get(_USER, _NOW)
    assert confirm_state is not None

    reply = service.handle_text_input(_USER, confirm_state, "9999,1,1", _NOW)

    assert "ボタンから操作してください" in reply.text
    unchanged = conversation_state_store.get(_USER, _NOW)
    assert unchanged is not None
    assert unchanged.operation_id == confirm_state.operation_id
    assert unchanged.stock_code == "8306"


def test_unknown_postback_action_returns_guidance(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    reply = service.handle_postback(_USER, "some_unknown_action", None, _NOW)
    assert "認識できない操作です" in reply.text
