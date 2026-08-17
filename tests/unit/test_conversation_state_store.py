"""ConversationStateStoreのテスト(LINEボタン起点会話型UI・実装プランv2 3節)。

moto(実DynamoDB互換バックエンド)を使い、ConditionExpressionの実際の
意味論(attribute_not_exists・比較演算子・REMOVE)を正確に検証する
(test_batch_tracker.pyのmoto_progress_dynamodbと同じ方針)。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.entities.enums import ConversationAction, ConversationStateName
from jstock_advisor.infrastructure.aws import conversation_state_store

_REGION = "ap-northeast-1"
_NOW = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)


@pytest.fixture
def moto_conversation_states(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "jstock")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName="jstock-conversation_states",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def test_get_returns_none_when_no_item(moto_conversation_states: None) -> None:
    assert conversation_state_store.get("U1", _NOW) is None


def test_start_or_replace_then_get(moto_conversation_states: None) -> None:
    conversation_state_store.start_or_replace("U1", ConversationAction.BUY, _NOW)
    state = conversation_state_store.get("U1", _NOW)
    assert state is not None
    assert state.action == ConversationAction.BUY
    assert state.state == ConversationStateName.INPUT_WAITING
    assert state.stock_code is None


def test_start_or_replace_overwrites_existing_pending(moto_conversation_states: None) -> None:
    """既存Pendingの有無に関わらず、新規アクション開始が常に成功する。"""
    conversation_state_store.start_or_replace("U1", ConversationAction.BUY, _NOW)
    conversation_state_store.record_input(
        "U1", ConversationAction.BUY, "8306", _NOW, shares=100, price=Decimal("1500")
    )
    conversation_state_store.start_or_replace("U1", ConversationAction.SELL, _NOW)
    state = conversation_state_store.get("U1", _NOW)
    assert state is not None
    assert state.action == ConversationAction.SELL
    assert state.state == ConversationStateName.INPUT_WAITING
    assert state.stock_code is None


def test_get_returns_none_when_ttl_expired_even_if_item_physically_present(
    moto_conversation_states: None,
) -> None:
    """DynamoDB Native TTLの物理削除タイミングに依存せず、アプリ層の
    `ttl <= now`判定だけで「会話状態なし」を判定できることの直接的な証明
    (motoはNative TTLを自動失効させないため、物理的に残っている状態を模擬できる)。
    """
    conversation_state_store.start_or_replace("U1", ConversationAction.BUY, _NOW)
    much_later = _NOW + dt.timedelta(seconds=conversation_state_store.TTL_SECONDS + 1)
    assert conversation_state_store.get("U1", much_later) is None


def test_record_input_succeeds_and_issues_new_operation_id(
    moto_conversation_states: None,
) -> None:
    started = conversation_state_store.start_or_replace("U1", ConversationAction.BUY, _NOW)
    new_state = conversation_state_store.record_input(
        "U1", ConversationAction.BUY, "8306", _NOW, shares=100, price=Decimal("1500")
    )
    assert new_state is not None
    assert new_state.state == ConversationStateName.CONFIRM_WAITING
    assert new_state.stock_code == "8306"
    assert new_state.shares == 100
    assert new_state.price == Decimal("1500")
    assert new_state.operation_id != started.operation_id


def test_record_input_fails_when_state_is_not_input_waiting(
    moto_conversation_states: None,
) -> None:
    conversation_state_store.start_or_replace("U1", ConversationAction.BUY, _NOW)
    conversation_state_store.record_input(
        "U1", ConversationAction.BUY, "8306", _NOW, shares=100, price=Decimal("1500")
    )
    # 既にCONFIRM_WAITINGのため、2回目のrecord_inputは条件不成立でNone。
    result = conversation_state_store.record_input(
        "U1", ConversationAction.BUY, "9999", _NOW, shares=1, price=Decimal("1")
    )
    assert result is None
    # 状態が変更されていないこと(1回目のCONFIRM_WAITINGのまま)。
    state = conversation_state_store.get("U1", _NOW)
    assert state is not None
    assert state.stock_code == "8306"


def test_record_input_fails_when_action_mismatch(moto_conversation_states: None) -> None:
    conversation_state_store.start_or_replace("U1", ConversationAction.BUY, _NOW)
    result = conversation_state_store.record_input(
        "U1", ConversationAction.SELL, "8306", _NOW, shares=100, price=Decimal("1500")
    )
    assert result is None


def test_record_input_fails_when_ttl_expired_though_physically_present(
    moto_conversation_states: None,
) -> None:
    conversation_state_store.start_or_replace("U1", ConversationAction.BUY, _NOW)
    much_later = _NOW + dt.timedelta(seconds=conversation_state_store.TTL_SECONDS + 1)
    result = conversation_state_store.record_input(
        "U1", ConversationAction.BUY, "8306", much_later, shares=100, price=Decimal("1500")
    )
    assert result is None


def test_record_input_for_watch_leaves_shares_and_price_unset(
    moto_conversation_states: None,
) -> None:
    conversation_state_store.start_or_replace("U1", ConversationAction.WATCH, _NOW)
    new_state = conversation_state_store.record_input(
        "U1", ConversationAction.WATCH, "8306", _NOW
    )
    assert new_state is not None
    assert new_state.stock_code == "8306"
    assert new_state.shares is None
    assert new_state.price is None


def test_retry_returns_to_input_waiting_and_clears_fields(
    moto_conversation_states: None,
) -> None:
    conversation_state_store.start_or_replace("U1", ConversationAction.BUY, _NOW)
    confirm_state = conversation_state_store.record_input(
        "U1", ConversationAction.BUY, "8306", _NOW, shares=100, price=Decimal("1500")
    )
    assert confirm_state is not None
    retried = conversation_state_store.retry(
        "U1", ConversationAction.BUY, confirm_state.operation_id, _NOW
    )
    assert retried is not None
    assert retried.state == ConversationStateName.INPUT_WAITING
    assert retried.stock_code is None
    assert retried.shares is None
    assert retried.price is None
    assert retried.operation_id != confirm_state.operation_id


def test_retry_fails_on_operation_id_mismatch(moto_conversation_states: None) -> None:
    conversation_state_store.start_or_replace("U1", ConversationAction.BUY, _NOW)
    conversation_state_store.record_input(
        "U1", ConversationAction.BUY, "8306", _NOW, shares=100, price=Decimal("1500")
    )
    result = conversation_state_store.retry("U1", ConversationAction.BUY, "wrong-op-id", _NOW)
    assert result is None
    # 状態が変更されていないこと。
    state = conversation_state_store.get("U1", _NOW)
    assert state is not None
    assert state.state == ConversationStateName.CONFIRM_WAITING


def test_cancel_deletes_item_on_match(moto_conversation_states: None) -> None:
    conversation_state_store.start_or_replace("U1", ConversationAction.BUY, _NOW)
    confirm_state = conversation_state_store.record_input(
        "U1", ConversationAction.BUY, "8306", _NOW, shares=100, price=Decimal("1500")
    )
    assert confirm_state is not None
    ok = conversation_state_store.cancel("U1", confirm_state.operation_id, _NOW)
    assert ok is True
    assert conversation_state_store.get("U1", _NOW) is None


def test_cancel_fails_and_keeps_item_on_operation_id_mismatch(
    moto_conversation_states: None,
) -> None:
    conversation_state_store.start_or_replace("U1", ConversationAction.BUY, _NOW)
    conversation_state_store.record_input(
        "U1", ConversationAction.BUY, "8306", _NOW, shares=100, price=Decimal("1500")
    )
    ok = conversation_state_store.cancel("U1", "wrong-op-id", _NOW)
    assert ok is False
    assert conversation_state_store.get("U1", _NOW) is not None


def test_build_confirm_delete_transact_item_executes_and_deletes_on_match(
    moto_conversation_states: None,
) -> None:
    """conversation_commit.pyが組み立てる低レベルDeleteアイテムが、実際の
    TransactWriteItems呼び出しの中で単独でも正しく機能することを確認する
    (追加条件2: ConditionCheck+Deleteの分離ではなく単一Deleteで条件確認+削除)。
    """
    conversation_state_store.start_or_replace("U1", ConversationAction.BUY, _NOW)
    confirm_state = conversation_state_store.record_input(
        "U1", ConversationAction.BUY, "8306", _NOW, shares=100, price=Decimal("1500")
    )
    assert confirm_state is not None

    item = conversation_state_store.build_confirm_delete_transact_item(
        "U1", ConversationAction.BUY, confirm_state.operation_id, _NOW
    )
    client = boto3.client("dynamodb", region_name=_REGION)
    client.transact_write_items(TransactItems=[item])

    assert conversation_state_store.get("U1", _NOW) is None


def test_build_confirm_delete_transact_item_fails_on_operation_id_mismatch(
    moto_conversation_states: None,
) -> None:
    from botocore.exceptions import ClientError

    conversation_state_store.start_or_replace("U1", ConversationAction.BUY, _NOW)
    conversation_state_store.record_input(
        "U1", ConversationAction.BUY, "8306", _NOW, shares=100, price=Decimal("1500")
    )

    item = conversation_state_store.build_confirm_delete_transact_item(
        "U1", ConversationAction.BUY, "wrong-op-id", _NOW
    )
    client = boto3.client("dynamodb", region_name=_REGION)
    with pytest.raises(ClientError):
        client.transact_write_items(TransactItems=[item])

    # 削除されていないこと。
    assert conversation_state_store.get("U1", _NOW) is not None
