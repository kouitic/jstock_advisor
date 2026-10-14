"""Issue #605 最小 CLI: 記録 → 画面モデル(純粋・LENIENT)の契約テスト。

固定するもの:
  - ★ 書込側(#943 の index_audit_id・day_skip_audit_id・items_audit_id)との key の一致 ★
    (実際の値で 3 つすべてを突き合わせる。接頭辞の literal 比較にしない)・定数の一致
  - 理由コードの日本語は閉じた語彙で、元の列挙と過不足なく一致(網羅)。
    未知のコードは『未対応の理由コード』
  - 索引の 6 状態(完全 / 一部未記録 / 記録 0 件 / 索引なし+skip / 索引なし / 対応していない版)
    を区別する
  - ★ LENIENT: どんな辞書でも例外を出さない(型・キーを崩す fuzz)。解釈できない項目は
    『不明』として数える ★
  - 補完・丸めをしない: 金額は記録の文字列に桁区切りだけ。読めない値は生の文字列。
    残余資金は記録された金額の引き算だけ
  - 実際の組み立て(F2-2a の compute + #943 の索引の builder)が作った記録を読める(結合)
  - この module は重い record module・配分・RAER・時計・I/O を import しない
"""

from __future__ import annotations

import ast
import copy
import datetime as dt
import random
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import BuyAction, BuyIndustrySector, buy_action_label
from jstock_advisor.domain.entities.identifier_ref import KeyUnavailableReason
from jstock_advisor.domain.notification import allocation_shadow_cli_view as view
from jstock_advisor.domain.notification.allocation_shadow_cli_renderer import (
    render_json,
    render_table,
)
from jstock_advisor.domain.notification.allocation_shadow_cli_view import (
    DAY_STATE_TEXT,
    DayInput,
    DayState,
    OwnerStatus,
    RecordInput,
    RecordState,
    build_screen,
    display_ref,
    format_decimal,
    refs_to_read,
    resolve_label,
)
from jstock_advisor.domain.signals import allocation_shadow_record as rec
from jstock_advisor.domain.signals.allocation_inputs import (
    CandidateExclusionReason,
    IncompleteReason,
)
from jstock_advisor.domain.signals.portfolio_allocation_types import (
    AllocationStatus,
    CandidateDecision,
    ReasonCode,
    UnavailableReason,
)
from jstock_advisor.domain.valuation.raer import RaerState
from jstock_advisor.services.allocation_shadow_compute import compute_owner
from jstock_advisor.services.allocation_shadow_service import Deadline, ShadowSkipReason
from tests.unit import test_allocation_shadow_compute_contract as compute_tests

_REPO = Path(__file__).resolve().parents[2]
_VIEW_PATH = (
    _REPO / "src" / "jstock_advisor" / "domain" / "notification" / "allocation_shadow_cli_view.py"
)
_DATE = dt.date(2026, 10, 14)
_DAY = "2026-10-14"
_BATCH = "buy-candidates-20261014"
FOUND = RecordState.FOUND
MISSING = RecordInput(RecordState.NOT_FOUND)


def _found(record: dict[str, Any]) -> RecordInput:
    return RecordInput(FOUND, record)


# --- 実際の組み立て(F2-2a + #943)が作った記録 ----------------------------------------------------


