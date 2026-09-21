"""Issue #458(#160 PR-4): shadow監査記録の集計(純関数)。

不変条件を固定する:
  * 分母は「評価した強い判定の総数」。条件別の率の分母は、`not_evaluated`を除いた評価済みの記録数。
  * `not_evaluated`は「該当なし」ではない。件数へ含めず別掲する。
  * 日別はJST暦日(UTC 15:00で日が変わる)。
  * 記録が0件のときは、0件であること(「問題なし」ではないこと)を明示する。
  * 未知のschema_versionは`unparsed`ではなく別掲する。
  * 本ツールは閾値の判定・専用Tableへの移行の提案をしない。

★ 銘柄コードは実在しない0000系のみ。Productionへは一切アクセスしない。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from jstock_advisor.domain.entities.audit import AuditLogEntry
from jstock_advisor.services.judgment_safety_shadow_report import (
    NO_RECORDS_MESSAGE,
    NOTES,
    READ_UNIT_PRICE_NOTE,
    READ_UNIT_PRICE_USD_PER_MILLION,
    Baseline,
    ScanSnapshot,
    TableSnapshot,
    Window,
    build_monthly_extrapolation,
    build_shadow_result_metrics,
    build_storage_read_metrics,
    build_vs_baseline,
)

_UTC = dt.UTC


def _entry(
    *,
    audit_id: str = "a",
    at: dt.datetime | None = None,
    engine: str = "BUY_CANDIDATES",
    findings: list[tuple[str, str, bool]] | None = None,
    not_evaluated: list[str] | None = None,
    schema_version: object = 1,
    buy_action: str | None = "BUY",
    recommendation_type: str | None = None,
) -> AuditLogEntry:
    return AuditLogEntry(
        audit_id=audit_id,
        timestamp=at or dt.datetime(2026, 9, 24, 0, 0, tzinfo=_UTC),
        stock_code="0000",
        decision_type="judgment_safety_shadow",
        input_values={
            "schema_version": schema_version,
            "engine": engine,
            "buy_action": buy_action,
            "recommendation_type": recommendation_type,
        },
        calculation_formulas={},
        output_values={
            "findings": [
                {"condition_id": c, "reason_code": r, "would_suppress": w}
                for c, r, w in (findings or [])
            ],
            "not_evaluated": not_evaluated or [],
            "unmeasurable_g3_inputs": ["c", "a", "b"],
            "strong": True,
        },
        data_sources=[],
        rule_version="v1",
    )


def _shadow(entries: list[AuditLogEntry], **kwargs: Any) -> dict[str, Any]:
    return build_shadow_result_metrics(entries, kwargs.pop("window", Window()), unparsed=0)


def test_condition_rate_excludes_not_evaluated_from_the_denominator() -> None:
    entries = [
        _entry(audit_id="1", findings=[("G2", "STALE_FINANCIALS", True)]),
        _entry(audit_id="2", findings=[]),
        _entry(audit_id="3", findings=[], not_evaluated=["G2"]),
        _entry(audit_id="4", findings=[], not_evaluated=["G2"]),
    ]

    result = _shadow(entries)

    g2 = result["condition_counts"]["G2"]
    assert result["shadow_record_count"] == 4
    assert g2["findings"] == 1
    assert g2["records_with_finding"] == 1
    assert g2["evaluated_records"] == 2  # not_evaluatedの2件は分母に含めない
    assert g2["rate"] == 0.5


def test_not_evaluated_is_reported_separately_and_never_counted_as_a_finding() -> None:
    entries = [_entry(audit_id=str(i), findings=[], not_evaluated=["G1", "G4"]) for i in range(3)]

    result = _shadow(entries)

    assert result["not_evaluated_counts"] == {"G1": 3, "G2": 0, "G3": 0, "G4": 3}
    assert all(result["condition_counts"][c]["findings"] == 0 for c in ("G1", "G2", "G3", "G4"))
    assert result["condition_counts"]["G1"]["rate"] is None  # 0割りを0%と読ませない


def test_overlap_and_would_suppress_counts() -> None:
    entries = [
        _entry(audit_id="1", findings=[("G2", "STALE_FINANCIALS", True), ("G3", "X", True)]),
        _entry(audit_id="2", findings=[("G2", "STALE_FINANCIALS", False)]),
        _entry(audit_id="3", findings=[]),
    ]

    result = _shadow(entries)

    assert result["overlap"] == {
        "records_with_2plus_conditions": 1,
        "by_combination": {"G2+G3": 1},
    }
    assert result["would_suppress_counts"] == {
        "records_with_any_suppressible_finding": 1,
        "findings_total": 2,
    }
    assert result["reason_code_counts"] == {"STALE_FINANCIALS": 2, "X": 1}


def test_records_per_day_uses_jst_calendar_days() -> None:
    entries = [
        _entry(audit_id="1", at=dt.datetime(2026, 9, 24, 14, 59, tzinfo=_UTC)),  # JST 9/24 23:59
        _entry(audit_id="2", at=dt.datetime(2026, 9, 24, 15, 0, tzinfo=_UTC)),  # JST 9/25 00:00
    ]

    result = _shadow(entries)

    assert result["records_per_day"] == {"2026-09-24": 1, "2026-09-25": 1}
    assert result["measurement_window"]["jst_dates_with_records"] == ["2026-09-24", "2026-09-25"]


def test_window_filters_by_jst_date_inclusive() -> None:
    entries = [
        _entry(audit_id="1", at=dt.datetime(2026, 9, 24, 3, 0, tzinfo=_UTC)),
        _entry(audit_id="2", at=dt.datetime(2026, 9, 25, 3, 0, tzinfo=_UTC)),
        _entry(audit_id="3", at=dt.datetime(2026, 9, 26, 3, 0, tzinfo=_UTC)),
    ]

    result = build_shadow_result_metrics(
        entries, Window(dt.date(2026, 9, 25), dt.date(2026, 9, 25)), unparsed=0
    )

    assert result["shadow_record_count"] == 1
    assert result["records_per_day"] == {"2026-09-25": 1}


def test_zero_records_is_stated_explicitly() -> None:
    result = _shadow([])

    assert result["shadow_record_count"] == 0
    assert result["message"] == NO_RECORDS_MESSAGE
    assert "問題なし" in result["message"]  # 「0件は問題なしを意味しない」ことを明示する


def test_unknown_schema_version_is_separate_from_unparsed() -> None:
    entries = [_entry(audit_id="1"), _entry(audit_id="2", schema_version=2)]

    result = build_shadow_result_metrics(entries, Window(), unparsed=5)

    assert result["shadow_record_count"] == 1  # 未知のschemaは集計へ混ぜない
    assert result["unknown_schema_versions"] == {"2": 1}
    assert result["unparsed"] == 5


def test_recommendation_type_counts_use_buy_action_for_buy_and_type_for_holdings() -> None:
    entries = [
        _entry(audit_id="1", engine="BUY_CANDIDATES", buy_action="BUY"),
        _entry(
            audit_id="2",
            engine="HOLDINGS_PROFIT_TAKING",
            buy_action=None,
            recommendation_type="FULL_PROFIT_TAKE",
            findings=[("G3", "X", True)],
        ),
    ]

    result = _shadow(entries)

    assert result["recommendation_type_counts"] == {
        "BUY": {"records": 1, "with_finding": 0},
        "FULL_PROFIT_TAKE": {"records": 1, "with_finding": 1},
    }
    assert result["by_engine"] == {"BUY_CANDIDATES": 1, "HOLDINGS_PROFIT_TAKING": 1}


def test_notes_state_the_misreading_guards_and_no_recommendation() -> None:
    result = _shadow([_entry()])

    text = "".join(result["notes"])
    assert list(NOTES) == result["notes"]
    assert "下限値" in text and "測定不能の3項目" in text  # G3
    assert "FULL_PROFIT_TAKE" in text  # G4
    assert "not_evaluated" in text
    assert "提案をしない" in text
    assert result["unmeasurable_g3_inputs"] == ["a", "b", "c"]  # 記録が保持する値の和集合
    assert _shadow([])["unmeasurable_g3_inputs"] == []
    # 判定・提案のキーを持たない(閾値の判定・移行の提案はしない)。
    assert not {"recommendation", "verdict", "should_migrate", "threshold", "migration"} & set(
        result
    )


def test_vs_baseline_short_elapsed_days_is_a_reference_value() -> None:
    baseline = Baseline(78_700, 153_000_000, dt.date(2026, 9, 20))
    table = TableSnapshot(80_000, 155_000_000)

    zero = build_vs_baseline(table, baseline, dt.date(2026, 9, 20))
    one = build_vs_baseline(table, baseline, dt.date(2026, 9, 21))
    many = build_vs_baseline(table, baseline, dt.date(2026, 9, 30))

    assert zero["records_per_day_since_baseline"] is None  # 0日: 算出しない
    assert one["records_delta"] == 1_300
    assert one["records_per_day_since_baseline"] == 1_300
    assert "参考値" in one["note"]
    assert many["records_per_day_since_baseline"] == 130
    assert "参考値" not in many["note"]
    assert many["size_delta_bytes"] == 2_000_000


def test_monthly_extrapolation_is_flagged_as_reference_below_three_days() -> None:
    short = build_monthly_extrapolation({"2026-09-24": 10, "2026-09-25": 20}, 2_000, 20)
    enough = build_monthly_extrapolation({"a": 10, "b": 20, "c": 30}, 3_000, 30)
    none = build_monthly_extrapolation({}, 0, 0)

    assert short["records"] == 450  # 平均15 × 30
    assert "参考値" in short["note"]
    assert enough["records"] == 600
    assert enough["bytes_estimate"] == 60_000  # 平均100B × 600
    assert "参考値" not in enough["note"]
    assert none["records"] is None


def _scan(**overrides: Any) -> ScanSnapshot:
    base: dict[str, Any] = {
        "read_pages": 3,
        "scanned_count": 1_000,
        "count": 1_000,
        "total_rru": 20_000.0,
        "elapsed_time_sec": 1.5,
        "item_bytes": 5_000,
        "shadow_item_bytes": 1_000,
        "decision_type_counts": {"buy_signal": 900, "judgment_safety_shadow": 100},
        "all_records_by_jst_date": {"2026-09-24": 1_000},
    }
    base.update(overrides)
    return ScanSnapshot(**base)


def test_storage_metrics_combine_estimate_and_measurement_separately() -> None:
    baseline = Baseline(78_700, 153_000_000, dt.date(2026, 9, 20))

    result = build_storage_read_metrics(
        _scan(),
        TableSnapshot(80_000, 155_000_000),
        baseline,
        dt.date(2026, 9, 24),
        shadow_matched=100,
        shadow_records_per_day={"2026-09-24": 100},
    )

    assert result["table_metrics"]["source"].startswith("describe_table(概算")
    assert result["audit_total_examined"] == 1_000
    assert result["match_ratio"] == 0.1
    assert result["consumed_capacity"] == {"total_rru": 20_000.0}
    # 20,000 RRU × $0.1425 / 100万 = $0.00285(桁・単価を直書きし、定数を写さない)
    assert abs(result["estimated_read_cost_usd"] - 0.00285) < 1e-12
    assert "2026-09-21に確認" in result["estimated_read_cost_basis"]
    assert result["baseline"]["records"] == 78_700
    assert list(result["decision_type_counts"]) == ["buy_signal", "judgment_safety_shadow"]
    assert result["estimated_monthly_shadow_growth"]["records"] == 3_000


def test_read_unit_price_matches_the_confirmed_aws_price() -> None:
    """単価は、AWS Price List API(ap-northeast-1・Standard・オンデマンド)で確認した値。"""
    assert READ_UNIT_PRICE_USD_PER_MILLION == 0.1425
    assert "Standard" in READ_UNIT_PRICE_NOTE
    assert "要確認" not in READ_UNIT_PRICE_NOTE  # 確認済みの事実として記す


def test_describe_only_has_no_scan_metrics() -> None:
    baseline = Baseline(78_700, 153_000_000, dt.date(2026, 9, 20))

    result = build_storage_read_metrics(
        None,
        TableSnapshot(80_000, 155_000_000),
        baseline,
        dt.date(2026, 9, 24),
        shadow_matched=0,
        shadow_records_per_day={},
    )

    assert "consumed_capacity" not in result
    assert "audit_total_examined" not in result
    assert result["vs_baseline"] is not None


def test_match_ratio_is_none_when_nothing_was_scanned() -> None:
    baseline = Baseline(1, 1, dt.date(2026, 9, 20))

    result = build_storage_read_metrics(
        _scan(scanned_count=0, count=0, total_rru=0.0),
        TableSnapshot(0, 0),
        baseline,
        dt.date(2026, 9, 24),
        shadow_matched=0,
        shadow_records_per_day={},
    )

    assert result["match_ratio"] is None
