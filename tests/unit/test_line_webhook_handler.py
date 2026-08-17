import base64
import datetime as dt
import hashlib
import hmac
import json
from typing import Any

import pytest

from jstock_advisor.infrastructure.line.client import QuickReplyButton
from jstock_advisor.lambda_handlers import line_webhook_handler
from jstock_advisor.lambda_handlers.line_webhook_handler import (
    _extract_body_bytes,
    _extract_signature,
    handler,
)
from jstock_advisor.services.conversation_service import ConversationReply


def test_extract_body_bytes_plain_text() -> None:
    event = {"body": '{"events": []}', "isBase64Encoded": False}
    assert _extract_body_bytes(event) == b'{"events": []}'


def test_extract_body_bytes_base64_encoded() -> None:
    raw = '{"events": []}'
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    event = {"body": encoded, "isBase64Encoded": True}
    assert _extract_body_bytes(event) == raw.encode("utf-8")


def test_extract_body_bytes_missing_body_returns_empty() -> None:
    assert _extract_body_bytes({}) == b""


def test_extract_signature_lowercase_header() -> None:
    event = {"headers": {"x-line-signature": "abc123"}}
    assert _extract_signature(event) == "abc123"


def test_extract_signature_uppercase_header() -> None:
    event = {"headers": {"X-Line-Signature": "abc123"}}
    assert _extract_signature(event) == "abc123"


def test_extract_signature_missing_returns_none() -> None:
    assert _extract_signature({"headers": {}}) is None
    assert _extract_signature({}) is None


_SECRET = "test-channel-secret"
_AUTHORIZED_USER = "Uauthorized0000000000000000000000"


class _FakeLineClient:
    def __init__(self) -> None:
        self.pushed: list[str] = []
        self.replies: list[tuple[str, str]] = []

    def push_message(self, text: str) -> None:
        self.pushed.append(text)

    def reply_message(
        self, reply_token: str, text: str, quick_reply: list[QuickReplyButton] | None = None
    ) -> None:
        self.replies.append((reply_token, text))


class _FakeRouter:
    def __init__(self) -> None:
        self.handled_texts: list[str] = []

    def route_text(self, event: object, now: dt.datetime) -> ConversationReply:
        text = event.text  # type: ignore[attr-defined]
        self.handled_texts.append(text)
        return ConversationReply(f"reply-to:{text}")

    def route_postback(self, event: object, now: dt.datetime) -> ConversationReply:
        return ConversationReply("postback-not-used-in-these-tests")


def _sign(body: bytes) -> str:
    computed = hmac.new(_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(computed).decode("utf-8")


def _build_event(text: str, user_id: str, reply_token: str = "rt-1") -> dict[str, Any]:
    payload = {
        "events": [
            {
                "type": "message",
                "replyToken": reply_token,
                "source": {"type": "user", "userId": user_id},
                "message": {"type": "text", "text": text},
            }
        ]
    }
    body = json.dumps(payload)
    return {
        "headers": {"x-line-signature": _sign(body.encode("utf-8"))},
        "body": body,
        "isBase64Encoded": False,
    }


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> _FakeLineClient:
    client = _FakeLineClient()
    monkeypatch.setenv("LINE_CHANNEL_SECRET", _SECRET)
    monkeypatch.setenv("LINE_USER_ID", _AUTHORIZED_USER)
    monkeypatch.setattr(line_webhook_handler, "build_line_client_from_env", lambda: client)
    monkeypatch.setattr(line_webhook_handler, "build_line_event_router", lambda: _FakeRouter())
    return client


def test_handler_replies_for_authorized_user(fake_client: _FakeLineClient) -> None:
    event = _build_event("ウォッチ,7203", _AUTHORIZED_USER)
    response = handler(event, None)
    assert response["statusCode"] == 200
    assert len(fake_client.replies) == 1
    assert fake_client.replies[0][0] == "rt-1"
    assert "ウォッチ,7203" in fake_client.replies[0][1]


def test_handler_ignores_unauthorized_user(fake_client: _FakeLineClient) -> None:
    event = _build_event("ウォッチ,7203", "Uunauthorized00000000000000000000")
    response = handler(event, None)
    assert response["statusCode"] == 200
    assert fake_client.replies == []


def test_handler_rejects_invalid_signature(fake_client: _FakeLineClient) -> None:
    event = _build_event("ウォッチ,7203", _AUTHORIZED_USER)
    event["headers"]["x-line-signature"] = "tampered"
    response = handler(event, None)
    assert response["statusCode"] == 403
    assert fake_client.replies == []


def test_handler_returns_500_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINE_CHANNEL_SECRET", raising=False)
    monkeypatch.delenv("LINE_USER_ID", raising=False)
    response = handler({"headers": {}, "body": "{}"}, None)
    assert response["statusCode"] == 500
