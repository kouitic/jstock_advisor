"""売買記録の一時停止フラグCLI(保有銘柄オーナー機能移行時の書込停止用)。

init/set/statusはTradingPauseConfig(専用DynamoDBテーブル)を直接操作し、
再デプロイ不要で反映される。`--target`(local | aws)は全コマンドで必須指定
とし、既定値は持たせない(コードレビュー対応: 本番のつもりでの操作忘れ・
タイプミスによる誤ったバックエンド操作を防ぐため)。`--target aws`を指定
すると本番DynamoDBを直接操作する(AWS認証情報が環境に設定済みであることが
前提。cli/holding_decision.pyと同じパターンだが、targetの型はCliTarget
Enumへ厳密化している点が異なる)。
"""

from __future__ import annotations

import contextlib
import enum
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


class CliTarget(enum.StrEnum):
    """--targetの許容値を"local"/"aws"の2値のみへ厳密に限定する。

    以前はtarget: strの自由文字列で「target != "aws" は全てlocal」という
    判定だったため、"--target awss"のようなタイプミスが本番操作のつもりで
    ローカル操作として黙って成功してしまい、本番を一時停止したと誤認した
    ままmigrationへ進む重大事故につながりかねなかった(コードレビュー対応)。
    TyperのEnum検証により、この2値以外は起動時点で非ゼロ終了しコマンドは
    一切実行されない。また全コマンドで--targetを必須(既定値なし)とし、
    「指定を忘れたらlocalへ静かにフォールバックする」という事故も防ぐ。
    """

    LOCAL = "local"
    AWS = "aws"


@contextlib.contextmanager
def _target_backend(target: CliTarget) -> Iterator[None]:
    """--target aws指定時、ローカルCLIから本番DynamoDBバックエンドを直接操作する。"""
    if target is not CliTarget.AWS:
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
    target: CliTarget = typer.Option(..., "--target", help="local | aws(必須指定)"),
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
        ...,
        "--buy-sell/--no-buy-sell",
        help="--buy-sell: BUY/SELLを一時停止する / --no-buy-sell: 解除する",
    ),
    changed_by: str = typer.Option(..., "--changed-by"),
    reason: str = typer.Option(..., "--reason"),
    target: CliTarget = typer.Option(..., "--target", help="local | aws(必須指定)"),
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
def status(
    target: CliTarget = typer.Option(..., "--target", help="local | aws(必須指定)"),
) -> None:
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
