"""LineEventRouterの判定ルールのテスト(LINEボタン起点会話型UI・実装プランv2 5節、
Issue #24でlegacy CSVコマンド経路を廃止)。

優先順位: ①postbackは常にConversationService、②有効なConversationStateが
あればテキスト形式に関わらずConversationService、③それ以外(状態無しのtext
全般。旧Legacy CSVフルコマンドを含む)はヘルプ(永続化なし)。

ConversationServiceが実際に何を書き込むかはtest_conversation_service.py・
test_conversation_commit.pyで検証済みのため、ここでは「どのサービスに
振り分けられるか(または振り分けられないか)」の判定ロジックのみを
フェイクで検証する。
"""

from __future__ import annotations

import datetime as dt

import pytest

from jstock_advisor.domain.entities.enums import ConversationAction, ConversationStateName
from jstock_advisor.infrastructure.aws.conversation_state_store import ConversationState
from jstock_advisor.infrastructure.line.webhook import LinePostbackEvent, LineTextMessageEvent
from jstock_advisor.services import line_event_router
from jstock_advisor.services.conversation_service import ConversationReply
from jstock_advisor.services.line_event_router import LineEventRouter

_NOW = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)
_USER = "U1"


class _FakeConversationService:
    def __init__(self) -> None:
        self.postback_calls: list[tuple[str, str, str | None]] = []
        self.postback_calls_with_owner_category: list[
            tuple[str, str, str | None, str | None, str | None]
        ] = []
        self.text_calls: list[tuple[str, ConversationState, str]] = []

    def handle_postback(
        self,
        user_id: str,
        action: str,
        op: str | None,
        now: dt.datetime,
        owner: str | None = None,
        category: str | None = None,
        code: str | None = None,
    ) -> ConversationReply:
        self.postback_calls.append((user_id, action, op))
        self.postback_calls_with_owner_category.append((user_id, action, op, owner, category))
        return ConversationReply("conversation-postback-reply")

    def handle_text_input(
        self, user_id: str, state: ConversationState, text: str, now: dt.datetime
    ) -> ConversationReply:
        self.text_calls.append((user_id, state, text))
        return ConversationReply("conversation-text-reply")


def _confirm_state() -> ConversationState:
    return ConversationState(
        user_id=_USER,
        action=ConversationAction.BUY,
        state=ConversationStateName.INPUT_WAITING,
        operation_id="op-1",
        stock_code=None,
        shares=None,
        price=None,
        owner=None,
        created_at=_NOW,
        updated_at=_NOW,
        ttl=int(_NOW.timestamp()) + 1200,
    )


def _patch_state(monkeypatch: pytest.MonkeyPatch, state: ConversationState | None) -> None:
    monkeypatch.setattr(line_event_router.conversation_state_store, "get", lambda *a, **kw: state)


def _build_router() -> tuple[LineEventRouter, _FakeConversationService]:
    conversation = _FakeConversationService()
    router = LineEventRouter(conversation_service=conversation)  # type: ignore[arg-type]
    return router, conversation


# --- ① postbackは常にConversationService ------------------------------------


def test_postback_always_goes_to_conversation_service_regardless_of_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_state(monkeypatch, None)
    router, conversation = _build_router()
    event = LinePostbackEvent(reply_token="rt", user_id=_USER, action="start_buy", op=None)

    reply = router.route_postback(event, _NOW)

    assert reply.text == "conversation-postback-reply"
    assert conversation.postback_calls == [(_USER, "start_buy", None)]


