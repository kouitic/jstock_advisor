"""Calibration Analysis(Issue #28 Phase C1)。

Phase Bが生成したcalibration dataset(JSONL)を入力に、記述統計の
analysis artifact(JSONL)を決定的に生成する。**BUY/SELL判定・閾値・
margin・score・EvaluationLabelは一切変更しない**。

Phase C1の範囲と原則:
- 記述統計のみ(good BUY composite・best cohort ranking・threshold提案・
  仮説選択・multiple-comparison winner selectionは行わない)
- RAW datasetはdescriptive・coverage・到達率・分布観察に使用可。ただし
  RAW行を独立標本と仮定した信頼区間・有意差判定は出さない
  (SAMPLE_DEPENDENCY_WARNINGをmetadataへ明示)
- NON_OVERLAPPING_WINDOW等は「同一銘柄内のoverlapping horizonによる
  pseudo-replicationを軽減する」sample定義であり、独立性を保証しない
  (market common factor・sector相関・serial correlation・銘柄内の
  非重複window間依存が残る。REMAINING_DEPENDENCY_NOTE参照)
- primary horizonは固定しない(全horizonを独立表示。60営業日は
  future primary candidateとしてmetadataに記載するのみ)
- 小標本セルは数値を隠さずSMALL_SAMPLE_WARNINGを付与(閾値は分析
  パラメータとしてmetadataへ保存)
- 既存EvaluationLabelはlegacy_reference_label(歴史的rule-basedラベル)
  として参考集計のみ(用途が異なる別物として扱い、誤りとは扱わない)
- market regime cohortはbenchmark将来リターンによるex-post層別であり、
  予測特徴ではない(REGIME_LOOK_AHEAD_WARNING)
- return_basis=PRICE_ONLY(配当・優待・税・手数料を含まない)の定型注記を
  常に出力する

決定性: 同一dataset+同一analysis parameters+同一seedからartifactが
バイト単位で一致する(実行時刻等のruntime値をartifactへ含めない)。
"""

from __future__ import annotations

import json
import random
import statistics
from dataclasses import dataclass, field
from typing import Any

ANALYSIS_ARTIFACT_SCHEMA_VERSION = "1"

# horizonの用途分類(descriptive metadata。calibration対象からのdropはしない)
_HORIZON_USAGE = {
    ("BUSINESS_DAYS", 1): "OPERATIONAL_CHECK",
    ("BUSINESS_DAYS", 5): "SHORT_TERM",
    ("BUSINESS_DAYS", 20): "SHORT_TERM",
    ("BUSINESS_DAYS", 60): "MEDIUM_TERM_PRIMARY_CANDIDATE",
    ("BUSINESS_DAYS", 120): "LONG_TERM",
    ("BUSINESS_DAYS", 250): "LONG_TERM",
    ("CALENDAR_DAYS", 7): "WEEKLY_REVIEW",
}

_SIGNAL_METRICS = (
    "price_return_pct",
    "excess_return_pct",
    "mae_from_recommendation_price_pct",
    "mfe_from_recommendation_price_pct",
)

_ENTRY_NUMERIC_METRICS = (
    "business_days_to_reach_entry",
    "hypothetical_return_from_standard_price_pct",
)

_SCORE_FIELDS = ("total_score", "company_quality_score", "purchase_attractiveness_score")

_VERSION_FIELDS = ("rule_version", "company_quality_score_model_version")


@dataclass(frozen=True)
class AnalysisParameters:
    """分析パラメータ。すべてartifact metadataへ記録される(再現性)。"""

    small_sample_warning_rows: int = 30
    small_sample_warning_distinct_stocks: int = 10
    bootstrap_iterations: int = 500
    bootstrap_seed: int = 42
    regime_flat_band_pct: float = 1.0
    # 帯の区切りは固定仕様ではない分析パラメータ(最後は上限の排他境界)
    score_band_edges: tuple[float, ...] = (0.0, 40.0, 50.0, 60.0, 70.0, 101.0)


@dataclass
class ParsedDataset:
    metadata: dict[str, Any]
    rows: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)


