"""Issue #605 最小 CLI: 所有者の符号に付けるローカルのラベルの契約テスト。

固定するもの:
  - 符号の桁の先頭(6〜24 桁の小文字の 16 進)→ 表示名。`kv1.N:…` 付きの符号でも指定できる
  - 前方一致する組(曖昧になる組)は作れない・同じ key への上書きはできる
  - 表示名の検証(長さ・制御文字)・リポジトリ配下のパスは拒否
  - ★ 例外は固定の文言で、表示名・符号・path を含めない ★
    書込は原子的(失敗しても一時ファイルを残さない)
  - ★ AWS・環境変数・通信に触れない(AST)★
"""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

import pytest

from jstock_advisor.cli import allocation_labels as labels
from jstock_advisor.cli.allocation_labels import (
    LabelError,
    default_labels_path,
    load_labels,
    normalize_prefix,
    remove_label,
    set_label,
)

_REPO = Path(__file__).resolve().parents[2]
_MODULE_PATH = _REPO / "src" / "jstock_advisor" / "cli" / "allocation_labels.py"
_SENTINEL_NAME = "sentinel-表示名-zz"
_PREFIX = "abcdef0123"


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".jstock" / "labels.json"


def test_a_label_is_saved_and_read_back(path: Path) -> None:
    set_label(_PREFIX, "本人A", path)

    assert load_labels(path) == {_PREFIX: "本人A"}
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body == {"version": 1, "labels": {_PREFIX: "本人A"}}


def test_a_missing_file_is_an_empty_set_not_an_error(path: Path) -> None:
    assert load_labels(path) == {}


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("abcdef", "abcdef"),
        ("ABCDEF", "abcdef"),
        ("  abcdef012345  ", "abcdef012345"),
        ("kv1.1:abcdef0123456789abcdef01", "abcdef0123456789abcdef01"),
        ("kv1.12:" + "0" * 24, "0" * 24),
    ],
)
def test_the_prefix_accepts_a_digest_start_or_a_whole_reference(given: str, expected: str) -> None:
    assert normalize_prefix(given) == expected


@pytest.mark.parametrize(
    "given",
    ["", "abcde", "abcdeg", "0" * 25, "kv1.1:abcde", "xyz", "abc def", "６６６６６６", "kv1.1:"],
)
def test_a_prefix_that_is_too_short_or_not_hex_is_refused(given: str) -> None:
    with pytest.raises(LabelError):
        normalize_prefix(given)


def test_a_label_can_be_overwritten_with_the_same_key(path: Path) -> None:
    set_label(_PREFIX, "旧", path)
    set_label(_PREFIX, "新", path)

    assert load_labels(path) == {_PREFIX: "新"}


@pytest.mark.parametrize(("first", "second"), [("abcdef", "abcdef01"), ("abcdef01", "abcdef")])
def test_overlapping_prefixes_are_refused_in_both_directions(
    path: Path, first: str, second: str
) -> None:
    set_label(first, "一", path)

    with pytest.raises(LabelError):
        set_label(second, "二", path)

    assert load_labels(path) == {first: "一"}  # 変わらない


def test_non_overlapping_prefixes_coexist(path: Path) -> None:
    set_label("abcdef", "一", path)
    set_label("abcdee", "二", path)

    assert load_labels(path) == {"abcdef": "一", "abcdee": "二"}


@pytest.mark.parametrize("name", ["", "   ", "x" * 41, "a\nb", "a\tb", "a\x00b", "a\x1bb"])
def test_a_bad_name_is_refused(path: Path, name: str) -> None:
    with pytest.raises(LabelError):
        set_label(_PREFIX, name, path)

    assert not path.exists()


def test_a_name_of_forty_characters_is_accepted_and_trimmed(path: Path) -> None:
    set_label(_PREFIX, "  " + "あ" * 40 + "  ", path)

    assert load_labels(path) == {_PREFIX: "あ" * 40}


def test_remove_returns_whether_a_label_was_removed(path: Path) -> None:
    set_label(_PREFIX, "本人A", path)

    assert remove_label(_PREFIX, path) is True
    assert remove_label(_PREFIX, path) is False
    assert load_labels(path) == {}


