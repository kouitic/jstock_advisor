"""判断の安全条件(G1〜G4)のshadow監査記録を集計する、**読み取り専用**のCLI(Issue #458 / #160 PR-4)。

`jstock judgment-safety-shadow report`は、shadow監査記録(`decision_type=judgment_safety_shadow`)の
集計と、`AuditLogTable`の増加量・scanメトリクスを出す。集計ロジックは持たず、入力の選択・整形・
終了コードだけを担う(集計は`services/judgment_safety_shadow_report.py`)。

* **既定はローカル(`--source local`)。Productionを既定で読まない。** `--source dynamodb`のときは、
  read-onlyであること・対象テーブル・資格情報が呼び出し元の環境(AWS_PROFILE等)であることを標準
  エラーへ明示する。
* 書き込み・保存・削除・invoke・通知のいずれにも到達しない(allowlistのproxy + テストで固定)。
* 閾値の判定・専用Tableへの移行の提案はしない(判断はUSER/MANAGER。U13 = OPTION_C)。

終了コード: 0 = 正常(記録が0件でも0。ただし0件を明示表示する)/ 2 = 引数不正 / 3 = 読み取り失敗。
"""

from __future__ import annotations

import datetime as dt
import enum
import json
from typing import Any

import typer

from jstock_advisor.domain.jst import to_jst
from jstock_advisor.infrastructure.aws.audit_shadow_reader import (
    DEFAULT_TABLE_NAME,
    ReadOnlyViolationError,
    build_read_only_client,
    describe_table_metrics,
    scan_shadow_records,
)
from jstock_advisor.infrastructure.local_repository.audit_log_repository import (
    AuditLogRepository,
)
from jstock_advisor.services.judgment_safety_shadow_report import (
    Baseline,
    ScanSnapshot,
    TableSnapshot,
    Window,
    build_shadow_result_metrics,
    build_storage_read_metrics,
)

app = typer.Typer(help="判断の安全条件(G1〜G4)のshadow監査記録の集計(読み取り専用)")

SHADOW_DECISION_TYPE = "judgment_safety_shadow"
DEFAULT_BASELINE_RECORDS = 78_700
DEFAULT_BASELINE_SIZE_BYTES = 153_000_000
DEFAULT_BASELINE_DATE = "2026-09-20"
EXIT_READ_FAILURE = 3


class Source(enum.StrEnum):
    LOCAL = "local"
    DYNAMODB = "dynamodb"


def _today_jst() -> dt.date:
    return to_jst(dt.datetime.now(dt.UTC)).date()


def _parse_date(value: str | None, option: str) -> dt.date | None:
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{option}はYYYY-MM-DD形式で指定してください: {value}") from exc


def _render_text(value: Any, indent: int = 0) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, dict | list) and child:
                lines.append(f"{pad}{key}:")
                lines.extend(_render_text(child, indent + 1))
            else:
                lines.append(f"{pad}{key}: {child if child != [] and child != {} else '(なし)'}")
    elif isinstance(value, list):
        for child in value:
            if isinstance(child, dict | list):
                lines.append(f"{pad}-")
                lines.extend(_render_text(child, indent + 1))
            else:
                lines.append(f"{pad}- {child}")
    else:
        lines.append(f"{pad}{value}")
    return lines


