"""`domain/backtest/`(replay の純粋関数。Issue #880 R1a。dormant)の package 全体の契約テスト。

設計: #880 rev2・rev2.1・rev2.2(…6206432201)・サブちゃんの設計レビュー(…6206340801)。

  P1  純粋: 標準ライブラリだけを import する(NEW の module・config・services を含まない)
  P2  ★ 値の既定なし ★: 数値の既定値(引数・field)を 1 つも持たない = 登録を忘れたら動かない
  P3  時計・環境変数・ファイル・ネットワークに触れない
  P4  ★ dormant ★: src のどこからも import されない = importer の完全一致の空リスト
      ★ この package は #606 の網の外 ★(名前が token・prefix に当たらない)。保証は本テストだけ
  P5  ★ 型の整合(rev2.2 SHOULD-1)★: R1a の入力の型が、NEW の `Decision` から作れる(テスト側だけが
      NEW を import する。R1a の src は NEW を import しない)
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from jstock_advisor.domain.backtest.replay_classification import (
    Judgement,
    ReplayAction,
    classify_pair,
)
from jstock_advisor.domain.exit_architecture.decision import Decision
from jstock_advisor.domain.exit_architecture.vocabulary import (
    ExitAction,
    ExitClass,
    Strength,
    TriggerKind,
)
from tests.unit.test_arbiter_contract import _decide, _fe1_layers, _inp, _proposal
from tests.unit.test_issue_606_production_connection_gate import (
    _is_unconnected_name,
    _module_files,
)

_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"
_BACKTEST = _PACKAGE_ROOT / "domain" / "backtest"
_FILES = sorted(p for p in _BACKTEST.glob("*.py"))
_MODULES = sorted(p for p in _FILES if p.name != "__init__.py")
_ALLOWED_STDLIB = {
    "__future__",
    "collections",
    "collections.abc",
    "dataclasses",
    "decimal",
    "enum",
    "fractions",
    "hashlib",
    "json",
    "math",
    "typing",
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_the_package_exists_with_the_planned_modules() -> None:
    names = {p.stem for p in _FILES}

    assert names >= {
        "__init__",
        "errors",
        "replay_events",
        "replay_classification",
        "outcome_window",
        "replay_tables",
        "preregistration_check",
        "replay_reproducibility",
    }


# --- P1 純粋 --------------------


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_p1_only_the_standard_library_and_its_own_package_are_imported(path: Path) -> None:
    for name in _imports(path):
        root = name.split(".")[0]
        assert name in _ALLOWED_STDLIB or name.startswith("jstock_advisor.domain.backtest"), (
            f"{path.name}: {name}"
        )
        if root == "jstock_advisor":
            assert not name.startswith(
                ("jstock_advisor.domain.exit_architecture", "jstock_advisor.domain.signals")
            )
    assert all(
        not n.startswith(("jstock_advisor.services", "jstock_advisor.config"))
        for n in _imports(path)
    )


def test_p1_the_check_is_not_vacuous() -> None:
    imports = set().union(*(_imports(p) for p in _MODULES))

    assert "hashlib" in imports and "decimal" in imports and "dataclasses" in imports
    assert any(name.startswith("jstock_advisor.domain.backtest") for name in imports)


# --- P2 値の既定なし --------------------


def _numeric_constant(node: ast.expr | None) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.UnaryOp):
        return _numeric_constant(node.operand)
    if isinstance(node, ast.Constant):
        return isinstance(node.value, int | float) and not isinstance(node.value, bool)
    if isinstance(node, ast.Call):  # Decimal("0.1") / Fraction(1, 2) / int(3) など
        name = getattr(node.func, "id", getattr(node.func, "attr", ""))
        return name in {"Decimal", "Fraction", "float", "int"}
    return False


@pytest.mark.parametrize("path", _MODULES, ids=lambda p: p.name)
def test_p2_no_numeric_default_for_any_parameter_or_field(path: Path) -> None:
    offenders: list[str] = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            defaults = [*node.args.defaults, *node.args.kw_defaults]
            offenders += [f"{node.name}" for d in defaults if _numeric_constant(d)]
        elif isinstance(node, ast.AnnAssign) and _numeric_constant(node.value):
            offenders.append(ast.unparse(node.target))
    assert offenders == []


def test_p2_the_default_check_is_not_vacuous() -> None:
    bad = ast.parse("def f(x=3, *, y=Decimal('1')): pass\nclass C:\n    n: int = 5\n")
    flagged = 0
    for node in ast.walk(bad):
        if isinstance(node, ast.FunctionDef):
            flagged += sum(
                _numeric_constant(d) for d in [*node.args.defaults, *node.args.kw_defaults]
            )
        elif isinstance(node, ast.AnnAssign):
            flagged += int(_numeric_constant(node.value))
    assert flagged == 3


# --- P3 時計・環境・ファイル --------------------


@pytest.mark.parametrize("path", _MODULES, ids=lambda p: p.name)
def test_p3_no_clock_environment_file_or_network(path: Path) -> None:
    tree = _tree(path)
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names = {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    assert not attributes & {"now", "utcnow", "today", "monotonic", "environ", "getenv", "sleep"}
    assert not names & {"open", "print", "input", "exec", "eval", "datetime", "time", "os"}


# --- P4 dormant・#606 の網の外 --------------------


def test_p4_nothing_in_src_imports_the_package_the_importer_list_is_exactly_empty() -> None:
    importers = sorted(
        path.relative_to(_PACKAGE_ROOT).as_posix()
        for path in _PACKAGE_ROOT.rglob("*.py")
        if _BACKTEST not in path.parents
        and "jstock_advisor.domain.backtest" in path.read_text(encoding="utf-8")
    )

    assert importers == []  # R1b の CLI が入るときに、このテストを意図して更新する(#988 の作法)


def test_p4_the_package_is_outside_the_606_name_net() -> None:
    """#606 の token・prefix に当たらない = #606 は何も言わない。dormant の保証は上のテストだけ。"""
    known = _module_files()
    ours = sorted(m for m in known if m.startswith("jstock_advisor.domain.backtest"))

    assert len(ours) >= 8  # 空振りでない: package と全 module が走査の対象に居る
    assert [m for m in ours if _is_unconnected_name(m)] == []


