"""Expected Return / RAER の Shadow の器(設定型・実行計画・排他制御)の契約テスト(#602 PR-1)。

## この PR の範囲(USER 承認: #122 issuecomment-6100082651。設計: #602 rev1 …6100131042)
  ExpectedReturnRuntimeConfig / ExpectedReturnExecutionPlan の型と、実行計画を作る純粋関数。
  ★ 永続化(表・IAM・repository)・RAER の計算・shadow の配線・設定を ACTIVE にする経路は含めない。

## 固定するもの
  (0) 先行(characterization): 本件の型が依存する既存の契約(RuntimeConfigMode の値・Entity /
      ImmutableSnapshot の性質・require_timezone_aware)は変更しない
  (1) 実行モードの排他制御: mode と 3 つの flag の組を厳密に対応づける。allow_allocation_use が
      True になるのは ACTIVE のときだけ(全 mode × 全 flag の網羅。期待値は literal)
  (2) 設定が無い(未作成)= LEGACY 相当(計算も記録もしない)
  (3) 設定型: 固定の config_id・版 >= 1・tz-aware・空でない更新者 / 理由・未知の field の拒否
  (4) 構造: 時計・available_cash・取得単価・通知の許可を持たない / 依存方向 /
      誰からも到達されない(dormant。import 連鎖の推移閉包)

時間意味論: updated_at は呼び出し側が渡す記録用の値で、時計・営業日を読まない
(TIME_SEMANTICS_IMPACT = NO)。表示例の更新者は架空値(operator-a)。
"""

from __future__ import annotations

import ast
import datetime as dt
import itertools
from pathlib import Path

import pytest
from pydantic import ValidationError

from jstock_advisor.domain.entities.base import Entity, ImmutableSnapshot
from jstock_advisor.domain.entities.enums import RuntimeConfigMode
from jstock_advisor.domain.jst import require_timezone_aware
from jstock_advisor.domain.valuation import expected_return_runtime as runtime
from jstock_advisor.domain.valuation.expected_return_runtime import (
    ExpectedReturnExecutionPlan,
    ExpectedReturnRuntimeConfig,
    resolve_expected_return_execution_plan,
)

_SRC = Path(__file__).resolve().parents[2] / "src"
_PACKAGE = _SRC / "jstock_advisor"
_MODULE_PATH = _PACKAGE / "domain" / "valuation" / "expected_return_runtime.py"
_MODULE_NAME = "jstock_advisor.domain.valuation.expected_return_runtime"
_AT = dt.datetime(2026, 10, 12, 8, 0, tzinfo=dt.UTC)

# --- (0) 先行: 本件の型が依存する既存の契約は変更しない ---


def test_runtime_config_mode_values_are_pinned() -> None:
    """plan の対応表(mode -> flag の組)は、この 3 値を前提にする。増減したら対応表も見直す。"""
    assert [(m.name, m.value) for m in RuntimeConfigMode] == [
        ("LEGACY", "legacy"),
        ("SHADOW", "shadow"),
        ("ACTIVE", "active"),
    ]


def test_entity_forbids_unknown_fields_and_validates_assignment() -> None:
    class _Probe(Entity):
        value: int

    probe = _Probe(value=1)
    with pytest.raises(ValidationError):
        _Probe(value=1, extra_field=2)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        probe.value = "not-an-int"  # type: ignore[assignment]


def test_immutable_snapshot_is_frozen_and_forbids_unknown_fields() -> None:
    class _Probe(ImmutableSnapshot):
        value: int

    probe = _Probe(value=1)
    with pytest.raises(ValidationError):
        probe.value = 2
    with pytest.raises(ValidationError):
        _Probe(value=1, extra_field=2)  # type: ignore[call-arg]


def test_require_timezone_aware_rejects_naive_and_accepts_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        require_timezone_aware(dt.datetime(2026, 10, 12, 8, 0))
    require_timezone_aware(dt.datetime(2026, 10, 12, 8, 0, tzinfo=dt.UTC))


# --- (1) 実行モードの排他制御 ---

