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

from jstock_advisor.domain.entities.enums import ConversationStateName, Priority
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.infrastructure.aws import conversation_state_store
from jstock_advisor.infrastructure.line.webhook import LineTextMessageEvent
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
)
from jstock_advisor.services.chat_command_service import ChatCommandResult
from jstock_advisor.services.conversation_service import ConversationService
from jstock_advisor.services.line_event_router import LineEventRouter

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


class _FakeResolverWithExistence:
    """銘柄実在チェック(コードレビュー2026-08-17指摘3)専用のフェイク。
    resolve()はstock_codeをそのまま返す(表示名は本テストの関心事ではない)。"""

    def __init__(self, known: set[str] | None = None, indeterminate: bool = False) -> None:
        self._known = known or set()
        self._indeterminate = indeterminate

    def resolve(
        self,
        stock_code: str,
        fallback_name: str | None = None,
        fallback_name_provider: object | None = None,
    ) -> str:
        return stock_code

    def exists(self, stock_code: str) -> bool | None:
        if self._indeterminate:
            return None
        return stock_code in self._known


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


def test_buy_confirmation_and_success_messages_use_comma_formatting(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    """コードレビュー2026-08-17再指摘4: 買付確認画面・登録完了メッセージの
    株数・単価・合計金額はすべてカンマ区切りで表示する(売却側と統一)。"""
    service.handle_postback(_USER, "start_buy", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None

    input_reply = service.handle_text_input(_USER, state, "8306,10000,1500", _NOW)

    assert "10,000株" in input_reply.text
    assert "@1,500円" in input_reply.text
    assert "合計: 15,000,000円" in input_reply.text

    confirm_state = conversation_state_store.get(_USER, _NOW)
    assert confirm_state is not None
    confirm_reply = service.handle_postback(_USER, "confirm", confirm_state.operation_id, _NOW)

    assert "10,000株" in confirm_reply.text
    assert "@1,500円" in confirm_reply.text


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


def test_buy_input_unknown_stock_code_does_not_proceed_to_confirmation(
    moto_conversation_tables: None,
) -> None:
    """コードレビュー2026-08-17再指摘3: 実在しない銘柄コードは確認画面へ
    進めず、入力エラーとして案内する。"""
    service = ConversationService(
        stock_display_name_resolver=_FakeResolverWithExistence(known={"8306"})
    )
    service.handle_postback(_USER, "start_buy", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None

    reply = service.handle_text_input(_USER, state, "9999,100,1500", _NOW)

    assert "9999に該当する銘柄が見つかりませんでした" in reply.text
    assert reply.quick_reply is None
    unchanged = conversation_state_store.get(_USER, _NOW)
    assert unchanged is not None
    assert unchanged.state == ConversationStateName.INPUT_WAITING


def test_buy_input_existence_indeterminate_still_proceeds_to_confirmation(
    moto_conversation_tables: None,
) -> None:
    """JPXデータソースが利用できず実在チェックが判定不能(None)の場合は、
    安全側としてブロックせず処理を継続する(一時的なデータ取得失敗を理由に
    正当な入力をブロックしないため)。"""
    service = ConversationService(
        stock_display_name_resolver=_FakeResolverWithExistence(indeterminate=True)
    )
    service.handle_postback(_USER, "start_buy", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None

    reply = service.handle_text_input(_USER, state, "8306,100,1500", _NOW)

    assert "登録します" in reply.text
    assert reply.quick_reply is not None


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


def test_sell_confirmation_shows_current_and_remaining_shares(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    """コードレビュー2026-08-17 指摘2(当初の受入条件): SELL確認画面には
    現在保有・今回売却・売却後の株数、売却単価・売却金額を明示する。"""
    _seed_holding(shares=300)
    service.handle_postback(_USER, "start_sell", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None

    reply = service.handle_text_input(_USER, state, "8306,100,3800", _NOW)

    assert "現在保有：300株" in reply.text
    assert "今回売却：100株" in reply.text
    assert "売却後：200株" in reply.text
    assert "売却単価：3,800円" in reply.text
    assert "売却金額：380,000円" in reply.text


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


def test_sell_input_unknown_stock_code_does_not_proceed_to_confirmation(
    moto_conversation_tables: None,
) -> None:
    """コードレビュー2026-08-17再指摘3: SELLでも実在しない銘柄コードは
    確認画面へ進めず、入力エラーとして案内する(保有チェックより先に判定)。"""
    service = ConversationService(
        stock_display_name_resolver=_FakeResolverWithExistence(known={"8306"})
    )
    service.handle_postback(_USER, "start_sell", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None

    reply = service.handle_text_input(_USER, state, "9999,100,1500", _NOW)

    assert "9999に該当する銘柄が見つかりませんでした" in reply.text
    assert reply.quick_reply is None


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


def test_watch_input_unknown_stock_code_does_not_proceed_to_confirmation(
    moto_conversation_tables: None,
) -> None:
    """コードレビュー2026-08-17再指摘3: WATCHでも実在しない銘柄コードは
    確認画面へ進めず、入力エラーとして案内する。"""
    service = ConversationService(
        stock_display_name_resolver=_FakeResolverWithExistence(known={"8306"})
    )
    service.handle_postback(_USER, "start_watch", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None

    reply = service.handle_text_input(_USER, state, "9999", _NOW)

    assert "9999に該当する銘柄が見つかりませんでした" in reply.text
    assert reply.quick_reply is None
    assert WatchlistRepository().get("9999") is None


def test_watch_input_already_registered_does_not_overwrite_existing_item(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    """コードレビュー2026-08-17 指摘1: 既にWatchlistへ存在する銘柄を
    Conversation UIから登録しようとした場合、既存設定(reason/priority/
    notify_enabled/memo等)を一切変更せず、確認画面へ進まずに終了する。"""
    existing = WatchlistItem(
        stock_code=_STOCK,
        stock_name="三菱UFJ",
        reason="配当利回り重視",
        priority=Priority.HIGH,
        notify_enabled=False,
        memo="custom memo",
        created_at=_NOW,
        updated_at=_NOW,
    )
    WatchlistRepository().upsert(existing)
    service.handle_postback(_USER, "start_watch", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None

    reply = service.handle_text_input(_USER, state, "8306", _NOW)

    assert f"{_STOCK}はすでにお気に入りに登録されています" in reply.text
    assert reply.quick_reply is None
    unchanged = WatchlistRepository().get(_STOCK)
    assert unchanged is not None
    assert unchanged.reason == "配当利回り重視"
    assert unchanged.priority == Priority.HIGH
    assert unchanged.notify_enabled is False
    assert unchanged.memo == "custom memo"


class _FakeChatCommandServiceForLegacyRouting:
    """LineEventRouterの振り分け確認専用の最小フェイク(is_legacy_command/
    handle以外は使わない)。"""

    def __init__(self) -> None:
        self.handled: list[str] = []

    def is_legacy_command(self, text: str) -> bool:
        return text.startswith("買付,")

    def handle(self, text: str, now: dt.datetime | None = None) -> ChatCommandResult:
        self.handled.append(text)
        return ChatCommandResult(f"chat-reply:{text}", True)


def test_watch_duplicate_ends_conversation_state_and_next_legacy_command_routes_normally(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    """再レビュー指摘(2026-08-17): 重複検出時にConversationStateを終了しない
    と、INPUT_WAITINGが残ったままになり、次の通常テキスト(Legacy CSV
    コマンド等)が誤ってWATCH入力として処理されてしまう。discard_input()に
    よる条件付きDeleteで対話を終了し、その後はLineEventRouterがLegacy CSV
    コマンドを通常どおりChatCommandServiceへルーティングすることを検証する。
    """
    WatchlistRepository().upsert(
        WatchlistItem(stock_code=_STOCK, created_at=_NOW, updated_at=_NOW)
    )
    service.handle_postback(_USER, "start_watch", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    assert state.state == ConversationStateName.INPUT_WAITING

    reply = service.handle_text_input(_USER, state, "8306", _NOW)

    assert f"{_STOCK}はすでにお気に入りに登録されています" in reply.text
    assert conversation_state_store.get(_USER, _NOW) is None

    chat_command = _FakeChatCommandServiceForLegacyRouting()
    router = LineEventRouter(conversation_service=service, chat_command_service=chat_command)  # type: ignore[arg-type]
    event = LineTextMessageEvent(reply_token="rt", user_id=_USER, text="買付,7203,100,2000")

    router_reply = router.route_text(event, _NOW)

    assert router_reply.text == "chat-reply:買付,7203,100,2000"
    assert chat_command.handled == ["買付,7203,100,2000"]


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
