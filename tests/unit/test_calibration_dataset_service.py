"""Issue #28 Phase B: Calibration Dataset Builderのテスト。

- join(正常・欠損・orphan・重複)/ row status / horizon意味論(off-by-one)
- 列の透過性(alias含む)/ model version / execution事実の保持
- selector(RAW / NON_OVERLAPPING_WINDOW)
- 決定性(入力順シャッフルでもJSONLバイト一致)/ serialization
- repository write禁止・source entity不変
"""

from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.decision_snapshot import DecisionSnapshot, build_decision_id
from jstock_advisor.domain.entities.enums import (
    BuyAction,
    ConfidenceLevel,
    DecisionType,
    EvaluationLabel,
    RecommendationType,
)
from jstock_advisor.domain.entities.evaluation import EvaluationResult
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.services.calibration_dataset_service import (
    CALIBRATION_DATASET_SCHEMA_VERSION,
    CalibrationDatasetBuilder,
    HorizonUnit,
    RowStatus,
    SampleDefinition,
    SelectionReason,
    to_csv,
    to_jsonl,
)
from jstock_advisor.services.recommendation_evaluation_service import _CALENDAR_HORIZON_DAYS

_CONFIG = load_config()
_CALENDAR = BusinessCalendar.from_config(_CONFIG.holiday_calendar)
_NOW = dt.datetime(2026, 8, 28, 7, 0, tzinfo=dt.UTC)  # JST 2026-08-28 16:00
_BUSINESS_HORIZONS = sorted(
    set(_CONFIG.schedule.evaluation_horizons_business_days["all_types_common"])
)


class _FakeRepo:
    """list系のみを提供するread-only fake。write系が呼ばれたら即失敗するspy。"""

    _WRITE_METHODS = ("save", "upsert", "upsert_many", "delete", "insert_if_absent")

    def __init__(self, items: list) -> None:
        self._items = items
        self.write_calls: list[str] = []

    def list_all(self) -> list:
        return list(self._items)

    def __getattr__(self, name: str):
        if name in self._WRITE_METHODS:

            def _fail(*args, **kwargs):
                self.write_calls.append(name)
                raise AssertionError(f"write method {name} must not be called (read-only)")

            return _fail
        raise AttributeError(name)


