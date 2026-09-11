"""policy registry が index として壊れていないことの guard(Issue #337 の C)。

## 何を検証するか

`docs/policy_registry.yaml` は**規則の正本ではない**。各 docs を指す pointer で
ある。したがって壊れ方は 2 種類ある。

    指す先が無い    ssot_file が消えた / ssot_anchor の見出しが書き換わった
    本文を持ち始めた registry へ規則の内容を書き写した(= 二重正本化)

**どちらも静かに起きる。** 見出しを変えた PR は registry を見ないし、
registry へ 1 行説明を足す変更も自然に見える。本モジュールが両方を塞ぐ。

## 何を検証しないか

**規則の内容が正しいかは判定しない。** それは機械では判定できない。
ここで見るのは pointer として成立しているかだけである。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REGISTRY_PATH = _REPO_ROOT / "docs" / "policy_registry.yaml"

sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from policy_check import MAX_REGISTRY_FIELD_LENGTH  # type: ignore[import-not-found]  # noqa: E402

_REQUIRED_KEYS = (
    "policy_id",
    "ssot_file",
    "ssot_anchor",
    "applicable_operations",
    "human_gate_required",
    "machine_enforceable",
)


@pytest.fixture(scope="module")
def registry() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(_REGISTRY_PATH.read_text(encoding="utf-8"))
    return loaded


def _policy_ids(registry: dict[str, Any]) -> list[str]:
    return [policy["policy_id"] for policy in registry["policies"]]


def test_registry_declares_that_it_is_not_the_ssot(registry: dict[str, Any]) -> None:
    """冒頭の自己限定が消えていないこと。

    この 1 行が消えると、registry が正本のように読まれる。
    functional_domains.md が「ルール本文を本書へ複製しない」と自ら宣言して
    いるのと同じ役割であり、★ 宣言そのものが仕様である。
    """
    head = _REGISTRY_PATH.read_text(encoding="utf-8")[:600]
    assert "REGISTRY_IS_NOT_SSOT = YES" in head
    assert "規則本文を持たない" in head


def test_required_keys_present(registry: dict[str, Any]) -> None:
    for policy in registry["policies"]:
        missing = [key for key in _REQUIRED_KEYS if key not in policy]
        assert not missing, f"{policy.get('policy_id')} に {missing} が無い"


def test_policy_id_is_unique(registry: dict[str, Any]) -> None:
    ids = _policy_ids(registry)
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, f"policy_id が重複している: {duplicates}"


def test_every_ssot_file_exists(registry: dict[str, Any]) -> None:
    for policy in registry["policies"]:
        target = _REPO_ROOT / policy["ssot_file"]
        assert target.is_file(), f"{policy['policy_id']}: {policy['ssot_file']} が無い"


def test_every_ssot_anchor_exists_verbatim(registry: dict[str, Any]) -> None:
    """anchor が当該ファイル内に ★ 完全一致で実在すること。

    ★ これが本モジュールの中心である。行番号ではなく見出し文字列を使う設計に
    したため、★ 見出しが書き換われば registry は静かに腐る。
    実際、Issue #277 では docs が registry の所在を古いまま指し続けていた。
    """
    for policy in registry["policies"]:
        text = (_REPO_ROOT / policy["ssot_file"]).read_text(encoding="utf-8")
        assert policy["ssot_anchor"] in text, (
            f"{policy['policy_id']}: anchor が {policy['ssot_file']} に無い\n"
            f"anchor = {policy['ssot_anchor']!r}"
        )


def test_applicable_operations_are_declared(registry: dict[str, Any]) -> None:
    declared = set(registry["operations"])
    for policy in registry["policies"]:
        unknown = [o for o in policy["applicable_operations"] if o not in declared]
        assert not unknown, f"{policy['policy_id']}: 未定義の operation {unknown}"


def test_every_operation_has_at_least_one_policy(registry: dict[str, Any]) -> None:
    """宣言されているのに 1 件も policy を持たない operation が無いこと。

    空の operation は「調べたが何も要らない」と「登録し忘れた」を
    区別できない。
    """
    for operation in registry["operations"]:
        matched = [
            p for p in registry["policies"] if operation in p["applicable_operations"]
        ]
        assert matched, f"{operation} に対応する policy が 1 件も無い"


def test_registry_does_not_contain_rule_text(registry: dict[str, Any]) -> None:
    """★ 規則本文の混入を検出する（二重正本化の予防）。

    registry は pointer だけを持つ。entry に本文らしい長い文字列が現れたら、
    そこから規則の写しが育つ。★ 長さで機械的に止める。

    ★ これは「本文かどうか」の意味判定ではない。★ 長さという形式だけを見る。
    意味判定は機械にはできないため、レビューが担う。
    """
    for policy in registry["policies"]:
        for key, value in policy.items():
            if isinstance(value, str):
                assert len(value) <= MAX_REGISTRY_FIELD_LENGTH, (
                    f"{policy['policy_id']}.{key} が長すぎる"
                    f"（{len(value)} 文字 > {MAX_REGISTRY_FIELD_LENGTH}）。"
                    "registry へ規則本文を書いていないか確認すること"
                )


def test_human_gate_operations_are_marked(registry: dict[str, Any]) -> None:
    """人間承認を要する操作に、human_gate_required = true の policy があること。

    対象は development_workflow.md 10 節が列挙する操作のうち、
    registry が operation として持つものである。★ ここで 10 節の一覧そのものを
    複製しない（複製すると二重正本になる）。
    """
    gated = ("MERGE", "PRODUCTION_MANUAL_INVOKE", "CHANGESET_CREATE", "CHANGESET_EXECUTE")
    for operation in gated:
        assert operation in registry["operations"], f"{operation} が未登録"
        matched = [
            p
            for p in registry["policies"]
            if operation in p["applicable_operations"] and p["human_gate_required"]
        ]
        assert matched, f"{operation} に human_gate_required の policy が無い"
