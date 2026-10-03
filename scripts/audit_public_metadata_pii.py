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

## 確認済みの偽陽性の許可リスト(Issue #393)

メール様式の検出(EMAIL_PATTERN)は、テストの decorator の参照のような「メール
アドレスではない文字列」にも一致する。公開面は後から編集できず(編集履歴が残る)、
そのまま残る偽陽性が日次監査を恒常的に赤くすると、新しい本物の検出に気づけなくなる。
そこで、**人が分類して確認した偽陽性だけ**を`pii_audit_known_false_positives.json`
へ明示的に宣言し、その検出だけを除外する(USER決定 2026-10-03 = A)。

    照合は (location_type, location_id, reason, hash_prefix) の**完全一致のみ**。
      hash単独・所在単独・部分一致・前方一致・パターンでの除外は行わない
      (将来の別の所在に同じ形の文字列が現れても、自動ではgreenにならない)。
    許可できるreasonはEMAIL_PATTERNのみ。DENYLIST(既知の個人情報)は許可できない。
    許可リストの読み込みはfail-closed。不正なら監査を成功にせず例外で終了する。
    除外した検出も黙って消さず、出力へ「既知の偽陽性として除外」と残す。
    EMAIL_PATTERN・denylist・code blockの扱いは変更しない(検出側は一切変えない)。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scan_for_pii import (  # noqa: E402
    _HASH_PREFIX_LEN,
    REASON_EMAIL_PATTERN,
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


# --- 確認済みの偽陽性の許可リスト(Issue #393) ---------------------------------------

KNOWN_FALSE_POSITIVES_PATH = (
    Path(__file__).resolve().parent / "pii_audit_known_false_positives.json"
)

#: 許可リストに載せられる面。audit が走査する面の識別子と同じ集合。
ALLOWLIST_SURFACES = frozenset(
    {
        SURFACE_ISSUE_BODY,
        SURFACE_ISSUE_TITLE,
        SURFACE_PR_BODY,
        SURFACE_PR_TITLE,
        SURFACE_COMMENT,
        SURFACE_REVIEW_COMMENT,
        SURFACE_LABEL,
        SURFACE_BRANCH,
    }
)

#: 許可リストに載せられるreason。★ DENYLIST(既知の個人情報との一致)は載せられない。
ALLOWLIST_REASONS = frozenset({REASON_EMAIL_PATTERN})

_REQUIRED_ENTRY_FIELDS = frozenset(
    {
        "location_type",
        "location_id",
        "hash_prefix",
        "reason",
        "confirmed_by",
        "confirmed_at",
        "classification",
    }
)
_OPTIONAL_ENTRY_FIELDS = frozenset({"issue_ref"})
_HASH_PREFIX_RE = re.compile(f"[0-9a-f]{{{_HASH_PREFIX_LEN}}}")


class KnownFalsePositivesError(ValueError):
    """許可リストが不正で、読み込めない(fail-closed。監査を成功にしない)。

    メッセージには、エントリの位置と不正な項目の名前だけを入れる。
    """


@dataclass(frozen=True)
class KnownFalsePositive:
    """人が分類して確認した偽陽性1件の宣言。照合に使うのは先頭の4項目だけ。"""

    location_type: str
    location_id: str
    reason: str
    hash_prefix: str
    confirmed_by: str
    confirmed_at: str
    classification: str
    issue_ref: str | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.location_type, self.location_id, self.reason, self.hash_prefix)


def _require_text(entry: dict[str, Any], field: str, index: int) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise KnownFalsePositivesError(
            f"許可リストのエントリ{index}: 必須項目{field}が無い、または空の文字列ではない"
        )
    return value


