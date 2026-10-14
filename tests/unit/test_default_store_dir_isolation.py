"""既定の store の場所の隔離の契約テスト(Issue #967)。

`tests/conftest.py` の autouse fixture `_isolated_default_store_dir` は、全テストで
`json_store.DEFAULT_STORE_DIR` を一時の場所へ差し替える。#967 でこの fixture が全テストで
`tmp_path` を要求するのをやめた(固定費の削減)。隔離の性質が保たれていることを固定する。

  T1  差し替えが効いている: 既定の場所は本物の `data/local_store` ではなく、pytest の基底
      ディレクトリの配下である(★ 受け取り忘れ・差し替え漏れで本物の場所を触らない ★)
  T2  初期状態は『存在しない』(従来と同じ)
  T3  テスト間で一意: セッション級の記録に重複が無い / 重複を検出する検査自体が働く
  T4  既定の store へ書いたものが既定の場所の配下だけに作られ、リポジトリの `data/` に何も作らない
  T5  ★ `tmp_path` を使わないテストでは `tmp_path` のディレクトリを作らない(固定費の削減の本体)★
      ★ `tmp_path` を使うテストは、従来どおり `tmp_path / "local_store"` が既定のまま ★
      (`store_dir` fixture = `tmp_path / "local_store"` と同じ場所を指す偶然の一致を保つ)
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from jstock_advisor.infrastructure.local_repository import json_store
from jstock_advisor.infrastructure.local_repository.json_store import JsonCollectionStore

_REPO_ROOT = Path(__file__).resolve().parents[2]
#: 本物の既定の場所(差し替え前の定義と同じ式)
_REAL_DEFAULT = Path(json_store.__file__).resolve().parents[4] / "data" / "local_store"
_PYTEST_NAME_LENGTH = 30  # pytest が tmp_path のディレクトリ名に使うテスト名の長さ


def _conftest() -> Any:
    return importlib.import_module("tests.conftest")


class _Item(BaseModel):
    item_id: str


def _tmp_dirs_named_after(base: Path, node_name: str) -> list[Path]:
    """`tmp_path` のディレクトリ名(テスト名の先頭 30 文字 + 番号)に一致するもの。"""
    prefix = re.sub(r"[\W]", "_", node_name)[:_PYTEST_NAME_LENGTH]
    return sorted(base.glob(f"{prefix}[0-9]*"))


# --- T1・T2・T5(tmp_path を使わないテスト)---------------------------------------------------


def test_the_default_store_is_replaced_and_is_not_the_real_location(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    assert json_store.DEFAULT_STORE_DIR != _REAL_DEFAULT
    assert _REAL_DEFAULT not in json_store.DEFAULT_STORE_DIR.parents
    assert tmp_path_factory.getbasetemp() in json_store.DEFAULT_STORE_DIR.parents
    assert _REPO_ROOT not in {json_store.DEFAULT_STORE_DIR, *json_store.DEFAULT_STORE_DIR.parents}


def test_the_default_store_does_not_exist_at_the_start() -> None:
    assert not json_store.DEFAULT_STORE_DIR.exists()


def test_a_test_without_tmp_path_gets_a_path_under_the_shared_default_store_base(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> None:
    store_dir = json_store.DEFAULT_STORE_DIR
    assert store_dir.parent == tmp_path_factory.getbasetemp() / "default_store"
    assert re.fullmatch(r"t[0-9]+", store_dir.name)
    assert "tmp_path" not in request.fixturenames


def test_a_test_without_tmp_path_creates_no_tmp_path_directory(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """★ 固定費の削減の本体 ★: この fixture が tmp_path を要求しなくなったことを確かめる。

    (pytest は `tmp_path` を要求されたときだけ、テスト名の先頭 30 文字 + 番号のディレクトリを作る。)
    """
    assert "tmp_path" not in request.fixturenames
    assert _tmp_dirs_named_after(tmp_path_factory.getbasetemp(), request.node.name) == []


def test_the_default_store_is_unique_per_test_part_one() -> None:
    assert json_store.DEFAULT_STORE_DIR in _conftest()._assigned_store_dirs


def test_the_default_store_is_unique_per_test_part_two() -> None:
    assigned = _conftest()._assigned_store_dirs
    assert json_store.DEFAULT_STORE_DIR in assigned
    assert assigned.count(json_store.DEFAULT_STORE_DIR) == 1


# --- T5(tmp_path を使うテスト)---------------------------------------------------------------


def test_a_test_with_tmp_path_keeps_the_old_default_location(tmp_path: Path) -> None:
    """従来どおり `tmp_path / "local_store"`(`store_dir` fixture と同じ場所)。作られてもいない。"""
    assert tmp_path / "local_store" == json_store.DEFAULT_STORE_DIR
    assert not json_store.DEFAULT_STORE_DIR.exists()


def test_the_store_dir_fixture_points_at_the_default_store_for_tests_that_use_it(
    store_dir: Path,
) -> None:
    """tests/unit/conftest.py の `store_dir` と既定の store が一致する(従来のとおり)。"""
    assert store_dir == json_store.DEFAULT_STORE_DIR


def test_tmp_path_tests_do_not_enter_the_unique_path_record(tmp_path: Path) -> None:
    assert json_store.DEFAULT_STORE_DIR not in _conftest()._assigned_store_dirs


# --- T4 実際の store への書込 -------------------------------------------------------------------


def _data_state() -> tuple[bool, tuple[str, ...]]:
    data = _REPO_ROOT / "data"
    if not data.exists():
        return (False, ())
    return (True, tuple(sorted(str(p.relative_to(data)) for p in data.rglob("*"))))


def test_a_default_store_write_lands_only_under_the_default_location_and_not_in_repo_data() -> None:
    before = _data_state()
    store = JsonCollectionStore(_Item, "isolation_probe.json", "item_id")
    store.upsert(_Item(item_id="a"))
    written = list(json_store.DEFAULT_STORE_DIR.rglob("*.json"))
    assert [p.name for p in written] == ["isolation_probe.json"]
    assert store._path.parent == json_store.DEFAULT_STORE_DIR
    assert _data_state() == before  # リポジトリの data/ に何も作らない・変えない


def test_a_default_store_write_with_tmp_path_also_stays_in_the_tmp_path(tmp_path: Path) -> None:
    before = _data_state()
    JsonCollectionStore(_Item, "isolation_probe.json", "item_id").upsert(_Item(item_id="a"))
    assert (tmp_path / "local_store" / "isolation_probe.json").exists()
    assert _data_state() == before


# --- T3 一意性の検査そのもの ---------------------------------------------------------------------


def test_the_duplicate_detector_finds_a_path_assigned_twice(tmp_path: Path) -> None:
    helper = _conftest().duplicated_store_dirs
    a, b = tmp_path / "a", tmp_path / "b"
    assert helper([a, b]) == []
    assert helper([a, b, a]) == [a]
    assert helper([a, a, a]) == [a, a]
    assert helper([]) == []


def test_the_session_finish_check_fails_the_session_only_on_a_duplicate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _conftest()
    session = SimpleNamespace(exitstatus=0)
    monkeypatch.setattr(module, "_assigned_store_dirs", [tmp_path / "a", tmp_path / "b"])
    module.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 0
    monkeypatch.setattr(module, "_assigned_store_dirs", [tmp_path / "a", tmp_path / "a"])
    module.pytest_sessionfinish(session, 0)
    assert session.exitstatus == int(pytest.ExitCode.TESTS_FAILED)


def test_zz_the_session_so_far_has_no_duplicate_default_store() -> None:
    """同じセッションの、これまでの全ての割り当てに重複が無い(一意性。これが最後のテスト)。"""
    module = _conftest()
    assert module.duplicated_store_dirs(module._assigned_store_dirs) == []
    assert len(module._assigned_store_dirs) >= 2