def parse_dataset_jsonl(text: str) -> ParsedDataset:
    """Phase B export(JSONL)を読み込む。1行目はrecord_type=metadata必須。"""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("datasetが空です")
    metadata = json.loads(lines[0])
    if metadata.get("record_type") != "metadata":
        raise ValueError("datasetの1行目がmetadataではありません")
    rows = []
    for line in lines[1:]:
        record = json.loads(line)
        if record.get("record_type") != "row":
            raise ValueError(f"未知のrecord_typeです: {record.get('record_type')}")
        rows.append(record)
    return ParsedDataset(metadata=metadata, rows=rows)


# --- 統計ヘルパー(決定的) -----------------------------------------------------


def _percentile(sorted_values: list[float], q: float) -> float:
    """線形補間percentile(statistics.quantilesへ依存せず定義を固定)。"""
    if not sorted_values:
        raise ValueError("empty")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _numeric_summary(values: list[Any]) -> dict[str, Any] | None:
    present: list[float] = sorted(float(v) for v in values if v is not None)
    if not present:
        return None
    return {
        "n": len(present),
        "mean": statistics.fmean(present),
        "median": statistics.median(present),
        "p10": _percentile(present, 0.10),
        "p25": _percentile(present, 0.25),
        "p75": _percentile(present, 0.75),
        "p90": _percentile(present, 0.90),
    }


def _wilson_interval(successes: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """Wilson score interval(95%)。"""
    if n == 0:
        raise ValueError("n=0")
    phat = successes / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z / denom) * ((phat * (1 - phat) / n + z * z / (4 * n * n)) ** 0.5)
    return (max(0.0, center - margin), min(1.0, center + margin))


def _proportion_summary(
    successes: int, n: int, *, with_wilson: bool, sample_definition: str
) -> dict[str, Any]:
    """比率指標。CIはsample定義を明示して付与し、RAW(依存標本)では付けない。"""
    summary: dict[str, Any] = {
        "successes": successes,
        "n": n,
        "proportion": (successes / n) if n else None,
        "sample_definition": sample_definition,
    }
    if with_wilson and n > 0:
        low, high = _wilson_interval(successes, n)
        summary["wilson_95_low"] = low
        summary["wilson_95_high"] = high
    return summary


def _cluster_bootstrap_median_ci(
    values_by_stock: dict[str, list[float]], iterations: int, seed: int
) -> dict[str, Any] | None:
    """stock単位cluster bootstrapによるmedianの95%区間(不確実性の記述専用。
    優劣判定には使用しない)。決定的seed・銘柄リストはソート順固定。"""
    stocks = sorted(s for s, vals in values_by_stock.items() if vals)
    if len(stocks) < 2:
        return None
    rng = random.Random(seed)
    medians: list[float] = []
    for _ in range(iterations):
        sampled: list[float] = []
        for _ in range(len(stocks)):
            stock = stocks[rng.randrange(len(stocks))]
            sampled.extend(values_by_stock[stock])
        if sampled:
            medians.append(statistics.median(sampled))
    if not medians:
        return None
    medians.sort()
    return {
        "median_bootstrap_95_low": _percentile(medians, 0.025),
        "median_bootstrap_95_high": _percentile(medians, 0.975),
        "iterations": iterations,
        "seed": seed,
        "n_clusters": len(stocks),
        "cluster_unit": "stock_code",
    }


# --- cohort軸 ------------------------------------------------------------------


def _horizon_key(row: dict[str, Any]) -> str:
    return f"{row['horizon_unit']}:{row['horizon_value']}"


def _score_band(value: float | None, edges: tuple[float, ...]) -> str:
    if value is None:
        return "SCORE_UNAVAILABLE"
    for i in range(len(edges) - 1):
        if edges[i] <= value < edges[i + 1]:
            return f"[{edges[i]:g},{edges[i + 1]:g})"
    return "OUT_OF_BANDS"


