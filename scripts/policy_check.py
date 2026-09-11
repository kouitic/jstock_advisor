"""操作の前に読むべき正本の条文を引く preflight(Issue #337 の D)。

「どの操作をするとき、どの正本のどの節を読む必要があるか」を
`docs/policy_registry.yaml` から引いて JSON で返す。CI からではなく
作業者が手元で任意に実行する。

    python scripts/policy_check.py --operation PR_CREATE

## 本スクリプトが保証しないこと

**required_policies を示すだけであり、読んだことも守ったことも保証しない。**
遵守の確認はレビューと各正本が担う。preflight を通したことを遵守の証拠として
扱ってはならない。

denylist 方式の `scan_for_pii.py` が「通過したからといって他の個人情報が一切
存在しないことを保証しない」と自ら書いているのと同じ趣旨である。機械的に
検査できるのは形式だけである。

## revision を固定して読む(POLICY_REF_PINNING)

**current effective policy は merge 済みの main にある。** 作業中の branch に
書かれた未 merge の規則は、まだ誰にも効いていない。

    POLICY_REF == REGISTRY_SOURCE_REVISION == SSOT_SOURCE_REVISION

registry も ssot_file も `git show <revision>:<path>` で**同一 revision から**読む。
working tree は読まない。

これを守らないと、**feature branch 上で書いた規則が merge 前に自分自身を
正当化する**構造になる。Issue #337 の設計はこの構造を明示的に禁じている。
`policy_ref` に main の SHA を表示しながら working tree の内容で判定するのは、
その禁止に反する。

読む revision を差し替えたい場合は `read_source` を明示的に渡す。
**その経路は呼び出し側が意図して選ぶものであり、既定では使わない。**

## 三値を厳格に区別する

    PASS     検証済み revision の registry から required_policies を引けた
    FAIL     未知の operation / registry の参照が壊れている
    UNKNOWN  判定に必要な情報が無い

**UNKNOWN を PASS へ倒さない。** 倒すと「確認できなかった」が「問題なし」に
化ける。これは fail-open であり、Issue #337 が問題にしている構図そのものである。

## policy source の鮮度(POLICY_SOURCE_FRESHNESS)

ローカルの `origin/main` は remote-tracking ref であり、最後の fetch 以降に
GitHub 上の main が進んでいれば stale である。「origin/main という名前だから
fresh」とは扱わない。

    VERIFIED    remote の main SHA と local の origin/main が一致することを確認した
    STALE       不一致を検出した
    UNVERIFIED  確認できなかった(ネットワーク断 / git の失敗)

**`VERIFIED` 以外では policy を読まない。** `STALE` も `UNVERIFIED` も
`UNKNOWN` とし、**PASS にしない**。古い規則へ自動 fallback して操作を許可しない
ためである。

確認は `git ls-remote` による**比較のみ**とし、fetch の副作用を持たせない。
`STALE` のときに fetch するかどうかは呼び出し側の判断である。

## 全文書を読まない

正本 4 文書は合計 7,000 行を超える。毎回すべてを読む設計にはせず、
`jit_reading` として**読むべき節だけ**を返す。

## 依存

標準ライブラリと PyYAML のみ。PyYAML は `config/*.yaml` の読み込みで
既に使われている。requirements を増やさない。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_RELPATH = "docs/policy_registry.yaml"

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

VERIFIED = "VERIFIED"
STALE = "STALE"
UNVERIFIED = "UNVERIFIED"

SOURCE_KIND_REVISION = "REVISION"
SOURCE_KIND_WORKING_TREE = "WORKING_TREE"

_REMOTE = "origin"
_MAIN_REF = "refs/heads/main"

# registry は pointer だけを持つ。entry に規則本文を書き始めたら、この長さで
# 気づけるようにする(検査そのものは tests/unit/test_policy_registry.py が行う)。
MAX_REGISTRY_FIELD_LENGTH = 200

# repository 相対 path を受け取り、内容を返す。存在しなければ None。
SourceReader = Callable[[str], "str | None"]


class RegistryError(Exception):
    """registry を読めない、または内容が壊れている。"""


def _git(*args: str) -> str:
    """git を呼ぶ。失敗したら CalledProcessError を送出する。"""
    completed = subprocess.run(
        ["git", *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return completed.stdout


def make_revision_reader(revision: str) -> SourceReader:
    """指定 revision から repository 相対 path の内容を読む reader を作る。

    ★ working tree を読まない。`git show <revision>:<path>` を使う。
    """

    def _read(relpath: str) -> str | None:
        try:
            return _git("show", f"{revision}:{relpath}")
        except (subprocess.SubprocessError, OSError):
            return None

    return _read


def make_working_tree_reader() -> SourceReader:
    """working tree を読む reader。

    ★ 既定では使わない。current effective policy は merge 済みの main にあり、
    working tree の内容は「まだ効いていない規則」を含みうるためである。
    呼び出し側が意図して選ぶときだけ渡す。
    """

    def _read(relpath: str) -> str | None:
        target = _REPO_ROOT / relpath
        if not target.is_file():
            return None
        return target.read_text(encoding="utf-8")

    return _read


def check_policy_freshness() -> tuple[str, str | None]:
    """policy source の鮮度を確かめる。

    返り値は (FRESHNESS_RESULT, local の origin/main SHA)。
    SHA が取れなかった場合は None を返す。

    **比較だけを行い fetch しない。** fetch は remote-tracking ref を書き換える
    副作用を持つため、確認のたびに走らせない。
    """
    try:
        local_sha = _git("rev-parse", f"{_REMOTE}/main").strip()
    except (subprocess.SubprocessError, OSError):
        return UNVERIFIED, None

    try:
        remote_line = _git("ls-remote", _REMOTE, _MAIN_REF).strip()
    except (subprocess.SubprocessError, OSError):
        # ネットワーク断 / 認証失敗 / git の不在。
        # ★ 「たぶん最新だろう」で VERIFIED にしない。
        return UNVERIFIED, local_sha

    remote_sha = remote_line.split("\t", 1)[0].strip() if remote_line else ""
    if not remote_sha:
        return UNVERIFIED, local_sha
    if remote_sha != local_sha:
        return STALE, local_sha
    return VERIFIED, local_sha


def load_registry(read_source: SourceReader) -> dict[str, Any]:
    """registry を読む。壊れていれば RegistryError を送出する。"""
    raw_text = read_source(REGISTRY_RELPATH)
    if raw_text is None:
        raise RegistryError(f"registry が見つからない: {REGISTRY_RELPATH}")
    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise RegistryError(f"registry を parse できない: {exc}") from exc

    if not isinstance(raw, dict):
        raise RegistryError("registry の最上位が mapping ではない")
    for key in ("operations", "policies"):
        if key not in raw:
            raise RegistryError(f"registry に {key} が無い")
    if not isinstance(raw["operations"], list) or not isinstance(raw["policies"], list):
        raise RegistryError("operations と policies は list でなければならない")
    return raw


def policies_for(registry: dict[str, Any], operation: str) -> list[dict[str, Any]]:
    """operation に該当する policy を返す。registry の順序を保つ。"""
    return [
        policy
        for policy in registry["policies"]
        if operation in policy.get("applicable_operations", [])
    ]


def validate_references(
    registry: dict[str, Any], read_source: SourceReader
) -> list[str]:
    """registry の参照が壊れていないかを確かめ、問題の一覧を返す。

    ★ registry と同じ revision の ssot_file を読む。working tree を見ない。

    ここで見るのは **pointer として成立しているか**だけである
    (ssot_file が実在し、ssot_anchor がその中に完全一致で存在するか)。
    規則の内容が正しいかは判定しない。機械では判定できない。
    """
    problems: list[str] = []
    operations = set(registry["operations"])
    seen: set[str] = set()

    for policy in registry["policies"]:
        policy_id = policy.get("policy_id")
        if not policy_id:
            problems.append("policy_id の無い entry がある")
            continue
        if policy_id in seen:
            problems.append(f"policy_id が重複している: {policy_id}")
        seen.add(policy_id)

        ssot_file = policy.get("ssot_file")
        anchor = policy.get("ssot_anchor")
        if not ssot_file or not anchor:
            problems.append(f"{policy_id}: ssot_file または ssot_anchor が無い")
            continue

        text = read_source(ssot_file)
        if text is None:
            problems.append(f"{policy_id}: ssot_file が対象 revision に無い: {ssot_file}")
            continue
        if anchor not in text:
            problems.append(f"{policy_id}: ssot_anchor が {ssot_file} に無い")

        for operation in policy.get("applicable_operations", []):
            if operation not in operations:
                problems.append(f"{policy_id}: 未定義の operation: {operation}")

    return problems


def check(
    operation: str,
    registry: dict[str, Any] | None = None,
    read_source: SourceReader | None = None,
) -> dict[str, Any]:
    """operation に必要な policy を引く。

    ★ result は三値である。UNKNOWN を PASS へ倒さない。
    ★ 既定では検証済み revision(origin/main)から読む。working tree を読まない。

    `read_source` を渡した場合は、その reader が返す内容で判定する。
    `registry` を渡した場合は registry の読み込みだけを差し替える。
    """
    freshness, local_sha = check_policy_freshness()

    report: dict[str, Any] = {
        "operation": operation,
        "policy_ref": local_sha,
        "policy_ref_freshness": freshness,
        "policy_source_kind": None,
        "policy_source_revision": None,
        "required_policies": [],
        "human_gate_required": False,
        "jit_reading": [],
        "result": UNKNOWN,
        "problems": [],
        "disclaimer": (
            "required_policies を示すものであり、読んだことも守ったことも保証しない"
        ),
    }

    if read_source is None:
        # ★ 検証済みでない revision からは読まない。
        #   STALE も UNVERIFIED も UNKNOWN であり、PASS にはならない。
        if freshness != VERIFIED or local_sha is None:
            report["result"] = UNKNOWN
            report["problems"] = [
                "policy source が検証済みでないため判定しない"
                f"(freshness = {freshness})",
                (
                    "git fetch origin main で origin/main を更新してから再実行してください"
                    if freshness == STALE
                    else "remote の main SHA を取得できませんでした"
                ),
            ]
            return report
        read_source = make_revision_reader(local_sha)
        report["policy_source_kind"] = SOURCE_KIND_REVISION
        report["policy_source_revision"] = local_sha
    else:
        # 呼び出し側が読み取り経路を明示した場合。
        # ★ current effective policy と区別できるよう source を記録する。
        report["policy_source_kind"] = SOURCE_KIND_WORKING_TREE

    try:
        reg = registry if registry is not None else load_registry(read_source)
    except RegistryError as exc:
        report["result"] = FAIL
        report["problems"] = [str(exc)]
        return report

    problems = validate_references(reg, read_source)
    if problems:
        # 参照が壊れている registry は「判定できない」ではなく「壊れている」。
        report["result"] = FAIL
        report["problems"] = problems
        return report

    if operation not in set(reg["operations"]):
        # ★ 未知の operation を PASS へ倒さない。知らない操作は検査できていない。
        report["result"] = FAIL
        report["problems"] = [f"未知の operation: {operation}"]
        return report

    matched = policies_for(reg, operation)
    report["required_policies"] = [policy["policy_id"] for policy in matched]
    report["human_gate_required"] = any(
        policy.get("human_gate_required", False) for policy in matched
    )
    report["jit_reading"] = [
        {
            "policy_id": policy["policy_id"],
            "ssot_file": policy["ssot_file"],
            "ssot_anchor": policy["ssot_anchor"],
        }
        for policy in matched
    ]
    report["result"] = PASS
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="操作の前に読むべき正本の条文を引く(Issue #337)"
    )
    parser.add_argument("--operation", required=True, help="操作名")
    parser.add_argument("--issue", type=int, default=None, help="対象 Issue 番号")
    parser.add_argument("--pr", type=int, default=None, help="対象 PR 番号")
    args = parser.parse_args(argv)

    report = check(args.operation)
    if args.issue is not None:
        report["issue"] = args.issue
    if args.pr is not None:
        report["pr"] = args.pr

    print(json.dumps(report, ensure_ascii=False, indent=2))
    # FAIL のときだけ非 0 とする。UNKNOWN は「判定できなかった」であり、
    # 呼び出し側が止まるかどうかを決める(Issue #337 で扱い方を検討中)。
    return 1 if report["result"] == FAIL else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
