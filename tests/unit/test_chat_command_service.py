import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import AccountType, TransactionType
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.local_repository.transaction_repository import (
    SkippedRecommendationRepository,
    TransactionRepository,
)
from jstock_advisor.infrastructure.local_repository.watchlist_repository import WatchlistRepository
from jstock_advisor.services.chat_command_service import ChatCommandService
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.transaction_history_service import TransactionHistoryService
from jstock_advisor.services.watchlist_service import WatchlistService

_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)


@pytest.fixture
def portfolio(tmp_path: Path) -> PortfolioService:
    return PortfolioService(
        holding_repository=HoldingRepository(store_dir=tmp_path),
        lot_repository=PurchaseLotRepository(store_dir=tmp_path),
    )


@pytest.fixture
def watchlist(tmp_path: Path) -> WatchlistService:
    return WatchlistService(repository=WatchlistRepository(store_dir=tmp_path))


@pytest.fixture
def transactions(tmp_path: Path) -> TransactionHistoryService:
    return TransactionHistoryService(
        transaction_repository=TransactionRepository(store_dir=tmp_path),
        skipped_repository=SkippedRecommendationRepository(store_dir=tmp_path),
        recommendation_repository=RecommendationRepository(store_dir=tmp_path),
    )


@pytest.fixture
def service(
    transactions: TransactionHistoryService,
    watchlist: WatchlistService,
    portfolio: PortfolioService,
) -> ChatCommandService:
    return ChatCommandService(
        transaction_history_service=transactions,
        watchlist_service=watchlist,
        portfolio_service=portfolio,
    )


def test_buy_command_registers_new_purchase(
    service: ChatCommandService,
    portfolio: PortfolioService,
    transactions: TransactionHistoryService,
) -> None:
    result = service.handle("買付,8136,100,3775", now=_NOW)
    assert result.success is True
    assert "8136" in result.reply_text
    saved = transactions.list_transactions("8136")
    assert len(saved) == 1
    assert saved[0].transaction_type == TransactionType.BUY
    assert saved[0].shares == 100
    assert saved[0].execution_price == Decimal("3775")

    holding = portfolio.get_holding("8136")
    assert holding is not None
    assert holding.shares == 100
    assert holding.average_purchase_price == Decimal("3775")


def test_buy_command_detects_additional_purchase(
    service: ChatCommandService,
    portfolio: PortfolioService,
    transactions: TransactionHistoryService,
) -> None:
    portfolio.register_purchase(
        stock_code="8136",
        stock_name="サンリオ",
        shares=100,
        purchase_price=Decimal("1000"),
        purchase_date=dt.date(2026, 1, 1),
        account_type=AccountType.NISA,
    )
    result = service.handle("買付,8136,50,1200", now=_NOW)
    assert result.success is True
    saved = transactions.list_transactions("8136")
    assert saved[0].transaction_type == TransactionType.ADDITIONAL_BUY

    holding = portfolio.get_holding("8136")
    assert holding is not None
    assert holding.shares == 150


def test_sell_command_detects_partial_sell(
    service: ChatCommandService,
    portfolio: PortfolioService,
    transactions: TransactionHistoryService,
) -> None:
    portfolio.register_purchase(
        stock_code="8136",
        stock_name="サンリオ",
        shares=100,
        purchase_price=Decimal("1000"),
        purchase_date=dt.date(2026, 1, 1),
        account_type=AccountType.NISA,
    )
    result = service.handle("売却,8136,30,1500", now=_NOW)
    assert result.success is True
    saved = transactions.list_transactions("8136")
    assert saved[0].transaction_type == TransactionType.PARTIAL_SELL

    holding = portfolio.get_holding("8136")
    assert holding is not None
    assert holding.shares == 70


def test_case_n_sell_command_updates_last_sale_date(
    service: ChatCommandService,
    portfolio: PortfolioService,
) -> None:
    """LINEテキストコマンド(「売却」、chat_command_service.py経由。CLI
    typerコマンド(cli/transactions.py sell-executed)はTransactionHistory
    Serviceのみを呼び出しPortfolioServiceを一切呼ばないため対象外)で
    一部売却が成立した場合、holding.last_sale_dateが売却日に更新される
    ことを確認する(再コードレビュー対応2026-08、指摘3 Case N)。
    last_purchase_dateが不変であること(Case O)もあわせて確認する。
    """
    portfolio.register_purchase(
        stock_code="8136",
        stock_name="サンリオ",
        shares=100,
        purchase_price=Decimal("1000"),
        purchase_date=dt.date(2026, 1, 1),
        account_type=AccountType.NISA,
    )
    holding_before = portfolio.get_holding("8136")
    assert holding_before is not None
    assert holding_before.last_sale_date is None
    assert holding_before.last_purchase_date == dt.date(2026, 1, 1)

    result = service.handle("売却,8136,30,1500", now=_NOW)
    assert result.success is True

    holding_after = portfolio.get_holding("8136")
    assert holding_after is not None
    assert holding_after.shares == 70
    # _NOW = 2026-07-24 00:00 UTC → JST 09:00、暦日は2026-07-24。
    assert holding_after.last_sale_date == dt.date(2026, 7, 24)
    assert holding_after.last_purchase_date == dt.date(2026, 1, 1)  # 不変(Case O)


