"""Shadow比較レポートのテスト(実装プラン修正6、コードレビュー対応)。"""

from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    BaselineOrigin,
    ExecutionPlanReason,
    HoldingDecisionCategory,
    HoldingDecisionConfidenceLevel,
)
from jstock_advisor.domain.entities.holding_decision import (
    CompanyQualityScore,
    ComponentCoverage,
    HoldingDecisionHardGate,
    HoldingDecisionResult,
    InvestmentThesisScore,
    RiskDeductionScore,
)
from jstock_advisor.infrastructure.local_repository.holding_repository import HoldingRepository
from jstock_advisor.services.holding_decision_compare_service import (
    CompareRow,
    ShouldNotifyComparison,
    _is_first_evaluation,
    run_compare,
    summarize_compare_rows,
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
        "hard_gate_reason_codes,positive_reasons,negative_reasons,first_evaluation"
    )
    assert len(lines) == 2


def test_write_compare_csv_handles_empty_rows(tmp_path: Path):
    csv_path = tmp_path / "empty.csv"
    write_compare_csv([], csv_path)
    content = csv_path.read_text(encoding="utf-8-sig")
    assert len(content.strip().splitlines()) == 1


def _hd_result(
    *,
    baseline_origin: BaselineOrigin | None,
    coverage_ratio: float,
) -> HoldingDecisionResult:
    """Issue #258のAND判定テスト用に、baseline_originと
    investment_thesis.coverage_ratioだけを可変にした最小限の
    HoldingDecisionResultを組み立てる(他フィールドは判定に無関係な固定値)。
    """
    return HoldingDecisionResult(
        holding_decision_result_id="test-result-id",
        holding_id="test-holding-id",
        stock_code="2914",
        evaluated_at=_NOW,
        company_quality=CompanyQualityScore(score=40.0, coverage_ratio=1.0),
        investment_thesis=InvestmentThesisScore(score=40.0, coverage_ratio=coverage_ratio),
        risk_deduction=RiskDeductionScore(score=90.0, coverage_ratio=1.0),
        base_score=170.0,
        hard_gate=HoldingDecisionHardGate(triggered=False),
        final_score=90.0,
        display_value=90,
        category=HoldingDecisionCategory.STRONG_HOLD,
        coverage=ComponentCoverage(
            overall=1.0, company_quality=1.0, investment_thesis=coverage_ratio, risk_deduction=1.0
        ),
        confidence=HoldingDecisionConfidenceLevel.HIGH,
        should_notify=False,
        baseline_origin=baseline_origin,
        scoring_model_version=1,
        runtime_config_version=1,
        execution_plan_reason=ExecutionPlanReason.NORMAL_SHADOW,
    )


def test_is_first_evaluation_true_when_both_conditions_hold():
    """Issue #258受入条件: originとcoverageの両方が揃って初めてTrue。"""
    result = _hd_result(baseline_origin=BaselineOrigin.SYSTEM_INITIALIZED, coverage_ratio=0.8)
    assert _is_first_evaluation(result) is True


def test_is_first_evaluation_false_when_origin_only():
    """origin=SYSTEM_INITIALIZEDでもcoverage_ratio=1.0(欠測なし)なら
    初回評価として除外しない(origin単独判定だと実測8件の通常保有まで
    誤除外してしまう回帰を防ぐ)。"""
    result = _hd_result(baseline_origin=BaselineOrigin.SYSTEM_INITIALIZED, coverage_ratio=1.0)
    assert _is_first_evaluation(result) is False


def test_is_first_evaluation_false_when_coverage_only():
    """coverage_ratio<1.0でもbaseline_originがSYSTEM_INITIALIZED以外
    (=baseline確定済み)なら、単なるデータ欠測の2回目以降であり初回評価
    ではない(coverage単独判定だと#55 Decision 3対象を誤除外する)。"""
    result = _hd_result(baseline_origin=BaselineOrigin.HUMAN_APPROVED, coverage_ratio=0.8)
    assert _is_first_evaluation(result) is False


def test_is_first_evaluation_false_when_neither_condition_holds():
    result = _hd_result(baseline_origin=BaselineOrigin.HUMAN_APPROVED, coverage_ratio=1.0)
    assert _is_first_evaluation(result) is False


def test_is_first_evaluation_false_when_baseline_origin_is_none():
    result = _hd_result(baseline_origin=None, coverage_ratio=0.8)
    assert _is_first_evaluation(result) is False


def test_summarize_compare_rows_excludes_first_evaluation_by_default():
    rows = [
        _row(new_should_notify=False, legacy_should_notify=False),
        dataclasses.replace(
            _row(new_should_notify=False, legacy_should_notify=False), first_evaluation=True
        ),
    ]

    summary = summarize_compare_rows(rows)

    assert summary.total_rows == 2
    assert summary.excluded_first_evaluation == 1
    assert summary.included_rows == 1
    assert summary.should_notify_comparable_count == 1
    assert summary.should_notify_match_count == 1


def test_summarize_compare_rows_reports_zero_exclusions_explicitly():
    """除外0件でもexcluded_first_evaluationを必ず保持する(受入条件3: 黙って
    捨てない)。"""
    rows = [_row(new_should_notify=False, legacy_should_notify=False)]

    summary = summarize_compare_rows(rows)

    assert summary.total_rows == 1
    assert summary.excluded_first_evaluation == 0
    assert summary.included_rows == 1


def test_summarize_compare_rows_counts_not_comparable_rows_separately():
    """非保有銘柄(NOT_COMPARABLE)はcomparable/matchの分母・分子どちらにも
    含めない。"""
    rows = [
        _row(legacy_should_notify=None, new_should_notify=True),
        _row(new_should_notify=True, legacy_should_notify=False),
    ]

    summary = summarize_compare_rows(rows)

    assert summary.total_rows == 2
    assert summary.excluded_first_evaluation == 0
    assert summary.should_notify_comparable_count == 1
    assert summary.should_notify_match_count == 0
