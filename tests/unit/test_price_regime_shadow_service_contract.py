"""価格の経路の Shadow の受け手と handler の合流点の契約テスト(Issue #877 PR-2b-ii)。

設計: #877 PR-2 設計 rev3(…6182483078)・設計レビュー PASS(…6182584823)・
USER 決定 #122 …6182420023 の 1(PR-2b に限定した Shadow 接続の承認)。

  R-1 ★ 閾値が供給されないとき(PR-2b では常に)は、facts の組み立ても分類もしない ★(None)
  R-2 閾値の供給元は常に None を返し、`RegimeThresholds(...)` を構築しない
  R-3 閾値が供給されたとき、facts は PR-2a の `build_price_facts` へ 1 対 1(★ 互いに異なる値 ★)
  R-4 ★ どの内部の失敗でも例外を送出しない ★(ログは型名だけ)
  R-5 ★ `classify_price_regime` を呼ばない ★(信頼性の供給元が未決定。RELIABLE にしない)
  R-6 純粋: import は標準 library + 同 package の型だけ・I/O・時計・保存・通知なし(AST)
  H-1 handler: 受け手の呼び出しは『保存・通知・保有の評価記録の後』(`return result` の直前)の
      2 か所・式文(戻り値を使わない)
  H-2 ★ 受け手が例外を投げても handler の結果が不変 ★(推奨の経路・HOLD の経路)

#606 の局所許可・C0 の importer・サービスの閉包は、それぞれ
test_issue_606_production_connection_gate.py・test_exit_architecture_c0.py・
test_price_path_facts_contract.py が固定する。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import NotificationStatus
from jstock_advisor.domain.exit_architecture import price_regime
from jstock_advisor.domain.exit_architecture.price_facts import build_price_facts
from jstock_advisor.domain.exit_architecture.price_regime import RegimeThresholds
from jstock_advisor.domain.signals.price_path_facts import PricePathFacts
from jstock_advisor.lambda_handlers import holdings_watchlist_handler as handler_module
from jstock_advisor.services import price_regime_shadow_service as receiver
from jstock_advisor.services.line_notification_service import NotificationOutcome
from jstock_advisor.services.price_regime_shadow_service import (
    PriceRegimeObservation,
    observe_price_regime_shadow,
    resolve_price_regime_thresholds,
)
from tests.unit.test_holdings_watchlist_handler import (
    _NoSignalOutcome,
    _run_attention_scenario,
    _run_holding_task_with_profit_taking_outcome,
)

_RECEIVER_PATH = Path(receiver.__file__)
_HANDLER_PATH = Path(handler_module.__file__)

#: テスト内だけの合成の閾値(未承認の値ではない。受け手が値を使わないことの確認のための入れ物)
_SYNTHETIC_THRESHOLDS = RegimeThresholds(
    peak_warning_drawdown_pct=1.0,
    downtrend_confirmed_drawdown_pct=2.0,
    breakdown_drawdown_pct=3.0,
)
#: 3 つの数値は互いに異なる(取り違えが偶然の一致で隠れない)
_FACTS = PricePathFacts(
    data_sufficient=True,
    drawdown_from_peak_pct=12.5,
    peak_gain_pct=60.0,
    current_gain_pct=40.0,
    trend_is_down=True,
)


class _Outcome:
    def __init__(self, price_path_facts: PricePathFacts | None) -> None:
        self.price_path_facts = price_path_facts


def _provider(value: RegimeThresholds | None) -> Any:
    calls: list[int] = []

    def _call() -> RegimeThresholds | None:
        calls.append(1)
        return value

    _call.calls = calls  # type: ignore[attr-defined]
    return _call


# --- R-1 / R-2 閾値が無い -------------------------------------------------------------------


def test_without_thresholds_nothing_is_built_and_nothing_is_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[Any] = []
    monkeypatch.setattr(receiver, "build_price_facts", lambda **kw: built.append(kw))
    classified: list[Any] = []
    monkeypatch.setattr(price_regime, "classify_price_regime", lambda *a, **k: classified.append(a))
    provider = _provider(None)

    assert observe_price_regime_shadow(_Outcome(_FACTS), thresholds_provider=provider) is None
    assert provider.calls == [1]
    assert built == []
    assert classified == []


def test_the_default_provider_never_supplies_thresholds() -> None:
    assert resolve_price_regime_thresholds() is None
    assert observe_price_regime_shadow(_Outcome(_FACTS)) is None


def test_the_receiver_never_constructs_regime_thresholds() -> None:
    tree = ast.parse(_RECEIVER_PATH.read_text(encoding="utf-8"))
    calls = {
        n.func.id if isinstance(n.func, ast.Name) else n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
    }
    assert "RegimeThresholds" not in calls


# --- R-3 閾値が供給されたとき -----------------------------------------------------------------


def test_with_thresholds_the_facts_follow_the_regime_supplier_one_to_one() -> None:
    assert len({_FACTS.drawdown_from_peak_pct, _FACTS.peak_gain_pct, _FACTS.current_gain_pct}) == 3
    observation = observe_price_regime_shadow(
        _Outcome(_FACTS), thresholds_provider=_provider(_SYNTHETIC_THRESHOLDS)
    )
    assert observation == PriceRegimeObservation(
        price_facts=build_price_facts(
            data_sufficient=True,
            drawdown_from_peak_pct=12.5,
            peak_gain_pct=60.0,
            current_gain_pct=40.0,
            trend_is_down=True,
        )
    )
    assert observation is not None
    assert observation.price_facts.drawdown_from_peak_pct.unwrap() == 12.5
    assert observation.price_facts.peak_gain_pct.unwrap() == 60.0
    assert observation.price_facts.current_gain_pct.unwrap() == 40.0


def test_insufficient_facts_are_carried_as_undetermined() -> None:
    insufficient = PricePathFacts(False, None, None, None, None)
    observation = observe_price_regime_shadow(
        _Outcome(insufficient), thresholds_provider=_provider(_SYNTHETIC_THRESHOLDS)
    )
    assert observation is not None
    assert not observation.price_facts.drawdown_from_peak_pct.is_determined
    assert not observation.price_facts.trend.is_determined


def test_missing_facts_give_none_even_with_thresholds() -> None:
    assert (
        observe_price_regime_shadow(
            _Outcome(None), thresholds_provider=_provider(_SYNTHETIC_THRESHOLDS)
        )
        is None
    )


# --- R-4 例外を送出しない ---------------------------------------------------------------------


class _RaisingFacts:
    @property
    def price_path_facts(self) -> PricePathFacts | None:
        raise RuntimeError("SENTINEL-FACTS")


def _raising_provider() -> RegimeThresholds | None:
    raise RuntimeError("SENTINEL-PROVIDER")


@pytest.mark.parametrize(
    ("outcome", "provider"),
    [
        (_Outcome(_FACTS), _raising_provider),
        (_RaisingFacts(), _provider(_SYNTHETIC_THRESHOLDS)),
        (object(), _provider(_SYNTHETIC_THRESHOLDS)),  # 属性が無い(duck-type の代替実装)
        (_Outcome(PricePathFacts(True, 150.0, 60.0, 40.0, True)), _provider(_SYNTHETIC_THRESHOLDS)),
        (_Outcome(PricePathFacts(True, None, 60.0, 40.0, True)), _provider(_SYNTHETIC_THRESHOLDS)),
        (
            _Outcome(PricePathFacts(True, float("nan"), 6.0, 4.0, True)),
            _provider(_SYNTHETIC_THRESHOLDS),
        ),
    ],
    ids=["provider", "facts_property", "no_attribute", "out_of_range", "missing", "nan"],
)
def test_no_internal_failure_is_raised_and_the_log_has_only_the_type(
    outcome: Any, provider: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        assert observe_price_regime_shadow(outcome, thresholds_provider=provider) is None
    assert "error_type=" in caplog.text
    assert "SENTINEL" not in caplog.text


def test_a_failing_supplier_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(**kwargs: Any) -> Any:
        raise ValueError("boom")

    monkeypatch.setattr(receiver, "build_price_facts", _boom)
    assert (
        observe_price_regime_shadow(
            _Outcome(_FACTS), thresholds_provider=_provider(_SYNTHETIC_THRESHOLDS)
        )
        is None
    )


# --- R-5 / R-6 分類を呼ばない・純粋 -----------------------------------------------------------


def _tree() -> ast.Module:
    return ast.parse(_RECEIVER_PATH.read_text(encoding="utf-8"))


def test_classification_is_never_called_or_imported() -> None:
    """★ 信頼性の供給元が未決定のため、分類を呼ばない(RELIABLE を既定にしない)★。"""
    names = {n.id for n in ast.walk(_tree()) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)
    }
    imported = {
        alias.name for n in ast.walk(_tree()) if isinstance(n, ast.ImportFrom) for alias in n.names
    }
    assert "classify_price_regime" not in names | imported
    assert "ReliabilityClass" not in names | imported


def test_even_with_thresholds_classify_is_not_called(monkeypatch: pytest.MonkeyPatch) -> None:
    classified: list[Any] = []
    monkeypatch.setattr(price_regime, "classify_price_regime", lambda *a, **k: classified.append(a))
    observe_price_regime_shadow(
        _Outcome(_FACTS), thresholds_provider=_provider(_SYNTHETIC_THRESHOLDS)
    )
    assert classified == []


def test_the_receiver_imports_only_the_standard_library_and_the_pure_types() -> None:
    imports: set[str] = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imports.add(node.module or "")
    assert imports == {
        "__future__",
        "logging",
        "collections.abc",
        "dataclasses",
        "typing",
        "jstock_advisor.domain.exit_architecture.price_facts",
        "jstock_advisor.domain.exit_architecture.price_regime",
        "jstock_advisor.domain.signals.price_path_facts",
    }


def test_the_receiver_has_no_io_clock_store_or_notification_call() -> None:
    called = {
        n.func.id if isinstance(n.func, ast.Name) else n.func.attr
        for n in ast.walk(_tree())
        if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
    }
    forbidden = {
        "open",
        "print",
        "now",
        "today",
        "getenv",
        "put_item",
        "save",
        "insert_if_absent",
        "record",
        "send",
        "notify",
        "push",
    }
    assert not called & forbidden
    identifiers = {n.id for n in ast.walk(_tree()) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(_tree()) if isinstance(n, ast.Attribute)
    }
    for token in ("boto3", "repository", "audit", "line", "environ", "queue"):
        assert not [i for i in identifiers if token in i.lower()], token


# --- H-1 handler の合流点 ----------------------------------------------------------------------


def _handler_tree() -> ast.Module:
    return ast.parse(_HANDLER_PATH.read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(name)


def _call_name(statement: ast.stmt) -> str | None:
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
        func = statement.value.func
        return func.id if isinstance(func, ast.Name) else None
    return None


def _statement_lists(node: ast.AST) -> list[list[ast.stmt]]:
    lists: list[list[ast.stmt]] = []
    for child in ast.walk(node):
        for field in ("body", "orelse", "finalbody"):
            value = getattr(child, field, None)
            if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                lists.append(value)
    return lists


def test_the_receiver_is_called_exactly_twice_right_after_persistence_and_before_return() -> None:
    """★ 2 か所だけ(推奨・HOLD の経路)。どちらも保有の評価記録の保存の直後・return の直前 ★。"""
    function = _function(_handler_tree(), "_analyze_one_holding")
    sites = 0
    for statements in _statement_lists(function):
        for index, statement in enumerate(statements):
            if _call_name(statement) != "_observe_price_regime_shadow_safely":
                continue
            sites += 1
            assert isinstance(statements[index + 1], ast.Return), "return の直前"
            assert _call_name(statements[index - 1]) == "_persist_holding_evaluation_record"
    assert sites == 2


def test_the_receiver_is_referenced_only_through_the_isolating_helper() -> None:
    tree = _handler_tree()
    direct = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "observe_price_regime_shadow"
    ]
    assert len(direct) == 1  # helper の中の 1 回だけ
    helper = _function(tree, "_observe_price_regime_shadow_safely")
    assert direct[0] in list(ast.walk(helper))
    # helper の呼び出しは全て式文(戻り値を使わない)
    helper_calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_observe_price_regime_shadow_safely"
    ]
    expression_calls = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Expr)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
        and n.value.func.id == "_observe_price_regime_shadow_safely"
    ]
    assert len(helper_calls) == len(expression_calls) == 2


def test_the_helper_isolates_any_exception_of_the_receiver(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(outcome: Any) -> Any:
        raise RuntimeError("SENTINEL-RECEIVER")

    monkeypatch.setattr(handler_module, "observe_price_regime_shadow", _boom)
    with caplog.at_level("WARNING"):
        handler_module._observe_price_regime_shadow_safely(_NoSignalOutcome())  # type: ignore[arg-type]
    assert "RuntimeError" in caplog.text
    assert "SENTINEL" not in caplog.text


# --- H-2 受け手が例外を投げても handler の結果が不変 ------------------------------------------


def _hold_result(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    return _run_holding_task_with_profit_taking_outcome(monkeypatch, _NoSignalOutcome())


def _recommendation_result(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    outcome = NotificationOutcome(
        status=NotificationStatus.SENT,
        sent=True,
        data_quality_blocked=False,
        notification_intent=None,
    )
    return _run_attention_scenario(monkeypatch, outcome)


@pytest.mark.parametrize("scenario", [_hold_result, _recommendation_result], ids=["hold", "rec"])
def test_a_failing_receiver_never_changes_the_handler_result(
    monkeypatch: pytest.MonkeyPatch, scenario: Any
) -> None:
    calls: list[Any] = []
    real = handler_module.observe_price_regime_shadow

    def _spy(outcome: Any) -> Any:
        calls.append(outcome)
        return real(outcome)

    monkeypatch.setattr(handler_module, "observe_price_regime_shadow", _spy)
    baseline = scenario(monkeypatch)
    assert len(calls) == 1  # 合流点で 1 回だけ呼ばれる(閾値が無いので何も組み立てない)

    def _boom(outcome: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(handler_module, "observe_price_regime_shadow", _boom)
    assert scenario(monkeypatch) == baseline


# --- Lambda artifact への同梱(handler の起動失敗を防ぐ)--------------------------------------------


def test_the_handler_module_imports_and_the_receiver_modules_ship_with_the_existing_artifact() -> (
    None
):
    """handler は受け手(exit_architecture の 7 module)を top-level で import する。

    import が失敗すると保有バッチの handler 全体が起動に失敗する。ここで (a) handler の import が
    成立すること (b) 受け手と exit_architecture の module が、既存の Lambda の artifact
    (`CodeUri: ../src/` の全関数共通)に含まれ、template の変更が要らないことを固定する。
    """
    import importlib

    from tests.unit.test_issue_606_production_connection_gate import _template_document

    assert importlib.import_module(handler_module.__name__) is handler_module
    assert callable(handler_module.handler)

    resources = _template_document()["Resources"]
    function = next(
        resource
        for resource in resources.values()
        if resource.get("Type") == "AWS::Serverless::Function"
        and resource["Properties"].get("Handler")
        == "jstock_advisor.lambda_handlers.holdings_watchlist_handler.handler"
    )
    code_uri = function["Properties"].get("CodeUri") or "../src/"  # Globals 由来なら既定
    assert code_uri == "../src/"
    artifact_root = _HANDLER_PATH.parents[2].resolve()  # src/
    template_root = (Path(__file__).resolve().parents[2] / "infra" / code_uri).resolve()
    assert artifact_root == template_root  # artifact の根 = src/
    for module in (
        "services/price_regime_shadow_service.py",
        "domain/signals/price_path_facts.py",
        "domain/exit_architecture/price_facts.py",
        "domain/exit_architecture/price_regime.py",
        "domain/exit_architecture/determination.py",
        "domain/exit_architecture/evidence.py",
        "domain/exit_architecture/verdicts.py",
        "domain/exit_architecture/vocabulary.py",
        "domain/exit_architecture/__init__.py",
    ):
        assert (artifact_root / "jstock_advisor" / module).is_file(), module
