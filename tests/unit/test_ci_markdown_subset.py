"""Markdown-only の CI が実行する subset の定義と、その網羅を検査する guard の検査(Issue #866)。

## 構成

- ``tests/ci_markdown_subset.txt`` の整合(実在・重複・形式)
- ``tests/support/markdown_read_audit.py`` の純粋な部分の表
- 静的な補助検査: 文字列定数に Markdown の path を持つテストファイルは、subset か
  ``NON_READERS``(理由つき)のどちらかに載っていなければならない
- 動的な guard の自己検査: 一時 project で別プロセスの pytest を走らせ、
  「Markdown を読むが subset に無いテスト」で session が失敗することを実測する

## 静的な検査の限界

静的な走査(文字列定数)は、走査で間接的に Markdown を読むテスト(``test_scan_for_pii``)を
見つけられない。実測(全体 pytest を audit 付きで 1 回実行)では、実際に Markdown を読むテスト
6 ファイルのうち 5 ファイルしか静的には見つからなかった。主は動的な guard
(``markdown_read_audit``)で、本ファイルの静的な検査は補助である。
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.support import markdown_read_audit as mra

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_DIR = _REPO_ROOT / "tests"
_SUBSET_FILE = _REPO_ROOT / mra.SUBSET_RELPATH

# 文字列定数に Markdown の path が現れるが、tests/ の外の Markdown を読まないテストファイル。
# ★ 理由を必ず書く。もう候補でなくなったら削除する(古い除外を残さない)。
NON_READERS: dict[str, str] = {
    "tests/unit/test_governance_checks.py": (
        "PR 本文の fixture(tests/fixtures/pr_bodies/*.md)を読む。tests/ の下は変更があれば FULL"
    ),
    "tests/unit/test_governance_pr_check_iam_contract_awareness.py": (
        "変更 path の一覧に 'docs/operations_manual.md' という文字列を渡すだけ(内容を読まない)"
    ),
    "tests/unit/test_issue_579_owner_propagation.py": "tmp_path の下へ出力する Markdown(b.md 等)",
    "tests/unit/test_issue_652_config_eol_gitattributes.py": (
        "git add / check-attr / ls-files に path 名を渡すだけ(一時 repo。内容は読まない)"
    ),
    "tests/unit/test_notification_message_golden.py": (
        "メッセージ本文に 'golden/notification/README.md' の文言を含むだけ"
    ),
    "tests/unit/test_producer_consumer_safety_contract.py": (
        "metadata の spec_ref に 'docs/functional_spec.md' の文字列を持つだけ(読まない)"
    ),
    "tests/unit/test_time_semantics_guard.py": (
        "エラーメッセージに 'docs/development_workflow.md' の文言を含むだけ"
    ),
    "tests/unit/test_ci_markdown_only.py": (
        "本 Issue のテスト。path の表に Markdown の文字列を持つだけ"
    ),
    "tests/unit/test_ci_markdown_subset.py": "本ファイル。path の表に Markdown の文字列を持つだけ",
}


def _subset() -> set[str]:
    return mra.parse_subset(_SUBSET_FILE.read_text(encoding="utf-8"))


# --- subset の定義の整合 ------------------------------------------------------------------


def test_subset_file_exists_and_is_not_empty() -> None:
    assert _subset()


def subset_entry_problems(entry: str, exists: Callable[[str], bool]) -> list[str]:
    """subset の entry 1 行の問題の一覧(空なら問題なし)。

    ci.yml は subset を ``$(grep ... | tr -d ...)`` で展開して pytest に渡す(= shell の単語分割)。
    entry の内部に空白があると別々の path として渡され、pytest の usage error で job が赤くなる
    (安全側だが原因が読み取りにくい)。そのため空白を機械的に禁止する。
    """
    problems: list[str] = []
    if any(char.isspace() for char in entry):
        problems.append("contains whitespace (the shell would split it into separate paths)")
    if not entry.startswith("tests/"):
        problems.append("does not start with tests/")
    if not entry.endswith(".py"):
        problems.append("does not end with .py")
    if "\\" in entry or ".." in entry.split("/"):
        problems.append("is not a normalized relative path")
    if not exists(entry):
        problems.append("does not exist")
    return problems


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        ("tests/unit/test_a.py", None),
        ("tests/unit/a b.py", "whitespace"),
        ("tests/unit/a\tb.py", "whitespace"),
        ("tests/unit/a\u3000b.py", "whitespace"),  # 全角空白
        ("tests/unit/a.py ", "whitespace"),
        ("unit/test_a.py", "start with tests/"),
        ("tests/unit/test_a.txt", "end with .py"),
        ("tests/../src/a.py", "normalized"),
        (r"tests\unit\a.py", "normalized"),
    ],
)
def test_subset_entry_problems_table(entry: str, problem: str | None) -> None:
    problems = subset_entry_problems(entry, lambda _entry: True)
    if problem is None:
        assert problems == []
    else:
        assert any(problem in item for item in problems), problems


def test_subset_entry_that_does_not_exist_is_a_problem() -> None:
    assert subset_entry_problems("tests/unit/test_a.py", lambda _entry: False) == ["does not exist"]


def test_every_subset_entry_is_an_existing_test_file_under_tests() -> None:
    for entry in sorted(_subset()):
        problems = subset_entry_problems(entry, lambda item: (_REPO_ROOT / item).is_file())
        assert problems == [], f"{entry}: {problems}"


def test_subset_entries_have_no_duplicates_and_no_trailing_whitespace() -> None:
    lines = [
        line
        for line in _SUBSET_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(lines) == len(set(lines)), "duplicate subset entries"
    for line in lines:
        assert line == line.strip(), repr(line)


def test_subset_is_also_run_by_the_full_suite_so_the_guard_sees_its_readers() -> None:
    # subset のテストは全体 pytest にも含まれる(別 invocation で二重に定義しない)
    collected = {p.relative_to(_REPO_ROOT).as_posix() for p in _TESTS_DIR.rglob("test_*.py")}
    assert _subset() <= collected


# --- plugin の純粋な部分 ---------------------------------------------------------------------


_ROOT = str(_REPO_ROOT)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (os.path.join(_ROOT, "docs", "functional_domains.md"), "docs/functional_domains.md"),
        (os.path.join(_ROOT, "CLAUDE.md"), "CLAUDE.md"),
        (os.path.join(_ROOT, "README.md"), "README.md"),
        (
            os.path.join(_ROOT, ".github", "PULL_REQUEST_TEMPLATE.md"),
            ".github/PULL_REQUEST_TEMPLATE.md",
        ),
        (os.path.join(_ROOT, "infra", "README.md"), "infra/README.md"),
        (os.path.join(_ROOT, "docs", "NOTES.MD"), "docs/NOTES.MD"),  # 大文字小文字は区別しない
        (os.path.join(_ROOT, "docs", "x.markdown"), "docs/x.markdown"),
        # 対象外
        (os.path.join(_ROOT, "tests", "fixtures", "pr_bodies", "compliant.md"), None),
        (os.path.join(_ROOT, ".venv", "Lib", "site-packages", "pkg", "README.md"), None),
        (os.path.join(_ROOT, "venv", "lib", "x.md"), None),
        (os.path.join(_ROOT, "node_modules", "x", "README.md"), None),
        (os.path.join(_ROOT, ".git", "x.md"), None),
        (os.path.join(_ROOT, "docs", "x.txt"), None),
        (os.path.join(_ROOT, "docs", "x.py"), None),
        (os.path.join(os.path.dirname(_ROOT), "elsewhere", "docs", "x.md"), None),  # repo の外
        (
            os.path.join(os.path.dirname(_ROOT), os.path.basename(_ROOT) + "-other", "x.md"),
            None,
        ),  # 前方一致の罠
    ],
)
def test_repo_markdown_relpath_table(path: str, expected: str | None) -> None:
    assert mra.repo_markdown_relpath(path, _ROOT) == expected


def test_repo_markdown_relpath_resolves_relative_paths_against_cwd() -> None:
    assert mra.repo_markdown_relpath("docs/a.md", _ROOT, cwd=_ROOT) == "docs/a.md"
    assert mra.repo_markdown_relpath("a.md", _ROOT, cwd=os.path.join(_ROOT, "docs")) == "docs/a.md"
    assert (
        mra.repo_markdown_relpath("../CLAUDE.md", _ROOT, cwd=os.path.join(_ROOT, "docs"))
        == "CLAUDE.md"
    )
    assert mra.repo_markdown_relpath("../x.md", _ROOT, cwd=_ROOT) is None


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["git", "show", "HEAD:docs/development_workflow.md"], "docs/development_workflow.md"),
        (["git", "show", "abc1234:docs/a.md"], "docs/a.md"),
        (["git", "-c", "core.quotepath=false", "show", "HEAD:CLAUDE.md"], "CLAUDE.md"),
        (
            ["C:\\Program Files\\Git\\bin\\git.EXE", "-C", "x", "show", "HEAD:docs/a.md"],
            "docs/a.md",
        ),
        (["git", "cat-file", "-p", "HEAD:docs/a.md"], "docs/a.md"),
        (["git", "show", "docs/a.md"], "docs/a.md"),
        # Markdown の内容を読まない git の使い方
        (["git", "add", "docs/a.md"], None),
        (["git", "check-attr", "text", "eol", "--", "docs/a.md"], None),
        (["git", "ls-files", "--eol", "--", "docs/a.md"], None),
        (["git", "diff", "--name-only", "a...b"], None),
        (["git", "show", "HEAD:src/a.py"], None),
        (["git", "show"], None),
        ([], None),
        (["python", "-m", "pytest", "docs/a.md"], None),
        (["cat", "docs/a.md"], None),  # 限界: git 以外の外部コマンドは検出しない
    ],
)
def test_git_markdown_read_target_table(argv: list[str], expected: str | None) -> None:
    assert mra.git_markdown_read_target(argv) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (["git", "show", "HEAD:docs/a.md"], ["git", "show", "HEAD:docs/a.md"]),
        (("git", "show"), ["git", "show"]),
        # Windows では連結済みのコマンドライン文字列で届く
        ("git show HEAD:docs/a.md", ["git", "show", "HEAD:docs/a.md"]),
        (
            r'"C:\Program Files\Git\git.EXE" -c a=b show HEAD:docs/a.md',
            [r"C:\Program Files\Git\git.EXE", "-c", "a=b", "show", "HEAD:docs/a.md"],
        ),
        ('git show "unterminated', ["git", "show", "unterminated"]),
        (None, None),
        (42, None),
    ],
)
def test_argv_from_popen_event(value: object, expected: list[str] | None) -> None:
    assert mra.argv_from_popen_event(value) == expected


def test_parse_subset_ignores_comments_blank_lines_and_surrounding_whitespace() -> None:
    text = "# comment\n\n  tests/unit/a.py  \n   # indented comment\ntests/unit/b.py\r\n"
    assert mra.parse_subset(text) == {"tests/unit/a.py", "tests/unit/b.py"}


def test_find_unlisted_readers_reports_only_readers_missing_from_the_subset() -> None:
    reads = {
        "tests/unit/a.py": {"docs/x.md"},
        "tests/unit/b.py": {"docs/y.md", "CLAUDE.md"},
        "tests/unit/c.py": set(),
    }
    assert mra.find_unlisted_readers(reads, {"tests/unit/a.py"}) == {
        "tests/unit/b.py": ["CLAUDE.md", "docs/y.md"]
    }
    assert mra.find_unlisted_readers(reads, set()) == {
        "tests/unit/a.py": ["docs/x.md"],
        "tests/unit/b.py": ["CLAUDE.md", "docs/y.md"],
    }
    assert mra.find_unlisted_readers(reads, {"tests/unit/a.py", "tests/unit/b.py"}) == {}
    assert mra.find_unlisted_readers({}, set()) == {}


# --- audit hook: 実行中のテストが無い・関係のない event は何も記録しない --------------------------


def _isolated_recorder(monkeypatch: pytest.MonkeyPatch, current: str | None) -> mra._Recorder:
    recorder = mra._Recorder(root=_ROOT, current=current)
    monkeypatch.setattr(mra, "_RECORDER", recorder)
    return recorder


def test_audit_records_a_markdown_open_for_the_current_test_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _isolated_recorder(monkeypatch, "tests/unit/x.py")
    mra._audit("open", (os.path.join(_ROOT, "docs", "a.md"), "r", 0))
    assert recorder.reads == {"tests/unit/x.py": {"docs/a.md"}}


def test_audit_records_an_upper_case_markdown_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _isolated_recorder(monkeypatch, "tests/unit/x.py")
    mra._audit("open", (os.path.join(_ROOT, "docs", "A.MD"), "r", 0))
    assert recorder.reads == {"tests/unit/x.py": {"docs/A.MD"}}


def test_audit_records_a_git_show_of_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _isolated_recorder(monkeypatch, "tests/unit/x.py")
    mra._audit("subprocess.Popen", ("git", ["git", "show", "HEAD:docs/a.md"], None, None))
    assert recorder.reads == {"tests/unit/x.py": {"docs/a.md"}}


def test_audit_records_a_git_show_given_as_a_windows_command_line_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _isolated_recorder(monkeypatch, "tests/unit/x.py")
    mra._audit("subprocess.Popen", (None, "git show HEAD:docs/a.md", None, None))
    assert recorder.reads == {"tests/unit/x.py": {"docs/a.md"}}


def test_audit_ignores_everything_when_no_test_is_running(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _isolated_recorder(monkeypatch, None)
    mra._audit("open", (os.path.join(_ROOT, "docs", "a.md"), "r", 0))
    mra._audit("subprocess.Popen", ("git", ["git", "show", "HEAD:docs/a.md"], None, None))
    assert recorder.reads == {}


@pytest.mark.parametrize(
    ("event", "args"),
    [
        ("os.listdir", (os.path.join(_ROOT, "docs"),)),
        ("compile", (b"x", "docs/a.md")),
        ("open", (3, "r", 0)),  # file descriptor
        ("open", (os.path.join(_ROOT, "docs", "a.txt"), "r", 0)),
        ("open", (os.path.join(_ROOT, "tests", "fixtures", "a.md"), "r", 0)),
        ("subprocess.Popen", ("git", ["git", "add", "docs/a.md"], None, None)),
        ("subprocess.Popen", ("git", "not-a-list", None, None)),
        ("open", ()),
    ],
)
def test_audit_ignores_unrelated_events(
    monkeypatch: pytest.MonkeyPatch, event: str, args: tuple[object, ...]
) -> None:
    recorder = _isolated_recorder(monkeypatch, "tests/unit/x.py")
    mra._audit(event, args)
    assert recorder.reads == {}


# --- 静的な補助検査: 文字列定数に Markdown の path を持つテストファイル ---------------------


_MARKDOWN_TOKEN = re.compile(r"\.md$|(^|/)docs(/|$)|^CLAUDE$|README")


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            first = body[0] if body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def static_markdown_candidates() -> dict[str, set[str]]:
    """文字列定数(docstring を除く)に Markdown の path らしいものを持つテストファイル。"""
    found: dict[str, set[str]] = {}
    for path in sorted(_TESTS_DIR.rglob("*.py")):
        relative = path.relative_to(_REPO_ROOT).as_posix()
        if relative.startswith("tests/support/") or path.name == "conftest.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstring_node_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skip
            ):
                for token in re.split(r"[\s\"'`(),]+", node.value):
                    if token and len(token) < 120 and _MARKDOWN_TOKEN.search(token):
                        found.setdefault(relative, set()).add(token)
    return found


def test_static_candidates_are_in_the_subset_or_explained_in_non_readers() -> None:
    candidates = static_markdown_candidates()
    unexplained = sorted(set(candidates) - _subset() - set(NON_READERS))
    assert not unexplained, (
        "Markdown の path らしい文字列を持つテストファイルが、subset にも NON_READERS(理由つき)にも"
        f"載っていません: {unexplained}。"
        "実際に Markdown を読むなら tests/ci_markdown_subset.txt へ、"
        "読まないなら NON_READERS へ理由つきで追加してください。"
    )


def test_non_readers_has_no_stale_entries() -> None:
    candidates = static_markdown_candidates()
    for name in NON_READERS:
        assert (_REPO_ROOT / name).is_file(), f"NON_READERS entry does not exist: {name}"
        assert name in candidates, f"NON_READERS entry is no longer a static candidate: {name}"
        assert name not in _subset(), f"NON_READERS entry is also in the subset: {name}"
        assert NON_READERS[name].strip(), f"NON_READERS entry without a reason: {name}"


# --- 動的な guard の自己検査: 一時 project で別プロセスの pytest を走らせる -------------


_READER_BODY = """\
from pathlib import Path

