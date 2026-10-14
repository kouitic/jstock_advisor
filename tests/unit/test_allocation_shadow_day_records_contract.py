"""Issue #925 kv1 設計 rev2(MUST-2)/ #603: 日単位の記録(索引・skip)の契約テスト。

固定するもの:
  - 索引: 記録が実際に存在する owner の符号(kv1)だけを載せる。件数の整合
    (attempted = recorded + unrecorded / unrecorded = 理由別の合計 / recorded = 符号の数)
    を builder が計算し、validate も検査する(部分で終わった日に CLI が黙って落とさない)
  - 索引の上限 16・重複・kv0・平文・鍵の版の不一致・語彙外の理由を拒否
  - owner_refs_truncated は OWNER_CAP の件数と一致する
  - skip(鍵が取れない日): owner を持たない。理由コードと件数だけ
  - audit_id は決定的で、owner 単位の記録の audit_id と衝突しない
  - allowlist は全階層を列挙(未知・不足のキーを拒否)。owner の平文・鍵は記録に出ない
  - builder は I/O・時計・ログに触れない(純粋)
"""

from __future__ import annotations

import ast
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.identifier_ref import (
    KeyedOwnerRef,
    KeyedReferenceRequiredError,
    KeyMaterial,
    KeyUnavailableReason,
    keyed_owner_ref,
    owner_ref,
)
from jstock_advisor.domain.signals import allocation_shadow_record as rec
from jstock_advisor.domain.signals.allocation_shadow_record import (
    DAY_SKIP_REASONS,
    MAX_INDEX_OWNERS,
    UNRECORDED_REASONS,
    RecordSchemaError,
    build_day_skip_record,
    build_index_record,
    day_skip_audit_id,
    index_audit_id,
    items_audit_id,
    validate_day_skip_record,
    validate_index_record,
)
from jstock_advisor.services.allocation_shadow_service import ShadowSkipReason

_REPO = Path(__file__).resolve().parents[2]
_SIGNALS = _REPO / "src" / "jstock_advisor" / "domain" / "signals"
_MODULE_PATH = _SIGNALS / "allocation_shadow_record.py"

_KEY = KeyMaterial(key_version=1, key=b"t" * 32)  # テスト用の鍵(秘密ではない)
_DATE = dt.date(2026, 10, 14)
_BATCH = "buy-candidates-20261014"
_SENTINEL_OWNER = "sentinel-owner-zz"


def _ref(owner: str, key: KeyMaterial = _KEY) -> KeyedOwnerRef:
    return keyed_owner_ref(owner, key)


def _index(
    owners: list[str] | None = None,
    reasons: dict[str, int] | None = None,
    key_version: int = 1,
) -> dict[str, Any]:
    key = KeyMaterial(key_version=key_version, key=b"t" * 32)
    return build_index_record(
        business_date=_DATE,
        batch_id=_BATCH,
        shadow_mode="SHADOW",
        key_version=key_version,
        recorded=[_ref(o, key) for o in (owners if owners is not None else ["owner-a", "owner-b"])],
        unrecorded_reasons=reasons or {},
    )


# --- 索引の形と件数 -----------------------------------------------------------------------------


def test_the_index_lists_the_recorded_owners_and_counts_them() -> None:
    record = _index(["owner-b", "owner-a"])

    validate_index_record(record)
    assert json.loads(json.dumps(record)) == record
    assert record["schema_version"] == rec.SCHEMA_VERSION == 1
    assert record["record_kind"] == "allocation_shadow_index"
    assert record["business_date"] == "2026-10-14"
    assert record["batch_id"] == _BATCH
    assert record["key_version"] == 1
    assert record["owner_refs"] == sorted([_ref("owner-a"), _ref("owner-b")])
    assert record["recorded_owner_count"] == 2
    assert record["unrecorded_owner_count"] == 0
    assert record["attempted_owner_count"] == 2
    assert record["unrecorded_reasons"] == {}
    assert record["owner_refs_truncated"] is False


