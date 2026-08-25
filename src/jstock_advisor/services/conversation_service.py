"""LINEボタン起点の会話型UI(2026-08)のオーケストレーション層。

責務: リッチメニュー/postback起点の対話(BUY/SELL/WATCH、INPUT_WAITING⇔
CONFIRM_WAITINGの状態遷移)を管理し、確認(「登録する」)時のみ
infrastructure.aws.conversation_commitを通じてTransactWriteItemsによる
原子コミットを実行する。

`TransactionHistoryService.record_execution()`・`PortfolioService.
register_purchase()`/`sell_shares()`・`WatchlistService.add_item()`は
一切呼ばない(これらは即時永続化する既存の同期API)。代わりに各サービスの
「計画のみを返す」新設メソッド(build_purchase_write_plan/build_sale_write_plan/
build_execution_plan/build_add_item_plan)を使い、実際の書き込みは
conversation_commit.commit_*()の単一TransactWriteItems呼び出しに集約する
(要求仕様: 明示的な確認前に一切の永続化を行わない、実装プランv2 1節)。

既存のCSVテキストコマンド(ChatCommandService)とは完全に独立した経路であり、
挙動を変更しない(LineEventRouterが状態の有無で振り分ける。実装プランv2 5節)。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import quote

from jstock_advisor.domain.entities.enums import (
    AccountType,
    ConversationAction,
    ConversationStateName,
    TransactionType,
)
from jstock_advisor.domain.entities.owner import InvalidOwnerError, normalize_and_validate_owner
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.infrastructure.aws import conversation_commit, conversation_state_store
from jstock_advisor.infrastructure.aws.conversation_state_store import ConversationState
from jstock_advisor.infrastructure.external_value_parser import ExternalValueParser
from jstock_advisor.infrastructure.line.client import QuickReplyButton
from jstock_advisor.infrastructure.local_repository.holding_repository import HoldingRepository
from jstock_advisor.services.buy_candidate_target_view_service import (
    CATEGORY_DISPLAY_LABELS,
    BuyCandidateTargetViewService,
    is_valid_category_label,
)
from jstock_advisor.services.holdings_view_service import HoldingsViewService
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.stock_analysis_view_service import StockAnalysisViewService
from jstock_advisor.services.trading_pause_service import TradingPauseService
from jstock_advisor.services.transaction_history_service import TransactionHistoryService
from jstock_advisor.services.watchlist_display_name import (
    StockDisplayNameResolver,
    build_stock_display_name_resolver,
)
from jstock_advisor.services.watchlist_service import WatchlistService
from jstock_advisor.services.watchlist_view_service import WatchlistViewService

# StockDisplayNameResolver.build_stock_display_name_resolver()向け。
# watchlist_screening_rules.yamlのjpx_name_negative_cache_ttl_seconds既定値
# (60秒)と同じ値を、本機能専用にハードコードする(会話型UIをウォッチリスト
# スクリーニング設定へ依存させないための意図的な分離)。
_STOCK_NAME_NEGATIVE_CACHE_TTL_SECONDS = 60

_BUY_PROMPT = "所有者,銘柄コード,株数,単価 の形式で送信してください(例: 本人,8306,100,1500)"
_SELL_PROMPT = "所有者,銘柄コード,株数,単価 の形式で送信してください(例: 本人,8306,100,1800)"
_WATCH_PROMPT = "銘柄コードを送信してください(例: 8306)"
_ANALYZE_PROMPT = "分析したい銘柄コードを送信してください(例: 8306)"
_INVALID_OWNER = "所有者の指定が不正です"
_START_PROMPTS: dict[ConversationAction, str] = {
    ConversationAction.BUY: f"購入記録を開始します。\n{_BUY_PROMPT}",
    ConversationAction.SELL: f"売却記録を開始します。\n{_SELL_PROMPT}",
    ConversationAction.WATCH: f"ウォッチリスト登録を開始します。\n{_WATCH_PROMPT}",
    ConversationAction.ANALYZE: f"銘柄分析を開始します。\n{_ANALYZE_PROMPT}",
}
_CONFIRM_WAITING_GUIDANCE = "ボタンから操作してください(登録する/やり直す/キャンセル)"
_NO_ACTIVE_OPERATION = "有効な操作がありません。メニューからやり直してください。"
_STATE_CHANGED = "状態が変わりました。もう一度メニューからやり直してください。"
# 追加条件1: 計画構築時点とTransactWriteItems実行時点の間に保有情報が変更された
# 場合、古い計画のままリトライせず、この文言で再操作を促す(安全側の挙動)。
_WRITE_CONFLICT = "最新の保有状況が変更されたため登録できませんでした。\nもう一度操作してください。"
_CANCELLED = "操作をキャンセルしました。"
_CANCEL_FAILED = "キャンセルできませんでした。もう一度お試しください。"
_RETRY_FAILED = "操作をやり直せませんでした。メニューからやり直してください。"
_UNKNOWN_POSTBACK = "認識できない操作です。メニューからやり直してください。"
_INVALID_STOCK_CODE = "銘柄コードが不正です(4桁の英数字が必要です)"
# 保有銘柄オーナー機能移行時の書込停止(TradingPauseConfig)。WATCHには適用しない
# (commit_watch()はHoldings/PurchaseLotsを一切更新しないため対象外)。
_TRADING_PAUSED = (
    "ただいまシステムメンテナンス中のため、購入・売却操作を一時的に停止しています。"
    "しばらくしてからもう一度お試しください。"
)

# --- LINE UI第二弾(保有銘柄/ウォッチリスト/対象確認、2026-08、読み取り専用) ---
_NO_OWNERS_REGISTERED = "保有銘柄が登録されていません。"
_SELECT_OWNER_PROMPT = "誰の保有銘柄を確認しますか？"
_NO_HOLDINGS_FOR_OWNER = "該当する保有銘柄がありません。"
_EMPTY_WATCHLIST = "ウォッチリストは空です。"
_SELECT_TARGET_CATEGORY_PROMPT = "確認する対象を選択してください。"
_NO_TARGETS_FOR_CATEGORY = "該当する銘柄がありません。"
_UNKNOWN_CATEGORY = "認識できないカテゴリーです。メニューからやり直してください。"

# --- 銘柄分析(Phase 2-B、2026-08、読み取り専用) --------------------------
_NO_ANALYSIS_DATA = (
    "この銘柄コードは分析対象データに見つかりませんでした。"
    "保有銘柄・直近のBUY候補いずれにも該当しない可能性があります。"
)
_SELECT_ANALYSIS_KIND_PROMPT = "確認したい内容を選択してください。"
_SELECT_ANALYSIS_OWNER_PROMPT = "誰の保有分について確認しますか？"
_ANALYSIS_BUY_LABEL = "購入判定を見る"
_ANALYSIS_SELL_LABEL = "売却・保有判定を見る"
# LINEテキストメッセージの上限(公式5000文字)に対する安全マージン込みの
# 打ち切り基準。件数ではなく実際の生成文字数を基準にする(v2 5節)。
_MESSAGE_CHAR_BUDGET = 4500


def _unknown_stock_code_reply(stock_code: str) -> ConversationReply:
    return ConversationReply(
        f"{stock_code}に該当する銘柄が見つかりませんでした。銘柄コードをご確認のうえ、"
        "もう一度送信してください。"
    )


@dataclass(frozen=True)
class ConversationReply:
    text: str
    quick_reply: list[QuickReplyButton] | None = None
    # ウォッチリスト表示改善(2026-08)向け: 複数LINEメッセージへ分割して返信
    # する場合に設定する(最大5件、LineClient.reply_messages参照)。設定時は
    # 呼び出し側(line_webhook_handler.py)がtextではなくこちらを使う。
    # textは後方互換・ログ表示用に先頭メッセージを保持する。
    texts: list[str] | None = None


def _confirm_quick_reply(operation_id: str) -> list[QuickReplyButton]:
    return [
        QuickReplyButton(label="登録する", postback_data=f"action=confirm&op={operation_id}"),
        QuickReplyButton(label="やり直す", postback_data=f"action=retry&op={operation_id}"),
        QuickReplyButton(label="キャンセル", postback_data=f"action=cancel&op={operation_id}"),
    ]


def _build_list_reply(header: str, lines: list[str], empty_message: str) -> ConversationReply:
    """一覧表示系(保有銘柄/ウォッチリスト/対象確認)の共通整形。件数ではなく
    実際の生成文字数(_MESSAGE_CHAR_BUDGET)を基準に打ち切る(v2 5節)。
    """
    if not lines:
        return ConversationReply(empty_message)

    body_lines = [header, ""]
    total_len = len(header) + 1
    included = 0
    for line in lines:
        candidate_len = total_len + len(line) + 1
        if candidate_len > _MESSAGE_CHAR_BUDGET:
            break
        body_lines.append(line)
        total_len = candidate_len
        included += 1
    if included < len(lines):
        omitted = len(lines) - included
        body_lines.append(f"…他{omitted}件(文字数上限のため省略、絞り込んでご確認ください)")
    return ConversationReply("\n".join(body_lines))


def _parse_csv_fields(text: str) -> list[str] | None:
    try:
        rows = list(csv.reader(io.StringIO(text.strip())))
    except csv.Error:
        return None
    if not rows or not rows[0]:
        return None
    return [field.strip() for field in rows[0]]


class ConversationService:
    def __init__(
        self,
        portfolio_service: PortfolioService | None = None,
        transaction_history_service: TransactionHistoryService | None = None,
        watchlist_service: WatchlistService | None = None,
        stock_display_name_resolver: StockDisplayNameResolver | None = None,
        trading_pause_service: TradingPauseService | None = None,
        holdings_view_service: HoldingsViewService | None = None,
        watchlist_view_service: WatchlistViewService | None = None,
        target_view_service: BuyCandidateTargetViewService | None = None,
        stock_analysis_view_service: StockAnalysisViewService | None = None,
        holding_repository: HoldingRepository | None = None,
    ) -> None:
        self._portfolio = portfolio_service or PortfolioService()
        self._transactions = transaction_history_service or TransactionHistoryService()
        self._watchlist = watchlist_service or WatchlistService()
        self._display_name_resolver = (
            stock_display_name_resolver
            or build_stock_display_name_resolver(_STOCK_NAME_NEGATIVE_CACHE_TTL_SECONDS)
        )
        self._trading_pause = trading_pause_service or TradingPauseService()
        # LINE UI第二弾(保有銘柄/ウォッチリスト/対象確認、2026-08、読み取り専用)。
        # いずれもHolding/Watchlist/BuyCandidateEvaluationRecord等の正データを
        # 一切書き換えない(19節: 読み取り専用機能としての安全性)。
        self._holdings_view = holdings_view_service or HoldingsViewService(
            display_name_resolver=self._display_name_resolver
        )
        self._watchlist_view = watchlist_view_service or WatchlistViewService(
            display_name_resolver=self._display_name_resolver
        )
        self._target_view = target_view_service or BuyCandidateTargetViewService(
            display_name_resolver=self._display_name_resolver
        )
        # Phase 2-B「銘柄分析」向け(2026-08、読み取り専用)。
        self._stock_analysis_view = stock_analysis_view_service or StockAnalysisViewService(
            display_name_resolver=self._display_name_resolver
        )
        self._holdings_repo = holding_repository or HoldingRepository()

    # --- postback(リッチメニュー起点・Quick Reply起点) ---------------------

    def handle_postback(
        self,
        user_id: str,
        action: str,
        op: str | None,
        now: dt.datetime,
        owner: str | None = None,
        category: str | None = None,
        code: str | None = None,
    ) -> ConversationReply:
        if action == "start_buy":
            return self._start(user_id, ConversationAction.BUY, now)
        if action == "start_sell":
            return self._start(user_id, ConversationAction.SELL, now)
        if action == "start_watch":
            return self._start(user_id, ConversationAction.WATCH, now)
        if action == "start_analyze":
            return self._start(user_id, ConversationAction.ANALYZE, now)
        if action == "confirm":
            return self._confirm(user_id, op, now)
        if action == "retry":
            return self._retry(user_id, op, now)
        if action == "cancel":
            return self._cancel(user_id, op, now)
        if action == "show_holdings":
            return self._show_holdings(owner)
        if action == "show_watchlist":
            return self._show_watchlist()
        if action == "show_targets":
            return self._show_targets(category)
        if action == "show_analysis_buy":
            return self._show_analysis_buy(code)
        if action == "show_analysis_sell":
            return self._show_analysis_sell(code, owner)
        return ConversationReply(_UNKNOWN_POSTBACK)

    # --- 保有銘柄/ウォッチリスト/対象確認(読み取り専用、状態を一切持たない) ---

    def _show_holdings(self, owner: str | None) -> ConversationReply:
        if owner is not None:
            lines = self._holdings_view.build_owner_holdings_lines(owner)
            return _build_list_reply(f"【{owner}の保有銘柄】", lines, _NO_HOLDINGS_FOR_OWNER)

        owners = self._holdings_view.list_owners()
        if not owners:
            return ConversationReply(_NO_OWNERS_REGISTERED)
        quick_reply = [
            QuickReplyButton(
                label=name, postback_data=f"action=show_holdings&owner={quote(name)}"
            )
            for name in owners
        ]
        return ConversationReply(_SELECT_OWNER_PROMPT, quick_reply=quick_reply)

    def _show_watchlist(self) -> ConversationReply:
        message_groups = self._watchlist_view.build_message_groups()
        if isinstance(message_groups, str):
            # GSI反映待ち(直近の分析結果を反映中です...)の安全側メッセージ。
            return ConversationReply(message_groups)
        if not message_groups:
            return ConversationReply(_EMPTY_WATCHLIST)
        texts = [
            "\n".join(["【ウォッチリスト】", *group] if i == 0 else group)
            for i, group in enumerate(message_groups)
        ]
        return ConversationReply(texts[0], texts=texts)

    def _show_targets(self, category: str | None) -> ConversationReply:
        if category is not None:
            if not is_valid_category_label(category):
                return ConversationReply(_UNKNOWN_CATEGORY)
            lines = self._target_view.build_lines(category)
            if isinstance(lines, str):
                return ConversationReply(lines)
            header = f"【{category}｜直近分析】"
            reply = _build_list_reply(header, lines, _NO_TARGETS_FOR_CATEGORY)
            if lines:
                return ConversationReply(f"{reply.text}\n{len(lines)}件")
            return reply

        quick_reply = [
            QuickReplyButton(
                label=label, postback_data=f"action=show_targets&category={quote(label)}"
            )
            for label in CATEGORY_DISPLAY_LABELS
        ]
        return ConversationReply(_SELECT_TARGET_CATEGORY_PROMPT, quick_reply=quick_reply)

    # --- 銘柄分析(Phase 2-B、2026-08、読み取り専用) -------------------------

    def _show_analysis_buy(self, stock_code: str | None) -> ConversationReply:
        if stock_code is None:
            return ConversationReply(_NO_ANALYSIS_DATA)
        return ConversationReply(self._stock_analysis_view.build_buy_analysis_text(stock_code))

    def _show_analysis_sell(
        self, stock_code: str | None, owner: str | None
    ) -> ConversationReply:
        if stock_code is None:
            return ConversationReply(_NO_ANALYSIS_DATA)
        if owner is not None:
            return ConversationReply(
                self._stock_analysis_view.build_holding_analysis_text(owner, stock_code)
            )

        holdings = self._holdings_repo.list_by_stock(stock_code)
        if not holdings:
            return ConversationReply(_NO_ANALYSIS_DATA)
        if len(holdings) == 1:
            return ConversationReply(
                self._stock_analysis_view.build_holding_analysis_text(
                    holdings[0].owner, stock_code
                )
            )
        # 複数owner保有(修正4): owner名を一切ハードコードせず、既存Holdingから
        # 動的にQuick Replyを組み立てる(HoldingRepository.list_distinct_owners()
        # と同じ設計方針)。
        quick_reply = [
            QuickReplyButton(
                label=holding.owner,
                postback_data=(
                    f"action=show_analysis_sell&code={quote(stock_code)}"
                    f"&owner={quote(holding.owner)}"
                ),
            )
            for holding in sorted(holdings, key=lambda h: h.owner)
        ]
        return ConversationReply(_SELECT_ANALYSIS_OWNER_PROMPT, quick_reply=quick_reply)

    def _start(
        self, user_id: str, action: ConversationAction, now: dt.datetime
    ) -> ConversationReply:
        if action in (
            ConversationAction.BUY,
            ConversationAction.SELL,
        ) and self._trading_pause.is_buy_sell_paused():
            return ConversationReply(_TRADING_PAUSED)
        conversation_state_store.start_or_replace(user_id, action, now)
        return ConversationReply(_START_PROMPTS[action])

    def _confirm_waiting_state_or_none(
        self, user_id: str, op: str | None, now: dt.datetime
    ) -> ConversationState | None:
        if not op:
            return None
        state = conversation_state_store.get(user_id, now)
        if (
            state is None
            or state.state != ConversationStateName.CONFIRM_WAITING
            or state.operation_id != op
        ):
            return None
        return state

    def _confirm(self, user_id: str, op: str | None, now: dt.datetime) -> ConversationReply:
        state = self._confirm_waiting_state_or_none(user_id, op, now)
        if state is None:
            return ConversationReply(_NO_ACTIVE_OPERATION)
        try:
            if state.action == ConversationAction.BUY:
                return self._commit_buy(user_id, state, now)
            if state.action == ConversationAction.SELL:
                return self._commit_sell(user_id, state, now)
            return self._commit_watch(user_id, state, now)
        except ValueError:
            # build_*_write_plan()が計画構築直前の状態不整合(保有株数不足等)を
            # 検知した場合。TransactWriteItemsの楽観ロック競合と同じ安全側の
            # 案内のみ返し、状態は変更しない(実装プランv2追加条件1)。
            return ConversationReply(_WRITE_CONFLICT)

    def _retry(self, user_id: str, op: str | None, now: dt.datetime) -> ConversationReply:
        state = self._confirm_waiting_state_or_none(user_id, op, now)
        if state is None:
            return ConversationReply(_NO_ACTIVE_OPERATION)
        new_state = conversation_state_store.retry(user_id, state.action, state.operation_id, now)
        if new_state is None:
            return ConversationReply(_RETRY_FAILED)
        return ConversationReply(_START_PROMPTS[new_state.action])

    def _cancel(self, user_id: str, op: str | None, now: dt.datetime) -> ConversationReply:
        state = self._confirm_waiting_state_or_none(user_id, op, now)
        if state is None:
            return ConversationReply(_NO_ACTIVE_OPERATION)
        ok = conversation_state_store.cancel(user_id, state.operation_id, now)
        return ConversationReply(_CANCELLED if ok else _CANCEL_FAILED)

    # --- text入力(LineEventRouterが有効なConversationStateを検知した場合のみ呼ぶ) --

    def handle_text_input(
        self, user_id: str, state: ConversationState, text: str, now: dt.datetime
    ) -> ConversationReply:
        if state.state == ConversationStateName.CONFIRM_WAITING:
            # 誤登録防止最優先: 想定外のtextでは状態を一切変更しない(実装プランv2 2節)。
            return ConversationReply(_CONFIRM_WAITING_GUIDANCE)
        if state.action == ConversationAction.WATCH:
            return self._handle_watch_input(user_id, state, text, now)
        if state.action == ConversationAction.ANALYZE:
            return self._handle_analyze_input(user_id, state, text, now)
        return self._handle_trade_input(user_id, state.action, text, now)

    def _handle_trade_input(
        self, user_id: str, action: ConversationAction, text: str, now: dt.datetime
    ) -> ConversationReply:
        # pause前に開始済みのBUY/SELL(INPUT_WAITING)がCSV入力によりCONFIRM_WAITING
        # へ進んでしまわないよう、ここでも確認する(_startのチェックだけに
        # 依存しない。ConversationStateは一切変更しない)。WATCHは対象外
        # (_handle_watch_inputは別経路のためここには来ない)。
        if self._trading_pause.is_buy_sell_paused():
            return ConversationReply(_TRADING_PAUSED)
        fields = _parse_csv_fields(text)
        if fields is None or len(fields) != 4:
            return ConversationReply(_START_PROMPTS[action])

        try:
            owner = normalize_and_validate_owner(fields[0])
        except InvalidOwnerError:
            return ConversationReply(_INVALID_OWNER)

        stock_code = ExternalValueParser.stock_code(fields[1])
        if stock_code is None:
            return ConversationReply(_INVALID_STOCK_CODE)
        # 指摘3: 存在しない銘柄コードは確認画面へ進めず、この時点で拒否する。
        # JPXデータソースが利用できない場合(None)は判定不能として処理を継続
        # する(一時的なデータ取得失敗を理由に正当な入力をブロックしないため)。
        if self._display_name_resolver.exists(stock_code) is False:
            return _unknown_stock_code_reply(stock_code)
        shares = ExternalValueParser.integer(fields[2])
        if shares is None or shares <= 0:
            return ConversationReply("株数は正の整数で指定してください")
        price = ExternalValueParser.decimal(fields[3])
        if price is None or price <= 0:
            return ConversationReply("単価は正の数値で指定してください")

        current_shares: int | None = None
        if action == ConversationAction.SELL:
            holding = self._portfolio.get_holding(owner, stock_code)
            if holding is None:
                return ConversationReply(f"{stock_code}は保有銘柄として登録されていません")
            if shares > holding.shares:
                return ConversationReply(f"保有株数({holding.shares}株)を超える売却はできません")
            current_shares = holding.shares

        new_state = conversation_state_store.record_input(
            user_id, action, stock_code, now, shares=shares, price=price, owner=owner
        )
        if new_state is None:
            return ConversationReply(_STATE_CHANGED)
        if action == ConversationAction.SELL:
            assert current_shares is not None
            return self._build_sell_confirmation_reply(
                owner, stock_code, current_shares, shares, price, new_state.operation_id
            )
        return self._build_buy_confirmation_reply(
            owner, stock_code, shares, price, new_state.operation_id
        )

    def _handle_watch_input(
        self, user_id: str, state: ConversationState, text: str, now: dt.datetime
    ) -> ConversationReply:
        fields = _parse_csv_fields(text)
        if fields is None or len(fields) != 1:
            return ConversationReply(_START_PROMPTS[ConversationAction.WATCH])
        stock_code = ExternalValueParser.stock_code(fields[0])
        if stock_code is None:
            return ConversationReply(_INVALID_STOCK_CODE)
        if self._display_name_resolver.exists(stock_code) is False:
            return _unknown_stock_code_reply(stock_code)

        if self._watchlist.get_item(stock_code) is not None:
            # コードレビュー2026-08-17 指摘1: build_add_item_plan()は既存項目の
            # reason/priority等をデフォルト値で再構築するため、確認画面へ進めて
            # しまうと既存設定を失う。確認前にここで終了し、既存WatchlistItemは
            # 一切変更しない(Legacy CSV側のadd_item()の上書き挙動自体は変更しない)。
            # 再レビュー指摘: ここで返信するだけではINPUT_WAITINGのConversationState
            # が残ったままになり、次の通常テキスト(Legacy CSVコマンド等)が誤って
            # WATCH入力として処理されてしまう。安全な条件付きDeleteで対話自体を
            # 終了させる(無条件Deleteはせず、action/state/operation_id/ttlが
            # 一致する場合のみ)。
            conversation_state_store.discard_input(
                user_id, ConversationAction.WATCH, state.operation_id, now
            )
            return ConversationReply(f"{stock_code}はすでにお気に入りに登録されています。")

        new_state = conversation_state_store.record_input(
            user_id, ConversationAction.WATCH, stock_code, now
        )
        if new_state is None:
            return ConversationReply(_STATE_CHANGED)

        display_name = self._display_name_resolver.resolve(stock_code)
        text_body = (
            f"{display_name}({stock_code})をウォッチリストに追加します。"
            "よろしければ「登録する」を押してください。"
        )
        return ConversationReply(
            text_body, quick_reply=_confirm_quick_reply(new_state.operation_id)
        )

    def _handle_analyze_input(
        self, user_id: str, state: ConversationState, text: str, now: dt.datetime
    ) -> ConversationReply:
        """銘柄分析(Phase 2-B、2026-08): 4文字銘柄コード入力→state終了→
        購入判定/売却・保有判定の選択。読み取り専用のためCONFIRM_WAITINGへは
        進まず、入力を受け取った時点でConversationStateを終了する
        (修正9: start_analyze→INPUT_WAITING→4文字コード入力→state終了→選択)。
        """
        fields = _parse_csv_fields(text)
        if fields is None or len(fields) != 1:
            return ConversationReply(_START_PROMPTS[ConversationAction.ANALYZE])
        stock_code = ExternalValueParser.stock_code(fields[0])
        if stock_code is None:
            return ConversationReply(_INVALID_STOCK_CODE)

        conversation_state_store.discard_input(
            user_id, ConversationAction.ANALYZE, state.operation_id, now
        )

        has_buy = self._stock_analysis_view.has_buy_analysis(stock_code)
        has_sell = bool(self._holdings_repo.list_by_stock(stock_code))
        if not has_buy and not has_sell:
            return _unknown_stock_code_reply(stock_code)

        buttons = []
        if has_buy:
            buttons.append(
                QuickReplyButton(
                    label=_ANALYSIS_BUY_LABEL,
                    postback_data=f"action=show_analysis_buy&code={quote(stock_code)}",
                )
            )
        if has_sell:
            buttons.append(
                QuickReplyButton(
                    label=_ANALYSIS_SELL_LABEL,
                    postback_data=f"action=show_analysis_sell&code={quote(stock_code)}",
                )
            )
        return ConversationReply(_SELECT_ANALYSIS_KIND_PROMPT, quick_reply=buttons)

    def _build_buy_confirmation_reply(
        self,
        owner: str,
        stock_code: str,
        shares: int,
        price: Decimal,
        operation_id: str,
    ) -> ConversationReply:
        display_name = self._display_name_resolver.resolve(stock_code)
        amount = shares * price
        text_body = (
            "以下の内容で登録します。よろしければ「登録する」を押してください。\n\n"
            f"所有者：{owner}\n"
            f"{display_name}({stock_code})\n"
            f"買付: {shares:,}株 @{price:,}円\n"
            f"合計: {amount:,}円"
        )
        return ConversationReply(text_body, quick_reply=_confirm_quick_reply(operation_id))

    def _build_sell_confirmation_reply(
        self,
        owner: str,
        stock_code: str,
        current_shares: int,
        shares: int,
        price: Decimal,
        operation_id: str,
    ) -> ConversationReply:
        """コードレビュー2026-08-17 指摘2(当初の受入条件): SELL確認画面には
        現在保有・今回売却・売却後の株数を明示する。確認画面生成時点で読み
        取ったHolding(current_shares)のみを表示に使い、実際の確定時には
        conversation_commit側の楽観ロックで別途整合性を保証する。"""
        display_name = self._display_name_resolver.resolve(stock_code)
        remaining_shares = current_shares - shares
        amount = shares * price
        text_body = (
            "売却内容をご確認ください。\n\n"
            f"所有者：{owner}\n"
            f"銘柄：{display_name}（{stock_code}）\n"
            f"現在保有：{current_shares:,}株\n"
            f"今回売却：{shares:,}株\n"
            f"売却後：{remaining_shares:,}株\n"
            f"売却単価：{price:,}円\n"
            f"売却金額：{amount:,}円\n\n"
            "この内容で登録しますか？"
        )
        return ConversationReply(text_body, quick_reply=_confirm_quick_reply(operation_id))

    # --- confirm(登録する)実行時の原子コミット -------------------------------

    def _commit_buy(
        self, user_id: str, state: ConversationState, now: dt.datetime
    ) -> ConversationReply:
        # 会話開始後にpauseがtrueへ切り替わった場合の防御(実際の書き込みが
        # 発生する直前でも必ず再確認する。開始時点(_start)のチェックだけに
        # 依存しない)。
        if self._trading_pause.is_buy_sell_paused():
            return ConversationReply(_TRADING_PAUSED)
        assert state.stock_code is not None
        assert state.shares is not None
        assert state.price is not None
        assert state.owner is not None
        holding = self._portfolio.get_holding(state.owner, state.stock_code)
        transaction_type = (
            TransactionType.ADDITIONAL_BUY if holding is not None else TransactionType.BUY
        )
        execution_date = evaluation_date_jst(now)
        plan = self._portfolio.build_purchase_write_plan(
            owner=state.owner,
            stock_code=state.stock_code,
            stock_name=None,
            shares=state.shares,
            purchase_price=state.price,
            purchase_date=execution_date,
            account_type=AccountType.GENERAL,
            now=now,
        )
        transaction = self._transactions.build_execution_plan(
            transaction_id=state.operation_id,
            owner=state.owner,
            stock_code=state.stock_code,
            transaction_type=transaction_type,
            shares=state.shares,
            execution_price=state.price,
            execution_date=execution_date,
            now=now,
        )
        success = conversation_commit.commit_buy(
            user_id, state.operation_id, plan, transaction, now
        )
        if not success:
            return ConversationReply(_WRITE_CONFLICT)
        return ConversationReply(
            f"登録しました: 買付 {state.stock_code} {state.shares:,}株 @{state.price:,}円"
        )

    def _commit_sell(
        self, user_id: str, state: ConversationState, now: dt.datetime
    ) -> ConversationReply:
        if self._trading_pause.is_buy_sell_paused():
            return ConversationReply(_TRADING_PAUSED)
        assert state.stock_code is not None
        assert state.shares is not None
        assert state.price is not None
        assert state.owner is not None
        holding = self._portfolio.get_holding(state.owner, state.stock_code)
        if holding is None:
            return ConversationReply(_WRITE_CONFLICT)
        transaction_type = (
            TransactionType.FULL_SELL
            if state.shares == holding.shares
            else TransactionType.PARTIAL_SELL
        )
        execution_date = evaluation_date_jst(now)
        plan = self._portfolio.build_sale_write_plan(
            state.owner, state.stock_code, state.shares, now=now
        )
        transaction = self._transactions.build_execution_plan(
            transaction_id=state.operation_id,
            owner=state.owner,
            stock_code=state.stock_code,
            transaction_type=transaction_type,
            shares=state.shares,
            execution_price=state.price,
            execution_date=execution_date,
            now=now,
        )
        success = conversation_commit.commit_sell(
            user_id, state.operation_id, plan, transaction, now
        )
        if not success:
            return ConversationReply(_WRITE_CONFLICT)
        return ConversationReply(
            f"登録しました: 売却 {state.stock_code} {state.shares:,}株 @{state.price:,}円"
        )

    def _commit_watch(
        self, user_id: str, state: ConversationState, now: dt.datetime
    ) -> ConversationReply:
        assert state.stock_code is not None
        watchlist_item = self._watchlist.build_add_item_plan(stock_code=state.stock_code)
        success = conversation_commit.commit_watch(
            user_id, state.operation_id, watchlist_item, now
        )
        if not success:
            return ConversationReply(_WRITE_CONFLICT)
        display_name = self._display_name_resolver.resolve(state.stock_code)
        return ConversationReply(
            f"ウォッチリストに追加しました: {display_name}({state.stock_code})"
        )
