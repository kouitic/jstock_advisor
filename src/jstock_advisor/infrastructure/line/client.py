"""LINE Messaging API クライアント(要求仕様16節)。

Push Message API を使用する。チャネルアクセストークン・ユーザーIDは
Secrets Manager(本番)または環境変数(ローカル)から取得し、コード・ログには
記録しない(要求仕様21節)。認証情報が無い場合は標準出力に表示するのみの
ドライラン実装(ConsoleLineClient)にフォールバックする。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class QuickReplyButton:
    """LINEのQuick Reply(postbackアクション)1件分(LINEボタン起点会話型UI・
    実装プランv2 4節)。`display_text`未指定時はlabelをチャット履歴表示用文言
    として流用する。"""

    label: str
    postback_data: str
    display_text: str | None = None


class LineClient(Protocol):
    def push_message(self, text: str) -> None: ...

    def reply_message(
        self, reply_token: str, text: str, quick_reply: list[QuickReplyButton] | None = None
    ) -> None: ...

    def reply_messages(
        self,
        reply_token: str,
        texts: list[str],
        quick_reply: list[QuickReplyButton] | None = None,
    ) -> None:
        """複数メッセージでの返信(ウォッチリスト表示改善2026-08、最大5件、
        LINE Reply APIの上限)。quick_replyは最後のメッセージにのみ付与する
        (LINEの一般的な慣行に合わせる)。reply_message()は後方互換のため
        変更しない(既存の単一メッセージ経路はそのまま動作する)。
        """
        ...


def _build_message(text: str, quick_reply: list[QuickReplyButton] | None) -> dict[str, object]:
    message: dict[str, object] = {"type": "text", "text": text}
    if quick_reply:
        message["quickReply"] = {
            "items": [
                {
                    "type": "action",
                    "action": {
                        "type": "postback",
                        "label": button.label,
                        "data": button.postback_data,
                        "displayText": button.display_text or button.label,
                    },
                }
                for button in quick_reply
            ]
        }
    return message


def _post_messages(token: str, endpoint: str, payload: dict[str, object]) -> None:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            if response.status >= 300:
                raise RuntimeError(f"LINE通知の送信に失敗しました: status={response.status}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LINE通知の送信に失敗しました: {e.code} {e.reason}") from e


class LiveLineClient:
    _PUSH_ENDPOINT = "https://api.line.me/v2/bot/message/push"
    _REPLY_ENDPOINT = "https://api.line.me/v2/bot/message/reply"

    def __init__(self, channel_access_token: str, user_id: str) -> None:
        self._token = channel_access_token
        self._user_id = user_id

    def push_message(self, text: str) -> None:
        _post_messages(
            self._token,
            self._PUSH_ENDPOINT,
            {"to": self._user_id, "messages": [{"type": "text", "text": text}]},
        )

    def reply_message(
        self, reply_token: str, text: str, quick_reply: list[QuickReplyButton] | None = None
    ) -> None:
        """Webhookで受信したイベントのreplyTokenを使って返信する(Push枠を消費しない)。"""
        _post_messages(
            self._token,
            self._REPLY_ENDPOINT,
            {"replyToken": reply_token, "messages": [_build_message(text, quick_reply)]},
        )

    def reply_messages(
        self,
        reply_token: str,
        texts: list[str],
        quick_reply: list[QuickReplyButton] | None = None,
    ) -> None:
        if not texts:
            return
        messages = [
            _build_message(text, quick_reply if i == len(texts) - 1 else None)
            for i, text in enumerate(texts)
        ]
        _post_messages(
            self._token,
            self._REPLY_ENDPOINT,
            {"replyToken": reply_token, "messages": messages},
        )


class ConsoleLineClient:
    """LINE認証情報が無い場合のドライラン実装。標準出力に表示するのみで送信しない。"""

    def __init__(self) -> None:
        self.sent_messages: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent_messages.append(text)
        print("----- [LINE通知(ドライラン・未送信)] -----")
        print(text)
        print("--------------------------------------")

    def reply_message(
        self, reply_token: str, text: str, quick_reply: list[QuickReplyButton] | None = None
    ) -> None:
        self.sent_messages.append(text)
        print(f"----- [LINE返信(ドライラン・未送信、reply_token={reply_token})] -----")
        print(text)
        if quick_reply:
            print(f"  quick_reply: {[b.label for b in quick_reply]}")
        print("--------------------------------------")

    def reply_messages(
        self,
        reply_token: str,
        texts: list[str],
        quick_reply: list[QuickReplyButton] | None = None,
    ) -> None:
        for i, text in enumerate(texts):
            self.sent_messages.append(text)
            print(
                f"----- [LINE返信(ドライラン・未送信、reply_token={reply_token}、"
                f"{i + 1}/{len(texts)}通目)] -----"
            )
            print(text)
            if quick_reply and i == len(texts) - 1:
                print(f"  quick_reply: {[b.label for b in quick_reply]}")
            print("--------------------------------------")


def build_line_client_from_env() -> LineClient:
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.environ.get("LINE_USER_ID")
    if token and user_id:
        return LiveLineClient(channel_access_token=token, user_id=user_id)
    return ConsoleLineClient()
