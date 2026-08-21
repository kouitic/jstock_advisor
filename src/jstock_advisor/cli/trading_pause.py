"""売買記録の一時停止フラグCLI(保有銘柄オーナー機能移行時の書込停止用)。

init/set/statusはTradingPauseConfig(専用DynamoDBテーブル)を直接操作し、
再デプロイ不要で反映される。ローカル実行時は既定でローカルJSONストアを操作するが、
`--target aws`を指定すると本番DynamoDBを直接操作する(AWS認証情報が環境に
設定済みであることが前提。cli/holding_decision.pyと同じパターン)。
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator

import typer

from jstock_advisor.infrastructure.aws import trading_pause_config as _repo
from jstock_advisor.services.trading_pause_service import (
    TradingPauseAlreadyInitializedError,
    TradingPauseService,
)

app = typer.Typer(help="売買記録の一時停止フラグ(保有銘柄オーナー機能移行用、要人間操作)")

_AWS_OVERRIDE_ENV_VAR = "AWS_LAMBDA_FUNCTION_NAME"


@contextlib.contextmanager
def _target_backend(target: str) -> Iterator[None]:
    """--target aws指定時、ローカルCLIから本番DynamoDBバックエンドを直接操作する。"""
    if target != "aws":
        yield
        return
    previous = os.environ.get(_AWS_OVERRIDE_ENV_VAR)
    os.environ[_AWS_OVERRIDE_ENV_VAR] = "cli-target-aws-override"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(_AWS_OVERRIDE_ENV_VAR, None)
        else:
            os.environ[_AWS_OVERRIDE_ENV_VAR] = previous


@app.command("init")
def init(
    changed_by: str = typer.Option(..., "--changed-by"),
    reason: str = typer.Option(..., "--reason"),
    paused: bool = typer.Option(
        False, "--paused", help="初期状態でBUY/SELLを停止するか(既定False)"
    ),
    target: str = typer.Option("local", "--target", help="local | aws"),
) -> None:
    """TradingPauseConfigの初回作成(既に存在する場合は失敗する)。"""
    with _target_backend(target):
        service = TradingPauseService()
        try:
            created = service.init_config(
                pause_buy_sell=paused, updated_by=changed_by, change_reason=reason
            )
        except TradingPauseAlreadyInitializedError as e:
            typer.echo(str(e))
            raise typer.Exit(code=1) from e
    typer.echo(
        f"初期化しました: pause_buy_sell={created.pause_buy_sell} "
        f"config_version={created.config_version}"
    )


@app.command("set")
def set_pause(
    buy_sell: bool = typer.Option(
        ..., "--buy-sell", help="true: BUY/SELLを一時停止する / false: 解除する"
    ),
    changed_by: str = typer.Option(..., "--changed-by"),
    reason: str = typer.Option(..., "--reason"),
    target: str = typer.Option("local", "--target", help="local | aws"),
) -> None:
    """BUY/SELLの一時停止フラグを切り替える。再デプロイ不要。WATCH(ウォッチリスト
    登録)には影響しない(HoldingsもPurchaseLotsも変更しない経路のため)。"""
    with _target_backend(target):
        service = TradingPauseService()
        current = service.get_config()
        if current is None:
            typer.echo("TradingPauseConfigが未初期化です。先にinitを実行してください。")
            raise typer.Exit(code=1)
        try:
            updated = service.update_config(
                expected_config_version=current.config_version,
                pause_buy_sell=buy_sell,
                updated_by=changed_by,
                change_reason=reason,
            )
        except _repo.TradingPauseConflictError as e:
            typer.echo(f"他の更新が先に行われたため再確認が必要です: {e}")
            raise typer.Exit(code=1) from e
    typer.echo(
        f"pause_buy_sellを{updated.pause_buy_sell}にしました"
        f"(config_version={updated.config_version})"
    )


@app.command("status")
def status(target: str = typer.Option("local", "--target", help="local | aws")) -> None:
    with _target_backend(target):
        service = TradingPauseService()
        config = service.get_config()
    if config is None:
        typer.echo("TradingPauseConfigは未初期化です(既定: pause_buy_sell=False、通常運用)。")
        return
    typer.echo(
        f"pause_buy_sell={config.pause_buy_sell} config_version={config.config_version} "
        f"updated_by={config.updated_by} updated_at={config.updated_at} "
        f"change_reason={config.change_reason}"
    )
