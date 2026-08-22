"""cli/migrate.py(保有銘柄オーナー機能移行CLI)のテスト。

--targetの厳密化(trading-pause CLIと同じ設計)と、preflight/run
コマンドの終了コード・書き込み制御を確認する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jstock_advisor.cli import migrate as cli_module
from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.infrastructure.aws import trading_pause_config
from jstock_advisor.infrastructure.collection_store import build_collection_store
from jstock_advisor.migrations.legacy_shapes import LegacyHoldingV1, LegacyPurchaseLotV1
from jstock_advisor.migrations.v2_entities import HoldingV2

_runner = CliRunner()
_NOW = dt.datetime(2026, 8, 22, 0, 0, tzinfo=dt.UTC)


def _seed_holding_and_lot(store_dir: Path) -> None:
    build_collection_store(LegacyPurchaseLotV1, "purchase_lots.json", "lot_id", store_dir).upsert(
        LegacyPurchaseLotV1(
            lot_id="lot-1",
            stock_code="8306",
            purchase_date=dt.date(2026, 1, 1),
            shares=100,
            purchase_price=Decimal("1500"),
            account_type=AccountType.GENERAL,
        )
    )
    build_collection_store(LegacyHoldingV1, "holdings.json", "stock_code", store_dir).upsert(
        LegacyHoldingV1(
            stock_code="8306",
            stock_name="三菱UFJ",
            shares=100,
            average_purchase_price=Decimal("1500"),
            total_purchase_amount=Decimal("150000"),
            first_purchase_date=dt.date(2026, 1, 1),
            last_purchase_date=dt.date(2026, 1, 1),
            account_type=AccountType.GENERAL,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )


@pytest.fixture(autouse=True)
def _redirect_local_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from jstock_advisor.infrastructure.local_repository import json_store

    monkeypatch.setattr(json_store, "DEFAULT_STORE_DIR", tmp_path)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    return tmp_path


def test_preflight_target_typo_exits_nonzero() -> None:
    result = _runner.invoke(
        cli_module.app, ["holdings-owner", "preflight", "--target", "awss"]
    )
    assert result.exit_code != 0


def test_preflight_target_missing_exits_nonzero() -> None:
    result = _runner.invoke(cli_module.app, ["holdings-owner", "preflight"])
    assert result.exit_code != 0


def test_preflight_passes_on_clean_local_data(_redirect_local_store: Path) -> None:
    _seed_holding_and_lot(_redirect_local_store)
    result = _runner.invoke(
        cli_module.app, ["holdings-owner", "preflight", "--target", "local"]
    )
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output


def test_run_defaults_to_dry_run_and_writes_nothing(_redirect_local_store: Path) -> None:
    _seed_holding_and_lot(_redirect_local_store)
    trading_pause_config.init(
        pause_buy_sell=True,
        updated_by="tester",
        change_reason="test",
        store_dir=_redirect_local_store,
    )

    result = _runner.invoke(cli_module.app, ["holdings-owner", "run", "--target", "local"])

    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    v2_store = build_collection_store(
        HoldingV2, "holdings_v2.json", "holding_id", _redirect_local_store
    )
    assert v2_store.list_all() == []


def test_run_no_dry_run_writes_when_paused(_redirect_local_store: Path) -> None:
    _seed_holding_and_lot(_redirect_local_store)
    trading_pause_config.init(
        pause_buy_sell=True,
        updated_by="tester",
        change_reason="test",
        store_dir=_redirect_local_store,
    )

    result = _runner.invoke(
        cli_module.app, ["holdings-owner", "run", "--target", "local", "--no-dry-run"]
    )

    assert result.exit_code == 0, result.output
    v2_store = build_collection_store(
        HoldingV2, "holdings_v2.json", "holding_id", _redirect_local_store
    )
    assert v2_store.get("本人#8306") is not None


def test_run_exits_nonzero_when_not_paused(_redirect_local_store: Path) -> None:
    _seed_holding_and_lot(_redirect_local_store)
    trading_pause_config.init(
        pause_buy_sell=False,
        updated_by="tester",
        change_reason="test",
        store_dir=_redirect_local_store,
    )

    result = _runner.invoke(
        cli_module.app, ["holdings-owner", "run", "--target", "local", "--no-dry-run"]
    )

    assert result.exit_code != 0
    assert "pause_buy_sell" in result.output