def test_unrecorded_owners_are_counted_by_reason_and_never_listed() -> None:
    """部分で終わった日(予算切れ・書込失敗)。記録できた owner だけを載せ、残りは件数で数える。"""
    record = _index(["owner-a"], {"BUDGET_EXHAUSTED": 2, "WRITE_FAILED": 1})

    validate_index_record(record)
    assert record["owner_refs"] == [_ref("owner-a")]
    assert record["recorded_owner_count"] == 1
    assert record["unrecorded_owner_count"] == 3
    assert record["attempted_owner_count"] == 4
    assert record["unrecorded_reasons"] == {"BUDGET_EXHAUSTED": 2, "WRITE_FAILED": 1}


def test_an_index_with_no_recorded_owner_is_a_valid_and_distinct_state() -> None:
    """『索引あり・記録 0 件』は『索引なし』と別の状態(CLI が 0 件と取り違えない根拠)。"""
    record = _index([], {"BUDGET_EXHAUSTED": 3})

    validate_index_record(record)
    assert record["owner_refs"] == []
    assert record["recorded_owner_count"] == 0
    assert record["attempted_owner_count"] == 3
    assert record["key_version"] == 1  # 符号が 0 件でも鍵の版を残す


def test_the_owner_cap_is_counted_and_flagged() -> None:
    owners = [f"owner-{n:02d}" for n in range(MAX_INDEX_OWNERS)]
    record = _index(owners, {"OWNER_CAP": 4})

    validate_index_record(record)
    assert len(record["owner_refs"]) == MAX_INDEX_OWNERS == 16
    assert record["owner_refs_truncated"] is True
    assert record["attempted_owner_count"] == 20
    assert _index(owners)["owner_refs_truncated"] is False


def test_zero_counts_are_not_recorded_as_reasons() -> None:
    record = _index(["owner-a"], {"BUDGET_EXHAUSTED": 0, "WRITE_FAILED": 1})

    assert record["unrecorded_reasons"] == {"WRITE_FAILED": 1}


def test_the_refs_are_sorted_so_the_record_does_not_depend_on_the_order_of_writes() -> None:
    assert _index(["owner-a", "owner-b"]) == _index(["owner-b", "owner-a"])


def test_every_listed_reason_is_a_code_in_the_vocabulary() -> None:
    assert set(UNRECORDED_REASONS) == {
        "BUDGET_EXHAUSTED",
        "KEY_UNAVAILABLE",
        "WRITE_FAILED",
        "OWNER_CAP",
        "REFUSED",
    }
    for code in UNRECORDED_REASONS:
        validate_index_record(_index(["owner-a"], {code: 1}))


# --- 索引の builder が拒否するもの ----------------------------------------------------------


def _build(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "business_date": _DATE,
        "batch_id": _BATCH,
        "shadow_mode": "SHADOW",
        "key_version": 1,
        "recorded": [_ref("owner-a")],
        "unrecorded_reasons": {},
    }
    args.update(overrides)
    return build_index_record(**args)


@pytest.mark.parametrize(
    "bad",
    [owner_ref("owner-a"), "owner-a", "kv1:" + "a" * 24, "", None],
)
def test_an_unkeyed_or_plain_reference_is_refused_without_echoing_it(bad: object) -> None:
    with pytest.raises(KeyedReferenceRequiredError) as caught:
        _build(recorded=[bad])

    assert not isinstance(bad, str) or not bad or bad not in str(caught.value)


def test_a_duplicate_reference_is_refused() -> None:
    with pytest.raises(RecordSchemaError):
        _build(recorded=[_ref("owner-a"), _ref("owner-a")])


def test_more_than_the_cap_is_refused() -> None:
    owners = [_ref(f"owner-{n:02d}") for n in range(MAX_INDEX_OWNERS + 1)]
    with pytest.raises(RecordSchemaError):
        _build(recorded=owners)


def test_a_reference_with_another_key_version_is_refused() -> None:
    other = _ref("owner-b", KeyMaterial(key_version=2, key=b"t" * 32))
    with pytest.raises(RecordSchemaError):
        _build(recorded=[_ref("owner-a"), other])
    with pytest.raises(RecordSchemaError):
        _build(recorded=[other], key_version=1)


