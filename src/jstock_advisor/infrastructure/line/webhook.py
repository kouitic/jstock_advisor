"""LINE Webhookの署名検証・イベント解析(将来拡張: チャット起点の登録)。

LINEはWebhookリクエストのボディに対するHMAC-SHA256署名(Base64エンコード)を
`X-Line-Signature`ヘッダーに付与する。Channel Secret(LINE_CHANNEL_SECRET、
LINE_CHANNEL_ACCESS_TOKENとは別物)を用いて検証し、正当なリクエストであることを
確認してから処理する(第三者による不正リクエストの実行を防ぐため)。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any


def verify_line_signature(channel_secret: str, body: bytes, signature: str) -> bool:
    computed = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(computed).decode("utf-8")
    return hmac.compare_digest(expected, signature)


@dataclass(frozen=True)
class LineTextMessageEvent:
    reply_token: str
    user_id: str
    text: str


def parse_text_message_events(body: bytes) -> list[LineTextMessageEvent]:
    """Webhookボディからテキストメッセージイベントのみを抽出する。

    テキスト以外のイベント(スタンプ・画像・フォロー等)は無視する。JSONとして
    解釈できない、または想定した構造でない場合は空リストを返す(推測で補完しない)。
    """
    try:
        payload: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []

    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        return []

    results: list[LineTextMessageEvent] = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "message":
            continue
        message = event.get("message")
        if not isinstance(message, dict) or message.get("type") != "text":
            continue
        text = message.get("text")
        source = event.get("source")
        reply_token = event.get("replyToken")
        if not isinstance(text, str) or not isinstance(source, dict):
            continue
        user_id = source.get("userId")
        if not isinstance(user_id, str) or not isinstance(reply_token, str):
            continue
        results.append(LineTextMessageEvent(reply_token=reply_token, user_id=user_id, text=text))
    return results
