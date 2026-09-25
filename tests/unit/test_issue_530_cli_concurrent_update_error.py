"""Issue #530 サブちゃんレビューF4: `ConcurrentUpdateError`(`ValueError`派生。
`write_plan.py`のdocstring参照)の目的は、CLIで生tracebackを見せず、
`except ValueError`の既存パターンで日本語の友好的なメッセージへ変換すること
にある。

`holdings add`・`holdings recompute-all`・`watchlist add`の3コマンドは
基盤サービス呼び出しに`try/except ValueError`が無く、この目的が未達成だった
(基底クラスをRuntimeErrorへ変えてもテストが落ちない、という指摘)。
本ファイルはこの3コマンドを直接固定する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from typer.testing import CliRunner

from jstock_advisor.cli import holdings as holdings_cli_module
from jstock_advisor.cli import watchlist as watchlist_cli_module
from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.services.write_plan import ConcurrentUpdateError

_runner = CliRunner()


class _RaisingWatchlistService:
    def add_item(self, stock_code: str, patch: dict[str, Any] | None = None) -> None:
        raise ConcurrentUpdateError(stock_code)


class _RaisingPortfolioServiceForAdd:
    def register_purchase(self, **kwargs: Any) -> None:
        raise ConcurrentUpdateError(kwargs["stock_code"])


class _RaisingPortfolioServiceForRecomputeAll:
    def __init__(self, holding: Holding) -> None:
        self._holding = holding

    def list_holdings(self) -> list[Holding]:
        return [self._holding]

    def recompute_holding(self, owner: str, stock_code: str) -> Holding:
        raise ConcurrentUpdateError(f"{owner}#{stock_code}")


def test_cli_watchlist_add_handles_concurrent_update_error(monkeypatch: Any) -> None:
    """`watchlist add`は生例外を伝播させず、友好的なメッセージで終了する。"""
    monkeypatch.setattr(watchlist_cli_module, "WatchlistService", _RaisingWatchlistService)

    result = _runner.invoke(watchlist_cli_module.app, ["add", "9999"])

    assert result.exit_code == 1
    # typer.Exit(code=1)経由であればresult.exceptionはSystemExitになる。
    # ConcurrentUpdateErrorが生のまま伝播していれば、ここがConcurrentUpdateError
    # インスタンスになる(捕捉できていないことの直接証拠)。
    assert isinstance(result.exception, SystemExit)
    assert "9999" in result.output


def test_cli_holdings_add_handles_concurrent_update_error(monkeypatch: Any) -> None:
    """`holdings add`は生例外を伝播させず、友好的なメッセージで終了する。"""
    monkeypatch.setattr(holdings_cli_module, "PortfolioService", _RaisingPortfolioServiceForAdd)

    result = _runner.invoke(
        holdings_cli_module.app,
        ["add", "9999", "--shares", "100", "--price", "1500"],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "9999" in result.output


def test_cli_holdings_recompute_all_skips_conflicting_holding_and_continues(
    monkeypatch: Any,
) -> None:
    """`holdings recompute-all`は1件が競合してもtracebackを見せず、
    スキップした旨を表示して処理自体は継続する(バッチ処理のため全体を
    止めない設計とした)。"""
    holding_id = build_holding_id(DEFAULT_OWNER, "9999")
    holding = Holding(
        owner=DEFAULT_OWNER,
        holding_id=holding_id,
        stock_code="9999",
        stock_name="テスト",
        shares=100,
        average_purchase_price=Decimal("1500"),
        total_purchase_amount=Decimal("150000"),
        first_purchase_date=dt.date(2026, 1, 1),
        last_purchase_date=dt.date(2026, 1, 1),
        account_type=AccountType.GENERAL,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        updated_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
    )
    monkeypatch.setattr(
        holdings_cli_module,
        "PortfolioService",
        lambda **kwargs: _RaisingPortfolioServiceForRecomputeAll(holding),
    )

    result = _runner.invoke(holdings_cli_module.app, ["recompute-all"])

    assert result.exit_code == 0
    assert result.exception is None
    assert "スキップしました: 9999" in result.output
