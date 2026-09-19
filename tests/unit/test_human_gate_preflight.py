"""human_gate_preflight(Issue #332 Unit 1-B)の三値・fail-close・read-only・docs 一致の guard。

## 何を検証するか

`scripts/human_gate_preflight.py` の危険な壊れ方は次の 3 つである。

**1 「確認できなかった」が「承認済み」に化けること(fail-open)。**

    却下(REJECT)の受領証を承認として読む(RECEIPT_STATE と APPROVAL_DECISION の不一致)
    有効期限・GitHub 側の現在時刻を取得できないのに PASS にする
    受領証の編集・時系列の逆転・対象の版の不一致を見逃す
    状態遷移の記録(消費済み・取消し・実行中)があるのに PASS にする
    読み取りに失敗したのに「記録が無い」として PASS にする

**2 read-only でなくなること。** 承認の検査が GitHub やファイルへ書き込んではならない。

**3 docs(protocol 2.7節 / contract 8.6節)と実装の値集合・条件の名前が食い違うこと。**
docs が正本であり、checker が docs から外れると、正本を後から検査できなくなる。

## 何を検証しないか

**承認が USER 本人のものであるかは検査しない(できない)。** preflight の PASS は形式の検査であり、
真正性の証拠ではない(モジュール docstring と出力の disclaimer が明記している)。
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import human_gate_preflight as pf  # type: ignore[import-not-found]  # noqa: E402

_REQUEST_ID = "20260101T000000000000Z-DEVELOPER_A-0a1b2c3d"
_EXPECTED = pf.Expected(
    gate_type="ISSUE_CLOSE_GATE",
    scope="Issue #9999 を close する",
    executor="DEVELOPER",
    target_identity="Issue #9999",
    target_version="STATE_ID EXAMPLE",
)
_NOW = dt.datetime(2026, 1, 1, 1, 0, tzinfo=dt.UTC)


def _t(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text)


def _request_body(**overrides: str) -> str:
    fields = {
        "REQUEST_ID": _REQUEST_ID,
        "GATE_TYPE": "ISSUE_CLOSE_GATE",
        "SCOPE": "Issue #9999 を close する",
        "EXECUTOR": "DEVELOPER",
        "TARGET_IDENTITY": "Issue #9999",
        "TARGET_VERSION": "STATE_ID EXAMPLE",
        "ISSUE_OR_PR": "Issue #9999",
        "REQUESTED_AT": "2026-01-01T00:00:00Z",
        "VALID_UNTIL": "2026-01-02T00:00:00Z",
        "APPROVAL_USE": "SINGLE_ATTEMPT",
    }
    fields.update(overrides)
    lines = ["```", "APPROVAL_REQUEST"] + [f"{k} = {v}" for k, v in fields.items()] + ["```"]
    return "\n".join(lines)


def _receipt_body(**overrides: str) -> str:
    fields = {
        "REQUEST_ID": _REQUEST_ID,
        "APPROVAL_DECISION": "APPROVE",
        "RECEIPT_CHANNEL": "DIRECT_INPUT",
        "APPROVAL_SUMMARY": "Issue #9999 の close を承認",
        "TARGET_IDENTITY": "Issue #9999",
        "TARGET_VERSION": "STATE_ID EXAMPLE",
        "RECEIVED_AT": "2026-01-01T00:05:00Z",
        "RECEIPT_STATE": "APPROVED",
    }
    fields.update(overrides)
    lines = ["```", "APPROVAL_RECEIPT"] + [f"{k} = {v}" for k, v in fields.items()] + ["```"]
    return "\n".join(lines)


def _transition_body(state: str, request_id: str = _REQUEST_ID) -> str:
    return f"```\nAPPROVAL_TRANSITION\nREQUEST_ID = {request_id}\nRECEIPT_STATE = {state}\n```"


def _comment(comment_id: int, body: str, created: str, updated: str | None = None) -> pf.Comment:
    return pf.Comment(comment_id, body, _t(created), _t(updated or created))


def _valid_comments(**receipt_overrides: str) -> list[pf.Comment]:
    return [
        _comment(1, _request_body(), "2026-01-01T00:00:00+00:00"),
        _comment(2, _receipt_body(**receipt_overrides), "2026-01-01T00:05:00+00:00"),
    ]


def _evaluate(
    comments: list[pf.Comment],
    *,
    expected: pf.Expected = _EXPECTED,
    now: dt.datetime | None = _NOW,
) -> dict[str, Any]:
    return pf.evaluate(comments, request_id=_REQUEST_ID, expected=expected, now=now)


def _by_id(report: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {c["check_id"]: c for c in report["checks"]}


# --- 基準となる有効な承認 -----------------------------------------------------------------


def test_valid_approval_passes_every_mechanical_check() -> None:
    report = _evaluate(_valid_comments())

    assert report["result"] == pf.PASS
    assert all(c["result"] == pf.PASS for c in report["checks"]), report["checks"]


def test_result_states_that_pass_is_not_proof_of_authenticity() -> None:
    """PASS を真正性の保証として読ませない(disclaimer と not_checked を常に出す)。"""
    report = _evaluate(_valid_comments())

    assert "証拠ではない" in report["disclaimer"]
    not_checked = {n["id"] for n in report["not_checked"]}
    assert {
        "USER_DIRECT_TURN",
        "EXPLICIT_APPROVAL_INTENT",
        "MANAGER_SCOPE_CHECK",
        "VALID_UNTIL_TTL_CAP",
    } <= not_checked


def test_all_seventeen_conditions_are_checked_or_explicitly_not_checked() -> None:
    """17 条件は、検査されるか、検査しないことが明示されるかのどちらかである(黙って省略しない)。"""
    report = _evaluate(_valid_comments())

    covered = {c["check_id"] for c in report["checks"]} | {n["id"] for n in report["not_checked"]}
    assert set(pf.HUMAN_GATE_VALID_CONDITIONS) <= covered


def test_not_implemented_items_are_marked_skipped_with_todo() -> None:
    """未実装・未決定の項目は、黙って省略せず、SKIPPED と TODO を明示する。"""
    for item_id in ("MANAGER_SCOPE_CHECK", "VALID_UNTIL_TTL_CAP"):
        reason = pf.NOT_IMPLEMENTED[item_id]
        assert reason.startswith("SKIPPED")
        assert "TODO" in reason


# --- FAIL: 1 つでも満たさなければ fail-close --------------------------------------------------


@pytest.mark.parametrize(
    ("label", "comments", "expected_check"),
    [
        (
            "request が無い",
            [_comment(2, _receipt_body(), "2026-01-01T00:05:00+00:00")],
            "APPROVAL_REQUEST_EXISTS",
        ),
        (
            "receipt が無い",
            [_comment(1, _request_body(), "2026-01-01T00:00:00+00:00")],
            "RECEIPT_EXISTS",
        ),
        (
            "receipt が 2 件(一意でない)",
            _valid_comments() + [_comment(3, _receipt_body(), "2026-01-01T00:06:00+00:00")],
            "RECEIPT_EXISTS",
        ),
        (
            "request の必須 field が未記入(テンプレートのまま)",
            [
                _comment(1, _request_body(SCOPE="<何を承認するか>"), "2026-01-01T00:00:00+00:00"),
                _comment(2, _receipt_body(), "2026-01-01T00:05:00+00:00"),
            ],
            "APPROVAL_REQUEST_EXISTS",
        ),
        (
            "GATE_TYPE が値集合の外",
            [
                _comment(1, _request_body(GATE_TYPE="MADE_UP_GATE"), "2026-01-01T00:00:00+00:00"),
                _comment(2, _receipt_body(), "2026-01-01T00:05:00+00:00"),
            ],
            "GATE_TYPE_MATCHES",
        ),
        (
            "受領証の RECEIPT_CHANNEL が値集合の外(転送・要約を承認の経路にしない)",
            _valid_comments(RECEIPT_CHANNEL="FORWARDED_FROM_MANAGER"),
            "RECEIPT_EXISTS",
        ),
        (
            "受領証の TARGET_IDENTITY が要求と違う",
            _valid_comments(TARGET_IDENTITY="Issue #1"),
            "TARGET_IDENTITY_MATCHES",
        ),
        (
            "受領証の TARGET_VERSION が要求と違う(依頼時点の版でない)",
            _valid_comments(TARGET_VERSION="STATE_ID OTHER"),
            "TARGET_MATCHES_AT_REQUEST_TIME",
        ),
        (
            "受領証が編集されている(created_at != updated_at)",
            [
                _comment(1, _request_body(), "2026-01-01T00:00:00+00:00"),
                _comment(
                    2, _receipt_body(), "2026-01-01T00:05:00+00:00", "2026-01-01T00:30:00+00:00"
                ),
            ],
            "RECEIPT_NOT_EDITED",
        ),
        (
            "受領証が要求より前(時系列の逆転)",
            [
                _comment(1, _request_body(), "2026-01-01T00:10:00+00:00"),
                _comment(2, _receipt_body(), "2026-01-01T00:05:00+00:00"),
            ],
            "RECEIVED_AT_AFTER_REQUESTED_AT",
        ),
        (
            "VALID_UNTIL を解釈できない",
            [
                _comment(1, _request_body(VALID_UNTIL="来週"), "2026-01-01T00:00:00+00:00"),
                _comment(2, _receipt_body(), "2026-01-01T00:05:00+00:00"),
            ],
            "NOT_EXPIRED",
        ),
    ],
)
def test_single_fault_fails_closed(
    label: str, comments: list[pf.Comment], expected_check: str
) -> None:
    report = _evaluate(comments)

    assert report["result"] == pf.FAIL, label
    assert _by_id(report)[expected_check]["result"] == pf.FAIL, label


@pytest.mark.parametrize(
    ("field", "wrong", "check_id"),
    [
        ("gate_type", "MERGE_GATE", "GATE_TYPE_MATCHES"),
        ("scope", "別の対象を close する", "SCOPE_MATCHES"),
        ("executor", "DEVELOPER_WITH_DEPLOY", "EXECUTOR_MATCHES"),
        ("target_identity", "Issue #1", "TARGET_IDENTITY_MATCHES"),
        ("target_version", "STATE_ID NEWER", "TARGET_VERSION_MATCHES"),
    ],
)
def test_action_about_to_run_must_match_the_approval(field: str, wrong: str, check_id: str) -> None:
    """承認された対象と違う操作を、これから実行しようとしていれば FAIL(TOCTOU への対処)。"""
    expected = pf.Expected(**{**_EXPECTED.__dict__, field: wrong})

    report = _evaluate(_valid_comments(), expected=expected)

    assert report["result"] == pf.FAIL
    assert _by_id(report)[check_id]["result"] == pf.FAIL


def test_expired_approval_fails() -> None:
    report = _evaluate(_valid_comments(), now=_t("2026-01-02T00:00:01+00:00"))

    assert report["result"] == pf.FAIL
    assert _by_id(report)["NOT_EXPIRED"]["result"] == pf.FAIL


# --- F1: 却下の受領証を承認として読まない ------------------------------------------------------


@pytest.mark.parametrize(
    ("decision", "state"),
    [
        ("REJECT", "APPROVED"),  # 却下なのに承認の状態(fail-open の向き)
        ("HOLD", "APPROVED"),
        ("APPROVE", "NOT_APPROVED"),
        ("APPROVE", "CONSUMED"),
        ("MAYBE", "APPROVED"),
    ],
)
def test_receipt_state_must_match_approval_decision(decision: str, state: str) -> None:
    report = _evaluate(_valid_comments(APPROVAL_DECISION=decision, RECEIPT_STATE=state))

    assert report["result"] == pf.FAIL
    assert _by_id(report)[pf.RECEIPT_STATE_MATCHES_DECISION]["result"] == pf.FAIL


@pytest.mark.parametrize("decision", ["HOLD", "REJECT"])
def test_non_approval_receipt_is_never_an_approval_even_when_consistent(decision: str) -> None:
    """HOLD / REJECT は、RECEIPT_STATE = NOT_APPROVED と一致していても承認ではない。"""
    report = _evaluate(_valid_comments(APPROVAL_DECISION=decision, RECEIPT_STATE="NOT_APPROVED"))

    assert report["result"] == pf.FAIL


# --- UNKNOWN: 判定できないものを PASS にしない --------------------------------------------------


def test_missing_github_time_is_unknown_not_pass() -> None:
    """GitHub 側の現在時刻を取得できないとき、実行者のローカル時計へ fallback せず UNKNOWN。"""
    report = _evaluate(_valid_comments(), now=None)

    assert report["result"] == pf.UNKNOWN
    assert _by_id(report)["NOT_EXPIRED"]["result"] == pf.UNKNOWN


def test_unparseable_record_mentioning_the_request_id_is_unknown() -> None:
    """同じ REQUEST_ID を含むが解釈できない comment(書式の違う状態遷移の記録かもしれない)を
    「記録なし」として PASS にしない。"""
    comments = _valid_comments() + [
        _comment(3, f"consumed: {_REQUEST_ID} was used", "2026-01-01T00:20:00+00:00")
    ]

    report = _evaluate(comments)

    assert report["result"] == pf.UNKNOWN
    assert _by_id(report)["NOT_CONSUMED"]["result"] == pf.UNKNOWN


def test_transition_record_with_an_invalid_state_is_unknown() -> None:
    comments = _valid_comments() + [
        _comment(3, _transition_body("HALF_DONE"), "2026-01-01T00:20:00+00:00")
    ]

    assert _evaluate(comments)["result"] == pf.UNKNOWN


def test_uninterpretable_mention_is_judged_per_record_not_per_comment() -> None:
    """PR #431 F1: 同じ comment に別の REQUEST_ID の正常な block があっても、
    この REQUEST_ID の解釈できない言及(消費済みの報告など)を見逃さない。"""
    other_request = _request_body(REQUEST_ID="OTHER-REQUEST-ID")
    prose = f"この承認 {_REQUEST_ID} は実行済みです。CONSUMED。"
    mixed = prose + "\n\n" + other_request
    comments = _valid_comments() + [_comment(3, mixed, "2026-01-01T00:20:00+00:00")]

    report = _evaluate(comments)

    assert report["result"] == pf.UNKNOWN
    assert _by_id(report)["NOT_CONSUMED"]["result"] == pf.UNKNOWN


def test_misspelled_transition_record_with_the_correct_id_in_prose_is_unknown() -> None:
    """PR #431 F1(最も現実的な形): 遷移記録の REQUEST_ID が誤記で、地の文には正しい ID がある。"""
    typo = _transition_body("CONSUMED", request_id=_REQUEST_ID[:-1] + "X")
    body = f"承認 {_REQUEST_ID} を使いました。" + "\n\n" + typo
    comments = _valid_comments() + [_comment(3, body, "2026-01-01T00:20:00+00:00")]

    report = _evaluate(comments)

    assert report["result"] == pf.UNKNOWN
    assert _by_id(report)["NOT_CONSUMED"]["result"] == pf.UNKNOWN
    assert "行目" in _by_id(report)["NOT_CONSUMED"]["detail"]