def _emit(report: dict[str, Any], as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return
    for section, body in report.items():
        typer.echo(f"[{section}]")
        for line in _render_text(body, 1):
            typer.echo(line)
        typer.echo("")


@app.command("report")
def report(
    source: Source = typer.Option(
        Source.LOCAL, "--source", help="local(既定。ローカルJSON)/ dynamodb(Production。read-only)"
    ),
    table: str = typer.Option(DEFAULT_TABLE_NAME, "--table", help="dynamodb時の対象テーブル"),
    date_from: str | None = typer.Option(None, "--from", help="JST暦日(YYYY-MM-DD)。既定は全期間"),
    date_to: str | None = typer.Option(None, "--to", help="JST暦日(YYYY-MM-DD)。既定は全期間"),
    baseline_records: int = typer.Option(DEFAULT_BASELINE_RECORDS, "--baseline-records"),
    baseline_size_bytes: int = typer.Option(DEFAULT_BASELINE_SIZE_BYTES, "--baseline-size-bytes"),
    baseline_date: str = typer.Option(DEFAULT_BASELINE_DATE, "--baseline-date"),
    describe_only: bool = typer.Option(
        False, "--describe-only", help="表のメトリクスだけ(scanしない)。dynamodbのみ"
    ),
    metrics_only: bool = typer.Option(
        False, "--metrics-only", help="表・scanのメトリクスだけ(shadowの集計は出さない)"
    ),
    json_output: bool = typer.Option(False, "--json", help="機械可読(JSON)で出力する"),
) -> None:
    """shadow監査記録を集計し、表の増加量・scanメトリクスを出す(読み取り専用)。"""
    window = Window(_parse_date(date_from, "--from"), _parse_date(date_to, "--to"))
    baseline_day = _parse_date(baseline_date, "--baseline-date")
    if baseline_day is None:  # 既定値があるため到達しないが、型を絞る
        raise typer.BadParameter("--baseline-dateが不正です")
    if describe_only and source is not Source.DYNAMODB:
        raise typer.BadParameter("--describe-onlyは--source dynamodbのときだけ使えます")
    baseline = Baseline(baseline_records, baseline_size_bytes, baseline_day)
    today = _today_jst()
    output: dict[str, Any] = {}

    if source is Source.LOCAL:
        entries = AuditLogRepository().list_by_decision_type(SHADOW_DECISION_TYPE)
        if not metrics_only:
            output["SHADOW_RESULT_METRICS"] = build_shadow_result_metrics(
                entries, window, unparsed=0
            )
        output["STORAGE_READ_METRICS"] = {
            "shadow_matched": len(entries),
            "note": "ローカルJSONの読み取り。表・scanのメトリクスは--source dynamodbのときだけ出る",
        }
        _emit(output, json_output)
        return

    typer.echo(
        f"[read-only] scan / describe_table のみを使います。対象テーブル: {table}。"
        "資格情報は呼び出し元の環境(AWS_PROFILE等)です。書き込みは行いません。",
        err=True,
    )
    try:
        client = build_read_only_client()
        described = describe_table_metrics(client, table)
        table_snapshot = TableSnapshot(described.item_count, described.table_size_bytes)
        scan_snapshot: ScanSnapshot | None = None
        entries = []
        unparsed = 0
        if not describe_only:
            result = scan_shadow_records(client, table)
            entries = result.shadow_entries
            unparsed = result.unparsed
            m = result.scan_metrics
            scan_snapshot = ScanSnapshot(
                read_pages=m.read_pages,
                scanned_count=m.scanned_count,
                count=m.count,
                total_rru=m.total_rru,
                elapsed_time_sec=m.elapsed_time_sec,
                item_bytes=m.item_bytes,
                shadow_item_bytes=result.shadow_item_bytes,
                decision_type_counts=result.decision_type_counts,
                all_records_by_jst_date=result.all_records_by_jst_date,
            )
    except ReadOnlyViolationError:
        raise
    except Exception as exc:  # noqa: BLE001 - 認証・ネットワーク等。原因の型だけを出す
        typer.echo(f"読み取りに失敗しました: {type(exc).__name__}", err=True)
        raise typer.Exit(EXIT_READ_FAILURE) from exc

    shadow_metrics: dict[str, Any] | None = None
    if not describe_only:
        shadow_metrics = build_shadow_result_metrics(entries, window, unparsed=unparsed)
        if not metrics_only:
            output["SHADOW_RESULT_METRICS"] = shadow_metrics
    output["STORAGE_READ_METRICS"] = build_storage_read_metrics(
        scan_snapshot,
        table_snapshot,
        baseline,
        today,
        shadow_matched=len(entries),
        shadow_records_per_day=(shadow_metrics or {}).get("records_per_day", {}),
    )
    _emit(output, json_output)
