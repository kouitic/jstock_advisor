"""docs/functional_domains.md の網羅性(M.5 COVERAGE_INVARIANT)を検査する read-only の script(Issue #480)。

Issue #212 Phase C の設計(issuecomment-5574967080)の算法を機械化する。

    python scripts/check_catalog_coverage.py
    python scripts/check_catalog_coverage.py --doc docs/functional_domains.md --src src/jstock_advisor

## 何を検査するか(M.5 の C1〜C4 と C3-b)

    C1   src/jstock_advisor/ 配下の全 module(__init__.py を除く)は、F 行 / S 行の「主要 source」に属する
    C2   属し方はファイル指定(`services/x.py`)とディレクトリ指定(`domain/valuation/`)のどちらでもよい
    C3   どちらにも属さない module は、UNCATALOGED 一覧に載っている
    C3-b UNCATALOGED 一覧に載っているのに、実は F 行 / S 行に覆われている module がある(一覧の陳腐化)
    C3-c UNCATALOGED 一覧に載っているのに、実在しない module がある(一覧の陳腐化)
    C4   F 行 / S 行の「主要 source」に書かれた path は実在する(src 配下、または repo root 相対の `infra/` `scripts/` 等)

## 照合の方式(#212 の実測で確認済みの前提)

- 照合するのは **F 行 / S 行の「主要 source」(S 行では「主要 path」)の列だけ**である。文書全文ではない。
  散文に例として書かれた path を「覆っている」と誤判定しないため。
- 照合は **相対 path** で行う。basename では照合しない(別ディレクトリの同名ファイルを、覆われていると誤判定するため。
  #212 の実測で 16 件がこの型だった)。
- 表記の揺れ(`src/jstock_advisor/` で始まる path)は、接頭辞を除去して正規化する。

## 本スクリプトが検査しないこと

**影響領域の記載と実際の参照元の整合(「正しさ」)は検査しない。** 本 script が見るのは「網羅」だけである。
同じ job に混ぜると FAIL の意味が 2 つになって読めなくなる(#212 Phase C の設計 (i))。
機能の説明が正しいかも機械では判定できない。

## read-only であること

ファイルを読むだけで、何も書き換えない。標準ライブラリのみを使う。

## exit code

    0  違反なし
    1  違反あり(違反の一覧を標準出力へ出す)
    2  検査できなかった(文書または src が見つからない、表を読めない)。「違反なし」とは読まない
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_SRC_PREFIX = "src/jstock_advisor/"
_ROW_RE = re.compile(r"^\|\s*(F-\d+|S-\d+)\s*\|")
_TOKEN_RE = re.compile(r"`([^`]+)`")
_UNCATALOGED_HEADING = "### UNCATALOGED 一覧"
# 「主要 source」(F 行)/「主要 path」(S 行)は、どちらも 3 列目である(ID | 機能 or 共通部品 | 主要…)。
_SOURCE_COLUMN_INDEX = 2

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNCHECKABLE = 2


@dataclass(frozen=True)
class Violation:
    condition: str
    path: str
    detail: str

    def render(self) -> str:
        return f"{self.condition} {self.path} - {self.detail}"


@dataclass
class Report:
    module_total: int = 0
    covered_by_file: int = 0
    covered_by_dir: int = 0
    uncataloged_listed: int = 0
    uncovered: int = 0
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def _normalize(token: str) -> str:
    token = token.strip()
    if token.startswith(_SRC_PREFIX):
        token = token[len(_SRC_PREFIX) :]
    return token


def parse_source_tokens(doc_text: str) -> tuple[set[str], set[str]]:
    """F 行 / S 行の主要 source 列から、(ファイル指定, ディレクトリ指定) を取り出す。

    対象は `.py` のファイルと、末尾が `/` のディレクトリだけである。`.yaml` / `.json` 等の設定は、
    src/jstock_advisor/ 配下の module ではないため対象外。
    """
    files: set[str] = set()
    dirs: set[str] = set()
    for line in doc_text.splitlines():
        if not _ROW_RE.match(line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) <= _SOURCE_COLUMN_INDEX:
            continue
        for raw in _TOKEN_RE.findall(cells[_SOURCE_COLUMN_INDEX]):
            token = _normalize(raw)
            if token.endswith(".py"):
                files.add(token)
            elif token.endswith("/"):
                dirs.add(token)
    return files, dirs


def parse_uncataloged(doc_text: str) -> set[str]:
    """UNCATALOGED 一覧の表から、module の path を取り出す。

    節の範囲は「### UNCATALOGED 一覧」の見出しから、次の `## ` 見出しまでである
    (途中の `####` は一覧の小見出しであり、範囲を切らない)。
    """
    lines = doc_text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith(_UNCATALOGED_HEADING)), None)
    if start is None:
        return set()
    modules: set[str] = set()
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        found = _TOKEN_RE.findall(cells[0])
        if len(found) == 1 and found[0].endswith(".py"):
            modules.add(_normalize(found[0]))
    return modules


def list_modules(src_root: Path) -> set[str]:
    return {
        p.relative_to(src_root).as_posix()
        for p in src_root.rglob("*.py")
        if p.name != "__init__.py" and "__pycache__" not in p.parts
    }


def _exists(token: str, src_root: Path, repo_root: Path, *, is_dir: bool) -> bool:
    """src 配下、または repo root 相対(`infra/` `scripts/` 等の src 外の path)のどちらかに実在するか。"""
    for base in (src_root, repo_root):
        target = base / token
        if target.is_dir() if is_dir else target.is_file():
            return True
    return False


def check(
    doc_text: str, modules: set[str], src_root: Path, repo_root: Path | None = None
) -> Report:
    repo_root = repo_root if repo_root is not None else src_root.resolve().parent.parent
    files, dirs = parse_source_tokens(doc_text)
    uncataloged = parse_uncataloged(doc_text)
    report = Report(module_total=len(modules), uncataloged_listed=len(uncataloged))

    covered_by_file = {m for m in modules if m in files}
    covered_by_dir = {
        m for m in modules if m not in covered_by_file and any(m.startswith(d) for d in dirs)
    }
    covered = covered_by_file | covered_by_dir
    uncovered = modules - covered
    report.covered_by_file = len(covered_by_file)
    report.covered_by_dir = len(covered_by_dir)
    report.uncovered = len(uncovered)

    for m in sorted(uncovered - uncataloged):
        report.violations.append(
            Violation("C1/C3", m, "F 行 / S 行の主要 source にも UNCATALOGED 一覧にも載っていない")
        )
    for m in sorted(uncataloged & covered):
        report.violations.append(
            Violation(
                "C3-b",
                m,
                "UNCATALOGED 一覧に載っているが、既に F 行 / S 行の主要 source に覆われている(一覧から削る)",
            )
        )
    for m in sorted(uncataloged - modules):
        report.violations.append(
            Violation("C3-c", m, "UNCATALOGED 一覧に載っているが、src に実在しない")
        )
    for f in sorted(files):
        if not _exists(f, src_root, repo_root, is_dir=False):
            report.violations.append(
                Violation("C4", f, "主要 source に書かれたファイルが実在しない")
            )
    for d in sorted(dirs):
        if not _exists(d, src_root, repo_root, is_dir=True):
            report.violations.append(
                Violation("C4", d, "主要 source に書かれたディレクトリが実在しない")
            )
    return report


def render(report: Report) -> str:
    lines = [
        f"MODULE_TOTAL    = {report.module_total}(__init__.py を除く)",
        f"COVERED_BY_FILE = {report.covered_by_file}",
        f"COVERED_BY_DIR  = {report.covered_by_dir}",
        f"UNCATALOGED     = {report.uncovered}(F 行 / S 行に覆われない module の数)",
        f"UNCATALOGED_LISTED = {report.uncataloged_listed}(一覧に載っている行の数)",
        f"VIOLATIONS      = {len(report.violations)}",
    ]
    lines.extend(f"  {v.render()}" for v in report.violations)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--doc", default="docs/functional_domains.md")
    parser.add_argument("--src", default="src/jstock_advisor")
    args = parser.parse_args(argv)

    doc_path = Path(args.doc)
    src_root = Path(args.src)
    if not doc_path.is_file() or not src_root.is_dir():
        print(
            f"検査できなかった: 文書 {doc_path} または src {src_root} が見つからない",
            file=sys.stderr,
        )
        return EXIT_UNCHECKABLE
    doc_text = doc_path.read_text(encoding="utf-8")
    files, dirs = parse_source_tokens(doc_text)
    if not files and not dirs:
        # 表を 1 行も読めなかった場合に「違反なし」へ倒さない(fail-close)。
        print("検査できなかった: F 行 / S 行の主要 source を 1 件も読めなかった", file=sys.stderr)
        return EXIT_UNCHECKABLE
    modules = list_modules(src_root)
    if not modules:
        print(f"検査できなかった: {src_root} に module が 1 件も無い", file=sys.stderr)
        return EXIT_UNCHECKABLE

    report = check(doc_text, modules, src_root)
    print(render(report))
    return EXIT_OK if report.ok else EXIT_VIOLATION


if __name__ == "__main__":
    sys.exit(main())
