"""価格の経路の事実(`PricePathFacts`)と、サービスの非永続 field の契約テスト(Issue #877 PR-2b-i)。

設計: #877 PR-2 設計 rev3(…6182483078)・設計レビュー PASS(…6182584823)。

  P-1 暫定写像: DOWNTREND・STRONG_DOWNTREND = 下落 / 他 = 非下落 / None = None(5 値の完全一致 + 版)
  P-2 実際の `compute_profit_protection_metrics` の出力と 1 対 1(★ 3 つの数値は互いに異なる値 ★)
  P-3 純粋・exit_architecture を import しない(#606 の網)・数値リテラルなし・判定側が import しない
  S-1 ★ 組み立てが失敗しても analyze の結果(recommendation・data_error・audit)が不変 ★
  S-2 field は compare=False・repr=False・値が判定・条件分岐・保存・通知へ流れない(AST)
  S-3 ★ ProfitTakingService の import の閉包に exit_architecture が無い ★(CLI の根が到達しない根拠)

本 PR は #606 の網・C0・handler に触れない(受け手と局所許可は PR-2b-ii)。
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import RecommendationType, TrendClassification
from jstock_advisor.domain.signals import price_path_facts as module
from jstock_advisor.domain.signals.price_path_facts import (
    TREND_IS_DOWN,
    TREND_IS_DOWN_MAP_VERSION,
    PricePathFacts,
    build_price_path_facts,
    trend_is_down,
)
from jstock_advisor.domain.signals.profit_protection import (
    ProfitProtectionMetrics,
    compute_profit_protection_metrics,
)
from jstock_advisor.interfaces.types import PriceBar
from jstock_advisor.services import profit_taking_service as service_module
from jstock_advisor.services.profit_taking_service import ProfitTakingOutcome
from tests.unit.test_issue_606_production_connection_gate import _module_files, _reachable
from tests.unit.test_profit_protection import (
    _CALENDAR,
    _CONFIG,
    _ENTRY,
    _bars,
)
from tests.unit.test_profit_taking_corporate_action_shadow import (
    _AFTER_BASIS,
    _NOW,
    _SHADOW_OFF,
    _event,
    _holding,
    _make_service,
    _normalized,
    _RecordingProvider,
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"
_MODULE_PATH = Path(module.__file__)
_SERVICE_PATH = Path(service_module.__file__)
_MODULE_NAME = "jstock_advisor.domain.signals.price_path_facts"
_AS_OF = dt.date(2026, 8, 14)

# --- P-1 暫定写像 --------------------------------------------------------------------------


def test_the_provisional_trend_map_is_exactly_the_approved_one() -> None:
    assert dict(TREND_IS_DOWN) == {
        TrendClassification.STRONG_UPTREND: False,
        TrendClassification.UPTREND: False,
        TrendClassification.NEUTRAL: False,
        TrendClassification.DOWNTREND: True,
        TrendClassification.STRONG_DOWNTREND: True,
    }
    assert set(TREND_IS_DOWN) == set(
        TrendClassification
    )  # 全メンバーを覆う(新メンバーは意図した変更)
    assert TREND_IS_DOWN_MAP_VERSION == "shadow-trend-map-v0"


@pytest.mark.parametrize("trend", list(TrendClassification))
def test_trend_is_down_follows_the_map(trend: TrendClassification) -> None:
    assert trend_is_down(trend) is TREND_IS_DOWN[trend]


def test_no_trend_gives_none_and_a_non_enum_is_rejected() -> None:
    assert trend_is_down(None) is None
    for bad in ("DOWNTREND", 1, True, object()):
        with pytest.raises(TypeError, match="TrendClassification"):
            trend_is_down(bad)  # type: ignore[arg-type]


# --- P-2 実際の指標との 1 対 1 -------------------------------------------------------------


def _metrics(
    current: str, bars: list[PriceBar], *, average: str = "920"
) -> ProfitProtectionMetrics:
    return compute_profit_protection_metrics(
        bars=bars,
        current_price=Decimal(current),
        average_purchase_price=Decimal(average),
        basis_date=_ENTRY,
        as_of_date=_AS_OF,
        ratio_adjustment_event_since_basis=False,
        config=_CONFIG,
        business_calendar=_CALENDAR,
    )


_SUFFICIENT = (
    ("plain_drawdown", "1400", "1454.5"),
    ("deeper_drawdown", "1200", "1454.5"),
    ("small_gain_large_drawdown", "950", "1000"),
    ("peak_barely_above_cost", "930", "1005"),
)


@pytest.mark.parametrize(
    ("current", "peak_high"), [case[1:] for case in _SUFFICIENT], ids=[c[0] for c in _SUFFICIENT]
)
@pytest.mark.parametrize("trend", [*TrendClassification, None])
def test_facts_equal_the_real_metrics_one_to_one(
    current: str, peak_high: str, trend: TrendClassification | None
) -> None:
    metrics = _metrics(current, _bars(Decimal(peak_high)))
    assert metrics.insufficient_data_reason is None
    facts = build_price_path_facts(metrics, trend)
    assert facts == PricePathFacts(
        data_sufficient=True,
        drawdown_from_peak_pct=metrics.drawdown_from_peak_pct,
        peak_gain_pct=metrics.peak_gain_pct,
        current_gain_pct=metrics.current_gain_pct,
        trend_is_down=None if trend is None else TREND_IS_DOWN[trend],
    )
    # 取り違えの検出: 3 つの数値は互いに異なる(同じ値だと入れ替えが生き残る)
    assert len({facts.drawdown_from_peak_pct, facts.peak_gain_pct, facts.current_gain_pct}) == 3


def test_the_numbers_are_passed_through_without_recomputation() -> None:
    """指標の値と『同一の float』(再計算・丸め・変換をしていない)。"""
    metrics = _metrics("1400", _bars(Decimal("1454.5")))
    facts = build_price_path_facts(metrics, TrendClassification.NEUTRAL)
    assert facts.drawdown_from_peak_pct is metrics.drawdown_from_peak_pct
    assert facts.peak_gain_pct is metrics.peak_gain_pct
    assert facts.current_gain_pct is metrics.current_gain_pct


_INSUFFICIENT = (
    ("bad_average_price", "1400", {"average": "0"}),
    ("no_bars_after_basis", "1400", {"bars": "only_basis"}),
)


@pytest.mark.parametrize(
    ("current", "options"), [case[1:] for case in _INSUFFICIENT], ids=[c[0] for c in _INSUFFICIENT]
)
def test_insufficient_metrics_give_no_numbers_and_keep_the_trend(
    current: str, options: dict[str, str]
) -> None:
    bars = _bars(Decimal("1454.5"))
    if options.get("bars") == "only_basis":
        bars = bars[:1]
    metrics = _metrics(current, bars, average=options.get("average", "920"))
    assert metrics.insufficient_data_reason is not None
    facts = build_price_path_facts(metrics, TrendClassification.DOWNTREND)
    assert facts == PricePathFacts(
        data_sufficient=False,
        drawdown_from_peak_pct=None,
        peak_gain_pct=None,
        current_gain_pct=None,
        trend_is_down=True,
    )


def test_the_carrier_has_exactly_the_five_values_of_the_regime_supplier() -> None:
    """PR-2a の `build_price_facts` の引数と同じ名前・同じ順(受け手が 1 対 1 に渡せる)。"""
    assert [f.name for f in dataclasses.fields(PricePathFacts)] == [
        "data_sufficient",
        "drawdown_from_peak_pct",
        "peak_gain_pct",
        "current_gain_pct",
        "trend_is_down",
    ]
    assert PricePathFacts.__dataclass_params__.frozen  # type: ignore[attr-defined]


# --- P-3 純粋・#606 の網・判定側が import しない ------------------------------------------------


def _imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            found.add(node.module or "")
    return found


def test_it_imports_only_the_standard_library_the_enums_and_the_metrics_type() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    assert _imports(tree) == {
        "__future__",
        "collections.abc",
        "dataclasses",
        "typing",
        "jstock_advisor.domain.entities.enums",
        "jstock_advisor.domain.signals.profit_protection",
    }


def test_it_has_no_numeric_literal_no_clock_and_no_io() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    numbers = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, (int, float))
        and not isinstance(n.value, bool)
    ]
    assert numbers == []
    called = {
        n.func.id if isinstance(n.func, ast.Name) else n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
    }
    assert not called & {"open", "print", "input", "now", "today", "random", "getenv"}


def test_only_the_service_and_the_shadow_receiver_import_it_among_src_modules() -> None:
    """判定側(利確・売却・購入の判定)は暫定写像を import しない。全 src を走査(列挙しない)。"""
    importers: list[str] = []
    scanned = 0
    for path in sorted(_SRC.rglob("*.py")):
        if path == _MODULE_PATH:
            continue
        scanned += 1
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == _MODULE_NAME:
                importers.append(path.relative_to(_SRC).as_posix())
            elif (
                isinstance(node, ast.ImportFrom) and node.module == "jstock_advisor.domain.signals"
            ):
                if any(alias.name == "price_path_facts" for alias in node.names):
                    importers.append(path.relative_to(_SRC).as_posix())
            elif isinstance(node, ast.Import) and any(a.name == _MODULE_NAME for a in node.names):
                importers.append(path.relative_to(_SRC).as_posix())
    assert scanned > 100
    # 値を詰めるサービスと、値を受け取る Shadow の受け手(#877 PR-2b-ii)だけ。判定側は import しない
    assert importers == [
        "services/price_regime_shadow_service.py",
        "services/profit_taking_service.py",
    ]


# --- S-3 サービスの閉包に exit_architecture が無い ---------------------------------------------


def test_the_service_import_closure_has_no_exit_architecture_module() -> None:
    """★ CLI の根が exit_architecture へ到達しない根拠 ★(#606: CLI の根に免除は無い)。"""
    known = _module_files()
    service = "jstock_advisor.services.profit_taking_service"
    assert service in known
    closure = _reachable({service}, known)
    assert _MODULE_NAME in closure  # 閉包の探索が実際に新 module まで届いている(空振りでない)
    assert [m for m in closure if "exit_architecture" in m] == []


# --- S-1 失敗の隔離・S-2 field の性質 ----------------------------------------------------------


def _analyzed(
    monkeypatch: pytest.MonkeyPatch, recommendation_type: RecommendationType
) -> tuple[ProfitTakingOutcome, dict[str, Any]]:
    service, audit = _make_service(
        monkeypatch,
        _RecordingProvider([_event(_AFTER_BASIS)]),
        _SHADOW_OFF,
        recommendation_type,
    )
    outcome = service.analyze(_holding("2914"), _NOW)
    return outcome, _normalized(outcome, audit)


@pytest.mark.parametrize(
    "recommendation_type",
    [RecommendationType.FULL_PROFIT_TAKE, RecommendationType.HOLD],
    ids=["recommendation", "hold"],
)
def test_facts_are_set_for_the_recommendation_path_and_for_hold(
    monkeypatch: pytest.MonkeyPatch, recommendation_type: RecommendationType
) -> None:
    """HOLD(推奨なし)でも設定する(主題は保有の価格経路で、推奨の有無とは独立)。"""
    outcome, _ = _analyzed(monkeypatch, recommendation_type)
    assert (outcome.recommendation is None) == (recommendation_type is RecommendationType.HOLD)
    assert isinstance(outcome.price_path_facts, PricePathFacts)
    assert outcome.data_error is None


@pytest.mark.parametrize(
    "recommendation_type",
    [RecommendationType.FULL_PROFIT_TAKE, RecommendationType.HOLD],
    ids=["recommendation", "hold"],
)
@pytest.mark.parametrize(
    "boom",
    [RuntimeError("boom"), ValueError("bad"), TypeError("type"), KeyError("k")],
    ids=["runtime", "value", "type", "key"],
)
def test_a_failing_assembly_never_changes_the_existing_business_result(
    monkeypatch: pytest.MonkeyPatch, recommendation_type: RecommendationType, boom: Exception
) -> None:
    expected_outcome, expected = _analyzed(monkeypatch, recommendation_type)
    assert expected_outcome.price_path_facts is not None

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise boom

    monkeypatch.setattr(service_module, "build_price_path_facts", _raise)
    outcome, actual = _analyzed(monkeypatch, recommendation_type)

    assert actual == expected  # recommendation・data_error・audit は不変
    assert outcome.audit_id == expected_outcome.audit_id
    assert outcome.price_path_facts is None


def test_a_failure_logs_only_the_error_type(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "SENTINEL-MESSAGE-123"

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(secret)

    monkeypatch.setattr(service_module, "build_price_path_facts", _raise)
    with caplog.at_level("WARNING"):
        _analyzed(monkeypatch, RecommendationType.HOLD)
    assert "RuntimeError" in caplog.text
    assert secret not in caplog.text


def test_the_service_passes_the_snapshot_trend_and_the_computed_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[Any, Any]] = []
    original = service_module.build_price_path_facts

    def _spy(metrics: Any, trend: Any) -> Any:
        seen.append((metrics, trend))
        return original(metrics, trend)

    monkeypatch.setattr(service_module, "build_price_path_facts", _spy)
    outcome, _ = _analyzed(monkeypatch, RecommendationType.FULL_PROFIT_TAKE)

    assert len(seen) == 1
    metrics, trend = seen[0]
    assert isinstance(metrics, ProfitProtectionMetrics)
    assert isinstance(trend, TrendClassification)
    assert outcome.price_path_facts == original(metrics, trend)


def test_the_field_is_not_compared_not_shown_and_defaults_to_none() -> None:
    field = {f.name: f for f in dataclasses.fields(ProfitTakingOutcome)}["price_path_facts"]
    assert field.compare is False
    assert field.repr is False
    assert field.default is None
    base = ProfitTakingOutcome("1000", None, None)
    assert base.price_path_facts is None
    with_facts = dataclasses.replace(
        base, price_path_facts=PricePathFacts(True, 1.0, 2.0, 3.0, False)
    )
    assert with_facts == base
    assert "price_path_facts" not in repr(with_facts)


def _service_tree() -> ast.Module:
    return ast.parse(_SERVICE_PATH.read_text(encoding="utf-8"))


_FACT_NAMES = frozenset({"price_path_facts", "_price_path_facts", "build_price_path_facts"})


def _mentions(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in _FACT_NAMES:
            return True
        if isinstance(child, ast.Attribute) and child.attr in _FACT_NAMES:
            return True
        if isinstance(child, ast.keyword) and child.arg in _FACT_NAMES:
            return True
    return False


def test_the_facts_never_reach_a_condition_a_decision_or_a_store() -> None:
    """★ 値が判定・条件分岐・保存・通知へ流れない(AST)★。

    名前が現れてよい文は次だけ: import / `ProfitTakingOutcome` の field 宣言 / 値を詰める private
    method の本体 / `ProfitTakingOutcome(...)` を返す 2 か所の return。条件(if / while / 三項 /
    比較 / 論理)・代入・呼び出しの引数(上の kwarg を除く)に現れたら落ちる。
    """
    tree = _service_tree()
    allowed_returns = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            assert not _mentions(node.test), ast.unparse(node)[:80]
        elif isinstance(
            node, (ast.Compare, ast.BoolOp, ast.UnaryOp, ast.Assign, ast.AugAssign, ast.Expr)
        ):
            assert not _mentions(node), ast.unparse(node)[:80]
        elif isinstance(node, ast.Return) and node.value is not None and _mentions(node):
            call = node.value
            assert isinstance(call, ast.Call), ast.unparse(node)
            if ast.unparse(call.func) == "ProfitTakingOutcome":
                others = [k for k in call.keywords if k.arg not in _FACT_NAMES]
                assert not any(_mentions(k.value) for k in others)
                allowed_returns += 1
            else:  # `_price_path_facts` 自身の return(値を作って返す)は許す
                assert ast.unparse(call.func) == "build_price_path_facts", ast.unparse(node)
    assert allowed_returns == 2  # 推奨あり・HOLD の 2 経路だけ


def test_only_two_outcome_constructions_carry_the_facts_and_the_error_paths_do_not() -> None:
    tree = _service_tree()
    carrying = 0
    plain = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ProfitTakingOutcome"
        ):
            if any(k.arg == "price_path_facts" for k in node.keywords):
                carrying += 1
            else:
                plain += 1
    assert carrying == 2
    assert plain >= 3  # data_error の経路(snapshot 取得失敗・入力不正・数量の不変条件違反 等)