def _regime(row: dict[str, Any], flat_band_pct: float) -> str:
    benchmark = row.get("benchmark_return_pct")
    if benchmark is None:
        return "BENCHMARK_UNAVAILABLE"
    if benchmark > flat_band_pct:
        return "UP"
    if benchmark < -flat_band_pct:
        return "DOWN"
    return "FLAT"


def _sample_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "n_rows": len(rows),
        "n_recommendations": len({r["recommendation_id"] for r in rows}),
        "n_windows": len({r["sample_group_id"] for r in rows if r.get("sample_selected")}),
        "n_distinct_stocks": len({r["stock_code"] for r in rows}),
    }


def _cell_warnings(counts: dict[str, int], params: AnalysisParameters) -> list[str]:
    warnings = []
    if (
        counts["n_rows"] < params.small_sample_warning_rows
        or counts["n_distinct_stocks"] < params.small_sample_warning_distinct_stocks
    ):
        # 数値は隠さない(value suppressionなし)。warningのみ付与する。
        warnings.append("SMALL_SAMPLE_WARNING")
    return warnings


# --- analysis本体 --------------------------------------------------------------


def analyze(dataset: ParsedDataset, params: AnalysisParameters) -> list[dict[str, Any]]:
    """analysis artifact(record dictのリスト)を生成する。先頭はanalysis_metadata。"""
    sample_definition = str(dataset.metadata.get("sample_definition", "RAW"))
    is_raw = sample_definition == "RAW"
    # 推論(CI・bootstrap)に使う行: 選択済みかつEVALUATED。
    # RAWのdescriptiveも同じ行集合を使う(RAWは全行selected=true)。
    evaluated = [
        r for r in dataset.rows if r.get("sample_selected") and r["row_status"] == "EVALUATED"
    ]

    cells: list[dict[str, Any]] = []

    def add_cell(section: str, cohort: dict[str, Any], rows: list[dict[str, Any]],
                 metrics: dict[str, Any]) -> None:
        counts = _sample_counts(rows)
        cells.append(
            {
                "record_type": "analysis_cell",
                "section": section,
                "cohort": cohort,
                "sample_counts": counts,
                "metrics": metrics,
                "warnings": _cell_warnings(counts, params),
            }
        )

    def signal_metrics(rows: list[dict[str, Any]], *, with_bootstrap: bool) -> dict[str, Any]:
        metrics: dict[str, Any] = {}
        for name in _SIGNAL_METRICS:
            metrics[name] = _numeric_summary([r.get(name) for r in rows])
        excess_present = [r for r in rows if r.get("excess_return_pct") is not None]
        positive = sum(1 for r in excess_present if r["excess_return_pct"] > 0)
        metrics["positive_excess_rate"] = _proportion_summary(
            positive,
            len(excess_present),
            with_wilson=not is_raw,
            sample_definition=sample_definition,
        )
        if with_bootstrap and not is_raw:
            by_stock: dict[str, list[float]] = {}
            for r in rows:
                if r.get("excess_return_pct") is not None:
                    by_stock.setdefault(r["stock_code"], []).append(r["excess_return_pct"])
            bootstrap = _cluster_bootstrap_median_ci(
                by_stock, params.bootstrap_iterations, params.bootstrap_seed
            )
            if bootstrap is not None:
                metrics["excess_return_median_bootstrap"] = bootstrap
        return metrics

    horizons = sorted({_horizon_key(r) for r in dataset.rows})

    # --- overview: horizon別SIGNAL(全horizon独立表示、primary固定なし) ---
    for horizon in horizons:
        rows = [r for r in evaluated if _horizon_key(r) == horizon]
        add_cell("signal_by_horizon", {"horizon": horizon}, rows,
                 signal_metrics(rows, with_bootstrap=True))

    # --- entry: 到達率(tier別)・到達日数・仮想リターン ---
    for horizon in horizons:
        rows = [r for r in evaluated if _horizon_key(r) == horizon]
        metrics: dict[str, Any] = {}
        for tier, column in (
            ("entry", "reached_entry_price"),
            ("standard", "reached_standard_price"),
            ("strong", "reached_strong_price"),
        ):
            present = [r for r in rows if r.get(column) is not None]
            reached = sum(1 for r in present if r[column] is True)
            metrics[f"reach_rate_{tier}"] = _proportion_summary(
                reached, len(present), with_wilson=not is_raw,
                sample_definition=sample_definition,
            )
        for name in _ENTRY_NUMERIC_METRICS:
            metrics[name] = _numeric_summary([r.get(name) for r in rows])
        add_cell("entry_by_horizon", {"horizon": horizon}, rows, metrics)

    # --- score cohorts(model version混在を避けるためversion軸も付与) ---
    for score_field in _SCORE_FIELDS:
        for horizon in horizons:
            horizon_rows = [r for r in evaluated if _horizon_key(r) == horizon]
            bands = sorted(
                {_score_band(r.get(score_field), params.score_band_edges) for r in horizon_rows}
            )
            for band in bands:
                rows = [
                    r
                    for r in horizon_rows
                    if _score_band(r.get(score_field), params.score_band_edges) == band
                ]
                add_cell(
                    "score_cohort",
                    {"score_field": score_field, "score_band": band, "horizon": horizon},
                    rows,
                    signal_metrics(rows, with_bootstrap=False),
                )

    # --- action cohorts ---
    for horizon in horizons:
        horizon_rows = [r for r in evaluated if _horizon_key(r) == horizon]
        actions = sorted({str(r.get("buy_action")) for r in horizon_rows})
        for action in actions:
            rows = [r for r in horizon_rows if str(r.get("buy_action")) == action]
            add_cell(
                "action_cohort",
                {"buy_action": action, "horizon": horizon},
                rows,
                signal_metrics(rows, with_bootstrap=False),
            )

    # --- model version cohorts(欠損は推測せずUNKNOWN cohortとして明示) ---
    for version_field in _VERSION_FIELDS:
        for horizon in horizons:
            horizon_rows = [r for r in evaluated if _horizon_key(r) == horizon]
            versions = sorted(
                {str(r.get(version_field) or "UNKNOWN") for r in horizon_rows}
            )
            for version in versions:
                rows = [
                    r
                    for r in horizon_rows
                    if str(r.get(version_field) or "UNKNOWN") == version
                ]
                add_cell(
                    "model_version_cohort",
                    {"version_field": version_field, "version": version, "horizon": horizon},
                    rows,
                    signal_metrics(rows, with_bootstrap=False),
                )

    # --- market regime cohorts(ex-post層別。予測特徴ではない) ---
    for horizon in horizons:
        horizon_rows = [r for r in evaluated if _horizon_key(r) == horizon]
        regimes = sorted({_regime(r, params.regime_flat_band_pct) for r in horizon_rows})
        for regime in regimes:
            rows = [
                r for r in horizon_rows if _regime(r, params.regime_flat_band_pct) == regime
            ]
            add_cell(
                "regime_cohort_ex_post",
                {"regime": regime, "horizon": horizon},
                rows,
                signal_metrics(rows, with_bootstrap=False),
            )

    # --- legacy label reference(historical rule-based labelの参考集計) ---
    for horizon in horizons:
        horizon_rows = [r for r in evaluated if _horizon_key(r) == horizon]
        labels = sorted({str(r.get("evaluation_label")) for r in horizon_rows})
        label_counts = {
            label: sum(1 for r in horizon_rows if str(r.get("evaluation_label")) == label)
            for label in labels
        }
        add_cell(
            "legacy_reference_label",
            {"horizon": horizon},
            horizon_rows,
            {"label_counts": label_counts},
        )

    # --- stock-level robustness(銘柄内median→銘柄間分布。自動優劣判定なし) ---
    for horizon in horizons:
        horizon_rows = [r for r in evaluated if _horizon_key(r) == horizon]
        metrics = {}
        for name in ("price_return_pct", "excess_return_pct"):
            by_stock: dict[str, list[float]] = {}
            for r in horizon_rows:
                if r.get(name) is not None:
                    by_stock.setdefault(r["stock_code"], []).append(r[name])
            per_stock_medians = sorted(
                statistics.median(vals) for vals in by_stock.values() if vals
            )
            metrics[f"{name}_per_stock_median_distribution"] = (
                _numeric_summary(per_stock_medians) if per_stock_medians else None
            )
        add_cell("stock_level_robustness", {"horizon": horizon}, horizon_rows, metrics)

    # --- 全体diagnostics(RAW含むdescriptive: coverage/missingness) ---
    all_rows = dataset.rows
    status_counts = {
        status: sum(1 for r in all_rows if r["row_status"] == status)
        for status in sorted({r["row_status"] for r in all_rows})
    }
    add_cell(
        "coverage",
        {"scope": "all_rows"},
        all_rows,
        {
            "row_status_counts": status_counts,
            "selected_row_count": sum(1 for r in all_rows if r.get("sample_selected")),
        },
    )

    metadata_record = _build_metadata(dataset, params, sample_definition, len(cells))
    cells.sort(key=lambda c: (c["section"], json.dumps(c["cohort"], sort_keys=True)))
    return [metadata_record, *cells]