@pytest.mark.parametrize("version", [0, -1, 1000, True, 1.0, "1", None])
def test_a_key_version_out_of_range_or_not_an_int_is_refused(version: object) -> None:
    with pytest.raises(RecordSchemaError):
        _build(recorded=[], key_version=version)


@pytest.mark.parametrize(
    "reasons",
    [
        {"UNKNOWN": 1},
        {"budget_exhausted": 1},
        {"BUDGET_EXHAUSTED": -1},
        {"BUDGET_EXHAUSTED": True},
        {"BUDGET_EXHAUSTED": 1.5},
        {"BUDGET_EXHAUSTED": "1"},
    ],
)
def test_a_reason_outside_the_vocabulary_or_a_bad_count_is_refused(reasons: dict[str, Any]) -> None:
    with pytest.raises(RecordSchemaError):
        _build(unrecorded_reasons=reasons)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("business_date", "2026-10-14"),
        ("business_date", dt.datetime(2026, 10, 14, 9, 0)),
        ("batch_id", "日本語のバッチ"),
        ("batch_id", "has space"),
        ("batch_id", ""),
        ("batch_id", "x" * 201),
        ("shadow_mode", "shadow"),
        ("shadow_mode", "日本語"),
    ],
)
def test_the_identifying_fields_are_checked(name: str, value: object) -> None:
    with pytest.raises(RecordSchemaError):
        _build(**{name: value})


# --- 索引の validate(改ざん・不整合の検出)---------------------------------------------------------


def _tampered(change: dict[str, Any]) -> dict[str, Any]:
    record = _index(["owner-a", "owner-b"], {"BUDGET_EXHAUSTED": 1})
    record.update(change)
    return record


@pytest.mark.parametrize(
    "change",
    [
        {"recorded_owner_count": 1},
        {"recorded_owner_count": 3},
        {"unrecorded_owner_count": 0},
        {"unrecorded_owner_count": 2},
        {"unrecorded_owner_count": 2, "attempted_owner_count": 4},  # 合計と理由別が合わない
        {"attempted_owner_count": 2},
        {"attempted_owner_count": 4},
        {"owner_refs_truncated": True},
        {"key_version": 2},
        {"key_version": 0},
        {"unrecorded_reasons": {"BUDGET_EXHAUSTED": 0}},
        {"unrecorded_reasons": {"NEW_REASON": 1}},
        {"owner_refs": [_ref("owner-a"), _ref("owner-a")]},
        {"owner_refs": [owner_ref("owner-a"), owner_ref("owner-b")]},
        {"owner_refs": ["owner-a", "owner-b"]},
        {"record_kind": "allocation_shadow_items"},
        {"schema_version": 2},
        {"batch_id": _BATCH + "\n"},  # 末尾の改行(`$` だけでは通ってしまう)
        {"business_date": "2026-10-14\n"},
        {"owner_refs": [_ref("owner-a") + "\n", _ref("owner-b")]},
    ],
)
def test_an_inconsistent_index_is_rejected(change: dict[str, Any]) -> None:
    with pytest.raises((RecordSchemaError, KeyedReferenceRequiredError)):
        validate_index_record(_tampered(change))


def test_the_index_allowlist_rejects_unknown_and_missing_keys() -> None:
    record = _index()
    with pytest.raises(RecordSchemaError):
        validate_index_record({**record, "owner": "owner-a"})
    for key in record:
        trimmed = {k: v for k, v in record.items() if k != key}
        with pytest.raises(RecordSchemaError):
            validate_index_record(trimmed)


def test_the_index_has_exactly_the_agreed_keys_and_no_identifier_but_the_references() -> None:
    record = _index(["owner-a"], {"WRITE_FAILED": 1})

    assert set(record) == {
        "schema_version",
        "record_kind",
        "business_date",
        "batch_id",
        "shadow_mode",
        "key_version",
        "owner_refs",
        "owner_refs_truncated",
        "attempted_owner_count",
        "recorded_owner_count",
        "unrecorded_owner_count",
        "unrecorded_reasons",
    }
    blob = json.dumps(record)
    assert "owner-a" not in blob  # 平文の owner は出ない(符号だけ)


