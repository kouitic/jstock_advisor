"""資金配分 Shadow(試験計算)の結果を、監査ログの記録から読んで表示する**読取専用**の CLI(#605)。

`jstock allocation today` は、購入側 Shadow が書いた記録(日単位の索引・日単位の skip・owner ごとの
項目つきの記録)を **GetItem だけで** 読み、表示するだけ。再計算・補完・書込・LINE 送信・配分の
再実行をしない。

* ★ 読取り元は必ず明示する(`--backend local|dynamodb`。既定なし)。環境変数からは何も選ばない。
  `AWS_LAMBDA_FUNCTION_NAME` が設定されている環境では `--backend local` を拒否する
  (#833・#886 と同じ作法)
* `--backend dynamodb` は `--aws-region` と `--confirm-table`(AuditLog の表名の完全一致)が
  揃わなければ 1 件も読まない。資格情報の選択(`--aws-profile` または環境の AWS_PROFILE 等)は
  backend と独立
* ★ 鍵を持たない・符号を計算しない: 索引が載せた符号で記録を引く。`--owner` は R1 では持たない
* ★ 索引なし・未記録ありの日は『0 件』と表示せず、状態を明示する(終了コードで区別)

終了コード: 0 = 完全 / 1 = 読取の失敗 / 2 = 引数不正 /
3 = 読めたが不完全(索引なし・未記録あり・記録 0 件・対応していない版・解釈できない項目)。
"""

from __future__ import annotations

import datetime as dt
import enum
import os
import re
from dataclasses import dataclass
from typing import Final

import typer

from jstock_advisor.cli.allocation_labels import (
    LabelError,
    default_labels_path,
    load_labels,
    remove_label,
    set_label,
)
from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.jst import to_jst
from jstock_advisor.domain.notification.allocation_shadow_cli_renderer import (
    render_json,
    render_table,
)
from jstock_advisor.domain.notification.allocation_shadow_cli_view import (
    DayInput,
    RecordInput,
    RecordState,
    build_screen,
    day_skip_key,
    index_key,
    items_key,
    refs_to_read,
)
from jstock_advisor.infrastructure.aws.allocation_shadow_reader import (
    DynamoRecordSource,
    ReadFailure,
    ReadFailureKind,
    ReadStatus,
    RecordRead,
    RecordSource,
    build_client,
)
from jstock_advisor.infrastructure.local_repository.audit_log_repository import (
    AuditLogRepository,
)

app = typer.Typer(help="資金配分 Shadow(試験計算)の結果を表示する(読取専用。投資判断には使わない)")
label_app = typer.Typer(help="所有者の符号に付けるローカルの表示名(AWS・鍵に触れない)")
app.add_typer(label_app, name="label")

DEFAULT_TABLE_PREFIX: Final = "jstock"
_DATE_PATTERN: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
EXIT_READ_FAILURE: Final = 1
EXIT_INCOMPLETE: Final = 3

_FAILURE_TEXT: Final = {
    ReadFailureKind.ACCESS_DENIED: "記録を読む権限がありません(AccessDenied)",
    ReadFailureKind.NOT_FOUND_TABLE: "対象の表が見つかりません",
    ReadFailureKind.CONNECTION: "接続に失敗しました",
    ReadFailureKind.ERROR: "記録の読取に失敗しました",
}


class Backend(enum.StrEnum):
    LOCAL = "local"
    DYNAMODB = "dynamodb"


class OutputFormat(enum.StrEnum):
    TABLE = "table"
    JSON = "json"


@dataclass(frozen=True)
class BackendSelection:
    backend: Backend
    region: str | None = None
    prefix: str = DEFAULT_TABLE_PREFIX
    profile: str | None = None

    @property
    def table(self) -> str:
        return f"{self.prefix}-audit_log"


