"""推奨の成績集計CLIコマンド(要求仕様37〜40節)。"""

from __future__ import annotations

import typer

from jstock_advisor.services.performance_metrics_service import (
    MetricsBucket,
    PerformanceMetricsService,
)

app = typer.Typer(help="推奨の成績集計(成功率・平均リターン等)")


def _echo_bucket(label: str, bucket: MetricsBucket) -> None:
    rate = f"{bucket.success_rate_pct:.1f}%" if bucket.success_rate_pct is not None else "-"
    avg_return = (
        f"{bucket.avg_price_return_pct:.1f}%" if bucket.avg_price_return_pct is not None else "-"
    )
    avg_excess = (
        f"{bucket.avg_excess_return_pct:.1f}%" if bucket.avg_excess_return_pct is not None else "-"
    )
    typer.echo(
        f"{label}: {bucket.count}件 成功率={rate} "
        f"平均リターン={avg_return} 平均超過リターン={avg_excess}"
    )
    if bucket.label_counts:
        typer.echo(f"  ラベル内訳: {bucket.label_counts}")


@app.command("summary")
def summary(
    horizon: int = typer.Option(
        None, "--horizon", help="営業日数で絞り込む(省略時は全ホライズン合算)"
    ),
) -> None:
    """成績サマリを表示する。"""
    service = PerformanceMetricsService()
    result = service.summarize(horizon_business_days=horizon)

    _echo_bucket("全体", result.overall)

    if result.by_recommendation_type:
        typer.echo("--- 推奨種別ごと ---")
        for bucket in result.by_recommendation_type:
            _echo_bucket(bucket.key, bucket)

    if result.by_confidence:
        typer.echo("--- 信頼度ごと ---")
        for bucket in result.by_confidence:
            _echo_bucket(bucket.key, bucket)

    if result.by_rule_version:
        typer.echo("--- ルールバージョンごと ---")
        for bucket in result.by_rule_version:
            _echo_bucket(bucket.key, bucket)