def parse_known_false_positives(data: Any) -> list[KnownFalsePositive]:
    """許可リストのJSON(読み込み済みの値)を検証して変換する。不正なら例外(fail-closed)。"""
    if not isinstance(data, dict) or set(data) != {"version", "entries"}:
        raise KnownFalsePositivesError("許可リストの最上位は version と entries だけを持つ")
    if data["version"] != 1:
        raise KnownFalsePositivesError("許可リストの version が未対応")
    raw_entries = data["entries"]
    if not isinstance(raw_entries, list):
        raise KnownFalsePositivesError("許可リストの entries が配列ではない")

    entries: list[KnownFalsePositive] = []
    seen: set[tuple[str, str, str, str]] = set()
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise KnownFalsePositivesError(f"許可リストのエントリ{index}: オブジェクトではない")
        unknown = set(raw) - _REQUIRED_ENTRY_FIELDS - _OPTIONAL_ENTRY_FIELDS
        if unknown:
            raise KnownFalsePositivesError(
                f"許可リストのエントリ{index}: 未知の項目 {sorted(unknown)}"
            )
        fields = {name: _require_text(raw, name, index) for name in _REQUIRED_ENTRY_FIELDS}
        if fields["location_type"] not in ALLOWLIST_SURFACES:
            raise KnownFalsePositivesError(f"許可リストのエントリ{index}: location_type が不正")
        if any(ch.isspace() for ch in fields["location_id"]):
            raise KnownFalsePositivesError(f"許可リストのエントリ{index}: location_id が不正")
        if fields["reason"] not in ALLOWLIST_REASONS:
            raise KnownFalsePositivesError(
                f"許可リストのエントリ{index}: reason は {sorted(ALLOWLIST_REASONS)} のみ許可"
            )
        if not _HASH_PREFIX_RE.fullmatch(fields["hash_prefix"]):
            raise KnownFalsePositivesError(f"許可リストのエントリ{index}: hash_prefix の形式が不正")
        try:
            dt.date.fromisoformat(fields["confirmed_at"])
        except ValueError:
            raise KnownFalsePositivesError(
                f"許可リストのエントリ{index}: confirmed_at が YYYY-MM-DD ではない"
            ) from None
        issue_ref = raw.get("issue_ref")
        if issue_ref is not None and (not isinstance(issue_ref, str) or not issue_ref.strip()):
            raise KnownFalsePositivesError(f"許可リストのエントリ{index}: issue_ref が不正")
        entry = KnownFalsePositive(
            location_type=fields["location_type"],
            location_id=fields["location_id"],
            reason=fields["reason"],
            hash_prefix=fields["hash_prefix"],
            confirmed_by=fields["confirmed_by"],
            confirmed_at=fields["confirmed_at"],
            classification=fields["classification"],
            issue_ref=issue_ref,
        )
        if entry.key in seen:
            raise KnownFalsePositivesError(f"許可リストのエントリ{index}: 照合の組が重複している")
        seen.add(entry.key)
        entries.append(entry)
    return entries


def load_known_false_positives(
    path: Path = KNOWN_FALSE_POSITIVES_PATH,
) -> list[KnownFalsePositive]:
    """許可リストのファイルを読み込む。無い・読めない・不正なら例外(fail-closed)。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise KnownFalsePositivesError(f"許可リストを読み込めない({type(exc).__name__})") from None
    return parse_known_false_positives(data)


def apply_known_false_positives(
    findings: Iterable[MetadataFinding], entries: Iterable[KnownFalsePositive]
) -> tuple[list[MetadataFinding], list[MetadataFinding], list[KnownFalsePositive]]:
    """検出を(残り, 既知の偽陽性として除外したもの, どの検出にも一致しなかったエントリ)へ分ける。

    照合は(面, 所在, reason, hash接頭辞)の完全一致のみ。1つでも異なれば除外しない
    (= 従来どおり検出として残る)。
    """
    entry_list = list(entries)
    allowed = {entry.key for entry in entry_list}
    remaining: list[MetadataFinding] = []
    suppressed: list[MetadataFinding] = []
    for finding in findings:
        key = (finding.surface, finding.location, finding.reason, finding.token_hash_prefix)
        (suppressed if key in allowed else remaining).append(finding)
    matched = {(f.surface, f.location, f.reason, f.token_hash_prefix) for f in suppressed}
    unused = [entry for entry in entry_list if entry.key not in matched]
    return remaining, suppressed, unused


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
    try:
        known_false_positives = load_known_false_positives()
    except KnownFalsePositivesError as exc:
        # fail-closed: 許可リストが不正なら、監査を成功にしない(検出を黙って消さない)
        print(f"確認済みの偽陽性の許可リストが不正です: {exc}", file=sys.stderr)
        return 2
    all_findings = run(sys.argv[1])
    findings, suppressed, unused = apply_known_false_positives(all_findings, known_false_positives)
    if suppressed:
        print(
            f"既知の偽陽性として除外した検出(確認済みの許可リスト。{len(suppressed)}件。"
            "一致した文字列自体は出力しません):"
        )
        for line in format_findings(suppressed):
            print(line)
    for entry in unused:
        print(
            "警告: 許可リストのエントリがどの検出にも一致しませんでした"
            f"(該当の本文・コメントが無くなった可能性): {entry.location_type} "
            f"{entry.location_id} reason={entry.reason} hash_prefix={entry.hash_prefix}"
        )
    if findings:
        print(
            "公開metadataのPII監査に失敗しました(一致した文字列自体は出力しません)。所在:",
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
