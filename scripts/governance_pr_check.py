"""PR本文の必須節・DoD項目・Issue参照を構文検査する(Issue #342)。

意味判定(宣言内容が変更と矛盾しないか)は行わない。それは引き続き
レビュワーの責務である(development_workflow.md 3.5.8)。

Closes/Fixes/ResolvesはFAILにしない。正本(development_workflow.md、
「NOの宣言は免罪符ではない...FAILとする」の行)が条件つきで許容しているため、
一律禁止は設計より厳しい(Issue #337 Phase A v2、issuecomment-5638458766
4節)。検出した場合はWARNINGとして出力し、reviewerへ意味判断を促す。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field

REQUIRED_SECTIONS = ["概要", "TIME_SEMANTICS_IMPACT", "DoD", "同型 sweep", "確認"]

DOD_ITEMS = ["境界の連続性", "単調性", "定常でない1回目", "単位・スケール", "失敗の可視性"]

# GitHub が linked issue として解釈する keyword(大文字小文字を問わない)。
# レビュー対応(PR #394 issuecomment、FINDING F1): 行頭アンカーは誤検出を
# 1件も防いでおらず(fixture 9件で無変化、実測済み)、文中の"This PR closes #1
# as well."のようなGitHubが実際にauto-closeする書き方を検出漏れにしていた。
# 誤検出を防いでいるのは"\s+#(\d+)"(数字を伴う)の方であるため、アンカーを外し
# 文中のCloses/Fixes/Resolvesも検出対象に含める。
_CLOSE_KEYWORDS = r"close[sd]?|fix(?:e[sd])?|resolve[sd]?"
_CLOSE_LINE_RE = re.compile(
    rf"({_CLOSE_KEYWORDS})\s+#(\d+)",
    re.IGNORECASE,
)
_ISSUE_REF_ANYWHERE_RE = re.compile(
    rf"(?:\bRefs\s+#\d+)|(?:\b(?:{_CLOSE_KEYWORDS})\s+#\d+)", re.IGNORECASE
)

FAIL = "FAIL"
WARNING = "WARNING"
PASS = "PASS"

EXIT_PASS = 0
EXIT_FAIL = 1


@dataclass
class CheckResult:
    result: str
    missing_sections: list[str] = field(default_factory=list)
    missing_dod_items: list[str] = field(default_factory=list)
    has_issue_reference: bool = False
    closes_matches: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _non_code_non_quote_lines(body: str) -> list[str]:
    """コードブロック(```...```)内、および引用(行頭 >)の行を除いた行を返す。"""
    lines = body.splitlines()
    out: list[str] = []
    in_code_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if stripped.startswith(">"):
            continue
        out.append(line)
    return out


_SECTION_HEADER_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")


def _split_into_sections(non_code_non_quote_lines: list[str]) -> dict[str, list[str]]:
    """code block外・quote外の有効行から、section名 -> section本文行、の構造化map
    を作る(レビュー対応: PR #394 issuecomment BLOCKING finding)。

    入力は既に_non_code_non_quote_lines()を通した行であるため、code block内・
    blockquote内(先頭 > )の見出しはここへ到達しない(=構造上、必須節・DoD項目
    として認識されない)。同名sectionが複数回現れた場合は本文行を連結する
    (存在確認・部分一致の判定なので結合しても判定は変わらない)。
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in non_code_non_quote_lines:
        m = _SECTION_HEADER_RE.match(line.strip())
        if m:
            current = m.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def check_pr_body(body: str) -> CheckResult:
    """PR本文を構文検査する(意味判定は行わない)。"""
    result = CheckResult(result=PASS)

    non_code_lines = _non_code_non_quote_lines(body)
    non_code_text = "\n".join(non_code_lines)
    sections = _split_into_sections(non_code_lines)

    for required in REQUIRED_SECTIONS:
        if required not in sections:
            result.missing_sections.append(required)

    # レビュー対応: DoD 5項目は本文全体ではなく実際の"## DoD" section本文
    # (code block/quote除外済み)のみを対象にする。別sectionやcode block
    # 内に同じ語句があるだけでは充足と判定しない。DoD section自体が無い/
    # 空の場合はdod_bodyが空文字列になり、5項目とも自然にmissing扱いになる。
    dod_body = "\n".join(sections.get("DoD", []))
    for item in DOD_ITEMS:
        if item not in dod_body:
            result.missing_dod_items.append(item)

    result.has_issue_reference = bool(_ISSUE_REF_ANYWHERE_RE.search(non_code_text))

    for line in non_code_lines:
        # アンカーを外したため、行頭固定の.match()ではなく行中を探す.search()を使う。
        m = _CLOSE_LINE_RE.search(line)
        if m:
            result.closes_matches.append(line.strip())

    if result.missing_sections:
        result.problems.append(f"必須節が欠落: {', '.join(result.missing_sections)}")
    if "DoD" not in result.missing_sections and result.missing_dod_items:
        result.problems.append(f"DoD項目が欠落: {', '.join(result.missing_dod_items)}")
    if not result.has_issue_reference:
        result.problems.append("Issue参照(Refs/Closes等)が見つかりません")

    if result.problems:
        result.result = FAIL
    elif result.closes_matches:
        result.result = WARNING
    else:
        result.result = PASS

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PR本文の構文検査(Issue #342)")
    parser.add_argument("--body-file", required=True, help="PR本文を含むファイル")
    args = parser.parse_args(argv)

    with open(args.body_file, encoding="utf-8") as f:
        body = f.read()

    check = check_pr_body(body)
    report = {
        "result": check.result,
        "missing_sections": check.missing_sections,
        "missing_dod_items": check.missing_dod_items,
        "has_issue_reference": check.has_issue_reference,
        "closes_matches": check.closes_matches,
        "problems": check.problems,
        "disclaimer": (
            "構文検査のみ。意味判定(宣言内容が変更と矛盾しないか)は"
            "レビュワーの責務である"
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if check.result == WARNING:
        for m in check.closes_matches:
            print(
                "::warning::Closes/Fixes/Resolvesの使用を検出しました"
                f"(意味判定はレビュワーが行います): {m}"
            )

    if check.result == FAIL:
        return EXIT_FAIL
    return EXIT_PASS


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