# 期待値は実装(_FLAGS_BY_MODE)から導出せず、独立に書き下した literal
# (compute_raer, record_shadow, allow_allocation_use)
_EXPECTED_FLAGS = {
    RuntimeConfigMode.LEGACY: (False, False, False),
    RuntimeConfigMode.SHADOW: (True, True, False),
    RuntimeConfigMode.ACTIVE: (True, True, True),
}


def _plan(
    mode: RuntimeConfigMode, compute: bool, record: bool, allocation: bool
) -> ExpectedReturnExecutionPlan:
    return ExpectedReturnExecutionPlan(
        mode=mode,
        compute_raer=compute,
        record_shadow=record,
        allow_allocation_use=allocation,
    )


_GRID = list(itertools.product(RuntimeConfigMode, itertools.product([False, True], repeat=3)))


def test_the_grid_covers_every_mode_and_every_flag_combination() -> None:
    assert len(_GRID) == 3 * 8  # 取りこぼしのない網羅になっていること


@pytest.mark.parametrize(("mode", "flags"), _GRID)
def test_a_plan_is_constructible_only_for_the_one_flag_combination_of_its_mode(
    mode: RuntimeConfigMode, flags: tuple[bool, bool, bool]
) -> None:
    if flags == _EXPECTED_FLAGS[mode]:
        plan = _plan(mode, *flags)
        assert (plan.compute_raer, plan.record_shadow, plan.allow_allocation_use) == flags
    else:
        with pytest.raises(ValidationError):
            _plan(mode, *flags)


def test_exactly_three_of_the_twenty_four_combinations_are_valid() -> None:
    valid = []
    for mode, flags in _GRID:
        try:
            _plan(mode, *flags)
        except ValidationError:
            continue
        valid.append((mode, flags))
    assert sorted((m.value, f) for m, f in valid) == [
        ("active", (True, True, True)),
        ("legacy", (False, False, False)),
        ("shadow", (True, True, False)),
    ]


def test_allocation_use_is_true_only_for_active() -> None:
    """USER の案 Y の必須条件: SHADOW は資金配分に使わない(構造で強制)。"""
    allowed_modes = set()
    for mode, flags in _GRID:
        try:
            plan = _plan(mode, *flags)
        except ValidationError:
            continue
        if plan.allow_allocation_use:
            allowed_modes.add(mode)
    assert allowed_modes == {RuntimeConfigMode.ACTIVE}


def test_shadow_with_allocation_use_is_rejected_with_the_dedicated_message() -> None:
    with pytest.raises(ValidationError, match="ACTIVE のときだけ"):
        _plan(RuntimeConfigMode.SHADOW, True, True, True)
    with pytest.raises(ValidationError, match="ACTIVE のときだけ"):
        _plan(RuntimeConfigMode.LEGACY, True, True, True)


def test_allocation_use_without_compute_is_rejected_with_the_dedicated_message() -> None:
    with pytest.raises(ValidationError, match="allow_allocation_use は compute_raer"):
        _plan(RuntimeConfigMode.ACTIVE, False, True, True)


def test_recording_without_compute_is_rejected_with_the_dedicated_message() -> None:
    with pytest.raises(ValidationError, match="record_shadow は compute_raer"):
        _plan(RuntimeConfigMode.SHADOW, False, True, False)


def test_a_mode_flag_mismatch_that_breaks_no_implication_is_still_rejected() -> None:
    """含意だけを満たす組(SHADOW で記録しない等)も、mode と食い違えば拒否する。"""
    with pytest.raises(ValidationError, match=r"mode=shadow の flag の組"):
        _plan(RuntimeConfigMode.SHADOW, True, False, False)
    with pytest.raises(ValidationError, match=r"mode=active の flag の組"):
        _plan(RuntimeConfigMode.ACTIVE, True, True, False)
    with pytest.raises(ValidationError, match=r"mode=legacy の flag の組"):
        _plan(RuntimeConfigMode.LEGACY, True, False, False)


def test_a_plan_is_frozen_and_has_exactly_these_fields() -> None:
    plan = _plan(RuntimeConfigMode.SHADOW, True, True, False)
    with pytest.raises(ValidationError):
        plan.allow_allocation_use = True
    assert list(ExpectedReturnExecutionPlan.model_fields) == [
        "mode",
        "compute_raer",
        "record_shadow",
        "allow_allocation_use",
    ]


