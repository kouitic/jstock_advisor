"""Issue #274: 「名前で生成している箇所が src に無い」enum member の reachability guard。

## 何を固定するのか

```
registry(tests/support/enum_reachability_registry.py)に載せた member は、src に名前による
生成箇所が無い。
  V-1  registry の member が enum に実在すること(消えたら FAIL = 削除は別判断)
  V-2  registry に載せている class に、生成箇所を持たない新しい member が増えたら FAIL
       (登録を強制する)
  V-3  registry の member に生成箇所が現れたら FAIL(= 生成器が追加された。宣言が古い)
  V-5  registry の policy(DYNAMIC_CLASS か)が、同じ規則で再計算した結果と一致すること
```

規則は `tests/support/enum_reachability.py` の1か所だけで、registry の再計算も同じ規則を使う。
guard の規則そのもの(分類・同名 class の解決・動的な構築の検出)は、合成したソースで固定する。

## ★ このテストが言っていること / 言っていないこと

```
言っている   「src に、その member を名前で(`Enum.MEMBER` として値を作る・渡す形で)生成している
             箇所が無い」こと。生成器が追加されたら落ちる
言っていない ・「到達不能」(DYNAMIC_CLASS の member は、永続データや入力文字列から `Enum(<値>)` で
               構築されて到達しうる)
             ・「削除してよい」(永続データがあるため、削除は実データの read-only 監査の後に別途判断)
             ・「全 enum の網羅」: registry に載せていない class に、生成箇所を持たない member が
               新しく現れても落ちない(V-2 は registry に載せている class にだけ働く)
視界外       pydantic の field 検証による構築 / getattr / 文字列からの構築 / fixture・parametrize
             経由の参照 / `cls.` `self.` 経由の参照は数えない
```
"""

from __future__ import annotations

import ast
from collections import Counter, defaultdict
from functools import cache
from pathlib import Path

import pytest