def test_the_index_stays_far_below_the_record_size_limit() -> None:
    owners = [f"owner-{n:02d}" for n in range(MAX_INDEX_OWNERS)]
    size = len(json.dumps(_index(owners, {"OWNER_CAP": 1})).encode())

    assert size < 4_000  # 1 件 400KB の上限に対して十分に小さい(1 回の GetItem で読める)


# --- 日単位の skip --------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", list(KeyUnavailableReason))
def test_the_day_skip_keeps_only_the_reason_and_the_count(reason: KeyUnavailableReason) -> None:
    record = build_day_skip_record(
        business_date=_DATE,
        batch_id=_BATCH,
        shadow_mode="SHADOW",
        reason="KEY_UNAVAILABLE",
        key_unavailable_reason=reason,
        unrecorded_owner_count=3,
    )

    validate_day_skip_record(record)
    assert json.loads(json.dumps(record)) == record
    assert record == {
        "schema_version": 1,
        "record_kind": "allocation_shadow_day_skip",
        "business_date": "2026-10-14",
        "batch_id": _BATCH,
        "shadow_mode": "SHADOW",
        "reason": "KEY_UNAVAILABLE",
        "key_unavailable_reason": reason.value,
        "unrecorded_owner_count": 3,
    }


@pytest.mark.parametrize("reason", ["TIMEOUT", "OTHER", "", None, 5, "key_unavailable"])
def test_the_day_skip_reason_must_be_one_of_the_closed_vocabulary(reason: object) -> None:
    with pytest.raises(RecordSchemaError):
        build_day_skip_record(
            business_date=_DATE,
            batch_id=_BATCH,
            shadow_mode="SHADOW",
            reason=reason,  # type: ignore[arg-type]
            key_unavailable_reason=KeyUnavailableReason.TIMEOUT,
            unrecorded_owner_count=0,
        )


@pytest.mark.parametrize("count", [-1, True, 1.5, "3", None])
def test_the_day_skip_count_must_be_a_non_negative_int(count: object) -> None:
    with pytest.raises(RecordSchemaError):
        build_day_skip_record(
            business_date=_DATE,
            batch_id=_BATCH,
            shadow_mode="SHADOW",
            reason="KEY_UNAVAILABLE",
            key_unavailable_reason=KeyUnavailableReason.TIMEOUT,
            unrecorded_owner_count=count,  # type: ignore[arg-type]
        )


def test_the_day_skip_allowlist_rejects_unknown_and_missing_keys_and_owner_fields() -> None:
    record = build_day_skip_record(
        business_date=_DATE,
        batch_id=_BATCH,
        shadow_mode="SHADOW",
        reason="KEY_UNAVAILABLE",
        key_unavailable_reason=KeyUnavailableReason.ACCESS_DENIED,
        unrecorded_owner_count=1,
    )
    for extra in ({"owner_ref": _ref("owner-a")}, {"owner": "owner-a"}, {"detail": "x"}):
        with pytest.raises(RecordSchemaError):
            validate_day_skip_record({**record, **extra})
    for key in record:
        with pytest.raises(RecordSchemaError):
            validate_day_skip_record({k: v for k, v in record.items() if k != key})
    assert not any("owner_ref" in key for key in record)  # owner を持たない


@pytest.mark.parametrize(
    "change",
    [
        {"reason": "OTHER"},
        {"key_unavailable_reason": "OTHER"},
        {"key_unavailable_reason": "timeout"},
        {"record_kind": "allocation_shadow_index"},
        {"schema_version": 2},
        {"unrecorded_owner_count": True},
    ],
)
def test_a_tampered_day_skip_is_rejected(change: dict[str, Any]) -> None:
    record = build_day_skip_record(
        business_date=_DATE,
        batch_id=_BATCH,
        shadow_mode="SHADOW",
        reason="KEY_UNAVAILABLE",
        key_unavailable_reason=KeyUnavailableReason.NOT_FOUND,
        unrecorded_owner_count=0,
    )
    with pytest.raises(RecordSchemaError):
        validate_day_skip_record({**record, **change})