def test_valid_records_are_covered_even_when_their_comment_has_extra_prose() -> None:
    """この REQUEST_ID を持つ記録の行は解釈済みとして数える(要求・受領証で UNKNOWN にならない)。"""
    assert _evaluate(_valid_comments())["result"] == pf.PASS


def test_unrelated_comments_do_not_affect_the_result() -> None:
    comments = _valid_comments() + [
        _comment(3, "レビューコメントです。REQUEST_ID の話ではない。", "2026-01-01T00:20:00+00:00"),
        _comment(
            4, _transition_body("CONSUMED", request_id="OTHER-REQUEST"), "2026-01-01T00:21:00+00:00"
        ),
    ]

    assert _evaluate(comments)["result"] == pf.PASS


# --- 状態遷移の記録 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "broken_condition"),
    [
        ("CONSUMED", "NOT_CONSUMED"),
        ("REVOKED", "NOT_REVOKED"),
        ("EXECUTING", "NOT_EXECUTING_BY_OTHER"),
        ("EXPIRED", "NOT_EXPIRED"),
        ("INVALIDATED_BY_TARGET_CHANGE", "TARGET_MATCHES_AT_REQUEST_TIME"),
    ],
)
def test_transition_record_breaks_the_matching_condition(state: str, broken_condition: str) -> None:
    comments = _valid_comments() + [
        _comment(3, _transition_body(state), "2026-01-01T00:20:00+00:00")
    ]

    report = _evaluate(comments)

    assert report["result"] == pf.FAIL
    assert _by_id(report)[broken_condition]["result"] == pf.FAIL


