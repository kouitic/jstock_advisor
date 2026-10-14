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

起動対象は registry のモジュールに限定する(テスト全体は収集しない)。

## 子プロセスは 3 回だけ(Issue #966。以前は cohort ごとの 20 回)

cohort ごとに `-m <marker>` で 1 回ずつ起動すると、起動の回数が registry の cohort 数に比例し、
最初の 1 件が全部の起動を払う(CI で約 140 秒 = pytest 全体の約 11%)。
検証の強さを落とさずに次の 3 回へ畳んだ。

  子 1  全 cohort の marker の OR(`-m "a or b or ..."`)で 1 回収集し、
        plugin(`tests/support/cohort_marker_dump.py`)が `pytest_collection_finish` で書いた
        『module -> 付いている cohort marker の集合』から、20 cohort 全部を等号で比べる。
        ★ 実際の `-m` を通す(自動付与が pytest 本体の deselect より先に走る順序まで確かめられる)★
        ★ OR で選ばれなかった module(どの marker も付かない)は和集合の等号で落ち、
          別の cohort の marker が付いた module や二重に付いた module は、
          module ごとの集合の等号で落ちる ★
  子 2  代表の通常 cohort を `-m <単一の marker>` で選ぶ
  子 3  代表の SOLO cohort を `-m <単一の marker>` で選ぶ
        (`SOLO:` の接頭辞の導出が `-m` の式に入ること)

全 cohort の marker 名が `-m` の式として成立することは、子プロセスを増やさず
`Expression.compile` で静的に確かめる。本モジュールが検証するのは『marker が正しく付き、
`-m` で選べること』であり、cohort の実行(同一プロセスでの共有状態の干渉)ではない。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import pytest
from _pytest.mark.expression import Expression

