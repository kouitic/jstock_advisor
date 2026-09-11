"""policy_check の三値・freshness・revision 固定を固定する guard(Issue #337 の D)。

## 何を検証するか

`scripts/policy_check.py` の危険な壊れ方は 2 つある。

**1 「確認できなかった」が「問題なし」に化けること。**

    未知の operation を PASS にする       -> 検査していない操作が通る
    registry が壊れていても PASS にする   -> 誤った条文を指したまま通る
    freshness が VERIFIED でないのに PASS -> 古い規則で判定したまま通る

**2 未 merge の規則が自分自身を正当化すること。**

    policy_ref に main の SHA を表示しながら、判定には working tree を読む
    -> feature branch 上で書いた規則が、merge 前から current policy として振る舞う

いずれも fail-open であり、Issue #337 が問題にしている構図そのものである。
**両方をテストで固定する。**

## 何を検証しないか

**引いた条文が正しいかは判定しない。** registry の pointer が成立しているかは
`test_policy_registry.py` が見る。規則の内容の妥当性は機械では判定できない。
"""

from __future__ import annotations

import subprocess
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
    SOURCE_KIND_REVISION,
    STALE,
    UNKNOWN,
    UNVERIFIED,
    VERIFIED,
)

_FAKE_SHA = "0" * 40
_OTHER_SHA = "1" * 40


def _worktree_reader() -> policy_check.SourceReader:
    """working tree を読む reader。

    ★ テスト専用である。production の既定経路は revision 固定であり、
    ここで working tree を読むのは「registry の中身そのもの」を対象に
    したい検証に限る。
    """
    return policy_check.make_working_tree_reader()


@pytest.fixture(scope="module")
def registry() -> dict[str, Any]:
    loaded: dict[str, Any] = policy_check.load_registry(
        policy_check.make_working_tree_reader()
    )
    return loaded


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """freshness の確認を VERIFIED に固定する。

    ★ ネットワークへ出ない。CI と手元で結果が変わらないようにするためである。
    """

    def _verified() -> tuple[str, str]:
        return VERIFIED, _FAKE_SHA

    monkeypatch.setattr(policy_check, "check_policy_freshness", _verified)


# --- operation ごとに必要な policy を引けること --------------------------------


def test_pr_create_returns_required_policies(
    registry: dict[str, Any], fresh: None
) -> None:
    report = policy_check.check("PR_CREATE", registry, _worktree_reader())
    assert report["result"] == PASS
    assert "DEVELOPMENT_WORKFLOW.DOD_DECLARATION" in report["required_policies"]
    assert "DEVELOPMENT_WORKFLOW.NO_AUTO_CLOSE_KEYWORD" in report["required_policies"]
    assert (
        "DEVELOPMENT_WORKFLOW.TIME_SEMANTICS_DECLARATION" in report["required_policies"]
    )


def test_issue_close_returns_required_policies(
    registry: dict[str, Any], fresh: None
) -> None:
    report = policy_check.check("ISSUE_CLOSE", registry, _worktree_reader())
    assert report["result"] == PASS
    assert "DEVELOPMENT_WORKFLOW.SAME_TYPE_SWEEP" in report["required_policies"]


def test_production_manual_invoke_requires_human_gate(
    registry: dict[str, Any], fresh: None
) -> None:
    report = policy_check.check("PRODUCTION_MANUAL_INVOKE", registry, _worktree_reader())
    assert report["result"] == PASS
    assert report["human_gate_required"] is True


def test_implementation_start_does_not_require_an_unsourced_gate(
    registry: dict[str, Any], fresh: None
) -> None:
    """★ IMPLEMENTATION_START が ★ 正本に無い追加 gate を要求しないこと。

    Issue #337 の監査で `IMPLEMENTATION_APPROVED` が docs 全体で 0 件と実測された。
    ★ preflight がこれを復活させると、正本に無いゲートを機械が要求することになる。

    development_workflow.md 10 節が列挙する人間承認の 8 操作に
    ★ 「実装の着手」は含まれていない。したがって human_gate_required は false。
    """
    report = policy_check.check("IMPLEMENTATION_START", registry, _worktree_reader())
    assert report["result"] == PASS
    assert report["human_gate_required"] is False
    assert "IMPLEMENTATION_APPROVED" not in " ".join(report["required_policies"])


def test_jit_reading_points_at_sections_not_whole_documents(
    registry: dict[str, Any], fresh: None
) -> None:
    """★ 全文書ではなく ★ 読むべき節を返すこと。"""
    report = policy_check.check("MERGE", registry, _worktree_reader())
    assert report["jit_reading"], "jit_reading が空"
    for entry in report["jit_reading"]:
        assert set(entry) == {"policy_id", "ssot_file", "ssot_anchor"}
        assert entry["ssot_anchor"].startswith("#"), "anchor は見出し文字列である"