def test_transition_states_are_a_terminal_or_executing_set() -> None:
    assert set(pf._CONDITION_BROKEN_BY_TRANSITION) == set(pf.TRANSITION_STATES)


# --- 取得(GitHub の GET のみ)----------------------------------------------------------------


def _api_item(comment_id: int, body: str) -> dict[str, Any]:
    return {
        "id": comment_id,
        "body": body,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def test_comment_reader_reads_every_page_and_only_gets() -> None:
    import json

    calls: list[list[str]] = []
    full_page = [_api_item(i, "x") for i in range(1, 101)]
    pages = {1: full_page, 2: [_api_item(101, "y")]}

    def fake_run(args: list[str]) -> str:
        calls.append(args)
        page = int(re.search(r"&page=(\d+)", args[1]).group(1))  # type: ignore[union-attr]
        return json.dumps(pages[page])

    comments = pf.make_comment_reader("owner/name", fake_run)(7)

    assert len(comments) == 101
    assert len(calls) == 2
    for args in calls:
        assert args[0] == "api"
        assert not {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"} & set(args)


def test_comment_reader_failure_raises_instead_of_returning_empty() -> None:
    def failing_run(args: list[str]) -> str:
        raise pf.ReadError("network down")

    with pytest.raises(pf.ReadError):
        pf.make_comment_reader("owner/name", failing_run)(7)


def test_unreadable_comments_make_the_report_unknown() -> None:
    def failing_reader(number: int) -> list[pf.Comment]:
        raise pf.ReadError("network down")

    report = pf.check(
        request_id=_REQUEST_ID,
        source=1,
        expected=_EXPECTED,
        read_comments=failing_reader,
        read_now=lambda: _NOW,
    )

    assert report["result"] == pf.UNKNOWN


def test_unreadable_github_time_makes_the_report_unknown() -> None:
    def failing_now() -> dt.datetime:
        raise pf.ReadError("no date header")

    report = pf.check(
        request_id=_REQUEST_ID,
        source=1,
        expected=_EXPECTED,
        read_comments=lambda n: _valid_comments(),
        read_now=failing_now,
    )

    assert report["result"] == pf.UNKNOWN


def test_now_reader_uses_the_github_date_header() -> None:
    raw = "HTTP/2.0 200 OK\nDate: Sat, 19 Sep 2026 02:29:30 GMT\nContent-Type: application/json\n"

    now = pf.make_now_reader(lambda args: raw)()

    assert now == dt.datetime(2026, 9, 19, 2, 29, 30, tzinfo=dt.UTC)


def test_now_reader_without_a_date_header_raises() -> None:
    with pytest.raises(pf.ReadError):
        pf.make_now_reader(lambda args: "HTTP/2.0 200 OK\n")()


def test_not_checked_is_always_reported_even_when_reading_fails() -> None:
    """PR #431 F2: 取得に失敗した報告でも、検査しなかった項目を常に明示する。"""

    def failing_reader(number: int) -> list[pf.Comment]:
        raise pf.ReadError("network down")

    report = pf.check(
        request_id=_REQUEST_ID,
        source=1,
        expected=_EXPECTED,
        read_comments=failing_reader,
        read_now=lambda: _NOW,
    )

    assert report["result"] == pf.UNKNOWN
    assert {n["id"] for n in report["not_checked"]} >= {
        "USER_DIRECT_TURN",
        "EXPLICIT_APPROVAL_INTENT",
        "MANAGER_SCOPE_CHECK",
        "VALID_UNTIL_TTL_CAP",
    }


def test_not_checked_is_reported_when_the_repository_cannot_be_resolved(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """PR #431 F2: repository を特定できない報告でも、検査しなかった項目を明示する。"""

    def failing_detect(*args: object, **kwargs: object) -> str:
        raise pf.ReadError("gh not found")

    monkeypatch.setattr(pf, "_detect_repo", failing_detect)

    code = pf.main(
        [
            "--request-id", _REQUEST_ID,
            "--source", "1",
            "--gate-type", _EXPECTED.gate_type,
            "--scope", _EXPECTED.scope,
            "--executor", _EXPECTED.executor,
            "--target-identity", _EXPECTED.target_identity,
            "--target-version", _EXPECTED.target_version,
        ]
    )  # fmt: skip

    out = capsys.readouterr().out
    assert code == pf.EXIT_UNKNOWN
    assert "USER_DIRECT_TURN" in out
    assert "MANAGER_SCOPE_CHECK" in out


def test_target_state_at_request_time_is_disclosed_as_not_fetched() -> None:
    """PR #431 F3: TARGET_MATCHES_AT_REQUEST_TIME は記録同士の一致だけを見ており、
    依頼時点の実際の対象の状態は参照していない。PASS を「対象を確かめた」と読ませない。"""
    report = _evaluate(_valid_comments())

    reasons = {n["id"]: n["reason"] for n in report["not_checked"]}
    assert "ACTUAL_TARGET_STATE_AT_REQUEST_TIME" in reasons
    assert "実際の対象" in reasons["ACTUAL_TARGET_STATE_AT_REQUEST_TIME"]
    assert "記録同士" in _by_id(report)["TARGET_MATCHES_AT_REQUEST_TIME"]["detail"]


# --- exit code の三値 ------------------------------------------------------------------------


def test_exit_codes_keep_the_three_values_apart() -> None:
    assert pf.EXIT_PASS == 0
    assert pf.EXIT_FAIL == 1
    assert pf.EXIT_CLI_USAGE_ERROR == 2  # argparse が使う。UNKNOWN に使わない
    assert pf.EXIT_UNKNOWN == 3
    assert pf._EXIT_CODE_BY_RESULT == {pf.PASS: 0, pf.FAIL: 1, pf.UNKNOWN: 3}


@pytest.mark.parametrize(
    ("comments", "now", "exit_code"),
    [
        (_valid_comments(), _NOW, pf.EXIT_PASS),
        (_valid_comments(RECEIPT_STATE="NOT_APPROVED"), _NOW, pf.EXIT_FAIL),
        (_valid_comments(), None, pf.EXIT_UNKNOWN),
    ],
)
def test_main_maps_the_result_to_the_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    comments: list[pf.Comment],
    now: dt.datetime | None,
    exit_code: int,
) -> None:
    def failing_now() -> dt.datetime:
        raise pf.ReadError("no date")

    monkeypatch.setattr(pf, "make_comment_reader", lambda repo: lambda n: comments)
    monkeypatch.setattr(pf, "make_now_reader", lambda: (lambda: now) if now else failing_now)

    code = pf.main(
        [
            "--request-id", _REQUEST_ID,
            "--source", "9999",
            "--repo", "owner/name",
            "--gate-type", _EXPECTED.gate_type,
            "--scope", _EXPECTED.scope,
            "--executor", _EXPECTED.executor,
            "--target-identity", _EXPECTED.target_identity,
            "--target-version", _EXPECTED.target_version,
        ]
    )  # fmt: skip

    assert code == exit_code
    assert "証拠ではない" in capsys.readouterr().out


# --- read-only(静的な確認)--------------------------------------------------------------------


def test_script_source_contains_no_write_operations() -> None:
    """承認の検査が書き込みを行わない(GitHub の書き込み・git・ファイルの書き込みを持たない)。"""
    source = (_REPO_ROOT / "scripts" / "human_gate_preflight.py").read_text(encoding="utf-8")
    # 実装部分(docstring を除く)を対象にする
    code = source.split('"""', 2)[2]
    for forbidden in (
        '"-X"', '"--method"', '"POST"', '"PATCH"', '"PUT"', '"DELETE"',
        '"issue", "comment"', '"pr", "merge"', '"git"', "write_text", "open(", ".unlink(", "shutil",
    ):  # fmt: skip
        assert forbidden not in code, forbidden


# --- docs との一致(正本は docs)---------------------------------------------------------------


def _doc(name: str) -> str:
    return (_REPO_ROOT / "docs" / name).read_text(encoding="utf-8").replace("\r\n", "\n")


def test_seventeen_conditions_match_the_protocol() -> None:
    text = _doc("user_manager_collaboration_protocol.md")
    block = re.search(r"HUMAN_GATE_VALID =\n(.*?)\n```", text, re.S)
    assert block is not None
    names = re.findall(r"^(?:AND )?\s*([A-Z_]+)\s*$", block.group(1), re.M)

    assert tuple(names) == pf.HUMAN_GATE_VALID_CONDITIONS


def test_transition_states_match_the_protocol() -> None:
    text = _doc("user_manager_collaboration_protocol.md")
    section = text.split("### 状態遷移", 1)[1].split("###", 1)[0]

    for state in pf.TRANSITION_STATES:
        assert state in section, state
    for created_state in ("APPROVED", "NOT_APPROVED"):
        assert created_state not in pf.TRANSITION_STATES


def _contract_block(header: str) -> str:
    text = _doc("ai_operation_message_contract.md")
    match = re.search(rf"```\n{header}\n(.*?)\n```", text, re.S)
    assert match is not None, header
    return match.group(1)


def test_request_and_receipt_fields_match_the_contract() -> None:
    request_keys = re.findall(r"^([A-Z_]+)\s*=", _contract_block("APPROVAL_REQUEST"), re.M)
    receipt_keys = re.findall(r"^([A-Z_]+)\s*=", _contract_block("APPROVAL_RECEIPT"), re.M)

    assert tuple(request_keys) == pf.REQUEST_FIELDS
    assert tuple(receipt_keys) == pf.RECEIPT_FIELDS


def test_gate_types_match_the_contract() -> None:
    text = _doc("ai_operation_message_contract.md")
    section = text.split("#### 8.6.4 GATE_TYPE の値集合", 1)[1].split("#### 8.6.5", 1)[0]
    identifiers = set(re.findall(r"\b[A-Z][A-Z_]*_GATE\b", section))

    assert identifiers == set(pf.GATE_TYPES)


def test_receipt_state_rule_matches_the_contract() -> None:
    text = _doc("ai_operation_message_contract.md")
    section = text.split("RECEIPT_STATE(APPROVAL_DECISION と必ず一致させる)", 1)[1][:600]

    assert "APPROVE          ->  RECEIPT_STATE = APPROVED" in section
    assert "HOLD / REJECT    ->  RECEIPT_STATE = NOT_APPROVED" in section
    assert pf.RECEIPT_STATE_BY_DECISION == {
        "APPROVE": "APPROVED",
        "HOLD": "NOT_APPROVED",
        "REJECT": "NOT_APPROVED",
    }
