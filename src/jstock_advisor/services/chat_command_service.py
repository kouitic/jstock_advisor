"""LINEチャット経由のコマンド処理(将来拡張: チャット起点の売買記録・ウォッチ登録)。

ユーザーがLINEで送ったCSV形式のテキストメッセージを、実売買記録・ウォッチリスト
登録のコマンドとして解釈する。誤登録を避けるため固定フォーマットのCSVのみを
受け付け、LLMによる自由文解析は行わない(要求仕様12節「推測で補完しない」原則)。
解釈できない入力は登録を行わず、書式を案内するエラー応答を返す。

送信者の認可(本人のLINEアカウントからの送信か)はWebhook層(lambda_handlers/
line_webhook.py)の責務とし、本サービスはコマンド解析・実行のみを担う。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass

from jstock_advisor.domain.entities.enums import AccountType, TransactionType
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.infrastructure.external_value_parser import ExternalValueParser
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.transaction_history_service import TransactionHistoryService
from jstock_advisor.services.watchlist_service import WatchlistService

_BUY_COMMAND = "買付"
_SELL_COMMAND = "売却"
_WATCH_COMMAND = "ウォッチ"

_HELP_TEXT = (
    "コマンドが認識できませんでした。以下のCSV形式で送信してください:\n"
    "買付,銘柄コード,株数,単価\n"
    "売却,銘柄コード,株数,単価\n"
    "ウォッチ,銘柄コード"
)


@dataclass(frozen=True)
class ChatCommandResult:
    reply_text: str
    success: bool


def _parse_csv_line(text: str) -> list[str] | None:
    try:
        rows = list(csv.reader(io.StringIO(text.strip())))
    except csv.Error:
        return None
    if not rows or not rows[0]:
        return None
    return [field.strip() for field in rows[0]]


class ChatCommandService:
    def __init__(
        self,
        transaction_history_service: TransactionHistoryService | None = None,
        watchlist_service: WatchlistService | None = None,
        portfolio_service: PortfolioService | None = None,
    ) -> None:
        self._transactions = transaction_history_service or TransactionHistoryService()
        self._watchlist = watchlist_service or WatchlistService()
        self._portfolio = portfolio_service or PortfolioService()

    def is_legacy_command(self, text: str) -> bool:
        """既存のCSV形式フルコマンド(買付/売却/ウォッチで始まる)かどうかを判定する。

        LineEventRouter(LINEボタン起点会話型UI・実装プランv2 5節)が、
        ConversationStateが無いテキストをChatCommandServiceへ渡すべきか判定
        するために使う。判定基準はhandle()の分岐条件と完全に同じ(先頭フィールド
        が_BUY_COMMAND/_SELL_COMMAND/_WATCH_COMMANDのいずれか)であり、
        ルーター側が日本語コマンド文字列を独自にハードコードして重複判定
        しないための公開メソッド(私有定数_BUY_COMMAND等はモジュール外から
        参照できないため)。
        """
        fields = _parse_csv_line(text)
        if not fields:
            return False
        return fields[0] in (_BUY_COMMAND, _SELL_COMMAND, _WATCH_COMMAND)

    def handle(self, text: str, now: dt.datetime | None = None) -> ChatCommandResult:
        now = now or dt.datetime.now(dt.UTC)
        fields = _parse_csv_line(text)
        if not fields:
            return ChatCommandResult(_HELP_TEXT, False)

        command, args = fields[0], fields[1:]
        if command in (_BUY_COMMAND, _SELL_COMMAND):
            return self._handle_transaction(command, args, now)
        if command == _WATCH_COMMAND:
            return self._handle_watchlist(args)
        return ChatCommandResult(_HELP_TEXT, False)

    def _handle_transaction(
        self, command: str, args: list[str], now: dt.datetime
    ) -> ChatCommandResult:
        if len(args) != 3:
            return ChatCommandResult(
                f"{command}は「{command},銘柄コード,株数,単価」の形式で送信してください", False
            )
        stock_code_raw, shares_raw, price_raw = args

        stock_code = ExternalValueParser.stock_code(stock_code_raw)
        if stock_code is None:
            return ChatCommandResult("銘柄コードが不正です(4桁の英数字が必要です)", False)

        shares = ExternalValueParser.integer(shares_raw)
        if shares is None or shares <= 0:
            return ChatCommandResult("株数は正の整数で指定してください", False)

        price = ExternalValueParser.decimal(price_raw)
        if price is None or price <= 0:
            return ChatCommandResult("単価は正の数値で指定してください", False)

        holding = self._portfolio.get_holding(stock_code)
        if command == _BUY_COMMAND:
            transaction_type = (
                TransactionType.ADDITIONAL_BUY if holding is not None else TransactionType.BUY
            )
        else:
            if holding is None:
                return ChatCommandResult(f"{stock_code}は保有銘柄として登録されていません", False)
            if shares > holding.shares:
                return ChatCommandResult(
                    f"保有株数({holding.shares}株)を超える売却はできません", False
                )
            transaction_type = (
                TransactionType.FULL_SELL
                if shares == holding.shares
                else TransactionType.PARTIAL_SELL
            )

        today_jst = evaluation_date_jst(now)
        try:
            self._transactions.record_execution(
                stock_code=stock_code,
                transaction_type=transaction_type,
                shares=shares,
                execution_price=price,
                execution_date=today_jst,
                now=now,
            )
        except ValueError as e:
            return ChatCommandResult(str(e), False)

        if command == _BUY_COMMAND:
            self._portfolio.register_purchase(
                stock_code=stock_code,
                stock_name=None,
                shares=shares,
                purchase_price=price,
                purchase_date=today_jst,
                account_type=AccountType.GENERAL,
            )
        else:
            self._portfolio.sell_shares(stock_code, shares)

        return ChatCommandResult(
            f"記録しました: {transaction_type.value} {stock_code} {shares}株 @{price}円", True
        )

    def _handle_watchlist(self, args: list[str]) -> ChatCommandResult:
        if len(args) != 1 or not args[0]:
            return ChatCommandResult(
                "ウォッチは「ウォッチ,銘柄コード」の形式で送信してください", False
            )
        stock_code = ExternalValueParser.stock_code(args[0])
        if stock_code is None:
            return ChatCommandResult("銘柄コードが不正です(4桁の英数字が必要です)", False)
        item = self._watchlist.add_item(stock_code=stock_code)
        return ChatCommandResult(f"ウォッチリストに追加しました: {item.stock_code}", True)
