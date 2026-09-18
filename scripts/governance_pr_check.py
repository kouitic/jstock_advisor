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
_CLOSE_KEYWORDS = r"close[sd]?|fix(?:e[sd])?|resolve[sd]?"
_CLOSE_LINE_RE = re.compile(
    rf"^\s*(?:[-*+]\s+|\d+[.)]\s+)?({_CLOSE_KEYWORDS})\s+#(\d+)",
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


def _find_section_headers(body: str) -> set[str]:
    found: set[str] = set()
    for line in body.splitlines():
        m = re.match(r"^#{1,6}\s+(.+?)\s*$", line.strip())
        if m:
            found.add(m.group(1).strip())
    return found


def check_pr_body(body: str) -> CheckResult:
    """PR本文を構文検査する(意味判定は行わない)。"""
    result = CheckResult(result=PASS)

    section_headers = _find_section_headers(body)
    for required in REQUIRED_SECTIONS:
        if required not in section_headers:
            result.missing_sections.append(required)

    for item in DOD_ITEMS:
        if item not in body:
            result.missing_dod_items.append(item)

    non_code_lines = _non_code_non_quote_lines(body)
    non_code_text = "\n".join(non_code_lines)

    result.has_issue_reference = bool(_ISSUE_REF_ANYWHERE_RE.search(non_code_text))

    for line in non_code_lines:
        m = _CLOSE_LINE_RE.match(line)
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
