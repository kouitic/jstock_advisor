"""Issue #64 B-2(F-A6): CLI一覧(holdings/transactions)でownerを識別できるように
する。

Holding.owner/Transaction.ownerは既にエンティティ側で保持されているが、
`holdings list` / `transactions list` の出力では表示しておらず、同一銘柄を
複数ownerが保有する場合に一覧上で区別できなかった。owner表示のみを追加し、
一覧の絞り込み・service層のシグネチャ変更は行わない。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jstock_advisor.cli import holdings as holdings_cli
from jstock_advisor.cli import transactions as transactions_cli
from jstock_advisor.domain.entities.enums import AccountType, TransactionType
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.infrastructure.local_repository.transaction_repository import (
    TransactionRepository,
)
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.transaction_history_service import (
    TransactionHistoryService,
)


@pytest.fixture
def portfolio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PortfolioService:
    """holdings CLIが生成するPortfolioServiceをtmp storeへ向ける。"""
    service = PortfolioService(
        holding_repository=HoldingRepository(store_dir=tmp_path),
        lot_repository=PurchaseLotRepository(store_dir=tmp_path),
    )
    monkeypatch.setattr(holdings_cli, "PortfolioService", lambda: service)
    return service


@pytest.fixture
def transaction_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TransactionHistoryService:
    """transactions CLIが生成するTransactionHistoryServiceをtmp storeへ向ける。"""
    service = TransactionHistoryService(
        transaction_repository=TransactionRepository(store_dir=tmp_path)
    )
    monkeypatch.setattr(transactions_cli, "TransactionHistoryService", lambda: service)
    return service


def test_holdings_list_shows_owner_for_each_row(portfolio: PortfolioService) -> None:
    """同一銘柄を複数ownerが保有していても、一覧でowner別に区別できる。"""
    portfolio.register_purchase(
        owner="所有者A",
        stock_code="9999",
        stock_name="テスト株式会社",
        shares=100,
        purchase_price=Decimal("1000"),
        purchase_date=dt.date(2026, 1, 1),
        account_type=AccountType.GENERAL,
    )
    portfolio.register_purchase(
        owner="所有者B",
        stock_code="9999",
        stock_name="テスト株式会社",
        shares=200,
        purchase_price=Decimal("1200"),
        purchase_date=dt.date(2026, 1, 2),
        account_type=AccountType.NISA,
    )

    result = CliRunner().invoke(holdings_cli.app, ["list"])

    assert result.exit_code == 0, result.output
    assert "owner:所有者A" in result.output
    assert "owner:所有者B" in result.output


def test_transactions_list_shows_owner_for_each_row(
    transaction_service: TransactionHistoryService,
) -> None:
    """同一銘柄への複数ownerの取引が、一覧でowner別に区別できる。"""
    transaction_service.record_execution(
        owner="所有者A",
        stock_code="9999",
        transaction_type=TransactionType.BUY,
        shares=100,
        execution_price=Decimal("1000"),
        execution_date=dt.date(2026, 1, 1),
    )
    transaction_service.record_execution(
        owner="所有者B",
        stock_code="9999",
        transaction_type=TransactionType.BUY,
        shares=200,
        execution_price=Decimal("1200"),
        execution_date=dt.date(2026, 1, 2),
    )

    result = CliRunner().invoke(transactions_cli.app, ["list"])

    assert result.exit_code == 0, result.output
    assert "owner:所有者A" in result.output
    assert "owner:所有者B" in result.output


def test_transactions_list_shows_placeholder_for_legacy_none_owner(
    transaction_service: TransactionHistoryService,
) -> None:
    """owner概念導入前のバックフィル未了レコード(owner=None)でも一覧が落ちず、
    プレースホルダーで表示されること。
    """
    transaction = transaction_service.record_execution(
        owner="所有者A",
        stock_code="9999",
        transaction_type=TransactionType.BUY,
        shares=100,
        execution_price=Decimal("1000"),
        execution_date=dt.date(2026, 1, 1),
    )
    legacy = transaction.model_copy(update={"owner": None})
    transaction_service._transactions.save(legacy)  # type: ignore[attr-defined]

    result = CliRunner().invoke(transactions_cli.app, ["list"])

    assert result.exit_code == 0, result.output
    assert "owner:-" in result.output