# --- (2) 設定が無い = LEGACY 相当 ---


def _config(mode: RuntimeConfigMode, **overrides: object) -> ExpectedReturnRuntimeConfig:
    values: dict[str, object] = {
        "config_version": 1,
        "mode": mode,
        "updated_at": _AT,
        "updated_by": "operator-a",
        "change_reason": "初期化",
    }
    values.update(overrides)
    return ExpectedReturnRuntimeConfig(**values)  # type: ignore[arg-type]


def test_no_config_resolves_to_legacy_which_neither_computes_nor_records() -> None:
    plan = resolve_expected_return_execution_plan(None)

    assert plan.mode is RuntimeConfigMode.LEGACY
    assert (plan.compute_raer, plan.record_shadow, plan.allow_allocation_use) == (
        False,
        False,
        False,
    )


@pytest.mark.parametrize("mode", list(RuntimeConfigMode))
def test_every_mode_resolves_to_its_literal_flag_triple(mode: RuntimeConfigMode) -> None:
    plan = resolve_expected_return_execution_plan(_config(mode))

    assert plan.mode is mode
    assert (plan.compute_raer, plan.record_shadow, plan.allow_allocation_use) == _EXPECTED_FLAGS[
        mode
    ]


def test_resolution_is_pure_and_repeatable() -> None:
    config = _config(RuntimeConfigMode.SHADOW)

    assert resolve_expected_return_execution_plan(config) == resolve_expected_return_execution_plan(
        config
    )


def test_shadow_computes_and_records_but_never_feeds_allocation() -> None:
    plan = resolve_expected_return_execution_plan(_config(RuntimeConfigMode.SHADOW))

    assert plan.compute_raer is True
    assert plan.record_shadow is True
    assert plan.allow_allocation_use is False


# --- (3) 設定型 ---


def test_config_id_is_fixed() -> None:
    assert runtime.CONFIG_ID == "expected_return"
    assert _config(RuntimeConfigMode.LEGACY).config_id == "expected_return"
    with pytest.raises(ValidationError):
        _config(RuntimeConfigMode.LEGACY, config_id="holding_decision")


@pytest.mark.parametrize("version", [0, -1])
def test_config_version_must_be_at_least_one(version: int) -> None:
    with pytest.raises(ValidationError):
        _config(RuntimeConfigMode.LEGACY, config_version=version)
    assert _config(RuntimeConfigMode.LEGACY, config_version=1).config_version == 1


def test_config_updated_at_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _config(RuntimeConfigMode.LEGACY, updated_at=dt.datetime(2026, 10, 12, 8, 0))


def test_config_updated_at_stays_timezone_aware_on_assignment() -> None:
    config = _config(RuntimeConfigMode.LEGACY)
    with pytest.raises(ValidationError, match="timezone-aware"):
        config.updated_at = dt.datetime(2026, 10, 12, 9, 0)


@pytest.mark.parametrize("field", ["updated_by", "change_reason"])
def test_config_requires_a_non_empty_actor_and_reason(field: str) -> None:
    with pytest.raises(ValidationError):
        _config(RuntimeConfigMode.SHADOW, **{field: ""})


def test_config_rejects_unknown_fields_including_a_notification_switch() -> None:
    """RAER は通知を作らない。notification_enabled・財務の方針の上書きを持たない。"""
    for unknown in ("notification_enabled", "financial_policy_override", "available_cash"):
        with pytest.raises(ValidationError):
            _config(RuntimeConfigMode.LEGACY, **{unknown: True})


def test_config_mode_must_be_a_runtime_config_mode() -> None:
    with pytest.raises(ValidationError):
        _config("bogus")  # type: ignore[arg-type]
    assert _config("shadow").mode is RuntimeConfigMode.SHADOW  # type: ignore[arg-type]


def test_config_has_exactly_these_fields() -> None:
    assert list(ExpectedReturnRuntimeConfig.model_fields) == [
        "config_id",
        "config_version",
        "mode",
        "updated_at",
        "updated_by",
        "change_reason",
    ]