# --- audit_id ------------------------------------------------------------------------------------


def test_the_audit_ids_are_deterministic_and_do_not_collide() -> None:
    owner = _ref("owner-a")

    assert index_audit_id(_DATE) == "allocation_shadow_index:2026-10-14"
    assert (
        day_skip_audit_id(_DATE) == "allocation_shadow_day_skip:2026-10-14"
    )  # 索引と対称(日付で引ける)
    assert items_audit_id(_DATE, owner) == f"allocation_shadow_items:2026-10-14:{owner}"
    # 既存の owner 単位の枠・skip の接頭辞(allocation_shadow_skip:{batch}:{ref})と衝突しない
    assert not day_skip_audit_id(_DATE).startswith("allocation_shadow_skip:")
    ids = {index_audit_id(_DATE), day_skip_audit_id(_DATE), items_audit_id(_DATE, owner)}
    assert len(ids) == 3
    assert index_audit_id(_DATE) == index_audit_id(_DATE)


def test_the_items_audit_id_accepts_only_a_keyed_reference() -> None:
    with pytest.raises(KeyedReferenceRequiredError):
        items_audit_id(_DATE, owner_ref("owner-a"))  # type: ignore[arg-type]
    with pytest.raises(KeyedReferenceRequiredError):
        items_audit_id(_DATE, "owner-a")  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["2026-10-14", dt.datetime(2026, 10, 14), None, 20261014])
def test_the_audit_ids_require_a_date(value: object) -> None:
    with pytest.raises(RecordSchemaError):
        index_audit_id(value)  # type: ignore[arg-type]
    with pytest.raises(RecordSchemaError):
        day_skip_audit_id(value)  # type: ignore[arg-type]
    with pytest.raises(RecordSchemaError):
        items_audit_id(value, _ref("owner-a"))  # type: ignore[arg-type]


def test_the_day_skip_key_is_the_day_only_and_the_batch_id_stays_in_the_body() -> None:
    """CLI は batch_id を知らない。日付だけで引け、同じ日の 2 つの batch は同じ key(先着)になる。"""
    first = build_day_skip_record(
        business_date=_DATE,
        batch_id="batch-a",
        shadow_mode="SHADOW",
        reason="KEY_UNAVAILABLE",
        key_unavailable_reason=KeyUnavailableReason.TIMEOUT,
        unrecorded_owner_count=1,
    )
    second = build_day_skip_record(
        business_date=_DATE,
        batch_id="batch-b",
        shadow_mode="SHADOW",
        reason="KEY_UNAVAILABLE",
        key_unavailable_reason=KeyUnavailableReason.TIMEOUT,
        unrecorded_owner_count=1,
    )

    assert first["batch_id"] == "batch-a"  # batch_id は本文に残る(失われる情報はない)
    assert day_skip_audit_id(dt.date(2026, 10, 14)) == day_skip_audit_id(dt.date(2026, 10, 14))
    assert day_skip_audit_id(dt.date(2026, 10, 15)) != day_skip_audit_id(_DATE)
    assert first["business_date"] == second["business_date"]
    assert "batch" not in day_skip_audit_id(_DATE)


# --- 部分で終わった日の整合(builder の不変条件)---------------------------------------------------


@pytest.mark.parametrize(
    ("recorded", "reasons"),
    [
        (0, {}),
        (3, {}),
        (2, {"BUDGET_EXHAUSTED": 1}),
        (1, {"BUDGET_EXHAUSTED": 1, "WRITE_FAILED": 1, "REFUSED": 2}),
        (16, {"OWNER_CAP": 5}),
        (0, {"KEY_UNAVAILABLE": 4}),
    ],
)
def test_attempted_always_equals_recorded_plus_unrecorded(
    recorded: int, reasons: dict[str, int]
) -> None:
    owners = [f"owner-{n:02d}" for n in range(recorded)]
    record = _index(owners, reasons)

    assert record["recorded_owner_count"] == len(record["owner_refs"]) == recorded
    assert record["unrecorded_owner_count"] == sum(reasons.values())
    assert record["attempted_owner_count"] == recorded + sum(reasons.values())
    assert record["owner_refs_truncated"] == (reasons.get("OWNER_CAP", 0) > 0)


