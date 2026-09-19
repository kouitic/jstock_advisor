"""Human Gate の承認要求・受領証を検査する read-only の preflight(Issue #332 Unit 1-B)。

実行者が gated action(merge / Issue close / ChangeSet EXECUTE 等)の**直前に**呼ぶ。
GitHub 上の APPROVAL_REQUEST と APPROVAL_RECEIPT を再読し、
docs/user_manager_collaboration_protocol.md 2.7節の HUMAN_GATE_VALID のうち機械で検査できる
条件と、preflight の検査項目を検査する。

    python scripts/human_gate_preflight.py --request-id <ID> --source 332 \\
        --gate-type ISSUE_CLOSE_GATE --scope "..." --executor DEVELOPER \\
        --target-identity "Issue #332" --target-version "STATE_ID ..."

## 本スクリプトが保証しないこと(最重要)

**preflight を通した(PASS)ことは、承認が USER 本人のものであることの証拠ではない。**
検査するのは**形式**(要求と受領証の存在・一致・時系列・未編集・状態)だけである。
次は機械では検査できず、本スクリプトは検査しない(結果の `not_checked` に明示する)。

    USER_DIRECT_TURN            USER 本人の直接の入力であったか(実行者の申告に依存。R-1・R-6)
    EXPLICIT_APPROVAL_INTENT    承認の意思が明示的であったか

全 AI セッションが同一の GitHub アカウントで投稿するため、受領証の author / comment の投稿者は
USER 本人の証拠にならない(protocol 2.7節 R-7)。本スクリプトはそれを解決しない。

## read-only であること

GitHub の GET(`gh api`)だけを行う。ファイル・GitHub・状態を一切書き換えない。
hook ではなく、実行者が呼ぶスクリプトである。CI・governance・policy_check へ接続しない
(hook への統合は Issue #332 の Unit 3 = 別 Issue)。

## 三値を厳格に区別する

    PASS     検査した項目がすべて成立した
    FAIL     1 つでも満たさない
    UNKNOWN  判定に必要な情報が無い / 取得できない / 解釈できない

**UNKNOWN を PASS へ倒さない。** 1 つでも満たさない、または判定できない場合は fail-close とする
(protocol 2.7節)。exit code も三値を保つ(scripts/policy_check.py と同じ契約)。

    0  PASS
    1  FAIL
    2  CLI usage / argument error(argparse が使う)
    3  UNKNOWN

## 時刻の権威

GitHub が付与する created_at / updated_at と、GitHub 側の現在時刻(API の Date ヘッダ)を正とする。
実行者のローカル時計・受領証本文の REQUESTED_AT / RECEIVED_AT(参考値)は判定に使わない。

## 検査しないもの(未実装・未決定。黙って省略せず、結果に明示する)

    MANAGER_SCOPE_CHECK   v3(#332 issuecomment-5737842742)§6(b)の検査項目だが、
                          内容が定義されていない。
                          TODO: #332 の該当箇所が定義された時点で、別の変更として実装する。
    VALID_UNTIL_TTL_CAP   標準の TTL より長い VALID_UNTIL を許すかは未決定(USER の決定を要する)。
                          TODO: 決定後に実装する。現状は VALID_UNTIL の経過だけを検査する。

## 状態遷移の追記記録(書式が contract に未定義)

contract 8.6.2節は、RECEIPT 作成後の状態遷移を「RECEIPT を編集せず、同じ REQUEST_ID を持つ
追記の記録として残す」と定めるが、その書式を定義していない。本スクリプトは、次の形の記録を状態遷移の記録として認識する
(**この形は本スクリプトの仮定であり、contract に定義が無い。発効前に確認が必要**)。

    APPROVAL_TRANSITION
    REQUEST_ID    = <対応する REQUEST_ID>
    RECEIPT_STATE = <EXECUTING | CONSUMED | EXPIRED | REVOKED | INVALIDATED_BY_TARGET_CHANGE>

同じ REQUEST_ID を本文に含む**行**が、その REQUEST_ID を持つ記録(要求・受領証・上の形)の一部として
解釈できない場合は(判定は comment 単位ではなく行単位。同じ comment に別の正常な記録があっても、
解釈できない言及は見逃さない。REQUEST_ID の誤記された遷移記録に、地の文で正しい ID が書かれている
場合を含む)、状態を判定できないため UNKNOWN とする(記録の書式の違いを PASS にしない)。
副作用として、地の文で REQUEST_ID に言及しただけの行があっても UNKNOWN になる(fail-close の側)。

## 承認依頼・受領証を書く人へ(偽陽性を避けるための書き方)

**REQUEST_ID は、必ず APPROVAL_REQUEST / APPROVAL_RECEIPT の block の中
(`REQUEST_ID = ...` の行)に書く。block の外の地の文では REQUEST_ID に言及しない。**
block の外の行に REQUEST_ID を書くと、その行は「記録として解釈できない言及」になり、
正しい承認でも UNKNOWN になる(fail-close の側であり、安全性の欠陥ではない)。
たとえば受領証の comment に「承認 <ID> を受領しました。」という1行を添えず、block だけを書く。
別の承認を指したいとき(「旧 <ID> は消費済みのため再依頼」等)も、
block の SCOPE 欄などに REQUEST_ID を書かない。

## 依存

標準ライブラリと gh CLI のみ。
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import re
import subprocess
import sys
from collections.abc import Callable
from typing import Any

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

EXIT_PASS = 0
EXIT_FAIL = 1
# 2 は argparse が引数不正で使う。ここからは返さない(契約を分離するため)。
EXIT_CLI_USAGE_ERROR = 2
EXIT_UNKNOWN = 3

_EXIT_CODE_BY_RESULT = {PASS: EXIT_PASS, FAIL: EXIT_FAIL, UNKNOWN: EXIT_UNKNOWN}

DISCLAIMER = (
    "本結果は形式の検査であり、承認が USER 本人のものであることの証拠ではない。"
    "PASS を真正性の保証として扱ってはならない(protocol 2.7節)。"
)

# --- docs(contract 8.6 / protocol 2.7節)と一致させる値集合 ------------------------------
REQUEST_HEADER = "APPROVAL_REQUEST"
RECEIPT_HEADER = "APPROVAL_RECEIPT"
TRANSITION_HEADER = "APPROVAL_TRANSITION"  # 仮定(モジュール docstring 参照)

REQUEST_FIELDS = (
    "REQUEST_ID",
    "GATE_TYPE",
    "SCOPE",
    "EXECUTOR",
    "TARGET_IDENTITY",
    "TARGET_VERSION",
    "ISSUE_OR_PR",
    "REQUESTED_AT",
    "VALID_UNTIL",
    "APPROVAL_USE",
)
RECEIPT_FIELDS = (
    "REQUEST_ID",
    "APPROVAL_DECISION",
    "RECEIPT_CHANNEL",
    "APPROVAL_SUMMARY",
    "TARGET_IDENTITY",
    "TARGET_VERSION",
    "RECEIVED_AT",
    "RECEIPT_STATE",
)

GATE_TYPES = frozenset(
    {
        "DESIGN_GATE",
        "MERGE_GATE",
        "PRODUCTION_CHANGESET_CREATE_GATE",
        "PRODUCTION_CHANGESET_EXECUTE_GATE",
        "ROLLBACK_GATE",
        "RELEASE_BLOCKER_REMOVAL_GATE",
        "ACTIVATION_GATE",
        "IAM_CHANGE_GATE",
        "DESTRUCTIVE_DELETE_GATE",
        "PRODUCTION_LAMBDA_INVOKE_GATE",
        "ISSUE_CLOSE_GATE",
    }
)
APPROVAL_USES = frozenset({"SINGLE_ATTEMPT", "BOUNDED_RETRY"})
APPROVAL_DECISIONS = frozenset({"APPROVE", "HOLD", "REJECT"})
RECEIPT_CHANNELS = frozenset({"DIRECT_INPUT", "EXECUTOR_ISSUED_CONFIRMATION"})
# RECEIPT の作成時点の値(contract 8.6.2節。APPROVAL_DECISION と必ず一致させる)。
RECEIPT_STATE_BY_DECISION = {
    "APPROVE": "APPROVED",
    "HOLD": "NOT_APPROVED",
    "REJECT": "NOT_APPROVED",
}
# 状態遷移(protocol 2.7節)。作成時点の値(APPROVED / NOT_APPROVED)は含めない。
TRANSITION_STATES = frozenset(
    {"EXECUTING", "CONSUMED", "EXPIRED", "REVOKED", "INVALIDATED_BY_TARGET_CHANGE"}
)
# 遷移状態 -> その状態があるとき成立しなくなる HUMAN_GATE_VALID の条件
_CONDITION_BROKEN_BY_TRANSITION = {
    "EXECUTING": "NOT_EXECUTING_BY_OTHER",
    "CONSUMED": "NOT_CONSUMED",
    "EXPIRED": "NOT_EXPIRED",
    "REVOKED": "NOT_REVOKED",
    "INVALIDATED_BY_TARGET_CHANGE": "TARGET_MATCHES_AT_REQUEST_TIME",
}

# HUMAN_GATE_VALID の 17 条件(protocol 2.7節)。checks か not_checked のどちらかに必ず現れる。
HUMAN_GATE_VALID_CONDITIONS = (
    "USER_DIRECT_TURN",
    "EXPLICIT_APPROVAL_INTENT",
    "APPROVAL_REQUEST_EXISTS",
    "RECEIPT_EXISTS",
    "REQUEST_ID_MATCHES",
    "GATE_TYPE_MATCHES",
    "SCOPE_MATCHES",
    "EXECUTOR_MATCHES",
    "TARGET_IDENTITY_MATCHES",
    "TARGET_VERSION_MATCHES",
    "RECEIVED_AT_AFTER_REQUESTED_AT",
    "TARGET_MATCHES_AT_REQUEST_TIME",
    "NOT_EXPIRED",
    "NOT_REVOKED",
    "NOT_CONSUMED",
    "NOT_EXECUTING_BY_OTHER",
    "RECEIPT_NOT_EDITED",
)
# 機械では検査できない条件(protocol 2.7節「17 条件との対応」)
NOT_MECHANICALLY_CHECKABLE = {
    "USER_DIRECT_TURN": (
        "機械では検査できない。USER 本人の直接の入力であったかは実行者の申告に依存する"
        "(残余リスク R-1・R-6)"
    ),
    "EXPLICIT_APPROVAL_INTENT": "機械では検査できない。承認の意思が明示的であったかは判定できない",
}
# HUMAN_GATE_VALID の外の検査項目
RECEIPT_STATE_MATCHES_DECISION = "RECEIPT_STATE_MATCHES_APPROVAL_DECISION"  # 【B】contract 8.6.2節
# 未実装・未決定(黙って省略しない)
NOT_IMPLEMENTED = {
    "MANAGER_SCOPE_CHECK": (
        "SKIPPED(未定義): v3 §6(b) の検査項目だが、内容が #332 に定義されていない。"
        "TODO: 定義された時点で別の変更として実装する"
    ),
    "ACTUAL_TARGET_STATE_AT_REQUEST_TIME": (
        "SKIPPED(未取得): TARGET_MATCHES_AT_REQUEST_TIME は、要求と受領証の記録同士の"
        "一致だけを見ている。"
        "依頼時点の実際の対象の状態は取得・参照していない"
    ),
    "VALID_UNTIL_TTL_CAP": (
        "SKIPPED(未決定): 標準の TTL より長い VALID_UNTIL を許すかが未決定(USER の決定を要する)。"
        "TODO: 決定後に実装する。現状は VALID_UNTIL の経過だけを検査する"
    ),
}


def _not_checked_items() -> list[dict[str, str]]:
    """検査しなかった項目(常に結果へ明示する。取得に失敗した報告にも含める)。"""
    return [
        {"id": cid, "reason": reason}
        for cid, reason in {**NOT_MECHANICALLY_CHECKABLE, **NOT_IMPLEMENTED}.items()
    ]


@dataclasses.dataclass(frozen=True)
class Comment:
    """GitHub の comment。時刻は GitHub が付与した値(時刻の権威)。"""

    comment_id: int
    body: str
    created_at: dt.datetime
    updated_at: dt.datetime


@dataclasses.dataclass(frozen=True)
class Block:
    kind: str  # REQUEST / RECEIPT / TRANSITION
    fields: dict[str, str]
    comment: Comment
    duplicate_keys: tuple[str, ...] = ()
    # comment 本文の行番号(0 始まり)のうち、この block が占める行(ヘッダ行 + `KEY = value` の行)
    line_indexes: frozenset[int] = frozenset()


@dataclasses.dataclass(frozen=True)
class Check:
    check_id: str
    result: str
    detail: str


@dataclasses.dataclass(frozen=True)
class Expected:
    """実行者が**これから実行する** gated action の識別子(承認と突き合わせる側)。"""

    gate_type: str
    scope: str
    executor: str
    target_identity: str
    target_version: str


# 「コメント一覧を返す」。取得に失敗した場合は None を返さず例外を送出する
# (「読めなかった」を「無い」に化けさせないため)。
CommentReader = Callable[[int], "list[Comment]"]
# GitHub 側の現在時刻。取得できなければ例外。
NowReader = Callable[[], dt.datetime]


class ReadError(Exception):
    """GitHub からの取得に失敗した(UNKNOWN の原因)。"""


_KEY_VALUE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$")
_HEADERS = {
    REQUEST_HEADER: "REQUEST",
    RECEIPT_HEADER: "RECEIPT",
    TRANSITION_HEADER: "TRANSITION",
}


def parse_timestamp(value: str) -> dt.datetime | None:
    """ISO 8601 の時刻(タイムゾーン付き)を返す。解釈できなければ None。"""
    try:
        parsed = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def parse_blocks(comment: Comment) -> list[Block]:
    """comment 本文から、ヘッダ行 + `KEY = value` の行の塊を取り出す。"""
    blocks: list[Block] = []
    lines = comment.body.splitlines()
    index = 0
    while index < len(lines):
        header = lines[index].strip()
        kind = _HEADERS.get(header)
        if kind is None:
            index += 1
            continue
        fields: dict[str, str] = {}
        duplicates: list[str] = []
        occupied = {index}
        index += 1
        while index < len(lines):
            match = _KEY_VALUE.match(lines[index].strip())
            if match is None:
                break
            key, value = match.group(1), match.group(2)
            if key in fields:
                duplicates.append(key)
            fields[key] = value
            occupied.add(index)
            index += 1
        blocks.append(Block(kind, fields, comment, tuple(duplicates), frozenset(occupied)))
    return blocks


def _is_unfilled(value: str) -> bool:
    """テンプレートの `<...>` のまま(未記入)の値。"""
    stripped = value.strip()
    return not stripped or (stripped.startswith("<") and stripped.endswith(">"))


def _missing_or_unfilled(block: Block, required: tuple[str, ...]) -> list[str]:
    return [k for k in required if k not in block.fields or _is_unfilled(block.fields[k])]


def _result_of(checks: list[Check]) -> str:
    """FAIL が1つでもあれば FAIL、無くて UNKNOWN が1つでもあれば UNKNOWN、それ以外は PASS。"""
    results = {c.result for c in checks}
    if FAIL in results:
        return FAIL
    if UNKNOWN in results or not checks:
        return UNKNOWN
    return PASS


def _uninterpretable_mentions(
    comments: list[Comment], blocks: list[Block], request_id: str
) -> list[tuple[int, int]]:
    """REQUEST_ID を含むが、**その REQUEST_ID を持つ記録**(block)の一部として解釈できない行。

    判定は comment 単位ではなく**行(記録)単位**である。同じ comment に別の REQUEST_ID の正常な
    block や、この REQUEST_ID の別の block が含まれていても、その comment 内の解釈できない言及
    (状態遷移の記録の誤記・書式違い・地の文)は見逃さない(Issue #332 PR #431 のレビュー指摘 F1)。
    (comment_id, 行番号(0 始まり))を返す。
    """
    covered: dict[int, set[int]] = {}
    for block in blocks:
        if block.fields.get("REQUEST_ID") == request_id:
            covered.setdefault(block.comment.comment_id, set()).update(block.line_indexes)
    found: list[tuple[int, int]] = []
    for comment in comments:
        for line_number, line in enumerate(comment.body.splitlines()):
            if request_id in line and line_number not in covered.get(comment.comment_id, set()):
                found.append((comment.comment_id, line_number))
    return found


def evaluate(
    comments: list[Comment],
    *,
    request_id: str,
    expected: Expected,
    now: dt.datetime | None,
) -> dict[str, Any]:
    """要求・受領証・追記記録から、preflight の結果(JSON 化できる dict)を組み立てる。

    ``now`` が None のとき(GitHub 側の現在時刻を取得できなかった)、有効期限は UNKNOWN とする。
    """
    all_blocks = [b for c in comments for b in parse_blocks(c)]
    requests = [
        b for b in all_blocks if b.kind == "REQUEST" and b.fields.get("REQUEST_ID") == request_id
    ]
    receipts = [
        b for b in all_blocks if b.kind == "RECEIPT" and b.fields.get("REQUEST_ID") == request_id
    ]
    transitions = [
        b for b in all_blocks if b.kind == "TRANSITION" and b.fields.get("REQUEST_ID") == request_id
    ]
    checks: list[Check] = []

    def add(check_id: str, result: str, detail: str) -> None:
        checks.append(Check(check_id, result, detail))

    # --- 存在 -------------------------------------------------------------------------
    request = requests[0] if len(requests) == 1 else None
    receipt = receipts[0] if len(receipts) == 1 else None
    if not requests:
        add(
            "APPROVAL_REQUEST_EXISTS", FAIL, "REQUEST_ID に対応する APPROVAL_REQUEST が見つからない"
        )
    elif len(requests) > 1:
        add(
            "APPROVAL_REQUEST_EXISTS",
            FAIL,
            f"同じ REQUEST_ID の APPROVAL_REQUEST が {len(requests)} 件ある(一意でない)",
        )
    else:
        problems = _missing_or_unfilled(requests[0], REQUEST_FIELDS) + list(
            requests[0].duplicate_keys
        )
        add(
            "APPROVAL_REQUEST_EXISTS",
            FAIL if problems else PASS,
            f"必須 field の欠落・未記入・重複: {problems}"
            if problems
            else "存在し、必須 field が揃っている",
        )
    if not receipts:
        add("RECEIPT_EXISTS", FAIL, "REQUEST_ID に対応する APPROVAL_RECEIPT が見つからない")
    elif len(receipts) > 1:
        add(
            "RECEIPT_EXISTS",
            FAIL,
            f"同じ REQUEST_ID の APPROVAL_RECEIPT が {len(receipts)} 件ある(一意でない)",
        )
    else:
        problems = _missing_or_unfilled(receipts[0], RECEIPT_FIELDS) + list(
            receipts[0].duplicate_keys
        )
        add(
            "RECEIPT_EXISTS",
            FAIL if problems else PASS,
            f"必須 field の欠落・未記入・重複: {problems}"
            if problems
            else "存在し、必須 field が揃っている",
        )

    req = request.fields if request is not None else {}
    rec = receipt.fields if receipt is not None else {}
    both = request is not None and receipt is not None

    # --- 【B】RECEIPT_STATE と APPROVAL_DECISION の一致(contract 8.6.2節) ---------------
    if receipt is None:
        add(RECEIPT_STATE_MATCHES_DECISION, UNKNOWN, "RECEIPT が無いため判定できない")
    else:
        decision, state = rec.get("APPROVAL_DECISION", ""), rec.get("RECEIPT_STATE", "")
        expected_state = RECEIPT_STATE_BY_DECISION.get(decision)
        if decision not in APPROVAL_DECISIONS:
            add(RECEIPT_STATE_MATCHES_DECISION, FAIL, f"APPROVAL_DECISION の値が不正: {decision!r}")
        elif state != expected_state:
            add(
                RECEIPT_STATE_MATCHES_DECISION,
                FAIL,
                f"APPROVAL_DECISION = {decision} なのに RECEIPT_STATE = {state!r}"
                f"(期待: {expected_state})。内部矛盾であり無効",
            )
        elif decision != "APPROVE":
            add(
                RECEIPT_STATE_MATCHES_DECISION,
                FAIL,
                f"APPROVAL_DECISION = {decision}。承認ではない",
            )
        else:
            add(RECEIPT_STATE_MATCHES_DECISION, PASS, "APPROVE と APPROVED が一致している")

    # --- 一致(要求 / 受領証 / これから実行する操作) ------------------------------------------
    def compare(
        check_id: str, request_value: str | None, receipt_value: str | None, want: str
    ) -> None:
        if not both:
            add(check_id, UNKNOWN, "要求または受領証が無いため判定できない")
            return
        if request_value != want:
            add(
                check_id,
                FAIL,
                "要求の値がこれから実行する操作と一致しない"
                f"(要求: {request_value!r} / 実行: {want!r})",
            )
        elif receipt_value is not None and receipt_value != request_value:
            add(
                check_id,
                FAIL,
                "受領証の値が要求と一致しない"
                f"(要求: {request_value!r} / 受領証: {receipt_value!r})",
            )
        else:
            add(check_id, PASS, "一致")

    if both and receipt is not None and rec.get("REQUEST_ID") != req.get("REQUEST_ID"):
        add("REQUEST_ID_MATCHES", FAIL, "受領証の REQUEST_ID が要求と一致しない")
    elif both:
        add("REQUEST_ID_MATCHES", PASS, "一致")
    else:
        add("REQUEST_ID_MATCHES", UNKNOWN, "要求または受領証が無いため判定できない")
    if req.get("GATE_TYPE") not in GATE_TYPES and both:
        add("GATE_TYPE_MATCHES", FAIL, f"GATE_TYPE の値が不正: {req.get('GATE_TYPE')!r}")
    else:
        compare("GATE_TYPE_MATCHES", req.get("GATE_TYPE"), None, expected.gate_type)
    compare("SCOPE_MATCHES", req.get("SCOPE"), None, expected.scope)
    compare("EXECUTOR_MATCHES", req.get("EXECUTOR"), None, expected.executor)
    compare(
        "TARGET_IDENTITY_MATCHES",
        req.get("TARGET_IDENTITY"),
        rec.get("TARGET_IDENTITY"),
        expected.target_identity,
    )
    compare(
        "TARGET_VERSION_MATCHES",
        req.get("TARGET_VERSION"),
        rec.get("TARGET_VERSION"),
        expected.target_version,
    )

    # --- 受領証の付加的な値の検査(値集合) ---------------------------------------------------
    if both:
        problems = []
        if req.get("APPROVAL_USE") not in APPROVAL_USES:
            problems.append(f"APPROVAL_USE={req.get('APPROVAL_USE')!r}")
        if rec.get("RECEIPT_CHANNEL") not in RECEIPT_CHANNELS:
            problems.append(f"RECEIPT_CHANNEL={rec.get('RECEIPT_CHANNEL')!r}")
        if problems:
            add("RECEIPT_EXISTS", FAIL, "値集合の外の値: " + ", ".join(problems))

    # --- 時系列(GitHub の created_at を正とする) ---------------------------------------------
    if both and request is not None and receipt is not None:
        after = receipt.comment.created_at > request.comment.created_at
        add(
            "RECEIVED_AT_AFTER_REQUESTED_AT",
            PASS if after else FAIL,
            "受領証の comment が要求の comment より後(GitHub の created_at)"
            if after
            else "受領証の comment が要求の comment より後ではない(GitHub の created_at)",
        )
        same = req.get("TARGET_IDENTITY") == rec.get("TARGET_IDENTITY") and req.get(
            "TARGET_VERSION"
        ) == rec.get("TARGET_VERSION")
        add(
            "TARGET_MATCHES_AT_REQUEST_TIME",
            PASS if same else FAIL,
            "受領証の対象・版が、要求の対象・版と一致(記録同士の比較のみ。"
            "依頼時点の実際の対象の状態は参照していない)"
            if same
            else "受領証の対象・版が、依頼時点(要求)の対象・版と一致しない",
        )
        edited = receipt.comment.created_at != receipt.comment.updated_at
        add(
            "RECEIPT_NOT_EDITED",
            FAIL if edited else PASS,
            "受領証の comment が編集されている(created_at != updated_at。R-10)"
            if edited
            else "created_at == updated_at(未編集)",
        )
    else:
        for cid in (
            "RECEIVED_AT_AFTER_REQUESTED_AT",
            "TARGET_MATCHES_AT_REQUEST_TIME",
            "RECEIPT_NOT_EDITED",
        ):
            add(cid, UNKNOWN, "要求または受領証が無いため判定できない")

    # --- 有効期限(GitHub 側の現在時刻を正とする) ---------------------------------------------
    valid_until = parse_timestamp(req["VALID_UNTIL"]) if "VALID_UNTIL" in req else None
    if request is None:
        add("NOT_EXPIRED", UNKNOWN, "要求が無いため判定できない")
    elif valid_until is None:
        add("NOT_EXPIRED", FAIL, f"VALID_UNTIL を解釈できない: {req.get('VALID_UNTIL')!r}")
    elif now is None:
        add(
            "NOT_EXPIRED",
            UNKNOWN,
            "GitHub 側の現在時刻を取得できない(実行者のローカル時計は使わない)",
        )
    elif now > valid_until:
        add("NOT_EXPIRED", FAIL, "VALID_UNTIL を過ぎている")
    else:
        add("NOT_EXPIRED", PASS, "VALID_UNTIL 以内")

    # --- 状態遷移の追記記録 ------------------------------------------------------------------
    unreadable = _uninterpretable_mentions(comments, all_blocks, request_id)
    transition_states = [t.fields.get("RECEIPT_STATE", "") for t in transitions]
    invalid_states = [s for s in transition_states if s not in TRANSITION_STATES]
    for state, condition in _CONDITION_BROKEN_BY_TRANSITION.items():
        if state in transition_states:
            add(
                condition,
                FAIL,
                f"状態遷移の記録がある: {state}"
                "(終端または実行中。再実行には新しい APPROVAL_REQUEST が要る)",
            )
    for condition in ("NOT_REVOKED", "NOT_CONSUMED", "NOT_EXECUTING_BY_OTHER"):
        if any(c.check_id == condition for c in checks):
            continue
        if invalid_states:
            add(condition, UNKNOWN, f"状態遷移の記録の RECEIPT_STATE の値が不正: {invalid_states}")
        elif unreadable:
            add(
                condition,
                UNKNOWN,
                "同じ REQUEST_ID を含むが、その REQUEST_ID を持つ記録として解釈できない行がある"
                "(状態遷移の記録の書式・REQUEST_ID の誤記の可能性): "
                + ", ".join(f"comment {cid} の {line + 1} 行目" for cid, line in unreadable),
            )
        elif request is None:
            add(condition, UNKNOWN, "要求が無いため判定できない")
        else:
            add(condition, PASS, "状態遷移の記録なし(認識できる書式の範囲)")

    checked_ids = {c.check_id for c in checks}
    not_checked = _not_checked_items()
    # 17 条件は checks か not_checked のどちらかに現れる(黙って省略しない)。
    missing = [
        c
        for c in HUMAN_GATE_VALID_CONDITIONS
        if c not in checked_ids and c not in NOT_MECHANICALLY_CHECKABLE
    ]
    if missing:  # pragma: no cover - 上の実装が 17 条件を網羅していることの内部検査
        add("INTERNAL_CONDITION_COVERAGE", UNKNOWN, f"検査していない条件がある: {missing}")

    # 同一 check_id が複数回追加された場合(値集合の検査等)は、最も悪い結果を残す。
    merged: dict[str, Check] = {}
    order = {PASS: 0, UNKNOWN: 1, FAIL: 2}
    for check in checks:
        prior = merged.get(check.check_id)
        if prior is None or order[check.result] > order[prior.result]:
            merged[check.check_id] = check
    final = list(merged.values())
    return {
        "result": _result_of(final),
        "request_id": request_id,
        "checks": [dataclasses.asdict(c) for c in final],
        "not_checked": not_checked,
        "disclaimer": DISCLAIMER,
    }


# --- GitHub からの取得(read-only。GET のみ) ------------------------------------------------
Runner = Callable[[list[str]], str]


def _run_gh(args: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["gh", *args], capture_output=True, text=True, encoding="utf-8", check=True, timeout=60
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise ReadError(f"gh の実行に失敗した: {exc}") from exc
    return completed.stdout


def make_comment_reader(repo: str, run: Runner = _run_gh) -> CommentReader:
    """Issue / PR の comment を全ページ読む reader(GET のみ)。"""

    def read(number: int) -> list[Comment]:
        comments: list[Comment] = []
        page = 1
        while True:
            raw = run(["api", f"repos/{repo}/issues/{number}/comments?per_page=100&page={page}"])
            try:
                items = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ReadError(f"GitHub の応答を解釈できない: {exc}") from exc
            if not isinstance(items, list):
                raise ReadError("GitHub の応答が一覧ではない")
            for item in items:
                created = parse_timestamp(str(item.get("created_at", "")))
                updated = parse_timestamp(str(item.get("updated_at", "")))
                if created is None or updated is None:
                    raise ReadError("comment の時刻を解釈できない")
                comments.append(
                    Comment(int(item["id"]), str(item.get("body") or ""), created, updated)
                )
            if len(items) < 100:
                return comments
            page += 1

    return read


def make_now_reader(run: Runner = _run_gh) -> NowReader:
    """GitHub 側の現在時刻(API 応答の Date ヘッダ)。実行者のローカル時計は使わない。"""

    def read() -> dt.datetime:
        raw = run(["api", "-i", "rate_limit"])
        for line in raw.splitlines():
            if line.lower().startswith("date:"):
                try:
                    parsed = email_date_to_datetime(line.split(":", 1)[1].strip())
                except (ValueError, TypeError) as exc:
                    raise ReadError(f"GitHub の Date ヘッダを解釈できない: {exc}") from exc
                return parsed
        raise ReadError("GitHub の応答に Date ヘッダが無い")

    return read


def email_date_to_datetime(value: str) -> dt.datetime:
    from email.utils import parsedate_to_datetime

    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        raise ValueError("タイムゾーンが無い")
    return parsed


def check(
    *,
    request_id: str,
    source: int,
    expected: Expected,
    read_comments: CommentReader,
    read_now: NowReader,
) -> dict[str, Any]:
    """GitHub から読んで evaluate する。読めなかった場合は UNKNOWN(PASS にしない)。"""
    try:
        comments = read_comments(source)
    except ReadError as exc:
        return {
            "result": UNKNOWN,
            "request_id": request_id,
            "checks": [
                dataclasses.asdict(
                    Check("READ_COMMENTS", UNKNOWN, f"comment を取得できない: {exc}")
                )
            ],
            "not_checked": _not_checked_items(),
            "disclaimer": DISCLAIMER,
        }
    try:
        now: dt.datetime | None = read_now()
    except ReadError:
        now = None
    return evaluate(comments, request_id=request_id, expected=expected, now=now)


def _detect_repo(run: Runner = _run_gh) -> str:
    raw = run(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"])
    return raw.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Human Gate の承認要求・受領証を検査する read-only の preflight"
        "(Issue #332 Unit 1-B)。"
        "PASS は承認の真正性の証拠ではない(形式の検査のみ)。",
        epilog="承認依頼・受領証を書く人へ: REQUEST_ID は必ず APPROVAL_REQUEST / "
        "APPROVAL_RECEIPT の block の中に書き、block の外の地の文では言及しない"
        "(block の外の行に書くと、正しい承認でも UNKNOWN になる)。",
    )
    parser.add_argument("--request-id", required=True)
    parser.add_argument(
        "--source", type=int, required=True, help="要求と受領証が書かれた Issue / PR の番号"
    )
    parser.add_argument(
        "--repo", default=None, help="owner/name(省略時は gh が現在の repository を解決する)"
    )
    parser.add_argument("--gate-type", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--executor", required=True)
    parser.add_argument("--target-identity", required=True)
    parser.add_argument("--target-version", required=True)
    args = parser.parse_args(argv)

    expected = Expected(
        args.gate_type, args.scope, args.executor, args.target_identity, args.target_version
    )
    try:
        repo = args.repo or _detect_repo()
    except ReadError as exc:
        report = {
            "result": UNKNOWN,
            "request_id": args.request_id,
            "checks": [
                dataclasses.asdict(
                    Check("READ_REPOSITORY", UNKNOWN, f"repository を特定できない: {exc}")
                )
            ],
            "not_checked": _not_checked_items(),
            "disclaimer": DISCLAIMER,
        }
    else:
        report = check(
            request_id=args.request_id,
            source=args.source,
            expected=expected,
            read_comments=make_comment_reader(repo),
            read_now=make_now_reader(),
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return _EXIT_CODE_BY_RESULT.get(report["result"], EXIT_UNKNOWN)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
