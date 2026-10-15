"""候補ごとの詳細を表示する読取専用の CLI(`jstock allocation detail`。Issue #605 追加 CLI)。

その日の資金配分 Shadow の記録(`today` と同じ索引・日単位の skip・owner ごとの項目つきの
記録)を GetItem だけで読み、候補ごとの属性・価格・RAER の入力・出力・寄与・配分の判断を表示する。
再計算・補完・書込・LINE 送信をしない。**R1 の観測条件(記録の契約・RAER の式・係数・配分)に
触れない**。
設計: #605 追加 CLI 設計 rev1〜rev3(…6185126921・…6187277235・…6188870271)。

* ★ 新しい読取を増やさない ★: 入口の検証は `today` と同じ `select_backend`・読取は `read_day`
  (最大 18 回の GetItem)を再利用する(検証を複製しない)。`today` の画面・JSON・終了コードは変えない
* ★ 絞り込み(--owner / --stock / --decision / --reason)は表示だけ ★。読取の回数・順序・再計算に
  影響しない。0 件は『該当する候補がありません(絞り込み: …)』と書き、『0 件』とは書かない
* ★ `--date` に営業日でない日を明示したときは、読まずに終了コード 2 ★(GetItem 0 回)。
  `--date` 省略 = 直近の営業日は、この層(CLI)が JST の今日と BusinessCalendar で決める
  (画面モデル・整形の module は時計を持たない)
* ★ `--reason` の語彙は閉じた理由コードの表から導く ★(手で写した一覧を持たない)。未知の語は
  終了コード 2 で、文面に使える語の一覧を出す
* ★ 表示項目(外部仕様)の確認が済むまで出さない項目(△)は、モデルにあっても画面へ出さない ★
  (`INCLUDE_OPTIONAL`)

終了コード: `today` と同じ(0 = 揃っている / 1 = 読取の失敗 / 2 = 指定の誤り / 3 = 読めたが不完全)。
"""

from __future__ import annotations

import re
from typing import Final

import typer

from jstock_advisor.cli.allocation import (
    _FAILURE_TEXT,
    EXIT_INCOMPLETE,
    EXIT_READ_FAILURE,
    Backend,
    OutputFormat,
    _build_source,
    _default_calendar,
    _load_labels_or_none,
    _today_jst,
    resolve_date,
    select_backend,
)
from jstock_advisor.domain.notification.allocation_shadow_detail_renderer import (
    render_detail_json,
    render_detail_table,
)
from jstock_advisor.domain.notification.allocation_shadow_detail_view import (
    DECISION_FILTERS,
    REASON_FILTER_CODES,
    DetailFilter,
    apply_filter,
    build_detail_screen,
)
from jstock_advisor.infrastructure.aws.allocation_shadow_reader import ReadFailure, read_day

#: ★ 表示項目(△)の確認が済むまで False ★(保有評価額・方針の値・経過時間と読取の状態)
INCLUDE_OPTIONAL: Final = False

_OWNER_PREFIX: Final = re.compile(r"[0-9a-f]{6,24}")
_STOCK_CODE: Final = re.compile(r"[0-9A-Za-z]{1,16}")


def _validated_filter(
    owner: str | None, stock: str | None, decision: str, reason: str | None
) -> DetailFilter:
    description: list[str] = []
    if owner is not None:
        if _OWNER_PREFIX.fullmatch(owner.lower()) is None:
            raise typer.BadParameter(
                "--owner は符号の桁の先頭 6〜24 桁(16 進数)で指定してください",
                param_hint="--owner",
            )
        description.append(f"所有者 {owner.lower()}…")
    if stock is not None:
        if _STOCK_CODE.fullmatch(stock) is None:
            raise typer.BadParameter(
                "--stock は英数字 16 文字までの銘柄コードで指定してください", param_hint="--stock"
            )
        description.append(f"銘柄 {stock}")
    if decision not in DECISION_FILTERS:
        raise typer.BadParameter(
            f"--decision は次のいずれか: {', '.join(DECISION_FILTERS)}", param_hint="--decision"
        )
    if DECISION_FILTERS[decision] is not None:
        description.append(f"判断 {decision}")
    if reason is not None:
        if reason not in REASON_FILTER_CODES:
            raise typer.BadParameter(
                f"--reason は次のいずれか: {', '.join(REASON_FILTER_CODES)}",
                param_hint="--reason",
            )
        description.append(f"理由 {reason}")
    return DetailFilter(
        owner_prefix=owner.lower() if owner is not None else None,
        stock_code=stock,
        decision=DECISION_FILTERS[decision],
        reason=reason,
        description=tuple(description),
    )