def _real(
    tmp_path: Path, owners: tuple[str, ...] = ("owner-a",)
) -> tuple[DayInput, dict[str, Any]]:
    """compute(F2-2a)で計算した記録と、#943 の索引の builder が作った索引。"""
    holdings = [compute_tests.holding(o, "9001", 100000) for o in owners]
    recs = {compute_tests._rec_id(c): compute_tests.rec(c) for c in ("0001", "0002")}
    recs[compute_tests._rec_id("9001")] = compute_tests.rec(
        "9001", buy_industry_sector=BuyIndustrySector.BANK
    )
    reader = compute_tests.FakeReader(
        holdings=holdings,
        recs=recs,
        cashes={o: compute_tests.cash(o, "20000000") for o in owners},
    )
    inputs = compute_tests._ready(tmp_path, reader)
    records = {
        o: compute_owner(
            inputs,
            o,
            compute_tests._ref(o),
            reader=reader,
            deadline=Deadline(30.0),
            io_timeout_seconds=1.0,
        ).record
        for o in owners
    }
    refs = [compute_tests._ref(o) for o in owners]
    index = rec.build_index_record(
        business_date=_DATE,
        batch_id=_BATCH,
        shadow_mode="SHADOW",
        key_version=1,
        recorded=refs,
        unrecorded_reasons={},
    )
    items: dict[str, RecordInput] = {}
    for o in owners:
        record = records[o]
        assert record is not None
        items[compute_tests._ref(o)] = _found(record)
    day = DayInput(_DAY, _found(index), MISSING, items)
    return day, {"index": index, "records": records, "refs": refs}


# =================================================================================================
# 書込側との一致
# =================================================================================================


def test_the_read_keys_equal_the_write_keys_for_real_values() -> None:
    ref = compute_tests._ref("owner-a")  # 本物の kv1 の符号

    assert view.index_key(_DAY) == rec.index_audit_id(_DATE)
    assert view.day_skip_key(_DAY) == rec.day_skip_audit_id(_DATE)
    assert view.items_key(_DAY, ref) == rec.items_audit_id(_DATE, ref)


@pytest.mark.parametrize("day", ["2026-01-01", "2026-12-31", "2027-02-28"])
def test_the_keys_follow_the_date_for_other_days(day: str) -> None:
    date = dt.date.fromisoformat(day)
    ref = compute_tests._ref("owner-b")

    assert view.index_key(day) == rec.index_audit_id(date)
    assert view.day_skip_key(day) == rec.day_skip_audit_id(date)
    assert view.items_key(day, ref) == rec.items_audit_id(date, ref)


def test_the_constants_match_the_writer() -> None:
    assert view.SCHEMA_VERSION == rec.SCHEMA_VERSION
    assert view.INDEX_KIND == rec.INDEX_RECORD_KIND
    assert view.ITEMS_KIND == rec.RECORD_KIND
    assert view.DAY_SKIP_KIND == rec.DAY_SKIP_RECORD_KIND
    assert view.MAX_OWNERS == rec.MAX_INDEX_OWNERS


# =================================================================================================
# 理由コードの語彙(閉じている・網羅)
# =================================================================================================


def _values(enum_type: Any) -> set[str]:
    return {member.value for member in enum_type}


@pytest.mark.parametrize(
    ("table", "source"),
    [
        (view.ALLOCATION_REASONS, _values(ReasonCode)),
        (view.UNAVAILABLE_REASONS, _values(UnavailableReason)),
        (view.KEY_UNAVAILABLE_REASONS, _values(KeyUnavailableReason)),
        (view.UNRECORDED_REASONS, set(rec.UNRECORDED_REASONS)),
        (view.INCOMPLETE_REASONS, _values(IncompleteReason)),
        (view.EXCLUSION_REASONS, _values(CandidateExclusionReason)),
        (view.RAER_STATES, _values(RaerState)),
        (view.RUN_STATUSES, _values(AllocationStatus)),
        (view.DECISIONS, _values(CandidateDecision)),
        (view.BUY_ACTIONS, _values(BuyAction)),
    ],
)
def test_each_vocabulary_equals_its_source_enumeration(
    table: dict[str, str], source: set[str]
) -> None:
    assert set(table) == source
    assert all(text and text.strip() for text in table.values())


def test_the_skip_reasons_cover_every_shadow_skip_reason_and_the_key_failure() -> None:
    assert set(view.SKIP_REASONS) == _values(ShadowSkipReason) | {"KEY_UNAVAILABLE"}
    assert set(view.OUTCOMES) == {"COMPUTED", "SKIPPED", "FAILED"}


