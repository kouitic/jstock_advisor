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

import json
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
    EXIT_CLI_USAGE_ERROR,
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_UNKNOWN,
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

    development_workflow.md 10 節が列挙する人間承認の 9 操作に
    ★ 「実装の着手」は含まれていない。したがって human_gate_required は false。
    """
    report = policy_check.check("IMPLEMENTATION_START", registry, _worktree_reader())
    assert report["result"] == PASS
    assert report["human_gate_required"] is False
    assert "IMPLEMENTATION_APPROVED" not in " ".join(report["required_policies"])


def test_pr_create_does_not_unconditionally_require_a_human_gate(
    registry: dict[str, Any], fresh: None
) -> None:
    """★ PR_CREATE という operation 全体は、無条件の Human Gate を要求しない
    (development_workflow.md 10 節が列挙する人間承認必須 9 操作に
    「PR 作成」自体は含まれていない)。

    Issue #689(SCOPE_REDUCTION_GATE、10.3節)は「Issue の scope/AC を
    縮小する」という特定の部分行為に対する条件付きの Human Gate であり、
    PR_CREATE という operation 全体には及ばない。この条件を
    human_gate_required(operation単位でany()集約される)へ素朴に
    PR_CREATE へ結び付けて true として載せると、PR_CREATEすべてが
    無条件にHuman Gateを要求すると読める state になり、#337と同型の
    欠陥(正本に無いgateを機械が要求する)を再生産する(サブちゃん
    レビュー指摘F6対応)。専用のoperation `ISSUE_SCOPE_REDUCTION`
    (下記test_issue_scope_reduction_requires_a_human_gate参照)を
    新設して結び付け直した(USER指摘F7対応)ため、本テストは
    PR_CREATE自体にSCOPE_REDUCTION_GATEが一切結び付いていないことを
    確認する。
    """
    report = policy_check.check("PR_CREATE", registry, _worktree_reader())
    assert report["result"] == PASS
    assert report["human_gate_required"] is False
    assert "DEVELOPMENT_WORKFLOW.SCOPE_REDUCTION_GATE" not in report["required_policies"]


def test_issue_scope_reduction_requires_a_human_gate(
    registry: dict[str, Any], fresh: None
) -> None:
    """★ ISSUE_SCOPE_REDUCTION(Issueのscope/AC縮小という部分行為)は
    human_gate_required = true でなければならない(development_workflow.md
    10節の人間承認必須9操作の9番目「Issueのscope/AC縮小(10.3節)」に
    対応する。USER指摘F7対応)。
    """
    report = policy_check.check("ISSUE_SCOPE_REDUCTION", registry, _worktree_reader())
    assert report["result"] == PASS
    assert report["human_gate_required"] is True
    assert "DEVELOPMENT_WORKFLOW.SCOPE_REDUCTION_GATE" in report["required_policies"]


def test_jit_reading_points_at_sections_not_whole_documents(
    registry: dict[str, Any], fresh: None
) -> None:
    """★ 全文書ではなく ★ 読むべき節を返すこと。"""
    report = policy_check.check("MERGE", registry, _worktree_reader())
    assert report["jit_reading"], "jit_reading が空"
    for entry in report["jit_reading"]:
        # Issue #656: 従来の 3 field(出典の pointer)は変えず、節の本文・出典の revision・
        # 範囲を**追加**した(既存の field の削除・改名なし)。
        assert set(entry) == {
            "policy_id",
            "ssot_file",
            "ssot_anchor",
            "source_commit_sha",
            "expand",
            "section_text",
        }
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
    # ★ policy_ref は REVISION 経路でのみ設定する。STALE では読んでいないので None。
    #   古い SHA を current policy の revision として表示しない。
    assert report["policy_ref"] is None
    assert report["policy_source_revision"] is None
    assert report["freshness_applies_to_judgment"] is False
    # ★ 診断に必要な local SHA は problems 側へ残す
    assert any(_FAKE_SHA in p for p in report["problems"])
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
    def _fake_git(*args: str) -> str:
        # ★ revision の検証は通す。失敗させるのは show だけ（= path の不在）。
        if args[0] == "rev-parse":
            return _FAKE_SHA
        raise subprocess.CalledProcessError(128, "git show")

    monkeypatch.setattr(policy_check, "_git", _fake_git)
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
# --- ★ 実際の git を通す（monkeypatch しない）-----------------------------------


def test_revision_reader_decodes_utf8_from_real_git() -> None:
    """★ `_git` を monkeypatch せず、★ 実際の git 出力を復号する。

    ★ この 1 件が無いと、Issue #337 で実際に起きた欠陥を捕まえられない。
    `subprocess.run(..., text=True)` は ★ locale の encoding で復号するため、
    cp932 の環境では ★ 日本語を含む正本を読めず `UnicodeDecodeError` になる。
    ★ 他のテストはすべて `_git` を monkeypatch しており、★ 実際の復号を
    1 度も通していなかった。CI は Linux / UTF-8 なので ★ green でも検出できない。

    revision は `HEAD` を使う。`origin/main` は ★ CI の shallow checkout では
    存在しないことがあるためである(actions/checkout の既定は fetch-depth = 1)。
    """
    reader = policy_check.make_revision_reader("HEAD")
    text = reader("docs/development_workflow.md")

    assert text is not None, "HEAD から正本を読めていない"
    # ★ 日本語を含む見出しが復号できていること
    assert "### Definition of Done(DoD) の申告" in text


def test_working_tree_reader_decodes_utf8_from_real_file() -> None:
    """working tree 側も同じく実ファイルで確認する。"""
    reader = policy_check.make_working_tree_reader()
    text = reader("docs/development_workflow.md")

    assert text is not None
    assert "### Definition of Done(DoD) の申告" in text


# --- ★ 取得の失敗を「不在」と報告しないこと -------------------------------------


def _undecodable_reader(relpath: str) -> str | None:
    """復号できない source を模す reader。"""
    raise policy_check.SourceReadError(f"{relpath} を UTF-8 として復号できなかった")


def test_unreadable_registry_is_not_reported_as_absent() -> None:
    """★ registry を取得できなかったとき「見つからない」と言わないこと。

    ★ 今回の欠陥の本質はここである。encoding を直しても、別の理由で読めなければ
    同じ誤診断が出る。★ 取得の失敗と事実の不在を分ける。
    """
    with pytest.raises(policy_check.RegistryError) as exc_info:
        policy_check.load_registry(_undecodable_reader)

    message = str(exc_info.value)
    assert "取得できなかった" in message
    assert "見つからない" not in message


def test_unreadable_ssot_file_is_not_reported_as_absent(
    registry: dict[str, Any],
) -> None:
    """★ ssot_file を取得できなかったとき「対象 revision に無い」と言わないこと。"""
    problems = policy_check.validate_references(registry, _undecodable_reader)

    assert problems
    assert all("取得できなかった" in p for p in problems)
    assert not any("revision に無い" in p for p in problems)


def test_decode_failure_surfaces_as_source_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 復号失敗が None ではなく SourceReadError になること。

    None を返すと呼び出し側は「存在しない」と解釈する。★ そこを型で分ける。
    """

    def _fake_git(*args: str) -> str:
        # ★ revision の検証は通す。復号に失敗するのは show である。
        if args[0] == "rev-parse":
            return _FAKE_SHA
        raise UnicodeDecodeError(
            "cp932", bytes([0x81]), 0, 1, "illegal multibyte sequence"
        )

    monkeypatch.setattr(policy_check, "_git", _fake_git)
    reader = policy_check.make_revision_reader(_FAKE_SHA)

    with pytest.raises(policy_check.SourceReadError):
        reader("docs/development_workflow.md")
