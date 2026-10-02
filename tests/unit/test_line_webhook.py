import base64
import hashlib
import hmac
import json

import pytest

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
        "start_available_cash_reconcile",
    ):
        events = parse_postback_events(_postback_body(f"action={action}&op=x"))
        assert len(events) == 1
        assert events[0].action == action


def test_parse_postback_events_handles_start_available_cash_reconcile() -> None:
    """Issue #628回帰テスト: `_VALID_POSTBACK_ACTIONS`への追加漏れにより、
    「余力管理」ボタン(action=start_available_cash_reconcile)のpostbackが
    サイレントに読み捨てられ(handled/ignoredいずれのカウンタにも到達せず)、
    Production上で無反応になっていた。
    """
    events = parse_postback_events(
        _postback_body(
            "action=start_available_cash_reconcile",
            user_id="Uavailablecash0001",
            reply_token="reply-628",
        )
    )
    assert len(events) == 1
    assert events[0].action == "start_available_cash_reconcile"
    assert events[0].user_id == "Uavailablecash0001"
    assert events[0].reply_token == "reply-628"


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


# --- Issue #630: allowlist不一致の失敗の可視性 ------------------------------


def test_issue_630_unknown_action_logs_warning_without_leaking_the_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """allowlist不一致のactionは無音で捨てず、WARNINGログへ事実(reject・
    action有無・長さ)のみを記録する。action自体の値(内容)はログへ一切
    出さない(PII/secret等の非公開値が紛れ込む可能性があるため。USER決定、
    #630)。
    """
    secret_action = "delete_everything_confidential_token_abc123"
    with caplog.at_level("WARNING"):
        events = parse_postback_events(_postback_body(f"action={secret_action}"))

    assert events == []
    assert "postback ignored" in caplog.text
    assert "action not in allowlist" in caplog.text
    assert "action_present=True" in caplog.text
    assert f"action_length={len(secret_action)}" in caplog.text
    # 値そのものが文字列として一切出現しないこと(単に改行が無いことの確認ではない)。
    assert secret_action not in caplog.text


def test_issue_630_action_length_is_capped_at_the_logged_maximum(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """長さの表示にも上限がある(値の実際の長さをそのまま出さない。長さ自体が
    手がかりになり得る極端なケースへの配慮)。"""
    very_long_action = "x" * 500
    with caplog.at_level("WARNING"):
        parse_postback_events(_postback_body(f"action={very_long_action}"))

    assert "action_length=64" in caplog.text
    assert "action_length=500" not in caplog.text


def test_issue_630_missing_action_logs_action_present_false(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """actionパラメータ自体が欠落している場合、action_present=False・
    action_length=Noneになる(action_valuesが空のケース)。"""
    with caplog.at_level("WARNING"):
        events = parse_postback_events(_postback_body("op=abc-123"))

    assert events == []
    assert "action_present=False" in caplog.text
    assert "action_length=None" in caplog.text


def test_issue_630_known_action_does_not_log_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """既知のactionは従来どおり警告を出さない(回帰確認)。"""
    with caplog.at_level("WARNING"):
        events = parse_postback_events(_postback_body("action=start_buy"))

    assert len(events) == 1
    assert "postback ignored" not in caplog.text
