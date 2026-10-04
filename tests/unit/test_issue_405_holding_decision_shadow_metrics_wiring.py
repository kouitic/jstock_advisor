"""Issue #405 PR-4: 保有判断(holding_decision_notification_builder と holdings_watchlist_handler)の
`*_metrics` の hoist・exit_price_range の受け渡し・算出が、取り違え・引数落としなく
Recommendation へ載ることを固定する。

PR-1(BUY)・PR-2(SELL)・PR-3(利確)と同じ方式(MANAGER 判断の選択肢 A1 + A3。
共有部品 = tests/support/shadow_metrics_wiring.py)を適用する。保有判断は構造が異なる。

    builder   `build_holding_decision_recommendation()` は exit_price_range を**算出せず**、
              呼び出し元から引数で受け取る。builder 自身が持つ隔離は `*_metrics` の 9 件だけ
    handler   `_notify_holding_decision_and_build_result()` が exit_price_range を
              `isolated_shadow_computation` で 1 回算出(7 引数)し、builder へ渡す

  A1  builder: 渡した exit_price_range と snapshot から、テスト側で `*_to_metrics` を同じ引数列で
      再計算(oracle)し、Recommendation の各 field と完全一致することを assert する。exit_price_range
      は「渡された結果そのもの」から oracle を作り(再計算しない)、`*_metrics` の第 1 引数・9 項目の
      コピー元が渡された結果であることを検査する(渡す結果は、再計算した結果 2 種と、再計算とは
      異なる失敗時の結果 1 種)。
      handler: `evaluate_exit_price_range` が受け取った 7 引数と、builder が受け取った 6 引数を
      捕捉し、snapshot・holding・config から独立に組んだ期待値と完全一致することを assert する。
      算出が失敗したときの代替結果(NOT_EVALUATED)の全 field も期待値と比べる。
  A3  A1 が空振りしない前提(取り違えで差が出る・引数を落とすと差が出る・値が None でない)を
      fixture ごとに固定する。

## このテストが検査している範囲 / していない範囲(★ 正確に)

している    holding_decision_notification_builder.py の `*_metrics` 9 種の hoist 9 件と、受け取った
            exit_price_range の 9 項目のコピー。holdings_watchlist_handler.py の
            exit_price_range の算出 1 件(7 引数)と代替結果、builder への受け渡し(6 引数)。
            2 ファイルの `isolated_shadow_*` の
            呼び出しが、検査対象の 9 件 + 1 件だけであること(増えたら落ちる)。
            mock provider の全 4 銘柄 × EVALUATED(取得単価 2 通り)・NOT_EVALUATED・失敗時の結果。
していない  ・`isolated_shadow_*` を介さず手書きの try/except で隔離している箇所
            ・src/ の外からの呼び出し
            ・hoist 側で `**dict` の展開や、式(関数呼び出しの結果)を直接渡している形
            ・Recommendation → DecisionSnapshot の複写での取り違え
            ・`*_to_metrics` / `evaluate_exit_price_range` 自身の算出内容の正しさ
              (各 domain のテスト)
            ・builder の判定(recommendation_type・売却価格・理由文・fair value 系 field)そのもの
            ・handler の通知の送信・保存・DecisionSnapshot の保存
              (validation の実行文脈で保存をスキップし、
              通知は無効にして呼ぶ)
            ・`*_metrics` という名前でも隔離を介さない代入
              (本ファイルの検査対象は `isolated_shadow_*`)
            ・builder の引数のうち bars のように mock の構造上値が区別できない列
              (本 PR の引数には無い)

fixture は mock provider のみ。銘柄コードは mock の一覧から機械的に取り、実在の銘柄コード・社名を
本ファイルへ書き込まない(銘柄名は架空の「テスト銘柄」)。時刻は固定。Production・AWS へは触れない。
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import inspect
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import (
    AccountType,
    ExecutionMode,
    ExecutionPlanReason,
    HoldingDecisionCategory,
    HoldingDecisionConfidenceLevel,
    PriceRangeEvaluationState,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.exit_price_range import ExitPriceRangeResult
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holding_decision import (
    CompanyQualityScore,
    ComponentCoverage,
    HoldingDecisionHardGate,
    HoldingDecisionResult,
    InvestmentThesisScore,
    RiskDeductionScore,
)
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.signals.exit_price_range import (
    evaluate_exit_price_range,
    exit_price_range_result_to_metrics,
)
from jstock_advisor.lambda_handlers import holdings_watchlist_handler as handler_module
from jstock_advisor.providers import mock_fixtures
from jstock_advisor.services import holding_decision_notification_builder as builder_module
from jstock_advisor.services.holding_decision_notification_builder import (
    build_holding_decision_recommendation,
)
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot
from tests.support.shadow_metrics_wiring import (
    EXIT_PRICE_RANGE_RECOMMENDATION_FIELDS,
    ComputationSpec,
    MetricsSpec,
    argument_drop_blind_spots,
    common_metrics_specs,
    mismatched_computation_fields,
    mismatched_fields,
    swap_blind_pairs,
)

_CONFIG = load_config()
_CALENDAR = BusinessCalendar.from_config(_CONFIG.holiday_calendar)
# 営業日の大引け後(水曜 16:00 JST)に固定する(wall clock を使わない)。
_NOW = dt.datetime(2026, 9, 2, 16, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
_MOCK_STOCK_CODES = tuple(mock_fixtures.MOCK_STOCKS)
# 取得単価を 2 通りにして、exit_price_range が取得単価に依存する経路を複数の値で通す。
_AVERAGE_PURCHASE_PRICES = (Decimal("1000"), Decimal("100"))
_RULE_VERSION = "rule-version-for-test"

_HOLDING_DECISION_METRICS_FIELDS = (
    "historical_valuation_metrics",
    "timing_metrics",
    "earnings_surprise_metrics",
    "earnings_trend_metrics",
    "entry_price_range_metrics",
    "market_metrics",
    "sector_metrics",
    "environment_metrics",
    "exit_price_range_metrics",
)
_EXIT_COPY_FIELDS = tuple(EXIT_PRICE_RANGE_RECOMMENDATION_FIELDS)
_BUILDER_COVERED_NAMES = frozenset(_HOLDING_DECISION_METRICS_FIELDS)
_HANDLER_COVERED_NAMES = frozenset({"exit_price_range"})

#: `evaluate_exit_price_range` の引数名(handler が位置引数で渡す 7 つ)。
_EXIT_ARGUMENT_NAMES = tuple(inspect.signature(evaluate_exit_price_range).parameters)
#: builder の呼び出しで handler が渡す先頭 6 つの位置引数の名前。
_BUILDER_ARGUMENT_NAMES = (
    "holding",
    "result",
    "snapshot",
    "rule_version",
    "config",
    "exit_price_range",
)


def _holding(stock_code: str, average_purchase_price: Decimal) -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        stock_name="テスト銘柄",
        shares=100,
        average_purchase_price=average_purchase_price,
        total_purchase_amount=average_purchase_price * 100,
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _decision_result(stock_code: str) -> HoldingDecisionResult:
    return HoldingDecisionResult(
        holding_decision_result_id="holding-decision-wiring-test",
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        evaluated_at=_NOW,
        company_quality=CompanyQualityScore(score=30.0, coverage_ratio=1.0),
        investment_thesis=InvestmentThesisScore(score=25.0, coverage_ratio=1.0),
        risk_deduction=RiskDeductionScore(score=10.0, coverage_ratio=1.0),
        base_score=45.0,
        hard_gate=HoldingDecisionHardGate(triggered=False),
        final_score=45.0,
        display_value=45,
        category=HoldingDecisionCategory.SELL_CONSIDERATION,
        coverage=ComponentCoverage(
            overall=1.0, company_quality=1.0, investment_thesis=1.0, risk_deduction=1.0
        ),
        confidence=HoldingDecisionConfidenceLevel.HIGH,
        should_notify=True,
        scoring_model_version=1,
        runtime_config_version=1,
        execution_plan_reason=ExecutionPlanReason.NORMAL_ACTIVE,
    )


def _snapshot(stock_code: str, *, not_evaluated: bool) -> Any:
    providers = build_mock_provider_bundle(_NOW)
    snapshot, error = build_stock_snapshot(
        providers, stock_code, _NOW, _CONFIG, business_calendar=_CALENDAR
    )
    assert snapshot is not None, error
    if not_evaluated:
        # 公正価値レンジを「売買判断に使えない」状態にして、exit_price_range を
        # NOT_EVALUATED(reason_codes が付く側の経路)へ到達させる。
        snapshot = dataclasses.replace(
            snapshot,
            fair_value_range=snapshot.fair_value_range.model_copy(
                update={"usable_for_trading_judgment": False}
            ),
        )
    return snapshot


def _recomputed_exit(snapshot: Any, average_purchase_price: Decimal) -> ExitPriceRangeResult:
    """handler と同じ 7 引数でテスト側が再計算した exit_price_range(oracle の入力)。"""
    return evaluate_exit_price_range(
        snapshot.fair_value_range,
        snapshot.historical_valuation,
        snapshot.timing,
        average_purchase_price,
        snapshot.current_price,
        _NOW,
        _CONFIG.entry_exit_price.exit,
    )


def _failure_exit(snapshot: Any) -> ExitPriceRangeResult:
    """算出が失敗したときの代替結果(handler の on_failure と同じ構成)。再計算の結果とは異なる。"""
    return ExitPriceRangeResult(
        state=PriceRangeEvaluationState.NOT_EVALUATED,
        current_price=snapshot.current_price,
        reason_codes=("SHADOW_COMPUTATION_FAILED:ZeroDivisionError",),
        evaluated_at=_NOW,
        model_version=_CONFIG.entry_exit_price.exit.model_version,
    )


def _metrics_specs(
    snapshot: Any, passed_exit: ExitPriceRangeResult, average_purchase_price: Decimal
) -> list[MetricsSpec]:
    """builder の oracle。exit_price_range_metrics の第 1 引数は「渡された結果そのもの」。"""
    return [
        *common_metrics_specs(snapshot, _CONFIG),
        MetricsSpec(
            "exit_price_range_metrics",
            exit_price_range_result_to_metrics,
            (
                passed_exit,
                snapshot.fair_value_range,
                snapshot.historical_valuation,
                snapshot.timing,
                average_purchase_price,
                _CONFIG.entry_exit_price.exit,
            ),
        ),
    ]


def _copy_spec(passed_exit: ExitPriceRangeResult) -> ComputationSpec:
    """渡された結果から、Recommendation へコピーされる 9 項目の期待値を作る(再計算しない)。"""
    return ComputationSpec(
        "passed_exit_price_range",
        lambda: passed_exit,
        (),
        EXIT_PRICE_RANGE_RECOMMENDATION_FIELDS,
    )


class _BuilderWired:
    """builder の 1 つの fixture(銘柄 × 取得単価 × 渡す exit_price_range の種類)。"""

    def __init__(self, stock_code: str, average_purchase_price: Decimal, kind: str) -> None:
        self.kind = kind
        snapshot = _snapshot(stock_code, not_evaluated=kind == "not_evaluated")
        if kind == "failure":
            passed = _failure_exit(snapshot)
        else:
            passed = _recomputed_exit(snapshot, average_purchase_price)
        self.passed = passed
        self.snapshot = snapshot
        self.recommendation: Any = build_holding_decision_recommendation(
            _holding(stock_code, average_purchase_price),
            _decision_result(stock_code),
            snapshot,
            _RULE_VERSION,
            _CONFIG,
            passed,
        )
        self.metrics_specs = _metrics_specs(snapshot, passed, average_purchase_price)
        self.copy_spec = _copy_spec(passed)


_EVALUATED = [(c, p, "evaluated") for c in _MOCK_STOCK_CODES for p in _AVERAGE_PURCHASE_PRICES]
_NOT_EVALUATED = [(c, _AVERAGE_PURCHASE_PRICES[0], "not_evaluated") for c in _MOCK_STOCK_CODES]
_FAILURE = [(c, _AVERAGE_PURCHASE_PRICES[0], "failure") for c in _MOCK_STOCK_CODES]
_ALL = [*_EVALUATED, *_NOT_EVALUATED, *_FAILURE]


def _ids(fixtures: list[tuple[str, Decimal, str]]) -> list[str]:
    return [f"{kind}-{index}" for index, (_, _, kind) in enumerate(fixtures)]


@pytest.fixture(params=_ALL, ids=_ids(_ALL))
def wired(request: pytest.FixtureRequest) -> _BuilderWired:
    """evaluated / not_evaluated / failure の全 fixture(A1 はすべてで検査する)。"""
    return _BuilderWired(*request.param)


@pytest.fixture(params=_FAILURE, ids=_ids(_FAILURE))
def failure(request: pytest.FixtureRequest) -> _BuilderWired:
    """渡す結果が失敗時の代替結果(再計算とは異なる)である fixture。"""
    return _BuilderWired(*request.param)


@pytest.fixture(params=_EVALUATED, ids=_ids(_EVALUATED))
def evaluated(request: pytest.FixtureRequest) -> _BuilderWired:
    """exit_price_range が実際に評価された fixture(5 価格・取得単価への依存を検査する側)。"""
    return _BuilderWired(*request.param)


# --- 検査の対象が、実際の隔離と一致する ---------------------------------------------------


def _isolated_names(module: Any) -> list[str]:
    """モジュールの `isolated_shadow_*` の呼び出しの第 1 引数(名前)。"""
    tree = ast.parse(Path(inspect.getsourcefile(module) or "").read_text("utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        func_name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if (
            func_name in {"isolated_shadow_observation", "isolated_shadow_computation"}
            and node.args
        ):
            first = node.args[0]
            assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
                "第 1 引数が文字列定数でない隔離の呼び出しがある(検査の対象を機械的に決められない)"
            )
            names.append(first.value)
    return names


def test_the_covered_computations_are_exactly_the_isolated_calls_in_both_modules() -> None:
    """★ 2 ファイルの `isolated_shadow_*` が、検査対象の 9 件 + 1 件だけであること。

    builder は exit_price_range を算出しない(`exit_price_range` の隔離は builder に無い)。
    隔離の呼び出しが増えたのに本テストが更新されない、という取りこぼしを防ぐ。AST で
    `isolated_shadow_*` を数えるため、手書きの try/except による隔離は数えない。
    """
    builder_names = _isolated_names(builder_module)
    handler_names = _isolated_names(handler_module)

    assert sorted(builder_names) == sorted(_BUILDER_COVERED_NAMES)
    assert sorted(handler_names) == sorted(_HANDLER_COVERED_NAMES)
    assert len(builder_names) == len(set(builder_names)) == 9
    assert len(handler_names) == 1
    assert "exit_price_range" not in builder_names


def _call_names(module: Any, name: str) -> int:
    tree = ast.parse(Path(inspect.getsourcefile(module) or "").read_text("utf-8"))
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == name) or (
                isinstance(func, ast.Attribute) and func.attr == name
            ):
                count += 1
    return count


def test_the_handler_calls_the_builder_and_the_exit_computation_exactly_once() -> None:
    """handler モジュール内の builder の呼び出しと、exit の算出の呼び出しが、それぞれ 1 回。

    ★ 数えているのは handler モジュールの中だけである。src 全体の呼び出し元は、下のテストが数える。
    """
    assert _call_names(handler_module, "build_holding_decision_recommendation") == 1
    assert _call_names(handler_module, "evaluate_exit_price_range") == 1


def _src_call_counts(name: str) -> dict[str, int]:
    """src/jstock_advisor/ 配下の全 .py で、`name` を関数として呼ぶ箇所の数(ファイル別。AST)。"""
    root = Path(inspect.getsourcefile(handler_module) or "").parents[1]
    counts: dict[str, int] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text("utf-8"))
        found = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (isinstance(func, ast.Name) and func.id == name) or (
                    isinstance(func, ast.Attribute) and func.attr == name
                ):
                    found += 1
        if found:
            counts[path.relative_to(root).as_posix()] = found
    return counts


def test_the_call_sites_in_the_whole_src_are_the_known_ones() -> None:
    """★ src 全体では、`evaluate_exit_price_range` の呼び出し元は 3 箇所である。

    保有判断の handler(本ファイルが検査)・SELL(sell_signal_service。#405 PR-2 が検査)・
    利確(profit_taking_service。#405 PR-3 が検査)。「exit の算出は 1 箇所だけ」ではない。
    引数列を変えるときは 3 経路すべてを見直すこと。4 箇所目が増えたら落ちる(引数列の検査が要る)。
    builder(`build_holding_decision_recommendation`)の呼び出し元は、src 全体で handler の 1 箇所。
    """
    assert _src_call_counts("evaluate_exit_price_range") == {
        "lambda_handlers/holdings_watchlist_handler.py": 1,
        "services/profit_taking_service.py": 1,
        "services/sell_signal_service.py": 1,
    }
    assert _src_call_counts("build_holding_decision_recommendation") == {
        "lambda_handlers/holdings_watchlist_handler.py": 1,
    }


def test_the_spec_covers_exactly_the_nine_metrics_fields_and_nine_copied_fields(
    wired: _BuilderWired,
) -> None:
    assert tuple(spec.field for spec in wired.metrics_specs) == _HOLDING_DECISION_METRICS_FIELDS
    assert tuple(wired.copy_spec.field_map) == _EXIT_COPY_FIELDS
    assert len(_EXIT_COPY_FIELDS) == 9


def test_the_fixtures_exercise_each_exit_path(wired: _BuilderWired) -> None:
    """空振り防止: fixture の種類ごとに、渡した結果の状態が実際に期待どおりであること。"""
    recommendation = wired.recommendation
    if wired.kind == "evaluated":
        assert recommendation.exit_price_range_state == PriceRangeEvaluationState.EVALUATED
        assert recommendation.exit_price_range_partial_low_price is not None
    else:
        assert recommendation.exit_price_range_state == PriceRangeEvaluationState.NOT_EVALUATED
        assert recommendation.exit_price_range_reason_codes
        assert recommendation.exit_price_range_partial_low_price is None
    if wired.kind == "failure":
        assert recommendation.exit_price_range_reason_codes == (
            "SHADOW_COMPUTATION_FAILED:ZeroDivisionError",
        )


# --- A3: oracle 一致が空振りしないための前提 ---------------------------------------


def test_a3_swapping_any_two_metrics_changes_the_result(evaluated: _BuilderWired) -> None:
    """★ 前提: 9 種の oracle が互いに異なる(等しい組があると、その取り違えが生き残る)。"""
    assert swap_blind_pairs(evaluated.metrics_specs) == []


def test_a3_dropping_any_metrics_argument_changes_the_result(evaluated: _BuilderWired) -> None:
    """★ 前提: `*_to_metrics` の引数を 1 つ None にすると oracle が変わる(または例外になる)。
    exit_price_range_metrics の第 1 引数(渡された結果)を落とした場合も含む。
    """
    assert argument_drop_blind_spots(evaluated.metrics_specs) == []


def test_a3_the_failure_result_differs_from_a_recomputation(failure: _BuilderWired) -> None:
    """★ 前提: 失敗時の結果は再計算の結果と 9 項目のうち少なくとも 1 つ異なる。
    等しいと「渡された結果ではなく再計算した値を使う」変異が区別できない。"""
    recomputed = _recomputed_exit(
        failure.snapshot, failure.recommendation.average_purchase_price_at_recommendation
    )
    spec = _copy_spec(recomputed)
    assert mismatched_computation_fields(failure.recommendation, spec) != {}


# --- A1: Recommendation が oracle と一致する ---------------------------------------


def test_a1_every_metrics_field_matches_the_oracle(wired: _BuilderWired) -> None:
    """★ 本体(metrics): 9 つの `*_metrics` を、同じ引数列で再計算した値と完全一致で比較する。"""
    assert mismatched_fields(wired.recommendation, wired.metrics_specs) == {}


def test_a1_the_nine_exit_fields_are_copied_from_the_passed_result(wired: _BuilderWired) -> None:
    """★ 本体(コピー): 渡された exit_price_range の 9 項目が Recommendation と完全一致する。"""
    assert mismatched_computation_fields(wired.recommendation, wired.copy_spec) == {}


# --- 検査そのものの反証(Recommendation 側へ変異を模して、検出できること) -------------


def test_the_check_detects_a_swap_between_market_and_sector(wired: _BuilderWired) -> None:
    rec = wired.recommendation
    swapped = rec.model_copy(
        update={"market_metrics": rec.sector_metrics, "sector_metrics": rec.market_metrics}
    )
    assert set(mismatched_fields(swapped, wired.metrics_specs)) == {
        "market_metrics",
        "sector_metrics",
    }


def test_the_check_detects_a_swap_between_earnings_surprise_and_trend(
    wired: _BuilderWired,
) -> None:
    rec = wired.recommendation
    swapped = rec.model_copy(
        update={
            "earnings_surprise_metrics": rec.earnings_trend_metrics,
            "earnings_trend_metrics": rec.earnings_surprise_metrics,
        }
    )
    assert set(mismatched_fields(swapped, wired.metrics_specs)) == {
        "earnings_surprise_metrics",
        "earnings_trend_metrics",
    }


def test_the_check_detects_a_swap_between_entry_and_exit_price_range_metrics(
    wired: _BuilderWired,
) -> None:
    rec = wired.recommendation
    swapped = rec.model_copy(
        update={
            "entry_price_range_metrics": rec.exit_price_range_metrics,
            "exit_price_range_metrics": rec.entry_price_range_metrics,
        }
    )
    assert set(mismatched_fields(swapped, wired.metrics_specs)) == {
        "entry_price_range_metrics",
        "exit_price_range_metrics",
    }


def test_the_check_detects_timing_without_current_price(wired: _BuilderWired) -> None:
    timing = next(s for s in wired.metrics_specs if s.field == "timing_metrics")
    index = timing.argument_names.index("current_price")
    degraded = wired.recommendation.model_copy(
        update={"timing_metrics": timing.oracle_with_argument_dropped(index)}
    )
    assert set(mismatched_fields(degraded, wired.metrics_specs)) == {"timing_metrics"}


@pytest.mark.parametrize("field", _HOLDING_DECISION_METRICS_FIELDS)
def test_the_check_detects_a_missing_metrics_hoist_for_every_field(
    wired: _BuilderWired, field: str
) -> None:
    omitted = wired.recommendation.model_copy(update={field: {}})
    assert set(mismatched_fields(omitted, wired.metrics_specs)) == {field}


def test_the_check_detects_exit_metrics_without_the_average_purchase_price(
    evaluated: _BuilderWired,
) -> None:
    spec = next(s for s in evaluated.metrics_specs if s.field == "exit_price_range_metrics")
    index = spec.argument_names.index("average_purchase_price")
    degraded = evaluated.recommendation.model_copy(
        update={"exit_price_range_metrics": spec.oracle_with_argument_dropped(index)}
    )
    assert set(mismatched_fields(degraded, evaluated.metrics_specs)) == {"exit_price_range_metrics"}


def test_the_check_detects_exit_metrics_computed_from_a_different_result(
    wired: _BuilderWired,
) -> None:
    """渡された結果ではなく、別の結果(再計算 / 失敗時)から作った metrics を検出する。"""
    other = (
        _recomputed_exit(
            wired.snapshot, wired.recommendation.average_purchase_price_at_recommendation
        )
        if wired.kind == "failure"
        else _failure_exit(wired.snapshot)
    )
    exit_spec = next(s for s in wired.metrics_specs if s.field == "exit_price_range_metrics")
    other_metrics = exit_price_range_result_to_metrics(other, *exit_spec.args[1:])
    assert other_metrics != exit_spec.oracle(), "この fixture では 2 つの結果の metrics が等しい"
    degraded = wired.recommendation.model_copy(update={"exit_price_range_metrics": other_metrics})
    assert set(mismatched_fields(degraded, wired.metrics_specs)) == {"exit_price_range_metrics"}


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("exit_price_range_partial_low_price", "exit_price_range_partial_high_price"),
        ("exit_price_range_strong_price", "exit_price_range_exit_review_price"),
        ("exit_price_range_downside_review_price", "exit_price_range_exit_review_price"),
    ],
)
def test_the_check_detects_a_swap_between_exit_price_fields(
    evaluated: _BuilderWired, first: str, second: str
) -> None:
    rec = evaluated.recommendation
    assert getattr(rec, first) != getattr(rec, second), "この fixture では 2 つの価格が等しい"
    swapped = rec.model_copy(update={first: getattr(rec, second), second: getattr(rec, first)})
    assert set(mismatched_computation_fields(swapped, evaluated.copy_spec)) == {first, second}


def _reset_value(current: Any) -> Any:
    return type(current)() if isinstance(current, (tuple, list)) else None


@pytest.mark.parametrize("field", _EXIT_COPY_FIELDS)
def test_the_check_detects_each_copied_field_being_reset(wired: _BuilderWired, field: str) -> None:
    current = getattr(wired.recommendation, field)
    reset = _reset_value(current)
    degraded = wired.recommendation.model_copy(update={field: reset})
    detected = set(mismatched_computation_fields(degraded, wired.copy_spec))
    assert detected == ({field} if reset != current else set())


def test_every_copied_field_is_detectable_on_at_least_one_fixture() -> None:
    """★ 9 項目のどれも、欠落を検出できる fixture が 1 つ以上あること(空振りしない)。"""
    all_wired = [_BuilderWired(*args) for args in _ALL]
    undetectable = [
        field
        for field in _EXIT_COPY_FIELDS
        if not any(
            _reset_value(getattr(w.recommendation, field)) != getattr(w.recommendation, field)
            for w in all_wired
        )
    ]
    assert undetectable == []


# =====================================================================================
# handler: exit_price_range の算出(7 引数)と、builder への受け渡し(6 引数)
# =====================================================================================

_MISSING = object()


class _FakeNotification:
    def check_data_quality_eligibility(self, recommendation: Any, now: Any) -> Any:
        return SimpleNamespace(eligible=True)


class _FakeRuleVersion:
    def get_active_version_or(self, default: str) -> str:
        return _RULE_VERSION


class _Unused:
    """validation の実行文脈では保存されない repository の代わり(呼ばれたら失敗)。"""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"保存先が呼ばれた: {name}")


def mismatched_arguments(
    captured: tuple[Any, ...], expected: tuple[Any, ...], names: tuple[str, ...]
) -> dict[str, tuple[Any, Any]]:
    """捕捉した位置引数が期待値と一致しない引数名 → (実際の値, 期待値)。個数の過不足も不一致。"""
    mismatches: dict[str, tuple[Any, Any]] = {}
    for index, name in enumerate(names):
        actual = captured[index] if index < len(captured) else _MISSING
        wanted = expected[index] if index < len(expected) else _MISSING
        if actual != wanted:
            mismatches[name] = (actual, wanted)
    if len(captured) != len(names):
        mismatches["__count__"] = (len(captured), len(names))
    return mismatches


class _HandlerWired:
    """handler の 1 つの fixture。evaluate と builder が受け取った引数を捕捉する。"""

    def __init__(
        self, stock_code: str, average_purchase_price: Decimal, *, fail: bool = False
    ) -> None:
        self.snapshot = _snapshot(stock_code, not_evaluated=False)
        self.holding = _holding(stock_code, average_purchase_price)
        self.result = _decision_result(stock_code)
        self.exit_calls: list[tuple[Any, ...]] = []
        self.builder_calls: list[tuple[Any, ...]] = []
        self.built: list[Any] = []

        def _exit_spy(*args: Any, **kwargs: Any) -> Any:
            assert not kwargs, "evaluate_exit_price_range をキーワード引数で呼んでいる"
            self.exit_calls.append(args)
            if fail:
                raise ZeroDivisionError("injected exit_price_range failure")
            return evaluate_exit_price_range(*args)

        def _builder_spy(*args: Any, **kwargs: Any) -> Any:
            self.builder_calls.append(args)
            recommendation = build_holding_decision_recommendation(*args, **kwargs)
            self.built.append(recommendation)
            return recommendation

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(handler_module, "evaluate_exit_price_range", _exit_spy)
            monkeypatch.setattr(
                handler_module, "build_holding_decision_recommendation", _builder_spy
            )
            handler_module._notify_holding_decision_and_build_result(
                self.holding,
                _NOW,
                self.result,
                self.snapshot,
                _CONFIG,
                _Unused(),  # type: ignore[arg-type]
                _Unused(),  # type: ignore[arg-type]
                _FakeNotification(),  # type: ignore[arg-type]
                False,
                _FakeRuleVersion(),  # type: ignore[arg-type]
                None,
                execution_context=ExecutionContext(mode=ExecutionMode.VALIDATION),
            )
        # 呼ばれていない fixture では、以下の検査が空振りする(vacuous)。
        assert len(self.exit_calls) == 1, "evaluate_exit_price_range が 1 回呼ばれていない"
        assert len(self.builder_calls) == 1, "builder が 1 回呼ばれていない"
        self.expected_exit_arguments: tuple[Any, ...] = (
            self.snapshot.fair_value_range,
            self.snapshot.historical_valuation,
            self.snapshot.timing,
            self.holding.average_purchase_price,
            self.snapshot.current_price,
            _NOW,
            _CONFIG.entry_exit_price.exit,
        )
        self.passed_exit: ExitPriceRangeResult = self.builder_calls[0][5]


_HANDLER_FIXTURES = [(c, p) for c in _MOCK_STOCK_CODES for p in _AVERAGE_PURCHASE_PRICES]


@pytest.fixture(
    params=_HANDLER_FIXTURES, ids=[f"handler-{i}" for i in range(len(_HANDLER_FIXTURES))]
)
def handler_wired(request: pytest.FixtureRequest) -> _HandlerWired:
    stock_code, price = request.param
    return _HandlerWired(stock_code, price)


def test_the_handler_argument_names_are_the_seven_and_six_checked_ones(
    handler_wired: _HandlerWired,
) -> None:
    assert len(_EXIT_ARGUMENT_NAMES) == 7
    assert len(handler_wired.exit_calls[0]) == 7
    assert len(_BUILDER_ARGUMENT_NAMES) == 6
    assert len(handler_wired.builder_calls[0]) >= 6


def test_a3_every_exit_argument_is_present_and_the_swap_prone_pairs_differ(
    handler_wired: _HandlerWired,
) -> None:
    """★ 前提: 7 引数がどれも None でなく、取り違えやすい組(現在値と取得単価)の値が異なる。"""
    expected = handler_wired.expected_exit_arguments
    for name, value in zip(_EXIT_ARGUMENT_NAMES, expected, strict=True):
        assert value is not None, f"{name} が None で、落としても差が出ない"
    assert expected[3] != expected[4], "取得単価と現在値が等しく、取り違えを検出できない"
    assert expected[2] != expected[1], "timing と historical_valuation が等しい"


def test_a1_the_handler_passes_the_seven_expected_arguments_to_the_exit_computation(
    handler_wired: _HandlerWired,
) -> None:
    """★ 本体(算出): `evaluate_exit_price_range` が受け取った 7 引数が、独立の期待値と一致する。"""
    assert (
        mismatched_arguments(
            handler_wired.exit_calls[0],
            handler_wired.expected_exit_arguments,
            _EXIT_ARGUMENT_NAMES,
        )
        == {}
    )


def test_a1_the_handler_passes_the_computed_result_and_the_other_arguments_to_the_builder(
    handler_wired: _HandlerWired,
) -> None:
    """★ 本体(受け渡し): builder が受け取った 6 引数が、期待値と一致する。exit_price_range は
    算出した結果そのもの(再計算した値と等しいこと)。"""
    w = handler_wired
    expected = (
        w.holding,
        w.result,
        w.snapshot,
        _RULE_VERSION,
        _CONFIG,
        _recomputed_exit(w.snapshot, w.holding.average_purchase_price),
    )
    assert mismatched_arguments(w.builder_calls[0][:6], expected, _BUILDER_ARGUMENT_NAMES) == {}


def test_the_recommendation_carries_the_computed_exit_price_range(
    handler_wired: _HandlerWired,
) -> None:
    """端から端: handler が算出した結果の 9 項目が、builder の出力(Recommendation)に載る。"""
    w = handler_wired
    recommendation = w.built[0]
    assert mismatched_computation_fields(recommendation, _copy_spec(w.passed_exit)) == {}
    assert recommendation.exit_price_range_state == PriceRangeEvaluationState.EVALUATED


@pytest.mark.parametrize("index", range(len(_EXIT_ARGUMENT_NAMES)), ids=_EXIT_ARGUMENT_NAMES)
def test_the_check_detects_each_exit_argument_being_dropped_or_none(
    handler_wired: _HandlerWired, index: int
) -> None:
    captured = list(handler_wired.exit_calls[0])
    nulled = captured.copy()
    nulled[index] = None
    dropped = captured[:index] + captured[index + 1 :]
    expected = handler_wired.expected_exit_arguments

    assert _EXIT_ARGUMENT_NAMES[index] in mismatched_arguments(
        tuple(nulled), expected, _EXIT_ARGUMENT_NAMES
    )
    assert mismatched_arguments(tuple(dropped), expected, _EXIT_ARGUMENT_NAMES) != {}


def test_the_check_detects_a_swap_between_average_purchase_price_and_current_price(
    handler_wired: _HandlerWired,
) -> None:
    captured = list(handler_wired.exit_calls[0])
    captured[3], captured[4] = captured[4], captured[3]

    detected = mismatched_arguments(
        tuple(captured), handler_wired.expected_exit_arguments, _EXIT_ARGUMENT_NAMES
    )
    assert set(detected) == {"average_purchase_price", "current_price"}


def test_the_check_detects_a_builder_call_that_passes_a_different_result(
    handler_wired: _HandlerWired,
) -> None:
    w = handler_wired
    other = _failure_exit(w.snapshot)
    captured = (*w.builder_calls[0][:5], other)
    expected = (
        w.holding,
        w.result,
        w.snapshot,
        _RULE_VERSION,
        _CONFIG,
        _recomputed_exit(w.snapshot, w.holding.average_purchase_price),
    )
    assert set(mismatched_arguments(captured, expected, _BUILDER_ARGUMENT_NAMES)) == {
        "exit_price_range"
    }


# --- 算出が失敗したときの代替結果(on_failure の全 field)-----------------------------------


@pytest.mark.parametrize(("stock_code", "price"), _HANDLER_FIXTURES[:4])
def test_a_failing_exit_computation_passes_the_expected_fallback_to_the_builder(
    stock_code: str, price: Decimal
) -> None:
    """算出が例外になっても handler は止まらず、代替の NOT_EVALUATED 結果を builder へ渡す。"""
    w = _HandlerWired(stock_code, price, fail=True)
    expected = ExitPriceRangeResult(
        state=PriceRangeEvaluationState.NOT_EVALUATED,
        current_price=w.snapshot.current_price,
        reason_codes=("SHADOW_COMPUTATION_FAILED:ZeroDivisionError",),
        evaluated_at=_NOW,
        model_version=_CONFIG.entry_exit_price.exit.model_version,
    )

    assert w.passed_exit == expected
    recommendation = w.built[0]
    assert recommendation.exit_price_range_state == PriceRangeEvaluationState.NOT_EVALUATED
    assert recommendation.exit_price_range_reason_codes == expected.reason_codes
    assert mismatched_computation_fields(recommendation, _copy_spec(expected)) == {}


def test_the_fallback_expectation_detects_each_wrong_field() -> None:
    """代替結果の検査が空振りしない: 期待値の各 field を変えると、等しくなくなる。"""
    snapshot = _snapshot(_MOCK_STOCK_CODES[0], not_evaluated=False)
    base = _failure_exit(snapshot)
    changes: dict[str, Any] = {
        "current_price": base.current_price + 1,
        "reason_codes": ("SHADOW_COMPUTATION_FAILED:TypeError",),
        "evaluated_at": _NOW + dt.timedelta(days=1),
        "model_version": base.model_version + "-x",
    }

    for field, value in changes.items():
        assert base.model_copy(update={field: value}) != base, field