# --- ★ WORKING_TREE 経路に policy_ref を付けないこと ---------------------------


def test_working_tree_source_does_not_carry_policy_ref(fresh: None) -> None:
    """★ working tree を読んだ report へ origin/main の SHA を付けないこと。

    付けると「表示している revision」と「実際に読んだ source」が食い違う。
    Finding 1 と同じ型の誤りであり、USER 指示が明文で禁じている。

    ★ policy_source_kind で区別できることと、★ policy_ref を付けないことは
    別の要求である。両方を満たす。
    """
    report = policy_check.check("PR_CREATE", read_source=_worktree_reader())

    assert report["policy_source_kind"] == policy_check.SOURCE_KIND_WORKING_TREE
    assert report["policy_ref"] is None
    assert report["policy_source_revision"] is None
    # ★ freshness は独立した事実として残すが、判定に使っていないことを明示する
    assert report["freshness_applies_to_judgment"] is False


def test_revision_source_carries_policy_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """REVISION 経路では policy_ref を設定し、判定に使ったことを示す。"""
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

    def _fake_git(*args: str) -> str:
        if args[0] == "rev-parse" and args[1] == "--verify":
            return _FAKE_SHA + chr(10)
        if args[0] == "rev-parse":
            return _FAKE_SHA + chr(10)
        if args[0] == "ls-remote":
            return _FAKE_SHA + chr(9) + "refs/heads/main" + chr(10)
        if args[0] == "show":
            _, _, relpath = args[1].partition(":")
            if relpath == policy_check.REGISTRY_RELPATH:
                return yaml.safe_dump(revision_registry, allow_unicode=True)
            return "### Definition of Done(DoD) の申告"
        raise AssertionError(f"想定外の git 呼び出し: {args}")

    monkeypatch.setattr(policy_check, "_git", _fake_git)
    report = policy_check.check("PR_CREATE")

    assert report["policy_source_kind"] == SOURCE_KIND_REVISION
    assert report["policy_ref"] == _FAKE_SHA
    assert report["policy_source_revision"] == _FAKE_SHA
    assert report["freshness_applies_to_judgment"] is True