def test_the_buy_action_text_is_the_existing_label() -> None:
    for action in BuyAction:
        assert view.BUY_ACTIONS[action.value] == buy_action_label(action)


def test_an_unknown_code_is_shown_with_the_unsupported_text() -> None:
    day = _real_day_with_reason("BRAND_NEW_CODE")

    screen = build_screen(day)

    row = screen.owners[0].rows[0]
    assert row.reason is not None
    assert row.reason.code == "BRAND_NEW_CODE"
    assert row.reason.text == view.UNSUPPORTED_TEXT


def _real_day_with_reason(code: str) -> DayInput:
    record = _owner_record()
    record["candidates"][0]["allocation"]["reason_code"] = code
    return _day_with(record)


# --- 最小の記録(手で作る)--------------------------------------------------------------------------


def _owner_record() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "record_kind": "allocation_shadow_items",
        "business_date": _DAY,
        "batch_id": _BATCH,
        "owner_ref": compute_tests._ref("owner-a"),
        "model": {
            "raer_model_version": "raer-combined-v1-provisional",
            "policy_id": "p1",
            "config_hash": "abc",
            "contract_version": "c1",
            "shadow_mode": "SHADOW",
        },
        "run": {
            "outcome": "COMPUTED",
            "status": "ALLOCATED_SOME",
            "unavailable_reason": None,
            "skip_reason": None,
            "policy_missing": [],
            "elapsed_ms": 5,
            "read_states": {},
        },
        "counts": {"incomplete_reasons": [], "exclusions": {}, "candidates_truncated": 0},
        "cash": {
            "available_cash": "10000000",
            "last_reconciled_at": "2026-10-11T09:05:00+09:00",
            "updated_at": None,
        },
        "portfolio": {"portfolio_value": "50000000"},
        "candidates": [
            {
                "seq": 1,
                "stock_code": "0001",
                "attrs": {"buy_action": "BUY"},
                "price": {"current_price": "1000"},
                "raer_out": {"state": "PROVISIONAL", "value": "35.400", "confidence": "MEDIUM"},
                "allocation": {
                    "rank": 1,
                    "decision": "ALLOCATED",
                    "reason_code": "ALLOCATED",
                    "shares": 5000,
                    "amount": "5000000",
                    "cap_limited_shares": 11700,
                    "cash_shortfall": "0",
                },
            },
            {
                "seq": 2,
                "stock_code": "0002",
                "attrs": {"buy_action": "SMALL_ENTRY"},
                "price": {"current_price": "2000"},
                "raer_out": {"state": "PROVISIONAL", "value": "20.1", "confidence": "LOW"},
                "allocation": {
                    "rank": 2,
                    "decision": "NOT_ALLOCATED",
                    "reason_code": "INSUFFICIENT_CASH_REMAINING",
                    "shares": 0,
                    "amount": "0",
                    "cap_limited_shares": 3000,
                    "cash_shortfall": "1000000",
                },
            },
        ],
    }


def _index(
    refs: list[str] | None = None,
    recorded: int | None = None,
    unrecorded: int = 0,
    reasons: dict[str, int] | None = None,
) -> dict[str, Any]:
    refs = [compute_tests._ref("owner-a")] if refs is None else refs
    return {
        "schema_version": 1,
        "record_kind": "allocation_shadow_index",
        "business_date": _DAY,
        "batch_id": _BATCH,
        "shadow_mode": "SHADOW",
        "key_version": 1,
        "owner_refs": refs,
        "owner_refs_truncated": False,
        "attempted_owner_count": len(refs) + unrecorded,
        "recorded_owner_count": len(refs) if recorded is None else recorded,
        "unrecorded_owner_count": unrecorded,
        "unrecorded_reasons": reasons or {},
    }


def _skip_record() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "record_kind": "allocation_shadow_day_skip",
        "business_date": _DAY,
        "batch_id": _BATCH,
        "shadow_mode": "SHADOW",
        "reason": "KEY_UNAVAILABLE",
        "key_unavailable_reason": "ACCESS_DENIED",
        "unrecorded_owner_count": 2,
    }


