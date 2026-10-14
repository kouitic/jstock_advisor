"""購入側 Shadow の観測の集計を表示する読取専用の CLI(`jstock allocation summary`。#934 BT-A PR-2)。

期間(営業日)を指定して、日ごとの記録を GetItem だけで読み、`allocation_shadow_summary`(PR-1。純粋)
で集計して、10 営業日の観測レポート(数える日・数えない理由・PARTIAL の欠落・版ごとの指標・
改善候補・測れないもの)を出す。再計算・補完・書込・LINE 送信をしない。

* ★ 読取元は必ず明示する(`--backend local|dynamodb`。既定なし)★。入口の検証は
  `jstock allocation today` と同じ関数(`select_backend`)を使う(検証を複製しない): 環境変数からは
  何も選ばない・Lambda 環境では local を拒否・dynamodb は region と表名の完全一致が揃わなければ
  1 件も読まない
* ★ 読むのは期待される営業日だけ ★(BusinessCalendar。休日を読まない・『0 件』と混同しない)。
  上限は営業日 60 日(超えたら 1 件も読まずに拒否)
* ★ 読取は `allocation_shadow_reader.read_days` ★(日付のループ。1 日分は `today` と同じ
  `read_day`)。1 日の失敗は他の日を止めない(失敗した日は『数えない日』に理由つきで出る)
* ★ owner 別の表は `--by-owner` のときだけ(公開面へ貼らない)★。符号の全文・金額・銘柄は出さない

終了コード: 0 = 目標の日数に達した / 1 = 読取の失敗が 1 日以上 / 2 = 引数不正 /
3 = 読めたが目標に未達(★ 0 は『目標に達した』であり、『記録が揃っている』ではない ★)。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Final

import typer

from jstock_advisor.cli.allocation import (
    Backend,
    OutputFormat,
    _build_source,
    _default_calendar,
    _today_jst,
    resolve_date,
    select_backend,
)
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.notification.allocation_shadow_cli_view import build_screen
from jstock_advisor.domain.notification.allocation_shadow_summary import (
    TARGET_COUNTED_DAYS,
    DayRecord,
    Measure,
    ShadowSummary,
    TenDayReport,
    build_ten_day_report,
    rate,
    report_to_dict,
    summarize,
)
from jstock_advisor.infrastructure.aws.allocation_shadow_reader import read_days

MAX_BUSINESS_DAYS: Final = 60  # 1 回の読取の上限(GetItem は 1 日あたり約 3〜5 回)
MAX_TARGET_DAYS: Final = 60
EXIT_READ_FAILURE: Final = 1
EXIT_NOT_REACHED: Final = 3


def expected_business_days(
    start: dt.date, end: dt.date, calendar: BusinessCalendar
) -> list[dt.date]:
    """start〜end(両端を含む)の営業日。休日は含めない。"""
    days: list[dt.date] = []
    day = start
    while day <= end:
        if calendar.is_business_day(day):
            days.append(day)
        day += dt.timedelta(days=1)
    return days


def _percent(value: Any) -> str:
    return f"{value * 100:.1f}%"


def _measure_line(measure: Measure) -> str:
    if measure.denominator <= 0:
        return f"  {measure.title}: 測れない(母数 0 = {measure.unit})"
    head = f"  {measure.title}: 母数 {measure.denominator} {measure.unit}"
    if measure.count is not None:
        value = rate(measure)
        shown = _percent(value) if value is not None else "測れない"
        head += f" / {measure.count} 件 = {shown}"
    if measure.buckets:
        head += " / " + ", ".join(f"{key}={number}" for key, number in measure.buckets)
    return head


def render_table(report: TenDayReport, summary: ShadowSummary, *, by_owner: bool) -> str:
    """レポート → 表(テキスト)。件数・割合だけ。銘柄・金額・符号の全文を出さない。"""
    lines: list[str] = []
    first = report.verdicts[0].business_date if report.verdicts else "-"
    last = report.verdicts[-1].business_date if report.verdicts else "-"
    lines.append(
        f"購入側 Shadow の観測: {first} 〜 {last}(期待される営業日 {report.expected_days_total} 日)"
    )
    status = "目標に到達" if report.reached else "目標に未到達"
    lines.append(f"数える日: {report.counted_days} / {report.target_days}({status})")
    lines.append(
        f"Day 1(最初に数えた日): {report.first_counted_date or 'なし'} / "
        f"10 日目: {report.tenth_counted_date or 'まだ'} / "
        f"見込み(以後の期待される営業日が全て数える日になる場合の仮定): "
        f"{report.projected_tenth_date or 'なし'}"
    )
    lines.append(f"数える日の規則の出所: {report.rule_source}")
    if report.not_counted_counts:
        reasons = ", ".join(f"{k}={v}" for k, v in report.not_counted_counts)
        lines.append(f"数えない日(理由別): {reasons} / 全 {report.expected_days_total} 日")
    else:
        lines.append("数えない日: なし")
    lines.append("")
    lines.append("日別(日付 状態 数える 理由 | 計算/見送り/失敗/その他 | 欠落/母数):")
    for v in report.verdicts:
        missing = (
            f"{v.missing_count}/{v.attempted}"
            if v.missing_count is not None and v.attempted is not None
            else "-"
        )
        lines.append(
            f"  {v.business_date} {v.state.value if v.state else '-'} "
            f"{'○' if v.counted else '×'} {v.not_counted.value if v.not_counted else '-'} | "
            f"{v.computed_owners}/{v.skipped_owners}/{v.failed_owners}/{v.other_owners} | {missing}"
            + (f" 読取失敗={v.read_failure}" if v.read_failure else "")
        )
    if report.partial_days:
        lines.append("PARTIAL の日(欠落件数 / 母数 = 試みた owner 数):")
        for v in report.partial_days:
            lines.append(f"  {v.business_date}: 欠落 {v.missing_count} / {v.attempted}")
    lines.append("")
    if report.periods_count == 0:
        lines.append("版ごとの指標: 測れない(記録が読めた owner のいる日が 0 日)")
    for period in summary.periods:
        version = period.version
        lines.append(
            f"版ごとの指標: {period.first_date} 〜 {period.last_date}({period.day_count} 日。"
            f"model={version.model_version} policy={version.policy_id} "
            f"contract={version.contract_version} "
            f"mode={version.shadow_mode})※ 版をまたいだ合算はしない"
        )
        lines.extend(_measure_line(m) for m in period.measures)
    lines.append("")
    lines.append("改善候補一覧(事実の欄のみ。解釈の欄は人が書く = 初案・未確定):")
    for number, c in enumerate(report.candidates, 1):
        lines.append(f"  {number}. [{c.logic}] {c.summary}")
        lines.append(f"       事実: {c.facts} / 頻度・範囲: {c.frequency}")
        lines.append(f"       関連: {', '.join(c.related_issues)} / {c.interpretation}")
    lines.append("")
    lines.append("測れないもの(0 や『問題なし』ではない):")
    for n in report.not_measurable:
        lines.append(f"  成果物 {n.deliverable}: {n.title} — {n.reason}({n.classification.value})")
    if by_owner:
        lines.append("")
        lines.append("owner 別(★ 公開面へ貼らない。手元で読むだけ ★):")
        for t in summary.by_owner:
            kinds = ", ".join(f"{k}={v}" for k, v in t.kinds)
            lines.append(f"  {t.display_ref}: {t.days_present} 日 / {kinds}")
    return "\n".join(lines) + "\n"


def _parse_date(value: str, option: str, today: dt.date) -> dt.date:
    try:
        return resolve_date(value, today, None)
    except typer.BadParameter as error:
        raise typer.BadParameter(error.message, param_hint=option) from None


def summary_command(
    backend: Backend = typer.Option(
        ..., "--backend", help="local(開発・試験用のローカル)| dynamodb(Production。明示指定)"
    ),
    date_from: str = typer.Option(..., "--from", help="開始日(JST。YYYY-MM-DD)"),
    date_to: str = typer.Option(..., "--to", help="終了日(JST。YYYY-MM-DD。未来日付は不可)"),
    output_format: OutputFormat = typer.Option(OutputFormat.TABLE, "--format", help="table | json"),
    target_days: int = typer.Option(
        TARGET_COUNTED_DAYS, "--target-days", help="目標の『数える日』の日数(既定 10)"
    ),
    by_owner: bool = typer.Option(
        False,
        "--by-owner",
        help="owner 別の表を含める(★ 公開面へ貼らない。手元で読むだけ ★)",
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
    """期間の資金配分 Shadow の観測を集計して表示する(読取専用。投資判断には使わない)。"""
    selection = select_backend(backend, aws_region, confirm_table, table_prefix, aws_profile)
    today = _today_jst()
    start = _parse_date(date_from, "--from", today)
    end = _parse_date(date_to, "--to", today)
    if start > end:
        raise typer.BadParameter("--from は --to 以前にしてください", param_hint="--from")
    if not 1 <= target_days <= MAX_TARGET_DAYS:
        raise typer.BadParameter(
            f"--target-days は 1〜{MAX_TARGET_DAYS} の範囲で指定してください",
            param_hint="--target-days",
        )
    days = expected_business_days(start, end, _default_calendar())
    if not days:
        raise typer.BadParameter(
            "指定の期間に営業日がありません(休日だけの期間は読みません)", param_hint="--from"
        )
    if len(days) > MAX_BUSINESS_DAYS:
        raise typer.BadParameter(
            f"営業日が {len(days)} 日あります。1 回の読取は {MAX_BUSINESS_DAYS} 日までです"
            "(1 件も読まずに拒否しました)",
            param_hint="--to",
        )
    typer.echo(f"backend={selection.backend.value}", err=True)
    if selection.backend is Backend.DYNAMODB:
        typer.echo(
            f"aws_region={selection.region} table={selection.table} read-only days={len(days)}",
            err=True,
        )
    reads = read_days(_build_source(selection), days)
    records = [
        DayRecord(r.business_date.isoformat(), build_screen(r.day, {}), None)
        if r.day is not None
        else DayRecord(r.business_date.isoformat(), None, r.failure.value if r.failure else "ERROR")
        for r in reads
    ]
    summary = summarize(records)
    report = build_ten_day_report(summary, [d.isoformat() for d in days], target_days=target_days)
    failed = sum(1 for r in records if r.screen is None)
    if failed:
        typer.echo(
            f"読取に失敗した日が {failed} 日あります(数えない日として出力に含めました)", err=True
        )
    if output_format is OutputFormat.JSON:
        payload = report_to_dict(report, summary, include_by_owner=by_owner)
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        typer.echo(render_table(report, summary, by_owner=by_owner), nl=False)
    if failed:
        raise typer.Exit(EXIT_READ_FAILURE)
    raise typer.Exit(0 if report.reached else EXIT_NOT_REACHED)


def register(app: typer.Typer) -> None:
    """`jstock allocation summary` を登録する(cli/main.py から 1 回だけ呼ぶ)。"""
    app.command("summary")(summary_command)
