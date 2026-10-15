"""PriceFacts の供給元 `build_price_facts` の契約テスト(Issue #877 PR-2a。dormant)。

利益保全の指標(`ProfitProtectionMetrics`)の値を、そのまま PriceFacts へ写す純粋関数を固定する。

  C-1 入力の格子(data_sufficient × トレンドの 3 値)の出力。★ 3 つの数値は互いに異なる値 ★
      (取り違えが偶然の一致で隠れない)。格子自身が『互いに異なる』ことを assert する
  C-2 不足が優先: data_sufficient = False のとき数値は UNDETERMINED
      (渡された数値は使わない・検査しない)
  C-3 契約違反: 充足なのに None・NaN / inf・範囲外・bool は例外
  C-4 純粋・dormant・閾値を持たない・peak を探す処理を持たない(AST)。dormant は src の全走査
  C-5 ★ 実際の `compute_profit_protection_metrics` の出力と 1 対 1 に一致(再計算していない)★
      peak の定義(basis_date の翌日以降の日次高値の最大)は指標の側にだけある
  C-6 記述的な比較(合否にしない): 現行の candidate / strong と、新しい regime の対応表

C-6 の閾値は『未承認の比較用の仮の値』であり、承認済みの値ではない(USER は 3 値を保留している)。
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import itertools
import math
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.exit_architecture import price_facts
from jstock_advisor.domain.exit_architecture.price_facts import (
    INSUFFICIENT_DETAIL,
    TREND_MISSING_DETAIL,
    build_price_facts,
)
from jstock_advisor.domain.exit_architecture.price_regime import (
    PriceFacts,
    RegimeThresholds,
    TrendReading,
    classify_price_regime,
)
from jstock_advisor.domain.exit_architecture.vocabulary import (
    RegimeState,
    ReliabilityClass,
    UndeterminedReason,
)
from jstock_advisor.domain.signals.profit_protection import (
    ProfitProtectionMetrics,
    compute_profit_protection_metrics,
)
from jstock_advisor.interfaces.types import PriceBar

_SRC = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"
_MODULE_PATH = Path(price_facts.__file__)
_MODULE_NAME = "jstock_advisor.domain.exit_architecture.price_facts"

#: 3 つの数値が互いに異なる、充足の入力(引数の取り違えが偶然の一致で隠れない)
_DRAWDOWN = 12.5
_PEAK_GAIN = 60.0
_CURRENT_GAIN = 40.0


def _sufficient(**override: object) -> dict[str, object]:
    base: dict[str, object] = {
        "data_sufficient": True,
        "drawdown_from_peak_pct": _DRAWDOWN,
        "peak_gain_pct": _PEAK_GAIN,
        "current_gain_pct": _CURRENT_GAIN,
        "trend_is_down": False,
    }
    base.update(override)
    return base


def _build(**kwargs: object) -> PriceFacts:
    return build_price_facts(**kwargs)  # type: ignore[arg-type]


def test_the_grid_values_are_pairwise_distinct() -> None:
    """格子の 3 つの数値は互いに異なる(取り違えが生き残らない前提を、テスト自身が確かめる)。"""
    assert len({_DRAWDOWN, _PEAK_GAIN, _CURRENT_GAIN}) == 3


# --- C-1 格子 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("trend_is_down", "expected_trend"),
    [(True, TrendReading.DOWN), (False, TrendReading.NOT_DOWN), (None, None)],
    ids=["down", "not_down", "missing"],
)
def test_sufficient_input_maps_each_number_to_its_own_field(
    trend_is_down: bool | None, expected_trend: TrendReading | None
) -> None:
    facts = _build(**_sufficient(trend_is_down=trend_is_down))
    assert facts.drawdown_from_peak_pct.unwrap() == _DRAWDOWN
    assert facts.peak_gain_pct.unwrap() == _PEAK_GAIN
    assert facts.current_gain_pct.unwrap() == _CURRENT_GAIN
    if expected_trend is None:
        assert not facts.trend.is_determined
        assert facts.trend.reason is UndeterminedReason.INPUT_MISSING
        assert facts.trend.detail == TREND_MISSING_DETAIL
    else:
        assert facts.trend.unwrap() is expected_trend


@pytest.mark.parametrize(
    ("trend_is_down", "expected_trend"),
    [(True, TrendReading.DOWN), (False, TrendReading.NOT_DOWN), (None, None)],
    ids=["down", "not_down", "missing"],
)
def test_insufficient_input_makes_all_numbers_undetermined_but_keeps_the_trend(
    trend_is_down: bool | None, expected_trend: TrendReading | None
) -> None:
    facts = _build(
        data_sufficient=False,
        drawdown_from_peak_pct=None,
        peak_gain_pct=None,
        current_gain_pct=None,
        trend_is_down=trend_is_down,
    )
    for number in (facts.drawdown_from_peak_pct, facts.peak_gain_pct, facts.current_gain_pct):
        assert not number.is_determined
        assert number.reason is UndeterminedReason.INPUT_MISSING
        assert number.detail == INSUFFICIENT_DETAIL
    assert (facts.trend.unwrap() if facts.trend.is_determined else None) == expected_trend


def test_the_detail_codes_are_fixed_short_symbols_and_carry_no_free_text() -> None:
    assert INSUFFICIENT_DETAIL == "PROFIT_PROTECTION_INSUFFICIENT"
    assert TREND_MISSING_DETAIL == "TREND_MISSING"


# --- C-2 不足が優先 ----------------------------------------------------------------------


def test_insufficient_wins_even_when_numbers_are_given() -> None:
    facts = _build(**_sufficient(data_sufficient=False))
    assert not facts.drawdown_from_peak_pct.is_determined
    assert not facts.peak_gain_pct.is_determined
    assert not facts.current_gain_pct.is_determined


def test_insufficient_does_not_validate_or_use_the_given_numbers() -> None:
    facts = _build(
        **_sufficient(
            data_sufficient=False,
            drawdown_from_peak_pct=math.nan,
            peak_gain_pct=math.inf,
            current_gain_pct=-1000.0,
        )
    )
    assert not facts.drawdown_from_peak_pct.is_determined
    assert not facts.peak_gain_pct.is_determined
    assert not facts.current_gain_pct.is_determined


# --- C-3 契約違反 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing",
    [
        subset
        for size in (1, 2, 3)
        for subset in itertools.combinations(
            ("drawdown_from_peak_pct", "peak_gain_pct", "current_gain_pct"), size
        )
    ],
    ids=lambda subset: "+".join(subset),
)
def test_sufficient_with_a_missing_number_is_a_contract_violation(missing: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="None であってはならない"):
        _build(**_sufficient(**dict.fromkeys(missing)))


@pytest.mark.parametrize("value", [-0.01, 100.01, math.nan, math.inf, -math.inf])
def test_a_drawdown_outside_the_range_or_not_finite_is_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="drawdown_from_peak_pct"):
        _build(**_sufficient(drawdown_from_peak_pct=value))


@pytest.mark.parametrize("name", ["peak_gain_pct", "current_gain_pct"])
@pytest.mark.parametrize("value", [-100.01, math.nan, math.inf, -math.inf])
def test_a_gain_below_the_floor_or_not_finite_is_rejected(name: str, value: float) -> None:
    with pytest.raises(ValueError, match=name):
        _build(**_sufficient(**{name: value}))


@pytest.mark.parametrize("name", ["drawdown_from_peak_pct", "peak_gain_pct", "current_gain_pct"])
@pytest.mark.parametrize("value", [True, False, "1.0", Decimal("1")])
def test_a_bool_or_non_number_is_not_accepted_as_a_number(name: str, value: object) -> None:
    with pytest.raises(TypeError, match=name):
        _build(**_sufficient(**{name: value}))


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_data_sufficient_must_be_a_bool(value: object) -> None:
    with pytest.raises(TypeError, match="data_sufficient"):
        _build(**_sufficient(data_sufficient=value))


@pytest.mark.parametrize("value", [1, 0, "down"])
def test_trend_is_down_must_be_a_bool_or_none(value: object) -> None:
    with pytest.raises(TypeError, match="trend_is_down"):
        _build(**_sufficient(trend_is_down=value))


def test_the_arguments_are_keyword_only() -> None:
    with pytest.raises(TypeError):
        build_price_facts(True, 1.0, 2.0, 3.0, False)  # type: ignore[misc]


def test_integers_are_accepted_and_converted_to_float() -> None:
    facts = _build(**_sufficient(drawdown_from_peak_pct=10, peak_gain_pct=30, current_gain_pct=20))
    values = (
        facts.drawdown_from_peak_pct.unwrap(),
        facts.peak_gain_pct.unwrap(),
        facts.current_gain_pct.unwrap(),
    )
    assert values == (10.0, 30.0, 20.0)
    assert all(isinstance(value, float) for value in values)


def test_the_boundaries_of_the_range_are_accepted() -> None:
    low = _build(**_sufficient(drawdown_from_peak_pct=0.0, peak_gain_pct=-100.0))
    high = _build(**_sufficient(drawdown_from_peak_pct=100.0))
    assert low.drawdown_from_peak_pct.unwrap() == 0.0
    assert low.peak_gain_pct.unwrap() == -100.0
    assert high.drawdown_from_peak_pct.unwrap() == 100.0


# --- C-4 純粋・dormant・閾値を持たない・peak を探さない(AST)-------------------------------------

_ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "jstock_advisor.domain.exit_architecture.determination",
        "jstock_advisor.domain.exit_architecture.price_regime",
        "jstock_advisor.domain.exit_architecture.vocabulary",
    }
)
#: 時計・I/O・乱数・環境・外部への出力
_FORBIDDEN_CALL_NAMES = frozenset(
    {"open", "print", "input", "exec", "eval", "compile", "__import__", "getattr", "setattr"}
)
#: peak を探す・集計する処理(peak の定義は指標の側にだけある)
_FORBIDDEN_PEAK_SEARCH_NAMES = frozenset({"max", "min", "sorted", "sum", "reversed", "filter"})
_FORBIDDEN_IDENTIFIERS = frozenset(
    {"bars", "high", "peak_price", "peak_date", "basis_date", "threshold", "thresholds"}
)


def _tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def _imported_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "相対 import は使わない"
            modules.add(node.module or "")
    return modules


def test_it_imports_only_the_same_package_modules_and_future() -> None:
    assert _imported_modules(_tree()) == _ALLOWED_IMPORTS


def test_it_does_not_import_the_metrics_or_momentum_types() -> None:
    imported = _imported_modules(_tree())
    assert not any("profit_protection" in name or "momentum" in name for name in imported)


def test_it_has_no_clock_io_random_or_peak_search_calls() -> None:
    called: set[str] = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    assert not called & _FORBIDDEN_CALL_NAMES
    assert not called & _FORBIDDEN_PEAK_SEARCH_NAMES


def test_it_has_no_numeric_literal_and_no_peak_or_threshold_identifiers() -> None:
    """数値リテラルを持たない(閾値・日数・係数を置かない)。peak・閾値に関わる名前も無い。"""
    tree = _tree()
    numbers = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ]
    assert numbers == []
    identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    identifiers |= {
        arg.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.arguments)
        for arg in (*node.args, *node.kwonlyargs)
    }
    assert not identifiers & _FORBIDDEN_IDENTIFIERS


def test_the_signature_is_exactly_the_five_values_and_takes_no_threshold() -> None:
    parameters = inspect.signature(build_price_facts).parameters
    assert list(parameters) == [
        "data_sufficient",
        "drawdown_from_peak_pct",
        "peak_gain_pct",
        "current_gain_pct",
        "trend_is_down",
    ]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters.values())
    assert all(p.default is inspect.Parameter.empty for p in parameters.values())


def _imports_the_module(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == _MODULE_NAME:
                return True
            if node.module == "jstock_advisor.domain.exit_architecture" and any(
                alias.name == "price_facts" for alias in node.names
            ):
                return True
        if isinstance(node, ast.Import) and any(a.name == _MODULE_NAME for a in node.names):
            return True
    return False


def test_the_import_scanner_detects_every_import_form() -> None:
    forms = (
        f"from {_MODULE_NAME} import build_price_facts",
        "from jstock_advisor.domain.exit_architecture import price_facts",
        f"import {_MODULE_NAME}",
    )
    assert all(_imports_the_module(form) for form in forms)
    assert not _imports_the_module("from jstock_advisor.domain.exit_architecture import decision")


def test_only_the_approved_shadow_receiver_imports_it() -> None:
    """import するのは #877 PR-2b-ii の受け手だけ(承認済みの限定接続)。全 src を走査する。"""
    scanned = 0
    importers: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path == _MODULE_PATH:
            continue
        scanned += 1
        if _imports_the_module(path.read_text(encoding="utf-8")):
            importers.append(path.relative_to(_SRC).as_posix())
    assert scanned > 100, "走査が空振りしていないこと"
    assert importers == ["services/price_regime_shadow_service.py"]