def test_postback_owner_and_category_propagate_to_conversation_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LINE UI第二弾(保有銘柄/対象確認、2026-08)向け、owner/categoryが
    ConversationService.handle_postback()まで正しく伝播すること。"""
    _patch_state(monkeypatch, None)
    router, conversation = _build_router()
    event = LinePostbackEvent(
        reply_token="rt", user_id=_USER, action="show_holdings", op=None, owner="所有者A"
    )

    router.route_postback(event, _NOW)

    assert conversation.postback_calls_with_owner_category == [
        (_USER, "show_holdings", None, "所有者A", None)
    ]


# --- ② 有効なConversationStateがあれば常にConversationService --------------


def test_text_during_input_waiting_goes_to_conversation_service_even_if_csv_shaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BUY_INPUT_WAITING中の"8306,100,2500"はCSV形式に見えるが、常に現在の
    入力待ち状態の期待フォーマットとして解釈されること(実装プランv2の指摘4)。"""
    state = _confirm_state()
    _patch_state(monkeypatch, state)
    router, conversation = _build_router()
    event = LineTextMessageEvent(reply_token="rt", user_id=_USER, text="8306,100,2500")

    reply = router.route_text(event, _NOW)

    assert reply.text == "conversation-text-reply"
    assert conversation.text_calls == [(_USER, state, "8306,100,2500")]


def test_text_during_confirm_waiting_with_unrelated_text_goes_to_conversation_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _confirm_state()
    _patch_state(monkeypatch, state)
    router, conversation = _build_router()
    event = LineTextMessageEvent(reply_token="rt", user_id=_USER, text="関係ないテキスト")

    router.route_text(event, _NOW)

    assert conversation.text_calls == [(_USER, state, "関係ないテキスト")]


# --- ③ 状態無しのtextはすべてヘルプ(Issue #24: legacy CSVコマンド経路廃止) --


@pytest.mark.parametrize(
    "text",
    [
        # 旧Legacy CSVフルコマンド(廃止済み。ヘルプのみ返し、一切永続化しない)
        "買付,7203,100,2000",
        "売却,7203,100,2000",
        "ウォッチ,7203",
        # 断片的なCSV・通常の未認識テキスト
        "8306,100,2500",
        "こんにちは",
    ],
)
def test_text_without_state_goes_to_help_and_never_reaches_any_service(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    """状態無しのtextは(旧Legacy CSVコマンドを含め)ConversationServiceへも
    どこへも到達せず、固定ヘルプのみ返すこと。永続化ゼロはフェイクが一切
    呼ばれないことで保証される(実書き込みの回帰はテスト末尾の
    test_legacy_csv_text_without_state_causes_zero_persistenceで検証)。"""
    _patch_state(monkeypatch, None)
    router, conversation = _build_router()
    event = LineTextMessageEvent(reply_token="rt", user_id=_USER, text=text)

    reply = router.route_text(event, _NOW)

    assert "コマンドが認識できませんでした" in reply.text
    assert conversation.text_calls == []
    assert conversation.postback_calls == []


def test_help_text_does_not_advertise_removed_legacy_csv_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ヘルプ文言が廃止済みのCSV書式(買付,/売却,/ウォッチ,)を案内しないこと。"""
    _patch_state(monkeypatch, None)
    router, _ = _build_router()
    event = LineTextMessageEvent(reply_token="rt", user_id=_USER, text="help")

    reply = router.route_text(event, _NOW)

    assert "買付," not in reply.text
    assert "売却," not in reply.text
    assert "ウォッチ," not in reply.text
    assert "メニュー" in reply.text


def test_legacy_csv_text_without_state_causes_zero_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #24の新仕様の核心回帰: 実ConversationServiceを組み込んだrouterへ
    旧Legacy CSVコマンドを送っても、リポジトリ層への書き込みが一切発生しない
    こと(routerがヘルプを返すだけでどのサービスにも到達しないため、
    conversation_state_store.get以外のインフラ呼び出しはゼロ)。"""
    _patch_state(monkeypatch, None)
    router = line_event_router.build_line_event_router()

    calls: list[str] = []
    for name in ("start_or_replace", "record_input", "retry", "cancel", "discard_input"):
        monkeypatch.setattr(
            line_event_router.conversation_state_store,
            name,
            lambda *a, _n=name, **kw: calls.append(_n),
        )

    for text in ("買付,7203,100,2000", "売却,7203,100,2000", "ウォッチ,7203"):
        event = LineTextMessageEvent(reply_token="rt", user_id=_USER, text=text)
        reply = router.route_text(event, _NOW)
        assert "コマンドが認識できませんでした" in reply.text

    assert calls == []


def test_build_line_event_router_constructs_real_dependencies() -> None:
    router = line_event_router.build_line_event_router()
    assert router.conversation_service is not None
