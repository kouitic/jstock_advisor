import base64
import hashlib
import hmac
import json

from jstock_advisor.infrastructure.line.webhook import (
    parse_postback_events,
    parse_text_message_events,
    verify_line_signature,
)

_SECRET = "test-channel-secret"


def _sign(body: bytes) -> str:
    computed = hmac.new(_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(computed).decode("utf-8")


def test_verify_line_signature_accepts_valid_signature() -> None:
    body = b'{"events": []}'
    assert verify_line_signature(_SECRET, body, _sign(body)) is True


def test_verify_line_signature_rejects_invalid_signature() -> None:
    body = b'{"events": []}'
    assert verify_line_signature(_SECRET, body, "invalid-signature") is False


def test_verify_line_signature_rejects_tampered_body() -> None:
    original = b'{"events": []}'
    signature = _sign(original)
    tampered = b'{"events": [1]}'
    assert verify_line_signature(_SECRET, tampered, signature) is False


def _event_body(
    text: str, user_id: str = "U1234567890abcdef", reply_token: str = "reply-1"
) -> bytes:
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
    return json.dumps(payload).encode("utf-8")


def test_parse_text_message_events_extracts_single_event() -> None:
    events = parse_text_message_events(_event_body("買付,8136,100,3775"))
    assert len(events) == 1
    assert events[0].text == "買付,8136,100,3775"
    assert events[0].user_id == "U1234567890abcdef"
    assert events[0].reply_token == "reply-1"


def test_parse_text_message_events_ignores_non_message_events() -> None:
    payload = {"events": [{"type": "follow", "source": {"type": "user", "userId": "U1"}}]}
    events = parse_text_message_events(json.dumps(payload).encode("utf-8"))
    assert events == []


def test_parse_text_message_events_ignores_non_text_messages() -> None:
    payload = {
        "events": [
            {
                "type": "message",
                "replyToken": "reply-1",
                "source": {"type": "user", "userId": "U1"},
                "message": {"type": "sticker"},
            }
        ]
    }
    events = parse_text_message_events(json.dumps(payload).encode("utf-8"))
    assert events == []


def test_parse_text_message_events_returns_empty_for_invalid_json() -> None:
    assert parse_text_message_events(b"not json") == []


def test_parse_text_message_events_returns_empty_for_unexpected_structure() -> None:
    assert parse_text_message_events(b'{"foo": "bar"}') == []


def test_parse_text_message_events_handles_multiple_events() -> None:
    payload = {
        "events": [
            {
                "type": "message",
                "replyToken": "reply-1",
                "source": {"type": "user", "userId": "U1"},
                "message": {"type": "text", "text": "買付,8136,100,3775"},
            },
            {
                "type": "message",
                "replyToken": "reply-2",
                "source": {"type": "user", "userId": "U1"},
                "message": {"type": "text", "text": "ウォッチ,7203"},
            },
        ]
    }
    events = parse_text_message_events(json.dumps(payload).encode("utf-8"))
    assert [e.text for e in events] == ["買付,8136,100,3775", "ウォッチ,7203"]


# --- parse_postback_events(LINEボタン起点会話型UI・実装プランv2 4節) ---------


def _postback_body(
    data: str, user_id: str = "U1234567890abcdef", reply_token: str = "reply-1"
) -> bytes:
    payload = {
        "events": [
            {
                "type": "postback",
                "replyToken": reply_token,
                "source": {"type": "user", "userId": user_id},
                "postback": {"data": data},
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


def test_parse_postback_events_extracts_action_only() -> None:
    events = parse_postback_events(_postback_body("action=start_buy"))
    assert len(events) == 1
    assert events[0].action == "start_buy"
    assert events[0].op is None
    assert events[0].user_id == "U1234567890abcdef"
    assert events[0].reply_token == "reply-1"


def test_parse_postback_events_extracts_action_and_op() -> None:
    events = parse_postback_events(_postback_body("action=confirm&op=abc-123"))
    assert len(events) == 1
    assert events[0].action == "confirm"
    assert events[0].op == "abc-123"


def test_parse_postback_events_ignores_unknown_action() -> None:
    events = parse_postback_events(_postback_body("action=delete_everything"))
    assert events == []


def test_parse_postback_events_ignores_unparseable_data() -> None:
    events = parse_postback_events(_postback_body(""))
    assert events == []


def test_parse_postback_events_ignores_non_postback_events() -> None:
    events = parse_postback_events(_event_body("買付,8136,100,3775"))
    assert events == []


def test_parse_postback_events_returns_empty_for_invalid_json() -> None:
    assert parse_postback_events(b"not json") == []


def test_parse_postback_events_handles_all_confirmed_actions() -> None:
    for action in (
        "start_buy",
        "start_sell",
        "start_watch",
        "confirm",
        "retry",
        "cancel",
        "show_holdings",
        "show_watchlist",
        "show_targets",
    ):
        events = parse_postback_events(_postback_body(f"action={action}&op=x"))
        assert len(events) == 1
        assert events[0].action == action


# --- LINE UI第二弾(保有銘柄/ウォッチリスト/対象確認、2026-08) --------------------


def test_parse_postback_events_extracts_owner_for_show_holdings() -> None:
    events = parse_postback_events(
        _postback_body("action=show_holdings&owner=%E6%89%80%E6%9C%89%E8%80%85A")
    )
    assert len(events) == 1
    assert events[0].action == "show_holdings"
    assert events[0].owner == "所有者A"
    assert events[0].category is None


def test_parse_postback_events_show_holdings_without_owner_is_none() -> None:
    events = parse_postback_events(_postback_body("action=show_holdings"))
    assert len(events) == 1
    assert events[0].owner is None


def test_parse_postback_events_extracts_category_for_show_targets() -> None:
    events = parse_postback_events(
        _postback_body("action=show_targets&category=%E8%B2%B7%E3%81%84%E9%96%93%E8%BF%91")
    )
    assert len(events) == 1
    assert events[0].action == "show_targets"
    assert events[0].category == "買い間近"
    assert events[0].owner is None


def test_parse_postback_events_show_watchlist_has_no_owner_or_category() -> None:
    events = parse_postback_events(_postback_body("action=show_watchlist"))
    assert len(events) == 1
    assert events[0].owner is None
    assert events[0].category is None