from tests.support import enum_reachability as er
from tests.support.enum_reachability_registry import (
    DYNAMIC_CLASS,
    LEGACY_ONLY,
    REGISTRY,
    UNGENERATED_BY_NAME,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "jstock_advisor"
_TESTS = _REPO_ROOT / "tests"
_VALID_POLICIES = {UNGENERATED_BY_NAME, DYNAMIC_CLASS}

_GUIDE = (
    "enum member の削除・rename は、永続データがあるため実データの read-only 監査の後に別途"
    "判断する(Issue #274)。registry を更新する前に、本当に意図した変更かを確認すること。"
)


@cache
def _state() -> dict[str, object]:
    enums = er.collect_enums(_SRC)
    duplicate_names = er.duplicate_class_names(enums)
    src = er.scan(_SRC, enums, module_prefix="jstock_advisor.", same_file_resolution=True)
    ids = {
        er.member_id(enum, member, duplicate_names=duplicate_names): (enum, member)
        for enum in enums
        for member in enum.members
    }
    return {
        "enums": enums,
        "duplicate_names": duplicate_names,
        "src": src,
        "ids": ids,
        "dynamic": set(src.dynamic),
    }


def _generated(enum: er.EnumDef, member: str) -> bool:
    src: er.ScanResult = _state()["src"]  # type: ignore[assignment]
    return src.refs.get((enum, member), Counter())[er.GEN] > 0


# =============================================================================
# guard 本体(registry と、同じ規則で再計算した結果の突き合わせ)
# =============================================================================


def test_v1_every_registered_member_exists_in_its_enum() -> None:
    ids: dict[str, tuple[er.EnumDef, str]] = _state()["ids"]  # type: ignore[assignment]

    missing = sorted(set(REGISTRY) - set(ids))

    assert not missing, (
        f"registry の member が enum に無い(削除・rename された): {missing}。{_GUIDE}"
    )


def test_v3_no_registered_member_has_a_name_based_generation_site() -> None:
    """★ 生成器が追加されたら落ちる(受入条件 1)。"""
    ids: dict[str, tuple[er.EnumDef, str]] = _state()["ids"]  # type: ignore[assignment]

    now_generated = sorted(
        member_id for member_id in REGISTRY if member_id in ids and _generated(*ids[member_id])
    )

    assert not now_generated, (
        "registry の member に、名前による生成箇所が現れた(= 宣言が古い)。"
        f"registry から外すこと: {now_generated}"
    )


def test_v2_no_unregistered_ungenerated_member_in_a_registered_class() -> None:
    """registry に載せている class に、生成箇所の無い新しい member が増えたら、登録を強制する。"""
    enums: list[er.EnumDef] = _state()["enums"]  # type: ignore[assignment]
    duplicate_names: frozenset[str] = _state()["duplicate_names"]  # type: ignore[assignment]
    registered_classes = {
        (enum.path, enum.name)
        for enum in enums
        for member in enum.members
        if er.member_id(enum, member, duplicate_names=duplicate_names) in REGISTRY
    }

    unregistered = sorted(
        er.member_id(enum, member, duplicate_names=duplicate_names)
        for enum in enums
        if (enum.path, enum.name) in registered_classes
        for member in enum.members
        if not _generated(enum, member)
        and er.member_id(enum, member, duplicate_names=duplicate_names) not in REGISTRY
    )

    assert not unregistered, (
        "registry に載せている class に、生成箇所の無い member がある。"
        f"registry へ登録するか、生成箇所を足すこと: {unregistered}"
    )


def test_v5_the_registered_policy_matches_the_recomputed_dynamic_class_detection() -> None:
    """DYNAMIC_CLASS(= 到達不能と読んではならない class)の宣言が、再計算の結果と一致すること。"""
    ids: dict[str, tuple[er.EnumDef, str]] = _state()["ids"]  # type: ignore[assignment]
    dynamic: set[er.EnumDef] = _state()["dynamic"]  # type: ignore[assignment]

    mismatched = sorted(
        member_id
        for member_id, policy in REGISTRY.items()
        if member_id in ids
        and policy != (DYNAMIC_CLASS if ids[member_id][0] in dynamic else UNGENERATED_BY_NAME)
    )

    assert not mismatched, (
        f"policy が再計算の結果と食い違う(動的な構築の有無が変わった): {mismatched}"
    )


def test_every_policy_is_one_of_the_known_values() -> None:
    assert set(REGISTRY.values()) <= _VALID_POLICIES


def test_the_scan_resolves_every_reference_to_a_single_enum() -> None:
    """★ 同名の複数 class への参照が曖昧なまま残っていないこと(残ると全候補へ帰属して数が歪む)。"""
    src: er.ScanResult = _state()["src"]  # type: ignore[assignment]

    assert src.ambiguous_refs == 0, (
        f"同名の enum class への参照が {src.ambiguous_refs} 件、解決できなかった。"
        "同じファイルに定義するか from-import で参照すること"
    )


# =============================================================================
# 維持するもの(削除しない・取り違えない)
# =============================================================================


def test_legacy_only_members_remain_in_their_enums_and_in_the_registry() -> None:
    """★ USER 決定で削除禁止の member は、enum に残っている(受入条件 3)。"""
    ids: dict[str, tuple[er.EnumDef, str]] = _state()["ids"]  # type: ignore[assignment]

    for member_id, reason in LEGACY_ONLY.items():
        assert member_id in ids, f"{member_id}: 削除された(LEGACY_ONLY・削除禁止)。{_GUIDE}"
        assert "削除しない" in reason
        # 「生成なし」と宣言されていても、削除候補としては扱わない(registry は削除を勧めない)
        assert member_id in REGISTRY


def test_a_member_with_a_generation_site_is_never_registered() -> None:
    """同名の別 enum・状態が変わった member を取り違えて載せていないこと(実例の固定)。

    `NotificationType.MANUAL_REVIEW_REQUIRED` は RecommendationType の同名 member とは別物で、
    本番で生成される。`RecommendationType.PARTIAL_RISK_REDUCTION` も現在は生成箇所がある。
    """
    ids: dict[str, tuple[er.EnumDef, str]] = _state()["ids"]  # type: ignore[assignment]

    for member_id in (
        "NotificationType.MANUAL_REVIEW_REQUIRED",
        "RecommendationType.PARTIAL_RISK_REDUCTION",
    ):
        assert member_id in ids
        assert _generated(*ids[member_id]), f"{member_id}: 生成箇所が無くなった(要確認)"
        assert member_id not in REGISTRY


def test_the_registry_makes_no_deletion_claim() -> None:
    """policy の語彙に「削除可」「到達不能」を意味するものが無い(誤読の防止)。"""
    assert {"UNGENERATED_BY_NAME", "DYNAMIC_CLASS"} == _VALID_POLICIES
    forbidden_words = ("DELETE", "REMOVABLE", "UNREACHABLE", "NOT_REACHABLE", "DEAD")
    assert not [p for p in _VALID_POLICIES if any(w in p for w in forbidden_words)]


# =============================================================================
# guard の規則そのもの(合成したソースで固定)
# =============================================================================

_ENUM_SRC = """
import enum

class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"
    _PRIVATE = "p"

class Level(Enum):
    LOW: int = 1
    HIGH: int = 2

class NotAnEnum:
    X = 1
"""


def _scan_snippet(
    code: str, *, extra_defs: str = _ENUM_SRC
) -> tuple[er.ScanResult, dict[str, er.EnumDef]]:
    defs_tree = ast.parse(extra_defs)
    enums = er.collect_enums_from_tree(defs_tree, "defs.py")
    by_name: dict[str, list[er.EnumDef]] = defaultdict(list)
    for enum in enums:
        by_name[enum.name].append(enum)
    tree = ast.parse(code)
    er._annotate_parents(tree)
    result = er.ScanResult()
    er.scan_tree(tree, "use.py", by_name, {}, result, same_file_resolution=False)
    return result, {e.name: e for e in enums}


def _kinds(code: str, member: str = "RED", cls: str = "Color") -> Counter[str]:
    result, defs = _scan_snippet(code)
    return result.refs.get((defs[cls], member), Counter())


def test_collect_enums_reads_members_and_ignores_private_names_and_non_enums() -> None:
    enums = er.collect_enums_from_tree(ast.parse(_ENUM_SRC), "defs.py")

    assert {e.name: e.members for e in enums} == {
        "Color": ("RED", "BLUE"),
        "Level": ("LOW", "HIGH"),
    }


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("x = Color.RED", er.GEN),
        ("def f():\n    return Color.RED", er.GEN),
        ("f(Color.RED)", er.GEN),
        ("f(a=Color.RED)", er.GEN),
        ("d = {'k': Color.RED}", er.GEN),
        ("def f(a=Color.RED):\n    pass", er.GEN),
        ("x = Color.RED if c else Color.BLUE", er.GEN),
        ("x = [Color.RED]", er.GEN),
        ("if v == Color.RED:\n    pass", er.CONSUME),
        ("if v != Color.RED:\n    pass", er.CONSUME),
        ("if v in (Color.RED, Color.BLUE):\n    pass", er.CONSUME),
        ("d = {Color.RED: 1}", er.CONSUME),
        ("y = m[Color.RED]", er.CONSUME),
        ("match v:\n    case Color.RED:\n        pass", er.CONSUME),
        ("ALLOWED = frozenset({Color.RED})", er.CONSUME),
        ("_ALLOWED = (Color.RED, Color.BLUE)", er.CONSUME),
        ("x = Color.RED.value", er.VALUE),
        ("x = Color.RED.name", er.VALUE),
    ],
    ids=[
        "assign",
        "return",
        "call_arg",
        "kwarg",
        "dict_value",
        "default_arg",
        "ifexp",
        "list_element",
        "compare_eq",
        "compare_ne",
        "compare_in_tuple",
        "dict_key",
        "subscript",
        "match_case",
        "constant_set",
        "private_constant_tuple",
        "value_attribute",
        "name_attribute",
    ],
)
def test_reference_classification(code: str, expected: str) -> None:
    # どのケースも Color.RED への参照は1件。期待した種別だけが1件数えられること
    assert dict(_kinds(code)) == {expected: 1}