def test_a_path_inside_the_repository_is_refused_for_every_operation() -> None:
    inside = _REPO / "src" / "labels.json"

    for call in (
        lambda: set_label(_PREFIX, "x", inside),
        lambda: load_labels(inside),
        lambda: remove_label(_PREFIX, inside),
    ):
        with pytest.raises(LabelError):
            call()
    assert not inside.exists()


def test_the_repository_root_itself_is_refused() -> None:
    with pytest.raises(LabelError):
        load_labels(_REPO)


def test_the_default_path_is_under_the_home_directory_and_outside_the_repository() -> None:
    default = default_labels_path().resolve()

    assert default.name == "allocation_owner_labels.json"
    assert Path.home().resolve() in default.parents
    assert _REPO.resolve() not in default.parents


# --- 不正なファイル --------------------


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        "null",
        json.dumps({"version": 2, "labels": {}}),
        json.dumps({"labels": {}}),
        json.dumps({"version": 1}),
        json.dumps({"version": 1, "labels": []}),
    ],
)
def test_an_unusable_file_is_an_error_with_a_fixed_message(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")

    with pytest.raises(LabelError) as caught:
        load_labels(path)

    assert text not in str(caught.value)


def test_invalid_entries_are_ignored_not_trusted(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "labels": {
                    _PREFIX: "ok",
                    "XYZ": "bad key",
                    "abcde": "short",
                    "aaaaaa": 5,
                    "bbbbbb": "",
                },
            }
        ),
        encoding="utf-8",
    )

    assert load_labels(path) == {_PREFIX: "ok"}
    # 捨てた行は黙って消さず、件数を返す(CLI が stderr へ出す)
    assert labels.load_labels_counting_dropped(path) == ({_PREFIX: "ok"}, 4)


def test_a_broken_file_is_not_overwritten_by_a_set(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")

    with pytest.raises(LabelError):
        set_label(_PREFIX, "x", path)

    assert path.read_text(encoding="utf-8") == "not json"


# --- 例外は固定の文言・書込は原子的 --------------------


def test_no_error_message_contains_the_name_the_prefix_or_the_path(path: Path) -> None:
    set_label("abcdef", _SENTINEL_NAME, path)
    messages: list[str] = []
    for prefix, name in (
        ("abcdef01", _SENTINEL_NAME),  # 重なり
        ("zzzzzz", _SENTINEL_NAME),  # 16 進でない
        ("abcdee", "x" * 41),  # 長すぎる
    ):
        with pytest.raises(LabelError) as caught:
            set_label(prefix, name, path)
        messages.append(str(caught.value))

    for message in messages:
        assert _SENTINEL_NAME not in message
        assert "abcdef" not in message
        assert str(path) not in message
        assert "zzzzzz" not in message


def test_a_failed_write_leaves_the_old_file_and_no_temporary_file(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_label("abcdef", "旧", path)
    before = path.read_text(encoding="utf-8")

    def broken(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full sentinel")

    monkeypatch.setattr(os, "replace", broken)

    with pytest.raises(LabelError) as caught:
        set_label("abcdee", "新", path)

    assert "sentinel" not in str(caught.value)
    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in path.parent.iterdir()] == [path.name]


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX の権限(Windows は手順書で home 配下を前提)"
)
def test_the_file_and_directory_are_private_to_the_user(path: Path) -> None:
    set_label(_PREFIX, "本人A", path)

    assert (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700


def test_the_written_file_is_sorted_and_deterministic(path: Path) -> None:
    set_label("bbbbbb", "二", path)
    set_label("aaaaaa", "一", path)

    text = path.read_text(encoding="utf-8")
    assert text.index("aaaaaa") < text.index("bbbbbb")


# --- AWS・環境・通信に触れない --------------------


def _imports() -> set[str]:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
    return names


def test_the_module_imports_nothing_that_can_reach_aws_or_the_network() -> None:
    imported = _imports()
    assert not [
        m
        for m in imported
        if m.split(".")[0] in {"boto3", "botocore", "requests", "urllib", "http", "socket", "httpx"}
    ]
    assert not [m for m in imported if "jstock_advisor" in m]  # 他の module へ到達しない


def test_the_module_reads_no_environment_and_makes_no_call_to_the_network() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    calls = {
        getattr(n.func, "attr", getattr(n.func, "id", ""))
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert "environ" not in attrs
    assert not {"getenv", "client", "resource", "Session", "urlopen"} & calls
    assert labels.FILE_VERSION == 1