def select_backend(
    backend: Backend,
    region: str | None,
    confirm_table: str | None,
    prefix: str | None,
    profile: str | None,
) -> BackendSelection:
    """backend の選択を検証する。満たさなければ `typer.BadParameter`(読む前に失敗)。"""
    if backend is Backend.LOCAL:
        if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
            raise typer.BadParameter(
                "環境変数 AWS_LAMBDA_FUNCTION_NAME が設定されているため、"
                "--backend local は拒否します"
                "(Lambda の中のつもりで実行している可能性があります)。"
                "Production を対象にするなら --backend dynamodb を明示してください。",
                param_hint="--backend",
            )
        if region or confirm_table or prefix or profile:
            raise typer.BadParameter(
                "--aws-region / --confirm-table / --table-prefix / --aws-profile は"
                " --backend dynamodb のときだけ指定できます",
                param_hint="--backend",
            )
        return BackendSelection(Backend.LOCAL)
    if not region:
        raise typer.BadParameter(
            "--backend dynamodb では --aws-region の明示が必要です(既定はありません)",
            param_hint="--aws-region",
        )
    selection = BackendSelection(Backend.DYNAMODB, region, prefix or DEFAULT_TABLE_PREFIX, profile)
    if not confirm_table:
        raise typer.BadParameter(
            "--backend dynamodb では --confirm-table(対象の AuditLog の表名)の入力が必要です",
            param_hint="--confirm-table",
        )
    if confirm_table != selection.table:
        raise typer.BadParameter(
            "--confirm-table が、対象の AuditLog の表名と一致しません", param_hint="--confirm-table"
        )
    return selection


def _today_jst() -> dt.date:
    return to_jst(dt.datetime.now(dt.UTC)).date()


def _default_calendar() -> BusinessCalendar:
    return BusinessCalendar.from_config(load_config().holiday_calendar)


def resolve_date(value: str | None, today: dt.date, calendar: BusinessCalendar | None) -> dt.date:
    """`--date` を解決する。省略時 = 直近の営業日(JST。今日が営業日なら今日。黙って遡らない)。"""
    if value is not None:
        try:
            if not _DATE_PATTERN.fullmatch(value):
                raise ValueError
            parsed = dt.date.fromisoformat(value)
        except ValueError:
            raise typer.BadParameter(
                "--date は YYYY-MM-DD 形式で指定してください", param_hint="--date"
            ) from None
        if parsed > today:
            raise typer.BadParameter("--date に未来の日付は指定できません", param_hint="--date")
        return parsed
    if calendar is None:
        calendar = _default_calendar()
    day = today
    while not calendar.is_business_day(day):
        day -= dt.timedelta(days=1)
    return day


class LocalRecordSource:
    """ローカルの AuditLog(開発・試験用)から読む。書込の経路を持たない(get だけ)。"""

    def __init__(self, repository: AuditLogRepository | None = None) -> None:
        self._repository = repository or AuditLogRepository()

    def get(self, audit_id: str) -> RecordRead:
        try:
            entry = self._repository.get(audit_id)
        except Exception:  # noqa: BLE001 - 固定の種別へ畳む
            raise ReadFailure(ReadFailureKind.ERROR) from None
        if entry is None:
            return RecordRead(ReadStatus.NOT_FOUND)
        output = entry.output_values
        if not isinstance(output, dict):
            return RecordRead(ReadStatus.UNREADABLE)
        return RecordRead(ReadStatus.FOUND, dict(output))


def _input(read: RecordRead) -> RecordInput:
    return RecordInput(RecordState(read.status.value), read.record)


def read_day(source: RecordSource, business_date: dt.date) -> DayInput:
    """索引 1 + skip 1 + 索引が載せた符号ごとの項目つきの記録(最大 16)を GetItem で読む。"""
    day = business_date.isoformat()
    index = _input(source.get(index_key(day)))
    skip = _input(source.get(day_skip_key(day)))
    items = {ref: _input(source.get(items_key(day, ref))) for ref in refs_to_read(index)}
    return DayInput(day, index, skip, items)


