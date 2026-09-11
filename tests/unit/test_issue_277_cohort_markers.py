"""Issue #277: cohort marker が registry どおりに付いていることの guard。

## 何を検証するか

`tests/conftest.py` の `pytest_collection_modifyitems` は、registry
(`tests/support/time_semantics_registry.py`) を正本として cohort marker を
収集時に動的に付ける。テスト側へ `pytestmark` を手で書かせないため
「付け忘れ」は原理的に起きないが、代わりに**付与そのものが静かに壊れる**
経路が生まれた(conftest が読まれない、path の解決がずれる、marker 名の
規則が変わる、registry の import が失敗して空になる、など)。
いずれも「`-m cohort_...` が 0 件を返す」という形で現れ、
**テストは 1 件も失敗しないまま選択だけが効かなくなる**。本モジュールが
その経路を塞ぐ。

registry 自身の健全性(V1-V8 / O1-O6)は
`tests/unit/test_time_semantics_guard.py` が引き続き持つ。ここで見るのは
「registry の内容が pytest の marker として実際に観測できるか」だけである。

## なぜ子プロセスで pytest を起動するか

「marker が付いている」ことの最も直接的な観測は、実際に `pytest -m <marker>`
で選択させて選ばれた集合を見ることである。現在の実行の収集結果を覗く方法も
あるが、それだと本モジュールを単体で実行したときに検証対象が 1 件も収集されず、
**何も確かめないまま緑になる guard** になってしまう。子プロセスなら、
どう起動されても同じことを検証できる。

起動対象は registry の 7 モジュールに限定する(テスト全体は収集しない)。
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import tomllib
from functools import cache
from pathlib import Path

import pytest

from tests.support.time_semantics_registry import (
    _REGISTRY,
    _SOLO_PREFIX,
    cohort_marker_name,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_REGISTRY_MODULES = tuple(entry.module for entry in _REGISTRY)
_COHORTS = tuple(sorted({entry.cohort for entry in _REGISTRY}))


def _modules_of(cohort: str) -> frozenset[str]:
    return frozenset(entry.module for entry in _REGISTRY if entry.cohort == cohort)


@cache
def _collect(extra_args: tuple[str, ...] = ()) -> frozenset[str]:
    """registry のモジュールを収集させ、収集された module path の集合を返す。

    `--collect-only` なのでテスト本体は実行されない。`-p no:cacheprovider` は
    親の実行が持つ cache を子が書き換えないようにするためである。

    子の出力は pipe ではなく一時ファイルで受ける。`capture_output=True` は
    Windows では stdout/stderr ごとに読み取りスレッドを起こし、その生成時に
    faulthandler が "Windows fatal exception: access violation" を出力して
    画面を埋める(テスト自体は通る)。ファイルで受ければスレッドを使わない。

    同じ引数の起動は使い回す。子プロセスの pytest 起動は 1 回あたり十数秒
    かかるため、素直に呼ぶと同じ収集を何度も繰り返すことになる。
    収集は決定的なので、使い回しても検証の強さは変わらない。
    """
    command = [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        *extra_args,
        *_REGISTRY_MODULES,
    ]
    with tempfile.TemporaryDirectory() as work_dir:
        out_path = Path(work_dir) / "stdout.txt"
        err_path = Path(work_dir) / "stderr.txt"
        with out_path.open("wb") as out, err_path.open("wb") as err:
            returncode = subprocess.run(
                command,
                cwd=_REPO_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                timeout=600,
            ).returncode
        stdout = out_path.read_text(encoding="utf-8", errors="replace")
        stderr = err_path.read_text(encoding="utf-8", errors="replace")
    # exit code 5 は「1 件も収集されなかった」であり、ここでは異常ではなく
    # 「選択結果が空」である。集合の比較として報告したいので通す
    # (異常として弾くと、marker が付いていないときの失敗が
    #  「子プロセスが失敗した」に化けて読みにくい)。
    if returncode not in (0, 5):
        raise AssertionError(
            f"""収集に失敗した。
command={command}
returncode={returncode}
stdout=
{stdout}
stderr=
{stderr}"""
        )
    collected = set()
    for line in stdout.splitlines():
        line = line.strip()
        if "::" not in line:
            continue
        collected.add(line.split("::", 1)[0].replace("\\", "/"))
    return frozenset(collected)


def _selections() -> dict[str, frozenset[str]]:
    """cohort ごとの選択結果。子プロセスの起動は cohort 数だけで済ませる。

    受入条件 4(CI の実行時間が悪化しないこと)があるため、起動回数は必要最小限に
    する。フィルタ無しの収集を別途行っていた時期があるが、ある cohort marker で
    選択されたなら、そのモジュールは収集もされている。冗長なので畳んだ。
    """
    return {cohort: _collect(("-m", cohort_marker_name(cohort))) for cohort in _COHORTS}


@pytest.mark.parametrize("cohort", _COHORTS)
def test_cohort_marker_selects_exactly_its_registry_modules(cohort: str) -> None:
    """`-m <cohort marker>` が、その cohort の registry 登録モジュールと一致すること。

    ★ これが本モジュールの中心である。等号で確かめるので、
      付いていない(不足)と、余計に付いている(混入)の両方を検出する。

    `-m` を実際に通すことが重要である。marker が item に付いてさえいれば選べる、
    とは限らない。pytest 本体の deselect も収集フックであり、**自動付与がその後に
    走れば marker は付くのに選べない**。その順序まで含めて確かめられるのは
    実際に `-m` で起動したときだけである。
    """
    assert _selections()[cohort] == _modules_of(cohort)


def test_every_registry_module_is_selected_by_exactly_one_cohort_marker() -> None:
    """どのモジュールも、ちょうど 1 つの cohort marker で選択されること。

    cohort ごとの検証を全て通しても、registry に cohort が増えたときに
    「どの marker でも選ばれないモジュール」が残りうる。ここで塞ぐ。
    """
    selections = _selections()
    for module in _REGISTRY_MODULES:
        owners = sorted(cohort for cohort, modules in selections.items() if module in modules)
        assert len(owners) == 1, f"{module} を選択する cohort marker が {owners} 件ある"



def test_registered_markers_match_registry_cohorts() -> None:
    """pyproject の `markers` と registry の cohort が一致すること。

    pyproject へ登録が無い marker は `PytestUnknownMarkWarning` になるだけで、
    テストは通ってしまう(本リポジトリは `--strict-markers` を付けていない)。
    逆に、registry から消えた cohort の marker が pyproject に残ると、
    「`-m` で選べるが中身は空」という紛らわしい状態になる。両方向を見る。
    """
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {
        entry.split(":", 1)[0].strip()
        for entry in pyproject["tool"]["pytest"]["ini_options"]["markers"]
    }
    expected = {cohort_marker_name(cohort) for cohort in _COHORTS}
    assert {name for name in declared if name.startswith("cohort_")} == expected


def test_marker_name_is_derived_from_cohort() -> None:
    """marker 名の導出規則。`SOLO:` は marker 名に使えない文字を含む。"""
    assert cohort_marker_name("market_session") == "cohort_market_session"
    assert cohort_marker_name(f"{_SOLO_PREFIX}widget") == "cohort_solo_widget"