# --- (4) 構造 ---


def _module_tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def test_module_reads_no_clock_and_does_not_bypass_validation() -> None:
    called = {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for n in ast.walk(_module_tree())
        if isinstance(n, ast.Call)
    }
    assert not called & {"now", "today", "utcnow", "time", "monotonic", "perf_counter", "sleep"}
    # validator を迂回する構築・複製の経路を持たない(plan は resolve_* の 1 経路で作る)
    assert not called & {"model_copy", "model_construct", "construct", "copy"}


def test_module_has_no_forbidden_names_or_fields() -> None:
    names = {n.id for n in ast.walk(_module_tree()) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(_module_tree()) if isinstance(n, ast.Attribute)}
    names |= {
        t.target.id
        for t in ast.walk(_module_tree())
        if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)
    }
    lowered = " ".join(sorted(n.lower() for n in names))
    for forbidden in (
        "available_cash",
        "acquisition",
        "average_price",
        "cost_basis",
        "notification",
    ):
        assert forbidden not in lowered, forbidden


def test_module_depends_only_on_domain_types_and_pydantic() -> None:
    imported: list[str] = []
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
        elif isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
    forbidden = tuple(
        f"jstock_advisor.{n}"
        for n in ("services", "infrastructure", "lambda_handlers", "providers", "cli", "config")
    )
    for module in imported:
        assert not module.startswith(forbidden), module
        assert "exit_architecture" not in module, module
    own = sorted(m for m in imported if m.startswith("jstock_advisor"))
    assert own == [
        "jstock_advisor.domain.entities.base",
        "jstock_advisor.domain.entities.enums",
        "jstock_advisor.domain.jst",
    ]


# --- dormant: import 連鎖の推移閉包(handler・CLI から到達しない) ---


def _module_files(package_root: Path, package: str) -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in package_root.rglob("*.py"):
        relative = path.relative_to(package_root.parent).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    assert package in {m.split(".")[0] for m in modules}
    return modules


def _direct_imports(module: str, path: Path, known: dict[str, Path]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    is_package = path.name == "__init__.py"
    base = module.split(".") if is_package else module.split(".")[:-1]
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
            # 実在する module だけを辺にする。親 package の __init__ も(import されるので)辺にする
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


def test_reachability_helper_follows_chains_and_relative_imports(tmp_path: Path) -> None:
    """helper 自体の検証: 間接の連鎖・相対 import・親 package を辿れる(空振りしない)。"""
    root = tmp_path / "pkg"
    (root / "sub").mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "sub" / "__init__.py").write_text("", encoding="utf-8")
    (root / "entry.py").write_text("from .sub import middle\n", encoding="utf-8")
    (root / "sub" / "middle.py").write_text("from . import leaf\n", encoding="utf-8")
    (root / "sub" / "leaf.py").write_text("X = 1\n", encoding="utf-8")
    (root / "orphan.py").write_text("Y = 2\n", encoding="utf-8")
    known = _module_files(root, "pkg")

    reached = _reachable({"pkg.entry"}, known)

    assert {"pkg.entry", "pkg.sub", "pkg.sub.middle", "pkg.sub.leaf"} <= reached
    assert "pkg.orphan" not in reached


def test_the_new_module_is_not_reachable_from_any_handler_or_cli() -> None:
    known = _module_files(_PACKAGE, "jstock_advisor")
    roots = {
        m
        for m in known
        if m.startswith(("jstock_advisor.lambda_handlers.", "jstock_advisor.cli."))
        or m in {"jstock_advisor.lambda_handlers", "jstock_advisor.cli"}
    }
    assert len(roots) > 20  # handler と CLI を根にできている(空振りでない)

    reached = _reachable(roots, known)

    assert "jstock_advisor.domain.entities.enums" in reached  # 推移閉包が実際に深く辿れている
    assert _MODULE_NAME not in reached


def test_no_source_file_mentions_the_new_module() -> None:
    importers = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        if path == _MODULE_PATH:
            continue
        if "expected_return_runtime" in path.read_text(encoding="utf-8"):
            importers.append(path.relative_to(_PACKAGE).as_posix())
    assert importers == []