def _recommendation(
    recommendation_id: str = "rec-a",
    stock_code: str = "8136",
    recommended_at: dt.datetime = dt.datetime(2026, 7, 30, 1, 0, tzinfo=dt.UTC),
    buy_action: BuyAction | None = BuyAction.WATCH_FOR_PRICE,
    **overrides: object,
) -> Recommendation:
    base = dict(
        recommendation_id=recommendation_id,
        stock_code=stock_code,
        stock_name="テスト株式会社",
        recommended_at=recommended_at,
        recommendation_type=RecommendationType.BUY,
        price_at_recommendation=Decimal("1000"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
        buy_action=buy_action,
        raw_buy_action=buy_action,
        total_score=61.5,
        company_quality_score=38.0,
        entry_buy_price=Decimal("950"),
        standard_buy_price=Decimal("900"),
        strong_buy_price=Decimal("850"),
        valuation_anchor=Decimal("1100"),
        buy_score_input_facts={"buy_score_input_facts_schema_version": 1},
    )
    base.update(overrides)
    return Recommendation(**base)  # type: ignore[arg-type]


def _evaluation(
    evaluation_id: str,
    recommendation_id: str = "rec-a",
    horizon_business_days: int | None = 5,
    horizon_calendar_days: int | None = None,
    evaluated_at: dt.datetime = dt.datetime(2026, 8, 6, 9, 30, tzinfo=dt.UTC),
    **overrides: object,
) -> EvaluationResult:
    base = dict(
        evaluation_id=evaluation_id,
        recommendation_id=recommendation_id,
        horizon_business_days=horizon_business_days,
        horizon_calendar_days=horizon_calendar_days,
        evaluated_at=evaluated_at,
        evaluation_date=dt.date(2026, 8, 6),
        price_at_evaluation=Decimal("1050"),
        price_return_pct=5.0,
        buy_price_based_return_pct=16.7,
        max_gain_pct=8.0,
        max_drawdown_pct=-3.0,
        reached_tentative_buy_price=False,
        reached_standard_buy_price=False,
        reached_aggressive_buy_price=False,
        business_days_to_reach_price=None,
        benchmark_symbol="TOPIX",
        benchmark_return_pct=2.0,
        excess_return_pct=3.0,
        evaluation_label=EvaluationLabel.SUCCESS,
        label_evidence="test",
    )
    base.update(overrides)
    return EvaluationResult(**base)  # type: ignore[arg-type]


def _snapshot(recommendation_id: str = "rec-a", stock_code: str = "8136") -> DecisionSnapshot:
    return DecisionSnapshot(
        decision_id=build_decision_id(recommendation_id),
        decision_type=DecisionType.BUY,
        stock_code=stock_code,
        evaluated_at=_NOW,
        evaluation_date_jst=dt.date(2026, 7, 30),
        recommendation_id=recommendation_id,
        market_price=Decimal("1000"),
        rule_version="v1-mvp",
        model_version="v5",
    )


def _builder(recommendations, evaluations, snapshots):
    return (
        CalibrationDatasetBuilder(
            config=_CONFIG,
            business_calendar=_CALENDAR,
            recommendation_repository=_FakeRepo(recommendations),  # type: ignore[arg-type]
            evaluation_repository=_FakeRepo(evaluations),  # type: ignore[arg-type]
            decision_snapshot_repository=_FakeRepo(snapshots),  # type: ignore[arg-type]
        ),
        None,
    )[0]


def _rows_for(dataset, recommendation_id, unit=None, value=None):
    return [
        r
        for r in dataset.rows
        if r.recommendation_id == recommendation_id
        and (unit is None or r.horizon_unit == unit)
        and (value is None or r.horizon_value == value)
    ]


# --- A/O: normal join・raw粒度 -------------------------------------------------


def test_raw_rows_cover_all_recommendations_and_horizons_without_dedup() -> None:
    recs = [_recommendation("rec-a"), _recommendation("rec-b", stock_code="8136")]
    dataset = _builder(recs, [], []).build(_NOW)
    per_rec = len(_BUSINESS_HORIZONS) + 1  # +1 = calendar horizon
    assert len(dataset.rows) == 2 * per_rec  # 同一銘柄の重複Recommendationもdedupしない
    assert dataset.metadata["row_count"] == len(dataset.rows)


def test_normal_join_populates_signal_entry_outcome_columns() -> None:
    rec = _recommendation()
    ev = _evaluation("ev-1", horizon_business_days=5)
    dataset = _builder([rec], [ev], [_snapshot()]).build(_NOW)
    row = _rows_for(dataset, "rec-a", HorizonUnit.BUSINESS_DAYS, 5)[0]
    assert row.row_status == RowStatus.EVALUATED
    assert row.price_return_pct == 5.0
    assert row.benchmark_symbol == "TOPIX"
    assert row.excess_return_pct == 3.0
    assert row.evaluation_label == "SUCCESS"
    assert row.decision_snapshot_present is True
    assert row.decision_snapshot_model_version == "v5"


# --- B/C/G: missing states -----------------------------------------------------


def test_missing_evaluation_due_vs_pending() -> None:
    rec = _recommendation()  # 2026-07-30起点
    dataset = _builder([rec], [], []).build(_NOW)
    due_5 = _rows_for(dataset, "rec-a", HorizonUnit.BUSINESS_DAYS, 5)[0]
    assert due_5.row_status == RowStatus.EVALUATION_MISSING  # 到来済みだが評価なし
    far = _rows_for(dataset, "rec-a", HorizonUnit.BUSINESS_DAYS, max(_BUSINESS_HORIZONS))[0]
    assert far.row_status == RowStatus.NOT_YET_EVALUABLE  # 未到来
    assert far.price_return_pct is None
    # dropされていない(全horizon行が存在する)
    assert len(_rows_for(dataset, "rec-a")) == len(_BUSINESS_HORIZONS) + 1


def test_missing_decision_snapshot_is_flagged_not_dropped() -> None:
    dataset = _builder([_recommendation()], [], []).build(_NOW)
    row = _rows_for(dataset, "rec-a")[0]
    assert row.decision_snapshot_present is False
    assert row.decision_snapshot_model_version is None


# --- D: orphan EvaluationResult ------------------------------------------------


def test_orphan_evaluation_goes_to_diagnostics_not_rows() -> None:
    orphan = _evaluation("ev-orphan", recommendation_id="rec-unknown")
    dataset = _builder([_recommendation()], [orphan], []).build(_NOW)
    assert dataset.diagnostics.orphan_evaluation_count == 1
    assert dataset.diagnostics.orphan_evaluation_ids_sample == ["ev-orphan"]
    assert dataset.metadata["orphan_evaluation_count"] == 1
    assert all(r.recommendation_id != "rec-unknown" for r in dataset.rows)


# --- E/17: duplicate EvaluationResult ------------------------------------------


def test_duplicate_evaluation_is_diagnosed_with_deterministic_representative() -> None:
    early = _evaluation(
        "ev-z-early",
        evaluated_at=dt.datetime(2026, 8, 6, 9, 0, tzinfo=dt.UTC),
        price_return_pct=1.0,
    )
    late = _evaluation(
        "ev-a-late",
        evaluated_at=dt.datetime(2026, 8, 6, 10, 0, tzinfo=dt.UTC),
        price_return_pct=9.0,
    )
    dataset = _builder([_recommendation()], [late, early], []).build(_NOW)
    row = _rows_for(dataset, "rec-a", HorizonUnit.BUSINESS_DAYS, 5)[0]
    assert row.duplicate_evaluation_count == 2  # 正常化ではないことの明示
    assert row.price_return_pct == 1.0  # evaluated_at昇順→evaluation_id辞書順の代表
    assert dataset.diagnostics.duplicate_evaluation_row_count == 1
    assert len(_rows_for(dataset, "rec-a", HorizonUnit.BUSINESS_DAYS, 5)) == 1  # 行は複製しない


# --- F: BUSINESS_DAYS / CALENDAR_DAYS ------------------------------------------


def test_business_and_calendar_horizons_are_structurally_distinct() -> None:
    cal_ev = _evaluation(
        "ev-cal",
        horizon_business_days=None,
        horizon_calendar_days=_CALENDAR_HORIZON_DAYS,
    )
    dataset = _builder([_recommendation()], [cal_ev], []).build(_NOW)
    cal_rows = _rows_for(dataset, "rec-a", HorizonUnit.CALENDAR_DAYS)
    assert len(cal_rows) == 1
    assert cal_rows[0].horizon_value == _CALENDAR_HORIZON_DAYS
    assert cal_rows[0].row_status == RowStatus.EVALUATED
    # 同値のBUSINESS_DAYS行と混ざらない
    biz_same_value = _rows_for(
        dataset, "rec-a", HorizonUnit.BUSINESS_DAYS, _CALENDAR_HORIZON_DAYS
    )
    for row in biz_same_value:
        assert row.row_status != RowStatus.EVALUATED


# --- H/I/J/K/L/M/N: 事実列の透過性 --------------------------------------------


def test_data_issue_and_inconclusive_labels_are_preserved() -> None:
    ev = _evaluation(
        "ev-di", evaluation_label=EvaluationLabel.DATA_ISSUE, price_return_pct=0.0
    )
    dataset = _builder([_recommendation()], [ev], []).build(_NOW)
    row = _rows_for(dataset, "rec-a", HorizonUnit.BUSINESS_DAYS, 5)[0]
    assert row.evaluation_label == "DATA_ISSUE"
    assert row.row_status == RowStatus.EVALUATED  # labelはlegacy事実、statusとは独立


def test_none_false_zero_are_distinguishable() -> None:
    ev = _evaluation(
        "ev-nfz",
        price_return_pct=0.0,
        reached_tentative_buy_price=False,
        business_days_to_reach_price=None,
    )
    dataset = _builder([_recommendation()], [ev], []).build(_NOW)
    row = _rows_for(dataset, "rec-a", HorizonUnit.BUSINESS_DAYS, 5)[0]
    assert row.price_return_pct == 0.0
    assert row.reached_entry_price is False
    assert row.business_days_to_reach_entry is None


def test_watch_for_price_execution_facts_are_kept_verbatim() -> None:
    dataset = _builder([_recommendation()], [], []).build(_NOW)
    row = _rows_for(dataset, "rec-a")[0]
    assert row.recommendation_type == "BUY"
    assert row.buy_action == "WATCH_FOR_PRICE"
    assert row.raw_buy_action == "WATCH_FOR_PRICE"


def test_entry_columns_and_aliases() -> None:
    ev = _evaluation(
        "ev-entry",
        reached_tentative_buy_price=True,
        reached_standard_buy_price=True,
        reached_aggressive_buy_price=False,
        business_days_to_reach_price=3,
        buy_price_based_return_pct=16.7,
        max_gain_pct=8.0,
        max_drawdown_pct=-3.0,
    )
    dataset = _builder([_recommendation()], [ev], []).build(_NOW)
    row = _rows_for(dataset, "rec-a", HorizonUnit.BUSINESS_DAYS, 5)[0]
    assert row.reached_entry_price is True  # = reached_tentative_buy_price
    assert row.reached_standard_price is True
    assert row.reached_strong_price is False  # = reached_aggressive_buy_price
    assert row.business_days_to_reach_entry == 3
    assert row.hypothetical_return_from_standard_price_pct == 16.7  # = buy_price_based
    assert row.mae_from_recommendation_price_pct == -3.0  # = max_drawdown_pct
    assert row.mfe_from_recommendation_price_pct == 8.0  # = max_gain_pct


def test_model_version_columns() -> None:
    dataset = _builder([_recommendation()], [], [_snapshot()]).build(_NOW)
    row = _rows_for(dataset, "rec-a")[0]
    assert row.rule_version == "v1-mvp"
    assert row.company_quality_score_model_version == "v1"
    assert row.decision_snapshot_model_version == "v5"
    assert row.input_facts_schema_version == "1"


# --- P/Q/31: NON_OVERLAPPING_WINDOW --------------------------------------------


def test_non_overlapping_window_selects_first_and_marks_overlaps() -> None:
    rec_a = _recommendation("rec-a", recommended_at=dt.datetime(2026, 7, 30, 1, 0, tzinfo=dt.UTC))
    rec_b = _recommendation("rec-b", recommended_at=dt.datetime(2026, 7, 31, 1, 0, tzinfo=dt.UTC))
    dataset = _builder([rec_a, rec_b], [], []).build(
        _NOW, sample_definition=SampleDefinition.NON_OVERLAPPING_WINDOW
    )
    a5 = _rows_for(dataset, "rec-a", HorizonUnit.BUSINESS_DAYS, 5)[0]
    b5 = _rows_for(dataset, "rec-b", HorizonUnit.BUSINESS_DAYS, 5)[0]
    assert a5.sample_selected is True
    assert a5.selection_reason == SelectionReason.FIRST_IN_WINDOW
    assert b5.sample_selected is False  # 7/31はrec-aの5営業日windowと重なる
    assert b5.selection_reason == SelectionReason.OVERLAPS_PRIOR_WINDOW
    assert b5.sample_group_id == a5.sample_group_id  # 代表元グループを追跡可能
    # 行自体は削除されない
    assert len(dataset.rows) == 2 * (len(_BUSINESS_HORIZONS) + 1)


def test_non_overlapping_window_reopens_after_window_end() -> None:
    rec_a = _recommendation("rec-a", recommended_at=dt.datetime(2026, 7, 30, 1, 0, tzinfo=dt.UTC))
    window_end = _CALENDAR.add_business_days(dt.date(2026, 7, 30), 5)
    next_start = window_end + dt.timedelta(days=1)
    rec_c = _recommendation(
        "rec-c",
        recommended_at=dt.datetime(
            next_start.year, next_start.month, next_start.day, 1, 0, tzinfo=dt.UTC
        ),
    )
    dataset = _builder([rec_a, rec_c], [], []).build(
        _NOW, sample_definition=SampleDefinition.NON_OVERLAPPING_WINDOW
    )
    c5 = _rows_for(dataset, "rec-c", HorizonUnit.BUSINESS_DAYS, 5)[0]
    assert c5.sample_selected is True  # window終了翌日は新window
    assert c5.selection_reason == SelectionReason.FIRST_IN_WINDOW


def test_horizon_due_date_matches_existing_evaluation_semantics() -> None:
    """off-by-one固定: builderのdue dateは既存評価サービスと同じ
    add_business_days(recommended_atのUTC暦日, horizon)であること。"""
    rec = _recommendation()
    dataset = _builder([rec], [], []).build(_NOW)
    for horizon in _BUSINESS_HORIZONS:
        row = _rows_for(dataset, "rec-a", HorizonUnit.BUSINESS_DAYS, horizon)[0]
        assert row.evaluation_due_date == _CALENDAR.add_business_days(
            dt.date(2026, 7, 30), horizon
        )
    cal_row = _rows_for(dataset, "rec-a", HorizonUnit.CALENDAR_DAYS)[0]
    assert cal_row.evaluation_due_date == dt.date(2026, 7, 30) + dt.timedelta(
        days=_CALENDAR_HORIZON_DAYS
    )


def test_due_boundary_today_is_due_not_pending() -> None:
    """到来日当日(today_jst == due date)は既存意味論どおり「到来済み」。"""
    rec = _recommendation()
    due_20 = _CALENDAR.add_business_days(dt.date(2026, 7, 30), 20)
    now_on_due = dt.datetime(due_20.year, due_20.month, due_20.day, 5, 0, tzinfo=dt.UTC)
    dataset = _builder([rec], [], []).build(now_on_due)
    row = _rows_for(dataset, "rec-a", HorizonUnit.BUSINESS_DAYS, 20)[0]
    assert row.row_status == RowStatus.EVALUATION_MISSING  # 当日=到来済み(未評価)


# --- R/S: determinism ----------------------------------------------------------


def test_shuffled_input_produces_byte_identical_jsonl() -> None:
    recs = [
        _recommendation(f"rec-{i}", recommended_at=dt.datetime(2026, 7, 30, 1, i, tzinfo=dt.UTC))
        for i in range(5)
    ]
    evs = [_evaluation(f"ev-{i}", recommendation_id=f"rec-{i}") for i in range(5)]
    snaps = [_snapshot(f"rec-{i}") for i in range(5)]

    baseline = to_jsonl(
        _builder(recs, evs, snaps).build(_NOW), selected_only=False, include_pending=True
    )
    rng = random.Random(42)
    for _ in range(3):
        shuffled_recs, shuffled_evs, shuffled_snaps = list(recs), list(evs), list(snaps)
        rng.shuffle(shuffled_recs)
        rng.shuffle(shuffled_evs)
        rng.shuffle(shuffled_snaps)
        result = to_jsonl(
            _builder(shuffled_recs, shuffled_evs, shuffled_snaps).build(_NOW),
            selected_only=False,
            include_pending=True,
        )
        assert result == baseline


# --- T/U/37/38: serialization --------------------------------------------------


def test_jsonl_format_and_metadata() -> None:
    import json

    dataset = _builder([_recommendation()], [_evaluation("ev-1")], []).build(_NOW)
    lines = to_jsonl(dataset, selected_only=False, include_pending=True).splitlines()
    metadata = json.loads(lines[0])
    assert metadata["record_type"] == "metadata"
    assert metadata["calibration_dataset_schema_version"] == CALIBRATION_DATASET_SCHEMA_VERSION
    assert metadata["return_basis"] == "PRICE_ONLY"
    assert metadata["horizon_definition"]["CALENDAR_DAYS"] == [_CALENDAR_HORIZON_DAYS]
    assert "all_types_common" in metadata["horizon_definition"]["BUSINESS_DAYS"]
    assert metadata["benchmark_mapping_current_instrument"]["TOPIX"] == "1306.T"
    assert "benchmark_mapping_interpreted_at_export" in metadata
    first_row = json.loads(lines[1])
    assert first_row["record_type"] == "row"
    # null/false/0が型として区別される
    evaluated = [
        json.loads(line)
        for line in lines[1:]
        if json.loads(line)["row_status"] == "EVALUATED"
    ][0]
    assert evaluated["reached_entry_price"] is False
    assert evaluated["business_days_to_reach_entry"] is None
    assert evaluated["price_at_recommendation"] == "1000"  # Decimalは固定小数文字列


def test_csv_format_none_false_zero() -> None:
    ev = _evaluation("ev-1", price_return_pct=0.0)
    dataset = _builder([_recommendation()], [ev], []).build(_NOW)
    csv_text, meta_json = to_csv(dataset, selected_only=False, include_pending=True)
    lines = csv_text.splitlines()
    header = lines[0].split(",")
    evaluated_line = next(line for line in lines[1:] if ",EVALUATED," in line)
    cells = evaluated_line.split(",")
    row = dict(zip(header, cells, strict=True))
    assert row["price_return_pct"] == "0.0"  # 0が空文字に潰れない
    assert row["reached_entry_price"] == "false"  # Falseは"false"
    assert row["business_days_to_reach_entry"] == ""  # Noneは空文字
    assert '"return_basis": "PRICE_ONLY"' in meta_json  # sidecar metadata


# --- V/W: read-only保証 --------------------------------------------------------


def test_repositories_receive_no_writes_and_sources_unchanged() -> None:
    rec = _recommendation()
    ev = _evaluation("ev-1")
    snap = _snapshot()
    rec_dump = rec.model_dump_json()
    ev_dump = ev.model_dump_json()
    snap_dump = snap.model_dump_json()

    rec_repo = _FakeRepo([rec])
    ev_repo = _FakeRepo([ev])
    snap_repo = _FakeRepo([snap])
    builder = CalibrationDatasetBuilder(
        config=_CONFIG,
        business_calendar=_CALENDAR,
        recommendation_repository=rec_repo,  # type: ignore[arg-type]
        evaluation_repository=ev_repo,  # type: ignore[arg-type]
        decision_snapshot_repository=snap_repo,  # type: ignore[arg-type]
    )
    builder.build(_NOW, sample_definition=SampleDefinition.NON_OVERLAPPING_WINDOW)

    assert rec_repo.write_calls == []
    assert ev_repo.write_calls == []
    assert snap_repo.write_calls == []
    assert rec.model_dump_json() == rec_dump  # source entity不変
    assert ev.model_dump_json() == ev_dump
    assert snap.model_dump_json() == snap_dump


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError):
        _builder([], [], []).build(dt.datetime(2026, 8, 28, 7, 0))  # tzなしは拒否