def _day_with(owner: dict[str, Any], index: dict[str, Any] | None = None) -> DayInput:
    ref = compute_tests._ref("owner-a")
    return DayInput(_DAY, _found(index or _index([ref])), MISSING, {ref: _found(owner)})


# =================================================================================================
# 索引の 6 状態
# =================================================================================================


def test_state_1_a_complete_day_shows_every_owner() -> None:
    screen = build_screen(_day_with(_owner_record()))

    assert screen.state is DayState.COMPLETE
    assert screen.complete is True
    assert [o.status for o in screen.owners] == [OwnerStatus.OK]
    assert screen.owners[0].display_ref == display_ref(compute_tests._ref("owner-a"))


def test_state_2_a_partial_day_shows_the_recorded_owners_and_the_unrecorded_count() -> None:
    index = _index(unrecorded=2, reasons={"BUDGET_EXHAUSTED": 1, "WRITE_FAILED": 1})

    screen = build_screen(_day_with(_owner_record(), index))

    assert screen.state is DayState.PARTIAL
    assert screen.complete is False
    assert screen.index is not None
    assert screen.index.unrecorded == "2"
    assert [(r.code, n) for r, n in screen.index.unrecorded_reasons] == [
        ("BUDGET_EXHAUSTED", "1"),
        ("WRITE_FAILED", "1"),
    ]
    assert len(screen.owners) == 1


def test_state_3_an_index_with_no_recorded_owner_is_not_the_same_as_no_index() -> None:
    index = _index(refs=[], recorded=0, unrecorded=3, reasons={"BUDGET_EXHAUSTED": 3})

    screen = build_screen(DayInput(_DAY, _found(index), MISSING, {}))

    assert screen.state is DayState.EMPTY
    assert screen.complete is False
    assert screen.owners == ()
    assert DAY_STATE_TEXT[DayState.EMPTY] != DAY_STATE_TEXT[DayState.NO_INDEX]


def test_state_4a_no_index_but_a_skip_record_names_the_key_failure() -> None:
    day = DayInput(_DAY, MISSING, _found(_skip_record()), {})

    screen = build_screen(day)

    assert screen.state is DayState.NO_INDEX_KEY_FAILURE
    assert screen.complete is False
    assert screen.skip is not None
    assert screen.skip.key_unavailable_reason is not None
    assert screen.skip.key_unavailable_reason.code == "ACCESS_DENIED"
    assert screen.skip.unrecorded_owner_count == "2"


def test_state_4b_no_index_and_no_skip_never_says_zero_records() -> None:
    screen = build_screen(DayInput(_DAY, MISSING, MISSING, {}))

    assert screen.state is DayState.NO_INDEX
    assert screen.complete is False
    assert screen.owners == ()
    text = render_table(screen)
    assert "索引がありません" in text
    assert "0 件" not in text


def test_state_5_an_unsupported_index_version_is_reported_with_the_seen_version() -> None:
    index = _index()
    index["schema_version"] = 2

    screen = build_screen(DayInput(_DAY, _found(index), MISSING, {}))

    assert screen.state is DayState.UNSUPPORTED
    assert screen.schema_version_seen == "2"
    assert screen.complete is False


@pytest.mark.parametrize("state", [RecordState.UNREADABLE])
def test_an_unreadable_index_is_unsupported_not_empty(state: RecordState) -> None:
    screen = build_screen(DayInput(_DAY, RecordInput(state), MISSING, {}))

    assert screen.state is DayState.UNSUPPORTED
    assert screen.unparsed == 1


def test_a_skip_on_a_day_that_also_has_an_index_is_kept_next_to_the_index_state() -> None:
    day = DayInput(
        _DAY,
        _found(_index()),
        _found(_skip_record()),
        {compute_tests._ref("owner-a"): _found(_owner_record())},
    )

    screen = build_screen(day)

    assert screen.state is DayState.COMPLETE  # 索引の状態を優先
    assert screen.skip is not None  # skip の存在は添える
    assert "日単位の skip" in render_table(screen)