def _build_metadata(
    dataset: ParsedDataset,
    params: AnalysisParameters,
    sample_definition: str,
    cell_count: int,
) -> dict[str, Any]:
    warnings = [
        # PRICE_ONLY定型注記(高配当スタイルでは長期horizonほど過小評価。
        # benchmark(TOPIX連動ETFのclose)もprice-onlyのためexcess returnは
        # 部分相殺により相対的に頑健)
        "PRICE_ONLY_RETURN_BASIS",
        # ex-post regime層別はLOOK-AHEAD(予測特徴として使用禁止)
        "REGIME_LOOK_AHEAD_WARNING",
        # EvaluationLabelは用途が異なるhistorical rule-based label
        "LEGACY_LABEL_REFERENCE_ONLY",
    ]
    if sample_definition == "RAW":
        # RAW行は独立標本ではない(同一銘柄の日次Recommendation重複)。
        # descriptive用途のみ。naive CI・有意差判定・優劣判定に使用しないこと。
        warnings.append("SAMPLE_DEPENDENCY_WARNING")
    else:
        # 非RAWでも独立性は保証されない(pseudo-replicationの軽減のみ)。
        # market common factor・sector相関・serial correlation・
        # 銘柄内の非重複window間依存が残る。
        warnings.append("REMAINING_DEPENDENCY_NOTE")
    return {
        "record_type": "analysis_metadata",
        "analysis_artifact_schema_version": ANALYSIS_ARTIFACT_SCHEMA_VERSION,
        "source_dataset": {
            "calibration_dataset_schema_version": dataset.metadata.get(
                "calibration_dataset_schema_version"
            ),
            "as_of": dataset.metadata.get("as_of"),
            "sample_definition": sample_definition,
            "sample_selector_parameters": dataset.metadata.get(
                "sample_selector_parameters", {}
            ),
            "return_basis": dataset.metadata.get("return_basis"),
            "row_count": dataset.metadata.get("row_count"),
        },
        "analysis_parameters": {
            "small_sample_warning_rows": params.small_sample_warning_rows,
            "small_sample_warning_distinct_stocks": (
                params.small_sample_warning_distinct_stocks
            ),
            "bootstrap_iterations": params.bootstrap_iterations,
            "bootstrap_seed": params.bootstrap_seed,
            "regime_flat_band_pct": params.regime_flat_band_pct,
            "score_band_edges": list(params.score_band_edges),
        },
        "warnings": sorted(warnings),
        "horizon_usage_classification": {
            f"{unit}:{value}": usage for (unit, value), usage in sorted(_HORIZON_USAGE.items())
        },
        "primary_horizon": None,  # C1では固定しない(60営業日はfuture primary candidate)
        "primary_horizon_future_candidate": "BUSINESS_DAYS:60",
        "cell_count": cell_count,
    }


def to_artifact_jsonl(records: list[dict[str, Any]]) -> str:
    lines = [json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records]
    return "\n".join(lines) + "\n"
