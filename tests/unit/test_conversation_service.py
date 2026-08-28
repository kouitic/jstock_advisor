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
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.infrastructure.aws import conversation_state_store, trading_pause_config
from jstock_advisor.infrastructure.line.webhook import LineTextMessageEvent
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
)
from jstock_advisor.services.conversation_service import ConversationService
from jstock_advisor.services.line_event_router import LineEventRouter

_REGION = "ap-northeast-1"
_NOW = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)
_USER = "U1"
_STOCK = "8306"
_HOLDING_ID = build_holding_id(DEFAULT_OWNER, _STOCK)


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
            ("jstock-holdings_v2", "holding_id"),
            ("jstock-watchlist", "stock_code"),
            # 保有銘柄オーナー機能移行M0: TradingPauseServiceがBUY/SELL開始・
            # 確定のたびに参照するため、moto環境にもテーブルが必要
            # (未初期化=pause_buy_sell False相当として扱われる、テーブル自体が
            # 無い場合はrepo.get()がClientErrorとなり安全側の一時停止扱いに
            # フォールバックしてしまうため)。
            ("jstock-trading_pause_config", "config_id"),
            # 銘柄分析(Phase 2-B、2026-08)向け。
            ("jstock-buy_candidate_batch_completion", "pointer_id"),
            ("jstock-buy_candidate_evaluation_records", "evaluation_id"),
            ("jstock-recommendations", "recommendation_id"),
        ):
            client.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        client.create_table(
            TableName="jstock-holding_evaluation_records",
            KeySchema=[{"AttributeName": "holding_evaluation_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "holding_evaluation_id", "AttributeType": "S"},
                {"AttributeName": "holding_id", "AttributeType": "S"},
                {"AttributeName": "evaluated_at", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "holding_id-index",
                    "KeySchema": [
                        {"AttributeName": "holding_id", "KeyType": "HASH"},
                        {"AttributeName": "evaluated_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
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

    input_reply = service.handle_text_input(_USER, state, "本人,8306,100,1500", _NOW)
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
    holding = HoldingRepository().get(_HOLDING_ID)
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

    input_reply = service.handle_text_input(_USER, state, "本人,8306,10000,1500", _NOW)

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
    reply = service.handle_text_input(_USER, state, "本人,ABC,100,1500", _NOW)
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

    reply = service.handle_text_input(_USER, state, "本人,9999,100,1500", _NOW)

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

    reply = service.handle_text_input(_USER, state, "本人,8306,100,1500", _NOW)

    assert "登録します" in reply.text
    assert reply.quick_reply is not None


def test_buy_input_wrong_field_count_reprompts(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    service.handle_postback(_USER, "start_buy", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    reply = service.handle_text_input(_USER, state, "本人,8306,100", _NOW)
    assert "銘柄コード" in reply.text
    assert conversation_state_store.get(_USER, _NOW).state == ConversationStateName.INPUT_WAITING  # type: ignore[union-attr]


# --- SELL: 保有チェック ------------------------------------------------


def _seed_holding(shares: int = 100) -> None:
    from jstock_advisor.domain.entities.enums import AccountType
    from jstock_advisor.domain.entities.holding import Holding, PurchaseLot

    PurchaseLotRepository().upsert(
        PurchaseLot(
            lot_id="lot-1",
            owner=DEFAULT_OWNER,
            holding_id=_HOLDING_ID,
            stock_code=_STOCK,
            purchase_date=dt.date(2026, 8, 1),
            shares=shares,
            purchase_price=Decimal("1000"),
            account_type=AccountType.GENERAL,
        )
    )
    HoldingRepository().upsert(
        Holding(
            owner=DEFAULT_OWNER,
            holding_id=_HOLDING_ID,
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
    input_reply = service.handle_text_input(_USER, state, "本人,8306,100,1800", _NOW)
    assert "売却" in input_reply.text
    confirm_state = conversation_state_store.get(_USER, _NOW)
    assert confirm_state is not None

    confirm_reply = service.handle_postback(_USER, "confirm", confirm_state.operation_id, _NOW)

    assert "登録しました" in confirm_reply.text
    assert HoldingRepository().get(_HOLDING_ID) is None
    assert conversation_state_store.get(_USER, _NOW) is None


def test_case_m_line_conversation_partial_sell_updates_last_sale_date(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    """LINE会話フロー(ボタン型UI)で一部売却が成立した場合、
    holding.last_sale_dateが売却日に更新されることを確認する
    (再コードレビュー対応2026-08、指摘3 Case M: Profit Protectionの
    basis_date再算出(max(last_purchase_date, last_sale_date))が正しく
    機能するための前提を、実際のLINE会話フロー経由で固定する回帰テスト)。
    last_purchase_dateは不変であること(Case O)もあわせて確認する。
    """
    _seed_holding(shares=300)
    holding_before = HoldingRepository().get(_HOLDING_ID)
    assert holding_before is not None
    assert holding_before.last_sale_date is None
    assert holding_before.last_purchase_date == dt.date(2026, 8, 1)

    service.handle_postback(_USER, "start_sell", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    service.handle_text_input(_USER, state, "本人,8306,100,3800", _NOW)
    confirm_state = conversation_state_store.get(_USER, _NOW)
    assert confirm_state is not None

    confirm_reply = service.handle_postback(_USER, "confirm", confirm_state.operation_id, _NOW)
    assert "登録しました" in confirm_reply.text

    holding_after = HoldingRepository().get(_HOLDING_ID)
    assert holding_after is not None
    assert holding_after.shares == 200
    # last_sale_dateが売却日(evaluation_date_jst(_NOW) = 2026-08-17)へ更新される。
    assert holding_after.last_sale_date == dt.date(2026, 8, 17)
    # last_purchase_dateは一部売却では不変(Case O、FIFO/average_purchase_price
    # の既存意味を壊さないことの確認)。
    assert holding_after.last_purchase_date == dt.date(2026, 8, 1)


def test_sell_confirmation_shows_current_and_remaining_shares(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    """コードレビュー2026-08-17 指摘2(当初の受入条件): SELL確認画面には
    現在保有・今回売却・売却後の株数、売却単価・売却金額を明示する。"""
    _seed_holding(shares=300)
    service.handle_postback(_USER, "start_sell", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None

    reply = service.handle_text_input(_USER, state, "本人,8306,100,3800", _NOW)

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
    reply = service.handle_text_input(_USER, state, "本人,8306,100,1800", _NOW)
    assert "保有株数" in reply.text
    assert conversation_state_store.get(_USER, _NOW).state == ConversationStateName.INPUT_WAITING  # type: ignore[union-attr]


def test_sell_input_rejects_when_not_holding(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    service.handle_postback(_USER, "start_sell", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    reply = service.handle_text_input(_USER, state, "本人,8306,100,1800", _NOW)
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

    reply = service.handle_text_input(_USER, state, "本人,9999,100,1500", _NOW)

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


def test_watch_duplicate_ends_conversation_state_and_next_text_is_not_watch_input(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    """再レビュー指摘(2026-08-17): 重複検出時にConversationStateを終了しない
    と、INPUT_WAITINGが残ったままになり、次の通常テキストが誤ってWATCH入力
    として処理されてしまう。discard_input()による条件付きDeleteで対話を終了し、
    その後の状態無しテキストはLineEventRouterがヘルプ応答のみ返すこと
    (Issue #24でLegacy CSVコマンド経路は廃止済みのため、旧「買付,…」形式の
    テキストもWATCH入力として解釈されず、どこへも到達しない)を検証する。
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

    router = LineEventRouter(conversation_service=service)
    event = LineTextMessageEvent(reply_token="rt", user_id=_USER, text="買付,7203,100,2000")

    router_reply = router.route_text(event, _NOW)

    assert "コマンドが認識できませんでした" in router_reply.text
    assert conversation_state_store.get(_USER, _NOW) is None


# --- retry / cancel --------------------------------------------------------


def test_retry_returns_to_input_waiting(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    service.handle_postback(_USER, "start_buy", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    service.handle_text_input(_USER, state, "本人,8306,100,1500", _NOW)
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
    service.handle_text_input(_USER, state, "本人,8306,100,1500", _NOW)
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
    service.handle_text_input(_USER, state, "本人,8306,100,1500", _NOW)

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
    service.handle_text_input(_USER, state, "本人,8306,100,1500", _NOW)
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


# --- 保有銘柄オーナー機能移行M0: TradingPauseConfig(BUY/SELL一時停止) --------------


def _set_trading_paused(paused: bool) -> None:
    existing = trading_pause_config.get()
    if existing is None:
        trading_pause_config.init(
            pause_buy_sell=paused, updated_by="tester", change_reason="test setup", now=_NOW
        )
        return
    trading_pause_config.update(
        expected_config_version=existing.config_version,
        pause_buy_sell=paused,
        updated_by="tester",
        change_reason="test setup",
        now=_NOW,
    )


def test_start_buy_blocked_when_trading_paused(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    _set_trading_paused(True)
    reply = service.handle_postback(_USER, "start_buy", None, _NOW)
    assert "メンテナンス中" in reply.text
    assert conversation_state_store.get(_USER, _NOW) is None


def test_start_sell_blocked_when_trading_paused(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    _seed_holding(shares=100)
    _set_trading_paused(True)
    reply = service.handle_postback(_USER, "start_sell", None, _NOW)
    assert "メンテナンス中" in reply.text
    assert conversation_state_store.get(_USER, _NOW) is None


def test_start_watch_not_blocked_when_trading_paused(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    """WATCH(ウォッチリスト登録)はHoldings/PurchaseLotsを一切更新しないため、
    BUY/SELLの一時停止フラグの対象外(commit_watch()参照)。"""
    _set_trading_paused(True)
    reply = service.handle_postback(_USER, "start_watch", None, _NOW)
    assert "ウォッチリスト登録を開始します" in reply.text
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None


def test_commit_buy_blocked_if_paused_after_conversation_started(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    """開始時点ではpause=falseだったが、確認直前にtrueへ切り替わった場合でも
    実際の書き込みは行わない(_commit_buy側の防御チェック、_startのチェック
    だけに依存しないことの確認)。"""
    service.handle_postback(_USER, "start_buy", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    service.handle_text_input(_USER, state, "本人,8306,100,1500", _NOW)
    confirm_state = conversation_state_store.get(_USER, _NOW)
    assert confirm_state is not None

    _set_trading_paused(True)

    reply = service.handle_postback(_USER, "confirm", confirm_state.operation_id, _NOW)

    assert "メンテナンス中" in reply.text
    assert HoldingRepository().get(_HOLDING_ID) is None
    # ConversationStateはまだ消費されていない(実際に書き込みが起きていないことの確認)
    assert conversation_state_store.get(_USER, _NOW) is not None


def test_buy_text_input_blocked_when_paused_after_start_stays_input_waiting(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    """pause前に開始済みのBUY(INPUT_WAITING)は、pause後のCSV入力では
    CONFIRM_WAITINGへ進めない(ConversationStateは一切変更しない)。"""
    service.handle_postback(_USER, "start_buy", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    assert state.state == ConversationStateName.INPUT_WAITING

    _set_trading_paused(True)

    reply = service.handle_text_input(_USER, state, "本人,8306,100,1500", _NOW)

    assert "メンテナンス中" in reply.text
    unchanged = conversation_state_store.get(_USER, _NOW)
    assert unchanged is not None
    assert unchanged.state == ConversationStateName.INPUT_WAITING
    assert unchanged.stock_code is None


def test_sell_text_input_blocked_when_paused_after_start_stays_input_waiting(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    _seed_holding(shares=100)
    service.handle_postback(_USER, "start_sell", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    assert state.state == ConversationStateName.INPUT_WAITING

    _set_trading_paused(True)

    reply = service.handle_text_input(_USER, state, "本人,8306,50,1800", _NOW)

    assert "メンテナンス中" in reply.text
    unchanged = conversation_state_store.get(_USER, _NOW)
    assert unchanged is not None
    assert unchanged.state == ConversationStateName.INPUT_WAITING
    assert unchanged.stock_code is None


def test_watch_text_input_not_blocked_when_paused(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    """WATCHはpause=trueでも入力・登録可能(HoldingsもPurchaseLotsも
    更新しない経路のため対象外)。"""
    service.handle_postback(_USER, "start_watch", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None

    _set_trading_paused(True)

    input_reply = service.handle_text_input(_USER, state, "8306", _NOW)
    assert "ウォッチリストに追加します" in input_reply.text
    confirm_state = conversation_state_store.get(_USER, _NOW)
    assert confirm_state is not None

    confirm_reply = service.handle_postback(_USER, "confirm", confirm_state.operation_id, _NOW)
    assert "ウォッチリストに追加しました" in confirm_reply.text
    assert WatchlistRepository().get(_STOCK) is not None


def test_commit_sell_blocked_if_paused_after_conversation_started(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    _seed_holding(shares=100)
    service.handle_postback(_USER, "start_sell", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    service.handle_text_input(_USER, state, "本人,8306,50,1800", _NOW)
    confirm_state = conversation_state_store.get(_USER, _NOW)
    assert confirm_state is not None

    _set_trading_paused(True)

    reply = service.handle_postback(_USER, "confirm", confirm_state.operation_id, _NOW)

    assert "メンテナンス中" in reply.text
    holding = HoldingRepository().get(_HOLDING_ID)
    assert holding is not None
    assert holding.shares == 100  # 変更されていない


# --- 銘柄分析(Phase 2-B、2026-08、読み取り専用) -----------------------------


def test_analyze_flow_start_prompts_for_code(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    reply = service.handle_postback(_USER, "start_analyze", None, _NOW)
    assert "銘柄コード" in reply.text
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None
    assert state.state == ConversationStateName.INPUT_WAITING


def test_analyze_flow_unknown_code_is_rejected_and_state_ends(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    service.handle_postback(_USER, "start_analyze", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None

    reply = service.handle_text_input(_USER, state, "9999", _NOW)

    assert "見つかりませんでした" in reply.text
    # 該当データが無くてもstateは終了する(読み取り専用フローは常に1往復で
    # 終わる、修正9)。
    assert conversation_state_store.get(_USER, _NOW) is None


def test_analyze_flow_invalid_code_keeps_state_for_retry(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    service.handle_postback(_USER, "start_analyze", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None

    reply = service.handle_text_input(_USER, state, "12", _NOW)

    assert "銘柄コードが不正です" in reply.text
    # 形式不正の時点ではstateを終了しない(_handle_watch_inputと同じ方針、
    # ユーザーが再送信できるように)。
    assert conversation_state_store.get(_USER, _NOW) is not None


def test_analyze_flow_single_owner_holding_offers_sell_button_only(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    _seed_holding(shares=100)
    service.handle_postback(_USER, "start_analyze", None, _NOW)
    state = conversation_state_store.get(_USER, _NOW)
    assert state is not None

    reply = service.handle_text_input(_USER, state, _STOCK, _NOW)

    assert conversation_state_store.get(_USER, _NOW) is None
    assert reply.quick_reply is not None
    labels = [button.label for button in reply.quick_reply]
    assert labels == ["売却・保有判定を見る"]
    assert reply.quick_reply[0].postback_data == f"action=show_analysis_sell&code={_STOCK}"


def test_analyze_flow_sell_analysis_no_evaluation_record_is_reported_honestly(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    """保有銘柄はあるが、まだ一度もholdings_watchlist_handlerの評価を経て
    いない(HoldingEvaluationRecordが無い)場合、「保有継続」等を捏造せず、
    データが見つからない旨を正直に伝える。"""
    _seed_holding(shares=300)

    reply = service.handle_postback(_USER, "show_analysis_sell", None, _NOW, code=_STOCK)

    assert "見つかりませんでした" in reply.text


def test_analyze_flow_sell_analysis_pure_hold_shows_unrestorable_reason(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    """HoldingEvaluationRecordはあるが、通知権限を持つエンジンがHOLDを返した
    (Recommendationが作られなかった)場合は「保有継続」+理由復元不可を表示する
    (E節: SHADOW中のHoldingDecisionResultは初期実装では参照しない)。"""
    from jstock_advisor.domain.entities.holding_evaluation_record import (
        HoldingEvaluationRecord,
        build_holding_evaluation_id,
    )
    from jstock_advisor.infrastructure.local_repository.holding_evaluation_record_repository import (  # noqa: E501
        HoldingEvaluationRecordRepository,
    )

    _seed_holding(shares=300)
    HoldingEvaluationRecordRepository().save(
        HoldingEvaluationRecord(
            holding_evaluation_id=build_holding_evaluation_id(_HOLDING_ID, _NOW),
            holding_id=_HOLDING_ID,
            owner=DEFAULT_OWNER,
            stock_code=_STOCK,
            evaluated_at=_NOW,
            rule_version="v1",
            authoritative_engine="LEGACY_SELL",
            authoritative_outcome_category="hold",
        )
    )

    reply = service.handle_postback(_USER, "show_analysis_sell", None, _NOW, code=_STOCK)

    assert "【銘柄分析】" in reply.text
    assert "保有継続" in reply.text
    assert "現行データでは" in reply.text


def test_analyze_flow_sell_analysis_multiple_owners_offers_choice(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    from jstock_advisor.domain.entities.enums import AccountType
    from jstock_advisor.domain.entities.holding import Holding

    _seed_holding(shares=100)
    second_owner = "owner-b"
    HoldingRepository().upsert(
        Holding(
            owner=second_owner,
            holding_id=build_holding_id(second_owner, _STOCK),
            stock_code=_STOCK,
            stock_name="x",
            shares=50,
            average_purchase_price=Decimal("1000"),
            total_purchase_amount=Decimal("50000"),
            first_purchase_date=dt.date(2026, 8, 1),
            last_purchase_date=dt.date(2026, 8, 1),
            account_type=AccountType.GENERAL,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )

    reply = service.handle_postback(_USER, "show_analysis_sell", None, _NOW, code=_STOCK)

    assert reply.quick_reply is not None
    labels = sorted(button.label for button in reply.quick_reply)
    assert labels == sorted([DEFAULT_OWNER, second_owner])
    for button in reply.quick_reply:
        assert button.postback_data.startswith(f"action=show_analysis_sell&code={_STOCK}&owner=")

    owner_reply = service.handle_postback(
        _USER, "show_analysis_sell", None, _NOW, code=_STOCK, owner=second_owner
    )
    assert second_owner in owner_reply.text


def test_analyze_flow_no_data_at_all_is_rejected(
    moto_conversation_tables: None, service: ConversationService
) -> None:
    reply = service.handle_postback(_USER, "show_analysis_buy", None, _NOW, code=None)
    assert "見つかりませんでした" in reply.text