# --- C-5 実際の指標との同値 ----------------------------------------------------------------------

_APP_CONFIG = load_config()
_PP_CONFIG = _APP_CONFIG.profit_taking.profit_protection
_CALENDAR = BusinessCalendar.from_config(_APP_CONFIG.holiday_calendar)
_ENTRY = dt.date(2026, 1, 5)
_AS_OF = dt.date(2026, 8, 14)
_PEAK_DAY = dt.date(2026, 6, 1)
_AVG = Decimal("100")


def _bar(date: dt.date, high: str) -> PriceBar:
    price = Decimal(high)
    return PriceBar(date=date, open=price, high=price, low=price, close=price, volume=1000)


def _bars(entry_high: str, next_high: str, peak_high: str) -> list[PriceBar]:
    return [
        _bar(_ENTRY, entry_high),
        _bar(_CALENDAR.next_business_day(_ENTRY), next_high),
        _bar(_PEAK_DAY, peak_high),
    ]


def _metrics(
    bars: list[PriceBar],
    current: str,
    *,
    average: str = "100",
    adjusted: bool = False,
) -> ProfitProtectionMetrics:
    return compute_profit_protection_metrics(
        bars=bars,
        current_price=Decimal(current),
        average_purchase_price=Decimal(average),
        basis_date=_ENTRY,
        as_of_date=_AS_OF,
        ratio_adjustment_event_since_basis=adjusted,
        config=_PP_CONFIG,
        business_calendar=_CALENDAR,
    )


