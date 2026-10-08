"""scripts/ci_markdown_only.py と、ci.yml の test job の構造の検査(Issue #866)。

## 何を固定するか

CI の test job は、変更が Markdown のみのとき全体 pytest を省略する。省略してよいかを決める判定
(``is_markdown_only`` / 比較範囲の選択 / fail-safe)と、省略が **required check ``test`` を
壊さない**ための ci.yml の構造(job の存在・``on:`` の不変・step 単位の ``if``)を固定する。

- 判定の表(USER の指定の検証ケース 6 件 + 境界 + MANAGER の MUST-2 のケース)
- 比較範囲: pull_request は ``base...head`` /
  issue-* の push は ``origin/main...HEAD``(branch 全体)/
  main の push・workflow_dispatch・未知の event は判定せず full。実 git の一時 repo で範囲を実測する
  (「過去 commit に src の変更があり、最新 commit は README のみ」は FULL)
- fail-safe: 空の差分・git の失敗・範囲を選べない・script の例外は、すべて ``false``
- ci.yml: job ``test`` が存在し job-level の ``if`` が無い / ``on:`` に paths・paths-ignore が無い /
  classify が重い step より前 / 全体 pytest は ``!= 'true'`` /
  subset は ``== 'true'`` かつ定義 file を読む
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import ci_markdown_only as cmo  # noqa: E402

_CI_YML = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


# --- is_markdown_only: 判定の表 ---------------------------------------------------------


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        # USER の指定の検証ケース(6 件)
        (["CLAUDE.md"], True),
        (["docs/foo.md"], True),
        (["docs/foo.md", "src/foo.py"], False),
        (["docs/policy_registry.yaml"], False),
        ([".github/workflows/ci.yml"], False),
        # 「過去 commit に src の変更・最新 commit は README のみ」は
        # branch 全体の path の一覧として
        # src/ を含む形で届く(範囲の実測は下の実 git の test)
        (["src/foo.py", "README.md"], False),
        # MANAGER の MUST-2: コード・設定・CI・テストの場所の下は *.md でも FULL
        (["tests/fixtures/pr_bodies/x.md"], False),
        (["infra/README.md"], False),
        (["src/jstock_advisor/README.md"], False),
        (["config/README.md"], False),
        (["scripts/README.md"], False),
        ([".github/PULL_REQUEST_TEMPLATE.md"], False),
        # 複数の Markdown
        (["CLAUDE.md", "docs/a.md", "docs/design/b.md"], True),
        (["README.md"], True),
        # 拡張子の境界(小文字の .md のみ)
        (["docs/foo.MD"], False),
        (["docs/foo.markdown"], False),
        (["docs/foo.md.bak"], False),
        (["docs/foo.txt"], False),
        (["docs/foo"], False),
        (["docs/policy_registry.yml"], False),
        (["requirements-lock.txt"], False),
        (["pyproject.toml"], False),
        # prefix に似た名前は FULL_PREFIXES に当たらない(docs/src/x.md は docs/ の下)
        (["docs/src/x.md"], True),
        (["srcdocs/x.md"], True),
        (["testsuite.md"], True),
        # 判定できない
        ([], False),
        ([""], False),
        (["  "], False),
        (["docs/a.md", ""], False),
        (["/etc/x.md"], False),
        (["./docs/a.md"], False),
        (["../x.md"], False),
        (["docs/../src/x.md"], False),
        (["docs\\a.md"], False),
    ],
)
def test_is_markdown_only_table(paths: list[str], expected: bool) -> None:
    assert cmo.is_markdown_only(paths) is expected


def test_is_markdown_only_accepts_any_iterable() -> None:
    assert cmo.is_markdown_only(p for p in ("CLAUDE.md", "docs/a.md")) is True
    assert cmo.is_markdown_only(iter(())) is False


# --- classify: 比較範囲の選択(fake の git で、呼ばれる引数を検証する) ------------------------


class _FakeGit:
    def __init__(self, output: str | None) -> None:
        self.output = output
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> str | None:
        self.calls.append(list(args))
        return self.output


def _z(*paths: str) -> str:
    return "\0".join(paths) + ("\0" if paths else "")


def test_pull_request_compares_base_three_dot_head_with_no_renames_and_nul_separated() -> None:
    git = _FakeGit(_z("docs/a.md"))
    markdown_only, _ = cmo.classify("pull_request", "refs/pull/1/merge", "BASE", "HEAD1", git)
    assert markdown_only is True
    assert git.calls == [["diff", "--name-only", "--no-renames", "-z", "BASE...HEAD1"]]


def test_issue_branch_push_compares_the_whole_branch_against_origin_main() -> None:
    git = _FakeGit(_z("docs/a.md"))
    markdown_only, _ = cmo.classify("push", "refs/heads/issue-866-x", "", "", git)
    assert markdown_only is True
    assert git.calls == [["diff", "--name-only", "--no-renames", "-z", "origin/main...HEAD"]]


@pytest.mark.parametrize(
    ("event", "ref"),
    [
        ("push", "refs/heads/main"),
        ("workflow_dispatch", "refs/heads/main"),
        ("workflow_dispatch", "refs/heads/issue-866-x"),
        ("push", "refs/heads/feature/x"),  # issue-** 以外の branch は CI の対象外だが、来ても full
        ("push", "refs/tags/v1"),
        ("schedule", "refs/heads/main"),
        ("", ""),
    ],
)
def test_events_without_a_comparison_range_are_always_full_and_never_call_git(
    event: str, ref: str
) -> None:
    git = _FakeGit(_z("docs/a.md"))  # 呼ばれたら Markdown-only になってしまう出力
    markdown_only, reason = cmo.classify(event, ref, "BASE", "HEAD1", git)
    assert markdown_only is False
    assert git.calls == []
    assert reason.startswith("FULL_TEST")


@pytest.mark.parametrize(("base", "head"), [("", "H"), ("B", ""), ("", "")])
def test_pull_request_without_both_shas_is_full(base: str, head: str) -> None:
    git = _FakeGit(_z("docs/a.md"))
    assert cmo.classify("pull_request", "refs/pull/1/merge", base, head, git)[0] is False
    assert git.calls == []


def test_git_failure_is_full() -> None:
    git = _FakeGit(None)
    markdown_only, reason = cmo.classify("pull_request", "r", "B", "H", git)
    assert markdown_only is False
    assert reason == "FULL_TEST: git diff failed for B...H"


def test_empty_diff_is_full_not_markdown_only() -> None:
    markdown_only, reason = cmo.classify("pull_request", "r", "B", "H", _FakeGit(""))
    assert markdown_only is False
    assert "empty diff" in reason


def test_reason_names_the_files_that_forced_full_test() -> None:
    markdown_only, reason = cmo.classify(
        "pull_request", "r", "B", "H", _FakeGit(_z("docs/a.md", "src/x.py", "tests/fixtures/y.md"))
    )
    assert markdown_only is False
    assert "src/x.py" in reason and "tests/fixtures/y.md" in reason
    assert "docs/a.md" not in reason


def test_paths_with_spaces_and_japanese_are_handled_via_nul_separation() -> None:
    names = _z("docs/日本語 の 文書.md", "docs/sp ace.md")
    assert cmo.classify("pull_request", "r", "B", "H", _FakeGit(names))[0] is True


# --- main(): GITHUB_OUTPUT・fail-safe・常に exit 0 -----------------------------------------


def _env(tmp_path: Path, **kwargs: str) -> dict[str, str]:
    base = {"GITHUB_OUTPUT": str(tmp_path / "out.txt")}
    base.update(kwargs)
    return base


def test_main_writes_true_for_markdown_only_and_returns_zero(tmp_path: Path) -> None:
    env = _env(
        tmp_path,
        GITHUB_EVENT_NAME="pull_request",
        GITHUB_REF="refs/pull/1/merge",
        PR_BASE_SHA="B",
        PR_HEAD_SHA="H",
    )
    assert cmo.main(env, _FakeGit(_z("CLAUDE.md"))) == 0
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "markdown_only=true\n"


def test_main_writes_false_for_code_change(tmp_path: Path) -> None:
    env = _env(
        tmp_path,
        GITHUB_EVENT_NAME="pull_request",
        GITHUB_REF="r",
        PR_BASE_SHA="B",
        PR_HEAD_SHA="H",
    )
    assert cmo.main(env, _FakeGit(_z("CLAUDE.md", "src/a.py"))) == 0
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "markdown_only=false\n"


def test_main_writes_false_when_the_classifier_raises_and_still_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(args: Sequence[str]) -> str | None:
        raise RuntimeError("git exploded")

    env = _env(
        tmp_path,
        GITHUB_EVENT_NAME="pull_request",
        GITHUB_REF="r",
        PR_BASE_SHA="B",
        PR_HEAD_SHA="H",
    )
    assert cmo.main(env, boom) == 0
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "markdown_only=false\n"
    assert "classifier failed" in capsys.readouterr().out


def test_main_survives_an_unwritable_output_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = {
        "GITHUB_OUTPUT": str(tmp_path / "no_such_dir" / "out.txt"),
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_REF": "r",
        "PR_BASE_SHA": "B",
        "PR_HEAD_SHA": "H",
    }
    assert cmo.main(env, _FakeGit(_z("CLAUDE.md"))) == 0
    assert "cannot write GITHUB_OUTPUT" in capsys.readouterr().out


def test_main_without_github_output_only_prints(capsys: pytest.CaptureFixture[str]) -> None:
    assert cmo.main({"GITHUB_EVENT_NAME": "workflow_dispatch"}, _FakeGit(None)) == 0
    assert capsys.readouterr().out.startswith("FULL_TEST")


# --- 実 git の一時 repo: 比較範囲を実測する -------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=ci-test",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.quotepath=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _write(repo: Path, relative: str, text: str = "x\n") -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _write(root, "src/app.py", "a = 1\n")
    _write(root, "docs/guide.md", "g\n")
    _write(root, "README.md", "r\n")
    _commit(root, "base")
    # CI の checkout (fetch-depth: 0) と同じく、origin/main を参照できる状態にする
    _git(root, "update-ref", "refs/remotes/origin/main", "refs/heads/main")
    monkeypatch.chdir(root)
    return root


def _classify_push(branch: str = "issue-866-x") -> bool:
    return cmo.classify("push", f"refs/heads/{branch}", "", "", cmo._subprocess_git)[0]


def test_real_git_branch_with_only_markdown_commits_is_markdown_only(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "issue-866-x")
    _write(repo, "docs/guide.md", "g2\n")
    _commit(repo, "c1")
    _write(repo, "docs/new.md", "n\n")
    _commit(repo, "c2")
    assert _classify_push() is True


def test_real_git_earlier_commit_changed_src_and_latest_commit_is_readme_only_is_full(
    repo: Path,
) -> None:
    # USER の検証ケース: 最後の commit だけを見ると Markdown-only に見えるが、
    # branch 全体では src を含む
    _git(repo, "checkout", "-q", "-b", "issue-866-x")
    _write(repo, "src/app.py", "a = 2\n")
    _commit(repo, "touch src")
    _write(repo, "README.md", "r2\n")
    last = _commit(repo, "readme only")
    assert (
        _git(repo, "diff", "--name-only", f"{last}~1", last) == "README.md"
    )  # 前提: 最後の commit は README のみ
    assert _classify_push() is False


def test_real_git_main_advancing_after_the_branch_point_does_not_leak_into_the_branch_range(
    repo: Path,
) -> None:
    _git(repo, "checkout", "-q", "-b", "issue-866-x")
    _write(repo, "docs/guide.md", "g2\n")
    _commit(repo, "md on branch")
    # main が先へ進む(src を変更)。origin/main も追随する = CI の checkout 時点の状態
    _git(repo, "checkout", "-q", "main")
    _write(repo, "src/app.py", "a = 9\n")
    _commit(repo, "main moves on with src")
    _git(repo, "update-ref", "refs/remotes/origin/main", "refs/heads/main")
    _git(repo, "checkout", "-q", "issue-866-x")
    assert _classify_push() is True  # merge-base ... HEAD なので main 側の src の変更は含まれない


def test_real_git_branch_that_merged_main_keeps_only_its_own_changes(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "issue-866-x")
    _write(repo, "docs/guide.md", "g2\n")
    _commit(repo, "md on branch")
    _git(repo, "checkout", "-q", "main")
    _write(repo, "src/app.py", "a = 9\n")
    _commit(repo, "main moves on with src")
    _git(repo, "update-ref", "refs/remotes/origin/main", "refs/heads/main")
    _git(repo, "checkout", "-q", "issue-866-x")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge main", "main")
    assert _classify_push() is True


def test_real_git_branch_with_src_and_markdown_is_full(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "issue-866-x")
    _write(repo, "docs/guide.md", "g2\n")
    _write(repo, "src/app.py", "a = 3\n")
    _commit(repo, "both")
    assert _classify_push() is False


def test_real_git_pull_request_range_is_base_three_dot_head(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "topic")
    _write(repo, "docs/guide.md", "g2\n")
    head = _commit(repo, "md")
    # base 側が先へ進んでも、PR の差分は merge-base からの分だけ(three-dot)
    _git(repo, "checkout", "-q", "main")
    _write(repo, "src/app.py", "a = 9\n")
    new_base = _commit(repo, "base moves on")
    result = cmo.classify("pull_request", "refs/pull/1/merge", new_base, head, cmo._subprocess_git)
    assert result[0] is True
    assert base != new_base


def test_real_git_pull_request_with_code_is_full(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "topic")
    _write(repo, "docs/guide.md", "g2\n")
    _write(repo, "tests/test_x.py", "def test_x(): pass\n")
    head = _commit(repo, "md + test")
    assert cmo.classify("pull_request", "r", base, head, cmo._subprocess_git)[0] is False


def test_real_git_rename_from_src_to_docs_is_full_because_the_old_path_is_seen(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "issue-866-x")
    _git(repo, "mv", "src/app.py", "docs/app.md")
    _commit(repo, "rename src py to docs md")
    assert _classify_push() is False


def test_real_git_rename_between_markdown_files_is_markdown_only(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "issue-866-x")
    _git(repo, "mv", "docs/guide.md", "docs/manual.md")
    _commit(repo, "rename md")
    assert _classify_push() is True


def test_real_git_deleting_a_markdown_file_is_markdown_only(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "issue-866-x")
    _git(repo, "rm", "-q", "docs/guide.md")
    _commit(repo, "delete md")
    assert _classify_push() is True


def test_real_git_deleting_a_source_file_is_full(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "issue-866-x")
    _git(repo, "rm", "-q", "src/app.py")
    _commit(repo, "delete src")
    assert _classify_push() is False


def test_real_git_japanese_file_name_is_read_without_quoting(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "issue-866-x")
    _write(repo, "docs/日本語の文書.md", "j\n")
    _commit(repo, "japanese name")
    assert _classify_push() is True


def test_real_git_branch_with_no_change_is_full_not_markdown_only(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "issue-866-x")
    assert _classify_push() is False  # 空の差分は判定できないので FULL_TEST


def test_real_git_main_push_is_full_even_for_a_markdown_only_commit(repo: Path) -> None:
    _write(repo, "docs/guide.md", "g2\n")
    _commit(repo, "md on main")
    result = cmo.classify("push", "refs/heads/main", "", "", cmo._subprocess_git)
    assert result[0] is False


def test_real_git_failure_missing_origin_main_is_full(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "issue-866-x")
    _write(repo, "docs/guide.md", "g2\n")
    _commit(repo, "md")
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert _classify_push() is False  # origin/main を解決できない = 範囲を選べない


# --- ci.yml の構造: required check `test` を壊さない -------------------------------------------


def _load_ci() -> dict[object, object]:
    return yaml.safe_load(_CI_YML.read_text(encoding="utf-8"))


def _jobs() -> dict[str, dict[str, object]]:
    jobs = _load_ci()["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _steps(job: str) -> list[dict[str, object]]:
    steps = _jobs()[job]["steps"]
    assert isinstance(steps, list)
    return steps


def test_ci_defines_the_expected_jobs_and_test_is_among_them() -> None:
    # required check の context 名(Ruleset の required_status_checks)は job 名。増減・改名を検知する
    assert set(_jobs()) == {
        "lint",
        "typecheck",
        "test",
        "secret-scan",
        "pii-scan",
        "pii-scan-commit-messages",
        "dependency-audit",
        "layer-lock-drift",
        "catalog-coverage",
    }


def test_test_job_has_no_job_level_condition_and_no_name_override() -> None:
    # job-level の if は job 全体を skipped にし、required check の context が成功にならない
    job = _jobs()["test"]
    assert "if" not in job
    assert "name" not in job
    assert "needs" not in job


def test_workflow_triggers_are_unchanged_and_have_no_path_filters() -> None:
    config = _load_ci()
    triggers = config.get("on", config.get(True))
    assert isinstance(triggers, dict)
    assert set(triggers) == {"push", "pull_request", "workflow_dispatch"}
    for name, spec in triggers.items():
        if isinstance(spec, dict):
            assert "paths" not in spec, name
            assert "paths-ignore" not in spec, name
    assert triggers["push"]["branches"] == ["main", "issue-**"]
    assert triggers["pull_request"] is None  # branches の絞りも無い(どの base の PR でも起動する)


def _step_index(steps: list[dict[str, object]], needle: str) -> int:
    for index, step in enumerate(steps):
        if needle in str(step.get("run", "")):
            return index
    raise AssertionError(f"no step runs {needle!r}")


def test_classify_runs_before_the_heavy_steps_and_fails_safe() -> None:
    steps = _steps("test")
    classify = _step_index(steps, "scripts/ci_markdown_only.py")
    install = _step_index(steps, "pip install -r requirements-lock.txt")
    full = _step_index(steps, "python -m pytest tests -q")
    subset = _step_index(steps, "tests/ci_markdown_subset.txt")
    assert classify < install < full
    assert classify < subset
    step = steps[classify]
    assert step["id"] == "classify"
    # classify 自体が失敗しても job は失敗しない(output が無い -> 全体 pytest が走る = fail-safe)
    assert step.get("continue-on-error") is True
    assert "if" not in step
    env = step["env"]
    assert isinstance(env, dict)
    assert set(env) == {"PR_BASE_SHA", "PR_HEAD_SHA"}  # event の値は env 経由(shell に展開しない)


def test_checkout_fetches_full_history_for_merge_base_comparison() -> None:
    checkout = _steps("test")[0]
    assert str(checkout["uses"]).startswith("actions/checkout@")
    assert checkout["with"] == {"fetch-depth": 0}


def test_full_pytest_is_skipped_only_when_the_classifier_said_true() -> None:
    steps = _steps("test")
    full = steps[_step_index(steps, "python -m pytest tests -q")]
    assert full["if"] == "steps.classify.outputs.markdown_only != 'true'"


def test_pip_install_is_unconditional_because_the_subset_needs_pytest_too() -> None:
    steps = _steps("test")
    install = steps[_step_index(steps, "pip install -r requirements-lock.txt")]
    assert "if" not in install


def test_markdown_only_runs_the_subset_from_the_single_definition_file() -> None:
    steps = _steps("test")
    subset = steps[_step_index(steps, "tests/ci_markdown_subset.txt")]
    assert subset["if"] == "steps.classify.outputs.markdown_only == 'true'"
    run = str(subset["run"])
    assert "python -m pytest" in run
    # 定義を ci.yml へ二重に書かない(test file の path をハードコードしない)
    assert "tests/unit/" not in run
    assert ".py" not in run.replace("python", "")


def test_markdown_only_prints_the_skip_notice() -> None:
    steps = _steps("test")
    notice = steps[_step_index(steps, "Markdown-only change")]
    assert notice["if"] == "steps.classify.outputs.markdown_only == 'true'"


def test_no_step_of_the_test_job_can_run_both_the_full_suite_and_the_subset() -> None:
    steps = _steps("test")
    full_if = steps[_step_index(steps, "python -m pytest tests -q")]["if"]
    subset_if = steps[_step_index(steps, "tests/ci_markdown_subset.txt")]["if"]
    assert full_if != subset_if  # 条件が補い合う(!= 'true' と == 'true')


def test_other_jobs_keep_their_own_conditions() -> None:
    # pii-scan-commit-messages は pull_request のみ(従来どおり)。他の job に if は無い
    assert _jobs()["pii-scan-commit-messages"]["if"] == "github.event_name == 'pull_request'"
    for name in (
        "lint",
        "typecheck",
        "secret-scan",
        "pii-scan",
        "dependency-audit",
        "layer-lock-drift",
        "catalog-coverage",
    ):
        assert "if" not in _jobs()[name], name


# --- script 自体の性質 ---------------------------------------------------------------------


def test_script_uses_only_the_standard_library() -> None:
    # classify は pip install の前に走る(依存を入れる前に判定する)
    source = (_REPO_ROOT / "scripts" / "ci_markdown_only.py").read_text(encoding="utf-8")
    import ast

    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), sorted(imported - set(sys.stdlib_module_names))


def test_runner_type_accepts_the_real_git_function() -> None:
    runner: Callable[[Sequence[str]], str | None] = cmo._subprocess_git
    assert callable(runner)
