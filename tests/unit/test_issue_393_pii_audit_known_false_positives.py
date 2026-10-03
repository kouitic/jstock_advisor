"""Issue #393: 公開metadata PII監査の「確認済みの既知の偽陽性」許可リスト。

## 何を固定するのか

```
既知の5件(人が偽陽性と分類して確認したもの)だけgreen・新しい検出は従来どおりred
  -> 照合は (location_type, location_id, reason, hash_prefix) の完全一致のみ
     (hash単独・所在単独・部分一致・前方一致では除外しない)
  -> 許可できるreasonはEMAIL_PATTERNのみ(DENYLISTは許可できない)
  -> 許可リストが不正ならfail-closed(監査を成功にしない)
  -> 検出側(EMAIL_PATTERN・denylist・機械アドレスの除外)は変更していない(正規表現を固定)
```

★ 公開repo。**一致した文字列そのものも、テストの decorator の参照の形も書かない**
  (本ファイルもpii-scan・日次監査の対象)。検出は`MetadataFinding`を直接構築するか、
  RFC 2606の予約TLD(.invalid)の架空メールで再現する。
★ 検査しているのはロジックとデータの整合であり、GitHubの実データには触れない
  (ネットワークアクセスなし。実データへの適用は別途、audit を実行して確認する)。
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import audit_public_metadata_pii as audit  # noqa: E402
import scan_for_pii  # noqa: E402
from scan_for_pii import (  # noqa: E402
    REASON_DENYLIST,
    REASON_EMAIL_PATTERN,
    MetadataFinding,
    _hash,
    scan_texts,
)

# main() の試験が読み込み関数を差し替えても、実ファイルの読み込みは影響されない
_LOAD_REAL_ALLOWLIST = audit.load_known_false_positives
_ALLOWLIST_FILE = _REPO_ROOT / "scripts" / "pii_audit_known_false_positives.json"

#: 実際の許可リストが宣言しているべき5件(面 / 所在 / hash接頭辞)。
#: ★ 許可リストへエントリを足すと検出の無効化になるため、足すときは本一覧も同じPRで
#:   更新させる(許可リストだけを黙って広げられないようにする)。
_EXPECTED_ENTRIES = {
    ("ISSUE_BODY", "#393", "6599e178"),
    ("ISSUE_BODY", "#393", "d8576854"),
    ("COMMENT", "comment5826672172", "6599e178"),
    ("COMMENT", "comment5957042928", "6599e178"),
    ("COMMENT", "comment5706372901", "d8576854"),
}

_FAKE_EMAIL = "owner-a@example.invalid"  # .invalid は到達しない予約TLD


def _finding(surface: str, location: str, hash_prefix: str, reason: str = REASON_EMAIL_PATTERN):
    return MetadataFinding(surface, location, reason, hash_prefix)


def _valid_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "location_type": "COMMENT",
        "location_id": "comment1",
        "hash_prefix": "0123abcd",
        "reason": "EMAIL_PATTERN",
        "confirmed_by": "classification per the USER decision (test fixture)",
        "confirmed_at": "2026-10-03",
        "classification": "TEST_FIXTURE",
    }
    entry.update(overrides)
    return entry


def _doc(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "entries": list(entries)}


def _real_entries() -> list[audit.KnownFalsePositive]:
    return _LOAD_REAL_ALLOWLIST(_ALLOWLIST_FILE)


def _real_findings() -> list[MetadataFinding]:
    return [_finding(e.location_type, e.location_id, e.hash_prefix) for e in _real_entries()]


# =============================================================================
# (a) 既知の5件はgreen
# =============================================================================


def test_the_real_allowlist_declares_exactly_the_five_confirmed_entries() -> None:
    entries = _real_entries()

    assert {(e.location_type, e.location_id, e.hash_prefix) for e in entries} == _EXPECTED_ENTRIES
    assert len(entries) == 5
    assert {e.reason for e in entries} == {REASON_EMAIL_PATTERN}


def test_the_five_known_findings_are_all_suppressed() -> None:
    remaining, suppressed, unused = audit.apply_known_false_positives(
        _real_findings(), _real_entries()
    )

    assert remaining == []
    assert len(suppressed) == 5
    assert unused == []


# =============================================================================
# (b)(c) 新しい検出は従来どおりred(完全一致のみ)
# =============================================================================


@pytest.mark.parametrize(
    "finding",
    [
        # (b) 同じhashでも、許可リストに無い所在
        _finding("COMMENT", "comment9999999999", "6599e178"),
        _finding("ISSUE_BODY", "#9999", "6599e178"),
        _finding("PR_BODY", "#393", "6599e178"),
        # (c) 同じ所在でも、許可リストに無いhash
        _finding("ISSUE_BODY", "#393", "00000000"),
        _finding("COMMENT", "comment5826672172", "d8576854"),
        # 面だけが違う(同じ所在・同じhash)
        _finding("REVIEW_COMMENT", "comment5826672172", "6599e178"),
        _finding("ISSUE_TITLE", "#393", "6599e178"),
        # reasonだけが違う(DENYLISTは許可リストの対象ではない)
        _finding("ISSUE_BODY", "#393", "6599e178", reason=REASON_DENYLIST),
        _finding("COMMENT", "comment5706372901", "d8576854", reason=REASON_DENYLIST),
        # 部分一致・前方一致・後方一致では除外しない(所在とhashの両方で)
        _finding("ISSUE_BODY", "#393", "6599e179"),
        _finding("ISSUE_BODY", "#393", "6599ffff"),
        _finding("ISSUE_BODY", "#393", "ffffe178"),
        _finding("ISSUE_BODY", "#3930", "6599e178"),
        _finding("ISSUE_BODY", "#39", "6599e178"),
        _finding("COMMENT", "comment58266721720", "6599e178"),
        _finding("COMMENT", "comment58266721", "6599e178"),
    ],
    ids=[
        "same_hash_other_comment",
        "same_hash_other_issue",
        "same_hash_other_surface",
        "same_location_other_hash",
        "same_comment_other_listed_hash",
        "other_surface_same_location_and_hash",
        "issue_title_same_location_and_hash",
        "denylist_same_location_and_hash",
        "denylist_other_entry",
        "hash_differs_in_last_char",
        "hash_shares_only_the_first_half",
        "hash_shares_only_the_second_half",
        "location_extended",
        "location_is_a_prefix_of_a_listed_one",
        "comment_id_extended",
        "comment_id_is_a_prefix_of_a_listed_one",
    ],
)
def test_a_detection_that_differs_in_any_one_part_stays_red(finding: MetadataFinding) -> None:
    remaining, suppressed, _unused = audit.apply_known_false_positives([finding], _real_entries())

    assert remaining == [finding]
    assert suppressed == []


def test_a_new_detection_among_known_ones_is_the_only_one_left() -> None:
    new = _finding("COMMENT", "comment9999999999", "6599e178")

    remaining, suppressed, _unused = audit.apply_known_false_positives(
        [*_real_findings(), new], _real_entries()
    )

    assert remaining == [new]
    assert len(suppressed) == 5


def test_an_entry_that_matches_no_detection_is_reported_as_unused() -> None:
    """該当の本文・コメントが無くなっても失敗にはしないが、警告として分かる。"""
    entries = _real_entries()
    findings = _real_findings()[:3]

    remaining, suppressed, unused = audit.apply_known_false_positives(findings, entries)

    assert remaining == []
    assert len(suppressed) == 3
    assert len(unused) == 2


def test_no_allowlist_means_every_detection_stays() -> None:
    findings = _real_findings()

    remaining, suppressed, unused = audit.apply_known_false_positives(findings, [])

    assert remaining == findings
    assert suppressed == [] and unused == []


# =============================================================================
# (d) 許可リストの読み込みはfail-closed
# =============================================================================


def test_a_valid_document_is_accepted() -> None:
    entries = audit.parse_known_false_positives(_doc(_valid_entry(issue_ref="#393")))

    assert len(entries) == 1 and entries[0].issue_ref == "#393"


@pytest.mark.parametrize(
    "field",
    [
        "location_type",
        "location_id",
        "hash_prefix",
        "reason",
        "confirmed_by",
        "confirmed_at",
        "classification",
    ],
)
def test_a_missing_required_field_is_rejected(field: str) -> None:
    entry = _valid_entry()
    del entry[field]

    with pytest.raises(audit.KnownFalsePositivesError):
        audit.parse_known_false_positives(_doc(entry))


@pytest.mark.parametrize(
    "field",
    [
        "location_type",
        "location_id",
        "hash_prefix",
        "reason",
        "confirmed_by",
        "confirmed_at",
        "classification",
    ],
)
@pytest.mark.parametrize("bad", ["", "   ", None, 0], ids=["empty", "blank", "none", "int"])
def test_an_empty_or_non_text_required_field_is_rejected(field: str, bad: Any) -> None:
    with pytest.raises(audit.KnownFalsePositivesError):
        audit.parse_known_false_positives(_doc(_valid_entry(**{field: bad})))


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason": "DENYLIST"},
        {"reason": "SOMETHING_ELSE"},
        {"location_type": "NOT_A_SURFACE"},
        {"location_type": "comment"},
        {"location_id": "comment 1"},
        {"hash_prefix": "0123ABCD"},
        {"hash_prefix": "0123abc"},
        {"hash_prefix": "0123abcde"},
        {"hash_prefix": "0123abcg"},
        {"confirmed_at": "2026/10/03"},
        {"confirmed_at": "yesterday"},
        {"confirmed_at": "2026-13-40"},
        {"issue_ref": ""},
        {"issue_ref": 393},
        {"unknown_field": "x"},
    ],
    ids=[
        "denylist_reason",
        "unknown_reason",
        "unknown_surface",
        "lowercase_surface",
        "location_with_space",
        "uppercase_hash",
        "short_hash",
        "long_hash",
        "non_hex_hash",
        "slash_date",
        "word_date",
        "impossible_date",
        "empty_issue_ref",
        "int_issue_ref",
        "unknown_field",
    ],
)
def test_an_invalid_field_value_is_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(audit.KnownFalsePositivesError):
        audit.parse_known_false_positives(_doc(_valid_entry(**overrides)))


def test_a_duplicate_match_key_is_rejected() -> None:
    with pytest.raises(audit.KnownFalsePositivesError):
        audit.parse_known_false_positives(_doc(_valid_entry(), _valid_entry()))


@pytest.mark.parametrize(
    "document",
    [
        [],
        "text",
        None,
        {"entries": []},
        {"version": 1},
        {"version": 2, "entries": []},
        {"version": 1, "entries": {}},
        {"version": 1, "entries": ["x"]},
        {"version": 1, "entries": [], "extra": 1},
    ],
    ids=[
        "list",
        "text",
        "none",
        "no_version",
        "no_entries",
        "future_version",
        "entries_not_a_list",
        "entry_not_an_object",
        "extra_top_level_key",
    ],
)
def test_a_malformed_document_is_rejected(document: Any) -> None:
    with pytest.raises(audit.KnownFalsePositivesError):
        audit.parse_known_false_positives(document)


def test_a_missing_or_unreadable_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(audit.KnownFalsePositivesError):
        audit.load_known_false_positives(tmp_path / "missing.json")

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(audit.KnownFalsePositivesError):
        audit.load_known_false_positives(broken)


def test_the_error_message_never_contains_the_entry_values() -> None:
    """例外のメッセージにエントリの値(所在・hash等)を出さない(位置と項目名だけ)。"""
    secret_looking = _valid_entry(location_id="comment-secret-marker", hash_prefix="not-a-hash")

    with pytest.raises(audit.KnownFalsePositivesError) as excinfo:
        audit.parse_known_false_positives(_doc(secret_looking))

    assert "comment-secret-marker" not in str(excinfo.value)
    assert "not-a-hash" not in str(excinfo.value)


# =============================================================================
# main(): 終了コード・出力(fail-closed / 除外の可視化)
# =============================================================================


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    findings: list[MetadataFinding],
    *,
    entries: list[audit.KnownFalsePositive] | None = None,
    load_error: bool = False,
) -> tuple[int, str, str, list[str]]:
    run_calls: list[str] = []

    def _fake_run(repo: str) -> list[MetadataFinding]:
        run_calls.append(repo)
        return findings

    def _fake_load() -> list[audit.KnownFalsePositive]:
        if load_error:
            raise audit.KnownFalsePositivesError("invalid")
        return entries if entries is not None else _real_entries()

    monkeypatch.setattr(audit, "run", _fake_run)
    monkeypatch.setattr(audit, "load_known_false_positives", _fake_load)
    monkeypatch.setattr(sys, "argv", ["audit_public_metadata_pii.py", "owner/repo"])
    code = audit.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err, run_calls


def test_main_is_green_when_only_the_known_findings_remain(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err, _ = _run_main(monkeypatch, capsys, _real_findings())

    assert code == 0
    assert err == ""
    assert "検出はありませんでした" in out


def test_main_does_not_hide_the_suppressed_findings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """除外した検出も出力に残す(黙って消さない)。件数と、面 / 所在 / reason / hashだけ。"""
    _code, out, _err, _ = _run_main(monkeypatch, capsys, _real_findings())

    assert "既知の偽陽性として除外" in out
    assert "5件" in out
    for surface, location, hash_prefix in _EXPECTED_ENTRIES:
        assert (
            f"{surface} {location} reason={REASON_EMAIL_PATTERN} hash_prefix={hash_prefix}" in out
        )


def test_main_is_red_for_a_new_detection_and_reports_only_that_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    new = _finding("COMMENT", "comment9999999999", "6599e178")

    code, out, err, _ = _run_main(monkeypatch, capsys, [*_real_findings(), new])

    assert code == 1
    assert "comment9999999999" in err
    assert "comment5826672172" not in err  # 既知の検出は失敗の所在へ出さない
    assert "既知の偽陽性として除外" in out


def test_main_fails_closed_when_the_allowlist_is_invalid(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """許可リストが不正なら、検出が無くても成功にしない(exit 2)。"""
    code, out, err, run_calls = _run_main(monkeypatch, capsys, [], load_error=True)

    assert code == 2
    assert "許可リストが不正" in err
    assert "検出はありませんでした" not in out
    assert run_calls == []


def test_main_without_entries_behaves_exactly_as_before(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """許可リストが空なら、従来と同じ(全検出がred)。"""
    code, _out, err, _ = _run_main(monkeypatch, capsys, _real_findings(), entries=[])

    assert code == 1
    assert err.count("reason=EMAIL_PATTERN") == 5


def test_main_warns_about_an_unused_entry_without_failing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _err, _ = _run_main(monkeypatch, capsys, _real_findings()[:4])

    assert code == 0
    assert "警告: 許可リストのエントリがどの検出にも一致しませんでした" in out


# =============================================================================
# (e) 検出側は変更していない
# =============================================================================


def test_the_email_pattern_is_unchanged() -> None:
    """★ EMAIL_PATTERN(正規表現そのもの)を固定する(USER決定: 変更しない・弱めない)。"""
    assert scan_for_pii._EMAIL.pattern == r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    assert scan_for_pii._EMAIL.flags == re.compile("").flags


def test_the_machine_address_exclusions_are_unchanged() -> None:
    assert frozenset({"noreply@anthropic.com"}) == scan_for_pii._NON_PERSONAL_EMAILS
    assert (
        frozenset({"users.noreply.github.com", "noreply.github.com"})
        == scan_for_pii._NON_PERSONAL_EMAIL_DOMAINS
    )


def test_the_hash_prefix_length_matches_the_allowlist_format() -> None:
    assert scan_for_pii._HASH_PREFIX_LEN == 8


def test_the_detection_layer_does_not_know_about_the_allowlist() -> None:
    """★ 検出関数は許可リストを知らない(除外は後段だけ)。同じ文字列は引き続き検出される。"""
    findings = scan_texts([("COMMENT", "comment1", f"連絡先 {_FAKE_EMAIL} です")])

    assert [(f.surface, f.location, f.reason) for f in findings] == [
        ("COMMENT", "comment1", REASON_EMAIL_PATTERN)
    ]


# =============================================================================
# (g) end-to-end: 本文 -> 検出 -> 許可リスト
# =============================================================================


def _e2e_findings() -> list[MetadataFinding]:
    items = audit.items_from_payloads(
        {"comments": [{"id": 777, "body": f"連絡先は {_FAKE_EMAIL} です"}]}
    )
    return scan_texts(items)


def test_end_to_end_a_detection_is_green_only_when_it_is_declared() -> None:
    findings = _e2e_findings()
    assert len(findings) == 1
    declared = audit.parse_known_false_positives(
        _doc(
            _valid_entry(
                location_type="COMMENT",
                location_id="comment777",
                hash_prefix=_hash(_FAKE_EMAIL)[:8],
            )
        )
    )

    declared_remaining, _s, _u = audit.apply_known_false_positives(findings, declared)
    undeclared_remaining, _s, _u = audit.apply_known_false_positives(findings, [])

    assert declared_remaining == []
    assert undeclared_remaining == findings


def test_end_to_end_the_same_text_in_another_comment_is_still_red() -> None:
    declared = audit.parse_known_false_positives(
        _doc(
            _valid_entry(
                location_type="COMMENT",
                location_id="comment778",  # 宣言した所在とは別のコメント
                hash_prefix=_hash(_FAKE_EMAIL)[:8],
            )
        )
    )

    remaining, suppressed, unused = audit.apply_known_false_positives(_e2e_findings(), declared)

    assert len(remaining) == 1
    assert suppressed == []
    assert len(unused) == 1


def test_end_to_end_main_never_prints_the_matched_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    declared = audit.parse_known_false_positives(
        _doc(
            _valid_entry(
                location_type="COMMENT",
                location_id="comment777",
                hash_prefix=_hash(_FAKE_EMAIL)[:8],
            )
        )
    )
    new = _finding("COMMENT", "comment778", _hash(_FAKE_EMAIL)[:8])

    _code, out, err, _ = _run_main(monkeypatch, capsys, [*_e2e_findings(), new], entries=declared)

    assert _FAKE_EMAIL not in out and _FAKE_EMAIL not in err


# =============================================================================
# (h) 実ファイルの整合
# =============================================================================


def test_every_real_entry_carries_the_required_confirmation_fields() -> None:
    for entry in _real_entries():
        assert entry.confirmed_by.strip()
        assert entry.confirmed_at == "2026-10-03"
        assert entry.classification.strip()
        # USER本人が個別に確認したのではなく、USER決定に基づく分類であることを明示する
        assert "USER decision of 2026-10-03" in entry.confirmed_by
        assert "not an individual confirmation by the USER" in entry.confirmed_by


def test_the_real_file_has_no_matched_text_at_all() -> None:
    """★ 許可リストのファイル自身に、メール様式になりうる文字('@')が1つも無いこと。

    公開面にも日次監査にも、一致した文字列を書かない(hash接頭辞と分類だけ)。
    """
    text = _ALLOWLIST_FILE.read_text(encoding="utf-8")

    assert "@" not in text
    assert scan_texts([("FILE", "allowlist", text)]) == []


def test_the_real_file_is_plain_json_with_the_documented_shape() -> None:
    data = json.loads(_ALLOWLIST_FILE.read_text(encoding="utf-8"))

    assert set(data) == {"version", "entries"}
    assert copy.deepcopy(data) == data
    assert all(
        set(e) <= {*audit._REQUIRED_ENTRY_FIELDS, *audit._OPTIONAL_ENTRY_FIELDS}
        for e in data["entries"]
    )


def test_only_email_pattern_can_be_allowlisted() -> None:
    assert frozenset({REASON_EMAIL_PATTERN}) == audit.ALLOWLIST_REASONS
    assert REASON_DENYLIST not in audit.ALLOWLIST_REASONS
