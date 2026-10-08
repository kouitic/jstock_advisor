"""CI の test job が「Markdown のみの変更」かどうかを判定する(Issue #866)。

## 何を解決するか

Markdown だけを変える PR(規則の文面・手順書など)でも、test job は約 9〜13 分(run により変動。
2026-10-09 の実測で、全体 pytest の step が 8 分台〜13 分台)の全体 pytest を push と
pull_request の両方で実行していた。コードが 1 行も変わらない
変更に対して同じ全体回帰を重ねて走らせるのは待ち時間と CI の占有の無駄である。

## 何をするか

``.github/workflows/ci.yml`` の test job の最初に本 script を実行し、結果(``markdown_only``)で
重い step(全体 pytest)を **step 単位で** 省略する。test job 自体は存在し続けて成功する
(required check ``test`` の context を Pending にしない。Ruleset は変更しない)。

## 判定(MARKDOWN_ONLY)

次をすべて満たすときだけ ``true``。1 つでも外れる・判定できないときは ``false`` (FULL_TEST)。
**曖昧なら FULL_TEST(fail-safe)**。

- 変更されたファイルが 1 つ以上ある
- すべてのファイルが小文字の ``.md`` で終わる
- どのファイルも ``src/`` ``tests/`` ``config/`` ``infra/`` ``scripts/`` ``.github/`` の下にない
  (Markdown でも、コード・設定・CI 定義・テストの fixture と同じ場所にあるものは FULL)
- 削除・rename も変更として見る(rename は旧 path と新 path の両方を見る。
  ``src/x.py -> docs/x.md`` の rename は ``src/x.py`` の削除を含むので FULL)

``docs/`` の下だから Markdown-only、とは **しない**。拡張子が ``.md`` でない文書
(``docs/policy_registry.yaml`` など)は FULL である。

## 比較範囲

| event | 範囲 |
|---|---|
| pull_request | ``base.sha...head.sha``(PR 全体。PR の「Files changed」と同じ) |
| push(``issue-*``) | ``origin/main...HEAD``(最新 main との merge-base から branch の先頭まで。
**最後の commit だけを見ない**) |
| push(main)・workflow_dispatch・その他 | 判定しない(``false``)。main の CI run は
release・lock 解放の証跡である |

## 本 script の責務の外

Markdown を実際に読むテスト群(subset)の定義・検査は ``tests/ci_markdown_subset.txt`` と
``tests/support/markdown_read_audit.py``。本 script は「全体 pytest を省略してよいか」だけを決める。

stdlib のみ(pip install の前に実行するため)。**常に exit 0** で、判定は ``GITHUB_OUTPUT`` の
``markdown_only=true|false`` で返す(出力が無い・script が失敗した場合は step の ``if`` が
``!= 'true'`` となり、全体 pytest が走る = fail-safe)。
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence

# Markdown でも FULL とする場所。tests/ の fixture(*.md)・infra/README.md・.github/ の
# テンプレート等は、subset が読まない reader(例: tests/fixtures を読むテスト)や
# CI 定義そのものに繋がるため、拡張子だけでは Markdown-only としない。
FULL_PREFIXES: tuple[str, ...] = ("src/", "tests/", "config/", "infra/", "scripts/", ".github/")

MARKDOWN_SUFFIX = ".md"

# git の実行を差し替えられるようにする(unit test が実 git を使わずに範囲の選択を検証できる)。
# 成功した出力(文字列)を返し、失敗したら None を返す。
RunGit = Callable[[Sequence[str]], "str | None"]


def is_markdown_only(paths: Iterable[str]) -> bool:
    """変更された path の一覧が「Markdown のみの変更」か。空・不明は False(fail-safe)。"""
    seen = False
    for raw in paths:
        path = raw.strip()
        if not path:
            return False  # 空の path は判定できない
        if "\\" in path or path.startswith(("/", "./", "../")) or ".." in path.split("/"):
            return False  # 正規化されていない path は判定しない(git は常に / 区切りの相対 path)
        if not path.endswith(MARKDOWN_SUFFIX):
            return False
        if path.startswith(FULL_PREFIXES):
            return False
        seen = True
    return seen


def _diff_args(range_spec: str) -> list[str]:
    # --no-renames: rename を delete + add の 2 件として列挙し、旧 path も判定に入れる。
    # -z: path を NUL 区切りで受ける(空白・日本語を含む path を quote されずに扱う)。
    return ["diff", "--name-only", "--no-renames", "-z", range_spec]


def _split_z(output: str) -> list[str]:
    return [p for p in output.split("\0") if p]


def range_spec(event_name: str, ref: str, base_sha: str, head_sha: str) -> str | None:
    """event に応じた比較範囲。範囲を選べないときは None(= 判定せず full)。"""
    if event_name == "pull_request":
        if not base_sha or not head_sha:
            return None
        return f"{base_sha}...{head_sha}"
    if event_name == "push" and ref.startswith("refs/heads/issue-"):
        return "origin/main...HEAD"
    return None  # main への push・workflow_dispatch・未知の event は常に full


def classify(
    event_name: str,
    ref: str,
    base_sha: str,
    head_sha: str,
    run_git: RunGit,
) -> tuple[bool, str]:
    """(markdown_only, 理由)。理由は CI のログへ出す(判定を後から読めるようにする)。"""
    spec = range_spec(event_name, ref, base_sha, head_sha)
    if spec is None:
        return False, f"FULL_TEST: no comparison range for event={event_name!r} ref={ref!r}"
    output = run_git(_diff_args(spec))
    if output is None:
        return False, f"FULL_TEST: git diff failed for {spec}"
    paths = _split_z(output)
    if not paths:
        return False, "FULL_TEST: empty diff"
    if is_markdown_only(paths):
        return (
            True,
            f"MARKDOWN_ONLY: {len(paths)} changed file(s), all *.md outside code/config/CI dirs",
        )
    others = [p for p in paths if not is_markdown_only([p])]
    return (
        False,
        f"FULL_TEST: {len(others)} of {len(paths)} changed file(s) are not plain Markdown: "
        f"{others[:10]}",
    )


def _subprocess_git(args: Sequence[str]) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603 - 固定の git 引数のみ(外部入力は SHA だけ)
            ["git", *args],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace")


def main(environ: dict[str, str] | None = None, run_git: RunGit | None = None) -> int:
    env = os.environ if environ is None else environ
    runner = _subprocess_git if run_git is None else run_git
    try:
        markdown_only, reason = classify(
            env.get("GITHUB_EVENT_NAME", ""),
            env.get("GITHUB_REF", ""),
            env.get("PR_BASE_SHA", ""),
            env.get("PR_HEAD_SHA", ""),
            runner,
        )
    except Exception as exc:  # noqa: BLE001 - どんな失敗も FULL_TEST に倒す(fail-safe)
        markdown_only, reason = False, f"FULL_TEST: classifier failed ({type(exc).__name__})"
    print(reason)
    output_path = env.get("GITHUB_OUTPUT")
    if output_path:
        try:
            with open(output_path, "a", encoding="utf-8") as handle:
                handle.write(f"markdown_only={'true' if markdown_only else 'false'}\n")
        except OSError as exc:
            # 出力が無ければ step の if(!= 'true')が全体 pytest を実行する = fail-safe
            print(f"FULL_TEST: cannot write GITHUB_OUTPUT ({type(exc).__name__})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
