"""Issue #131: 公開されるGitHub metadataのPII走査のテスト。

CIの`pii-scan`はGit管理ファイルだけを対象としており、PUBLICリポジトリで
同じく公開されるIssue / PRのテキストとcommit messageが対象外だった。
本テストはその面を走査する関数を検証する。

検出ロジックの検証には**完全に架空のトークン**だけを使い、本番denylist
(実在人物名のハッシュ)を再利用しない。本ファイル自身もCIのpii-scan対象で
あるため、実在人物名の平文もローマ字表記も一切書かない
(このファイルさえ書けば通ってしまう自己参照的な抜け穴を作らないため)。

ネットワークアクセスを行わない。GitHub APIの応答形はfixtureのdictで与える。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from audit_public_metadata_pii import (  # noqa: E402
    SURFACE_BRANCH,
    SURFACE_COMMENT,
    SURFACE_ISSUE_BODY,
    SURFACE_ISSUE_TITLE,
    SURFACE_LABEL,
    SURFACE_PR_BODY,
    SURFACE_PR_TITLE,
    SURFACE_REVIEW_COMMENT,
    items_from_payloads,
    summarize,
)
from scan_commit_messages_pii import (  # noqa: E402
    SURFACE_COMMIT_MESSAGE,
    items_from_git_log,
)
from scan_for_pii import (  # noqa: E402
    REASON_DENYLIST,
    REASON_EMAIL_PATTERN,
    _hash,
    format_findings,
    scan_texts,
)

# 検出ロジックのテスト専用の架空トークン。実在の人物・組織とは無関係。
_CANARY_TOKEN = "zzmetadatacanary"
_CANARY_HASHES = frozenset({_hash(_CANARY_TOKEN)})

# 架空のメールアドレス。.invalid は RFC 2606 で予約された到達しないTLD。
_FAKE_EMAIL = "owner-a@example.invalid"


# --- denylist 一致 -------------------------------------------------------


def test_denylist_match_is_detected_in_metadata() -> None:
    findings = scan_texts(
        [(SURFACE_ISSUE_BODY, "#1", f"保有状況について {_CANARY_TOKEN} と記載")],
        _CANARY_HASHES,
        detect_email_pattern=False,
    )

    assert len(findings) == 1
    assert findings[0].surface == SURFACE_ISSUE_BODY
    assert findings[0].location == "#1"
    assert findings[0].reason == REASON_DENYLIST


def test_clean_text_produces_no_finding() -> None:
    findings = scan_texts(
        [(SURFACE_ISSUE_BODY, "#1", "所有者A の保有件数は 3 件です")],
        _CANARY_HASHES,
        detect_email_pattern=False,
    )

    assert findings == []


def test_same_token_in_one_text_is_reported_once() -> None:
    text = f"{_CANARY_TOKEN} と {_CANARY_TOKEN} が 2 回出る"
    findings = scan_texts(
        [(SURFACE_COMMENT, "comment1", text)], _CANARY_HASHES, detect_email_pattern=False
    )

    assert len(findings) == 1


# --- メール様式（denylist に無い未知の個人情報を拾う層） -------------------


def test_email_pattern_is_detected_without_denylist_entry() -> None:
    findings = scan_texts([(SURFACE_COMMENT, "comment1", f"連絡先は {_FAKE_EMAIL} です")])

    assert [f.reason for f in findings] == [REASON_EMAIL_PATTERN]


def test_machine_address_in_commit_trailer_is_not_reported() -> None:
    """Co-Authored-By trailer は全commitへ付与が義務づけられており、
    検出しても是正できない。除外しないと警告が常時鳴り意味を失う。"""
    trailer = "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
    raw = _git_log_output(("d" * 40, f"feat: 何かを直す\n\n{trailer}"))

    assert scan_texts(items_from_git_log(raw), _CANARY_HASHES) == []


def test_github_noreply_address_is_not_reported() -> None:
    """GitHubのnoreplyは、実アドレスを隠すためにGitHubが発行する値である。"""
    findings = scan_texts(
        [(SURFACE_COMMENT, "comment1", "1234567+owner-a@users.noreply.github.com")],
        _CANARY_HASHES,
    )

    assert findings == []


def test_allowlist_is_limited_to_the_machine_address_not_the_whole_domain() -> None:
    """anthropic.com 全体を除外すると実アドレスの見逃し口になる。"""
    findings = scan_texts(
        [(SURFACE_COMMENT, "comment1", "owner-a@anthropic.com")], _CANARY_HASHES
    )

    assert [f.reason for f in findings] == [REASON_EMAIL_PATTERN]


def test_allowlist_can_be_disabled_to_verify_the_underlying_detection() -> None:
    findings = scan_texts(
        [(SURFACE_COMMENT, "comment1", "noreply@anthropic.com")],
        _CANARY_HASHES,
        apply_email_allowlist=False,
    )

    assert [f.reason for f in findings] == [REASON_EMAIL_PATTERN]


def test_email_detection_can_be_disabled() -> None:
    findings = scan_texts(
        [(SURFACE_COMMENT, "comment1", _FAKE_EMAIL)],
        _CANARY_HASHES,
        detect_email_pattern=False,
    )

    assert findings == []


# --- 一致文字列を出力しないこと（本 Issue の中核要件） ---------------------


def test_finding_does_not_carry_the_matched_string() -> None:
    findings = scan_texts(
        [(SURFACE_ISSUE_BODY, "#1", f"{_CANARY_TOKEN} と {_FAKE_EMAIL}")], _CANARY_HASHES
    )

    assert findings
    for finding in findings:
        rendered = repr(finding)
        assert _CANARY_TOKEN not in rendered
        assert _FAKE_EMAIL not in rendered


def test_formatted_output_does_not_contain_the_matched_string() -> None:
    findings = scan_texts(
        [(SURFACE_ISSUE_BODY, "#1", f"{_CANARY_TOKEN} と {_FAKE_EMAIL}")], _CANARY_HASHES
    )

    output = "\n".join(format_findings(findings))

    assert _CANARY_TOKEN not in output
    assert _FAKE_EMAIL not in output
    assert "#1" in output, "所在は出す（是正できるようにするため）"
    assert "hash_prefix=" in output


def test_hash_prefix_is_short_enough_not_to_be_a_lookup_table() -> None:
    findings = scan_texts(
        [(SURFACE_ISSUE_BODY, "#1", _CANARY_TOKEN)], _CANARY_HASHES, detect_email_pattern=False
    )

    assert len(findings[0].token_hash_prefix) == 8
    assert _hash(_CANARY_TOKEN).startswith(findings[0].token_hash_prefix)


# --- 空入力・欠損 ---------------------------------------------------------


def test_empty_input_is_safe() -> None:
    assert scan_texts([]) == []


def test_empty_text_is_skipped() -> None:
    assert scan_texts([(SURFACE_ISSUE_BODY, "#1", "")], _CANARY_HASHES) == []


# --- GitHub API の応答形からの変換（pagination 済みの全件を受け取る） -------


def _payloads() -> dict[str, list[dict[str, object]]]:
    return {
        "issues": [
            {"number": 1, "title": "issue title", "body": "issue body"},
            {"number": 2, "title": "pr title", "body": "pr body", "pull_request": {}},
        ],
        "pulls": [{"number": 2, "title": "pr title", "body": "pr body"}],
        "comments": [{"id": 100, "body": "comment body"}],
        "review_comments": [{"id": 200, "body": "review body"}],
        "labels": [{"name": "bug", "description": "desc"}],
        "branches": [{"name": "issue-1-example"}],
    }


def test_items_cover_every_public_surface() -> None:
    surfaces = {surface for surface, _loc, _text in items_from_payloads(_payloads())}

    assert surfaces == {
        SURFACE_ISSUE_BODY,
        SURFACE_ISSUE_TITLE,
        SURFACE_PR_BODY,
        SURFACE_PR_TITLE,
        SURFACE_COMMENT,
        SURFACE_REVIEW_COMMENT,
        SURFACE_LABEL,
        SURFACE_BRANCH,
    }


def test_issue_and_pull_request_bodies_are_labelled_by_their_own_surface() -> None:
    items = items_from_payloads(_payloads())

    assert (SURFACE_ISSUE_BODY, "#1", "issue body") in items
    assert (SURFACE_PR_BODY, "#2", "pr body") in items


def test_pull_request_body_is_not_scanned_twice() -> None:
    """issues APIはPRも返す。二重に走査すると1件の露出が2件として報告される。"""
    items = items_from_payloads(_payloads())

    assert [i for i in items if i[0] == SURFACE_PR_BODY] == [(SURFACE_PR_BODY, "#2", "pr body")]
    assert (SURFACE_ISSUE_BODY, "#2", "pr body") not in items
    assert (SURFACE_ISSUE_TITLE, "#2", "pr title") not in items


def test_multiple_pages_are_all_scanned() -> None:
    """--paginate で連結された全件を受け取り、単一ページで打ち切らない。"""
    payloads = {
        "comments": [{"id": i, "body": "x"} for i in range(1, 251)],
    }

    items = items_from_payloads(payloads)

    assert summarize(items)[SURFACE_COMMENT] == 250, "100 件で打ち切らないこと"


def test_missing_keys_and_null_bodies_do_not_raise() -> None:
    payloads = {"issues": [{"number": 1, "title": None, "body": None}]}

    items = items_from_payloads(payloads)

    assert scan_texts(items, _CANARY_HASHES) == []


def test_detection_reaches_a_title() -> None:
    payloads = {"issues": [{"number": 3, "title": _CANARY_TOKEN, "body": ""}]}

    findings = scan_texts(
        items_from_payloads(payloads), _CANARY_HASHES, detect_email_pattern=False
    )

    assert [(f.surface, f.location) for f in findings] == [(SURFACE_ISSUE_TITLE, "#3")]


# --- commit message ------------------------------------------------------


def _git_log_output(*records: tuple[str, str]) -> str:
    return "".join(f"{sha}\x1f{message}\x1e" for sha, message in records)


def test_commit_messages_are_split_by_record() -> None:
    raw = _git_log_output(("a" * 40, "first\n\nbody"), ("b" * 40, "second"))

    items = items_from_git_log(raw)

    assert [(s, loc) for s, loc, _t in items] == [
        (SURFACE_COMMIT_MESSAGE, "a" * 12),
        (SURFACE_COMMIT_MESSAGE, "b" * 12),
    ]


def test_commit_message_body_is_scanned_not_only_the_subject() -> None:
    raw = _git_log_output(("c" * 40, f"subject line\n\n{_CANARY_TOKEN} は本文にある"))

    findings = scan_texts(items_from_git_log(raw), _CANARY_HASHES, detect_email_pattern=False)

    assert len(findings) == 1
    assert findings[0].surface == SURFACE_COMMIT_MESSAGE


def test_empty_git_log_is_safe() -> None:
    assert items_from_git_log("") == []
    assert items_from_git_log("\n") == []
