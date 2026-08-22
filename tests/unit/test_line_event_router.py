"""LineEventRouterの判定ルールのテスト(LINEボタン起点会話型UI・実装プランv2 5節)。

優先順位: ①postbackは常にConversationService、②有効なConversationStateが
あればテキスト形式に関わらずConversationService、③状態が無く既存のLegacy
CSVフルコマンドのみChatCommandService、④それ以外はヘルプ。

ConversationServiceが実際に何を書き込むかはtest_conversation_service.py・
test_conversation_commit.pyで検証済みのため、ここでは「どちらのサービスに
振り分けられるか」の判定ロジックのみをフェイクで検証する。
"""

from __future__ import annotations

import datetime as dt

import pytest

from jstock_advisor.domain.entities.enums import ConversationAction, ConversationStateName
from jstock_advisor.infrastructure.aws.conversation_state_store import ConversationState
from jstock_advisor.infrastructure.line.webhook import LinePostbackEvent, LineTextMessageEvent
from jstock_advisor.services import line_event_router
from jstock_advisor.services.chat_command_service import ChatCommandResult
from jstock_advisor.services.conversation_service import ConversationReply
from jstock_advisor.services.line_event_router import LineEventRouter

_NOW = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)
_USER = "U1"


class _FakeConversationService:
    def __init__(self) -> None:
        self.postback_calls: list[tuple[str, str, str | None]] = []
        self.text_calls: list[tuple[str, ConversationState, str]] = []

    def handle_postback(
        self, user_id: str, action: str, op: str | None, now: dt.datetime
    ) -> ConversationReply:
        self.postback_calls.append((user_id, action, op))
        return ConversationReply("conversation-postback-reply")

    def handle_text_input(
        self, user_id: str, state: ConversationState, text: str, now: dt.datetime
    ) -> ConversationReply:
        self.text_calls.append((user_id, state, text))
        return ConversationReply("conversation-text-reply")


class _FakeChatCommandService:
    def __init__(self, is_legacy: bool) -> None:
        self._is_legacy = is_legacy
        self.handled: list[str] = []

    def is_legacy_command(self, text: str) -> bool:
        return self._is_legacy

    def handle(self, text: str, now: dt.datetime | None = None) -> ChatCommandResult:
        self.handled.append(text)
        return ChatCommandResult(f"chat-reply:{text}", True)


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


def _build_router(
    is_legacy: bool = False,
) -> tuple[LineEventRouter, _FakeConversationService, _FakeChatCommandService]:
    conversation = _FakeConversationService()
    chat_command = _FakeChatCommandService(is_legacy=is_legacy)
    router = LineEventRouter(conversation_service=conversation, chat_command_service=chat_command)  # type: ignore[arg-type]
    return router, conversation, chat_command


# --- ① postbackは常にConversationService ------------------------------------


def test_postback_always_goes_to_conversation_service_regardless_of_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_state(monkeypatch, None)
    router, conversation, chat_command = _build_router()
    event = LinePostbackEvent(reply_token="rt", user_id=_USER, action="start_buy", op=None)

    reply = router.route_postback(event, _NOW)

    assert reply.text == "conversation-postback-reply"
    assert conversation.postback_calls == [(_USER, "start_buy", None)]
    assert chat_command.handled == []


# --- ② 有効なConversationStateがあれば常にConversationService --------------


def test_text_during_input_waiting_goes_to_conversation_service_even_if_csv_shaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BUY_INPUT_WAITING中の"8306,100,2500"はCSV形式に見えるが、
    ChatCommandServiceは一切呼ばれないこと(実装プランv2の指摘4の核心)。"""
    state = _confirm_state()
    _patch_state(monkeypatch, state)
    router, conversation, chat_command = _build_router(is_legacy=False)
    event = LineTextMessageEvent(reply_token="rt", user_id=_USER, text="8306,100,2500")

    reply = router.route_text(event, _NOW)

    assert reply.text == "conversation-text-reply"
    assert conversation.text_calls == [(_USER, state, "8306,100,2500")]
    assert chat_command.handled == []


def test_text_during_confirm_waiting_with_unrelated_text_goes_to_conversation_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _confirm_state()
    _patch_state(monkeypatch, state)
    router, conversation, chat_command = _build_router()
    event = LineTextMessageEvent(reply_token="rt", user_id=_USER, text="関係ないテキスト")

    router.route_text(event, _NOW)

    assert conversation.text_calls == [(_USER, state, "関係ないテキスト")]
    assert chat_command.handled == []


# --- ③ 状態が無く、既存のLegacy CSVフルコマンドのみChatCommandService -------


def test_text_without_state_and_legacy_command_goes_to_chat_command_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_state(monkeypatch, None)
    router, conversation, chat_command = _build_router(is_legacy=True)
    event = LineTextMessageEvent(reply_token="rt", user_id=_USER, text="買付,8306,100,2500")

    reply = router.route_text(event, _NOW)

    assert reply.text == "chat-reply:買付,8306,100,2500"
    assert chat_command.handled == ["買付,8306,100,2500"]
    assert conversation.text_calls == []


# --- ④ 状態無し・非Legacy CSV → ヘルプ(両サービスとも呼ばれない) -----------


def test_text_without_state_and_fragment_goes_to_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_state(monkeypatch, None)
    router, conversation, chat_command = _build_router(is_legacy=False)
    event = LineTextMessageEvent(reply_token="rt", user_id=_USER, text="8306,100,2500")

    reply = router.route_text(event, _NOW)

    assert "コマンドが認識できませんでした" in reply.text
    assert conversation.text_calls == []
    assert chat_command.handled == []


def test_build_line_event_router_constructs_real_dependencies() -> None:
    router = line_event_router.build_line_event_router()
    assert router.conversation_service is not None
    assert router.chat_command_service is not None