def _facts_from(metrics: ProfitProtectionMetrics, *, trend_is_down: bool | None) -> PriceFacts:
    return build_price_facts(
        data_sufficient=metrics.insufficient_data_reason is None,
        drawdown_from_peak_pct=metrics.drawdown_from_peak_pct,
        peak_gain_pct=metrics.peak_gain_pct,
        current_gain_pct=metrics.current_gain_pct,
        trend_is_down=trend_is_down,
    )


#: (名前, bars, 現在値, 期待する peak の高値)。★ peak = basis_date の翌日以降の日次高値の最大 ★
_SUFFICIENT_CASES = (
    ("plain_drawdown", _bars("101", "105", "160"), "140", "160"),
    # basis_date 当日の高値(300)は peak に含めない = 衝突しない別の peak の導入
    ("basis_day_high_is_excluded", _bars("300", "105", "120"), "110", "120"),
    ("small_gain_large_drawdown", _bars("100", "104", "105"), "90", "105"),
    ("peak_on_the_day_after_basis", _bars("100", "150", "120"), "130", "150"),
    ("no_drawdown_yet_but_below_peak", _bars("100", "103", "130"), "125", "130"),
)


@pytest.mark.parametrize(
    ("bars", "current", "expected_peak"),
    [case[1:] for case in _SUFFICIENT_CASES],
    ids=[case[0] for case in _SUFFICIENT_CASES],
)
def test_facts_equal_the_real_metrics_one_to_one(
    bars: list[PriceBar], current: str, expected_peak: str
) -> None:
    metrics = _metrics(bars, current)
    assert metrics.insufficient_data_reason is None
    # peak の定義は指標の側にある(basis_date 当日を除く・翌日以降の最大)ことを、ここで独立に固定する
    assert metrics.peak_price_since_entry == Decimal(expected_peak)
    assert metrics.peak_date is not None and metrics.peak_date > _ENTRY

    facts = _facts_from(metrics, trend_is_down=False)
    assert facts.drawdown_from_peak_pct.unwrap() == metrics.drawdown_from_peak_pct
    assert facts.peak_gain_pct.unwrap() == metrics.peak_gain_pct
    assert facts.current_gain_pct.unwrap() == metrics.current_gain_pct
    # 取り違えの検出: 3 つの数値は互いに異なる(同じ値を渡しても通る、を防ぐ)
    assert (
        len(
            {
                metrics.drawdown_from_peak_pct,
                metrics.peak_gain_pct,
                metrics.current_gain_pct,
            }
        )
        == 3
    )
    # 下落率は『指標の peak』からの値と一致する(別の peak から再計算した値ではない)
    recomputed = float(1 - Decimal(current) / Decimal(expected_peak)) * 100
    assert facts.drawdown_from_peak_pct.unwrap() == pytest.approx(recomputed)


