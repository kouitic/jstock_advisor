"""scripts/check_lock_consistency.py(Issue #244。O-1)の照合方式・fail-close・CI の置き場の guard。

## 何を検証するか

`scripts/check_lock_consistency.py` の危険な壊れ方は次のとおりである。

**1 欠けている package を「ある」と読む(見逃し)。**

    比較の向きが逆(lock の全 package が Layer にあるか、を見てしまう。lock は CLI・開発ツール
    を含むため常に多く、検査が何も落とさなくなる)
    名前の正規化をしない / 正規化しすぎる(``Foo_Bar`` と ``foo-bar`` を別物と読んで偽陽性、
    または別 package を同一視)
    environment marker 付きの行(CI の Linux に入る保証がない)を存在と数える
    読めない行を無視する(行頭に `-e` や範囲指定があっても気づかない)

**2 「検査できなかった」を「違反なし」へ倒す(fail-open)。**
ファイルが無い・UTF-8 として読めない・Layer 側を 1 件も読めない場合は exit 2。

**3 失敗にしてはいけないものを失敗にする(偽陽性)。**
版が違うだけの package(O-2 の対象。本 Issue の範囲外)、コメントと空行、lock だけにある package。

**4 read-only でなくなること。**

**5 CI の置き場が崩れること。** layer-lock-drift job に本 script の step があり、
job 名の集合・required check は不変。

## 何を検証しないか

版の一致(O-2)。pyproject の extra。requirements-lock.txt の再生成手順。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_lock_consistency.py"
_CI_YML = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_spec = importlib.util.spec_from_file_location("check_lock_consistency", _SCRIPT)
assert _spec is not None and _spec.loader is not None
clc = importlib.util.module_from_spec(_spec)
sys.modules["check_lock_consistency"] = clc
_spec.loader.exec_module(clc)


def _kinds(layer: str, lock: str) -> list[tuple[str, str]]:
    return [(v.kind, v.detail.split(":")[0]) for v in clc.find_violations(layer, lock)]


# --- 存在の検査(受入条件の核) -----------------------------------------------------------


def test_every_layer_package_present_in_lock_is_ok() -> None:
    layer = "a==1.0\nb==2.0\n"
    lock = "a==1.0\nb==2.0\nc==3.0\n"
    assert clc.find_violations(layer, lock) == []


def test_missing_package_is_reported_with_its_layer_version() -> None:
    violations = clc.find_violations("a==1.0\nopenpyxl==3.1.5\n", "a==1.0\n")
    assert [(v.kind, v.detail) for v in violations] == [
        ("MISSING", "openpyxl==3.1.5: requirements-lock.txt に無い")
    ]


def test_direction_is_layer_subset_of_lock_not_the_reverse() -> None:
    # lock にだけある package(CLI・開発ツール)は違反ではない。逆向きに検査すると、これが落ちる
    assert clc.find_violations("a==1.0\n", "a==1.0\nruff==0.1\npytest==8.0\n") == []
    # Layer にだけある package は違反
    assert _kinds("a==1.0\nonly-layer==1.0\n", "a==1.0\nruff==0.1\n") == [
        ("MISSING", "only-layer==1.0")
    ]


def test_direct_and_transitive_packages_are_both_checked() -> None:
    # openpyxl(direct)と et-xmlfile(推移的)のどちらを抜いても落ちる
    layer = "et-xmlfile==2.0.0\nopenpyxl==3.1.5\n"
    assert _kinds(layer, "openpyxl==3.1.5\n") == [("MISSING", "et-xmlfile==2.0.0")]
    assert _kinds(layer, "et-xmlfile==2.0.0\n") == [("MISSING", "openpyxl==3.1.5")]


def test_each_missing_package_is_reported_once_and_in_layer_order() -> None:
    layer = "zeta==1\nalpha==1\nzeta==1\n"
    assert _kinds(layer, "") == [("MISSING", "zeta==1"), ("MISSING", "alpha==1")]


# --- 名前の正規化(PEP 503) --------------------------------------------------------------


@pytest.mark.parametrize(
    ("layer_name", "lock_name"),
    [
        ("Foo_Bar", "foo-bar"),
        ("foo-bar", "Foo_Bar"),
        ("foo.bar", "foo-bar"),
        ("foo__bar", "foo-bar"),
        ("zope.interface", "zope-interface"),
        ("PyYAML", "pyyaml"),
    ],
)
def test_names_are_normalized_before_comparison(layer_name: str, lock_name: str) -> None:
    assert clc.find_violations(f"{layer_name}==1.0\n", f"{lock_name}==1.0\n") == []


def test_normalization_does_not_merge_different_packages() -> None:
    # 区切りを除去して同一視する実装だと foobar と foo-bar が一致してしまう
    assert _kinds("foo-bar==1.0\n", "foobar==1.0\n") == [("MISSING", "foo-bar==1.0")]


def test_extras_are_ignored_for_the_package_name() -> None:
    assert clc.find_violations("pkg[extra]==1.0\n", "pkg==1.0\n") == []
    assert clc.find_violations("pkg==1.0\n", "pkg[extra,more]==1.0\n") == []


# --- environment marker(保守的な向き) ---------------------------------------------------


def test_marker_only_lock_line_does_not_count_as_present() -> None:
    lock = 'pywin32==312; sys_platform == "win32"\n'
    violations = clc.find_violations("pywin32==312\n", lock)
    assert [v.kind for v in violations] == ["MARKER_ONLY"]
    assert "pywin32==312" in violations[0].detail


def test_unmarked_line_alongside_a_marked_one_counts_as_present() -> None:
    lock = 'pywin32==312; sys_platform == "win32"\npywin32==312\n'
    assert clc.find_violations("pywin32==312\n", lock) == []


def test_marker_on_a_layer_line_does_not_hide_a_missing_package() -> None:
    assert _kinds('a==1.0; python_version >= "3.12"\n', "") == [("MISSING", "a==1.0")]


def test_empty_marker_is_still_treated_as_a_marker() -> None:
    assert [v.kind for v in clc.find_violations("a==1.0\n", "a==1.0;\n")] == ["MARKER_ONLY"]


# --- 読めない行は無視しない(fail-safe) ----------------------------------------------------


@pytest.mark.parametrize(
    "bad_line",
    [
        "-e git+https://example.invalid/x.git#egg=x",
        "-r other.txt",
        "--hash=sha256:abc",
        "pkg>=1.0",
        "pkg==1.0,<2",
        "pkg",
        "pkg===1.0",
        "https://example.invalid/pkg.whl",
        "==1.0",
        "pkg==",
        "pkg==1.0#glued-comment",
    ],
)
def test_unparsable_line_in_lock_is_a_violation(bad_line: str) -> None:
    violations = clc.find_violations("a==1.0\n", f"a==1.0\n{bad_line}\n")
    assert [v.kind for v in violations] == ["UNPARSABLE"]
    assert "requirements-lock.txt:2" in violations[0].detail


def test_unparsable_line_in_layer_is_a_violation_and_names_the_layer_file() -> None:
    violations = clc.find_violations("a==1.0\npkg>=1.0\n", "a==1.0\npkg==1.0\n")
    assert [v.kind for v in violations] == ["UNPARSABLE"]
    assert "infra/layer/requirements.txt:2" in violations[0].detail


@pytest.mark.parametrize(
    "ok_line",
    [
        "",
        "   ",
        "# a comment",
        "   # an indented comment",
        "a==1.0  # trailing comment",
        "a==1.0\t# tab then comment",
        'a==1.0; sys_platform == "win32"  # marker then comment',
        "A_B==1.0.post1",
        "a==2026.7.22",
    ],
)
def test_comments_blank_lines_and_trailing_comments_are_not_violations(ok_line: str) -> None:
    layer = "a==1.0\n"
    lock = f"{ok_line}\na==1.0\n"
    assert not [v for v in clc.find_violations(layer, lock) if v.kind == "UNPARSABLE"]


def test_crlf_line_endings_are_read_like_lf() -> None:
    assert clc.find_violations("a==1.0\r\nb==2.0\r\n", "a==1.0\r\nb==2.0\r\n") == []


# --- 版は見ない(O-2 は範囲外)。件数だけ情報として数える -------------------------------------


def test_different_versions_are_not_a_violation() -> None:
    assert clc.find_violations("a==2.0\nb==2.0\n", "a==1.0\nb==1.0\n") == []


def test_version_difference_count_counts_only_packages_present_in_both() -> None:
    layer = "a==2.0\nb==2.0\nc==2.0\nonly-layer==1\n"
    lock = "a==1.0\nb==2.0\nc==1.0\nonly-lock==1\n"
    assert clc.count_version_differences(layer, lock) == 2


def test_version_difference_count_ignores_marked_lock_lines() -> None:
    assert clc.count_version_differences("a==2.0\n", 'a==1.0; sys_platform == "win32"\n') == 0


def test_version_difference_count_is_zero_when_one_of_the_lock_lines_matches() -> None:
    assert clc.count_version_differences("a==2.0\n", "a==1.0\na==2.0\n") == 0


# --- main(): exit code・出力・fail-close・read-only --------------------------------------


def _write(tmp_path: Path, layer: str, lock: str) -> list[str]:
    layer_path = tmp_path / "layer.txt"
    lock_path = tmp_path / "lock.txt"
    layer_path.write_text(layer, encoding="utf-8", newline="")
    lock_path.write_text(lock, encoding="utf-8", newline="")
    return ["--layer", str(layer_path), "--lock", str(lock_path)]


def test_main_exit_0_and_ok_line_when_consistent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = clc.main(_write(tmp_path, "a==1.0\n", "a==1.0\nb==1\n"))
    out = capsys.readouterr().out
    assert code == 0
    assert "LOCK_CONSISTENCY: OK" in out
    assert "INFO: 版が違う package = 0 件" in out


def test_main_exit_1_names_the_missing_package_and_how_to_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = clc.main(_write(tmp_path, "a==1.0\nopenpyxl==3.1.5\n", "a==1.0\n"))
    out = capsys.readouterr().out
    assert code == 1
    assert "openpyxl==3.1.5" in out
    assert "[MISSING]" in out
    assert "requirements-lock.txt に同じ package を追加する" in out
    assert "pywin32" in out


def test_main_prints_the_version_difference_count_even_on_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = clc.main(_write(tmp_path, "a==2.0\nb==1.0\nmissing==1\n", "a==1.0\nb==1.0\n"))
    out = capsys.readouterr().out
    assert code == 1
    assert "INFO: 版が違う package = 1 件" in out


def test_main_exit_2_when_a_file_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _write(tmp_path, "a==1.0\n", "a==1.0\n")
    (tmp_path / "lock.txt").unlink()
    assert clc.main(args) == 2
    assert "検査できなかった" in capsys.readouterr().err
    args = _write(tmp_path, "a==1.0\n", "a==1.0\n")
    (tmp_path / "layer.txt").unlink()
    assert clc.main(args) == 2


def test_main_exit_2_when_a_file_is_not_utf8(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _write(tmp_path, "a==1.0\n", "a==1.0\n")
    (tmp_path / "lock.txt").write_bytes(b"a==1.0\n# \xff\xfe\n")
    assert clc.main(args) == 2
    assert "UTF-8" in capsys.readouterr().err


def test_main_exit_2_when_the_layer_file_has_no_package(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Layer 側を 1 件も読めないときに「違反なし」へ倒さない
    assert clc.main(_write(tmp_path, "# only comments\n\n", "a==1.0\n")) == 2
    assert "1 件も読めなかった" in capsys.readouterr().err


def test_main_does_not_modify_the_input_files(tmp_path: Path) -> None:
    args = _write(tmp_path, "a==1.0\nmissing==1\n", "a==1.0\n")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    clc.main(args)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


# --- 受入条件の実測: 実ファイルで『openpyxl を lock から抜くと job が落ちる』 -------------------


# 子 process の標準出力を UTF-8 に固定する
# (Windows のコンソール既定の cp932 だと日本語の出力を読めない)。
_CHILD_ENV = {**os.environ, "PYTHONUTF8": "1"}


def _run_script(layer: Path, lock: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--layer", str(layer), "--lock", str(lock)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_CHILD_ENV,
        check=False,
    )


@pytest.fixture
def real_copies(tmp_path: Path) -> tuple[Path, Path]:
    layer = tmp_path / "layer.txt"
    lock = tmp_path / "lock.txt"
    shutil.copyfile(_REPO_ROOT / "infra" / "layer" / "requirements.txt", layer)
    shutil.copyfile(_REPO_ROOT / "requirements-lock.txt", lock)
    return layer, lock


def test_real_files_are_currently_consistent(real_copies: tuple[Path, Path]) -> None:
    result = _run_script(*real_copies)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "LOCK_CONSISTENCY: OK" in result.stdout


@pytest.mark.parametrize("package", ["openpyxl", "et-xmlfile"])
def test_removing_a_package_from_the_real_lock_fails_the_script_and_names_it(
    real_copies: tuple[Path, Path], package: str
) -> None:
    layer, lock = real_copies
    lines = lock.read_text(encoding="utf-8").splitlines(keepends=True)
    # 実際の lock は et_xmlfile(アンダースコア)、Layer は et-xmlfile(ハイフン)と表記が違う
    # (正規化が必要な実例)。行頭の名前を正規化して対象の行を探す
    kept = [ln for ln in lines if clc.normalize_name(ln.split("==")[0]) != package]
    assert len(kept) == len(lines) - 1, "lock に対象の行がちょうど 1 行ある前提"
    lock.write_text("".join(kept), encoding="utf-8", newline="")
    result = _run_script(layer, lock)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{package}==" in result.stdout, result.stdout


def test_removing_a_package_from_the_real_layer_does_not_fail(
    real_copies: tuple[Path, Path],
) -> None:
    # Layer から外す(包含される側が減る)のは違反ではない。向きの固定
    layer, lock = real_copies
    lines = layer.read_text(encoding="utf-8").splitlines(keepends=True)
    layer.write_text(
        "".join(ln for ln in lines if not ln.startswith("openpyxl==")), encoding="utf-8", newline=""
    )
    assert _run_script(layer, lock).returncode == 0


def test_the_script_runs_from_the_repo_root_with_default_paths() -> None:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=_REPO_ROOT,
        env=_CHILD_ENV,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- CI の置き場: layer-lock-drift job に step がある。job 名・required check は不変 ---------


def _ci() -> dict[str, object]:
    loaded = yaml.safe_load(_CI_YML.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _drift_steps() -> list[dict[str, object]]:
    jobs = _ci()["jobs"]
    assert isinstance(jobs, dict)
    steps = jobs["layer-lock-drift"]["steps"]
    assert isinstance(steps, list)
    return steps


def test_layer_lock_drift_job_runs_the_consistency_script() -> None:
    runs = [str(s.get("run", "")) for s in _drift_steps()]
    assert "python scripts/check_lock_consistency.py" in runs


def test_the_consistency_step_has_a_name_that_distinguishes_it_from_the_drift_check() -> None:
    step = next(s for s in _drift_steps() if "check_lock_consistency" in str(s.get("run", "")))
    assert "name" in step
    # 失敗したとき、直前の「Layer の lock が .in と一致するか」の step と見分けられること
    assert "lock" in str(step.get("name", ""))


def test_the_existing_drift_steps_are_unchanged_and_come_first() -> None:
    runs = [str(s.get("run", "")) for s in _drift_steps()]
    drift_index = runs.index("git diff --exit-code -- infra/layer/requirements.txt")
    script_index = runs.index("python scripts/check_lock_consistency.py")
    assert drift_index < script_index
    assert any(r.startswith("python -m uv pip compile infra/layer/requirements.in") for r in runs)
    assert "pip install -r infra/layer/build-requirements.txt" in runs


def test_the_consistency_step_is_not_conditional_and_does_not_continue_on_error() -> None:
    step = next(s for s in _drift_steps() if "check_lock_consistency" in str(s.get("run", "")))
    assert "if" not in step
    assert "continue-on-error" not in step


def test_no_new_job_is_added_and_the_drift_job_stays_unconditional() -> None:
    jobs = _ci()["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {
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
    assert "if" not in jobs["layer-lock-drift"]
    assert "name" not in jobs["layer-lock-drift"]
