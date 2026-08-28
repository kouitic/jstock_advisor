"""Calibration Dataset export CLI(Issue #28 Phase B)。

datasetを生成・exportするだけのCLI。分析(score集計・成功率・CI・bootstrap・
threshold提案等)はPhase Cの責務であり、ここへ追加してはならない。
既存3テーブル(Recommendation / EvaluationResult / DecisionSnapshot)は
read-onlyで、日次バッチ・Lambda経路からは呼ばれない(人間のオンデマンド実行専用)。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import typer

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.services.calibration_dataset_service import (
    CalibrationDatasetBuilder,
    SampleDefinition,
    write_export,
)

app = typer.Typer(help="BUY calibration用datasetの生成・export(Issue #28 Phase B)")

_FORMATS = ("jsonl", "csv")
_SAMPLE_DEFINITIONS = {
    "raw": SampleDefinition.RAW,
    "non-overlapping-window": SampleDefinition.NON_OVERLAPPING_WINDOW,
    "action-change": SampleDefinition.ACTION_CHANGE,
}


@app.command("export-dataset")
def export_dataset(
    output: Path = typer.Option(..., "--output", help="出力ファイルパス"),
    export_format: str = typer.Option(
        "jsonl", "--format", help="jsonl(canonical)またはcsv(閲覧用+sidecar meta.json)"
    ),
    sample_definition: str = typer.Option(
        "raw",
        "--sample-definition",
        help="raw(全Recommendation×horizon)/ non-overlapping-window / action-change",
    ),
    selected_only: bool = typer.Option(
        False, "--selected-only", help="sample_selected=trueの行のみexportする"
    ),
    include_pending: bool = typer.Option(
        True,
        "--include-pending/--no-include-pending",
        help="horizon未到来(NOT_YET_EVALUABLE)行を含めるか(既定: 含める)",
    ),
) -> None:
    if export_format not in _FORMATS:
        typer.echo(f"未対応のformatです: {export_format}(jsonl/csvのみ)")
        raise typer.Exit(code=1)
    if sample_definition not in _SAMPLE_DEFINITIONS:
        typer.echo(
            f"未対応のsample definitionです: {sample_definition}"
            f"({'/'.join(sorted(_SAMPLE_DEFINITIONS))}のみ)"
        )
        raise typer.Exit(code=1)

    config = load_config()
    builder = CalibrationDatasetBuilder(
        config=config,
        business_calendar=BusinessCalendar.from_config(config.holiday_calendar),
    )
    dataset = builder.build(
        now=dt.datetime.now(dt.UTC),
        sample_definition=_SAMPLE_DEFINITIONS[sample_definition],
    )
    written = write_export(
        dataset,
        output,
        export_format=export_format,
        selected_only=selected_only,
        include_pending=include_pending,
    )
    typer.echo(
        f"export完了: rows={dataset.metadata['row_count']} "
        f"orphan_evaluations={dataset.diagnostics.orphan_evaluation_count} "
        f"duplicate_evaluation_rows={dataset.diagnostics.duplicate_evaluation_row_count}"
    )
    for path in written:
        typer.echo(f"  -> {path}")


@app.command("analyze")
def analyze_dataset(
    input_path: Path = typer.Option(..., "--input", help="export-datasetが生成したJSONL"),
    output: Path = typer.Option(..., "--output", help="analysis artifact(JSONL)の出力先"),
    bootstrap_iterations: int = typer.Option(500, "--bootstrap-iterations"),
    bootstrap_seed: int = typer.Option(42, "--bootstrap-seed"),
    small_sample_rows: int = typer.Option(30, "--small-sample-rows"),
    small_sample_stocks: int = typer.Option(10, "--small-sample-stocks"),
) -> None:
    """Phase C1: 記述統計のanalysis artifactを生成する(判定・閾値・scoreは
    一切変更しない。good BUY composite・優劣判定・threshold提案は行わない)。"""
    from jstock_advisor.services.calibration_analysis_service import (
        AnalysisParameters,
        analyze,
        parse_dataset_jsonl,
        to_artifact_jsonl,
    )

    dataset = parse_dataset_jsonl(input_path.read_text(encoding="utf-8"))
    params = AnalysisParameters(
        small_sample_warning_rows=small_sample_rows,
        small_sample_warning_distinct_stocks=small_sample_stocks,
        bootstrap_iterations=bootstrap_iterations,
        bootstrap_seed=bootstrap_seed,
    )
    records = analyze(dataset, params)
    output.write_text(to_artifact_jsonl(records), encoding="utf-8", newline="\n")
    typer.echo(
        f"analyze完了: cells={records[0]['cell_count']} "
        f"sample_definition={records[0]['source_dataset']['sample_definition']}"
    )
    typer.echo(f"  -> {output}")