def test_a_peak_before_the_basis_date_would_give_a_different_drawdown() -> None:
    """basis_date 当日の高値(300)から下落率を作ると 63.3% になる。指標の peak(120)なら 8.3%。"""
    metrics = _metrics(_bars("300", "105", "120"), "110")
    facts = _facts_from(metrics, trend_is_down=False)
    wrong = float(1 - Decimal("110") / Decimal("300")) * 100
    assert facts.drawdown_from_peak_pct.unwrap() != pytest.approx(wrong)
    assert facts.drawdown_from_peak_pct.unwrap() == pytest.approx(8.3333, abs=1e-3)


_INSUFFICIENT_CASES = (
    ("bad_average_price", _bars("100", "105", "160"), "140", {"average": "0"}),
    ("split_after_basis", _bars("100", "105", "160"), "140", {"adjusted": True}),
    ("no_bars_after_basis", [_bar(_ENTRY, "100")], "140", {}),
    (
        "bars_missing_right_after_basis",
        [_bar(_ENTRY, "100"), _bar(_PEAK_DAY, "160")],
        "140",
        {},
    ),
    ("invalid_peak", _bars("100", "0", "0"), "140", {}),
)


@pytest.mark.parametrize(
    ("bars", "current", "options"),
    [case[1:] for case in _INSUFFICIENT_CASES],
    ids=[case[0] for case in _INSUFFICIENT_CASES],
)
def test_insufficient_real_metrics_give_undetermined_numbers(
    bars: list[PriceBar], current: str, options: dict[str, object]
) -> None:
    metrics = _metrics(bars, current, **options)  # type: ignore[arg-type]
    assert metrics.insufficient_data_reason is not None
    facts = _facts_from(metrics, trend_is_down=True)
    assert not facts.drawdown_from_peak_pct.is_determined
    assert not facts.peak_gain_pct.is_determined
    assert not facts.current_gain_pct.is_determined
    assert facts.trend.unwrap() is TrendReading.DOWN
    # 自由文の理由は持ち出さない
    for number in (facts.drawdown_from_peak_pct, facts.peak_gain_pct, facts.current_gain_pct):
        assert number.detail == INSUFFICIENT_DETAIL
        assert metrics.insufficient_data_reason not in number.detail