# --- ★ revision の不在を path の不在と報告しないこと ----------------------------


def test_missing_path_at_valid_revision_is_none() -> None:
    """存在する revision の、存在しない path -> ★ None（= 不在）。"""
    reader = policy_check.make_revision_reader("HEAD")
    assert reader("docs/no_such_file_for_test.md") is None


def test_invalid_revision_is_read_error_not_absence() -> None:
    """★ 存在しない revision -> ★ SourceReadError。★ None ではない。

    None を返すと呼び出し側は「その path が無い」と解釈する。
    ★ revision 自体が無いことと、path が無いことは別の事実である。
    stderr の文言に依存せず、★ 失敗の単位を構造で分ける
    (reader の生成時に revision を 1 回検証する)。
    """
    with pytest.raises(policy_check.SourceReadError):
        policy_check.make_revision_reader("de" + "ad" * 19)


def test_unresolvable_revision_in_check_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既定経路で revision を解決できなかったら ★ UNKNOWN。★ 「policy が無い」ではない。"""

    def _fake_git(*args: str) -> str:
        if args[0] == "rev-parse" and args[1] == "--verify":
            raise subprocess.CalledProcessError(1, "git rev-parse")
        if args[0] == "rev-parse":
            return _FAKE_SHA + chr(10)
        if args[0] == "ls-remote":
            return _FAKE_SHA + chr(9) + "refs/heads/main" + chr(10)
        raise AssertionError(f"想定外の git 呼び出し: {args}")

    monkeypatch.setattr(policy_check, "_git", _fake_git)
    report = policy_check.check("PR_CREATE")

    assert report["result"] == UNKNOWN
    assert any("revision" in p for p in report["problems"])

# --- exit code の契約(Issue #343) ---------------------------------------------


@pytest.fixture
def fresh_at_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """freshness を VERIFIED に固定する。★ revision は ★ 実在する HEAD を使う。

    ★ `fresh` fixture は架空の SHA を返すため、`main()` の既定経路
    (revision 固定の reader)では revision を解決できず UNKNOWN になる。
    exit code の契約を `main()` 越しに確かめるには実在の revision が要る。
    ★ ネットワークへは出ない(`ls-remote` を呼ばずに VERIFIED を返すため)。
    """
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        check=True,
        cwd=_REPO_ROOT,
    ).stdout.decode("utf-8").strip()

    def _verified() -> tuple[str, str]:
        return VERIFIED, head

    monkeypatch.setattr(policy_check, "check_policy_freshness", _verified)


def _stale_git(*args: str) -> str:
    """local と remote の main が食い違う状態(STALE)を作る。"""
    if args[0] == "rev-parse":
        return _FAKE_SHA + chr(10)
    if args[0] == "ls-remote":
        return _OTHER_SHA + chr(9) + "refs/heads/main" + chr(10)
    raise AssertionError("STALE のとき policy を読んではいけない")


def test_exit_code_is_zero_for_pass(
    fresh_at_head: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """PASS -> exit 0。"""
    code = policy_check.main(["--operation", "PR_CREATE"])

    assert code == EXIT_PASS == 0
    assert json.loads(capsys.readouterr().out)["result"] == PASS


def test_exit_code_is_one_for_fail(
    fresh_at_head: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """FAIL -> exit 1。未知の operation はここへ来る。"""
    code = policy_check.main(["--operation", "NO_SUCH_OPERATION"])

    assert code == EXIT_FAIL == 1
    assert json.loads(capsys.readouterr().out)["result"] == FAIL


def test_exit_code_is_two_for_cli_usage_error() -> None:
    """CLI の使い方が不正 -> exit 2(argparse が返す)。

    ★ この 2 は argparse のものであり、policy 判定の結果ではない。
    UNKNOWN と同じ値にしないために契約を分けている。
    """
    with pytest.raises(SystemExit) as exc:
        policy_check.main([])  # --operation が無い

    assert exc.value.code == EXIT_CLI_USAGE_ERROR == 2


def test_exit_code_is_three_for_unknown_when_policy_source_is_stale(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """★ STALE -> UNKNOWN -> exit 3。

    ★ 本 Issue(#343)が閉じる fail-open そのものである。
    exit code だけを見る呼び出し側から見て、PASS(0)と区別できなければならない。
    """
    monkeypatch.setattr(policy_check, "_git", _stale_git)

    code = policy_check.main(["--operation", "PR_CREATE"])

    report = json.loads(capsys.readouterr().out)
    assert report["policy_ref_freshness"] == STALE
    assert report["result"] == UNKNOWN
    assert code == EXIT_UNKNOWN == 3
    # ★ PASS(0)と区別できること。ここが 0 なら fail-open へ戻る。
    assert code != EXIT_PASS
    # ★ argparse の 2 と衝突しないこと(受入条件 3)。
    assert code != EXIT_CLI_USAGE_ERROR


def test_exit_code_is_three_for_unknown_when_freshness_is_unverified(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UNVERIFIED(ネットワーク断など) -> UNKNOWN -> exit 3。"""

    def _fake_git(*args: str) -> str:
        if args[0] == "rev-parse":
            return _FAKE_SHA + chr(10)
        if args[0] == "ls-remote":
            raise OSError("network down")
        raise AssertionError("UNVERIFIED のとき policy を読んではいけない")

    monkeypatch.setattr(policy_check, "_git", _fake_git)

    code = policy_check.main(["--operation", "PR_CREATE"])

    report = json.loads(capsys.readouterr().out)
    assert report["policy_ref_freshness"] == UNVERIFIED
    assert report["result"] == UNKNOWN
    assert code == EXIT_UNKNOWN == 3


def test_exit_codes_are_four_distinct_values() -> None:
    """4 値が互いに異なること。★ UNKNOWN に 2 を割り当てない(受入条件 3)。"""
    codes = [EXIT_PASS, EXIT_FAIL, EXIT_CLI_USAGE_ERROR, EXIT_UNKNOWN]

    assert codes == [0, 1, 2, 3]
    assert len(set(codes)) == 4


def test_unexpected_result_does_not_exit_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """想定外の result は ★ 0 へ倒さない(fail-close)。

    判定語が増えた/壊れたときに、黙って「成功」として返るのを防ぐ。
    """

    def _fake_check(operation: str, **kwargs: Any) -> dict[str, Any]:
        return {"result": "SOMETHING_ELSE", "operation": operation}

    monkeypatch.setattr(policy_check, "check", _fake_check)

    code = policy_check.main(["--operation", "PR_CREATE"])

    assert code == EXIT_UNKNOWN
    assert code != EXIT_PASS
    assert json.loads(capsys.readouterr().out)["result"] == "SOMETHING_ELSE"


# --- 節の本文の抽出(Issue #656。#364 Unit 4) ------------------------------------
#
# 期待値は literal(小さな文書を直接書く)。実装の切り出しを再現して期待値を作らない。

_DOC = """\
# 文書の題

前置き

## 1 第 1 章

1 章の本文

### 1.1 節 A

A の本文

#### 1.1.1 小節 A1

A1 の本文

### 1.2 節 B

B の本文
```
# コードの中のコメント(見出しではない)
### コードの中の偽の見出し
```
B の続き

## 2 第 2 章

2 章の本文
"""


def test_extract_section_stops_before_the_next_heading_of_the_same_or_higher_level() -> None:
    text = policy_check.extract_section(_DOC, "### 1.1 節 A")
    assert text == "### 1.1 節 A\n\nA の本文\n\n#### 1.1.1 小節 A1\n\nA1 の本文"


def test_extract_section_includes_deeper_sub_sections() -> None:
    text = policy_check.extract_section(_DOC, "## 1 第 1 章")
    assert text is not None
    assert "#### 1.1.1 小節 A1" in text
    assert "### 1.2 節 B" in text
    assert "## 2 第 2 章" not in text
    assert text.splitlines()[0] == "## 1 第 1 章"


def test_extract_section_runs_to_the_end_of_the_file_for_the_last_section() -> None:
    text = policy_check.extract_section(_DOC, "## 2 第 2 章")
    assert text == "## 2 第 2 章\n\n2 章の本文"


def test_code_fence_lines_are_not_headings_and_do_not_end_a_section() -> None:
    """★ フェンス内の「# ...」「### ...」で節が途中で切れない。"""
    text = policy_check.extract_section(_DOC, "### 1.2 節 B")
    assert text is not None
    assert "# コードの中のコメント(見出しではない)" in text
    assert "### コードの中の偽の見出し" in text
    assert text.endswith("B の続き")


def test_an_anchor_that_only_exists_inside_a_code_fence_is_not_found() -> None:
    assert policy_check.extract_section(_DOC, "### コードの中の偽の見出し") is None


def test_an_anchor_that_only_appears_in_prose_is_not_a_heading() -> None:
    doc = "# 題\n\nここに ### 1.1 節 A と書いてある文\n"
    assert policy_check.extract_section(doc, "### 1.1 節 A") is None


def test_an_anchor_matching_several_headings_is_an_error_not_a_silent_choice() -> None:
    doc = "## 同じ見出し\n\n本文 1\n\n## 同じ見出し\n\n本文 2\n"
    with pytest.raises(policy_check.SectionExtractionError):
        policy_check.extract_section(doc, "## 同じ見出し")


def test_tilde_fences_and_longer_fences_are_recognised() -> None:
    doc = (
        "## 章\n\n~~~\n# 偽\n~~~\n\n````\n```\n# 偽 2\n```\n````\n\n"
        "## 次の章\n\n次の本文\n"
    )
    text = policy_check.extract_section(doc, "## 章")
    assert text is not None
    assert "# 偽" in text and "# 偽 2" in text
    assert "## 次の章" not in text


def test_crlf_content_gives_the_same_section_as_lf_content() -> None:
    lf = policy_check.extract_section(_DOC, "### 1.1 節 A")
    crlf = policy_check.extract_section(_DOC.replace("\n", "\r\n"), "### 1.1 節 A")
    assert crlf == lf


def test_expand_parent_returns_the_enclosing_section() -> None:
    parent = policy_check.extract_section(_DOC, "### 1.1 節 A", "parent")
    assert parent == policy_check.extract_section(_DOC, "## 1 第 1 章")


def test_expand_parent_of_a_top_level_heading_is_the_whole_document() -> None:
    top = policy_check.extract_section(_DOC, "# 文書の題", "parent")
    assert top == policy_check.extract_section(_DOC, "# 文書の題", "full")
    assert top is not None and top.startswith("# 文書の題") and "2 章の本文" in top


def test_expand_full_returns_the_whole_document_but_still_requires_the_anchor() -> None:
    full = policy_check.extract_section(_DOC, "### 1.1 節 A", "full")
    assert full is not None
    assert full.startswith("# 文書の題") and full.endswith("2 章の本文")
    assert policy_check.extract_section(_DOC, "### 存在しない見出し", "full") is None


def test_extract_section_rejects_an_unknown_expand_mode() -> None:
    with pytest.raises(ValueError):
        policy_check.extract_section(_DOC, "## 2 第 2 章", "everything")


# --- check() が節の本文と出典を返すこと ----------------------------------------


def _doc_reader(files: dict[str, str]) -> policy_check.SourceReader:
    return lambda relpath: files.get(relpath)


_FAKE_REGISTRY: dict[str, Any] = {
    "operations": ["OP_A"],
    "policies": [
        {
            "policy_id": "FAKE.SECTION_B",
            "ssot_file": "docs/fake.md",
            "ssot_anchor": "### 1.2 節 B",
            "applicable_operations": ["OP_A"],
            "human_gate_required": False,
            "machine_enforceable": False,
        }
    ],
}


def test_check_returns_the_section_text_with_provenance(fresh: None) -> None:
    report = policy_check.check(
        "OP_A", _FAKE_REGISTRY, _doc_reader({"docs/fake.md": _DOC})
    )
    assert report["result"] == PASS
    (entry,) = report["jit_reading"]
    assert entry["policy_id"] == "FAKE.SECTION_B"
    assert entry["ssot_file"] == "docs/fake.md"
    assert entry["ssot_anchor"] == "### 1.2 節 B"
    assert entry["expand"] == "section"
    assert entry["section_text"] == (
        "### 1.2 節 B\n\nB の本文\n```\n# コードの中のコメント(見出しではない)\n"
        "### コードの中の偽の見出し\n```\nB の続き"
    )


def test_source_commit_sha_is_none_when_the_source_is_not_a_verified_revision(
    fresh: None,
) -> None:
    """★ working tree 等を読んだ report へ revision の SHA を付けない(policy_ref と同じ理由)。"""
    report = policy_check.check("OP_A", _FAKE_REGISTRY, _doc_reader({"docs/fake.md": _DOC}))
    assert report["policy_ref"] is None
    (entry,) = report["jit_reading"]
    assert entry["source_commit_sha"] is None


def test_source_commit_sha_matches_the_revision_the_text_was_read_from(
    monkeypatch: pytest.MonkeyPatch, fresh: None
) -> None:
    monkeypatch.setattr(
        policy_check, "make_revision_reader", lambda revision: _doc_reader({"docs/fake.md": _DOC})
    )
    monkeypatch.setattr(
        policy_check, "load_registry", lambda read_source: _FAKE_REGISTRY
    )
    report = policy_check.check("OP_A")
    assert report["result"] == PASS
    assert report["policy_source_kind"] == SOURCE_KIND_REVISION
    (entry,) = report["jit_reading"]
    assert report["policy_source_revision"] == _FAKE_SHA
    assert entry["source_commit_sha"] == _FAKE_SHA


def test_check_expand_widens_the_range(fresh: None) -> None:
    reader = _doc_reader({"docs/fake.md": _DOC})
    section = policy_check.check("OP_A", _FAKE_REGISTRY, reader)["jit_reading"][0]
    parent = policy_check.check("OP_A", _FAKE_REGISTRY, reader, expand="parent")["jit_reading"][0]
    full = policy_check.check("OP_A", _FAKE_REGISTRY, reader, expand="full")["jit_reading"][0]
    assert section["section_text"] in parent["section_text"] in full["section_text"]
    assert len(section["section_text"]) < len(parent["section_text"]) < len(full["section_text"])
    assert (section["expand"], parent["expand"], full["expand"]) == ("section", "parent", "full")


def test_check_rejects_an_unknown_expand_mode(fresh: None) -> None:
    with pytest.raises(ValueError):
        policy_check.check("OP_A", _FAKE_REGISTRY, _doc_reader({"docs/fake.md": _DOC}), expand="x")


def test_an_anchor_that_is_not_a_heading_line_is_fail_not_an_empty_section(
    fresh: None,
) -> None:
    """★ validate_references は文書内に anchor が「在る」ことだけを見る。見出し行として
    成立しない anchor(散文にしか無い)は、空の節を返さず FAIL にする(fail-closed)。"""
    registry = {
        "operations": ["OP_A"],
        "policies": [
            {**_FAKE_REGISTRY["policies"][0], "ssot_anchor": "### 1.2 節 B と書いた文"}
        ],
    }
    doc = _DOC + "\n文中に ### 1.2 節 B と書いた文 がある\n"
    report = policy_check.check("OP_A", registry, _doc_reader({"docs/fake.md": doc}))
    assert report["result"] == FAIL
    assert report["jit_reading"] == []
    assert any("見出し行として一致しない" in p for p in report["problems"])


def test_an_anchor_matching_several_headings_is_fail(fresh: None) -> None:
    doc = "## 同じ見出し\n\n1\n\n## 同じ見出し\n\n2\n"
    registry = {
        "operations": ["OP_A"],
        "policies": [{**_FAKE_REGISTRY["policies"][0], "ssot_anchor": "## 同じ見出し"}],
    }
    report = policy_check.check("OP_A", registry, _doc_reader({"docs/fake.md": doc}))
    assert report["result"] == FAIL
    assert report["jit_reading"] == []


def test_report_states_that_section_text_is_not_the_ssot(fresh: None) -> None:
    """★ 抽出結果が cache / generated context であって SSoT ではないことが出力に明示される。"""
    report = policy_check.check("OP_A", _FAKE_REGISTRY, _doc_reader({"docs/fake.md": _DOC}))
    notice = report["section_text_notice"]
    assert "SSoT ではない" in notice
    assert "正本は ssot_file 自体" in notice
    assert "--expand" in notice


# --- 実際の registry の全 policy で、節が取り出せること ---------------------------------


def test_every_registered_policy_yields_a_non_empty_section_that_starts_with_its_anchor(
    registry: dict[str, Any], fresh: None
) -> None:
    for operation in registry["operations"]:
        report = policy_check.check(operation, registry, _worktree_reader())
        assert report["result"] == PASS, (operation, report["problems"])
        for entry in report["jit_reading"]:
            text = entry["section_text"]
            first_line = text.splitlines()[0].strip()
            assert first_line == entry["ssot_anchor"].strip(), entry["policy_id"]
            assert len(text) > len(first_line), f"{entry['policy_id']}: 節の本文が空"


def test_every_registered_section_is_contained_in_its_parent_and_the_full_document(
    registry: dict[str, Any], fresh: None
) -> None:
    reader = _worktree_reader()
    for policy in registry["policies"]:
        content = reader(policy["ssot_file"])
        assert content is not None
        section = policy_check.extract_section(content, policy["ssot_anchor"], "section")
        parent = policy_check.extract_section(content, policy["ssot_anchor"], "parent")
        full = policy_check.extract_section(content, policy["ssot_anchor"], "full")
        assert section is not None and parent is not None and full is not None
        assert section in parent in full, policy["policy_id"]


# --- CLI --------------------------------------------------------------------------


def test_cli_passes_expand_to_check(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _fake_check(operation: str, **kwargs: Any) -> dict[str, Any]:
        seen["operation"] = operation
        seen.update(kwargs)
        return {"result": PASS}

    monkeypatch.setattr(policy_check, "check", _fake_check)
    assert policy_check.main(["--operation", "PR_CREATE", "--expand", "parent"]) == EXIT_PASS
    assert seen == {"operation": "PR_CREATE", "expand": "parent"}


def test_cli_default_expand_is_section(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _fake_check(operation: str, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"result": PASS}

    monkeypatch.setattr(policy_check, "check", _fake_check)
    policy_check.main(["--operation", "PR_CREATE"])
    assert seen == {"expand": "section"}


def test_cli_rejects_an_unknown_expand_mode_with_the_usage_error_code() -> None:
    with pytest.raises(SystemExit) as excinfo:
        policy_check.main(["--operation", "PR_CREATE", "--expand", "everything"])
    assert excinfo.value.code == EXIT_CLI_USAGE_ERROR


def test_cli_output_is_utf8_even_when_stdout_cannot_encode_the_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 標準出力が cp932 でも、section_text の「—」等で UnicodeEncodeError にならない
    (Issue #656 の実機確認で、Windows の既定 encoding により実際に落ちた)。JSON は UTF-8 で出る。"""
    import io

    raw = io.BytesIO()
    cp932_stdout = io.TextIOWrapper(raw, encoding="cp932", write_through=True)
    monkeypatch.setattr(sys, "stdout", cp932_stdout)
    monkeypatch.setattr(
        policy_check, "check", lambda operation, **kwargs: {"result": PASS, "text": "A — B"}
    )
    assert policy_check.main(["--operation", "PR_CREATE"]) == EXIT_PASS
    assert json.loads(raw.getvalue().decode("utf-8")) == {"result": PASS, "text": "A — B"}