def test_an_unsupported_skip_version_is_ignored_not_misread() -> None:
    skip = _skip_record()
    skip["schema_version"] = 9

    screen = build_screen(DayInput(_DAY, MISSING, _found(skip), {}))

    assert screen.state is DayState.NO_INDEX
    assert screen.skip is None


# --- 索引と記録の不一致・符号の検査 -------------------------------------------------------


def test_an_owner_in_the_index_without_a_record_is_listed_by_reference() -> None:
    ref = compute_tests._ref("owner-a")
    day = DayInput(_DAY, _found(_index([ref])), MISSING, {ref: MISSING})

    screen = build_screen(day)

    assert screen.owners[0].status is OwnerStatus.MISSING
    assert screen.missing_refs == (ref,)
    assert screen.complete is False
    text = render_table(screen)
    assert ref.split(":")[1][:12] in text  # どの符号か(不透明な識別子)


def test_a_reference_that_is_not_keyed_is_refused_and_never_read() -> None:
    index = _index(refs=["kv0:" + "a" * 24, "owner-a", "kv1:" + "b" * 24])

    assert refs_to_read(_found(index)) == []
    screen = build_screen(DayInput(_DAY, _found(index), MISSING, {}))
    assert [o.status for o in screen.owners] == [OwnerStatus.REFUSED] * 3
    assert screen.unparsed == 3


def test_at_most_sixteen_references_are_read_and_the_rest_are_counted() -> None:
    refs: list[str] = [compute_tests._ref(f"owner-{n:02d}") for n in range(20)]
    index = _index(refs=refs)

    assert refs_to_read(_found(index)) == refs[:16]
    screen = build_screen(DayInput(_DAY, _found(index), MISSING, {}))
    assert screen.over_cap_refs == 4
    assert screen.state is DayState.PARTIAL


def test_the_references_to_read_come_only_from_a_supported_found_index() -> None:
    assert refs_to_read(MISSING) == []
    assert refs_to_read(RecordInput(RecordState.UNREADABLE)) == []
    unsupported = _index()
    unsupported["schema_version"] = 3
    assert refs_to_read(_found(unsupported)) == []
    wrong_kind = _index()
    wrong_kind["record_kind"] = "allocation_shadow_items"
    assert refs_to_read(_found(wrong_kind)) == []


def test_an_owner_record_of_another_version_or_kind_is_unsupported() -> None:
    for change in ({"schema_version": 2}, {"record_kind": "other"}):
        record = _owner_record()
        record.update(change)

        screen = build_screen(_day_with(record))

        assert screen.owners[0].status is OwnerStatus.UNSUPPORTED
        assert screen.unparsed == 1


def test_an_unreadable_owner_record_is_reported_not_dropped() -> None:
    ref = compute_tests._ref("owner-a")
    day = DayInput(_DAY, _found(_index([ref])), MISSING, {ref: RecordInput(RecordState.UNREADABLE)})

    screen = build_screen(day)

    assert screen.owners[0].status is OwnerStatus.UNREADABLE
    assert screen.unparsed == 1


# =================================================================================================
# 値の整形(補完・丸めをしない)
# =================================================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("5000000", "5,000,000"),
        ("1234567.8900", "1,234,567.8900"),
        ("-1500", "-1,500"),
        ("0", "0"),
        ("999", "999"),
        ("1000", "1,000"),
        ("0.00000012", "0.00000012"),
        ("1E+3", "1,000"),
    ],
)
def test_decimals_get_thousand_separators_without_rounding(raw: str, expected: str) -> None:
    assert format_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "1,000", "NaN", "Infinity", "-inf", "1 000"])
def test_a_value_that_is_not_a_finite_decimal_is_shown_as_the_raw_string(raw: str) -> None:
    assert format_decimal(raw) == raw