def test_a_current_price_above_the_peak_high_is_rejected_by_the_range_check() -> None:
    """★ 既知の端 ★: 現在値が peak の高値を上回ると、指標の下落率は負になり、PriceFacts は拒否する。

    黙って 0 に丸めない(値を捏造しない)。呼び出し側(PR-2b)がこの例外を、主の処理へ影響させない
    形で扱う必要がある(設計の確認事項。PR 本文に記載)。
    """
    metrics = _metrics(_bars("100", "105", "160"), "170")
    assert metrics.drawdown_from_peak_pct is not None and metrics.drawdown_from_peak_pct < 0
    with pytest.raises(ValueError, match="drawdown_from_peak_pct"):
        _facts_from(metrics, trend_is_down=False)


# --- C-6 記述的な比較(合否にしない)-------------------------------------------------------------

#: ★ 未承認の比較用の仮の値(承認済みの値ではない。USER は 3 値を保留している)★
#: テストの中にだけ置く。src のどこからも読めない(下のテストで固定する)
_NOT_APPROVED_COMPARISON_ONLY_THRESHOLDS = RegimeThresholds(
    peak_warning_drawdown_pct=5.0,
    downtrend_confirmed_drawdown_pct=10.0,
    breakdown_drawdown_pct=20.0,
)

