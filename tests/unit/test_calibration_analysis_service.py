"""Issue #28 Phase C1: Calibration Analysisのテスト。

- parse / RAW依存警告 / 非RAWのCI・bootstrap / SIGNAL・ENTRY KPI /
  horizon分離 / score・action・version・regime cohort / legacy label /
  stock-level robustness / 小標本warning(数値非表示なし)/ 決定性 /
  CLI smoke
"""

from __future__ import annotations

import datetime as dt
import json
import random
from decimal import Decimal

import pytest

from jstock_advisor.services.calibration_analysis_service import (
    ANALYSIS_ARTIFACT_SCHEMA_VERSION,
    AnalysisParameters,
    ParsedDataset,
    _wilson_interval,
    analyze,
    parse_dataset_jsonl,
    to_artifact_jsonl,
)

_PARAMS = AnalysisParameters(bootstrap_iterations=50, bootstrap_seed=7)


def _row(
    recommendation_id: str = "rec-1",
    stock_code: str = "1001",
    horizon_unit: str = "BUSINESS_DAYS",
    horizon_value: int = 5,
    row_status: str = "EVALUATED",
    sample_selected: bool = True,
    **overrides: object,
) -> dict:
    base = {
        "record_type": "row",
        "recommendation_id": recommendation_id,
        "stock_code": stock_code,
        "horizon_unit": horizon_unit,
        "horizon_value": horizon_value,
        "recommendation_date_jst": "2026-07-30",
        "recommended_at": "2026-07-30T01:00:00+00:00",
        "row_status": row_status,
        "sample_selected": sample_selected,
        "sample_group_id": f"{recommendation_id}|{horizon_unit}|{horizon_value}",
        "selection_reason": "RAW",
        "sample_definition": "RAW",
        "price_return_pct": 5.0,
        "excess_return_pct": 3.0,
        "mae_from_recommendation_price_pct": -3.0,
        "mfe_from_recommendation_price_pct": 8.0,
        "benchmark_return_pct": 2.0,
        "reached_entry_price": False,
        "reached_standard_price": False,
        "reached_strong_price": False,
        "business_days_to_reach_entry": None,
        "hypothetical_return_from_standard_price_pct": 16.7,
        "buy_action": "WATCH_FOR_PRICE",
        "raw_buy_action": "WATCH_FOR_PRICE",
        "recommendation_type": "BUY",
        "total_score": 61.5,
        "company_quality_score": 38.0,
        "purchase_attractiveness_score": None,
        "rule_version": "v1-mvp",
        "company_quality_score_model_version": "v1",
        "evaluation_label": "SUCCESS",
    }
    base.update(overrides)
    return base


def _dataset(rows: list[dict], sample_definition: str = "RAW") -> ParsedDataset:
    return ParsedDataset(
        metadata={
            "record_type": "metadata",
            "calibration_dataset_schema_version": "1",
            "as_of": "2026-08-28T07:00:00+00:00",
            "sample_definition": sample_definition,
            "sample_selector_parameters": {},
            "return_basis": "PRICE_ONLY",
            "row_count": len(rows),
        },
        rows=rows,
    )


def _cells(records: list[dict], section: str) -> list[dict]:
    return [r for r in records if r.get("section") == section]


# --- parse ---------------------------------------------------------------------


def test_parse_rejects_empty_and_missing_metadata() -> None:
    with pytest.raises(ValueError):
        parse_dataset_jsonl("")
    with pytest.raises(ValueError):
        parse_dataset_jsonl(json.dumps({"record_type": "row"}) + "\n")


# --- RAW / 非RAWのsemantics ----------------------------------------------------


def test_raw_metadata_has_dependency_warning_and_no_ci() -> None:
    records = analyze(_dataset([_row()]), _PARAMS)
    metadata = records[0]
    assert metadata["record_type"] == "analysis_metadata"
    assert metadata["analysis_artifact_schema_version"] == ANALYSIS_ARTIFACT_SCHEMA_VERSION
    assert "SAMPLE_DEPENDENCY_WARNING" in metadata["warnings"]
    assert "PRICE_ONLY_RETURN_BASIS" in metadata["warnings"]
    assert "REGIME_LOOK_AHEAD_WARNING" in metadata["warnings"]
    assert "LEGACY_LABEL_REFERENCE_ONLY" in metadata["warnings"]
    signal = _cells(records, "signal_by_horizon")[0]
    rate = signal["metrics"]["positive_excess_rate"]
    assert "wilson_95_low" not in rate  # RAWでは独立標本仮定のCIを出さない
    assert rate["sample_definition"] == "RAW"
    assert "excess_return_median_bootstrap" not in signal["metrics"]


