"""check_catalog_coverage(Issue #480。#212 Phase C-1)の照合方式・fail-close・read-only の guard。

## 何を検証するか

`scripts/check_catalog_coverage.py` の危険な壊れ方は次のとおりである。

**1 覆われていないものを「覆われている」と読む(見逃し)。**

    basename だけで照合し、別ディレクトリの同名 module を覆われたと数える(#212 の実測で 16 件)
    散文(F 行 / S 行の外)に書かれた path を覆いと数える
    UNCATALOGED 一覧に載っているだけで、実は覆われている module を見逃す(一覧の陳腐化)

**2 覆われているものを「覆われていない」と読む(偽陽性の FAIL)。**

    `src/jstock_advisor/` で始まる表記を別物として扱う
    `infra/` `scripts/` のような src 外の path を「存在しない」と誤検出する

**3 「検査できなかった」を「違反なし」へ倒す(fail-open)。**
表を 1 行も読めない・文書が無い場合は exit 2。

**4 read-only でなくなること。**

## 何を検証しないか

影響領域の記載の正しさ(「網羅」ではなく「正しさ」)は、本 script が検査しない。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_catalog_coverage.py"
_spec = importlib.util.spec_from_file_location("check_catalog_coverage", _SCRIPT)
assert _spec is not None and _spec.loader is not None
ccc = importlib.util.module_from_spec(_spec)
sys.modules["check_catalog_coverage"] = ccc
_spec.loader.exec_module(ccc)

_F_HEADER = (
    "| ID | 機能 | 主要 source | 主要 config | 永続契約 | 影響領域 |\n|---|---|---|---|---|---|\n"
)
_S_HEADER = (
    "| SHARED_ID | 共通部品 | 主要 path | lock する領域 | 実測した主な参照元 |\n"
    "|---|---|---|---|---|\n"
)


def _doc(f_rows: str = "", s_rows: str = "", uncataloged: str = "", prose: str = "") -> str:
    """F 行 / S 行 / UNCATALOGED 一覧 / 散文を持つ最小の文書を組み立てる。"""
    parts = ["# functional_domains\n", prose, "\n## E-L. 機能一覧\n", _F_HEADER + f_rows]
    parts += ["\n## K. 共通部品\n", _S_HEADER + s_rows]
    parts += ["\n## M. 維持契約\n", "### UNCATALOGED 一覧(Issue #212 / baseline)\n"]
    if uncataloged:
        parts.append("\n| module | 割り当て予定 |\n|---|---|\n" + uncataloged)
    parts.append("\n## 変更履歴\n")
    return "\n".join(parts)


def _make_src(tmp_path: Path, modules: list[str]) -> Path:
    root = tmp_path / "src" / "jstock_advisor"
    for m in modules:
        target = root / m
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# module\n", encoding="utf-8")
    return root


def _conditions(report: object) -> list[tuple[str, str]]:
    return [(v.condition, v.path) for v in report.violations]  # type: ignore[attr-defined]


# --- 正常系 ---------------------------------------------------------------------------------


def test_clean_catalog_has_no_violation_and_counts_each_kind(tmp_path: Path) -> None:
    src = _make_src(tmp_path, ["a/one.py", "b/two.py", "c/three.py", "a/__init__.py"])
    doc = _doc(
        f_rows="| F-01 | 機能 | `a/one.py` | - | - | D1 |\n",
        s_rows="| S-01 | 部品 | `b/` | D1 | x |\n",
        uncataloged="| `c/three.py` | F-01 |\n",
    )
    modules = ccc.list_modules(src)
    report = ccc.check(doc, modules, src)
    assert report.ok, _conditions(report)
    assert modules == {"a/one.py", "b/two.py", "c/three.py"}  # __init__.py は数えない
    assert (report.module_total, report.covered_by_file, report.covered_by_dir) == (3, 1, 1)
    assert report.uncovered == 1
    assert report.uncataloged_listed == 1


# --- 見逃し(C1 / C3)の反証 -------------------------------------------------------------------


def test_new_module_in_no_row_and_no_list_is_reported(tmp_path: Path) -> None:
    src = _make_src(tmp_path, ["a/one.py", "a/new_module.py"])
    doc = _doc(f_rows="| F-01 | 機能 | `a/one.py` | - | - | D1 |\n")
    report = ccc.check(doc, ccc.list_modules(src), src)
    assert _conditions(report) == [("C1/C3", "a/new_module.py")]


def test_prose_path_does_not_cover_a_module(tmp_path: Path) -> None:
    """F 行 / S 行の外(散文)に書かれた path は、覆いと数えない。"""
    src = _make_src(tmp_path, ["a/one.py", "a/only_in_prose.py"])
    doc = _doc(
        f_rows="| F-01 | 機能 | `a/one.py` | - | - | D1 |\n",
        prose="説明の例: `a/only_in_prose.py` のように書く。\n",
    )
    report = ccc.check(doc, ccc.list_modules(src), src)
    assert _conditions(report) == [("C1/C3", "a/only_in_prose.py")]


def _naive_basename_check(doc_text: str, modules: set[str]) -> set[str]:
    """直す前の実装(#212 の basename 照合)。文書全文に basename が現れれば覆いと数える。"""
    return {m for m in modules if Path(m).name in doc_text}


def test_same_basename_in_another_directory_is_not_treated_as_covered(tmp_path: Path) -> None:
    src = _make_src(tmp_path, ["domain/entities/audit.py", "cli/audit.py"])
    doc = _doc(f_rows="| F-01 | 機能 | `cli/audit.py` | - | - | D9 |\n")
    modules = ccc.list_modules(src)

    # 直す前の実装(basename 照合)は、両方を覆いと数えて素通りさせる。テストが欠陥を捉えている証拠。
    assert _naive_basename_check(doc, modules) == modules

    report = ccc.check(doc, modules, src)
    assert _conditions(report) == [("C1/C3", "domain/entities/audit.py")]


# --- 一覧の陳腐化(C3-b / C3-c)-----------------------------------------------------------------


def test_listed_module_that_is_already_covered_is_reported_as_stale(tmp_path: Path) -> None:
    src = _make_src(tmp_path, ["a/one.py", "a/two.py"])
    doc = _doc(
        f_rows="| F-01 | 機能 | `a/one.py` `a/two.py` | - | - | D1 |\n",
        uncataloged="| `a/two.py` | F-01 |\n",
    )
    report = ccc.check(doc, ccc.list_modules(src), src)
    assert _conditions(report) == [("C3-b", "a/two.py")]


def test_listed_module_that_does_not_exist_is_reported(tmp_path: Path) -> None:
    src = _make_src(tmp_path, ["a/one.py"])
    doc = _doc(
        f_rows="| F-01 | 機能 | `a/one.py` | - | - | D1 |\n",
        uncataloged="| `a/removed.py` | F-01 |\n",
    )
    report = ccc.check(doc, ccc.list_modules(src), src)
    assert _conditions(report) == [("C3-c", "a/removed.py")]


def test_uncataloged_subheadings_do_not_cut_the_list_but_the_next_section_does() -> None:
    doc = (
        "### UNCATALOGED 一覧(x)\n\n#### `a/`  1 件\n\n"
        "| module | 予定 |\n|---|---|\n| `a/one.py` | F-01 |\n\n"
        "#### `b/`  1 件\n\n| module | 予定 |\n|---|---|\n| `b/two.py` | F-02 |\n\n"
        "## 変更履歴\n\n| `c/after.py` | F-03 |\n"
    )
    assert ccc.parse_uncataloged(doc) == {"a/one.py", "b/two.py"}


# --- 偽陽性(正規化・src 外の path)--------------------------------------------------------------


def test_src_prefix_is_normalized(tmp_path: Path) -> None:
    src = _make_src(tmp_path, ["services/audit_service.py"])
    doc = _doc(
        f_rows="| F-01 | 機能 | `src/jstock_advisor/services/audit_service.py` | - | - | D9 |\n"
    )
    report = ccc.check(doc, ccc.list_modules(src), src)
    assert report.ok, _conditions(report)
    assert report.covered_by_file == 1


def test_paths_outside_src_that_exist_at_repo_root_are_not_dead_references(tmp_path: Path) -> None:
    src = _make_src(tmp_path, ["a/one.py"])
    (tmp_path / "infra").mkdir()
    (tmp_path / "scripts").mkdir()
    doc = _doc(
        f_rows="| F-01 | 機能 | `a/one.py` `infra/` `scripts/` `config/x.yaml` | - | - | D9 |\n"
    )
    report = ccc.check(doc, ccc.list_modules(src), src)
    assert report.ok, _conditions(report)


def test_s_row_uses_the_path_column_not_the_reference_column(tmp_path: Path) -> None:
    src = _make_src(tmp_path, ["a/one.py", "z/mentioned_only_as_reference.py"])
    doc = _doc(
        f_rows="",
        s_rows="| S-01 | 部品 | `a/one.py` | D1 | `z/mentioned_only_as_reference.py` |\n",
    )
    report = ccc.check(doc, ccc.list_modules(src), src)
    assert _conditions(report) == [("C1/C3", "z/mentioned_only_as_reference.py")]


# --- 実在しない参照(C4)------------------------------------------------------------------------


def test_dead_file_and_dead_directory_references_are_reported(tmp_path: Path) -> None:
    src = _make_src(tmp_path, ["a/one.py"])
    doc = _doc(f_rows="| F-01 | 機能 | `a/one.py` `a/gone.py` `nowhere/` | - | - | D1 |\n")
    report = ccc.check(doc, ccc.list_modules(src), src)
    assert sorted(_conditions(report)) == [("C4", "a/gone.py"), ("C4", "nowhere/")]


# --- fail-close(exit code)---------------------------------------------------------------------


def _run(tmp_path: Path, doc_text: str | None, src: Path | None) -> int:
    doc_path = tmp_path / "functional_domains.md"
    if doc_text is not None:
        doc_path.write_text(doc_text, encoding="utf-8")
    src_path = src if src is not None else tmp_path / "missing_src"
    return int(ccc.main(["--doc", str(doc_path), "--src", str(src_path)]))


def test_exit_codes_distinguish_ok_violation_and_uncheckable(tmp_path: Path) -> None:
    src = _make_src(tmp_path, ["a/one.py"])
    ok_doc = _doc(f_rows="| F-01 | 機能 | `a/one.py` | - | - | D1 |\n")
    assert _run(tmp_path, ok_doc, src) == 0
    assert _run(tmp_path, _doc(), src) == 2  # 表を 1 行も読めない = 「違反なし」ではない
    (src / "a" / "two.py").write_text("# x\n", encoding="utf-8")
    assert _run(tmp_path, ok_doc, src) == 1  # 覆われていない module が増えた


def test_missing_document_or_src_is_uncheckable_not_ok(tmp_path: Path) -> None:
    src = _make_src(tmp_path, ["a/one.py"])
    assert _run(tmp_path / "nowhere", None, src) == 2  # 文書が無い
    assert (
        _run(tmp_path, _doc(f_rows="| F-01 | 機能 | `a/one.py` | - | - | D1 |\n"), None) == 2
    )  # src が無い


def test_empty_src_is_uncheckable(tmp_path: Path) -> None:
    empty = tmp_path / "src" / "jstock_advisor"
    empty.mkdir(parents=True)
    assert _run(tmp_path, _doc(f_rows="| F-01 | 機能 | `a/one.py` | - | - | D1 |\n"), empty) == 2


# --- read-only ----------------------------------------------------------------------------------


def test_run_does_not_modify_any_file(tmp_path: Path) -> None:
    src = _make_src(tmp_path, ["a/one.py", "a/two.py"])
    doc_text = _doc(f_rows="| F-01 | 機能 | `a/one.py` | - | - | D1 |\n")
    doc_path = tmp_path / "functional_domains.md"
    doc_path.write_text(doc_text, encoding="utf-8")
    before = {p: p.read_bytes() for p in [doc_path, *src.rglob("*.py")]}
    ccc.main(["--doc", str(doc_path), "--src", str(src)])
    assert {p: p.read_bytes() for p in before} == before
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(["functional_domains.md", "src"])


# --- 実際の文書の構造(列の位置の前提が崩れていないことの guard)--------------


def test_real_catalog_yields_source_tokens_from_both_row_kinds() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "functional_domains.md").read_text(
        encoding="utf-8"
    )
    files, dirs = ccc.parse_source_tokens(doc)
    assert len(files) > 100  # F 行の主要 source 列(2026-09 時点で 140 件超)
    assert "domain/valuation/" in dirs  # S-05 の主要 path 列(S 行)
    assert all(not f.startswith("src/") for f in files)  # 接頭辞は正規化されている
    assert len(ccc.parse_uncataloged(doc)) > 0 or "UNCATALOGED" in doc


@pytest.mark.parametrize("token", ["config/holiday_calendar.json", "schedule.yaml"])
def test_non_python_tokens_are_ignored(token: str) -> None:
    doc = _doc(f_rows=f"| F-01 | 機能 | `{token}` | - | - | D1 |\n")
    assert ccc.parse_source_tokens(doc) == (set(), set())
