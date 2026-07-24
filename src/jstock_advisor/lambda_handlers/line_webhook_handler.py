"""LINE Webhookハンドラ(API Gateway経由)。

チャット起点の売買記録・ウォッチリスト登録を受け付ける。処理の流れ:
1. 署名検証(X-Line-Signatureヘッダー、LINE_CHANNEL_SECRET環境変数)。
   検証に失敗した場合は403を返し一切処理しない。
2. 送信者認可(LINE_USER_ID環境変数と一致するuserIdのみ処理する。
   Webhook URLが第三者に知られた場合でも本人以外のメッセージは無視する)。
3. テキストメッセージイベントをCSVコマンドとして解釈・実行
   (jstock_advisor.services.chat_command_service参照)。
4. 実行結果をreplyTokenで返信する。
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.infrastructure.line.webhook import (
    parse_text_message_events,
    verify_line_signature,
)
from jstock_advisor.services.chat_command_service import ChatCommandService

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _extract_body_bytes(event: dict[str, Any]) -> bytes:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return str(body).encode("utf-8")


def _extract_signature(event: dict[str, Any]) -> str | None:
    headers = event.get("headers") or {}
    # API Gateway(HTTP API)はヘッダー名を小文字化して渡すが、念のため両方見る
    signature = headers.get("x-line-signature") or headers.get("X-Line-Signature")
    return str(signature) if signature else None


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    channel_secret = os.environ.get("LINE_CHANNEL_SECRET")
    authorized_user_id = os.environ.get("LINE_USER_ID")
    if not channel_secret or not authorized_user_id:
        logger.error("LINE_CHANNEL_SECRET/LINE_USER_ID is not configured")
        return {"statusCode": 500, "body": "not configured"}

    body_bytes = _extract_body_bytes(event)
    signature = _extract_signature(event)
    if not signature or not verify_line_signature(channel_secret, body_bytes, signature):
        logger.warning("invalid or missing X-Line-Signature")
        return {"statusCode": 403, "body": "invalid signature"}

    line_client = build_line_client_from_env()
    chat_service = ChatCommandService()

    handled = 0
    ignored = 0
    for message_event in parse_text_message_events(body_bytes):
        if message_event.user_id != authorized_user_id:
            logger.warning("ignoring message from unauthorized userId")
            ignored += 1
            continue
        result = chat_service.handle(message_event.text)
        line_client.reply_message(message_event.reply_token, result.reply_text)
        handled += 1

    logger.info("line_webhook_handler done: handled=%d ignored=%d", handled, ignored)
    return {"statusCode": 200, "body": "ok"}