def test_sell_command_detects_full_sell(
    service: ChatCommandService,
    portfolio: PortfolioService,
    transactions: TransactionHistoryService,
) -> None:
    portfolio.register_purchase(
        stock_code="8136",
        stock_name="サンリオ",
        shares=100,
        purchase_price=Decimal("1000"),
        purchase_date=dt.date(2026, 1, 1),
        account_type=AccountType.NISA,
    )
    result = service.handle("売却,8136,100,1500", now=_NOW)
    assert result.success is True
    saved = transactions.list_transactions("8136")
    assert saved[0].transaction_type == TransactionType.FULL_SELL

    assert portfolio.get_holding("8136") is None


def test_sell_command_rejects_unheld_stock(service: ChatCommandService) -> None:
    result = service.handle("売却,8136,100,1500", now=_NOW)
    assert result.success is False
    assert "保有銘柄として登録されていません" in result.reply_text


def test_sell_command_rejects_oversell(
    service: ChatCommandService,
    portfolio: PortfolioService,
    transactions: TransactionHistoryService,
) -> None:
    portfolio.register_purchase(
        stock_code="8136",
        stock_name="サンリオ",
        shares=100,
        purchase_price=Decimal("1000"),
        purchase_date=dt.date(2026, 1, 1),
        account_type=AccountType.NISA,
    )
    result = service.handle("売却,8136,150,1500", now=_NOW)
    assert result.success is False
    assert "保有株数" in result.reply_text
    assert transactions.list_transactions("8136") == []
    holding = portfolio.get_holding("8136")
    assert holding is not None
    assert holding.shares == 100


def test_watch_command_adds_to_watchlist(
    service: ChatCommandService, watchlist: WatchlistService
) -> None:
    result = service.handle("ウォッチ,7203", now=_NOW)
    assert result.success is True
    assert watchlist.get_item("7203") is not None


def test_unknown_command_returns_help_text(service: ChatCommandService) -> None:
    result = service.handle("よくわからないメッセージ", now=_NOW)
    assert result.success is False
    assert "CSV形式" in result.reply_text


def test_empty_text_returns_help_text(service: ChatCommandService) -> None:
    result = service.handle("", now=_NOW)
    assert result.success is False


def test_buy_command_wrong_field_count_returns_error(service: ChatCommandService) -> None:
    result = service.handle("買付,8136,100", now=_NOW)
    assert result.success is False
    assert "買付" in result.reply_text


def test_buy_command_non_numeric_shares_returns_error(service: ChatCommandService) -> None:
    result = service.handle("買付,8136,abc,3775", now=_NOW)
    assert result.success is False
    assert "株数" in result.reply_text


def test_buy_command_non_positive_price_returns_error(service: ChatCommandService) -> None:
    result = service.handle("買付,8136,100,-1", now=_NOW)
    assert result.success is False
    assert "単価" in result.reply_text


def test_watch_command_wrong_field_count_returns_error(service: ChatCommandService) -> None:
    result = service.handle("ウォッチ", now=_NOW)
    assert result.success is False


def test_buy_command_uses_jst_calendar_date_across_utc_midnight(
    service: ChatCommandService,
    portfolio: PortfolioService,
    transactions: TransactionHistoryService,
) -> None:
    # UTC 2026-07-23 20:00 = JST 2026-07-24 05:00。素朴なnow.date()だと
    # 前日(2026-07-23)になってしまう境界(明治HD事例と同種のバグ)。
    now_near_utc_midnight = dt.datetime(2026, 7, 23, 20, 0, tzinfo=dt.UTC)
    result = service.handle("買付,8136,100,3775", now=now_near_utc_midnight)
    assert result.success is True

    saved = transactions.list_transactions("8136")
    assert saved[0].execution_date == dt.date(2026, 7, 24)

    holding = portfolio.get_holding("8136")
    assert holding is not None
    assert holding.first_purchase_date == dt.date(2026, 7, 24)


def test_sell_command_uses_jst_calendar_date_across_utc_midnight(
    service: ChatCommandService,
    portfolio: PortfolioService,
    transactions: TransactionHistoryService,
) -> None:
    portfolio.register_purchase(
        stock_code="8136",
        stock_name="サンリオ",
        shares=100,
        purchase_price=Decimal("1000"),
        purchase_date=dt.date(2026, 1, 1),
        account_type=AccountType.NISA,
    )
    now_near_utc_midnight = dt.datetime(2026, 7, 23, 20, 0, tzinfo=dt.UTC)
    result = service.handle("売却,8136,30,1500", now=now_near_utc_midnight)
    assert result.success is True

    saved = transactions.list_transactions("8136")
    assert saved[0].execution_date == dt.date(2026, 7, 24)


# --- is_legacy_command: LineEventRouter向け(LINEボタン起点会話型UI・実装プランv2 5節) ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("買付,8306,100,2500", True),
        ("売却,8306,100,2500", True),
        ("ウォッチ,8306", True),
        ("8306,100,2500", False),  # 操作語のない断片(ConversationState中の入力と同型)
        ("よくわからない", False),
        ("", False),
    ],
)
def test_is_legacy_command(service: ChatCommandService, text: str, expected: bool) -> None:
    assert service.is_legacy_command(text) is expected


def test_is_legacy_command_matches_handle_dispatch_decision(service: ChatCommandService) -> None:
    """is_legacy_command()がTrueを返す入力は、handle()が実際にコマンドとして
    処理する(ヘルプへ落ちない)ことを保証する(判定基準の重複実装防止)。"""
    result = service.handle("ウォッチ,8306")
    assert service.is_legacy_command("ウォッチ,8306") is True
    assert result.success is True
