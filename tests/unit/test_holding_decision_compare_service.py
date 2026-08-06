"""Shadow比較レポートのテスト(実装プラン修正6、コードレビュー対応)。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from jstock_advisor.config.loader import load_config
from jstock_advisor.infrastructure.local_repository.holding_repository import HoldingRepository
from jstock_advisor.services.holding_decision_compare_service import (
    CompareRow,
    ShouldNotifyComparison,
    run_compare,
    write_compare_csv,
)
from jstock_advisor.services.portfolio_service import PortfolioService
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


def test_run_compare_non_holding_stock_does_not_evaluate_legacy(store_dir: Path):
    """非保有銘柄は旧方式(SellSignalService)を評価しない(コードレビュー対応)。"""
    portfolio = PortfolioService(holding_repository=HoldingRepository(store_dir=store_dir))
    rows = run_compare(["2914"], _PROVIDERS, _CFG, _NOW, portfolio_service=portfolio)
    row = rows[0]
    assert row.legacy_category == "NOT_EVALUATED_NON_HOLDING"
    assert row.legacy_should_notify is None
    # 新方式は非保有でも評価される。
    assert row.new_score is not None
    assert row.should_notify_diff == ShouldNotifyComparison.NOT_COMPARABLE
    assert row.category_diff == "対象外(非保有・比較不能)"


def _row(
    *,
    legacy_category: str = "HOLD",
    legacy_should_notify: bool | None = False,
    new_category: str | None = "STRONG_HOLD",
    new_score: float | None = 90.0,
    new_should_notify: bool | None = False,
    data_error: str | None = None,
) -> CompareRow:
    return CompareRow(
        stock_code="2914",
        legacy_category=legacy_category,
        legacy_should_notify=legacy_should_notify,
        legacy_reason_codes=(),
        new_category=new_category,
        new_score=new_score,
        new_should_notify=new_should_notify,
        coverage_overall=1.0,
        hard_gate_triggered=False,
        hard_gate_reason_codes=(),
        positive_reasons=(),
        negative_reasons=(),
        data_error=data_error,
    )


def test_category_diff_matches_both_pass():
    row = _row(legacy_should_notify=False, new_should_notify=False)
    assert row.category_diff == "一致(両方見送り)"
    assert row.should_notify_diff == ShouldNotifyComparison.MATCH


def test_category_diff_matches_both_notify():
    row = _row(
        legacy_category="SELL",
        legacy_should_notify=True,
        new_category="SELL_CONSIDERATION",
        new_score=-12.0,
        new_should_notify=True,
    )
    assert row.category_diff == "一致(両方検討)"
    assert row.should_notify_diff == ShouldNotifyComparison.MATCH


def test_category_diff_legacy_only():
    row = _row(legacy_category="SELL", legacy_should_notify=True, new_should_notify=False)
    assert row.category_diff == "差分(旧のみ検討)"
    assert row.should_notify_diff == ShouldNotifyComparison.DIFFERENT


def test_category_diff_new_only():
    row = _row(
        legacy_should_notify=False,
        new_category="SELL_CONSIDERATION",
        new_score=-12.0,
        new_should_notify=True,
    )
    assert row.category_diff == "差分(新のみ検討)"
    assert row.should_notify_diff == ShouldNotifyComparison.DIFFERENT


def test_category_diff_data_error_takes_precedence():
    row = _row(
        legacy_category="DATA_ERROR",
        legacy_should_notify=False,
        new_category=None,
        new_score=None,
        new_should_notify=False,
        data_error="株価データを取得できません",
    )
    assert row.category_diff == "データ取得エラー"


def test_category_diff_not_comparable_when_legacy_is_none():
    row = _row(
        legacy_category="NOT_EVALUATED_NON_HOLDING",
        legacy_should_notify=None,
        new_should_notify=True,
    )
    assert row.category_diff == "対象外(非保有・比較不能)"
    assert row.should_notify_diff == ShouldNotifyComparison.NOT_COMPARABLE


def test_write_compare_csv_round_trips(tmp_path: Path):
    rows = run_compare(["2914"], _PROVIDERS, _CFG, _NOW)
    csv_path = tmp_path / "compare.csv"
    write_compare_csv(rows, csv_path)

    content = csv_path.read_text(encoding="utf-8-sig")
    lines = content.strip().splitlines()
    assert lines[0] == (
        "stock_code,legacy_category,new_category,score,category_diff,"
        "should_notify_diff,coverage_overall,hard_gate_triggered,"
        "hard_gate_reason_codes,positive_reasons,negative_reasons"
    )
    assert len(lines) == 2


def test_write_compare_csv_handles_empty_rows(tmp_path: Path):
    csv_path = tmp_path / "empty.csv"
    write_compare_csv([], csv_path)
    content = csv_path.read_text(encoding="utf-8-sig")
    assert len(content.strip().splitlines()) == 1
