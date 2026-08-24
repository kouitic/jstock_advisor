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
from urllib.parse import parse_qs


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


@dataclass(frozen=True)
class LinePostbackEvent:
    reply_token: str
    user_id: str
    action: str
    op: str | None
    # LINE UI第二弾(保有銘柄/ウォッチリスト/対象確認、2026-08)向け。
    # show_holdings(owner選択後)・show_targets(category選択後)のみ設定される。
    owner: str | None = None
    category: str | None = None


# LINEボタン起点会話型UI(2026-08)・実装プランv2 4節で確定したpostback data値
# (リッチメニュー3種+Quick Reply3種)。LINE UI第二弾(2026-08)でshow_holdings/
# show_watchlist/show_targetsを追加(保有銘柄/ウォッチリスト/対象確認、
# いずれも読み取り専用)。ここに無い値・パースできないdataは「想定外の
# action値」として無視する(推測で補完しない)。
_VALID_POSTBACK_ACTIONS = frozenset(
    {
        "start_buy",
        "start_sell",
        "start_watch",
        "confirm",
        "retry",
        "cancel",
        "show_holdings",
        "show_watchlist",
        "show_targets",
    }
)


def parse_postback_events(body: bytes) -> list[LinePostbackEvent]:
    """Webhookボディからpostbackイベントのみを抽出する。

    `data`(`action=confirm&op=xxxx`のようなURLクエリ文字列)をパースし、
    actionが既知の値(4節のpostback data定義)でない場合はそのイベント自体を
    無視する(不正なdata・パースできないdataも同様)。
    """
    try:
        payload: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []

    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        return []

    results: list[LinePostbackEvent] = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "postback":
            continue
        postback = event.get("postback")
        if not isinstance(postback, dict):
            continue
        data = postback.get("data")
        source = event.get("source")
        reply_token = event.get("replyToken")
        if not isinstance(data, str) or not isinstance(source, dict):
            continue
        user_id = source.get("userId")
        if not isinstance(user_id, str) or not isinstance(reply_token, str):
            continue

        parsed = parse_qs(data, keep_blank_values=True)
        action_values = parsed.get("action")
        if not action_values or action_values[0] not in _VALID_POSTBACK_ACTIONS:
            continue
        op_values = parsed.get("op")
        op = op_values[0] if op_values else None
        owner_values = parsed.get("owner")
        owner = owner_values[0] if owner_values else None
        category_values = parsed.get("category")
        category = category_values[0] if category_values else None
        results.append(
            LinePostbackEvent(
                reply_token=reply_token,
                user_id=user_id,
                action=action_values[0],
                op=op,
                owner=owner,
                category=category,
            )
        )
    return results
