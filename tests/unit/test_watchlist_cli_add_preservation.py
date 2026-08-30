"""Issue #58 Phase B1: CLI `watchlist add` が既存itemを破壊しないこと。

`--priority` / `--notify` / `--benefit-interest` は**既定値と同じ値を明示指定できる**
ため、値だけでは「利用者が指定した」のか「オプションのdefaultが入っただけ」なのかを
区別できない。従来はdefaultをそのまま `add_item()` へ渡していたため、
既存銘柄に対して `jstock watchlist add 9999` と打つだけで
priority が MEDIUM、notify_enabled が True へ戻っていた。

本ファイルは、Clickのparameter sourceにより
COMMANDLINE 指定だけがpatchへ入ることを固定する。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jstock_advisor.cli import watchlist as cli_module
from jstock_advisor.domain.entities.enums import Priority, WatchlistRegistrationSource
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
)
from jstock_advisor.services.watchlist_service import WatchlistService

_NOW = dt.datetime(2026, 8, 30, tzinfo=dt.UTC)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WatchlistRepository:
    """CLIが生成する WatchlistService をtmp storeへ向ける。"""
    repository = WatchlistRepository(store_dir=tmp_path)
    monkeypatch.setattr(
        cli_module, "WatchlistService", lambda: WatchlistService(repository)
    )
    repository.upsert(
        WatchlistItem(
            stock_code="9999",
            stock_name="テスト株式会社",
            memo="決算後に再確認",
            priority=Priority.HIGH,
            notify_enabled=False,
            benefit_interest=True,
            registration_source=WatchlistRegistrationSource.AUTO_SCREENING,
            registration_policy="multi_style_monitoring",
            registration_batch_id="batch-orig",
            consecutive_not_qualified_count=2,
            removal_candidate_since=_NOW - dt.timedelta(days=40),
            created_at=_NOW - dt.timedelta(days=120),
            updated_at=_NOW - dt.timedelta(days=7),
        )
    )
    return repository


def _invoke(args: list[str]) -> None:
    result = CliRunner().invoke(cli_module.app, args)
    assert result.exit_code == 0, result.output


def test_cli_add_existing_item_preserves_system_state(repo: WatchlistRepository) -> None:
    """T13: 既存のAUTO_SCREENING銘柄を再登録してもsystem stateを壊さない。"""
    _invoke(["add", "9999"])

    item = repo.get("9999")
    assert item is not None
    assert item.registration_source is WatchlistRegistrationSource.AUTO_SCREENING
    assert item.registration_policy == "multi_style_monitoring"
    assert item.registration_batch_id == "batch-orig"
    assert item.consecutive_not_qualified_count == 2
    assert item.removal_candidate_since == _NOW - dt.timedelta(days=40)


def test_cli_add_without_options_preserves_user_fields(repo: WatchlistRepository) -> None:
    """T14: 未指定のオプションはdefaultであって「明示指定」ではない。"""
    before = repo.get("9999")
    assert before is not None

    _invoke(["add", "9999"])

    item = repo.get("9999")
    assert item is not None
    assert item.priority is Priority.HIGH, "オプション未指定でMEDIUMへ戻ってはならない"
    assert item.notify_enabled is False, "オプション未指定でTrueへ戻ってはならない"
    assert item.benefit_interest is True
    assert item.memo == "決算後に再確認"
    # 実質的な変更が無いため updated_at も進めない
    assert item.updated_at == before.updated_at


def test_cli_add_explicit_option_updates_only_that_field(
    repo: WatchlistRepository,
) -> None:
    """T15: 明示指定した項目だけを更新する。"""
    _invoke(["add", "9999", "--priority", "LOW"])

    item = repo.get("9999")
    assert item is not None
    assert item.priority is Priority.LOW
    assert item.notify_enabled is False, "指定していない項目は変わらない"
    assert item.memo == "決算後に再確認"
    assert item.registration_source is WatchlistRegistrationSource.AUTO_SCREENING


def test_cli_add_explicit_default_valued_option_is_applied(
    repo: WatchlistRepository,
) -> None:
    """defaultと同じ値でも、明示指定されたなら更新として扱う。

    `--notify` は default(True)と同じ値だが、利用者が明示している以上
    「通知を有効にしたい」という指定である。
    """
    _invoke(["add", "9999", "--notify"])

    item = repo.get("9999")
    assert item is not None
    assert item.notify_enabled is True


def test_cli_add_new_item_creates_with_defaults(repo: WatchlistRepository) -> None:
    """T16: 未登録銘柄は従来どおり作成できる。"""
    _invoke(["add", "7203", "--name", "トヨタ自動車"])

    item = repo.get("7203")
    assert item is not None
    assert item.stock_name == "トヨタ自動車"
    assert item.priority is Priority.MEDIUM
    assert item.notify_enabled is True
    assert item.registration_source is WatchlistRegistrationSource.MANUAL
