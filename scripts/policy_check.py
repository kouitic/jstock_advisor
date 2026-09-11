"""操作の前に読むべき正本の条文を引く preflight(Issue #337 の D)。

「どの操作をするとき、どの正本のどの節を読む必要があるか」を
`docs/policy_registry.yaml` から引いて JSON で返す。CI からではなく
作業者が手元で任意に実行する(2026-09-12 時点では誰の作業も止めない)。

    python scripts/policy_check.py --operation PR_CREATE

## 本スクリプトが保証しないこと

**required_policies を示すだけであり、読んだことも守ったことも保証しない。**
遵守の確認はレビューと各正本が担う。preflight を通したことを遵守の証拠として
扱ってはならない。

denylist 方式の `scan_for_pii.py` が「通過したからといって他の個人情報が一切
存在しないことを保証しない」と自ら書いているのと同じ趣旨である。機械的に
検査できるのは形式だけである。

## 三値を厳格に区別する

    PASS     required_policies を引けた
    FAIL     未知の operation / registry の参照が壊れている
    UNKNOWN  判定に必要な情報が無い

**UNKNOWN を PASS へ倒さない。** 倒すと「確認できなかった」が「問題なし」に
化ける。これは fail-open であり、Issue #337 が問題にしている構図そのものである。

## policy source の鮮度(POLICY_SOURCE_FRESHNESS)

正本は origin/main にある。しかし**ローカルの `origin/main` は
remote-tracking ref であり、最後の fetch 以降に GitHub 上の main が進んでいれば
stale である**。「origin/main という名前だから fresh」とは扱わない。

    VERIFIED    remote の main SHA と local の origin/main が一致することを確認した
    STALE       不一致を検出した(fetch してから再確認する)
    UNVERIFIED  確認できなかった(ネットワーク断 / git の失敗)

`UNVERIFIED` のとき **result を PASS にしない**(UNKNOWN とする)。
古い規則へ自動 fallback して操作を許可しないためである。

確認は `git ls-remote` による**比較のみ**とし、fetch の副作用を持たせない
(Issue #337 の設計で候補 A/B/C を比較し、副作用が最小の B を採った)。

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
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REGISTRY_PATH = _REPO_ROOT / "docs" / "policy_registry.yaml"

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

VERIFIED = "VERIFIED"
STALE = "STALE"
UNVERIFIED = "UNVERIFIED"

_REMOTE = "origin"
_MAIN_REF = "refs/heads/main"

# registry は pointer だけを持つ。entry に規則本文を書き始めたら、この長さで
# 気づけるようにする(検査そのものは tests/unit/test_policy_registry.py が行う)。
MAX_REGISTRY_FIELD_LENGTH = 200


class RegistryError(Exception):
    """registry を読めない、または内容が壊れている。"""


def load_registry(path: Path | None = None) -> dict[str, Any]:
    """registry を読む。壊れていれば RegistryError を送出する。"""
    target = path or _REGISTRY_PATH
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"registry が見つからない: {target}") from exc
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
    return completed.stdout.strip()


def check_policy_freshness() -> tuple[str, str | None]:
    """policy source の鮮度を確かめる。

    返り値は (FRESHNESS_RESULT, local の origin/main SHA)。
    SHA が取れなかった場合は None を返す。

    **比較だけを行い fetch しない。** fetch は remote-tracking ref を書き換える
    副作用を持つため、確認のたびに走らせない。不一致(STALE)を検出したときに
    fetch するかどうかは呼び出し側の判断である。
    """
    try:
        local_sha = _git("rev-parse", f"{_REMOTE}/main")
    except (subprocess.SubprocessError, OSError):
        return UNVERIFIED, None

    try:
        remote_line = _git("ls-remote", _REMOTE, _MAIN_REF)
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


def policies_for(registry: dict[str, Any], operation: str) -> list[dict[str, Any]]:
    """operation に該当する policy を返す。registry の順序を保つ。"""
    return [
        policy
        for policy in registry["policies"]
        if operation in policy.get("applicable_operations", [])
    ]


def _validate_references(registry: dict[str, Any]) -> list[str]:
    """registry の参照が壊れていないかを確かめ、問題の一覧を返す。

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

        target = _REPO_ROOT / ssot_file
        if not target.is_file():
            problems.append(f"{policy_id}: ssot_file が実在しない: {ssot_file}")
            continue
        if anchor not in target.read_text(encoding="utf-8"):
            problems.append(f"{policy_id}: ssot_anchor が {ssot_file} に無い")

        for operation in policy.get("applicable_operations", []):
            if operation not in operations:
                problems.append(f"{policy_id}: 未定義の operation: {operation}")

    return problems


def check(operation: str, registry: dict[str, Any] | None = None) -> dict[str, Any]:
    """operation に必要な policy を引く。

    ★ result は三値である。UNKNOWN を PASS へ倒さない。
    """
    freshness, policy_ref = check_policy_freshness()

    report: dict[str, Any] = {
        "operation": operation,
        "policy_ref": policy_ref,
        "policy_ref_freshness": freshness,
        "required_policies": [],
        "human_gate_required": False,
        "jit_reading": [],
        "result": UNKNOWN,
        "problems": [],
        "disclaimer": (
            "required_policies を示すものであり、読んだことも守ったことも保証しない"
        ),
    }

    try:
        reg = registry if registry is not None else load_registry()
    except RegistryError as exc:
        report["result"] = FAIL
        report["problems"] = [str(exc)]
        return report

    problems = _validate_references(reg)
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

    if freshness == UNVERIFIED:
        # ★ stale の可能性がある SHA を current policy として報告しない。
        report["result"] = UNKNOWN
        report["problems"] = [
            "policy source の鮮度を確認できなかったため PASS にしない"
        ]
        return report

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
    # 呼び出し側が止まるかどうかを決める。
    return 1 if report["result"] == FAIL else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
