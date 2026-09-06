"""公開されるGitHub metadata(Issue / PR の本文・コメント・タイトル、label、
branch名)を走査し、既知の個人情報とメールアドレス様式を検出する(Issue #131)。

CIの`pii-scan`ジョブはGit管理ファイルだけを対象としており、**PUBLICリポジトリで
同じく公開されるIssue / PRのテキストは対象外**であった。実際に1件の公開露出が
発生し、検知はCIではなく人手の監査による偶然であった。本スクリプトはその面を
日次で走査する。

設計上の制約(Issue #131 Phase A)。

    denylistのSSoTは`scan_for_pii.py`の1箇所に保つ。ここでは再実装しない。
    一致した文字列をstdout / job summary / artifactのいずれへも出さない。
      出すのは 面 / 所在 / 検出理由 / ハッシュ接頭辞 / 件数 だけである。
    取得した本文をartifactとして保存しない(走査はメモリ内で完結させる)。
    既存のrequired job(`pii-scan`)へGitHub API依存を持ち込まない。
      本スクリプトは別workflowから実行する。

事前防止の代わりにはならない。日次実行のため、露出してから検知までに最大で
実行間隔ぶんの時間がかかる。編集履歴・外部cache・検索エンジンの複製は
本文を直しても消えない。投稿前の手続き
(user_manager_collaboration_protocol.md 11節)が第一の防壁である。
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_for_pii import (  # noqa: E402
    MetadataFinding,
    format_findings,
    scan_texts,
)

# 走査する面の識別子。報告に使う。
SURFACE_ISSUE_BODY = "ISSUE_BODY"
SURFACE_ISSUE_TITLE = "ISSUE_TITLE"
SURFACE_PR_BODY = "PR_BODY"
SURFACE_PR_TITLE = "PR_TITLE"
SURFACE_COMMENT = "COMMENT"
SURFACE_REVIEW_COMMENT = "REVIEW_COMMENT"
SURFACE_LABEL = "LABEL"
SURFACE_BRANCH = "BRANCH"


def items_from_payloads(payloads: dict[str, Any]) -> list[tuple[str, str, str]]:
    """GitHub APIの取得結果を(surface, location, text)の並びへ変換する。

    ネットワークアクセスを含まないため、fixtureのJSONだけで単体テストできる。
    `payloads`のキーはissues / comments / pulls / review_comments / labels / branches。
    未知のキーは無視し、欠けているキーは空として扱う(APIの応答形が変わっても
    走査全体が落ちないようにするため)。

    issues APIはPRも返すため、そのままでは同じPR本文をissues側とpulls側で
    二重に走査し、1件の露出が別々の所在で2件報告されてしまう。担当を分け、
    PRはpulls側だけで扱う(issues側ではPRを読み飛ばす)。Issue番号と
    PR番号は同じ採番空間なので、所在の表記は`#<番号>`で揃える。
    """
    items: list[tuple[str, str, str]] = []

    for issue in payloads.get("issues") or []:
        if "pull_request" in issue:
            continue
        location = f"#{issue.get('number')}"
        items.append((SURFACE_ISSUE_BODY, location, issue.get("body") or ""))
        items.append((SURFACE_ISSUE_TITLE, location, issue.get("title") or ""))

    for pull in payloads.get("pulls") or []:
        location = f"#{pull.get('number')}"
        items.append((SURFACE_PR_BODY, location, pull.get("body") or ""))
        items.append((SURFACE_PR_TITLE, location, pull.get("title") or ""))

    for comment in payloads.get("comments") or []:
        items.append((SURFACE_COMMENT, f"comment{comment.get('id')}", comment.get("body") or ""))

    for comment in payloads.get("review_comments") or []:
        items.append(
            (
                SURFACE_REVIEW_COMMENT,
                f"review_comment{comment.get('id')}",
                comment.get("body") or "",
            )
        )

    for label in payloads.get("labels") or []:
        name = label.get("name") or ""
        items.append((SURFACE_LABEL, name, f"{name} {label.get('description') or ''}"))

    for branch in payloads.get("branches") or []:
        name = branch.get("name") or ""
        items.append((SURFACE_BRANCH, name, name))

    return items


def _gh_api(endpoint: str) -> Any:
    """`gh api --paginate`でGETする。書き込みは行わない。

    pagination を完走させる。単一ページの結果で「0件」と断定しない
    (Issue #122 の pagination finding と同型の事故を避けるため)。
    """
    result = subprocess.run(
        ["gh", "api", "--paginate", endpoint],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def fetch_payloads(repo: str) -> dict[str, Any]:
    """走査対象の面をGitHub APIから取得する(read-only)。"""
    per_page = "per_page=100"
    return {
        "issues": _gh_api(f"repos/{repo}/issues?state=all&{per_page}"),
        "comments": _gh_api(f"repos/{repo}/issues/comments?{per_page}"),
        "pulls": _gh_api(f"repos/{repo}/pulls?state=all&{per_page}"),
        "review_comments": _gh_api(f"repos/{repo}/pulls/comments?{per_page}"),
        "labels": _gh_api(f"repos/{repo}/labels?{per_page}"),
        "branches": _gh_api(f"repos/{repo}/branches?{per_page}"),
    }


def summarize(items: Iterable[tuple[str, str, str]]) -> dict[str, int]:
    """面ごとの走査件数。報告用(本文は含めない)。"""
    counts: dict[str, int] = {}
    for surface, _location, _text in items:
        counts[surface] = counts.get(surface, 0) + 1
    return counts


def run(repo: str) -> list[MetadataFinding]:
    payloads = fetch_payloads(repo)
    items = items_from_payloads(payloads)
    counts = summarize(items)
    print("走査した面と件数:")
    for surface in sorted(counts):
        print(f"  {surface} {counts[surface]}")
    return scan_texts(items)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: audit_public_metadata_pii.py <owner>/<repo>", file=sys.stderr)
        return 2
    findings = run(sys.argv[1])
    if findings:
        print(
            "公開metadataのPII監査に失敗しました"
            "(一致した文字列自体は出力しません)。所在:",
            file=sys.stderr,
        )
        for line in format_findings(findings):
            print(line, file=sys.stderr)
        print(
            "docs/operations_manual.mdの是正手順に従ってください。"
            "本文の編集だけでは編集履歴・外部cacheの複製は消えません。",
            file=sys.stderr,
        )
        return 1
    print("公開metadataのPII監査: 検出はありませんでした。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