def test_none_is_none_and_non_strings_are_shown_raw_not_guessed() -> None:
    assert format_decimal(None) is None
    assert format_decimal(12.5) == "12.5"
    assert format_decimal(True) == "True"


def test_amounts_are_the_recorded_strings_and_the_remaining_cash_is_a_plain_subtraction() -> None:
    screen = build_screen(_day_with(_owner_record()))

    first, second = screen.owners[0].rows
    assert first.shares == "5000"
    assert first.required_funds == "5,000,000円"
    assert first.remaining_cash == "5,000,000円"  # 10,000,000 − 5,000,000
    assert second.decision is not None and second.decision.code == "NOT_ALLOCATED"
    assert second.shares is None
    assert second.required_funds == "6,000,000円"  # 上限株数 3000 × 価格 2000(記録の写し)
    assert second.shortfall == "1,000,000円"
    assert second.remaining_cash is None
    assert screen.owners[0].expected_remaining_cash == "5,000,000円"
    assert (screen.owners[0].allocated_count, screen.owners[0].not_allocated_count) == (1, 1)


def test_the_remaining_cash_is_not_shown_when_a_recorded_amount_cannot_be_read() -> None:
    record = _owner_record()
    record["candidates"][0]["allocation"]["amount"] = "five million"

    screen = build_screen(_day_with(record))

    first = screen.owners[0].rows[0]
    assert first.required_funds == "five million円"  # 読めない値は生の文字列(補わない)
    assert first.remaining_cash is None
    assert screen.owners[0].expected_remaining_cash is None


def test_a_zero_shortfall_is_not_shown() -> None:
    screen = build_screen(_day_with(_owner_record()))

    assert screen.owners[0].rows[0].shortfall is None


def test_raer_is_the_recorded_value_and_state() -> None:
    row = build_screen(_day_with(_owner_record())).owners[0].rows[0]

    assert row.raer_value == "35.400"
    assert row.raer_state is not None and row.raer_state.code == "PROVISIONAL"
    assert row.confidence == "MEDIUM"
    assert row.buy_action is not None and row.buy_action.text == "購入候補"


# =================================================================================================
# 結合: 実際の組み立てが作った記録を読める
# =================================================================================================


def test_a_record_built_by_the_real_assembly_is_read_without_unparsed_items(
    tmp_path: Path,
) -> None:
    day, parts = _real(tmp_path)

    screen = build_screen(day)

    assert screen.state is DayState.COMPLETE
    assert screen.complete is True
    assert screen.unparsed == 0
    owner = screen.owners[0]
    assert owner.status is OwnerStatus.OK
    assert owner.outcome is not None and owner.outcome.code == "COMPUTED"
    assert owner.run_status is not None and owner.run_status.code == "ALLOCATED_SOME"
    assert owner.available_cash == "20,000,000円"
    assert owner.allocated_count == 2
    assert owner.model_version and owner.policy_id and owner.contract_version
    assert all(row.readable for row in owner.rows)
    assert all(row.decision is not None for row in owner.rows)
    assert render_json(screen)  # JSON にもできる
    assert parts["index"]["recorded_owner_count"] == 1


def test_two_real_owners_are_both_shown_in_the_order_of_the_index(tmp_path: Path) -> None:
    day, parts = _real(tmp_path, owners=("owner-a", "owner-b"))

    screen = build_screen(day)

    assert [o.ref for o in screen.owners] == list(parts["index"]["owner_refs"])
    assert screen.state is DayState.COMPLETE


def test_a_header_only_real_record_shows_the_skip_reason(tmp_path: Path) -> None:
    holdings = [compute_tests.holding("owner-a", "9001", 100000)]
    reader = compute_tests.FakeReader(
        holdings=holdings,
        recs={compute_tests._rec_id(c): compute_tests.rec(c) for c in ("0001", "0002", "9001")},
        cashes={},
    )
    inputs = compute_tests._ready(tmp_path, reader)
    record = compute_tests._owner(inputs, reader, "owner-a").record
    assert record is not None
    ref = record["owner_ref"]
    day = DayInput(_DAY, _found(_index([ref])), MISSING, {ref: _found(record)})

    owner = build_screen(day).owners[0]

    assert owner.outcome is not None and owner.outcome.code == "SKIPPED"
    assert owner.skip_reason is not None and owner.skip_reason.code == "CASH_NOT_REGISTERED"
    assert owner.rows == ()
    assert owner.allocated_count is None


