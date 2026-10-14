"""買付余力(available_cash)の参照・棚卸し更新CLIコマンド(Issue #594、#128 A5c)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import typer

from jstock_advisor.infrastructure.collection_store import running_on_lambda
from jstock_advisor.infrastructure.external_value_parser import ExternalValueParser
from jstock_advisor.services.available_cash_service import (
    AvailableCashReconciliationExhaustedError,
    AvailableCashService,
)

app = typer.Typer(help="買付余力(available_cash)の参照・棚卸し更新")

_AWS_OVERRIDE_ENV_VAR = "AWS_LAMBDA_FUNCTION_NAME"


def _reject_when_aws_env_is_set(command: str) -> None:
    """show / reconcile を、AWS_LAMBDA_FUNCTION_NAME が設定された環境で実行させない(Issue #923)。

    保存先は `running_on_lambda()`(この環境変数が空でない値で設定されているか)だけで決まり、
    設定されていて AWS 資格情報が有効だと、これらのコマンドが作る `AvailableCashService()` は
    Production の DynamoDB(AvailableCashTable)を読み書きする。運用手順書 3.5 節は『本コマンドは
    常にローカル』と述べるため、『ローカルのつもりで Production に触れる』経路を入口で断つ。
    holding-decision(#886)・weekly-aggregate(#833)の拒否と同じ型で、環境変数からは何も選ばず、
    設定されていたら拒否する方向にだけ使う。

    **各コマンドの最初に呼ぶこと**。ストアの構築・設定の読込・読取・書込の前に拒否する
    (読取・書込・ネットワークの呼出を 0 件にするため)。show も拒否する(設定されていると暗黙に
    Production を読むため)。空文字は未設定として扱う(`running_on_lambda()` と同じ定義)。
    Production の買付余力の登録・確認は、LINE の『余力管理』メニュー(#592)を使う。
    """
    if running_on_lambda():
        raise typer.BadParameter(
            f"環境変数 {_AWS_OVERRIDE_ENV_VAR} が設定されているため、available-cash {command} は"
            "拒否します(この設定のもとでは、ローカルのつもりでも Production の DynamoDB の"
            "買付余力を読み書きする可能性があります)。"
            "ローカルで実行するなら、この環境変数を外してください。"
            "Production の買付余力の登録・確認は、LINE の『余力管理』メニューで行ってください",
            param_hint=_AWS_OVERRIDE_ENV_VAR,
        )


def _parse_decimal(value: str, field_name: str) -> Decimal:
    parsed = ExternalValueParser.decimal(value)
    if parsed is None:
        raise typer.BadParameter(f"{field_name}は数値で指定してください")
    return parsed


@app.command("show")
def show_available_cash(
    owner: str = typer.Option(..., "--owner", help="所有者"),
) -> None:
    """owner単位の現在の買付余力を表示する(未登録と0円を区別する)。"""
    _reject_when_aws_env_is_set("show")
    service = AvailableCashService()
    try:
        record = service.get(owner)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e

    if record is None:
        typer.echo(f"所有者{owner}の買付余力は未登録です。")
        return

    reconciled = (
        record.last_reconciled_at.isoformat()
        if record.last_reconciled_at is not None
        else "未棚卸し"
    )
    typer.echo(
        f"owner:{record.owner}\t買付余力:{record.available_cash}円\t"
        f"更新種別:{record.last_update_type.value}\t"
        f"最終更新:{record.updated_at.isoformat()}\t最終棚卸し:{reconciled}"
    )


@app.command("reconcile")
def reconcile_available_cash(
    owner: str = typer.Option(..., "--owner", help="所有者"),
    amount: str = typer.Option(..., "--amount", help="実額と照合した現在の買付余力(円、0以上)"),
) -> None:
    """証券会社等の実額と照合し、買付余力を明示的に上書きする(USER_RECONCILIATION)。

    過去の理論値との差額イベントは作らない(入力値をそのまま正とする。#589と同じ契約)。
    """
    _reject_when_aws_env_is_set("reconcile")
    parsed_amount = _parse_decimal(amount, "買付余力")
    service = AvailableCashService()
    now = dt.datetime.now(dt.UTC)
    try:
        record = service.reconcile(owner, parsed_amount, now)
    except (ValueError, AvailableCashReconciliationExhaustedError) as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e

    typer.echo(f"棚卸ししました: owner:{record.owner}\t買付余力:{record.available_cash}円")
