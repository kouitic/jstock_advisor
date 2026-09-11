"""policy_check の三値と freshness の扱いを固定する guard(Issue #337 の D)。

## 何を検証するか

`scripts/policy_check.py` の危険な壊れ方は 1 つである。
**「確認できなかった」が「問題なし」に化けること。**

    未知の operation を PASS にする       -> 検査していない操作が通る
    registry が壊れていても PASS にする   -> 誤った条文を指したまま通る
    freshness が UNVERIFIED でも PASS     -> 古い規則で判定したまま通る

いずれも fail-open であり、Issue #337 が問題にしている構図そのものである。
**三値をテストで固定する。**

## 何を検証しないか

**引いた条文が正しいかは判定しない。** registry の pointer が成立しているかは
`test_policy_registry.py` が見る。規則の内容の妥当性は機械では判定できない。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import policy_check  # type: ignore[import-not-found]  # noqa: E402
from policy_check import (  # noqa: E402
    FAIL,
    PASS,
    STALE,
    UNKNOWN,
    UNVERIFIED,
    VERIFIED,
)


@pytest.fixture(scope="module")
def registry() -> dict[str, Any]:
    loaded: dict[str, Any] = policy_check.load_registry()
    return loaded


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """freshness の確認を VERIFIED に固定する。

    ★ ネットワークへ出ない。CI と手元で結果が変わらないようにするためである。
    """

    def _verified() -> tuple[str, str]:
        return VERIFIED, "0" * 40

    monkeypatch.setattr(policy_check, "check_policy_freshness", _verified)


# --- operation ごとに必要な policy を引けること --------------------------------


def test_pr_create_returns_required_policies(registry: dict[str, Any], fresh: None) -> None:
    report = policy_check.check("PR_CREATE", registry)
    assert report["result"] == PASS
    assert "DEVELOPMENT_WORKFLOW.DOD_DECLARATION" in report["required_policies"]
    assert "DEVELOPMENT_WORKFLOW.NO_AUTO_CLOSE_KEYWORD" in report["required_policies"]
    assert "DEVELOPMENT_WORKFLOW.TIME_SEMANTICS_DECLARATION" in report["required_policies"]


def test_issue_close_returns_required_policies(registry: dict[str, Any], fresh: None) -> None:
    report = policy_check.check("ISSUE_CLOSE", registry)
    assert report["result"] == PASS
    assert "DEVELOPMENT_WORKFLOW.SAME_TYPE_SWEEP" in report["required_policies"]


def test_production_manual_invoke_requires_human_gate(
    registry: dict[str, Any], fresh: None,
) -> None:
    report = policy_check.check("PRODUCTION_MANUAL_INVOKE", registry)
    assert report["result"] == PASS
    assert report["human_gate_required"] is True


def test_implementation_start_does_not_require_an_unsourced_gate(
    registry: dict[str, Any], fresh: None,
) -> None:
    """★ IMPLEMENTATION_START が ★ 正本に無い追加 gate を要求しないこと。

    Issue #337 の監査で `IMPLEMENTATION_APPROVED` が docs 全体で 0 件と実測された。
    ★ preflight がこれを復活させると、正本に無いゲートを機械が要求することになる。

    development_workflow.md 10 節が列挙する人間承認の 8 操作に
    ★ 「実装の着手」は含まれていない。したがって human_gate_required は false。
    """
    report = policy_check.check("IMPLEMENTATION_START", registry)
    assert report["result"] == PASS
    assert report["human_gate_required"] is False

    joined = " ".join(report["required_policies"])
    assert "IMPLEMENTATION_APPROVED" not in joined
    assert "APPROVAL" not in joined.upper().replace("HUMAN_APPROVAL_BOUNDARY", "")


def test_jit_reading_points_at_sections_not_whole_documents(
    registry: dict[str, Any], fresh: None,
) -> None:
    """★ 全文書ではなく ★ 読むべき節を返すこと。"""
    report = policy_check.check("MERGE", registry)
    assert report["jit_reading"], "jit_reading が空"
    for entry in report["jit_reading"]:
        assert set(entry) == {"policy_id", "ssot_file", "ssot_anchor"}
        assert entry["ssot_anchor"].startswith("#"), "anchor は見出し文字列である"


def test_report_states_that_it_guarantees_nothing(registry: dict[str, Any], fresh: None) -> None:
    """preflight を通したことが遵守の証拠と誤解されないこと。"""
    report = policy_check.check("PR_CREATE", registry)
    assert "保証しない" in report["disclaimer"]


# --- 三値 ----------------------------------------------------------------------


def test_unknown_operation_is_fail_not_pass(registry: dict[str, Any], fresh: None) -> None:
    """★ 未知の operation を PASS へ倒さない。"""
    report = policy_check.check("NO_SUCH_OPERATION", registry)
    assert report["result"] == FAIL
    assert report["problems"]


def test_broken_policy_reference_is_fail(fresh: None) -> None:
    """★ registry の参照が壊れていたら FAIL。"""
    broken = {
        "operations": ["PR_CREATE"],
        "policies": [
            {
                "policy_id": "BROKEN.ANCHOR",
                "ssot_file": "docs/development_workflow.md",
                "ssot_anchor": "### この見出しは存在しない",
                "applicable_operations": ["PR_CREATE"],
                "human_gate_required": False,
                "machine_enforceable": True,
            }
        ],
    }
    report = policy_check.check("PR_CREATE", broken)
    assert report["result"] == FAIL
    assert any("anchor" in p for p in report["problems"])


def test_missing_ssot_file_is_fail(fresh: None) -> None:
    broken = {
        "operations": ["PR_CREATE"],
        "policies": [
            {
                "policy_id": "BROKEN.FILE",
                "ssot_file": "docs/no_such_document.md",
                "ssot_anchor": "### x",
                "applicable_operations": ["PR_CREATE"],
                "human_gate_required": False,
                "machine_enforceable": True,
            }
        ],
    }
    report = policy_check.check("PR_CREATE", broken)
    assert report["result"] == FAIL


def test_undeclared_operation_in_policy_is_fail(fresh: None) -> None:
    broken = {
        "operations": ["PR_CREATE"],
        "policies": [
            {
                "policy_id": "BROKEN.OP",
                "ssot_file": "docs/development_workflow.md",
                "ssot_anchor": "### Definition of Done(DoD) の申告",
                "applicable_operations": ["PR_CREATE", "NOT_DECLARED"],
                "human_gate_required": False,
                "machine_enforceable": True,
            }
        ],
    }
    report = policy_check.check("PR_CREATE", broken)
    assert report["result"] == FAIL


# --- freshness ------------------------------------------------------------------


def test_unverified_freshness_is_unknown_not_pass(
    registry: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 本モジュールで最も重要な 1 件。

    freshness を確認できなかったとき、★ PASS にしない。
    古い規則へ自動 fallback して操作を許可するのは fail-open である。
    """
    monkeypatch.setattr(
        policy_check, "check_policy_freshness", lambda: (UNVERIFIED, None)
    )
    report = policy_check.check("PR_CREATE", registry)
    assert report["result"] == UNKNOWN
    assert report["policy_ref_freshness"] == UNVERIFIED
    assert report["required_policies"], "引けた policy 自体は返す"