_PEAK_HIGHS = ("104", "110", "125", "150", "200")
_CURRENTS = ("70", "85", "95", "100", "105", "120", "140")


def _comparison_table() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for peak_high, current in itertools.product(_PEAK_HIGHS, _CURRENTS):
        if Decimal(current) > Decimal(peak_high):
            continue  # 現在値が peak を上回る組は PriceFacts が拒否する(上のテスト)
        metrics = _metrics(_bars("100", "103", peak_high), current)
        facts = _facts_from(metrics, trend_is_down=False)
        regime = classify_price_regime(
            facts, _NOT_APPROVED_COMPARISON_ONLY_THRESHOLDS, ReliabilityClass.RELIABLE
        )
        rows.append(
            {
                "peak_high": peak_high,
                "current": current,
                "old_signal": metrics.signal_label.value,
                "regime": regime.state.unwrap(),
                "current_gain_pct": metrics.current_gain_pct,
                "drawdown_pct": metrics.drawdown_from_peak_pct,
            }
        )
    return rows


def test_the_comparison_table_is_not_empty_and_contains_a_known_hole() -> None:
    """★ 何も assert しないテストにしない ★: 表が空なら落ちる。既知の穴の行が 1 行以上あること。

    既知の穴(baseline): 含み益が小さく吐き出しが大きい行で、現行は無信号(NONE)だが、
    新しい regime は健全でない(含み益を入口 gate にしないため)。表の内容は固定しない。
    """
    rows = _comparison_table()
    assert len(rows) > 0
    holes = [
        row
        for row in rows
        if row["old_signal"] == "NONE"
        and row["regime"] is not RegimeState.HEALTHY
        and float(row["current_gain_pct"]) < _PP_CONFIG.candidate.min_current_gain_pct  # type: ignore[arg-type]
    ]
    assert holes, "既知の穴(現行は無信号・regime は非健全)の行が 1 つも無い"
    # 穴の行は『含み益が小さく、下落が大きい』行である
    assert all(float(row["drawdown_pct"]) >= 5.0 for row in holes)  # type: ignore[arg-type]


def test_every_row_has_both_a_current_signal_and_a_determined_regime() -> None:
    for row in _comparison_table():
        assert row["old_signal"] in {"NONE", "CANDIDATE", "STRONG"}
        assert isinstance(row["regime"], RegimeState)


def test_the_not_approved_comparison_values_are_not_readable_from_src() -> None:
    """未承認の仮の値が『既にある値』として扱われる経路を閉じる: src のどこにも現れない。"""
    scanned = 0
    for path in sorted(_SRC.rglob("*.py")):
        scanned += 1
        text = path.read_text(encoding="utf-8")
        assert "NOT_APPROVED_COMPARISON" not in text, path
    assert scanned > 100
