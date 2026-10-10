"""#128 の各機能の『Production 接続 gate』を、コード上で保証する契約テスト(Issue #606)。

## 位置づけ
  Expected Return(#601)・RAER(#602)・Portfolio Allocation(#603)・Capital Rotation(#604)・
  利用者向けの出力(#605)は、次を経ずに Production の BUY / SELL / 保有判断・通知へ接続しない
  (USER 決定: #122 issuecomment-6100082651 / 6054586019):

      shadow -> replay / backtest -> human review -> USER の明示的な承認

  運用の gate は docs/operations_manual.md の新節(§37)が定める(既存の Human Gate の再利用。
  新しい gate 機構・有効化経路を作らない)。本テストは、その gate を**迂回する経路がコード上に
  無いこと**を、PR のたびに全走査で確認する。許可リストの変更は『接続を許す』変更であり、
  USER の承認の記録(Issue 番号)を要する(`_UNCONNECTED_PREFIXES` の各行に根拠の Issue を書く)。

## 固定するもの
  (0) 先行(characterization): 既存の段階的 Human Gate の記述・AllocationShadowMode・出荷 config・
      RuntimeConfigMode の参照元・dormant module の到達性は、本 PR で変更しない
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from jstock_advisor.domain.signals.allocation_shadow_config import (
    AllocationShadowMode,
    load_allocation_shadow_config,
)

_REPO = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO / "src" / "jstock_advisor"
_OPERATIONS_MANUAL = _REPO / "docs" / "operations_manual.md"
_SHIPPED_CONFIG_DIR = _REPO / "config"


def _manual() -> str:
    return _OPERATIONS_MANUAL.read_text(encoding="utf-8")


# --- import 連鎖の推移閉包(全走査。列挙しない) ---


def _module_files() -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in _PACKAGE.rglob("*.py"):
        relative = path.relative_to(_PACKAGE.parent).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


def _direct_imports(module: str, path: Path, known: dict[str, Path]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    base = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = base[: len(base) - (node.level - 1)]
                prefix = ".".join(anchor + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            targets = [prefix] + [f"{prefix}.{a.name}" for a in node.names]
        else:
            continue
        for target in targets:
            parts = target.split(".")
            for i in range(1, len(parts) + 1):
                candidate = ".".join(parts[:i])
                if candidate in known:
                    found.add(candidate)
    return found


def _reachable(roots: set[str], known: dict[str, Path]) -> set[str]:
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        stack.extend(_direct_imports(module, known[module], known) - seen)
    return seen


def _production_reachable() -> tuple[set[str], dict[str, Path]]:
    """Lambda handler と CLI の全 module を根として、import で到達する module の集合。"""
    known = _module_files()
    roots = {
        m
        for m in known
        if m.startswith(("jstock_advisor.lambda_handlers.", "jstock_advisor.cli."))
        or m in {"jstock_advisor.lambda_handlers", "jstock_advisor.cli"}
    }
    assert len(roots) > 20  # handler と CLI を根にできている(空振りでない)
    return _reachable(roots, known), known


# --- (0) 先行: 既存の仕組みは本 PR で変更しない ---


@pytest.mark.parametrize(
    "heading",
    [
        "### 5.1 GitHub Issue自動起票の設定",
        "### 10.2 Shadow運用手順",
        "### 10.3 Active切替手順",
        "### 10.4 kill switch運用",
        "### 28.2 切替の順序(各段が別の Human Gate。順序を入れ替えない)",
    ],
)
def test_the_existing_staged_human_gates_that_the_new_section_reuses_exist(heading: str) -> None:
    """新節は、これらの既存の Human Gate を再利用する。見出しが消えたら参照先が失われる。"""
    assert any(line.startswith(heading) for line in _manual().splitlines()), heading


def test_allocation_shadow_mode_has_no_active_member() -> None:
    """購入側 Shadow(Q')の mode は OFF / SHADOW のみ。ACTIVE へ進める値そのものが無い。"""
    assert [(m.name, m.value) for m in AllocationShadowMode] == [
        ("OFF", "OFF"),
        ("SHADOW", "SHADOW"),
    ]


def test_the_shipped_allocation_shadow_config_is_off() -> None:
    """出荷 config は OFF。SHADOW にするのは USER の別承認 + IAM の反映の後(本テストを更新する)。"""
    text = (_SHIPPED_CONFIG_DIR / "allocation_shadow.yaml").read_text(encoding="utf-8")
    assert re.search(r'^mode:\s*"OFF"\s*$', text, flags=re.MULTILINE)
    assert load_allocation_shadow_config(_SHIPPED_CONFIG_DIR).enabled is False


def test_runtime_config_mode_is_referenced_only_by_the_holding_decision_modules() -> None:
    """mode(legacy / shadow / active)を扱う src は保有判断の機構だけ。新しい参照元は新しい経路。"""
    referencing = sorted(
        path.relative_to(_PACKAGE).as_posix()
        for path in _PACKAGE.rglob("*.py")
        if "RuntimeConfigMode" in path.read_text(encoding="utf-8")
    )
    assert referencing == [
        "cli/holding_decision.py",
        "domain/entities/enums.py",
        "domain/entities/holding_decision.py",
        "domain/signals/holding_decision_execution_plan.py",
        "infrastructure/local_repository/holding_decision_runtime_config_repository.py",
        "services/holding_decision_runtime_config_service.py",
    ]


# Production の判断へ未接続の module。接続は『shadow -> backtest -> human review -> USER 承認』
# の後だけ。接続を許す変更は、この表からの『除外』であり、USER の承認の記録(Issue 番号)を要する。
_UNCONNECTED_PREFIXES = {
    "jstock_advisor.domain.exit_architecture": "#846 / #884(Exit 統合。USER REVIEW 前)",
    "jstock_advisor.domain.valuation.expected_return": "#601 / #602(Expected Return と器)",
    "jstock_advisor.domain.signals.buy_affordability": "#603 M2(購入可能株数の純粋関数)",
    "jstock_advisor.domain.notification.today_actions_view": "#605-A(本日の投資アクションの表示)",
}
# 名前に含まれる語で、将来の module も捕らえる(RAER・配分の最適化・入替など)。
_UNCONNECTED_NAME_TOKENS = (
    "raer",
    "expected_return",
    "portfolio_allocation",
    "allocation_optimizer",
    "capital_rotation",
    "today_actions",
    "buy_affordability",
    "exit_architecture",
)


def test_unconnected_modules_exist_so_the_check_is_not_vacuous() -> None:
    known = _module_files()
    for prefix in _UNCONNECTED_PREFIXES:
        assert any(m == prefix or m.startswith(prefix + ".") for m in known), prefix


def test_no_handler_or_cli_reaches_an_unconnected_module() -> None:
    reached, known = _production_reachable()
    assert "jstock_advisor.domain.entities.enums" in reached  # 推移閉包が実際に深く辿れている
    offenders = sorted(
        m
        for m in reached
        if any(m == p or m.startswith(p + ".") for p in _UNCONNECTED_PREFIXES)
        or any(token in m for token in _UNCONNECTED_NAME_TOKENS)
    )
    assert offenders == []


def test_the_only_allocation_modules_reachable_from_production_are_the_off_shadow_frame() -> None:
    """配分に関係する module で Production から到達できるのは、出荷 OFF の Shadow の枠(Q')だけ。"""
    reached, _ = _production_reachable()
    assert sorted(m for m in reached if "allocation" in m) == [
        "jstock_advisor.domain.signals.allocation_shadow_config",
        "jstock_advisor.services.allocation_shadow_service",
    ]