# --- 構造 ----------------------------------------------------------------------------------------


def test_the_day_record_builders_are_pure() -> None:
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    names = {"build_index_record", "validate_index_record", "build_day_skip_record"}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    target = getattr(call.func, "attr", getattr(call.func, "id", ""))
                    assert target not in {"now", "today", "open", "print", "getenv", "info"}
                    assert not str(target).startswith("log")


# --- 日単位の skip の理由の拡張(#603 F2-2b 設計 rev6): バッチ共通の入力が作れない日 --------------

_INPUT_FAILURES = (
    "HOLDINGS_READ_FAILED",
    "RECOMMENDATIONS_READ_FAILED",
    "INPUT_LIMIT_EXCEEDED",
    "COEFFICIENTS_UNAVAILABLE",
    "TIME_LIMIT",
    "COMPUTATION_FAILED",
)
_COMPUTE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "jstock_advisor"
    / "services"
    / "allocation_shadow_compute.py"
)


def _reasons_prepare_batch_inputs_can_return() -> set[str]:
    """prepare_batch_inputs の AST から、BatchUnavailable(...) の理由を全数集める(手で書かない)。"""
    tree = ast.parse(_COMPUTE.read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "prepare_batch_inputs"
    )
    found: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "BatchUnavailable":
            reason = node.args[0]
            # 理由は ShadowSkipReason.X の形だけ(変数・式にしない = 導出が成り立つ前提)
            assert isinstance(reason, ast.Attribute), ast.dump(reason)
            assert getattr(reason.value, "id", None) == "ShadowSkipReason", ast.dump(reason)
            found.add(ShadowSkipReason[reason.attr].value)
    return found


def test_the_day_skip_vocabulary_is_the_key_failure_the_input_failures_and_the_budget() -> None:
    # 入口の判定(Lambda の残り時間が足りない日)の TIME_BUDGET は、導出の網の外に 1 語だけ別に持つ
    assert ("KEY_UNAVAILABLE", *_INPUT_FAILURES, "TIME_BUDGET") == DAY_SKIP_REASONS


@pytest.mark.parametrize("reason", _INPUT_FAILURES)
def test_an_input_failure_day_skip_has_no_key_reason_and_no_count(reason: str) -> None:
    record = build_day_skip_record(
        business_date=_DATE, batch_id=_BATCH, shadow_mode="SHADOW", reason=reason
    )

    validate_day_skip_record(record)
    assert json.loads(json.dumps(record)) == record
    assert record["reason"] == reason
    assert record["key_unavailable_reason"] is None  # 鍵の話ではない
    assert record["unrecorded_owner_count"] is None  # owner が分からない(0 件と言わない)
    assert not any("owner_ref" in key for key in record)


@pytest.mark.parametrize("reason", _INPUT_FAILURES)
def test_an_input_failure_day_skip_cannot_carry_a_key_reason_or_a_count(reason: str) -> None:
    with pytest.raises(RecordSchemaError):
        build_day_skip_record(
            business_date=_DATE,
            batch_id=_BATCH,
            shadow_mode="SHADOW",
            reason=reason,
            key_unavailable_reason=KeyUnavailableReason.TIMEOUT,
        )
    with pytest.raises(RecordSchemaError):
        build_day_skip_record(
            business_date=_DATE,
            batch_id=_BATCH,
            shadow_mode="SHADOW",
            reason=reason,
            unrecorded_owner_count=0,
        )


