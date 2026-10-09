"""CI の依存 lock が Lambda Layer の lock を包含するか検査する script(Issue #244。O-1)。

    python scripts/check_lock_consistency.py
    python scripts/check_lock_consistency.py --layer LAYER_FILE --lock LOCK_FILE
    (既定: LAYER_FILE = infra/layer/requirements.txt / LOCK_FILE = requirements-lock.txt)

## 何を解決するか

CI は ``requirements-lock.txt`` から依存を入れて、lint / typecheck / test / dependency-audit を
走らせる。
一方、Production の Lambda に載るのは ``infra/layer/requirements.txt``(uv の生成物)である。
Layer に依存を足したのに ``requirements-lock.txt`` へ足し忘れると、CI にその依存が入らず、
import できない・検査されないままの状態で CI が緑になる(起票時に実例がある)。
既存の ``layer-lock-drift`` job は「``.in`` から再生成した結果と Layer の lock の一致」だけを
見ており、この「CI の lock が Layer の lock を包含しているか」は誰も検査していなかった。

## 何を検査するか(O-1 = 存在のみ)

``infra/layer/requirements.txt`` の全 package が、``requirements-lock.txt`` にも存在すること。

- 比較するのは **名前だけ**。版は見ない(版の一致は O-2 で、本 Issue の範囲外)。
- 名前は PEP 503 で正規化して比較する(小文字化し、``-`` ``_`` ``.`` の連続を ``-`` 1 つにする)。
  extras 指定(``pkg[extra]==x``)は package 名だけを見る。
- ``requirements-lock.txt`` の行に environment marker(``; sys_platform == "win32"`` など)が
  ある package は、**CI の Linux に入る保証がない** ので、存在とは数えない。Layer 側の package が
  marker 付きの行でしか lock に無ければ違反(保守的な向き)。
- 読めない行は無視しない(``name==version`` でも、コメント・空行でもない行)。
  **読めないものを OK にしない**。
  どちらのファイルの行でも違反として報告する。
- 逆向き(lock にあって Layer に無い)は検査しない。lock は CLI・開発ツールを含むため多い
  (それが正常)。

付随の情報行(失敗にしない): 版が違う package の件数。O-2 の要否の判断材料として CI のログに残す。

## 違反の直し方

``requirements-lock.txt`` に **同じ package を追加する**(Layer 側から外さない)。
Windows の venv で ``pip freeze`` し直すと、``pywin32`` の marker が落ちる点に注意する
(``requirements-lock.txt`` の冒頭の注記を参照)。

## read-only であること

ファイルを読むだけで、何も書き換えない。標準ライブラリのみを使う(job の依存の導入に左右されない)。

## exit code

    0  違反なし
    1  違反あり(違反の一覧を標準出力へ出す)
    2  検査できなかった(ファイルが無い、UTF-8 として読めない)。「違反なし」とは読まない
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNCHECKABLE = 2

_DEFAULT_LAYER = "infra/layer/requirements.txt"
_DEFAULT_LOCK = "requirements-lock.txt"

# name==version(任意で extras・environment marker・行末コメント)。
# pip freeze と uv の生成物はこの形だけ。
# -e / -r / URL / --hash などのオプション行・範囲指定(>=)・複数指定は、読めない行として扱う。
_PACKAGE_RE = re.compile(
    r"""^
    (?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)
    (?:\[[A-Za-z0-9._,\s-]*\])?
    ==(?P<version>[^\s;#=<>!~,]+)
    \s*(?:;(?P<marker>[^#]*?))?
    \s*(?:\s\#.*)?
    $""",
    re.VERBOSE,
)


@dataclass(frozen=True)
class Requirement:
    name: str  # PEP 503 で正規化した名前
    version: str
    has_marker: bool


@dataclass(frozen=True)
class Violation:
    kind: str  # MISSING / MARKER_ONLY / UNPARSABLE
    detail: str


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(text: str, label: str) -> tuple[list[Requirement], list[Violation]]:
    """行ごとに読む。読めない行は無視せず Violation(UNPARSABLE)にする。"""
    requirements: list[Requirement] = []
    problems: list[Violation] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PACKAGE_RE.match(line)
        if match is None:
            problems.append(
                Violation(
                    "UNPARSABLE",
                    f"{label}:{line_no}: 読めない行(name==version の形ではない): {line}",
                )
            )
            continue
        requirements.append(
            Requirement(
                name=normalize_name(match.group("name")),
                version=match.group("version"),
                # ';' があれば(中身が空でも)marker 付きとして扱う。保守的な向き。
                has_marker=match.group("marker") is not None,
            )
        )
    return requirements, problems


def find_violations(layer_text: str, lock_text: str) -> list[Violation]:
    """Layer の全 package が lock に存在する(marker なしの行がある)ことを検査する。"""
    layer, layer_problems = parse_requirements(layer_text, "infra/layer/requirements.txt")
    lock, lock_problems = parse_requirements(lock_text, "requirements-lock.txt")
    violations = [*layer_problems, *lock_problems]

    lock_unconditional = {r.name for r in lock if not r.has_marker}
    lock_conditional = {r.name for r in lock if r.has_marker}
    reported: set[str] = set()
    for req in layer:
        if req.name in reported or req.name in lock_unconditional:
            continue
        reported.add(req.name)
        if req.name in lock_conditional:
            violations.append(
                Violation(
                    "MARKER_ONLY",
                    f"{req.name}=={req.version}: requirements-lock.txt には"
                    " environment marker 付きの行しかない"
                    "(CI の Linux に入る保証がない)",
                )
            )
        else:
            violations.append(
                Violation("MISSING", f"{req.name}=={req.version}: requirements-lock.txt に無い")
            )
    return violations


def count_version_differences(layer_text: str, lock_text: str) -> int:
    """Layer と lock の両方にあり、版が違う package の数(情報のみ。失敗にしない)。"""
    layer, _ = parse_requirements(layer_text, "infra/layer/requirements.txt")
    lock, _ = parse_requirements(lock_text, "requirements-lock.txt")
    lock_versions: dict[str, set[str]] = {}
    for req in lock:
        if not req.has_marker:
            lock_versions.setdefault(req.name, set()).add(req.version)
    differing = {
        req.name
        for req in layer
        if req.name in lock_versions and req.version not in lock_versions[req.name]
    }
    return len(differing)


def render(violations: list[Violation], version_differences: int) -> str:
    lines: list[str] = []
    if violations:
        lines.append(f"LOCK_CONSISTENCY: 違反 {len(violations)} 件")
        lines.extend(f"  [{v.kind}] {v.detail}" for v in violations)
        lines.append(
            "直し方: requirements-lock.txt に同じ package を追加する"
            "(infra/layer/requirements.txt 側から外さない)。"
            "Windows で pip freeze し直すと pywin32 の marker が落ちる点に注意"
            "(requirements-lock.txt の冒頭の注記を参照)"
        )
    else:
        lines.append("LOCK_CONSISTENCY: OK(Layer の全 package が requirements-lock.txt に存在する)")
    lines.append(
        f"INFO: 版が違う package = {version_differences} 件"
        "(失敗にしない。版の一致は別の Issue の判断。Issue #244 O-2)"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--layer", default=_DEFAULT_LAYER)
    parser.add_argument("--lock", default=_DEFAULT_LOCK)
    args = parser.parse_args(argv)

    texts: list[str] = []
    for path in (Path(args.layer), Path(args.lock)):
        if not path.is_file():
            print(f"検査できなかった: {path} が見つからない", file=sys.stderr)
            return EXIT_UNCHECKABLE
        try:
            texts.append(path.read_bytes().decode("utf-8"))
        except UnicodeDecodeError:
            print(f"検査できなかった: {path} が UTF-8 として読めない", file=sys.stderr)
            return EXIT_UNCHECKABLE
    layer_text, lock_text = texts

    layer, _ = parse_requirements(layer_text, args.layer)
    if not layer:
        # Layer 側を 1 件も読めなかった場合に「違反なし」へ倒さない(fail-close)。
        print(f"検査できなかった: {args.layer} から package を 1 件も読めなかった", file=sys.stderr)
        return EXIT_UNCHECKABLE

    violations = find_violations(layer_text, lock_text)
    print(render(violations, count_version_differences(layer_text, lock_text)))
    return EXIT_VIOLATION if violations else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