from tests.support.time_semantics_registry import (
    _REGISTRY,
    _SOLO_PREFIX,
    cohort_marker_name,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_REGISTRY_MODULES = tuple(entry.module for entry in _REGISTRY)
_COHORTS = tuple(sorted({entry.cohort for entry in _REGISTRY}))
_DUMP_PLUGIN = "tests.support.cohort_marker_dump"

#: 実際に起動した子プロセスの (extra_args, with_dump)(`_collect` が起動のたびに記録する)。
#: 回数だけでなく「何を起動したか」を持ち、`_launch_plan()` との突き合わせに使う
_launched: list[tuple[tuple[str, ...], bool]] = []


def _modules_of(cohort: str) -> frozenset[str]:
    return frozenset(entry.module for entry in _REGISTRY if entry.cohort == cohort)


@dataclass(frozen=True)
class _Collected:
    """子プロセスの収集の結果。

    modules  標準出力から読んだ、収集された module path の集合
    markers  plugin の出力(module -> 付いている cohort marker の集合)。dump を頼んでいなければ空
    """

    modules: frozenset[str]
    markers: dict[str, frozenset[str]] = field(default_factory=dict)


@cache
def _collect(extra_args: tuple[str, ...], with_dump: bool) -> _Collected:
    """registry のモジュールを収集させ、収集された module path の集合(と marker の集合)を返す。

    `--collect-only` なのでテスト本体は実行されない。`-p no:cacheprovider` は
    親の実行が持つ cache を子が書き換えないようにするためである。

    子の出力は pipe ではなく一時ファイルで受ける。`capture_output=True` は
    Windows では stdout/stderr ごとに読み取りスレッドを起こし、その生成時に
    faulthandler が "Windows fatal exception: access violation" を出力して
    画面を埋める(テスト自体は通る)。ファイルで受ければスレッドを使わない。

    同じ引数の起動は使い回す(★ 呼び出しは常に位置引数 2 つ ★: functools.cache は引数の渡し方が違うと
    別の key にするため、同じ起動が重複して数えられる)。子プロセスの pytest 起動は 1 回あたり十数秒
    かかるため、素直に呼ぶと同じ収集を何度も繰り返すことになる。
    収集は決定的なので、使い回しても検証の強さは変わらない。

    `with_dump=True` のとき、子へ
    `-p tests.support.cohort_marker_dump --cohort-dump <path>` を渡す。
    plugin の出力が読めない・壊れているときは AssertionError(★ 空のまま緑にしない ★)。
    """
    with tempfile.TemporaryDirectory() as work_dir:
        out_path = Path(work_dir) / "stdout.txt"
        err_path = Path(work_dir) / "stderr.txt"
        dump_path = Path(work_dir) / "markers.json"
        plugin_args = ("-p", _DUMP_PLUGIN, "--cohort-dump", str(dump_path)) if with_dump else ()
        command = [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            *plugin_args,
            *extra_args,
            *_REGISTRY_MODULES,
        ]
        _launched.append((extra_args, with_dump))
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
        markers: dict[str, frozenset[str]] = {}
        if with_dump:
            try:
                raw = json.loads(dump_path.read_text(encoding="utf-8"))
                markers = {str(module): frozenset(names) for module, names in raw.items()}
            except (OSError, ValueError, AttributeError, TypeError) as error:
                raise AssertionError(
                    f"plugin の出力を読めない({type(error).__name__})。空のまま緑にしない\n"
                    f"command={command}\nstderr=\n{stderr}"
                ) from error
    collected = set()
    for line in stdout.splitlines():
        line = line.strip()
        if "::" not in line:
            continue
        collected.add(line.split("::", 1)[0].replace("\\", "/"))
    return _Collected(frozenset(collected), markers)


def _union_expression() -> str:
    """全 cohort の marker の OR(子 1 の `-m` の式)。"""
    return " or ".join(cohort_marker_name(cohort) for cohort in _COHORTS)


def _representative(*, solo: bool) -> str | None:
    """代表の cohort(registry の cohort を昇順に並べた先頭。SOLO か通常か)。無ければ None。"""
    for cohort in _COHORTS:
        if cohort.startswith(_SOLO_PREFIX) == solo:
            return cohort
    return None


def _launch_plan() -> tuple[tuple[tuple[str, ...], bool], ...]:
    """子プロセスの起動の計画(extra_args, with_dump)。★ 起動の回数は cohort の数に依存しない ★"""
    plan: list[tuple[tuple[str, ...], bool]] = [(("-m", _union_expression()), True)]
    for solo in (False, True):
        cohort = _representative(solo=solo)
        if cohort is not None:
            plan.append((("-m", cohort_marker_name(cohort)), False))
    return tuple(plan)


def _union() -> _Collected:
    """子 1(OR で 1 回収集 + marker の dump)。"""
    extra_args, with_dump = _launch_plan()[0]
    return _collect(extra_args, with_dump)


def _planned_single(cohort: str) -> tuple[tuple[str, ...], bool]:
    """計画の中の、その cohort の単一 marker の `-m` の起動(★ 起動の出所は計画だけ ★)。"""
    wanted = ("-m", cohort_marker_name(cohort))
    matches = [entry for entry in _launch_plan() if entry[0] == wanted]
    assert len(matches) == 1, f"{cohort} の単一 marker の起動が計画に {len(matches)} 件(期待 1)"
    return matches[0]


def _owners_of(marker: str) -> frozenset[str]:
    """子 1 の dump で、その marker が付いている module の集合。"""
    return frozenset(module for module, names in _union().markers.items() if marker in names)


@pytest.mark.parametrize("cohort", _COHORTS)
def test_cohort_marker_selects_exactly_its_registry_modules(cohort: str) -> None:
    """その cohort の marker が付いている module が、registry の登録モジュールと一致すること。

    ★ これが本モジュールの中心である。等号で確かめるので、
      付いていない(不足)と、余計に付いている(混入)の両方を検出する。

    観測は子 1(全 cohort の marker の OR で実際に `-m` を通した収集)の結果である。
    marker が item に付いてさえいれば選べる、とは限らない。
    pytest 本体の deselect も収集フックであり、
    **自動付与がその後に走れば marker は付くのに選べない**。その順序まで含めて確かめられるのは
    実際に `-m` で起動したときだけである(その場合は何も選ばれず、等号で落ちる)。
    """
    assert _owners_of(cohort_marker_name(cohort)) == _modules_of(cohort)


def test_the_union_of_all_cohort_markers_selects_every_registry_module() -> None:
    """全 cohort の marker の OR で、registry の全 module が(過不足なく)選ばれること。

    どの marker も付かない module は選ばれず、ここで落ちる。
    """
    assert _union().modules == frozenset(_REGISTRY_MODULES)


def test_every_registry_module_is_selected_by_exactly_one_cohort_marker() -> None:
    """どのモジュールも、ちょうど 1 つの cohort marker で選択されること。

    cohort ごとの検証を全て通しても、registry に cohort が増えたときに
    「どの marker でも選ばれないモジュール」が残りうる。二重に付いたモジュールも、
    module ごとの marker の集合が『自分の cohort の marker 1 つ』と一致しないので落ちる。
    """
    markers = _union().markers
    expected = {entry.module: frozenset({cohort_marker_name(entry.cohort)}) for entry in _REGISTRY}
    for module in _REGISTRY_MODULES:
        owners = sorted(markers.get(module, frozenset()))
        assert markers.get(module) == expected[module], (
            f"{module} を選択する cohort marker が {owners}(期待 {sorted(expected[module])})"
        )


@pytest.mark.parametrize("solo", [False, True], ids=["normal", "solo"])
def test_a_single_cohort_marker_selects_through_dash_m(solo: bool) -> None:
    """代表の cohort(通常 / SOLO)を単一の marker の `-m` で選べること(子 2・子 3)。

    単一の marker の `-m` が実際に効くこと、`SOLO:` の接頭辞の marker 名の導出が `-m` の式に
    入ることを、実際に起動して確かめる。
    """
    cohort = _representative(solo=solo)
    assert cohort is not None, "registry に代表にできる cohort が無い"
    extra_args, with_dump = _planned_single(cohort)
    assert _collect(extra_args, with_dump).modules == _modules_of(cohort)


def test_every_cohort_marker_name_is_a_valid_dash_m_expression() -> None:
    """全 cohort の marker 名が安全な形で、`-m` の式として成立すること(静的。子プロセスなし)。

    cohort ごとの `-m` の起動を畳んだことで、『ある marker 名だけが `-m` を壊す形
    (予期しない文字・予約語・空白など)』を子プロセスでは捕らえなくなる。
    その検出力をここで埋める。
    """
    for cohort in _COHORTS:
        name = cohort_marker_name(cohort)
        assert re.fullmatch(r"cohort_(solo_)?[a-z][a-z0-9_]*", name), name
        assert name.startswith("cohort_solo_") == cohort.startswith(_SOLO_PREFIX), name
        expression = Expression.compile(name)
        assert expression.evaluate(lambda candidate, n=name: candidate == n)
        assert not expression.evaluate(lambda candidate: False)
    # 子 1 の式(OR)も、各 marker だけが真のとき真・どれも真でないとき偽になる
    union = Expression.compile(_union_expression())
    for cohort in _COHORTS:
        name = cohort_marker_name(cohort)
        assert union.evaluate(lambda candidate, n=name: candidate == n)
    assert not union.evaluate(lambda candidate: False)


def test_the_number_of_child_processes_does_not_depend_on_the_number_of_cohorts() -> None:
    """子プロセスの起動は(代表の通常・代表の SOLO・OR の)3 回以内で、cohort の数に比例しない。

    以前は cohort の数(20)だけ起動していた(#966)。起動の回数が cohort の数に戻ると、ここで落ちる。
    ★ 実際に起動した引数の組を記録し、計画(`_launch_plan()`)と一致することを確かめる ★
    (回数だけでは『計画が起動を記述している』ことを守れない)。
    """
    plan = _launch_plan()
    assert 1 <= len(plan) <= 3
    assert len(_COHORTS) > len(plan)  # 計画が cohort ごとの起動になっていない
    # このテストが単独で走っても、計画どおり全部起動する(同じ引数は使い回され、再起動しない)
    for extra_args, with_dump in plan:
        _collect(extra_args, with_dump)
    # 実際の起動 = 計画(順序は問わない。重複も計画外の起動も無い)
    assert sorted(_launched) == sorted(plan)
    assert len(_launched) <= 3


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
