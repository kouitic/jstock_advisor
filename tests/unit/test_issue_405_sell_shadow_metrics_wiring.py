"""Issue #405 PR-2: SELL(sell_signal_service)の `*_metrics` の hoist と exit_price_range の算出が、
取り違え・引数落としなく Recommendation へ載ることを固定する。

PR-1(BUY)と同じ方式(MANAGER 判断の選択肢 A1 + A3。
共有部品 = tests/support/shadow_metrics_wiring.py)を SELL へ適用する。
SELL は BUY の共通 8 種に加えて、次の 2 つを持つ。

    exit_price_range_metrics   `exit_price_range_result_to_metrics` の hoist(9 種目)
    exit_price_range の算出     `evaluate_exit_price_range` の引数列と、その結果の
                               Recommendation へのコピー 9 項目(state / confidence / coverage /
                               reason_codes / 5 価格)

  A1  サービスが消費した snapshot から、テスト側で同じ算出を同じ引数列で再計算(oracle)し、
      Recommendation の各 field と完全一致することを assert する。
  A3  oracle 一致が空振りしない前提(取り違えで差が出る・引数を落とすと差が出る)を
      fixture ごとに固定する。

## このテストが検査している範囲 / していない範囲(★ 正確に)

している    sell_signal_service.py の `*_metrics` 9 種の hoist 9 件と、exit_price_range の算出 1 件
            (引数 7 つ)。mock provider の全 4 銘柄で、EVALUATED(取得単価 2 通り)と
            NOT_EVALUATED(reason_codes が付く側)の両方の fixture で、Recommendation の field が
            oracle と一致すること。sell_signal_service.py が `isolated_shadow_*` で隔離している
            算出が、ここで検査している 10 件だけであること(増えたら落ちる)。
していない  ・`isolated_shadow_*` を介さず手書きの try/except で隔離している箇所
            ・src/ の外からの呼び出し
            ・hoist 側で `**dict` の展開や、式(関数呼び出しの結果)を直接渡している形
            ・Recommendation → DecisionSnapshot の複写(decision_snapshot_builder)での取り違え
            ・`*_to_metrics` / `evaluate_exit_price_range` 自身の算出内容の正しさ
              (各 domain のテストの責務)
            ・exit_price_range の算出結果のうち、Recommendation へコピーされない項目
              (evaluated_at 等)
            ・利確 / 保有判断の hoist(Issue #405 の別 PR)・stock_snapshot_service.py 等(範囲外)
            ・legacy SELL の判定(`evaluate_sell_signal`)自体: 判定は検証対象外のため、決定的な
              SELL 結果へ差し替えて Recommendation 構築の経路へ到達させる
              (既存の test_shadow_feature_isolation と同型)

fixture は mock provider のみ(架空値)。時刻は固定。Production・AWS へは触れない。
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import inspect
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import (
    AccountType,
    PriceRangeEvaluationState,
    RecommendationType,
)
from jstock_advisor.domain.entities.exit_price_range import ExitPriceRangeResult
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.signals.sell_signal import SellSignalResult
from jstock_advisor.providers import mock_fixtures
from jstock_advisor.providers.corporate_action.mock_impl import MockCorporateActionProvider
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.dividend_data.mock_impl import MockDividendDataProvider
from jstock_advisor.providers.financial_data.mock_impl import MockFinancialDataProvider
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.shareholder_benefit.mock_impl import MockShareholderBenefitProvider
from jstock_advisor.services import sell_signal_service as sell_signal_service_module
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.sell_signal_service import SellSignalService
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

_SELL_METRICS_FIELDS = (
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

#: sell_signal_service.py が `isolated_shadow_*` で隔離している算出の名前
#: (= 本テストが検査する 10 件)。
_COVERED_ISOLATED_NAMES = frozenset({*_SELL_METRICS_FIELDS, "exit_price_range"})


def _providers() -> ProviderBundle:
    return ProviderBundle(
        market_data=MockMarketDataProvider(now=_NOW),
        financial_data=MockFinancialDataProvider(now=_NOW),
        dividend_data=MockDividendDataProvider(now=_NOW),
        shareholder_benefit=MockShareholderBenefitProvider(now=_NOW),
        disclosure=MockDisclosureProvider(now=_NOW),
        corporate_action=MockCorporateActionProvider(),
    )


def _canned_sell_result() -> SellSignalResult:
    """legacy SELL の判定結果を決定的に SELL 成立させ、Recommendation 構築の経路へ到達させる。

    判定ロジックは検証対象外(既存の test_shadow_feature_isolation と同じパターン)。
    """
    return SellSignalResult(
        recommendation_type=RecommendationType.SELL,
        triggered_rules=["dividend_omission"],
        reasons=["テスト用の売却理由"],
        hold_reasons=[],
        evidence_details=[],
        independent_evidence_group_count=1,
        all_evidence_yfinance_only=False,
        immediate_execution_price=None,
        stop_review_price=None,
    )


def _holding(stock_code: str, average_purchase_price: Decimal) -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        stock_name="x",
        shares=100,
        average_purchase_price=average_purchase_price,
        total_purchase_amount=average_purchase_price * 100,
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


class _Wired:
    """1 つの fixture(銘柄 × 取得単価)の snapshot・Recommendation・oracle 一式。"""

    def __init__(
        self, stock_code: str, average_purchase_price: Decimal, *, not_evaluated: bool = False
    ) -> None:
        self.not_evaluated = not_evaluated
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                sell_signal_service_module,
                "evaluate_sell_signal",
                lambda *args, **kwargs: _canned_sell_result(),
            )
            providers = _providers()
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
            outcome = SellSignalService(
                providers=providers, config=_CONFIG, business_calendar=_CALENDAR
            ).analyze(
                _holding(stock_code, average_purchase_price),
                _NOW,
                snapshot=snapshot,
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


# --- 検査の対象が、sell_signal_service の実際の隔離と一致する ---------------------------


def _isolated_names_in_source() -> list[str]:
    """sell_signal_service.py の `isolated_shadow_*` の呼び出しの第 1 引数(名前)。"""
    tree = ast.parse(
        Path(inspect.getsourcefile(sell_signal_service_module) or "").read_text("utf-8")
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
    """★ sell_signal_service.py の隔離している算出が、本テストの検査対象と一致すること。

    隔離の呼び出しが増えた(= 取り違え・引数落としの対象が増えた)のに本テストが更新されない、
    という取りこぼしを防ぐ。AST で `isolated_shadow_*` の呼び出しを数えるため、手書きの
    try/except による隔離は数えない(検査範囲の限定。モジュールの docstring 参照)。
    """
    names = _isolated_names_in_source()

    assert sorted(names) == sorted(_COVERED_ISOLATED_NAMES), (
        "sell_signal_service.py の隔離している算出と、本テストの検査対象が食い違う"
    )
    assert len(names) == len(set(names)) == 10


def test_the_spec_covers_exactly_the_nine_sell_metrics_fields(wired: _Wired) -> None:
    """検査対象の field が、sell_signal_service の hoist 9 件と一致する(黙って見落とさない)。"""
    assert tuple(spec.field for spec in wired.metrics_specs) == _SELL_METRICS_FIELDS


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


def test_a1_every_sell_metrics_field_matches_the_oracle(wired: _Wired) -> None:
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


@pytest.mark.parametrize("field", _SELL_METRICS_FIELDS)
def test_the_check_detects_a_missing_metrics_hoist_for_every_field(
    wired: _Wired, field: str
) -> None:
    """kwarg の欠落を模す: その field が既定の空 dict のままの Recommendation を検出する。"""
    omitted = wired.recommendation.model_copy(update={field: {}})
    assert set(mismatched_fields(omitted, wired.metrics_specs)) == {field}


# --- ★ 新しい検査対象(exit_price_range)に、同形のメタテスト -----------------------------


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


@pytest.mark.parametrize("field", _EXIT_COMPUTATION_FIELDS)
def test_the_check_detects_each_copied_field_being_reset(wired: _Wired, field: str) -> None:
    """算出結果のコピーの欠落を模す: その field が既定値(None / 空)のままの場合を検出する。"""
    current = getattr(wired.recommendation, field)
    reset = _reset_value(current)
    degraded = wired.recommendation.model_copy(update={field: reset})
    detected = set(mismatched_computation_fields(degraded, wired.computation_spec))
    # 既定値と異なる値の fixture では必ず検出し、同じ値の fixture では(差が無いので)何も出ない。
    assert detected == ({field} if reset != current else set())


def _reset_value(current: Any) -> Any:
    return type(current)() if isinstance(current, (tuple, list)) else None


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
