"""判定精度向上機能Phase A: DecisionSnapshotの成績集計CLIコマンド。"""

from __future__ import annotations

from typing import cast, get_args

import typer

from jstock_advisor.services.decision_performance_service import (
    DecisionPerformanceComparison,
    DecisionPerformanceSegment,
    DecisionPerformanceService,
    ScoreName,
    score_predicate,
)
from jstock_advisor.services.performance_metrics_service import MetricsBucket

app = typer.Typer(help="DecisionSnapshotの成績集計(判定精度向上機能Phase A)")

_VALID_SCORE_NAMES = get_args(ScoreName)


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


def _pct(value: float | None) -> str:
    return f"{value:.1f}%" if value is not None else "-"


def _echo_segment(segment: DecisionPerformanceSegment) -> None:
    typer.echo(
        f"{segment.bucket_key}: {segment.sample_count}件(有効{segment.conclusive_count}件) "
        f"成功率={_pct(segment.success_rate_pct)} "
        f"平均リターン={_pct(segment.average_return_pct)} "
        f"中央値リターン={_pct(segment.median_return_pct)} "
        f"平均超過リターン={_pct(segment.average_excess_return_pct)} "
        f"中央値超過リターン={_pct(segment.median_excess_return_pct)} "
        f"平均MFE={_pct(segment.average_mfe_pct)} 平均MAE={_pct(segment.average_mae_pct)}"
    )


def _ranges_overlap(
    min_a: float | None, max_a: float | None, min_b: float | None, max_b: float | None
) -> bool:
    lo_a = min_a if min_a is not None else float("-inf")
    hi_a = max_a if max_a is not None else float("inf")
    lo_b = min_b if min_b is not None else float("-inf")
    hi_b = max_b if max_b is not None else float("inf")
    return lo_a <= hi_b and lo_b <= hi_a


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
    median = (
        f"{result.median_price_return_pct:.1f}%"
        if result.median_price_return_pct is not None
        else "-"
    )
    mfe = f"{result.avg_mfe_pct:.1f}%" if result.avg_mfe_pct is not None else "-"
    mae = f"{result.avg_mae_pct:.1f}%" if result.avg_mae_pct is not None else "-"
    typer.echo(f"  中央値リターン={median} 平均MFE(最大値幅)={mfe} 平均MAE(最大逆行幅)={mae}")

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


@app.command("segments")
def segments(
    score: str = typer.Option(
        ...,
        "--score",
        help="対象スコア(historical_valuation/timing/earnings_surprise/earnings_trend)",
    ),
    horizon: int = typer.Option(
        ..., "--horizon", help="営業日数(5/20/60/120/250のいずれか、必須)"
    ),
) -> None:
    """1つのShadow Scoreをcategory/confidence/coverage tier/個別model_version別に
    分析する。異なるhorizonのOutcomeを混在させないため--horizonは必須。"""
    service = DecisionPerformanceService()
    try:
        result = service.summarize_score_segments(
            score_name=cast(ScoreName, score), horizon_business_days=horizon
        )
    except ValueError as exc:
        typer.echo(f"エラー: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"スコア={result.score_name} horizon={result.horizon_business_days}営業日")
    typer.echo("--- category別 ---")
    for segment in result.by_category:
        _echo_segment(segment)
    typer.echo("--- confidence別 ---")
    for segment in result.by_confidence:
        _echo_segment(segment)
    typer.echo("--- coverage tier別 ---")
    for segment in result.by_coverage_tier:
        _echo_segment(segment)
    typer.echo("--- model_version別 ---")
    for segment in result.by_model_version:
        _echo_segment(segment)


@app.command("compare")
def compare(
    score: str = typer.Option(..., "--score", help="対象スコア"),
    label_a: str = typer.Option(..., "--label-a", help="比較群Aのラベル"),
    min_a: float = typer.Option(None, "--min-a", help="比較群Aのscore下限(この値以上)"),
    max_a: float = typer.Option(None, "--max-a", help="比較群Aのscore上限(この値以下)"),
    label_b: str = typer.Option(..., "--label-b", help="比較群Bのラベル"),
    min_b: float = typer.Option(None, "--min-b", help="比較群Bのscore下限(この値以上)"),
    max_b: float = typer.Option(None, "--max-b", help="比較群Bのscore上限(この値以下)"),
    horizon: int = typer.Option(
        ..., "--horizon", help="営業日数(5/20/60/120/250のいずれか、必須)"
    ),
) -> None:
    """2つの母集団(スコアの数値レンジで指定)の成績を比較する。--min-a/--max-a/
    --min-b/--max-bで指定した範囲が重複する場合は、比較結果が誤解を招くため
    事前にエラーとする(範囲を分けて指定し直してください)。"""
    if score not in _VALID_SCORE_NAMES:
        typer.echo(
            f"エラー: --scoreは{_VALID_SCORE_NAMES}のいずれかを指定してください", err=True
        )
        raise typer.Exit(code=1)
    if min_a is not None and max_a is not None and min_a > max_a:
        typer.echo(
            f"エラー: 比較群Aの範囲が不正です(--min-a={min_a} > --max-a={max_a})", err=True
        )
        raise typer.Exit(code=1)
    if min_b is not None and max_b is not None and min_b > max_b:
        typer.echo(
            f"エラー: 比較群Bの範囲が不正です(--min-b={min_b} > --max-b={max_b})", err=True
        )
        raise typer.Exit(code=1)
    if _ranges_overlap(min_a, max_a, min_b, max_b):
        typer.echo(
            f"エラー: 比較群A([{min_a}, {max_a}])と比較群B([{min_b}, {max_b}])の"
            "範囲が重複しています。重複しない範囲を指定してください。",
            err=True,
        )
        raise typer.Exit(code=1)

    service = DecisionPerformanceService()
    try:
        typed_score = cast(ScoreName, score)
        result: DecisionPerformanceComparison = service.compare_segments(
            label_a,
            score_predicate(typed_score, minimum=min_a, maximum=max_a),
            label_b,
            score_predicate(typed_score, minimum=min_b, maximum=max_b),
            horizon_business_days=horizon,
        )
    except ValueError as exc:
        typer.echo(f"エラー: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"スコア={score} horizon={result.horizon_business_days}営業日")
    _echo_segment(result.group_a)
    _echo_segment(result.group_b)
    if result.overlap_count:
        typer.echo(f"警告: 両群に同時に該当した件数={result.overlap_count}")