def detail_command(
    backend: Backend = typer.Option(
        ..., "--backend", help="local(開発・試験用のローカル)| dynamodb(Production。明示指定)"
    ),
    date: str | None = typer.Option(
        None, "--date", help="対象日(JST。YYYY-MM-DD。営業日)。省略 = 直近の営業日"
    ),
    owner: str | None = typer.Option(
        None, "--owner", help="所有者の符号の桁の先頭 6〜24 桁(表示だけ絞り込む)"
    ),
    stock: str | None = typer.Option(None, "--stock", help="銘柄コード(表示だけ絞り込む)"),
    decision: str = typer.Option(
        "all", "--decision", help="all | allocated | not-allocated(表示だけ絞り込む)"
    ),
    reason: str | None = typer.Option(
        None, "--reason", help="理由コード(表示だけ絞り込む。例: BELOW_HURDLE)"
    ),
    output_format: OutputFormat = typer.Option(OutputFormat.TABLE, "--format", help="table | json"),
    with_labels: bool = typer.Option(
        False,
        "--with-labels",
        help="JSON に表示名を含める(既定は含めない。共有されうる出力に実名を載せないため)",
    ),
    aws_region: str | None = typer.Option(
        None, "--aws-region", help="--backend dynamodb のとき必須(既定なし)"
    ),
    confirm_table: str | None = typer.Option(
        None, "--confirm-table", help="--backend dynamodb のとき必須。AuditLog の表名(完全一致)"
    ),
    table_prefix: str | None = typer.Option(
        None, "--table-prefix", help="表名の prefix(dynamodb のみ。既定 jstock)"
    ),
    aws_profile: str | None = typer.Option(
        None,
        "--aws-profile",
        help="dynamodb のとき使う AWS profile(任意。読取専用の profile で足りる)",
    ),
) -> None:
    """候補ごとの詳細(RAER の入力・出力・寄与・配分の判断)を表示する(読取専用)。"""
    selection = select_backend(backend, aws_region, confirm_table, table_prefix, aws_profile)
    flt = _validated_filter(owner, stock, decision, reason)
    calendar = _default_calendar()
    business_date = resolve_date(date, _today_jst(), calendar)
    if date is not None and not calendar.is_business_day(business_date):
        raise typer.BadParameter(
            "指定の日は営業日ではありません(営業日ではない日は読みません)", param_hint="--date"
        )
    typer.echo(f"backend={selection.backend.value}", err=True)
    if selection.backend is Backend.DYNAMODB:
        typer.echo(f"aws_region={selection.region} table={selection.table} read-only", err=True)
    try:
        day = read_day(_build_source(selection), business_date)
    except ReadFailure as failure:
        typer.echo(f"読取に失敗しました: {_FAILURE_TEXT[failure.kind]}", err=True)
        raise typer.Exit(EXIT_READ_FAILURE) from None
    labels = _load_labels_or_none()
    detail = apply_filter(build_detail_screen(day, labels), flt)
    if output_format is OutputFormat.JSON:
        typer.echo(
            render_detail_json(detail, with_labels=with_labels, include_optional=INCLUDE_OPTIONAL),
            nl=False,
        )
    else:
        typer.echo(
            render_detail_table(detail, show_labels=True, include_optional=INCLUDE_OPTIONAL),
            nl=False,
        )
    raise typer.Exit(0 if detail.screen.complete else EXIT_INCOMPLETE)


def register(app: typer.Typer) -> None:
    """`jstock allocation detail` を登録する(cli/main.py から 1 回だけ呼ぶ)。"""
    app.command("detail")(detail_command)
