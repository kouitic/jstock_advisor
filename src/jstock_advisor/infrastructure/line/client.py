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
from typing import Protocol


class LineClient(Protocol):
    def push_message(self, text: str) -> None: ...


class LiveLineClient:
    _ENDPOINT = "https://api.line.me/v2/bot/message/push"

    def __init__(self, channel_access_token: str, user_id: str) -> None:
        self._token = channel_access_token
        self._user_id = user_id

    def push_message(self, text: str) -> None:
        body = json.dumps(
            {"to": self._user_id, "messages": [{"type": "text", "text": text}]}
        ).encode("utf-8")
        request = urllib.request.Request(
            self._ENDPOINT,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                if response.status >= 300:
                    raise RuntimeError(f"LINE通知の送信に失敗しました: status={response.status}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"LINE通知の送信に失敗しました: {e.code} {e.reason}") from e


class ConsoleLineClient:
    """LINE認証情報が無い場合のドライラン実装。標準出力に表示するのみで送信しない。"""

    def __init__(self) -> None:
        self.sent_messages: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent_messages.append(text)
        print("----- [LINE通知(ドライラン・未送信)] -----")
        print(text)
        print("--------------------------------------")


def build_line_client_from_env() -> LineClient:
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.environ.get("LINE_USER_ID")
    if token and user_id:
        return LiveLineClient(channel_access_token=token, user_id=user_id)
    return ConsoleLineClient()
