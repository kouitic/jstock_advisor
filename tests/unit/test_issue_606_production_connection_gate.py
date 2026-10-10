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


def test_runtime_config_mode_is_referenced_only_by_the_approved_modules() -> None:
    """mode(legacy / shadow / active)を扱う src は、承認された module だけ。

    新しい参照元は新しい経路である。

    - 保有判断の機構(既存。§10)
    - `domain/valuation/expected_return_runtime.py`(#602 PR-1。USER 承認: #122 …6100082651)。
      型と実行計画のみで、書込・永続化・CLI・handler を持たず、どこからも到達されない
    """
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
        "domain/valuation/expected_return_runtime.py",
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


# --- (1) 有効化の経路の集合は PR の前後で差分 0(全走査) ---
#
# 『#128 の機能を有効にできる経路』= 設定型・実行計画を作る/読む src、環境変数、template、config。
# いずれも**列挙ではなく src・infra・config の全走査**で、許可リストと比べる。
# 新しい参照元が現れたら、それは新しい経路である。本テストが落ちる = 承認の記録を添えて更新する。

_HANDLE_NAMES = (
    "ExpectedReturnRuntimeConfig",
    "ExpectedReturnExecutionPlan",
    "resolve_expected_return_execution_plan",
    "allow_allocation_use",
)
# 上の名前を参照してよい src(定義するモジュール自身だけ。#602 PR-A。未 merge の間は存在しなくてよい)
_HANDLE_ALLOWED_REFERENCERS = {"domain/valuation/expected_return_runtime.py"}
_FEATURE_TOKEN = re.compile(
    r"(allocation|expected_return|raer|capital_rotation|today_actions)", re.IGNORECASE
)


def test_nothing_but_the_defining_module_references_the_expected_return_runtime_handles() -> None:
    """設定型・実行計画を『作る・読む・書く』src は、定義するモジュールの外に 1 つも無い。"""
    referencing = {
        path.relative_to(_PACKAGE).as_posix()
        for path in _PACKAGE.rglob("*.py")
        if any(name in path.read_text(encoding="utf-8") for name in _HANDLE_NAMES)
    }
    assert referencing <= _HANDLE_ALLOWED_REFERENCERS, sorted(referencing)


def _environment_variable_names() -> set[str]:
    """src の `os.environ.get(...)` / `os.getenv(...)` / `os.environ[...]` が読む名前(定数のみ)。"""
    names: set[str] = set()
    for path in _PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                target = ast.unparse(node.func)
                if target in {"os.environ.get", "os.getenv", "os.environ.pop"} and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        names.add(arg.value)
            elif isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
                if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                    names.add(node.slice.value)
    return names


def test_the_environment_variable_scan_is_not_vacuous() -> None:
    names = _environment_variable_names()
    assert "AWS_LAMBDA_FUNCTION_NAME" in names  # 既知の名前を拾えている(走査が空振りでない)


def test_no_environment_variable_can_enable_a_128_feature() -> None:
    offenders = sorted(n for n in _environment_variable_names() if _FEATURE_TOKEN.search(n))
    assert offenders == []


def test_the_template_has_no_parameter_environment_or_resource_for_a_128_feature() -> None:
    """infra/template.yaml を全文走査: #128 の機能の Parameter・環境変数・表・Function が無い。"""
    template = (_REPO / "infra" / "template.yaml").read_text(encoding="utf-8")
    tokens = re.findall(
        r"[A-Za-z0-9_]*(?:[Aa]llocation|[Ee]xpected_?[Rr]eturn|[Rr]aer|RAER)\w*", template
    )
    assert sorted(set(tokens)) == []


def test_the_only_config_file_for_a_128_feature_is_the_off_allocation_shadow() -> None:
    by_name = sorted(p.name for p in _SHIPPED_CONFIG_DIR.iterdir() if _FEATURE_TOKEN.search(p.name))
    assert by_name == ["allocation_shadow.yaml"]
    by_content = sorted(
        p.name
        for p in _SHIPPED_CONFIG_DIR.iterdir()
        if p.suffix in {".yaml", ".json"}
        and p.name != "allocation_shadow.yaml"
        and re.search(r"(expected_return|raer\b|allocation)", p.read_text(encoding="utf-8"))
    )
    assert by_content == []


# --- (2) 運用手順書の新節(§37) ---

_SECTION_HEADING = re.compile(r"^## 37\. ")