def test_non_raw_has_wilson_and_cluster_bootstrap() -> None:
    rows = [
        _row(f"rec-{i}", stock_code=f"10{i:02d}", excess_return_pct=float(i - 2))
        for i in range(5)
    ]
    records = analyze(_dataset(rows, "NON_OVERLAPPING_WINDOW"), _PARAMS)
    metadata = records[0]
    assert "REMAINING_DEPENDENCY_NOTE" in metadata["warnings"]
    assert "SAMPLE_DEPENDENCY_WARNING" not in metadata["warnings"]
    signal = _cells(records, "signal_by_horizon")[0]
    rate = signal["metrics"]["positive_excess_rate"]
    assert "wilson_95_low" in rate and "wilson_95_high" in rate
    assert rate["sample_definition"] == "NON_OVERLAPPING_WINDOW"
    assert rate["n"] == 5  # CIにsample定義とnが必ず紐付く
    bootstrap = signal["metrics"]["excess_return_median_bootstrap"]
    assert bootstrap["seed"] == 7
    assert bootstrap["iterations"] == 50
    assert bootstrap["cluster_unit"] == "stock_code"
    assert bootstrap["n_clusters"] == 5


def test_unselected_rows_are_excluded_from_analysis_sample() -> None:
    rows = [
        _row("rec-1", excess_return_pct=100.0),
        _row("rec-2", sample_selected=False, excess_return_pct=-100.0),
    ]
    records = analyze(_dataset(rows, "NON_OVERLAPPING_WINDOW"), _PARAMS)
    signal = _cells(records, "signal_by_horizon")[0]
    assert signal["sample_counts"]["n_rows"] == 1  # 非選択行は推論サンプル外
    coverage = _cells(records, "coverage")[0]
    assert coverage["sample_counts"]["n_rows"] == 2  # coverageは全行を対象
    assert coverage["metrics"]["selected_row_count"] == 1


# --- SIGNAL / ENTRY KPI --------------------------------------------------------


def test_signal_metric_summary_values() -> None:
    rows = [_row(f"rec-{i}", price_return_pct=v) for i, v in enumerate([1.0, 2.0, 3.0, 100.0])]
    records = analyze(_dataset(rows), _PARAMS)
    summary = _cells(records, "signal_by_horizon")[0]["metrics"]["price_return_pct"]
    assert summary["n"] == 4
    assert summary["median"] == 2.5
    assert summary["p25"] == 1.75
    assert summary["p75"] == 27.25
    assert summary["mean"] == pytest.approx(26.5)  # meanは外れ値の影響を受ける(参考値)


def test_entry_metrics_reach_rate_and_days() -> None:
    rows = [
        _row("rec-1", reached_entry_price=True, business_days_to_reach_entry=3),
        _row("rec-2", reached_entry_price=False),
        _row("rec-3", reached_entry_price=None),  # 到達判定不能はn分母から除外
    ]
    records = analyze(_dataset(rows), _PARAMS)
    metrics = _cells(records, "entry_by_horizon")[0]["metrics"]
    assert metrics["reach_rate_entry"]["successes"] == 1
    assert metrics["reach_rate_entry"]["n"] == 2
    assert metrics["business_days_to_reach_entry"]["n"] == 1
    assert metrics["hypothetical_return_from_standard_price_pct"]["n"] == 3


# --- horizon分離 ---------------------------------------------------------------


