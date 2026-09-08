"""commit messageを走査し、既知の個人情報とメールアドレス様式を検出する
(Issue #131)。

CIの`pii-scan`ジョブはworking treeの**ファイル内容**しか読まないため、
commit messageは検査されていなかった。CLAUDE.mdの禁止対象には
「コミットメッセージ」が含まれているのに、CIはそれを検証していない状態だった。

commit messageの走査は**GitHub APIを必要としない**(checkout済みのworking treeで
git logを読めば足りる)。したがって既存のrequired job(`pii-scan`)へAPI依存を
持ち込む懸念が当てはまらず、PR単位の事前防止として機能させられる。

走査範囲はPRが持ち込むcommitに限定する。リポジトリ全履歴を対象にすると、
過去のcommit messageは書き換えられない(append-onlyの記録である)ため、
直せない失敗でPRを止め続けることになる。

一致した文字列はstdout / job summary / artifactのいずれへも出さない。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_for_pii import MetadataFinding, format_findings, scan_texts  # noqa: E402

SURFACE_COMMIT_MESSAGE = "COMMIT_MESSAGE"

# git logの区切り。commit message本文に現れない制御文字を使う。
_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"


def items_from_git_log(raw: str) -> list[tuple[str, str, str]]:
    """`git log --format=%H%x1f%B%x1e`の出力を(surface, location, text)へ変換する。

    ネットワークもgitも呼ばないため、fixture文字列だけで単体テストできる。
    """
    items: list[tuple[str, str, str]] = []
    for record in raw.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        sha, _, message = record.partition(_FIELD_SEP)
        sha = sha.strip()
        if not sha:
            continue
        items.append((SURFACE_COMMIT_MESSAGE, sha[:12], message))
    return items


def _git_log(commit_range: str) -> str:
    result = subprocess.run(
        ["git", "log", commit_range, f"--format=%H{_FIELD_SEP}%B{_RECORD_SEP}"],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout


def run(commit_range: str) -> tuple[int, list[MetadataFinding]]:
    items = items_from_git_log(_git_log(commit_range))
    return len(items), scan_texts(items)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: scan_commit_messages_pii.py <base>..<head>", file=sys.stderr)
        return 2
    scanned, findings = run(sys.argv[1])
    print(f"走査したcommit message: {scanned} 件")
    if findings:
        print(
            "commit messageのPIIスキャンに失敗しました"
            "(一致した文字列自体は出力しません)。所在:",
            file=sys.stderr,
        )
        for line in format_findings(findings):
            print(line, file=sys.stderr)
        print(
            "commit messageは書き換えが履歴の改変になるため、"
            "docs/operations_manual.mdの是正手順に従ってください。",
            file=sys.stderr,
        )
        return 1
    print("commit messageのPIIスキャン: 検出はありませんでした。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
