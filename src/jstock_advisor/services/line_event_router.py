"""LINE Webhookイベントの振り分けルール(LINEボタン起点会話型UI・
実装プランv2 5節、Issue #24でlegacy CSVコマンド経路を廃止)。

優先順位(この順序を変更しないこと。詳細はfunctional_spec.md参照):
  ① postbackイベントは常にConversationServiceへ(状態の有無に関わらず)。
  ② textイベントで有効なConversationStateが存在する → ConversationService
     (テキストがCSV形式に見えるかどうかに関わらず、常に現在の入力待ち状態
     の期待フォーマットとして解釈する。例: BUY_INPUT_WAITING中の
     "8306,100,2500")。
  ③ それ以外(状態無しのtext全般) → ヘルプ応答(状態変更なし・永続化なし)。

かつて存在した「状態無し+Legacy CSVフルコマンド(買付/売却/ウォッチ)→
ChatCommandService」の分岐はIssue #24で廃止した(利用実態がなく、非原子書き込み・
TradingPause非対応・owner固定等の劣位があった。売買・ウォッチ登録は
リッチメニュー起点のConversationService経路へ一本化)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from jstock_advisor.infrastructure.aws import conversation_state_store
from jstock_advisor.infrastructure.line.webhook import LinePostbackEvent, LineTextMessageEvent
from jstock_advisor.services.conversation_service import ConversationReply, ConversationService

_HELP_TEXT = (
    "コマンドが認識できませんでした。\n"
    "トーク画面下部のメニューから「買った」「売った」「お気に入り登録」などの"
    "操作を選び、画面の案内に従って入力してください。"
)


@dataclass(frozen=True)
class LineEventRouter:
    conversation_service: ConversationService

    def route_postback(self, event: LinePostbackEvent, now: dt.datetime) -> ConversationReply:
        return self.conversation_service.handle_postback(
            event.user_id,
            event.action,
            event.op,
            now,
            owner=event.owner,
            category=event.category,
            code=event.code,
        )

    def route_text(self, event: LineTextMessageEvent, now: dt.datetime) -> ConversationReply:
        state = conversation_state_store.get(event.user_id, now)
        if state is not None:
            return self.conversation_service.handle_text_input(
                event.user_id, state, event.text, now
            )
        return ConversationReply(_HELP_TEXT)


def build_line_event_router() -> LineEventRouter:
    return LineEventRouter(conversation_service=ConversationService())