def test_horizons_are_reported_independently_without_pooling() -> None:
    rows = [
        _row("rec-1", horizon_value=5),
        _row("rec-2", horizon_value=60),
        _row("rec-3", horizon_unit="CALENDAR_DAYS", horizon_value=7),
    ]
    records = analyze(_dataset(rows), _PARAMS)
    horizons = {c["cohort"]["horizon"] for c in _cells(records, "signal_by_horizon")}
    assert horizons == {"BUSINESS_DAYS:5", "BUSINESS_DAYS:60", "CALENDAR_DAYS:7"}
    metadata = records[0]
    assert metadata["primary_horizon"] is None  # C1ではprimary固定なし
    assert metadata["primary_horizon_future_candidate"] == "BUSINESS_DAYS:60"
    assert metadata["horizon_usage_classification"]["BUSINESS_DAYS:1"] == "OPERATIONAL_CHECK"


# --- cohorts -------------------------------------------------------------------


def test_score_cohort_banding_and_unavailable() -> None:
    rows = [
        _row("rec-1", total_score=45.0),
        _row("rec-2", total_score=65.0),
        _row("rec-3", total_score=None),
    ]
    records = analyze(_dataset(rows), _PARAMS)
    bands = {
        c["cohort"]["score_band"]
        for c in _cells(records, "score_cohort")
        if c["cohort"]["score_field"] == "total_score"
    }
    assert bands == {"[40,50)", "[60,70)", "SCORE_UNAVAILABLE"}


def test_action_cohorts_are_separated() -> None:
    rows = [
        _row("rec-1", buy_action="WATCH_FOR_PRICE"),
        _row("rec-2", buy_action="SMALL_ENTRY"),
        _row("rec-3", buy_action="NOT_ATTRACTIVE"),
    ]
    records = analyze(_dataset(rows), _PARAMS)
    actions = {c["cohort"]["buy_action"] for c in _cells(records, "action_cohort")}
    assert actions == {"WATCH_FOR_PRICE", "SMALL_ENTRY", "NOT_ATTRACTIVE"}


def test_model_version_cohort_with_unknown() -> None:
    rows = [
        _row("rec-1", company_quality_score_model_version="v1"),
        _row("rec-2", company_quality_score_model_version=None),  # 欠損は推測せずUNKNOWN
    ]
    records = analyze(_dataset(rows), _PARAMS)
    versions = {
        c["cohort"]["version"]
        for c in _cells(records, "model_version_cohort")
        if c["cohort"]["version_field"] == "company_quality_score_model_version"
    }
    assert versions == {"v1", "UNKNOWN"}


def test_regime_cohorts_are_ex_post_classification() -> None:
    rows = [
        _row("rec-1", benchmark_return_pct=5.0),
        _row("rec-2", benchmark_return_pct=-5.0),
        _row("rec-3", benchmark_return_pct=0.5),
        _row("rec-4", benchmark_return_pct=None),
    ]
    records = analyze(_dataset(rows), _PARAMS)
    regimes = {c["cohort"]["regime"] for c in _cells(records, "regime_cohort_ex_post")}
    assert regimes == {"UP", "DOWN", "FLAT", "BENCHMARK_UNAVAILABLE"}


def test_legacy_label_reference_counts() -> None:
    rows = [
        _row("rec-1", evaluation_label="SUCCESS"),
        _row("rec-2", evaluation_label="ACCEPTABLE"),
        _row("rec-3", evaluation_label="SUCCESS"),
    ]
    records = analyze(_dataset(rows), _PARAMS)
    counts = _cells(records, "legacy_reference_label")[0]["metrics"]["label_counts"]
    assert counts == {"SUCCESS": 2, "ACCEPTABLE": 1}


def test_stock_level_robustness_uses_per_stock_medians() -> None:
    rows = [
        _row("rec-1", stock_code="1001", price_return_pct=10.0),
        _row("rec-2", stock_code="1001", price_return_pct=20.0),
        _row("rec-3", stock_code="1002", price_return_pct=-10.0),
    ]
    records = analyze(_dataset(rows), _PARAMS)
    dist = _cells(records, "stock_level_robustness")[0]["metrics"][
        "price_return_pct_per_stock_median_distribution"
    ]
    assert dist["n"] == 2  # 銘柄内median(15.0と-10.0)→銘柄間分布
    assert dist["median"] == 2.5


# --- 小標本warning(数値は隠さない) --------------------------------------------


