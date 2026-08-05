"""Shadow比較レポートのテスト(実装プラン修正6)。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from jstock_advisor.config.loader import load_config
from jstock_advisor.services.holding_decision_compare_service import (
    CompareRow,
    run_compare,
    write_compare_csv,
)
from jstock_advisor.services.provider_factory import build_mock_provider_bundle

_CFG = load_config()
_NOW = dt.datetime(2026, 8, 5, tzinfo=dt.UTC)
_PROVIDERS = build_mock_provider_bundle(_NOW)


def test_run_compare_returns_one_row_per_stock_code():
    rows = run_compare(["2914", "9861"], _PROVIDERS, _CFG, _NOW)
    assert [r.stock_code for r in rows] == ["2914", "9861"]


def test_run_compare_row_carries_detailed_fields():
    rows = run_compare(["2914"], _PROVIDERS, _CFG, _NOW)
    row = rows[0]
    assert row.legacy_category is not None
    assert row.new_category is not None
    assert row.new_score is not None
    assert row.coverage_overall is not None
    assert isinstance(row.hard_gate_triggered, bool)
    assert isinstance(row.positive_reasons, tuple)
    assert isinstance(row.negative_reasons, tuple)


def test_category_diff_matches_both_pass():
    row = CompareRow(
        stock_code="2914",
        legacy_category="HOLD",
        legacy_notified=False,
        legacy_reason_codes=(),
        new_category="STRONG_HOLD",
        new_score=90.0,
        new_notified=False,
        coverage_overall=1.0,
        hard_gate_triggered=False,
        hard_gate_reason_codes=(),
        positive_reasons=(),
        negative_reasons=(),
    )
    assert row.category_diff == "一致(両方見送り)"
    assert row.notification_diff is False


def test_category_diff_matches_both_notify():
    row = CompareRow(
        stock_code="2914",
        legacy_category="SELL",
        legacy_notified=True,
        legacy_reason_codes=("dividend_cut",),
        new_category="SELL_CONSIDERATION",
        new_score=-12.0,
        new_notified=True,
        coverage_overall=1.0,
        hard_gate_triggered=False,
        hard_gate_reason_codes=(),
        positive_reasons=(),
        negative_reasons=(),
    )
    assert row.category_diff == "一致(両方検討)"
    assert row.notification_diff is False


def test_category_diff_legacy_only():
    row = CompareRow(
        stock_code="2914",
        legacy_category="SELL",
        legacy_notified=True,
        legacy_reason_codes=("dividend_cut",),
        new_category="STRONG_HOLD",
        new_score=90.0,
        new_notified=False,
        coverage_overall=1.0,
        hard_gate_triggered=False,
        hard_gate_reason_codes=(),
        positive_reasons=(),
        negative_reasons=(),
    )
    assert row.category_diff == "差分(旧のみ検討)"
    assert row.notification_diff is True


def test_category_diff_new_only():
    row = CompareRow(
        stock_code="2914",
        legacy_category="HOLD",
        legacy_notified=False,
        legacy_reason_codes=(),
        new_category="SELL_CONSIDERATION",
        new_score=-12.0,
        new_notified=True,
        coverage_overall=1.0,
        hard_gate_triggered=False,
        hard_gate_reason_codes=(),
        positive_reasons=(),
        negative_reasons=(),
    )
    assert row.category_diff == "差分(新のみ検討)"
    assert row.notification_diff is True


def test_category_diff_data_error_takes_precedence():
    row = CompareRow(
        stock_code="2914",
        legacy_category="DATA_ERROR",
        legacy_notified=False,
        legacy_reason_codes=(),
        new_category=None,
        new_score=None,
        new_notified=False,
        coverage_overall=None,
        hard_gate_triggered=False,
        hard_gate_reason_codes=(),
        positive_reasons=(),
        negative_reasons=(),
        data_error="株価データを取得できません",
    )
    assert row.category_diff == "データ取得エラー"


def test_write_compare_csv_round_trips(tmp_path: Path):
    rows = run_compare(["2914"], _PROVIDERS, _CFG, _NOW)
    csv_path = tmp_path / "compare.csv"
    write_compare_csv(rows, csv_path)

    content = csv_path.read_text(encoding="utf-8-sig")
    lines = content.strip().splitlines()
    assert lines[0] == (
        "stock_code,legacy_category,new_category,score,category_diff,"
        "notification_diff,coverage_overall,hard_gate_triggered,"
        "hard_gate_reason_codes,positive_reasons,negative_reasons"
    )
    assert len(lines) == 2


def test_write_compare_csv_handles_empty_rows(tmp_path: Path):
    csv_path = tmp_path / "empty.csv"
    write_compare_csv([], csv_path)
    content = csv_path.read_text(encoding="utf-8-sig")
    assert len(content.strip().splitlines()) == 1
