"""LINE Webhookイベントの振り分けルール(LINEボタン起点会話型UI・
実装プランv2 5節)。

優先順位(この順序を変更しないこと。詳細はfunctional_spec.md参照):
  ① postbackイベントは常にConversationServiceへ(状態の有無に関わらず)。
  ② textイベントで有効なConversationStateが存在する → ConversationService
     (テキストがCSV形式に見えるかどうかに関わらず、常に現在の入力待ち状態
     の期待フォーマットとして解釈する。例: BUY_INPUT_WAITING中の
     "8306,100,2500")。
  ③ textイベントで状態が無く、既存の完全なLegacy CSVコマンド(買付/売却/
     ウォッチで始まる)である → ChatCommandService(現状維持・挙動変更なし)。
  ④ それ以外(状態無し・非CSV・断片的なCSV等) → ヘルプ応答(状態変更なし)。

「CSV形式か否か」だけで判定しない設計へ変更した理由: 新LINEフロー自身が
期待する入力("8306,100,2500")もCSV形式であり、旧方式の「CSV→ChatCommand
Service」判定ではConversationState保有中の入力を誤ってChatCommandServiceへ
渡してしまう(実装プランv2の指摘4)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from jstock_advisor.infrastructure.aws import conversation_state_store
from jstock_advisor.infrastructure.line.webhook import LinePostbackEvent, LineTextMessageEvent
from jstock_advisor.services.chat_command_service import ChatCommandService
from jstock_advisor.services.conversation_service import ConversationReply, ConversationService

_HELP_TEXT = (
    "コマンドが認識できませんでした。\n"
    "メニューから「買った」「売った」「お気に入り登録」を選ぶか、"
    "以下のCSV形式で送信してください:\n"
    "買付,銘柄コード,株数,単価\n"
    "売却,銘柄コード,株数,単価\n"
    "ウォッチ,銘柄コード"
)


@dataclass(frozen=True)
class LineEventRouter:
    conversation_service: ConversationService
    chat_command_service: ChatCommandService

    def route_postback(self, event: LinePostbackEvent, now: dt.datetime) -> ConversationReply:
        return self.conversation_service.handle_postback(
            event.user_id,
            event.action,
            event.op,
            now,
            owner=event.owner,
            category=event.category,
        )

    def route_text(self, event: LineTextMessageEvent, now: dt.datetime) -> ConversationReply:
        state = conversation_state_store.get(event.user_id, now)
        if state is not None:
            return self.conversation_service.handle_text_input(
                event.user_id, state, event.text, now
            )
        if self.chat_command_service.is_legacy_command(event.text):
            result = self.chat_command_service.handle(event.text, now)
            return ConversationReply(result.reply_text)
        return ConversationReply(_HELP_TEXT)


def build_line_event_router() -> LineEventRouter:
    return LineEventRouter(
        conversation_service=ConversationService(),
        chat_command_service=ChatCommandService(),
    )
