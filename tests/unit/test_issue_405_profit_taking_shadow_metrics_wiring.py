"""Issue #405 PR-3: 利確(profit_taking_service)の `*_metrics` の hoist・exit_price_range の算出・
corporate_action の shadow facts が、取り違え・引数落としなく Recommendation / 検査へ
渡ることを固定する。

PR-1(BUY)・PR-2(SELL)と同じ方式(MANAGER 判断の選択肢 A1 + A3。
共有部品 = tests/support/shadow_metrics_wiring.py)を利確へ適用する。利確は SELL と同じく
共通 8 種 + exit_price_range_metrics の 9 種と exit_price_range の算出(引数 7 つ)を持ち、
加えて次の 1 つを持つ。

    corporate_action の shadow facts   保有の FULL_PROFIT_TAKE のとき、`check_split_consistency`
                                       へ 10 個の引数を渡して `CorporateActionFacts` を作る

  A1  `*_metrics` / exit_price_range は、サービスが消費した snapshot から、テスト側で同じ算出を
      同じ引数列で再計算(oracle)し、Recommendation の各 field と完全一致することを assert する。
      corporate_action は、`check_split_consistency` が受け取った引数そのもの(10 個)を捕捉し、
      snapshot・holding・events・config から独立に組み立てた期待値と完全一致することを assert する
      (結果の `CorporateActionFacts` は、通常の fixture では引数を落としても変わらないため、
      結果ではなく引数を直接比べる)。
  A3  A1 が空振りしない前提(取り違えで差が出る・引数を落とすと差が出る)を fixture ごとに固定する。

## このテストが検査している範囲 / していない範囲(★ 正確に)

している    profit_taking_service.py の `*_metrics` 9 種の hoist 9 件、exit_price_range の算出 1 件
            (引数 7 つ)、corporate_action の shadow facts 1 件
            (`check_split_consistency` の引数 10 個)。
            mock provider の全 4 銘柄で、EVALUATED(取得単価 2 通り)と NOT_EVALUATED(reason_codes が
            付く側)の両方の fixture。profit_taking_service.py が `isolated_shadow_*` で隔離している
            算出 12 件が、「検査対象 11 件 + 対象外 1 件(profit_taking_gate_trace)」であること
            (増えたら落ちる)。
していない  ・`profit_taking_gate_trace`(Issue #720 の因果追跡。`*_metrics` の hoist ではなく、
              test_issue_720_profit_taking_gate_trace.py の責務)
            ・`isolated_shadow_*` を介さず手書きの try/except で隔離している箇所
            ・src/ の外からの呼び出し
            ・hoist 側で `**dict` の展開や、式(関数呼び出しの結果)を直接渡している形
            ・Recommendation → DecisionSnapshot の複写(decision_snapshot_builder)での取り違え
            ・`*_to_metrics` / `evaluate_exit_price_range` / `check_split_consistency` 自身の
              算出内容の正しさ、`CorporateActionFacts` への写像
              (既存の test_profit_taking_corporate_action_shadow.py)
            ・exit_price_range の算出結果のうち、Recommendation へコピーされない項目
              (evaluated_at 等)
            ・corporate_action の対象条件(shadow ON・FULL_PROFIT_TAKE のみ。既存テストの責務)
            ・保有判断の hoist(Issue #405 の PR-4)・stock_snapshot_service.py 等(範囲外)
            ・利確の判定(`evaluate_profit_taking`)自体: 判定は検証対象外のため、決定的な結果へ
              差し替えて Recommendation 構築の経路へ到達させる
             (既存の test_profit_taking_service と同型)

fixture は mock provider のみ(架空値)。時刻は固定。Production・AWS へは触れない。
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
from jstock_advisor.domain.entities.common import (
    DataSourceReference,
    PriceWithRationale,
    SellPriceLevels,
)
from jstock_advisor.domain.entities.enums import (
    AccountType,
    CorporateActionType,
    PriceRangeEvaluationState,
    RecommendationType,
    SellIntensity,
    TimingAction,
)
from jstock_advisor.domain.entities.exit_price_range import ExitPriceRangeResult
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.signals.judgment_safety_shadow_config import (
    JudgmentSafetyShadowConfig,
    ShadowMode,
)
from jstock_advisor.domain.signals.profit_taking import ProfitTakingResult, UnrealizedPnl
from jstock_advisor.interfaces.types import CorporateActionEvent
from jstock_advisor.providers import mock_fixtures
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.dividend_data.mock_impl import MockDividendDataProvider
from jstock_advisor.providers.financial_data.mock_impl import MockFinancialDataProvider
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.shareholder_benefit.mock_impl import MockShareholderBenefitProvider
from jstock_advisor.services import profit_taking_service as profit_taking_service_module
from jstock_advisor.services.data_quality_service import check_split_consistency
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot
from tests.support.shadow_metrics_wiring import (
    EXIT_PRICE_RANGE_RECOMMENDATION_FIELDS,
    ComputationSpec,
    MetricsSpec,
    argument_drop_blind_spots,
    common_metrics_specs,
    computation_argument_drop_blind_spots,
    exit_price_range_computation_spec,
    exit_price_range_metrics_spec,
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

_PROFIT_TAKING_METRICS_FIELDS = (
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
_EXIT_COMPUTATION_FIELDS = tuple(EXIT_PRICE_RANGE_RECOMMENDATION_FIELDS)

#: corporate_action の shadow facts が `check_split_consistency` へ渡す 10 個の引数名。
_G4_ARGUMENT_NAMES = (
    "stock_code",
    "current_price",
    "bars_close_by_date",
    "fair_value",
    "actual_annual_dividend_per_share",
    "previous_fiscal_year_dividend_per_share",
    "corporate_action_events",
    "holding",
    "now",
    "config",
)
_CORPORATE_ACTION_NAME = "judgment_safety_corporate_action"
#: 検査の対象外とする隔離の算出(Issue #720。モジュールの docstring 参照)。
_EXCLUDED_ISOLATED_NAMES = frozenset({"profit_taking_gate_trace"})
#: profit_taking_service.py が `isolated_shadow_*` で隔離している算出のうち、
#: 本テストが検査する 11 件。
_COVERED_ISOLATED_NAMES = frozenset(
    {*_PROFIT_TAKING_METRICS_FIELDS, "exit_price_range", _CORPORATE_ACTION_NAME}
)

_SHADOW_ON = JudgmentSafetyShadowConfig(mode=ShadowMode.SHADOW)


def _providers(corporate_action: Any | None = None) -> ProviderBundle:
    bundle = ProviderBundle(
        market_data=MockMarketDataProvider(now=_NOW),
        financial_data=MockFinancialDataProvider(now=_NOW),
        dividend_data=MockDividendDataProvider(now=_NOW),
        shareholder_benefit=MockShareholderBenefitProvider(now=_NOW),
        disclosure=MockDisclosureProvider(now=_NOW),
        corporate_action=_NoEventsProvider(),
    )
    if corporate_action is not None:
        bundle = dataclasses.replace(bundle, corporate_action=corporate_action)
    return bundle


class _NoEventsProvider:
    def get_corporate_actions(self, stock_code: str, since: dt.date) -> list[CorporateActionEvent]:
        return []


class _EventsProvider:
    """実 provider と同じく `since` より古い events を返さない。"""

    def __init__(self, events: list[CorporateActionEvent]) -> None:
        self._events = events

    def get_corporate_actions(self, stock_code: str, since: dt.date) -> list[CorporateActionEvent]:
        return [e for e in self._events if e.effective_date is None or e.effective_date >= since]


def _canned_profit_taking_result() -> ProfitTakingResult:
    """利確の判定結果を決定的に FULL_PROFIT_TAKE 成立させ、Recommendation 構築の経路へ到達させる。

    判定ロジックは検証対象外(既存の test_profit_taking_service と同じパターン)。
    """
    return ProfitTakingResult(
        recommendation_type=RecommendationType.FULL_PROFIT_TAKE,
        fundamental_action=RecommendationType.FULL_PROFIT_TAKE,
        timing_action=TimingAction.NEUTRAL,
        final_action=RecommendationType.FULL_PROFIT_TAKE,
        triggered_reasons=["含み益率が全部利確基準を超過"],
        mitigating_factors_applied=[],
        hold_reasons=[],
        sell_prices=SellPriceLevels(
            recommended_limit_price=PriceWithRationale(price=Decimal("5000"), rationale="test")
        ),
        pnl=UnrealizedPnl(
            unrealized_pnl=Decimal("100000"),
            unrealized_pnl_pct=25.0,
            total_return_including_income=Decimal("105000"),
            total_return_pct=26.25,
        ),
        independent_condition_count=1,
        fair_value_used_as_sole_strong_basis=False,
        current_price_vs_neutral_fair_value_pct=10.0,
        current_price_vs_bull_fair_value_pct=5.0,
        fair_value_action_usable=False,
        fair_value_action_block_reason_code=None,
        mitigating_downgrade_applied=False,
        timing_downgrade_applied=False,
        origin="OTHER_CONDITIONS",
        ceiling_price=None,
        upside_pct=None,
        profit_protection_signal="NONE",
        profit_protection_basis_date=None,
        profit_protection_peak_price=None,
        profit_protection_peak_date=None,
        profit_protection_peak_gain_pct=None,
        profit_protection_current_gain_pct=None,
        profit_protection_drawdown_from_peak_pct=None,
        profit_protection_gain_giveback_ratio_pct=None,
        profit_protection_insufficient_reason=None,
        sell_intensity=SellIntensity.STANDARD,
    )


def _holding(stock_code: str, average_purchase_price: Decimal) -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        stock_name="x",
        shares=300,
        average_purchase_price=average_purchase_price,
        total_purchase_amount=average_purchase_price * 300,
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


class _RecordingAudit:
    def record(self, **kwargs: Any) -> Any:
        return SimpleNamespace(audit_id="audit-1")


def _analyze(
    stock_code: str,
    average_purchase_price: Decimal,
    *,
    not_evaluated: bool = False,
    shadow: JudgmentSafetyShadowConfig | None = None,
    events: list[CorporateActionEvent] | None = None,
    distinct_previous_dividend: bool = False,
) -> tuple[Any, Any, Holding, list[dict[str, Any]]]:
    """(snapshot, outcome, holding, `check_split_consistency` が受け取った引数の捕捉)を返す。"""
    captured: list[dict[str, Any]] = []

    def _spy(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return check_split_consistency(**kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            profit_taking_service_module,
            "evaluate_profit_taking",
            lambda **kwargs: _canned_profit_taking_result(),
        )
        monkeypatch.setattr(profit_taking_service_module, "check_split_consistency", _spy)
        providers = _providers(_EventsProvider(events) if events is not None else None)
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
        if distinct_previous_dividend:
            # mock の配当は、実績と前期が全銘柄で等しく、2 つの取り違えが検出できない。
            actual = snapshot.dividend.actual_annual_dividend_per_share
            assert actual is not None
            snapshot = dataclasses.replace(
                snapshot,
                dividend=snapshot.dividend.model_copy(
                    update={"previous_fiscal_year_dividend_per_share": actual * 2}
                ),
            )
        holding = _holding(stock_code, average_purchase_price)
        service = ProfitTakingService(
            providers=providers,
            config=_CONFIG,
            business_calendar=_CALENDAR,
            shadow_config=shadow or JudgmentSafetyShadowConfig(mode=ShadowMode.OFF),
        )
        monkeypatch.setattr(service, "_audit", _RecordingAudit())
        outcome = service.analyze(holding, _NOW, snapshot=snapshot)
    return snapshot, outcome, holding, captured


class _Wired:
    """1 つの fixture(銘柄 × 取得単価)の snapshot・Recommendation・oracle 一式(metrics・算出)。"""

    def __init__(
        self, stock_code: str, average_purchase_price: Decimal, *, not_evaluated: bool = False
    ) -> None:
        self.not_evaluated = not_evaluated
        snapshot, outcome, _, _ = _analyze(
            stock_code, average_purchase_price, not_evaluated=not_evaluated
        )
        # Recommendation が作られない fixture では、以下の検査が空振りする(vacuous)。
        assert outcome.recommendation is not None, "この fixture では Recommendation が作られない"
        self.recommendation: Any = outcome.recommendation
        self.metrics_specs: list[MetricsSpec] = [
            *common_metrics_specs(snapshot, _CONFIG),
            exit_price_range_metrics_spec(snapshot, _CONFIG, average_purchase_price, _NOW),
        ]
        self.computation_spec: ComputationSpec = exit_price_range_computation_spec(
            snapshot, _CONFIG, average_purchase_price, _NOW
        )


# EVALUATED の fixture(4 銘柄 × 取得単価 2 通り)と、NOT_EVALUATED の fixture(4 銘柄)。
_EVALUATED_FIXTURES = [
    (code, price, False) for code in _MOCK_STOCK_CODES for price in _AVERAGE_PURCHASE_PRICES
]
_NOT_EVALUATED_FIXTURES = [(code, _AVERAGE_PURCHASE_PRICES[0], True) for code in _MOCK_STOCK_CODES]
_ALL_FIXTURES = [*_EVALUATED_FIXTURES, *_NOT_EVALUATED_FIXTURES]


def _fixture_ids(fixtures: list[tuple[str, Decimal, bool]]) -> list[str]:
    return [
        f"{'not-evaluated' if not_evaluated else 'evaluated'}-{index}"
        for index, (_, _, not_evaluated) in enumerate(fixtures)
    ]


@pytest.fixture(params=_ALL_FIXTURES, ids=_fixture_ids(_ALL_FIXTURES))
def wired(request: pytest.FixtureRequest) -> _Wired:
    """EVALUATED と NOT_EVALUATED の両方の fixture(A1 は両方で検査する)。"""
    stock_code, average_purchase_price, not_evaluated = request.param
    return _Wired(stock_code, average_purchase_price, not_evaluated=not_evaluated)


@pytest.fixture(params=_EVALUATED_FIXTURES, ids=_fixture_ids(_EVALUATED_FIXTURES))
def evaluated(request: pytest.FixtureRequest) -> _Wired:
    """exit_price_range が実際に評価された fixture(5 価格・取得単価への依存を検査する側)。"""
    stock_code, average_purchase_price, not_evaluated = request.param
    return _Wired(stock_code, average_purchase_price, not_evaluated=not_evaluated)


# --- 検査の対象が、profit_taking_service の実際の隔離と一致する -----------------------------


def _isolated_names_in_source() -> list[str]:
    """profit_taking_service.py の `isolated_shadow_*` の呼び出しの第 1 引数(名前)。"""
    tree = ast.parse(
        Path(inspect.getsourcefile(profit_taking_service_module) or "").read_text("utf-8")
    )
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


def test_the_covered_computations_are_exactly_the_isolated_calls_in_the_service() -> None:
    """★ profit_taking_service.py の隔離している算出 12 件 = 検査対象 11 件 + 対象外 1 件。

    隔離の呼び出しが増えた(= 取り違え・引数落としの対象が増えた)のに本テストが更新されない、
    という取りこぼしを防ぐ。AST で `isolated_shadow_*` の呼び出しを数えるため、手書きの
    try/except による隔離は数えない(検査範囲の限定。モジュールの docstring 参照)。
    """
    names = _isolated_names_in_source()

    assert sorted(names) == sorted(_COVERED_ISOLATED_NAMES | _EXCLUDED_ISOLATED_NAMES), (
        "profit_taking_service.py の隔離している算出と、本テストの検査対象 / 対象外が食い違う"
    )
    assert len(names) == len(set(names)) == 12
    assert len(_COVERED_ISOLATED_NAMES) == 11
    assert _COVERED_ISOLATED_NAMES.isdisjoint(_EXCLUDED_ISOLATED_NAMES)


def test_the_spec_covers_exactly_the_nine_profit_taking_metrics_fields(wired: _Wired) -> None:
    """検査対象の field が、profit_taking_service の hoist 9 件と一致する(黙って見落とさない)。"""
    assert tuple(spec.field for spec in wired.metrics_specs) == _PROFIT_TAKING_METRICS_FIELDS


def test_the_computation_spec_covers_exactly_the_nine_copied_fields(wired: _Wired) -> None:
    assert tuple(wired.computation_spec.field_map) == _EXIT_COMPUTATION_FIELDS
    assert len(_EXIT_COMPUTATION_FIELDS) == 9


def test_the_evaluated_fixtures_exercise_the_evaluated_path(evaluated: _Wired) -> None:
    """exit_price_range が実際に評価された fixture であること(5 価格が None のままの空振り防止)。"""
    recommendation = evaluated.recommendation
    assert recommendation.exit_price_range_state == PriceRangeEvaluationState.EVALUATED
    assert recommendation.exit_price_range_partial_low_price is not None
    assert recommendation.exit_price_range_metrics.get("state") is not None


def test_the_not_evaluated_fixtures_exercise_the_reason_code_path() -> None:
    """NOT_EVALUATED の fixture では reason_codes が実際に付き、5 価格は None であること。

    reason_codes は EVALUATED の fixture では空のため、そこだけでは「reason_codes のコピーの欠落」を
    検出できない。この fixture が、その検出を可能にする。
    """
    for stock_code, price, not_evaluated in _NOT_EVALUATED_FIXTURES:
        recommendation = _Wired(stock_code, price, not_evaluated=not_evaluated).recommendation
        assert recommendation.exit_price_range_state == PriceRangeEvaluationState.NOT_EVALUATED
        assert recommendation.exit_price_range_reason_codes
        assert recommendation.exit_price_range_partial_low_price is None


# --- A3: oracle 一致が空振りしないための前提 ---------------------------------------


def test_a3_swapping_any_two_metrics_changes_the_result(evaluated: _Wired) -> None:
    """★ 前提: 9 種の oracle が互いに異なる。等しい組があると、その 2 つを取り違える変異は、
    Recommendation の値が変わらず生き残る。
    """
    assert swap_blind_pairs(evaluated.metrics_specs) == []


def test_a3_dropping_any_metrics_argument_changes_the_result(evaluated: _Wired) -> None:
    """★ 前提: `*_to_metrics` の引数を 1 つ None にすると、oracle が変わる(または例外になる)。"""
    assert argument_drop_blind_spots(evaluated.metrics_specs) == []


def test_a3_dropping_any_exit_computation_argument_changes_the_result(
    evaluated: _Wired,
) -> None:
    """★ 前提: `evaluate_exit_price_range` の 7 引数のどれを落としても、9 項目の oracle が変わる
    (または例外になる)。変わらない引数があると、その引数を落とす変異は生き残る。
    """
    assert computation_argument_drop_blind_spots(evaluated.computation_spec) == []


# --- A1: Recommendation が oracle と一致する ---------------------------------------


def test_a1_every_profit_taking_metrics_field_matches_the_oracle(wired: _Wired) -> None:
    """★ 本体(metrics): 9 つの `*_metrics` を、同じ引数列で再計算した値と完全一致で比較する。

    取り違え・引数落とし・kwarg の欠落(既定の空 dict)のいずれも、この 1 つの比較で落ちる。
    """
    assert mismatched_fields(wired.recommendation, wired.metrics_specs) == {}


def test_a1_the_exit_price_range_computation_matches_the_oracle(wired: _Wired) -> None:
    """★ 本体(算出): `evaluate_exit_price_range` を同じ 7 引数で再計算した結果の 9 項目が、
    Recommendation の exit_price_range_* と完全一致する(取得単価・現在値などの引数落とし、
    5 価格の取り違えを捉える)。
    """
    assert mismatched_computation_fields(wired.recommendation, wired.computation_spec) == {}


# --- 検査そのものの反証(Recommendation 側へ変異を模して、検出できること) -------------


def test_the_check_detects_a_swap_between_market_and_sector(wired: _Wired) -> None:
    """P6 を模す: market_metrics と sector_metrics を入れ替えた Recommendation を検出する。"""
    recommendation = wired.recommendation
    swapped = recommendation.model_copy(
        update={
            "market_metrics": recommendation.sector_metrics,
            "sector_metrics": recommendation.market_metrics,
        }
    )
    assert set(mismatched_fields(swapped, wired.metrics_specs)) == {
        "market_metrics",
        "sector_metrics",
    }


def test_the_check_detects_a_swap_between_earnings_surprise_and_trend(wired: _Wired) -> None:
    """P9 を模す: earnings_surprise_metrics と earnings_trend_metrics の入れ替えを検出する。"""
    recommendation = wired.recommendation
    swapped = recommendation.model_copy(
        update={
            "earnings_surprise_metrics": recommendation.earnings_trend_metrics,
            "earnings_trend_metrics": recommendation.earnings_surprise_metrics,
        }
    )
    assert set(mismatched_fields(swapped, wired.metrics_specs)) == {
        "earnings_surprise_metrics",
        "earnings_trend_metrics",
    }


def test_the_check_detects_timing_without_current_price(wired: _Wired) -> None:
    """P7 を模す: timing の `current_price` を落として算出した値が載った場合を検出する。"""
    timing = next(spec for spec in wired.metrics_specs if spec.field == "timing_metrics")
    index = timing.argument_names.index("current_price")
    degraded = wired.recommendation.model_copy(
        update={"timing_metrics": timing.oracle_with_argument_dropped(index)}
    )
    assert set(mismatched_fields(degraded, wired.metrics_specs)) == {"timing_metrics"}


@pytest.mark.parametrize("field", _PROFIT_TAKING_METRICS_FIELDS)
def test_the_check_detects_a_missing_metrics_hoist_for_every_field(
    wired: _Wired, field: str
) -> None:
    """kwarg の欠落を模す: その field が既定の空 dict のままの Recommendation を検出する。"""
    omitted = wired.recommendation.model_copy(update={field: {}})
    assert set(mismatched_fields(omitted, wired.metrics_specs)) == {field}


def test_the_check_detects_a_swap_between_entry_and_exit_price_range_metrics(
    wired: _Wired,
) -> None:
    """exit_price_range_metrics と entry_price_range_metrics の取り違え(9 種目)を検出する。"""
    recommendation = wired.recommendation
    swapped = recommendation.model_copy(
        update={
            "entry_price_range_metrics": recommendation.exit_price_range_metrics,
            "exit_price_range_metrics": recommendation.entry_price_range_metrics,
        }
    )
    assert set(mismatched_fields(swapped, wired.metrics_specs)) == {
        "entry_price_range_metrics",
        "exit_price_range_metrics",
    }


def test_the_check_detects_exit_metrics_without_the_average_purchase_price(
    evaluated: _Wired,
) -> None:
    """P7 を模す: exit_price_range_metrics の算出から取得単価を落とした値が載った場合を検出する。"""
    exit_spec = next(s for s in evaluated.metrics_specs if s.field == "exit_price_range_metrics")
    index = exit_spec.argument_names.index("average_purchase_price")
    degraded = evaluated.recommendation.model_copy(
        update={"exit_price_range_metrics": exit_spec.oracle_with_argument_dropped(index)}
    )
    assert set(mismatched_fields(degraded, evaluated.metrics_specs)) == {"exit_price_range_metrics"}


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("exit_price_range_partial_low_price", "exit_price_range_partial_high_price"),
        ("exit_price_range_strong_price", "exit_price_range_exit_review_price"),
        ("exit_price_range_downside_review_price", "exit_price_range_exit_review_price"),
    ],
)
def test_the_check_detects_a_swap_between_exit_price_fields(
    evaluated: _Wired, first: str, second: str
) -> None:
    """5 価格のうち 2 つの取り違えを検出する(fixture の 2 つの価格が異なる前提も確認する)。"""
    recommendation = evaluated.recommendation
    assert getattr(recommendation, first) != getattr(recommendation, second), (
        "この fixture では 2 つの価格が等しく、取り違えを検出できない"
    )
    swapped = recommendation.model_copy(
        update={first: getattr(recommendation, second), second: getattr(recommendation, first)}
    )
    assert set(mismatched_computation_fields(swapped, evaluated.computation_spec)) == {
        first,
        second,
    }


def _reset_value(current: Any) -> Any:
    return type(current)() if isinstance(current, (tuple, list)) else None


@pytest.mark.parametrize("field", _EXIT_COMPUTATION_FIELDS)
def test_the_check_detects_each_copied_field_being_reset(wired: _Wired, field: str) -> None:
    """算出結果のコピーの欠落を模す: その field が既定値(None / 空)のままの場合を検出する。"""
    current = getattr(wired.recommendation, field)
    reset = _reset_value(current)
    degraded = wired.recommendation.model_copy(update={field: reset})
    detected = set(mismatched_computation_fields(degraded, wired.computation_spec))
    # 既定値と異なる値の fixture では必ず検出し、同じ値の fixture では(差が無いので)何も出ない。
    assert detected == ({field} if reset != current else set())


def test_every_copied_field_is_detectable_on_at_least_one_fixture() -> None:
    """★ fixture 全体で、9 項目のどれも「欠落を検出できる fixture」が 1 つ以上あること。

    EVALUATED の fixture だけでは reason_codes が常に空で、NOT_EVALUATED の fixture だけでは
    5 価格が常に None になる。両方の fixture を持つことで、9 項目すべての欠落が少なくとも
    1 つの fixture で検出できる(= 検査が全項目で空振りしない)ことを固定する。
    """
    wired_all = [_Wired(code, price, not_evaluated=flag) for code, price, flag in _ALL_FIXTURES]
    undetectable = [
        field
        for field in _EXIT_COMPUTATION_FIELDS
        if not any(
            _reset_value(getattr(w.recommendation, field)) != getattr(w.recommendation, field)
            for w in wired_all
        )
    ]
    assert undetectable == []


def test_the_check_detects_the_computation_without_the_average_purchase_price(
    evaluated: _Wired,
) -> None:
    """P7 を模す: exit_price_range の算出から取得単価を落とした場合を検出する。

    取得単価を None にすると算出は例外になり、本番では `isolated_shadow_computation` が
    NOT_EVALUATED の失敗結果(SHADOW_COMPUTATION_FAILED)へ変える。Recommendation にその失敗結果が
    載った状態を模して、検出されることを確認する。
    """
    spec = evaluated.computation_spec
    index = spec.argument_names.index("average_purchase_price")
    assert "__raised__" in spec.oracle_with_argument_dropped(index)  # 例外になること(上の前提)
    failure = ExitPriceRangeResult(
        state=PriceRangeEvaluationState.NOT_EVALUATED,
        current_price=evaluated.recommendation.price_at_recommendation,
        reason_codes=("SHADOW_COMPUTATION_FAILED:TypeError",),
        evaluated_at=_NOW,
        model_version=_CONFIG.entry_exit_price.exit.model_version,
    )
    degraded = evaluated.recommendation.model_copy(update=spec.projection(failure))
    assert "exit_price_range_state" in mismatched_computation_fields(degraded, spec)


def test_the_a3_preconditions_themselves_detect_a_blind_computation() -> None:
    """A3(算出)の検査が空振りしないこと: 引数を無視する関数は、blind な引数として検出される。"""

    class _Result:
        value = 1

    def ignores_argument(_ignored: object) -> _Result:
        return _Result()

    spec = ComputationSpec("blind", ignores_argument, ("a",), {"field": "value"})

    assert computation_argument_drop_blind_spots(spec) == ["_ignored"]


# =====================================================================================
# corporate_action の shadow facts(`check_split_consistency` の引数 10 個)
# =====================================================================================

#: 取得開始日(`min(基準日, lookback_start)`)より新しく、基準日(保有の最終購入日)より前の分割。
#: 利確の既存判定は基準日以降しか見ないが、G4 へは広い窓の events が渡る(既存テスト T3 と同じ前提)。
_EVENT_DATE = dt.date(2023, 10, 1)
_SPLIT_EVENT = CorporateActionEvent(
    stock_code="0000",
    event_type=CorporateActionType.SPLIT,
    announced_date=_EVENT_DATE - dt.timedelta(days=30),
    effective_date=_EVENT_DATE,
    ratio=Decimal("2"),
    source=DataSourceReference(provider="test-fixture", fetched_at=_NOW),
)

_MISSING = object()


def _expected_g4_arguments(
    snapshot: Any, holding: Holding, events: list[CorporateActionEvent]
) -> dict[str, Any]:
    """`check_split_consistency` が受け取るべき 10 個の引数(snapshot・holding・events から組む)。"""
    return {
        "stock_code": holding.stock_code,
        "current_price": snapshot.current_price,
        "bars_close_by_date": [(bar.date, bar.close) for bar in snapshot.bars],
        "fair_value": snapshot.fair_value,
        "actual_annual_dividend_per_share": snapshot.dividend.actual_annual_dividend_per_share,
        "previous_fiscal_year_dividend_per_share": (
            snapshot.dividend.previous_fiscal_year_dividend_per_share
        ),
        "corporate_action_events": events,
        "holding": holding,
        "now": _NOW,
        "config": _CONFIG.data_validation.split_consistency,
    }


def mismatched_g4_arguments(
    captured: dict[str, Any], expected: dict[str, Any]
) -> dict[str, tuple[Any, Any]]:
    """捕捉した引数が期待値と一致しない引数名 → (実際の値, 期待値)。キーの過不足も不一致。"""
    return {
        name: (captured.get(name, _MISSING), expected.get(name, _MISSING))
        for name in sorted({*captured, *expected})
        if captured.get(name, _MISSING) != expected.get(name, _MISSING)
    }


class _WiredG4:
    """corporate_action の shadow facts が走る fixture(shadow ON・FULL_PROFIT_TAKE)。"""

    def __init__(self, stock_code: str, average_purchase_price: Decimal) -> None:
        event = _SPLIT_EVENT.model_copy(update={"stock_code": stock_code})
        self.events = [event]
        snapshot, outcome, holding, captured = _analyze(
            stock_code,
            average_purchase_price,
            shadow=_SHADOW_ON,
            events=self.events,
            distinct_previous_dividend=True,
        )
        # shadow が走っていない fixture では、以下の検査が空振りする(vacuous)。
        assert outcome.recommendation is not None
        assert outcome.recommendation.recommendation_type is RecommendationType.FULL_PROFIT_TAKE
        assert len(captured) == 1, "check_split_consistency が 1 回呼ばれていない"
        self.captured: dict[str, Any] = captured[0]
        self.expected: dict[str, Any] = _expected_g4_arguments(snapshot, holding, self.events)
        self.facts = outcome.safety_facts.corporate_action


_G4_FIXTURES = [(code, _AVERAGE_PURCHASE_PRICES[0]) for code in _MOCK_STOCK_CODES]


@pytest.fixture(params=_G4_FIXTURES, ids=[f"g4-{i}" for i in range(len(_G4_FIXTURES))])
def g4(request: pytest.FixtureRequest) -> _WiredG4:
    stock_code, average_purchase_price = request.param
    return _WiredG4(stock_code, average_purchase_price)


def test_the_g4_argument_list_is_exactly_the_ten_checked_arguments(g4: _WiredG4) -> None:
    """検査対象の引数名が、サービスの実際の呼び出しと一致する(増減を黙って見落とさない)。"""
    assert tuple(g4.captured) == _G4_ARGUMENT_NAMES == tuple(g4.expected)
    assert len(_G4_ARGUMENT_NAMES) == 10


def test_a3_every_g4_argument_is_present_and_the_swap_prone_pairs_differ(g4: _WiredG4) -> None:
    """★ 前提: どの引数も None / 空ではなく、取り違えやすい組の値が互いに異なる。

    引数を落とす変異は None / 欠落になるため、期待値が None / 空だと差が出ない。取り違えやすい組
    (現在値と公正価値・実績配当と前期配当)の値が等しいと、その取り違えを検出できない。
    mock の配当は実績と前期が全銘柄で等しいため、fixture では前期の配当を別の値にしている。
    """
    expected = g4.expected
    for name, value in expected.items():
        assert value is not None and value != [], f"{name} が None / 空で、落としても差が出ない"
    assert expected["current_price"] != expected["fair_value"]
    assert (
        expected["actual_annual_dividend_per_share"]
        != expected["previous_fiscal_year_dividend_per_share"]
    )
    assert len(expected["corporate_action_events"]) == 1
    assert expected["bars_close_by_date"], "bars が空で、取り違えても差が出ない"


def test_a1_check_split_consistency_receives_exactly_the_expected_arguments(g4: _WiredG4) -> None:
    """★ 本体(corporate_action): 受け取った引数 10 個が、独立に組んだ期待値と完全一致する。"""
    assert mismatched_g4_arguments(g4.captured, g4.expected) == {}


def test_the_g4_facts_are_evaluated_in_the_fixture(g4: _WiredG4) -> None:
    """shadow facts が COMPUTATION_FAILED ではなく評価された(引数の不備で失敗側に落ちていない)。"""
    assert g4.facts is not None
    assert g4.facts.state == "EVALUATED"


@pytest.mark.parametrize("name", _G4_ARGUMENT_NAMES)
def test_the_g4_check_detects_each_argument_being_dropped_or_none(g4: _WiredG4, name: str) -> None:
    """引数落としを模す: その引数が欠落 / None のときを検出する(10 個すべて)。"""
    dropped = {k: v for k, v in g4.captured.items() if k != name}
    nulled = {**g4.captured, name: None}

    assert set(mismatched_g4_arguments(dropped, g4.expected)) == {name}
    assert set(mismatched_g4_arguments(nulled, g4.expected)) == {name}


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("current_price", "fair_value"),
        ("actual_annual_dividend_per_share", "previous_fiscal_year_dividend_per_share"),
    ],
)
def test_the_g4_check_detects_a_swap_between_two_arguments(
    g4: _WiredG4, first: str, second: str
) -> None:
    """取り違えを模す: 同じ型の 2 引数(現在値と公正価値・実績配当と前期配当)の入れ替え。"""
    swapped = {**g4.captured, first: g4.captured[second], second: g4.captured[first]}

    assert set(mismatched_g4_arguments(swapped, g4.expected)) == {first, second}


def test_the_g4_check_detects_events_that_are_not_the_fetched_ones(g4: _WiredG4) -> None:
    """events を取り違えた(空・別の events)呼び出しを検出する。"""
    assert set(
        mismatched_g4_arguments({**g4.captured, "corporate_action_events": []}, g4.expected)
    ) == {"corporate_action_events"}


def test_the_g4_check_detects_a_different_holding_or_time(g4: _WiredG4) -> None:
    holding = g4.captured["holding"]
    other = holding.model_copy(
        update={"average_purchase_price": holding.average_purchase_price + 1}
    )
    later = g4.captured["now"] + dt.timedelta(days=1)

    assert set(mismatched_g4_arguments({**g4.captured, "holding": other}, g4.expected)) == {
        "holding"
    }
    assert set(mismatched_g4_arguments({**g4.captured, "now": later}, g4.expected)) == {"now"}


def test_the_g4_expected_arguments_are_built_independently_of_the_service() -> None:
    """期待値の組み立てが、サービスの捕捉値を使わない(自己参照の防止)。

    期待値は `_expected_g4_arguments(snapshot, holding, events)` だけから作られる。
    """
    parameters = tuple(inspect.signature(_expected_g4_arguments).parameters)

    assert parameters == ("snapshot", "holding", "events")


def test_the_a3_preconditions_themselves_detect_a_blind_argument() -> None:
    """A3(corporate_action)の検査が空振りしないこと: 期待値が None の引数は、置換で差が出ない。

    欠落は(None と区別されて)検出されるが、None への置換は期待値が None だと検出できない。
    `test_a3_every_g4_argument_is_present_and_the_swap_prone_pairs_differ` がその状態を落とす。
    """
    expected = {"a": None, "b": 1}

    assert mismatched_g4_arguments({"a": None, "b": 1}, expected) == {}  # 期待値が None = blind
    assert mismatched_g4_arguments({"b": 1}, expected) == {"a": (_MISSING, None)}  # 欠落は検出
    assert [name for name, value in expected.items() if value is None] == ["a"]
