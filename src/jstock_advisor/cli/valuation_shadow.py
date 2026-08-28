"""valuation shadow分析export CLI(Issue #20 Phase C)。

保存済みRecommendation(ローカルストア)から、valuation集約仮説のraw shadow
observation(canonical JSONL)と記述統計summary(CSV)を書き出すだけのCLI。
本番判定・DynamoDB・LINEへは一切接続しない(人間のオンデマンド実行専用。
本番Recommendationのread-only分析は別途承認のうえ実施する)。
成功率・最良仮説の決定・ランキング・閾値提案はこのCLIの責務外
(performance結合は#28 calibration datasetとのrecommendation_id joinによる
後段分析)。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import typer

from jstock_advisor.analysis.valuation_shadow_analysis import write_shadow_export
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)

app = typer.Typer(help="valuation集約仮説のshadow分析export(Issue #20 Phase C)")


@app.command("export")
def export(
    output: Path = typer.Option(..., "--output", help="canonical JSONLの出力ファイルパス"),
    summary: Path | None = typer.Option(
        None, "--summary", help="記述統計summary CSVの出力ファイルパス(任意)"
    ),
) -> None:
    repository = RecommendationRepository()
    recommendations = repository.list_all()
    result = write_shadow_export(
        recommendations,
        output,
        generated_at=dt.datetime.now(dt.UTC),
        summary_path=summary,
    )
    typer.echo(
        f"export完了: recommendations={result.recommendation_count} "
        f"rows={result.row_count} "
        f"unavailable_shadow_rows={result.unavailable_shadow_row_count} "
        f"unavailable_contexts={result.unavailable_context_count} "
        f"reconstruction_mismatch={result.reconstruction_mismatch_count}"
    )
    typer.echo(f"  -> {output}")
    if summary is not None:
        typer.echo(f"  -> {summary}")