def test_reads_a_document():
    assert (Path(__file__).resolve().parents[1] / "docs" / "guide.md").read_text(encoding="utf-8")
"""

_MODULE_LEVEL_READER = """\
from pathlib import Path

_TEXT = (Path(__file__).resolve().parents[1] / "docs" / "guide.md").read_text(encoding="utf-8")

def test_uses_text_read_at_import_time():
    assert _TEXT
"""

_GIT_SHOW_READER = """\
import subprocess

def test_reads_a_document_through_git_show():
    subprocess.run(["git", "show", "HEAD:docs/guide.md"], capture_output=True, check=False)
"""

_FIXTURE_ONLY_READER = """\
from pathlib import Path

def test_reads_only_a_fixture_under_tests():
    assert (Path(__file__).resolve().parent / "fixture.md").read_text(encoding="utf-8")
"""

_NON_READER = """\
def test_does_not_read_any_document():
    assert 1 + 1 == 2
"""


def _run_child_pytest(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_REPO_ROOT), env.get("PYTHONPATH", "")]))
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.support.markdown_read_audit",
            "-p",
            "no:cacheprovider",
            "-q",
            *args,
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=120,
    )


def _make_project(tmp_path: Path, test_name: str, body: str, subset: str | None) -> Path:
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (project / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
    (project / "tests" / "fixture.md").write_text("fixture\n", encoding="utf-8")
    (project / "tests" / test_name).write_text(body, encoding="utf-8")
    if subset is not None:
        (project / "tests" / "ci_markdown_subset.txt").write_text(subset, encoding="utf-8")
    return project


def test_guard_fails_the_session_when_a_markdown_reader_is_not_in_the_subset(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path, "test_reader.py", _READER_BODY, "# empty\n")
    result = _run_child_pytest(project)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 passed" in result.stdout  # テスト自体は通っている(guard が session を失敗にした)
    assert "tests/test_reader.py" in result.stdout
    assert "docs/guide.md" in result.stdout
    assert "tests/ci_markdown_subset.txt" in result.stdout


def test_guard_passes_when_the_reader_is_listed(tmp_path: Path) -> None:
    project = _make_project(tmp_path, "test_reader.py", _READER_BODY, "tests/test_reader.py\n")
    result = _run_child_pytest(project)
    assert result.returncode == 0, result.stdout + result.stderr


def test_guard_catches_a_markdown_read_at_import_time(tmp_path: Path) -> None:
    project = _make_project(tmp_path, "test_import_time.py", _MODULE_LEVEL_READER, "# empty\n")
    result = _run_child_pytest(project)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "tests/test_import_time.py" in result.stdout


def test_guard_catches_a_markdown_read_through_git_show(tmp_path: Path) -> None:
    project = _make_project(tmp_path, "test_git_show.py", _GIT_SHOW_READER, "# empty\n")
    result = _run_child_pytest(project)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "tests/test_git_show.py" in result.stdout


def test_guard_does_not_flag_a_test_that_reads_only_a_fixture_under_tests(tmp_path: Path) -> None:
    project = _make_project(tmp_path, "test_fixture.py", _FIXTURE_ONLY_READER, "# empty\n")
    result = _run_child_pytest(project)
    assert result.returncode == 0, result.stdout + result.stderr


def test_guard_does_not_flag_a_test_that_reads_nothing(tmp_path: Path) -> None:
    project = _make_project(tmp_path, "test_plain.py", _NON_READER, "# empty\n")
    result = _run_child_pytest(project)
    assert result.returncode == 0, result.stdout + result.stderr


def test_collect_only_run_never_fails_even_with_an_unlisted_import_time_reader(
    tmp_path: Path,
) -> None:
    # tests/unit/test_issue_277_cohort_markers.py は子の `pytest --collect-only` を起こす。
    # 子が plugin の影響で exit 1 になってはならない。
    project = _make_project(tmp_path, "test_import_time.py", _MODULE_LEVEL_READER, "# empty\n")
    result = _run_child_pytest(project, "--collect-only")
    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_subset_definition_fails_when_a_reader_exists(tmp_path: Path) -> None:
    project = _make_project(tmp_path, "test_reader.py", _READER_BODY, None)
    result = _run_child_pytest(project)
    assert result.returncode == 1, result.stdout + result.stderr


def test_missing_subset_definition_is_ignored_when_nothing_reads_markdown(tmp_path: Path) -> None:
    project = _make_project(tmp_path, "test_plain.py", _NON_READER, None)
    result = _run_child_pytest(project)
    assert result.returncode == 0, result.stdout + result.stderr


def test_guard_works_for_a_single_file_argument_and_a_different_cwd(tmp_path: Path) -> None:
    project = _make_project(tmp_path, "test_reader.py", _READER_BODY, "# empty\n")
    result = _run_child_pytest(project, "tests/test_reader.py")
    assert result.returncode == 1, result.stdout + result.stderr
    inside = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.support.markdown_read_audit",
            "-p",
            "no:cacheprovider",
            "-q",
            "test_reader.py",
        ],
        cwd=project / "tests",
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(_REPO_ROOT), os.environ.get("PYTHONPATH", "")]),
            "PYTHONIOENCODING": "utf-8",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=120,
    )
    assert inside.returncode == 1, inside.stdout + inside.stderr
