"""データ移行コマンド群(保有銘柄オーナー機能移行、2026-08)。

`jstock migrate holdings-owner preflight` と
`jstock migrate holdings-owner run` は別々のコマンドであり、1コマンドで
連続実行はしない(承認済み設計)。`run`は既定でdry-run(書き込みなし)で
あり、実際に書き込むには明示的に`--no-dry-run`を指定する必要がある。
"""

from __future__ import annotations

import typer

from jstock_advisor.migrations.holdings_owner_migration import (
    MigrationAbortedError,
    run_migration,
)
from jstock_advisor.migrations.holdings_owner_preflight import run_preflight
from jstock_advisor.migrations.target import MigrationTarget

app = typer.Typer(help="データ移行コマンド群")
holdings_owner_app = typer.Typer(help="保有銘柄オーナー機能移行(owner/holding_id対応)")
app.add_typer(holdings_owner_app, name="holdings-owner")


@holdings_owner_app.command("preflight")
def preflight(
    target: MigrationTarget = typer.Option(..., "--target", help="local | aws(必須指定)"),
    accept_unresolved: list[str] = typer.Option(
        [],
        "--accept-unresolved",
        help=(
            "参照先Recommendationが存在しないNotificationLogのnotification_idを"
            "明示的に許可する(複数指定可)。指定したIDはowner/holding_id=Noneの"
            "未解決のまま扱う(暗黙に本人扱いにはしない)。"
        ),
    ),
) -> None:
    """migration本体の実行前に必ず単独で実行する検証コマンド。"""
    accepted = frozenset(accept_unresolved)
    report = run_preflight(target, accepted_unresolved_notification_ids=accepted)
    typer.echo(report.render_text())
    if not report.passed:
        raise typer.Exit(code=1)


@holdings_owner_app.command("run")
def run(
    target: MigrationTarget = typer.Option(..., "--target", help="local | aws(必須指定)"),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--no-dry-run",
        help="既定はdry-run(書き込みなし)。実際に書き込むには--no-dry-runを明示指定する。",
    ),
    owner: str = typer.Option("本人", "--owner", help="移行後に設定するowner(既定: 本人)"),
    accept_unresolved: list[str] = typer.Option(
        [],
        "--accept-unresolved",
        help="preflightと同じ--accept-unresolved指定を渡すこと(内部で再度preflightする)。",
    ),
) -> None:
    """migration本体。実行直前にpause_buy_sell==trueであることとpreflight合格を
    このコード自身が再確認する(fail-closed)。preflightコマンドを先に単独で
    実行し、人間が結果を確認してから実行すること。"""
    accepted = frozenset(accept_unresolved)
    try:
        result = run_migration(
            target,
            dry_run=dry_run,
            owner=owner,
            accepted_unresolved_notification_ids=accepted,
        )
    except MigrationAbortedError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e
    typer.echo(result.render_text())