def test_a_lowercase_assignment_of_a_collection_is_a_generation_not_a_constant() -> None:
    """ALL_CAPS ではない変数への代入は「判定用の集合定数」ではない(生成側へ倒す)。"""
    assert _kinds("allowed = (Color.RED, Color.BLUE)")[er.GEN] == 1


def test_a_non_member_attribute_is_not_counted() -> None:
    result, _ = _scan_snippet("x = Color.NOT_A_MEMBER\ny = Other.RED")

    assert dict(result.refs) == {}


def test_construction_and_iteration_make_a_class_dynamic() -> None:
    cases = {
        "Color('red')": "construct",
        "for c in Color:\n    pass": "iterate",
        "x = [c for c in Color]": "iterate",
        "x = list(Color)": "iterate",
        "x = sorted(Color)": "iterate",
        "x = enum_module.Color('red')": "construct",
    }
    for code, kind in cases.items():
        result, defs = _scan_snippet(code)
        assert result.dynamic[defs["Color"]][kind] == 1, code
        assert defs["Level"] not in result.dynamic, code


def test_naming_a_member_is_not_a_dynamic_construction() -> None:
    result, _ = _scan_snippet("x = Color.RED\nif v == Color.BLUE:\n    pass")

    assert dict(result.dynamic) == {}


