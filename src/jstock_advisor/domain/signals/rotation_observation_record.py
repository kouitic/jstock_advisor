"""資金入替の観測の『日の集計』の記録の形(Issue #604 P-b1。#128 R2。dormant)。

`rotation_observation.CapitalRotationObservation` ⇄ 記録の dict
(AuditLog の `output_values` に載せる形)。
書込・読取の I/O は持たない(P-b3 が注入された port で書く)。**時計・I/O・AWS・LINE に触れない**。
どこからも import されない(dormant)。

## 記録に載せるもの・載せないもの(rev4 差分 1 = X)

載せる: 対象日・日の状態・バッチの状態と時刻(JST)・保有の件数・適格性 3 値と
理由コード 13 種ごとの件数・
読取の失敗の件数。載せない(★ 禁止 ★): owner・保有 ID・銘柄コード・数量・金額・Recommendation ID・
鍵・ARN・Account ID。検証は『キーの欠落も余りも拒否』する閉じた形で、禁止の語を持つキーも拒否する。

## schema_version の規約(★ 語彙が増えたら版を上げる ★)

記録は P-a の理由コード(`EligibilityReason`)13 種を、0 も含めて**全部**持つ。検証が『キーの欠落も
余りも拒否』する形のため、語彙の追加・削除は**非破壊のキー追加にならない**(古い記録が新しい検証を
通らない・新しい記録が古い検証を通らない)。したがって P-a の語彙が変わったら、
★ `RECORD_SCHEMA_VERSION` を上げ、`_SCHEMA_VOCABULARY` に新しい版の語彙を足す ★
(古い版の記録を読む必要が出たら、その版の読み方を足す。今は版 1 だけ)。
契約テストが、現在の `EligibilityReason` と `_SCHEMA_VOCABULARY[RECORD_SCHEMA_VERSION]` の一致を
固定する(P-a の語彙を変えると落ちる = 版を上げることを忘れない)。同じ規約を #603 の契約
(`C603-v1.3`)も採っている。

## 記録の ID(決定的。日付だけ。先に書いた回が勝つ)

* `capital_rotation_observation:{YYYY-MM-DD}` = ★ `OBSERVED` の日だけ ★(日を固定する)
* `capital_rotation_observation_gap:{YYYY-MM-DD}` = それ以外の日の状態
  (完了前・読取の失敗・上限超過)。
  一時的な状態の可能性があるため、日を固定する観測の ID を使わない(先勝ちで、不完全な記録が日を
  固定しない)。同じ日に両方があるときは `OBSERVED` の記録が優先する(読み手の規則)。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any, Final

from jstock_advisor.domain.jst import to_jst
from jstock_advisor.domain.signals.rotation_observation import (
    ELIGIBILITY_VALUES,
    BatchCompletionState,
    CapitalRotationObservation,
    DayStatus,
    RotationObservationError,
)
from jstock_advisor.domain.signals.rotation_source_eligibility import EligibilityReason

RECORD_SCHEMA_VERSION: Final = 1
RECORD_KIND: Final = "capital_rotation_observation"
CONTRACT_VERSION: Final = "P604-v1"
OBSERVATION_ID_PREFIX: Final = "capital_rotation_observation"
GAP_ID_PREFIX: Final = "capital_rotation_observation_gap"

#: 版ごとの理由コードの語彙(★ 語彙が増減したら版を上げ、ここへ足す ★)。
#: 契約テストが現在の語彙と突き合わせる
_SCHEMA_VOCABULARY: Final[Mapping[int, frozenset[str]]] = {
    1: frozenset(
        {
            "RECORD_MISSING",
            "OUTSIDE_WINDOW",
            "CATEGORY_UNKNOWN",
            "EVALUATION_NOT_COMPLETED",
            "ENGINE_UNKNOWN",
            "ENGINE_NOT_AUTHORITATIVE",
            "RECOMMENDATION_MISSING",
            "TYPE_UNCLASSIFIED",
            "EXIT_RECOMMENDATION",
            "REVIEW_RECOMMENDATION",
            "ATTENTION",
            "NOTIFIED_OR_SUPPRESSED",
            "NO_EXIT_EVIDENCE",
        }
    )
}

_TOP_KEYS: Final = frozenset(
    {
        "schema_version",
        "record_kind",
        "contract_version",
        "target_date",
        "day_status",
        "batch",
        "holdings_total",
        "eligibility",
        "reasons",
        "unreadable",
    }
)
_BATCH_KEYS: Final = frozenset({"state", "started_at", "completed_at", "window_seconds"})
_UNREADABLE_KEYS: Final = frozenset({"record_read_failed", "recommendation_read_failed"})

#: 記録のキー・値に現れてはならない語(識別子・資産・秘密)。閉じたキー集合に加えた二重の網
FORBIDDEN_KEY_WORDS: Final = (
    "owner",
    "holding_id",
    "stock_code",
    "code",
    "shares",
    "quantity",
    "amount",
    "price",
    "recommendation_id",
    "secret",
    "arn",
    "account",
    "key",
)


class RotationRecordError(ValueError):
    """記録の dict が契約に反する。値を含めない(キー名・値を例外へ載せない)。"""


def _fail(problem: str) -> RotationRecordError:
    return RotationRecordError(problem)


def audit_id_for(observation: CapitalRotationObservation) -> str:
    """決定的な記録の ID。OBSERVED だけが日を固定する ID を使う(上の『記録の ID』)。"""
    prefix = (
        OBSERVATION_ID_PREFIX if observation.day_status is DayStatus.OBSERVED else GAP_ID_PREFIX
    )
    return f"{prefix}:{observation.target_date.isoformat()}"


def _iso_jst(moment: dt.datetime | None) -> str | None:
    return None if moment is None else to_jst(moment).isoformat()


def to_output_values(observation: CapitalRotationObservation) -> dict[str, Any]:
    """観測 → 記録の dict(AuditLog の output_values)。識別子・資産・秘密を持たない閉じた形。"""
    if not isinstance(observation, CapitalRotationObservation):
        raise _fail("observation must be a CapitalRotationObservation")
    values: dict[str, Any] = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "record_kind": RECORD_KIND,
        "contract_version": CONTRACT_VERSION,
        "target_date": observation.target_date.isoformat(),
        "day_status": observation.day_status.value,
        "batch": {
            "state": observation.batch_state.value,
            "started_at": _iso_jst(observation.batch_started_at),
            "completed_at": _iso_jst(observation.batch_completed_at),
            "window_seconds": observation.window_seconds,
        },
        "holdings_total": observation.holdings_total,
        "eligibility": {value: observation.eligibility[value] for value in ELIGIBILITY_VALUES},
        "reasons": {
            reason.value: observation.reasons[reason.value]
            for reason in sorted(EligibilityReason, key=lambda r: r.value)
        },
        "unreadable": {
            "record_read_failed": observation.record_read_failed,
            "recommendation_read_failed": observation.recommendation_read_failed,
        },
    }
    assert_no_forbidden_words(values)
    return values


def assert_no_forbidden_words(values: object) -> None:
    """キー名に禁止の語を含まない(再帰)。値は載せない。"""
    if isinstance(values, Mapping):
        for key, value in values.items():
            lowered = str(key).lower()
            if any(word in lowered for word in FORBIDDEN_KEY_WORDS):
                raise _fail("a forbidden word appears in a key")
            assert_no_forbidden_words(value)
    elif isinstance(values, (list, tuple)):
        for item in values:
            assert_no_forbidden_words(item)


def _need_mapping(value: object, keys: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise _fail("the record has missing or extra keys")
    return value


def _parse_moment(value: object) -> dt.datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _fail("a time must be an ISO string or null")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        raise _fail("a time is not ISO 8601") from None
    if parsed.tzinfo is None:
        raise _fail("a time must carry an offset")
    return parsed


def _parse_enum[E](enum_type: type[E], value: object) -> E:
    try:
        return enum_type(value)  # type: ignore[call-arg]
    except ValueError:
        raise _fail("a value is outside its closed vocabulary") from None


def observation_from_output_values(values: object) -> CapitalRotationObservation:
    """記録の dict → 観測。★ 厳格 ★: キーの欠落も余りも・語彙外・型違い・件数の不整合を拒否する。"""
    top = _need_mapping(values, _TOP_KEYS)
    assert_no_forbidden_words(top)
    version = top["schema_version"]
    if isinstance(version, bool) or version not in _SCHEMA_VOCABULARY:
        raise _fail("unsupported schema_version")
    if top["record_kind"] != RECORD_KIND or top["contract_version"] != CONTRACT_VERSION:
        raise _fail("unexpected record kind or contract version")
    batch = _need_mapping(top["batch"], _BATCH_KEYS)
    reasons = top["reasons"]
    if not isinstance(reasons, Mapping) or set(reasons) != _SCHEMA_VOCABULARY[version]:
        raise _fail("the reason keys do not match the schema vocabulary")
    unreadable = _need_mapping(top["unreadable"], _UNREADABLE_KEYS)
    target = top["target_date"]
    if not isinstance(target, str):
        raise _fail("target_date must be an ISO date string")
    try:
        target_date = dt.date.fromisoformat(target)
    except ValueError:
        raise _fail("target_date is not an ISO date") from None
    try:
        observation = CapitalRotationObservation(
            target_date=target_date,
            batch_state=_parse_enum(BatchCompletionState, batch["state"]),
            batch_started_at=_parse_moment(batch["started_at"]),
            batch_completed_at=_parse_moment(batch["completed_at"]),
            holdings_total=top["holdings_total"],
            eligibility=top["eligibility"],
            reasons=reasons,
            record_read_failed=unreadable["record_read_failed"],
            recommendation_read_failed=unreadable["recommendation_read_failed"],
            day_status=_parse_enum(DayStatus, top["day_status"]),
        )
    except RotationObservationError:
        raise _fail("the record is inconsistent") from None
    if batch["window_seconds"] != observation.window_seconds:
        raise _fail("window_seconds does not match the batch times")
    return observation