# --- P5 型の整合(rev2.2 SHOULD-1)-----------------------------------------------------


def test_p5_the_new_modules_are_not_imported_by_the_package() -> None:
    for path in _MODULES:
        assert not any("exit_architecture" in name for name in _imports(path)), path.name
    assert (
        "jstock_advisor.domain.exit_architecture.decision" in sys.modules
    )  # テスト側は import している


def test_p5_every_new_action_is_known_to_the_replay_action_vocabulary() -> None:
    """NEW の `ExitAction` に値が増えたら、R1a の分類の語彙が追従していなければここで落ちる。"""
    assert {a.value for a in ExitAction} == {a.value for a in ReplayAction}


def _judgement_of(decision: Decision) -> Judgement:
    """NEW の `Decision` の素の値から、R1a の入力(`Judgement`)を作る(テスト側だけが知る写し)。"""
    return Judgement(
        action=ReplayAction(decision.action.value),
        exit_class=None if decision.exit_class is ExitClass.NONE else decision.exit_class.value,
        # HOLD の根拠が確定(hold_optimal)でなく、評価できなかった層がある = 不明(HOLD にも数えない)
        undetermined=(
            decision.action is ExitAction.HOLD
            and bool(decision.undetermined_layers)
            and not decision.hold_optimal
        ),
    )


def _real_decision(action: ExitAction) -> Decision:
    """実際の Arbiter が出す `Decision`(手で組み立てず、本物の出力から取る)。"""
    if action is ExitAction.HOLD:
        return _decide(_inp([]))
    if action is ExitAction.PARTIAL:
        return _decide(_inp([_proposal(TriggerKind.PRICE_UPSIDE_MATRIX, Strength.PARTIAL)]))
    return _decide(
        _inp(
            [_proposal(TriggerKind.PRICE_UPSIDE_MATRIX, Strength.FULL)],
            layers=_fe1_layers(),
        )
    )


@pytest.mark.parametrize("action", list(ExitAction), ids=lambda a: a.value)
def test_p5_the_replay_input_can_be_built_from_every_kind_of_decision(action: ExitAction) -> None:
    decision = _real_decision(action)
    assert decision.action is action  # 実際に 3 種類の action が出ている(空振りでない)

    judgement = _judgement_of(decision)

    assert judgement.action.value == action.value
    recorded = Judgement(judgement.action, judgement.exit_class)  # OLD は確定した判定
    expected = "UNDETERMINED" if judgement.undetermined else "MATCH"
    assert classify_pair(recorded, judgement).value == expected
    # Arbiter の HOLD は評価できない層が残るため、実際の出力は『不明』になる(空振りでない)
    assert judgement.undetermined is (action is ExitAction.HOLD)


def test_p5_every_exit_class_value_is_accepted_as_a_class_name() -> None:
    for exit_class in ExitClass:
        Judgement(ReplayAction.FULL, exit_class.value)  # 例外にならない(class は素の文字列)
