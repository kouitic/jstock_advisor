"""Governance CI(PR本文の構文検査)のテスト(Issue #342)。

fixture(tests/fixtures/pr_bodies/)を実ファイルとして読み、
scripts/governance_pr_check.py の実装(check_pr_body())をそのまま呼ぶ。
意味判定はしない(構文のみ)ため、false positive/negativeの境界例を
中心に固定する。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from governance_pr_check import (  # type: ignore[import-not-found]  # noqa: E402
    DOD_ITEMS,
    FAIL,
    PASS,
    WARNING,
    check_pr_body,
)

_FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures" / "pr_bodies"


def _read_fixture(name: str) -> str:
    return (_FIXTURES_DIR / name).read_text(encoding="utf-8")


def test_compliant_body_passes() -> None:
    result = check_pr_body(_read_fixture("compliant.md"))
    assert result.result == PASS
    assert result.missing_sections == []
    assert result.missing_dod_items == []
    assert result.has_issue_reference is True
    assert result.closes_matches == []


def test_missing_dod_item_fails() -> None:
    result = check_pr_body(_read_fixture("missing_dod_item.md"))
    assert result.result == FAIL
    assert "定常でない1回目" in result.missing_dod_items


def test_missing_section_fails() -> None:
    result = check_pr_body(_read_fixture("missing_section.md"))
    assert result.result == FAIL
    assert "同型 sweep" in result.missing_sections


def test_no_issue_reference_fails() -> None:
    result = check_pr_body(_read_fixture("no_issue_reference.md"))
    assert result.result == FAIL
    assert result.has_issue_reference is False


def test_closes_at_line_start_is_warning_not_fail() -> None:
    """Closes/Fixes/Resolvesの使用はFAILにしない(#337設計正本どおり)。"""
    result = check_pr_body(_read_fixture("closes_at_line_start.md"))
    assert result.result == WARNING
    assert len(result.closes_matches) == 1
    assert result.has_issue_reference is True


def test_closes_after_bullet_is_detected() -> None:
    result = check_pr_body(_read_fixture("closes_after_bullet.md"))
    assert result.result == WARNING
    assert len(result.closes_matches) == 1


def test_closes_mid_sentence_is_detected() -> None:
    """PR #394レビューFINDING F1: 行頭に限らず文中のCloses/Fixes/Resolves
    ("This PR closes #1 as well."のような、GitHubが実際にauto-closeする
    書き方)もWARNINGとして検出する(行頭限定では素通りしていた)。"""
    result = check_pr_body(_read_fixture("closes_mid_sentence.md"))
    assert result.result == WARNING
    assert len(result.closes_matches) == 1


# --- レビュー対応(PR #394 issuecomment BLOCKING finding): 必須節・DoD項目の
# 判定がcode block/quoteを除外しておらず、本文全体への素朴な部分一致だった
# ことへの固定テスト(T1-T6)。


def test_t1_required_section_only_in_code_block_fails() -> None:
    """T1: 正式な必須節を欠落させ、code block内にのみ例示見出しがある本文はFAIL。"""
    result = check_pr_body(_read_fixture("required_section_only_in_code_block.md"))
    assert result.result == FAIL
    assert "TIME_SEMANTICS_IMPACT" in result.missing_sections


def test_t2_dod_items_only_in_code_block_fails() -> None:
    """T2: 実DoD sectionは存在するが中身が空で、code block内にのみ5項目を書く場合はFAIL。"""
    result = check_pr_body(_read_fixture("dod_items_only_in_code_block.md"))
    assert result.result == FAIL
    assert result.missing_dod_items == DOD_ITEMS


def test_t3_dod_items_in_wrong_section_fails() -> None:
    """T3: DoD 5項目の名称が"## 概要"にあるだけで"## DoD"に無ければFAIL
    (本文全体への部分一致ではなく、実際のDoD section本文のみを見る)。"""
    result = check_pr_body(_read_fixture("dod_items_in_wrong_section.md"))
    assert result.result == FAIL
    assert result.missing_dod_items == DOD_ITEMS


def test_t4_normal_dod_section_passes() -> None:
    """T4: 通常の正しい"## DoD" sectionに5項目があればPASS(既存compliant.mdで固定)。"""
    result = check_pr_body(_read_fixture("compliant.md"))
    assert result.result == PASS
    assert result.missing_dod_items == []


def test_t5_duplicate_heading_in_code_block_does_not_affect_result() -> None:
    """T5: 実見出しに加えてcode block内に同名見出し("## DoD")が重複しても判定は変わらずPASS。"""
    result = check_pr_body(_read_fixture("duplicate_heading_in_code_block.md"))
    assert result.result == PASS
    assert result.missing_sections == []
    assert result.missing_dod_items == []


def test_t6_dod_heading_in_blockquote_is_not_recognized() -> None:
    """T6: ">"で始まるblockquote内の見出し("> ## DoD")はDoD節として認識せずFAIL。"""
    result = check_pr_body(_read_fixture("dod_section_in_blockquote.md"))
    assert result.result == FAIL
    assert "DoD" in result.missing_sections
    assert result.missing_dod_items == DOD_ITEMS


def test_closes_explained_in_prose_is_not_detected() -> None:
    """「Closesは使わずRefsとしています」のような説明文は誤検出しない。"""
    result = check_pr_body(_read_fixture("closes_explained_away.md"))
    assert result.result == PASS
    assert result.closes_matches == []


def test_closes_in_code_block_is_not_detected() -> None:
    result = check_pr_body(_read_fixture("closes_in_code_block.md"))
    assert result.result == PASS
    assert result.closes_matches == []


def test_closes_in_blockquote_is_not_detected() -> None:
    result = check_pr_body(_read_fixture("closes_in_quote.md"))
    assert result.result == PASS
    assert result.closes_matches == []


def test_fail_takes_priority_over_warning() -> None:
    """必須節欠落とCloses使用が同時に起きた場合、FAILが優先される。"""
    body = _read_fixture("closes_at_line_start.md").replace("## 同型 sweep", "## 別の節")
    result = check_pr_body(body)
    assert result.result == FAIL
    assert "同型 sweep" in result.missing_sections
    assert result.closes_matches  # WARNING対象も検出されているが、結果はFAIL


def test_exit_code_contract() -> None:
    """CLIのexit codeは FAIL=1 / PASS・WARNING=0 である。"""
    for fixture, expected_exit in [
        ("compliant.md", 0),
        ("closes_at_line_start.md", 0),
        ("missing_dod_item.md", 1),
        ("no_issue_reference.md", 1),
    ]:
        script = str(Path(__file__).resolve().parents[2] / "scripts" / "governance_pr_check.py")
        body_path = _FIXTURES_DIR / fixture
        completed = subprocess.run(
            [sys.executable, script, "--body-file", str(body_path)],
            capture_output=True,
            timeout=30,
        )
        assert completed.returncode == expected_exit, fixture
