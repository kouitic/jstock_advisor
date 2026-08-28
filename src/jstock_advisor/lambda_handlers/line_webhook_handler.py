"""LINE Webhookハンドラ(API Gateway経由)。

リッチメニュー/Quick Reply起点のボタン操作(会話型UI)を受け付ける
(legacy CSVテキストコマンド経路はIssue #24で廃止済み)。処理の流れ:
1. 署名検証(X-Line-Signatureヘッダー、LINE_CHANNEL_SECRET環境変数)。
   検証に失敗した場合は403を返し一切処理しない。
2. 送信者認可(LINE_USER_ID環境変数と一致するuserIdのみ処理する。
   Webhook URLが第三者に知られた場合でも本人以外のメッセージは無視する)。
3. postback/テキストメッセージイベントをLineEventRouterへ振り分ける
   (jstock_advisor.services.line_event_router参照。判定ルールはそちらの
   モジュールdocstringを参照)。
4. 実行結果をreplyTokenで返信する(Quick Reply付きの場合あり)。
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
import os
from typing import Any

from jstock_advisor.infrastructure.line.client import LineClient, build_line_client_from_env
from jstock_advisor.infrastructure.line.webhook import (
    parse_postback_events,
    parse_text_message_events,
    verify_line_signature,
)
from jstock_advisor.services.conversation_service import ConversationReply
from jstock_advisor.services.line_event_router import build_line_event_router

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# レビュー対応(2026-08、本番デプロイ後確認で発覚): 銘柄分析等の処理中に想定外の
# 例外(今回のAuditLogTable IAM不足のような設定不備・その他バグ)が発生すると、
# 従来はhandler()の外まで例外が伝播しAPI Gatewayが5xxを返すのみで、reply_token
# を使った返信が一切行われず、LINEユーザーには何の応答も届かない状態になって
# いた。他のLambdaハンドラ(holdings_watchlist_handler.py等)が既に採用している
# 「1件の想定外エラーで全体を落とさない」パターンをそのまま踏襲し、1イベント
# 単位で例外を隔離する(例外を握りつぶすのではなく、CloudWatchへ詳細を記録した
# うえで、ユーザーには内部情報を含まない簡潔な応答のみを返す)。
_INTERNAL_ERROR_REPLY = ConversationReply(
    "内部エラーが発生しました。しばらくしてからもう一度お試しください。"
)


def _send_reply(line_client: LineClient, reply_token: str, reply: ConversationReply) -> None:
    """ウォッチリスト表示改善(2026-08)向け: reply.textsが設定されている場合は
    複数メッセージ返信(LineClient.reply_messages)、それ以外は既存の単一
    メッセージ返信を使う(後方互換)。"""
    if reply.texts:
        line_client.reply_messages(reply_token, reply.texts, reply.quick_reply)
    else:
        line_client.reply_message(reply_token, reply.text, reply.quick_reply)


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
    router = build_line_event_router()
    now = dt.datetime.now(dt.UTC)

    handled = 0
    ignored = 0
    failed = 0
    for postback_event in parse_postback_events(body_bytes):
        if postback_event.user_id != authorized_user_id:
            logger.warning("ignoring postback from unauthorized userId")
            ignored += 1
            continue
        try:
            reply = router.route_postback(postback_event, now)
            _send_reply(line_client, postback_event.reply_token, reply)
        except Exception:  # noqa: BLE001 - 1件の想定外エラーで他イベント処理・返信を止めない
            logger.exception(
                "postback handling failed unexpectedly action=%s", postback_event.action
            )
            failed += 1
            try:
                _send_reply(line_client, postback_event.reply_token, _INTERNAL_ERROR_REPLY)
            except Exception:  # noqa: BLE001 - 返信自体の失敗(reply_token失効等)は記録のみ
                logger.exception("failed to send internal error reply for postback")
            continue
        handled += 1

    for message_event in parse_text_message_events(body_bytes):
        if message_event.user_id != authorized_user_id:
            logger.warning("ignoring message from unauthorized userId")
            ignored += 1
            continue
        try:
            reply = router.route_text(message_event, now)
            _send_reply(line_client, message_event.reply_token, reply)
        except Exception:  # noqa: BLE001 - 1件の想定外エラーで他イベント処理・返信を止めない
            logger.exception("text message handling failed unexpectedly")
            failed += 1
            try:
                _send_reply(line_client, message_event.reply_token, _INTERNAL_ERROR_REPLY)
            except Exception:  # noqa: BLE001 - 返信自体の失敗(reply_token失効等)は記録のみ
                logger.exception("failed to send internal error reply for text message")
            continue
        handled += 1

    logger.info(
        "line_webhook_handler done: handled=%d ignored=%d failed=%d", handled, ignored, failed
    )
    return {"statusCode": 200, "body": "ok"}