def test_small_sample_warning_without_value_suppression() -> None:
    records = analyze(_dataset([_row()]), _PARAMS)
    signal = _cells(records, "signal_by_horizon")[0]
    assert "SMALL_SAMPLE_WARNING" in signal["warnings"]
    assert signal["metrics"]["price_return_pct"]["median"] == 5.0  # 数値非表示にしない
    params = records[0]["analysis_parameters"]
    assert params["small_sample_warning_rows"] == 30
    assert params["small_sample_warning_distinct_stocks"] == 10


# --- Wilson ---------------------------------------------------------------------


def test_wilson_interval_bounds() -> None:
    low, high = _wilson_interval(8, 10)
    assert 0.0 <= low < 0.8 < high <= 1.0
    low0, high0 = _wilson_interval(0, 10)
    assert low0 == 0.0
    assert high0 > 0.0


# --- determinism ----------------------------------------------------------------


def test_shuffled_rows_produce_byte_identical_artifact() -> None:
    rows = [
        _row(f"rec-{i}", stock_code=f"10{i:02d}", excess_return_pct=float(i))
        for i in range(6)
    ]
    baseline = to_artifact_jsonl(analyze(_dataset(rows, "NON_OVERLAPPING_WINDOW"), _PARAMS))
    rng = random.Random(1)
    for _ in range(3):
        shuffled = list(rows)
        rng.shuffle(shuffled)
        result = to_artifact_jsonl(
            analyze(_dataset(shuffled, "NON_OVERLAPPING_WINDOW"), _PARAMS)
        )
        assert result == baseline
    # runtime値(生成時刻等)がartifactへ混入していないことの粗い確認
    assert "generated_at" not in baseline


# --- CLI smoke(end-to-end: export → analyze) ----------------------------------


def test_cli_export_then_analyze_smoke(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from jstock_advisor.cli.calibration import app

    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    runner = CliRunner()
    dataset_path = tmp_path / "dataset.jsonl"
    result = runner.invoke(app, ["export-dataset", "--output", str(dataset_path)])
    assert result.exit_code == 0, result.output
    artifact_path = tmp_path / "report.jsonl"
    result = runner.invoke(
        app,
        ["analyze", "--input", str(dataset_path), "--output", str(artifact_path)],
    )
    assert result.exit_code == 0, result.output
    first = json.loads(artifact_path.read_text(encoding="utf-8").splitlines()[0])
    assert first["record_type"] == "analysis_metadata"


# --- Phase B dataset由来のend-to-end(builder→jsonl→analyze) -------------------


def test_end_to_end_from_builder_jsonl() -> None:
    from jstock_advisor.config.loader import load_config
    from jstock_advisor.domain.business_calendar import BusinessCalendar
    from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
    from jstock_advisor.domain.entities.recommendation import Recommendation
    from jstock_advisor.services.calibration_dataset_service import (
        CalibrationDatasetBuilder,
        to_jsonl,
    )

    config = load_config()
    now = dt.datetime(2026, 8, 28, 7, 0, tzinfo=dt.UTC)

    class _Repo:
        def __init__(self, items):
            self._items = items

        def list_all(self):
            return list(self._items)

    rec = Recommendation(
        recommendation_id="rec-e2e",
        stock_code="8136",
        stock_name="テスト株式会社",
        recommended_at=dt.datetime(2026, 7, 30, 1, 0, tzinfo=dt.UTC),
        recommendation_type=RecommendationType.BUY,
        price_at_recommendation=Decimal("1000"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )
    builder = CalibrationDatasetBuilder(
        config=config,
        business_calendar=BusinessCalendar.from_config(config.holiday_calendar),
        recommendation_repository=_Repo([rec]),  # type: ignore[arg-type]
        evaluation_repository=_Repo([]),  # type: ignore[arg-type]
        decision_snapshot_repository=_Repo([]),  # type: ignore[arg-type]
    )
    jsonl = to_jsonl(builder.build(now), selected_only=False, include_pending=True)
    parsed = parse_dataset_jsonl(jsonl)
    records = analyze(parsed, _PARAMS)
    assert records[0]["source_dataset"]["return_basis"] == "PRICE_ONLY"
    assert any(c["section"] == "coverage" for c in records[1:])
