"""Issue #592(#128 A5a): LINE会話から買付余力の参照・棚卸し更新を行えるようにする。

moto(実DynamoDB互換バックエンド)上で、postback/text起点の状態遷移
(owner未確定→owner確定→amount入力→confirm)をエンドツーエンドに検証する。
test_conversation_service.pyと同じ構成パターンを踏襲する。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.entities.enums import (
    AccountType,
    AvailableCashUpdateType,
    ConversationAction,
    ConversationStateName,
)
from jstock_advisor.infrastructure.aws import conversation_commit, conversation_state_store
from jstock_advisor.infrastructure.local_repository.available_cash_repository import (
    AvailableCashRepository,
)
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.services.conversation_service import ConversationService

_REGION = "ap-northeast-1"
_NOW = dt.datetime(2026, 9, 26, 8, 0, tzinfo=dt.UTC)
_USER = "U1"
_KNOWN_OWNER = "owner-a"
_NEW_OWNER = "owner-b"


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
            ("jstock-holdings_v2", "holding_id"),
            ("jstock-purchase_lots", "lot_id"),
            ("jstock-available_cash", "owner"),
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


def _register_holding_for_known_owner() -> None:
    """owner一覧のQuick Replyに`_KNOWN_OWNER`を出すため、保有銘柄を1件登録する
    (#589/#594と同様、AvailableCashとHoldingsは独立領域のため、owner一覧
    生成は既存のHoldingsViewService.list_owners()をそのまま再利用する)。
    """
    from jstock_advisor.services.portfolio_service import PortfolioService

    PortfolioService(
        holding_repository=HoldingRepository(), lot_repository=PurchaseLotRepository()
    ).register_purchase(
        owner=_KNOWN_OWNER,
        stock_code="8306",
        stock_name=None,
        shares=100,
        purchase_price=Decimal("1500"),
        purchase_date=_NOW.date(),
        account_type=AccountType.GENERAL,
    )


# --- start: owner未選択 -----------------------------------------------------


def test_start_shows_owner_quick_reply_when_known_owner_exists(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    _register_holding_for_known_owner()
    reply = service.handle_postback(_USER, "start_available_cash_reconcile", None, _NOW)
    assert "所有者を選択" in reply.text
    assert reply.quick_reply is not None
    assert any(b.label == _KNOWN_OWNER for b in reply.quick_reply)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    assert state.action == ConversationAction.AVAILABLE_CASH_RECONCILE
    assert state.state == ConversationStateName.INPUT_WAITING
    assert state.owner is None


def test_start_without_any_known_owner_still_allows_free_text(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    """D1(USER承認): 保有0件の新規ownerもLINE側で棚卸しできる。"""
    reply = service.handle_postback(_USER, "start_available_cash_reconcile", None, _NOW)
    assert reply.quick_reply is None
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    assert state.owner is None


# --- owner確定(自由テキスト。新規owner) -------------------------------------


def test_free_text_owner_input_shows_unregistered_and_prompts_amount(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    service.handle_postback(_USER, "start_available_cash_reconcile", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None

    reply = service.handle_text_input(_USER, state, _NEW_OWNER, _NOW)
    assert "未登録" in reply.text
    assert "新しい買付余力を入力してください" in reply.text

    updated = conversation_state_store.get(_USER, _NOW)
    assert updated is not None
    assert updated.owner == _NEW_OWNER
    assert updated.state == ConversationStateName.INPUT_WAITING  # amountはまだ未確定


def test_free_text_invalid_owner_is_rejected_without_state_change(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    service.handle_postback(_USER, "start_available_cash_reconcile", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None

    reply = service.handle_text_input(_USER, state, "所有者#不正", _NOW)
    assert "所有者の指定が不正です" in reply.text

    unchanged = conversation_state_store.get(_USER, _NOW)
    assert unchanged is not None
    assert unchanged.owner is None


# --- owner確定(Quick Reply postback) ---------------------------------------


def test_owner_via_postback_shows_current_value_and_prompts_amount(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    _register_holding_for_known_owner()
    start_reply = service.handle_postback(_USER, "start_available_cash_reconcile", None, _NOW)
    op = start_reply.quick_reply[0].postback_data.split("op=")[1]

    reply = service.handle_postback(
        _USER, "start_available_cash_reconcile", op, _NOW, owner=_KNOWN_OWNER
    )
    assert "未登録" in reply.text  # AvailableCash自体はまだ未登録(Holdingsとは独立)

    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    assert state.owner == _KNOWN_OWNER


def test_owner_via_postback_with_stale_op_is_rejected(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    reply = service.handle_postback(
        _USER, "start_available_cash_reconcile", "stale-op", _NOW, owner=_KNOWN_OWNER
    )
    assert "状態が変わりました" in reply.text


# --- amount入力 -------------------------------------------------------------


def test_amount_input_negative_is_rejected_without_state_change(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    service.handle_postback(_USER, "start_available_cash_reconcile", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    service.handle_text_input(_USER, state, _KNOWN_OWNER, _NOW)
    state_with_owner = conversation_state_store.get(_USER, _NOW)
    assert state_with_owner is not None

    reply = service.handle_text_input(_USER, state_with_owner, "-1", _NOW)
    assert "0以上の数値" in reply.text

    unchanged = conversation_state_store.get(_USER, _NOW)
    assert unchanged is not None
    assert unchanged.state == ConversationStateName.INPUT_WAITING


def test_amount_input_zero_is_legal(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    service.handle_postback(_USER, "start_available_cash_reconcile", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    service.handle_text_input(_USER, state, _KNOWN_OWNER, _NOW)
    state_with_owner = conversation_state_store.get(_USER, _NOW)
    assert state_with_owner is not None

    reply = service.handle_text_input(_USER, state_with_owner, "0", _NOW)
    assert "新しい買付余力：0円" in reply.text
    assert reply.quick_reply is not None

    confirm_state = conversation_state_store.get(_USER, _NOW)
    assert confirm_state is not None
    assert confirm_state.state == ConversationStateName.CONFIRM_WAITING
    assert confirm_state.amount == Decimal("0")


# --- confirm: 実際の原子コミット --------------------------------------------


def _advance_to_confirm_waiting(service: ConversationService, owner: str, amount: str) -> str:
    service.handle_postback(_USER, "start_available_cash_reconcile", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    service.handle_text_input(_USER, state, owner, _NOW)
    state_with_owner = conversation_state_store.get(_USER, _NOW)
    assert state_with_owner is not None
    service.handle_text_input(_USER, state_with_owner, amount, _NOW)
    confirm_state = conversation_state_store.get(_USER, _NOW)
    assert confirm_state is not None
    return confirm_state.operation_id


def test_confirm_creates_available_cash_with_user_reconciliation(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    op = _advance_to_confirm_waiting(service, _NEW_OWNER, "500000")

    reply = service.handle_postback(_USER, "confirm", op, _NOW)
    assert "更新しました" in reply.text

    record = AvailableCashRepository().get(_NEW_OWNER)
    assert record is not None
    assert record.available_cash == Decimal("500000")
    assert record.last_update_type == AvailableCashUpdateType.USER_RECONCILIATION
    assert record.updated_at == _NOW
    assert record.last_reconciled_at == _NOW
    # ConversationStateはconfirm実行と同一トランザクションで消費される。
    assert conversation_state_store.get(_USER, _NOW) is None


def test_confirm_overwrites_absolute_value_not_additive(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    op1 = _advance_to_confirm_waiting(service, _KNOWN_OWNER, "100000")
    service.handle_postback(_USER, "confirm", op1, _NOW)

    op2 = _advance_to_confirm_waiting(service, _KNOWN_OWNER, "50000")
    service.handle_postback(_USER, "confirm", op2, _NOW)

    record = AvailableCashRepository().get(_KNOWN_OWNER)
    assert record is not None
    assert record.available_cash == Decimal("50000")


def test_duplicate_confirm_does_not_double_update(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    op = _advance_to_confirm_waiting(service, _NEW_OWNER, "500000")

    first = service.handle_postback(_USER, "confirm", op, _NOW)
    assert "更新しました" in first.text

    second = service.handle_postback(_USER, "confirm", op, _NOW)
    assert "有効な操作がありません" in second.text

    record = AvailableCashRepository().get(_NEW_OWNER)
    assert record is not None
    assert record.available_cash == Decimal("500000")


def test_write_conflict_shows_available_cash_specific_message(
    moto_conversation_tables: None,
    service: ConversationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """書き込み競合(#589/#594と別経路での同時更新等)時、保有株数を主語に
    した既存の汎用メッセージ(「最新の保有状況が変更されたため」)ではなく、
    買付余力向けの文言を表示する(サブちゃんレビューF3対応)。状態は
    変更されない(#584/#589と同じ安全側の挙動)。
    """
    op = _advance_to_confirm_waiting(service, _NEW_OWNER, "500000")
    monkeypatch.setattr(
        conversation_commit, "commit_available_cash_reconcile", lambda *a, **kw: False
    )

    reply = service.handle_postback(_USER, "confirm", op, _NOW)

    assert "最新の買付余力が変更されたため" in reply.text
    assert "最新の保有状況が変更されたため" not in reply.text
    assert AvailableCashRepository().get(_NEW_OWNER) is None


# --- retry / cancel ----------------------------------------------------------


def test_retry_clears_owner_and_amount(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    op = _advance_to_confirm_waiting(service, _NEW_OWNER, "500000")

    reply = service.handle_postback(_USER, "retry", op, _NOW)
    assert "所有者を選択" in reply.text or "所有者" in reply.text

    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    assert state.state == ConversationStateName.INPUT_WAITING
    assert state.owner is None
    assert state.amount is None


def test_cancel_discards_conversation(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    op = _advance_to_confirm_waiting(service, _NEW_OWNER, "500000")

    reply = service.handle_postback(_USER, "cancel", op, _NOW)
    assert "キャンセルしました" in reply.text
    assert conversation_state_store.get(_USER, _NOW) is None
    assert AvailableCashRepository().get(_NEW_OWNER) is None


# --- 既存BUY等への回帰防止(record_input()のstock_code Optional化) ----------


def test_watch_flow_unaffected_by_optional_stock_code_signature(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    """record_input()のstock_code引数Optional化がBUY/SELL/WATCHの挙動を
    変えないことを確認する(Issue #592、既存呼び出し元は非Noneを渡し続ける)。
    """
    service.handle_postback(_USER, "start_watch", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    reply = service.handle_text_input(_USER, state, "8306", _NOW)
    assert "よろしければ" in reply.text
    confirm_state = conversation_state_store.get(_USER, _NOW)
    assert confirm_state is not None
    assert confirm_state.stock_code == "8306"
    assert confirm_state.state == ConversationStateName.CONFIRM_WAITING