def test_stale_freshness_is_reported_verbatim(
    registry: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """STALE は隠さずそのまま報告すること。"""
    monkeypatch.setattr(
        policy_check, "check_policy_freshness", lambda: (STALE, "a" * 40)
    )
    report = policy_check.check("PR_CREATE", registry)
    assert report["policy_ref_freshness"] == STALE


def test_freshness_check_does_not_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 鮮度の確認が fetch の副作用を持たないこと。

    fetch は remote-tracking ref を書き換える。確認のたびに走らせない設計に
    したことを、ここで固定する。
    """
    calls: list[tuple[str, ...]] = []

    def _fake_git(*args: str) -> str:
        calls.append(args)
        if args[0] == "rev-parse":
            return "b" * 40
        if args[0] == "ls-remote":
            return "b" * 40 + "\trefs/heads/main"
        raise AssertionError(f"想定外の git 呼び出し: {args}")

    monkeypatch.setattr(policy_check, "_git", _fake_git)
    result, sha = policy_check.check_policy_freshness()
    assert result == VERIFIED
    assert sha == "b" * 40
    assert not any(args[0] == "fetch" for args in calls), "fetch を呼んでいる"


def test_freshness_detects_divergence(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_git(*args: str) -> str:
        if args[0] == "rev-parse":
            return "c" * 40
        return "d" * 40 + "\trefs/heads/main"

    monkeypatch.setattr(policy_check, "_git", _fake_git)
    result, _ = policy_check.check_policy_freshness()
    assert result == STALE


def test_freshness_failure_is_unverified_not_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ git が失敗したら「たぶん最新」ではなく UNVERIFIED。"""

    def _raise(*args: str) -> str:
        raise OSError("network down")

    monkeypatch.setattr(policy_check, "_git", _raise)
    result, sha = policy_check.check_policy_freshness()
    assert result == UNVERIFIED
    assert sha is None


# --- registry の読み込み --------------------------------------------------------


def test_broken_yaml_is_registry_error(tmp_path: Path) -> None:
    bad = tmp_path / "broken.yaml"
    bad.write_text("policies: [\n", encoding="utf-8")
    with pytest.raises(policy_check.RegistryError):
        policy_check.load_registry(bad)


def test_missing_registry_is_registry_error(tmp_path: Path) -> None:
    with pytest.raises(policy_check.RegistryError):
        policy_check.load_registry(tmp_path / "absent.yaml")


def test_registry_without_required_sections_is_error(tmp_path: Path) -> None:
    bad = tmp_path / "partial.yaml"
    bad.write_text(yaml.safe_dump({"policies": []}), encoding="utf-8")
    with pytest.raises(policy_check.RegistryError):
        policy_check.load_registry(bad)