# =================================================================================================
# LENIENT: どんな辞書でも例外を出さない
# =================================================================================================

_JUNK: list[Any] = [
    None,
    0,
    -1,
    5,
    1.5,
    True,
    "",
    "x",
    "kv1.1:" + "a" * 24,
    "NaN",
    [],
    [None],
    [1, "a", {}],
    {},
    {"a": 1},
    {"x": {"y": [None]}},
    10**30,
]


def _paths(value: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    paths: list[tuple[Any, ...]] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            paths.append((*prefix, key))
            paths.extend(_paths(inner, (*prefix, key)))
    elif isinstance(value, list):
        for position, inner in enumerate(value):
            paths.append((*prefix, position))
            paths.extend(_paths(inner, (*prefix, position)))
    return paths


def _replace(root: Any, path: tuple[Any, ...], junk: Any) -> Any:
    clone = copy.deepcopy(root)
    cursor = clone
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = copy.deepcopy(junk)
    return clone


def _everything_works(day: DayInput) -> None:
    screen = build_screen(day)  # 例外を出さない
    assert screen is not None
    render_table(screen)
    render_json(screen)
    render_json(screen, with_labels=True)


def test_no_junk_in_any_field_of_an_owner_record_raises(tmp_path: Path) -> None:
    day, parts = _real(tmp_path)
    ref = parts["refs"][0]
    base = parts["records"]["owner-a"]
    count = 0
    for path in _paths(base):
        for junk in _JUNK:
            record = _replace(base, path, junk)
            _everything_works(DayInput(_DAY, day.index, MISSING, {ref: _found(record)}))
            count += 1
    assert count > 500  # 空振りでない


def test_no_junk_in_any_field_of_the_index_raises() -> None:
    ref = compute_tests._ref("owner-a")
    base = _index([ref], unrecorded=1, reasons={"WRITE_FAILED": 1})
    owner = _found(_owner_record())
    for path in _paths(base):
        for junk in _JUNK:
            index = _replace(base, path, junk)
            _everything_works(DayInput(_DAY, _found(index), MISSING, {ref: owner}))


def test_no_junk_in_any_field_of_the_skip_raises() -> None:
    base = _skip_record()
    for path in _paths(base):
        for junk in _JUNK:
            _everything_works(DayInput(_DAY, MISSING, _found(_replace(base, path, junk)), {}))


def test_the_whole_record_being_junk_never_raises() -> None:
    ref = compute_tests._ref("owner-a")
    for junk in _JUNK:
        for index, owner in ((junk, _owner_record()), (_index([ref]), junk)):
            day = DayInput(
                _DAY,
                RecordInput(FOUND, index),
                RecordInput(FOUND, junk),
                {ref: RecordInput(FOUND, owner)},
            )
            _everything_works(day)


def test_a_random_structure_never_raises() -> None:
    rng = random.Random(605)

    def make(depth: int) -> Any:
        kind = rng.randrange(7)
        if depth <= 0 or kind < 3:
            return rng.choice(_JUNK)
        if kind == 3:
            return [make(depth - 1) for _ in range(rng.randrange(4))]
        keys = [
            "schema_version",
            "record_kind",
            "owner_refs",
            "candidates",
            "run",
            "counts",
            "allocation",
            "raer_out",
            "cash",
            "portfolio",
            "unrecorded_reasons",
            "x",
        ]
        return {rng.choice(keys): make(depth - 1) for _ in range(rng.randrange(5))}

    ref = compute_tests._ref("owner-a")
    for _ in range(300):
        index = make(3)
        if isinstance(index, dict) and rng.random() < 0.7:
            index.update({"schema_version": 1, "record_kind": "allocation_shadow_index"})
        owner = make(3)
        if isinstance(owner, dict) and rng.random() < 0.7:
            owner.update({"schema_version": 1, "record_kind": "allocation_shadow_items"})
        _everything_works(
            DayInput(_DAY, RecordInput(FOUND, index), MISSING, {ref: RecordInput(FOUND, owner)})
        )


def test_an_unreadable_item_is_shown_as_unknown_not_dropped() -> None:
    record = _owner_record()
    record["candidates"].append("not a candidate")
    record["candidates"].append(None)
    record["candidates"].append(7)

    screen = build_screen(_day_with(record))

    owner = screen.owners[0]
    assert len(owner.rows) == 5  # 黙って省かない
    assert [r.readable for r in owner.rows] == [True, True, False, False, False]
    assert owner.unparsed == 3
    assert screen.complete is False
    assert "解釈できなかった" in render_table(screen)
    assert "不明" in render_table(screen)


# =================================================================================================
# ラベルの解決
# =================================================================================================


def test_a_label_matches_by_the_digest_prefix_with_the_longest_match() -> None:
    ref = "kv1.1:" + "abcdef0123456789abcdef01"

    assert resolve_label(ref, {"abcdef": "本人A"}) == "本人A"
    assert resolve_label(ref, {"abcdef": "短い", "abcdef0123": "長い"}) == "長い"
    assert resolve_label(ref, {"abcdee": "別"}) is None
    assert resolve_label(ref, {}) is None
    assert display_ref(ref) == "owner-abcdef"


def test_the_digest_of_a_reference_is_what_a_label_prefix_matches_not_the_version() -> None:
    ref = "kv1.7:" + "0123456789abcdef01234567"

    assert resolve_label(ref, {"012345": "x"}) == "x"
    assert resolve_label(ref, {"kv1.7:": "x"}) is None


def test_labels_are_resolved_on_the_screen_and_not_invented() -> None:
    ref = compute_tests._ref("owner-a")
    digest = ref.split(":")[1]

    labelled = build_screen(_day_with(_owner_record()), {digest[:8]: "表示名テスト"})
    plain = build_screen(_day_with(_owner_record()))

    assert labelled.owners[0].label == "表示名テスト"
    assert plain.owners[0].label is None


# =================================================================================================
# 構造
# =================================================================================================


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
    return names


def test_the_view_does_not_import_the_heavy_modules_io_or_a_clock() -> None:
    imported = _imports(_VIEW_PATH)
    assert not [m for m in imported if "allocation_shadow_record" in m or "shadow_compute" in m]
    assert not [m for m in imported if "portfolio_allocation" in m or "raer" in m]
    assert not imported & {"boto3", "os", "pathlib", "time", "datetime", "logging", "json"}
    tree = ast.parse(_VIEW_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"now", "today", "open", "getenv"}


def test_the_view_uses_the_identifier_module_only_for_the_keyed_reference_check() -> None:
    tree = ast.parse(_VIEW_PATH.read_text(encoding="utf-8"))
    names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "jstock_advisor.domain.entities.identifier_ref"
        for alias in node.names
    }
    assert names == {"IdentifierRefError", "parse_keyed_ref"}


def test_a_cap_limited_share_count_of_zero_shows_no_required_funds() -> None:
    record = _owner_record()
    record["candidates"][1]["allocation"]["cap_limited_shares"] = 0

    row = build_screen(_day_with(record)).owners[0].rows[1]

    assert row.required_funds is None  # 0 円を作らない(補完しない)


def test_an_unexpected_failure_inside_the_build_still_gives_a_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(view, "_build", boom)

    screen = build_screen(_day_with(_owner_record()))

    assert screen.state is DayState.UNSUPPORTED
    assert screen.complete is False
    assert screen.unparsed == 1