def _section_37() -> str:
    lines = _manual().splitlines()
    start = next(i for i, line in enumerate(lines) if _SECTION_HEADING.match(line))
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _subsection(text: str, heading: str) -> str:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("### ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_the_section_exists_and_names_the_gate() -> None:
    heading = next(line for line in _manual().splitlines() if _SECTION_HEADING.match(line))
    for word in ("#128", "Production 接続 gate", "#606"):
        assert word in heading, word


def test_the_section_has_the_required_subsections_in_order() -> None:
    headings = [line for line in _section_37().splitlines() if line.startswith("### 37.")]
    assert [h.split(" ")[1] for h in headings] == [
        "37.1",
        "37.2",
        "37.3",
        "37.4",
        "37.5",
        "37.6",
    ]


def _stage_text(stage: str) -> str:
    block = _subsection(_section_37(), "### 37.2")
    lines = block.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(stage))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("段 ")), len(lines))
    return "\n".join(lines[start:end])


@pytest.mark.parametrize("stage", ["段 A", "段 B", "段 C", "段 D"])
def test_every_stage_states_entry_exit_record_and_stop_condition(stage: str) -> None:
    text = _stage_text(stage)
    for label in ("入口", "出口", "記録", "停止条件"):
        assert label in text, (stage, label)


def test_the_four_stages_are_in_the_gate_order() -> None:
    block = _subsection(_section_37(), "### 37.2")
    positions = [block.index(f"段 {x}") for x in "ABCD"]
    assert positions == sorted(positions)
    titles = re.findall(r"^段 [ABCD] ([^\n(:：]+)", block, flags=re.MULTILINE)
    assert [t.strip() for t in titles][:4] == [
        "Shadow",
        "Backtest / Replay",
        "Human Review",
        "USER Approval",
    ]


def test_user_approval_names_its_target_and_is_separate_from_the_changeset_gates() -> None:
    stage_d = _stage_text("段 D")
    for phrase in ("明示的な承認", "承認の対象", "別々", "ChangeSet", "CREATE", "EXECUTE"):
        assert phrase in stage_d, phrase
    # 新しい制約として読まれないよう、既存の正本(#122 / §36)に基づくことを示す
    assert "新しい制約ではなく" in stage_d
    assert "#122" in stage_d and "§36" in stage_d


def test_the_section_reuses_existing_gates_and_does_not_create_a_new_one() -> None:
    text = _section_37()
    assert "新しい gate 機構" in text and "新しい有効化経路" in text
    for reference in ("§28", "§10", "§5.1", "#122", "#846", "#128"):
        assert reference in text, reference
    # 恒久規則の制定・変更は HUMAN_ONLY。導入部と『決めていないこと』の両方で明示する
    intro = text[: text.index("### 37.1")]
    assert "POLICY_AUTHORITY = HUMAN_ONLY" in intro
    assert "HUMAN_ONLY" in _subsection(text, "### 37.6")


def test_the_section_does_not_fix_thresholds_or_periods() -> None:
    """合格基準・最低期待リターン改善幅・観測期間は『shadow 運用の後に決める』。数値を書かない。"""
    text = _subsection(_section_37(), "### 37.2") + _subsection(_section_37(), "### 37.3")
    assert not re.search(r"\d+\s*(営業日|日間|週間|か月|ヶ月|%|％|ポイント)", text)
    undecided = _subsection(_section_37(), "### 37.6")
    for phrase in ("合格基準", "観測期間", "持ち主", "ACTIVE"):
        assert phrase in undecided, phrase


def test_the_section_states_the_dont_list_and_the_rollback() -> None:
    dont = _subsection(_section_37(), "### 37.3")
    for phrase in ("段を飛ばさ", "投資判断に使", "IAM", "有効化の経路", "merge"):
        assert phrase in dont, phrase
    rollback = _subsection(_section_37(), "### 37.4")
    for phrase in ("OFF", "legacy", "再 deploy"):
        assert phrase in rollback, phrase


def test_the_section_points_at_this_contract_test_and_it_exists() -> None:
    mapping = _subsection(_section_37(), "### 37.5")
    name = "tests/unit/test_issue_606_production_connection_gate.py"
    assert name in mapping
    assert (_REPO / name).exists()


def test_this_test_is_registered_as_a_markdown_reading_test() -> None:
    registry = (_REPO / "tests" / "ci_markdown_subset.txt").read_text(encoding="utf-8")
    assert "tests/unit/test_issue_606_production_connection_gate.py" in registry.splitlines()
