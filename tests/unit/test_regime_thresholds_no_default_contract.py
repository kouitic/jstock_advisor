"""閾値の契約: 『閾値が未指定のとき、勝手な既定値で計算しない』を今日から守る(Issue #877 PR-2a)。

USER 決定(#122 …6174293468 の 2 の 6)=『閾値未指定時に、勝手な既定値で計算しないことを契約テストで
保証する』。閾値 3 値は USER が保留している(事前登録 P1・承認の後に確定する)。

  T-1 `RegimeThresholds(...)` を構築する箇所が src の**どこにも無い**(型の定義を除く)。
      全 src を走査する(走査対象を列挙しない = 新しい file を取りこぼさない)。
      承認された設定の loader を足す PR だけが、このテストを意図して更新する
  T-2 `RegimeThresholds` の field にも `classify_price_regime` の `thresholds` 引数にも既定値が無い
  T-6 config(`config/*.yaml`)に regime の閾値のキーが無い(足す PR は事前登録の承認の後。
      config の追加は Layer の変更として release に現れるため、値が決まるまで足さない)

T-3(PR-2b の入口が `thresholds is None` のとき何も計算しない)は PR-2b の契約テストで固定する。
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

from jstock_advisor.domain.exit_architecture.price_regime import (
    RegimeThresholds,
    classify_price_regime,
)

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "jstock_advisor"
_CONFIG_DIR = _ROOT / "config"
_TYPE_NAME = "RegimeThresholds"
_THRESHOLD_KEY_FRAGMENTS = (
    "peak_warning_drawdown",
    "downtrend_confirmed_drawdown",
    "breakdown_drawdown",
)


def _constructions(source: str) -> list[int]:
    """`RegimeThresholds(...)` の呼び出し(`x.RegimeThresholds(...)` と別名も含む)の行番号。"""
    tree = ast.parse(source)
    aliases = {_TYPE_NAME}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            aliases.update(a.asname for a in node.names if a.name == _TYPE_NAME and a.asname)
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Name) and func.id in aliases) or (
            isinstance(func, ast.Attribute) and func.attr == _TYPE_NAME
        ):
            lines.append(node.lineno)
    return lines


# --- T-1 ------------------------------------------------------------------------------------


def test_the_scanner_detects_every_construction_form() -> None:
    forms = (
        "RegimeThresholds(1, 2, 3)",
        "m.RegimeThresholds(peak_warning_drawdown_pct=1.0)",
        "from x import RegimeThresholds as T\nT(1, 2, 3)",
        "def f():\n    return RegimeThresholds(*args)",
    )
    for form in forms:
        assert _constructions(form), form
    assert not _constructions("from x import RegimeThresholds\nvalue: RegimeThresholds | None")


def test_no_src_module_constructs_regime_thresholds() -> None:
    scanned = 0
    constructions: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        scanned += 1
        for line in _constructions(path.read_text(encoding="utf-8")):
            constructions.append(f"{path.relative_to(_SRC).as_posix()}:{line}")
    assert scanned > 100, "走査が空振りしていないこと"
    assert constructions == []


# --- T-2 ------------------------------------------------------------------------------------


def test_regime_thresholds_fields_have_no_default() -> None:
    fields = dataclasses.fields(RegimeThresholds)
    assert [f.name for f in fields] == [
        "peak_warning_drawdown_pct",
        "downtrend_confirmed_drawdown_pct",
        "breakdown_drawdown_pct",
    ]
    for field in fields:
        assert field.default is dataclasses.MISSING, field.name
        assert field.default_factory is dataclasses.MISSING, field.name


def test_regime_thresholds_cannot_be_built_without_values() -> None:
    try:
        RegimeThresholds()  # type: ignore[call-arg]
    except TypeError:
        return
    raise AssertionError("既定値なしで構築できてはならない")


def test_classify_price_regime_takes_thresholds_without_a_default() -> None:
    parameters = inspect.signature(classify_price_regime).parameters
    assert list(parameters) == ["facts", "thresholds", "reliability"]
    for name, parameter in parameters.items():
        assert parameter.default is inspect.Parameter.empty, name


def test_no_function_in_price_regime_gives_thresholds_a_default() -> None:
    """price_regime.py のどの関数も、閾値の型の引数に既定値を持たない(AST)。"""
    tree = ast.parse(
        (_SRC / "domain" / "exit_architecture" / "price_regime.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        positional = [*node.args.posonlyargs, *node.args.args]
        defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
        pairs = list(zip(positional, defaults, strict=True)) + list(
            zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)
        )
        for arg, default in pairs:
            annotation = ast.unparse(arg.annotation) if arg.annotation else ""
            if _TYPE_NAME in annotation:
                assert default is None, f"{node.name}({arg.arg}) に既定値がある"


# --- T-6 ------------------------------------------------------------------------------------


def test_config_has_no_regime_threshold_key() -> None:
    files = sorted(_CONFIG_DIR.rglob("*.yaml")) + sorted(_CONFIG_DIR.rglob("*.yml"))
    assert files, "走査が空振りしていないこと"
    for path in files:
        text = path.read_text(encoding="utf-8")
        for fragment in _THRESHOLD_KEY_FRAGMENTS:
            assert fragment not in text, f"{path.name} に {fragment}"