_TWO_SAME_NAMED = {
    "a.py": "import enum\nclass Status(enum.Enum):\n    OK = 1\n    GOOD = 2\n",
    "b.py": "import enum\nclass Status(enum.Enum):\n    OK = 1\n    FINE = 2\n",
}


def _scan_two(
    use_code: str, rel_path: str, imports: dict[str, str]
) -> tuple[er.ScanResult, dict[str, er.EnumDef]]:
    enums: list[er.EnumDef] = []
    for path, code in _TWO_SAME_NAMED.items():
        enums.extend(er.collect_enums_from_tree(ast.parse(code), path))
    by_name: dict[str, list[er.EnumDef]] = defaultdict(list)
    for enum in enums:
        by_name[enum.name].append(enum)
    tree = ast.parse(use_code)
    er._annotate_parents(tree)
    result = er.ScanResult()
    er.scan_tree(tree, rel_path, by_name, imports, result, same_file_resolution=True)
    return result, {e.path: e for e in enums}


def test_a_same_named_class_is_resolved_by_the_file_that_defines_it() -> None:
    """同名の enum class を、定義のあるファイル内の参照へ帰属させる(取り違えない)。"""
    result, defs = _scan_two("x = Status.OK", "a.py", {})

    assert result.refs[(defs["a.py"], "OK")][er.GEN] == 1
    assert (defs["b.py"], "OK") not in result.refs
    assert result.ambiguous_refs == 0


def test_a_same_named_class_is_resolved_by_the_import_in_another_file() -> None:
    result, defs = _scan_two("x = Status.OK", "c.py", {"Status": "jstock_advisor.b"})

    assert result.refs[(defs["b.py"], "OK")][er.GEN] == 1
    assert (defs["a.py"], "OK") not in result.refs
    assert result.ambiguous_refs == 0


def test_an_unresolvable_reference_is_counted_and_attributed_to_every_candidate() -> None:
    """曖昧な参照は黙って捨てず、全候補へ帰属させ(生成と数える側へ倒す)、件数を報告する。"""
    result, defs = _scan_two("x = Status.OK", "c.py", {})

    assert result.ambiguous_refs == 1
    assert result.refs[(defs["a.py"], "OK")][er.GEN] == 1
    assert result.refs[(defs["b.py"], "OK")][er.GEN] == 1


def test_a_member_only_one_of_the_same_named_classes_has_is_not_ambiguous() -> None:
    """片方にしか無い member への参照は、候補が1つに決まる(曖昧にしない)。"""
    result, defs = _scan_two("x = Status.GOOD", "c.py", {})

    assert result.ambiguous_refs == 0
    assert result.refs[(defs["a.py"], "GOOD")][er.GEN] == 1


def test_member_ids_are_qualified_only_for_duplicated_class_names() -> None:
    enums: list[er.EnumDef] = []
    for path, code in _TWO_SAME_NAMED.items():
        enums.extend(er.collect_enums_from_tree(ast.parse(code), path))
    enums.extend(er.collect_enums_from_tree(ast.parse(_ENUM_SRC), "defs.py"))
    duplicates = er.duplicate_class_names(enums)

    assert duplicates == frozenset({"Status"})
    ids = {er.member_id(e, m, duplicate_names=duplicates) for e in enums for m in e.members}
    assert "Color.RED" in ids
    assert "Status[a.py].OK" in ids and "Status[b.py].OK" in ids
    assert "Status.OK" not in ids


def test_the_real_source_has_enum_classes_with_the_same_name() -> None:
    """★ 同名の class が実在する(ShadowMode / CsvRowStatus)ため、class 名だけで数えると歪む。

    この実測が設計時の数(156 class / 726 member)を訂正した理由である。
    """
    duplicates: frozenset[str] = _state()["duplicate_names"]  # type: ignore[assignment]

    assert {"ShadowMode", "CsvRowStatus"} <= set(duplicates)
