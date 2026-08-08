"""判定精度向上機能Phase A: DecisionSnapshotの成績集計CLIコマンド。"""

from __future__ import annotations

import typer

from jstock_advisor.services.decision_performance_service import DecisionPerformanceService
from jstock_advisor.services.performance_metrics_service import MetricsBucket

app = typer.Typer(help="DecisionSnapshotの成績集計(判定精度向上機能Phase A)")


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
    """DecisionSnapshotの成績サマリを表示する。

    Phase A時点ではDecisionSnapshotのスコア項目が全てNoneのため、意味のある
    内訳は限定的(スコアリングロジックが揃うPhase B以降で本格的に活用される)。
    """
    service = DecisionPerformanceService()
    result = service.summarize(horizon_business_days=horizon)

    _echo_bucket("全体", result.overall)

    if result.by_decision_type:
        typer.echo("--- パイプラインごと ---")
        for bucket in result.by_decision_type:
            _echo_bucket(bucket.key, bucket)

    if result.by_existing_action:
        typer.echo("--- 既存判定種別ごと ---")
        for bucket in result.by_existing_action:
            _echo_bucket(bucket.key, bucket)

    if result.by_model_version:
        typer.echo("--- model_versionごと ---")
        for bucket in result.by_model_version:
            _echo_bucket(bucket.key, bucket)