def _build_source(selection: BackendSelection) -> RecordSource:
    if selection.backend is Backend.LOCAL:
        return LocalRecordSource()
    assert selection.region is not None  # select_backend が保証
    client = build_client(region=selection.region, profile=selection.profile)
    return DynamoRecordSource(client, selection.table)


def _load_labels_or_none() -> dict[str, str]:
    try:
        return load_labels()
    except LabelError:
        typer.echo("ラベルのファイルを読めないため、表示名なしで表示します", err=True)
        return {}


@app.command("today")
def today(
    backend: Backend = typer.Option(
        ..., "--backend", help="local(開発・試験用のローカル)| dynamodb(Production。明示指定)"
    ),
    date: str | None = typer.Option(
        None, "--date", help="対象日(JST。YYYY-MM-DD)。省略 = 直近の営業日"
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
        None, "--table-prefix", help=f"表名の prefix(dynamodb のみ。既定 {DEFAULT_TABLE_PREFIX})"
    ),
    aws_profile: str | None = typer.Option(
        None,
        "--aws-profile",
        help="dynamodb のとき使う AWS profile(任意。読取専用の profile で足りる)",
    ),
) -> None:
    """その日の資金配分 Shadow の結果を表示する(読取専用)。表には表示名(ラベル)が含まれます。"""
    selection = select_backend(backend, aws_region, confirm_table, table_prefix, aws_profile)
    business_date = resolve_date(date, _today_jst(), None)
    typer.echo(f"backend={selection.backend.value}", err=True)
    if selection.backend is Backend.DYNAMODB:
        typer.echo(f"aws_region={selection.region} table={selection.table} read-only", err=True)
    try:
        day = read_day(_build_source(selection), business_date)
    except ReadFailure as failure:
        typer.echo(f"読取に失敗しました: {_FAILURE_TEXT[failure.kind]}", err=True)
        raise typer.Exit(EXIT_READ_FAILURE) from None
    labels = _load_labels_or_none()
    screen = build_screen(day, labels)
    if output_format is OutputFormat.JSON:
        typer.echo(render_json(screen, with_labels=with_labels), nl=False)
    else:
        typer.echo(render_table(screen, show_labels=True), nl=False)
    raise typer.Exit(0 if screen.complete else EXIT_INCOMPLETE)


# --- ラベル(ローカルの設定だけ。AWS・鍵・記録に触れない)------------------------------------------


@label_app.command("set")
def label_set(
    ref: str = typer.Option(..., "--ref", help="符号の桁の先頭 6 桁以上(または kv1.N:… の符号)"),
    name: str = typer.Option(..., "--name", help="表示名(40 文字まで)"),
) -> None:
    """符号の先頭に表示名を付ける(利用者の端末のローカルのファイルだけを更新)。"""
    try:
        set_label(ref, name)
    except LabelError as error:
        raise typer.BadParameter(str(error)) from None
    typer.echo("ラベルを保存しました")


@label_app.command("remove")
def label_remove(
    ref: str = typer.Option(..., "--ref", help="符号の桁の先頭 6 桁以上"),
) -> None:
    """付けた表示名を外す。"""
    try:
        removed = remove_label(ref)
    except LabelError as error:
        raise typer.BadParameter(str(error)) from None
    typer.echo("ラベルを外しました" if removed else "該当するラベルはありません")


@label_app.command("list")
def label_list() -> None:
    """付けた表示名の一覧(端末の画面だけ。共有しない)。"""
    try:
        labels = load_labels()
    except LabelError as error:
        typer.echo(f"{error}", err=True)
        raise typer.Exit(EXIT_READ_FAILURE) from None
    typer.echo(f"ファイル: {default_labels_path()}")
    if not labels:
        typer.echo("(ラベルはありません)")
        return
    for prefix, name in sorted(labels.items()):
        typer.echo(f"{prefix}  {name}")
    typer.echo("※ これは符号と表示名の対応表です。共有しないでください")