# --- Phase C1: ACTION_CHANGE selector -----------------------------------------


def test_action_change_selector_marks_transitions_without_deleting_rows() -> None:
    base_at = dt.datetime(2026, 7, 30, 1, 0, tzinfo=dt.UTC)
    recs = [
        _recommendation("rec-1", recommended_at=base_at, buy_action=BuyAction.WATCH_FOR_PRICE),
        _recommendation(
            "rec-2",
            recommended_at=base_at + dt.timedelta(days=1),
            buy_action=BuyAction.WATCH_FOR_PRICE,
        ),
        _recommendation(
            "rec-3",
            recommended_at=base_at + dt.timedelta(days=2),
            buy_action=BuyAction.SMALL_ENTRY,
        ),
    ]
    dataset = _builder(recs, [], []).build(
        _NOW, sample_definition=SampleDefinition.ACTION_CHANGE
    )
    r1 = _rows_for(dataset, "rec-1", HorizonUnit.BUSINESS_DAYS, 5)[0]
    r2 = _rows_for(dataset, "rec-2", HorizonUnit.BUSINESS_DAYS, 5)[0]
    r3 = _rows_for(dataset, "rec-3", HorizonUnit.BUSINESS_DAYS, 5)[0]
    assert r1.sample_selected is True
    assert r1.selection_reason == SelectionReason.FIRST_OBSERVED
    assert r2.sample_selected is False
    assert r2.selection_reason == SelectionReason.NO_ACTION_CHANGE
    assert r2.sample_group_id == r1.sample_group_id
    assert r3.sample_selected is True
    assert r3.selection_reason == SelectionReason.ACTION_CHANGED
    # 行は削除されない(canonical raw datasetの粒度は不変)
    assert len(dataset.rows) == 3 * (len(_BUSINESS_HORIZONS) + 1)
    # metadataへselectorパラメータを記録
    assert dataset.metadata["sample_selector_parameters"] == {"comparison_field": "buy_action"}


def test_non_action_change_definitions_have_empty_selector_parameters() -> None:
    dataset = _builder([_recommendation()], [], []).build(_NOW)
    assert dataset.metadata["sample_selector_parameters"] == {}