def test_report_states_that_it_guarantees_nothing(
    registry: dict[str, Any], fresh: None
) -> None:
    """preflight を通したことが遵守の証拠と誤解されないこと。"""
    report = policy_check.check("PR_CREATE", registry, _worktree_reader())
    assert "保証しない" in report["disclaimer"]


# --- 三値 ----------------------------------------------------------------------


def test_unknown_operation_is_fail_not_pass(
    registry: dict[str, Any], fresh: None
) -> None:
    """★ 未知の operation を PASS へ倒さない。"""
    report = policy_check.check("NO_SUCH_OPERATION", registry, _worktree_reader())
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
    report = policy_check.check("PR_CREATE", broken, _worktree_reader())
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
    report = policy_check.check("PR_CREATE", broken, _worktree_reader())
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
    report = policy_check.check("PR_CREATE", broken, _worktree_reader())
    assert report["result"] == FAIL


# --- freshness と revision 固定の組み合わせ（Case A〜D）--------------------------


def test_case_a_verified_uses_revision_not_working_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case A  ★ 本モジュールで最も重要な 1 件。

    freshness = VERIFIED のとき、判定は ★ 検証済み revision の内容で行う。
    ★ working tree に未 merge の policy 変更があっても、それは使われない。

    未 merge の規則が merge 前から自分自身を正当化する構造を禁じるためである。
    """
    revision_registry = {
        "operations": ["PR_CREATE"],
        "policies": [
            {
                "policy_id": "FROM.REVISION",
                "ssot_file": "docs/development_workflow.md",
                "ssot_anchor": "### Definition of Done(DoD) の申告",
                "applicable_operations": ["PR_CREATE"],
                "human_gate_required": False,
                "machine_enforceable": True,
            }
        ],
    }
    seen: list[tuple[str, str]] = []

    def _fake_git(*args: str) -> str:
        assert args[0] != "fetch", "fetch を呼んではいけない"
        if args[0] == "rev-parse":
            return _FAKE_SHA + "\n"
        if args[0] == "ls-remote":
            return _FAKE_SHA + "\trefs/heads/main\n"
        if args[0] == "show":
            revision, _, relpath = args[1].partition(":")
            seen.append((revision, relpath))
            if relpath == policy_check.REGISTRY_RELPATH:
                return yaml.safe_dump(revision_registry, allow_unicode=True)
            return "### Definition of Done(DoD) の申告"
        raise AssertionError(f"想定外の git 呼び出し: {args}")

    monkeypatch.setattr(policy_check, "_git", _fake_git)

    report = policy_check.check("PR_CREATE")

    assert report["result"] == PASS
    assert report["policy_ref"] == _FAKE_SHA
    assert report["policy_source_kind"] == SOURCE_KIND_REVISION
    assert report["policy_source_revision"] == _FAKE_SHA
    # ★ revision 側の registry が使われている（working tree の 21 件ではない）
    assert report["required_policies"] == ["FROM.REVISION"]
    # ★ registry と ssot_file を ★ 同一 revision から読んでいる
    assert seen, "git show を呼んでいない"
    assert {revision for revision, _ in seen} == {_FAKE_SHA}
    assert policy_check.REGISTRY_RELPATH in {relpath for _, relpath in seen}


def test_case_b_stale_is_unknown_and_never_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case B  STALE のとき UNKNOWN。★ PASS にならない。"""

    def _fake_git(*args: str) -> str:
        if args[0] == "rev-parse":
            return _FAKE_SHA + "\n"
        if args[0] == "ls-remote":
            return _OTHER_SHA + "\trefs/heads/main\n"
        raise AssertionError("STALE のとき policy を読んではいけない")

    monkeypatch.setattr(policy_check, "_git", _fake_git)
    report = policy_check.check("PR_CREATE")

    assert report["policy_ref_freshness"] == STALE
    assert report["result"] == UNKNOWN
    assert report["result"] != PASS
    # ★ 古い SHA が残っていても current policy として扱われない
    assert report["policy_ref"] == _FAKE_SHA
    assert report["policy_source_revision"] is None
    assert report["required_policies"] == []
    assert any("origin/main" in p for p in report["problems"])