def test_a_key_failure_day_skip_requires_the_key_reason_and_the_count() -> None:
    with pytest.raises(RecordSchemaError):
        build_day_skip_record(
            business_date=_DATE,
            batch_id=_BATCH,
            shadow_mode="SHADOW",
            reason="KEY_UNAVAILABLE",
            unrecorded_owner_count=1,
        )
    with pytest.raises(RecordSchemaError):
        build_day_skip_record(
            business_date=_DATE,
            batch_id=_BATCH,
            shadow_mode="SHADOW",
            reason="KEY_UNAVAILABLE",
            key_unavailable_reason=KeyUnavailableReason.ERROR,
        )


def test_the_validator_rejects_a_hand_made_record_that_breaks_the_pairing() -> None:
    base = build_day_skip_record(
        business_date=_DATE, batch_id=_BATCH, shadow_mode="SHADOW", reason="HOLDINGS_READ_FAILED"
    )
    for change in (
        {"key_unavailable_reason": "TIMEOUT"},
        {"unrecorded_owner_count": 0},
        {"reason": "KEY_UNAVAILABLE"},
    ):
        with pytest.raises(RecordSchemaError):
            validate_day_skip_record({**base, **change})
    keyed = build_day_skip_record(
        business_date=_DATE,
        batch_id=_BATCH,
        shadow_mode="SHADOW",
        reason="KEY_UNAVAILABLE",
        key_unavailable_reason=KeyUnavailableReason.TIMEOUT,
        unrecorded_owner_count=2,
    )
    for change in ({"key_unavailable_reason": None}, {"unrecorded_owner_count": None}):
        with pytest.raises(RecordSchemaError):
            validate_day_skip_record({**keyed, **change})


def test_every_reason_prepare_batch_inputs_can_return_can_be_written_as_a_day_skip() -> None:
    """★ 導出の網 ★ prepare_batch_inputs が将来新しい理由を返せば、ここで落ちる(取りこぼし防止)。"""
    derived = _reasons_prepare_batch_inputs_can_return()

    assert len(derived) >= 6  # 空振り防止: 実際に 6 種が導出されている(AST が空を返していない)
    assert derived <= set(DAY_SKIP_REASONS)
    assert derived == set(_INPUT_FAILURES)  # 手書きの一覧との完全一致も併せて残す


def test_every_reason_the_entry_precheck_can_return_can_be_written_as_a_day_skip() -> None:
    """★ 導出の網をもう 1 か所 ★ 入口の判定(`_precheck`)の返し得る理由を AST で全数導出し ⊆ を固定。

    入口の判定に理由が増えたとき、語彙の追加忘れで落ちる(取りこぼし防止)。
    """
    service = _COMPUTE.with_name("allocation_shadow_service.py")
    tree = ast.parse(service.read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_precheck"
    )
    derived: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Return) and node.value is not None:
            if isinstance(node.value, ast.Constant) and node.value.value is None:
                continue
            # 理由は ShadowSkipReason.X の形だけ(変数・式にしない = 導出が成り立つ前提)
            assert isinstance(node.value, ast.Attribute), ast.dump(node.value)
            assert getattr(node.value.value, "id", None) == "ShadowSkipReason"
            derived.add(ShadowSkipReason[node.value.attr].value)

    assert derived == {"TIME_BUDGET"}  # 空振り防止: 実際に導出されている
    assert derived <= set(DAY_SKIP_REASONS)


def test_the_most_likely_input_failures_can_actually_be_written() -> None:
    for reason in ("TIME_LIMIT", "COMPUTATION_FAILED", "TIME_BUDGET"):
        record = build_day_skip_record(
            business_date=_DATE, batch_id=_BATCH, shadow_mode="SHADOW", reason=reason
        )
        validate_day_skip_record(record)
        assert record["reason"] == reason


def test_the_input_failure_words_are_real_shadow_skip_reasons() -> None:
    """綴り間違いが入ると CLI が『未対応の理由コード』しか出せない(部分集合であることを固定)。"""
    shadow_reasons = {reason.value for reason in ShadowSkipReason}

    assert set(DAY_SKIP_REASONS) - {"KEY_UNAVAILABLE"} <= shadow_reasons
