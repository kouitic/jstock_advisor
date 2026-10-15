"""`tests/support/cohort_marker_dump.py`(cohort marker の dump plugin)の契約テスト(Issue #966)。

この plugin は `tests/unit/test_issue_277_cohort_markers.py` が子プロセスの pytest へ
明示して渡すもので、親の実行には読み込まれない。ここで固定するのは次の 3 点。

  1  出力の形: repository root からの相対 path -> 昇順の cohort marker 名(cohort_ で始まるものだけ)
  2  `--cohort-dump` が無いときは何も書かない / 有るときは、その path へだけ書く
  3  副作用が無い: 時計・環境変数・乱数・標準出力を使わず、import は標準 library と pytest だけ

★ 実際の `-m` の後の item を見ること(`pytest_collection_finish` で書く)・module ごとの集合の等号は、
  `test_issue_277_cohort_markers.py`(子プロセスを使う実測)が固定する。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.support import cohort_marker_dump as plugin

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_PATH = _REPO_ROOT / "tests" / "support" / "cohort_marker_dump.py"


def _item(relative: str, *marker_names: str) -> Any:
    markers = [SimpleNamespace(name=name) for name in marker_names]
    return SimpleNamespace(path=_REPO_ROOT / relative, iter_markers=lambda: iter(markers))


def test_the_output_maps_each_module_to_its_sorted_cohort_markers_only() -> None:
    items = [
        _item("tests/unit/test_a.py", "cohort_b", "parametrize", "cohort_a", "xcohort_z", "cohort"),
        _item("tests/unit/test_a.py", "cohort_a"),  # 同じ module の別 item は集合へ合流する
        _item(
            "tests/unit/test_b.py", "slow", "usefixtures"
        ),  # cohort の marker が無い module も現れる
        _item("tests/unit/test_c.py", "cohort_solo_x"),
    ]
    assert plugin.collect_cohort_markers(items) == {
        "tests/unit/test_a.py": ["cohort_a", "cohort_b"],
        "tests/unit/test_b.py": [],
        "tests/unit/test_c.py": ["cohort_solo_x"],
    }


def test_no_item_gives_an_empty_mapping_not_an_error() -> None:
    assert plugin.collect_cohort_markers([]) == {}


def test_items_outside_the_repository_are_ignored(tmp_path: Path) -> None:
    outside = SimpleNamespace(path=tmp_path / "x.py", iter_markers=lambda: iter([]))
    assert plugin.collect_cohort_markers([outside]) == {}


def _session(target: str | None, items: list[Any]) -> Any:
    config = SimpleNamespace(getoption=lambda name: target if name == "--cohort-dump" else None)
    return SimpleNamespace(config=config, items=items)


def test_it_writes_only_to_the_given_path_and_only_when_asked(tmp_path: Path) -> None:
    out = tmp_path / "dump.json"
    plugin.pytest_collection_finish(_session(None, [_item("tests/unit/test_a.py", "cohort_a")]))
    assert not out.exists()  # option が無いときは何も書かない
    assert list(tmp_path.iterdir()) == []
    plugin.pytest_collection_finish(_session(str(out), [_item("tests/unit/test_a.py", "cohort_a")]))
    assert json.loads(out.read_text(encoding="utf-8")) == {"tests/unit/test_a.py": ["cohort_a"]}
    assert [p.name for p in tmp_path.iterdir()] == ["dump.json"]


def test_a_collection_with_no_items_writes_an_empty_object_so_the_caller_can_fail_on_equality(
    tmp_path: Path,
) -> None:
    """`-m` が何も選ばなかった(自動付与が deselect より後に走った等)ときも、出力は『空』であり、
    呼び出し側の等号が落ちる(出力が無いのではなく、空の集合として届く)。"""
    out = tmp_path / "dump.json"
    plugin.pytest_collection_finish(_session(str(out), []))
    assert json.loads(out.read_text(encoding="utf-8")) == {}


def test_the_option_is_registered_with_no_default() -> None:
    added: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    parser = SimpleNamespace(addoption=lambda *names, **kwargs: added.append((names, kwargs)))
    plugin.pytest_addoption(parser)  # type: ignore[arg-type]
    assert len(added) == 1
    names, kwargs = added[0]
    assert names == ("--cohort-dump",)
    assert kwargs["default"] is None


# --- 副作用が無い(AST)----------------------------------------------------------------------


def _tree() -> ast.Module:
    return ast.parse(_PLUGIN_PATH.read_text(encoding="utf-8"))


def test_the_plugin_imports_only_the_standard_library_and_pytest() -> None:
    roots: set[str] = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= {"__future__", "json", "pathlib", "pytest"}, roots


def test_the_plugin_has_no_clock_environment_random_or_stdout() -> None:
    banned_attrs = {"now", "today", "utcnow", "getenv", "environ", "random", "time", "urandom"}
    banned_names = {"print", "input", "exec", "eval", "open"}
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Attribute):
            assert node.attr not in banned_attrs, node.attr
        if isinstance(node, ast.Name):
            assert node.id not in banned_names, node.id


@pytest.mark.parametrize("bad", [None, ""])
def test_a_missing_or_empty_target_writes_nothing(bad: str | None, tmp_path: Path) -> None:
    plugin.pytest_collection_finish(_session(bad, [_item("tests/unit/test_a.py", "cohort_a")]))
    assert list(tmp_path.iterdir()) == []