def test_case_c_unverified_is_unknown_and_never_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case C  remote SHA を取得できないとき UNKNOWN。★ PASS にならない。"""

    def _fake_git(*args: str) -> str:
        if args[0] == "rev-parse":
            return _FAKE_SHA + "\n"
        if args[0] == "ls-remote":
            raise OSError("network down")
        raise AssertionError("UNVERIFIED のとき policy を読んではいけない")

    monkeypatch.setattr(policy_check, "_git", _fake_git)
    report = policy_check.check("PR_CREATE")

    assert report["policy_ref_freshness"] == UNVERIFIED
    assert report["result"] == UNKNOWN
    assert report["result"] != PASS
    assert report["policy_source_revision"] is None
    assert report["required_policies"] == []


def test_case_d_broken_registry_at_verified_revision_is_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case D  検証済み revision 側の参照が壊れていたら FAIL。"""
    revision_registry = {
        "operations": ["PR_CREATE"],
        "policies": [
            {
                "policy_id": "BROKEN.AT.REVISION",
                "ssot_file": "docs/development_workflow.md",
                "ssot_anchor": "### 対象 revision に存在しない見出し",
                "applicable_operations": ["PR_CREATE"],
                "human_gate_required": False,
                "machine_enforceable": True,
            }
        ],
    }

    def _fake_git(*args: str) -> str:
        if args[0] == "rev-parse":
            return _FAKE_SHA + "\n"
        if args[0] == "ls-remote":
            return _FAKE_SHA + "\trefs/heads/main\n"
        if args[0] == "show":
            _, _, relpath = args[1].partition(":")
            if relpath == policy_check.REGISTRY_RELPATH:
                return yaml.safe_dump(revision_registry, allow_unicode=True)
            return "### 別の見出ししか無い本文"
        raise AssertionError(f"想定外の git 呼び出し: {args}")

    monkeypatch.setattr(policy_check, "_git", _fake_git)
    report = policy_check.check("PR_CREATE")

    assert report["result"] == FAIL
    assert any("anchor" in p for p in report["problems"])


def test_registry_absent_at_verified_revision_is_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """検証済み revision に registry がまだ無いなら FAIL。

    ★ 未 merge の registry を working tree から拾って PASS にしない。
    """

    def _fake_git(*args: str) -> str:
        if args[0] == "rev-parse":
            return _FAKE_SHA + "\n"
        if args[0] == "ls-remote":
            return _FAKE_SHA + "\trefs/heads/main\n"
        if args[0] == "show":
            raise subprocess.CalledProcessError(128, "git show")
        raise AssertionError(f"想定外の git 呼び出し: {args}")

    monkeypatch.setattr(policy_check, "_git", _fake_git)
    report = policy_check.check("PR_CREATE")

    assert report["result"] == FAIL
    assert any("registry" in p for p in report["problems"])


# --- freshness の判定そのもの ----------------------------------------------------


def test_freshness_check_does_not_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 鮮度の確認が fetch の副作用を持たないこと。

    fetch は remote-tracking ref を書き換える。確認のたびに走らせない設計に
    したことを、ここで固定する。
    """
    calls: list[tuple[str, ...]] = []

    def _fake_git(*args: str) -> str:
        calls.append(args)
        if args[0] == "rev-parse":
            return "b" * 40 + "\n"
        if args[0] == "ls-remote":
            return "b" * 40 + "\trefs/heads/main\n"
        raise AssertionError(f"想定外の git 呼び出し: {args}")

    monkeypatch.setattr(policy_check, "_git", _fake_git)
    result, sha = policy_check.check_policy_freshness()
    assert result == VERIFIED
    assert sha == "b" * 40
    assert not any(args[0] == "fetch" for args in calls), "fetch を呼んでいる"


def test_freshness_detects_divergence(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_git(*args: str) -> str:
        if args[0] == "rev-parse":
            return "c" * 40 + "\n"
        return "d" * 40 + "\trefs/heads/main\n"

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


# --- reader ----------------------------------------------------------------------


def test_revision_reader_returns_none_for_missing_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*args: str) -> str:
        raise subprocess.CalledProcessError(128, "git show")

    monkeypatch.setattr(policy_check, "_git", _raise)
    reader = policy_check.make_revision_reader(_FAKE_SHA)
    assert reader("docs/absent.md") is None


def test_working_tree_reader_returns_none_for_missing_path() -> None:
    reader = policy_check.make_working_tree_reader()
    assert reader("docs/absent.md") is None


# --- registry の読み込み --------------------------------------------------------


def test_broken_yaml_is_registry_error() -> None:
    with pytest.raises(policy_check.RegistryError):
        policy_check.load_registry(lambda _relpath: "policies: [\n")


def test_missing_registry_is_registry_error() -> None:
    with pytest.raises(policy_check.RegistryError):
        policy_check.load_registry(lambda _relpath: None)


def test_registry_without_required_sections_is_error() -> None:
    with pytest.raises(policy_check.RegistryError):
        policy_check.load_registry(lambda _relpath: yaml.safe_dump({"policies": []}))
