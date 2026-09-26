"""Issue #619(#128 A3-CLI): `jstock transactions register-buy`/`register-sell`の
CLI層。既存の`TradeRegistrationService`を通じてのみ操作する薄いCLI層で
あることを確認する(CLI層自体が独自の業務ロジックを持たない)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jstock_advisor.cli import transactions as transactions_cli
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER
from jstock_advisor.infrastructure.local_repository.available_cash_repository import (
    AvailableCashRepository,
)
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.infrastructure.local_repository.transaction_repository import (
    TransactionRepository,
)
from jstock_advisor.services.available_cash_service import AvailableCashService
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.trade_registration_service import TradeRegistrationService
from jstock_advisor.services.transaction_history_service import TransactionHistoryService

runner = CliRunner()
_NOW = dt.datetime(2026, 9, 26, 9, 0, tzinfo=dt.UTC)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ac_repo = AvailableCashRepository(store_dir=tmp_path)
    lot_repo = PurchaseLotRepository(store_dir=tmp_path)
    holding_repo = HoldingRepository(store_dir=tmp_path)
    tx_repo = TransactionRepository(store_dir=tmp_path)
    ac_service = AvailableCashService(repository=ac_repo)
    portfolio = PortfolioService(lot_repository=lot_repo, holding_repository=holding_repo)
    tx_history = TransactionHistoryService(transaction_repository=tx_repo)
    service = TradeRegistrationService(
        portfolio_service=portfolio,
        transaction_history_service=tx_history,
        available_cash_service=ac_service,
        transaction_repository=tx_repo,
        lot_repository=lot_repo,
        holding_repository=holding_repo,
        available_cash_repository=ac_repo,
    )
    monkeypatch.setattr(transactions_cli, "TradeRegistrationService", lambda: service)
    monkeypatch.setattr(transactions_cli, "AvailableCashService", lambda: ac_service)
    return {"ac_service": ac_service, "ac_repo": ac_repo, "holding_repo": holding_repo}


def test_register_buy_success_shows_holding_and_cash(env) -> None:
    env["ac_service"].reconcile(DEFAULT_OWNER, Decimal("1000000"), _NOW)

    result = runner.invoke(
        transactions_cli.app, ["register-buy", "8306", "100", "1500", "--idempotency-key", "k1"]
    )

    assert result.exit_code == 0
    assert "記録しました" in result.stdout
    assert "100" in result.stdout
    assert "850,000円" in result.stdout


def test_register_buy_retry_same_key_reports_already_registered(env) -> None:
    env["ac_service"].reconcile(DEFAULT_OWNER, Decimal("1000000"), _NOW)
    runner.invoke(
        transactions_cli.app, ["register-buy", "8306", "100", "1500", "--idempotency-key", "k1"]
    )

    retry = runner.invoke(
        transactions_cli.app, ["register-buy", "8306", "100", "1500", "--idempotency-key", "k1"]
    )

    assert retry.exit_code == 0
    assert "既に登録済み" in retry.stdout
    assert env["ac_repo"].get(DEFAULT_OWNER).available_cash == Decimal("850000")


def test_register_buy_unregistered_owner_exits_nonzero(env) -> None:
    result = runner.invoke(
        transactions_cli.app, ["register-buy", "8306", "100", "1500", "--idempotency-key", "k1"]
    )

    assert result.exit_code == 1
    assert "未登録" in result.stdout


def test_register_buy_insufficient_cash_exits_nonzero(env) -> None:
    env["ac_service"].reconcile(DEFAULT_OWNER, Decimal("1000"), _NOW)

    result = runner.invoke(
        transactions_cli.app, ["register-buy", "8306", "100", "1500", "--idempotency-key", "k1"]
    )

    assert result.exit_code == 1
    assert "上回っています" in result.stdout or "超えています" in result.stdout


def test_register_sell_success_shows_cash_credit(env) -> None:
    env["ac_service"].reconcile(DEFAULT_OWNER, Decimal("1000000"), _NOW)
    runner.invoke(
        transactions_cli.app, ["register-buy", "8306", "100", "1000", "--idempotency-key", "buy1"]
    )

    result = runner.invoke(
        transactions_cli.app,
        ["register-sell", "8306", "100", "1800", "--idempotency-key", "sell1"],
    )

    assert result.exit_code == 0
    assert "全部売却済み" in result.stdout
    assert "1,080,000円" in result.stdout


def test_register_buy_without_idempotency_key_generates_one_each_call(env) -> None:
    """--idempotency-key省略時は毎回新規登録(D2)。"""
    env["ac_service"].reconcile(DEFAULT_OWNER, Decimal("1000000"), _NOW)

    first = runner.invoke(transactions_cli.app, ["register-buy", "8306", "100", "1000"])
    second = runner.invoke(transactions_cli.app, ["register-buy", "8306", "100", "1000"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert env["holding_repo"].get_raw_data is not None
    assert env["ac_repo"].get(DEFAULT_OWNER).available_cash == Decimal("800000")  # 2回分減算
