"""資金入替の観測の『日の集計』の記録の形(`rotation_observation_record`)の契約テスト。

Issue #604 P-b1。dormant。

設計: #604 P-b 設計 rev4(…6168001528)・rev5(…6168319086)・設計レビュー PASS(…6168292046)。

  1  記録は閉じた形で、日の集計だけを持つ
     (★ owner・保有 ID・銘柄・金額・Recommendation ID を持たない ★)
  2  ★ 厳格な検証 ★: キーの欠落も余りも・語彙外・型違い・件数の不整合を拒否(値を例外へ載せない)
  3  ★ 語彙が増減したら schema_version を上げる規約 ★(P-a の理由コードとの対応を固定)
  4  決定的な記録の ID(OBSERVED だけが日を固定する)
  5  純粋性(AST)・dormant
"""

from __future__ import annotations

import ast
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.signals import rotation_observation_record as module
from jstock_advisor.domain.signals.rotation_observation import (
    BatchFacts,
    CapitalRotationObservation,
    DayStatus,
    HoldingInput,
    observe_day,
)
from jstock_advisor.domain.signals.rotation_observation_record import (
    _SCHEMA_VOCABULARY,
    CONTRACT_VERSION,
    FORBIDDEN_KEY_WORDS,
    GAP_ID_PREFIX,
    OBSERVATION_ID_PREFIX,
    RECORD_KIND,
    RECORD_SCHEMA_VERSION,
    RotationRecordError,
    assert_no_forbidden_words,
    audit_id_for,
    observation_from_output_values,
    to_output_values,
)
from jstock_advisor.domain.signals.rotation_source_eligibility import (
    EligibilityReason,
    RecommendationFacts,
)
from tests.unit.test_rotation_observation_contract import _importers_of
from tests.unit.test_rotation_source_eligibility_contract import facts

_REPO = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO / "src" / "jstock_advisor"
_MODULE_PATH = _PACKAGE / "domain" / "signals" / "rotation_observation_record.py"

_JST = dt.timezone(dt.timedelta(hours=9))
_DAY = dt.date(2026, 10, 14)
_START = dt.datetime(2026, 10, 14, 8, 0, 41, tzinfo=_JST)
_END = dt.datetime(2026, 10, 14, 8, 2, 41, tzinfo=_JST)
_INSIDE = dt.datetime(2026, 10, 14, 8, 1, tzinfo=_JST)
_SENTINEL = "SENTINEL-OWNER-AND-CODE-9999"


def _observed() -> CapitalRotationObservation:
    holdings = [
        HoldingInput(facts(evaluated_at=_INSIDE), {}),
        HoldingInput(None, {}, record_read_failed=True),
        HoldingInput(
            facts(evaluated_at=_INSIDE, authoritative_recommendation_id=_SENTINEL),
            {_SENTINEL: RecommendationFacts(_SENTINEL)},  # 未知の型 = TYPE_UNCLASSIFIED
        ),
    ]
    return observe_day(target_date=_DAY, batch=BatchFacts(_START, _END), holdings=holdings)


def _every_status() -> dict[DayStatus, CapitalRotationObservation]:
    complete = BatchFacts(_START, _END)
    return {
        DayStatus.OBSERVED: _observed(),
        DayStatus.BATCH_NOT_COMPLETED_IN_WINDOW: observe_day(
            target_date=_DAY, batch=BatchFacts(_START, None), holdings=[]
        ),
        DayStatus.INPUT_LIMIT_EXCEEDED: observe_day(
            target_date=_DAY,
            batch=complete,
            holdings=[HoldingInput(facts(evaluated_at=_INSIDE), {})] * 201,
        ),
        DayStatus.READ_FAILED: observe_day(target_date=_DAY, batch=None, holdings=[]),
    }


def _values() -> dict[str, Any]:
    return to_output_values(_observed())


# =================================================================================================
# 1 閉じた形・日の集計だけ
# =================================================================================================

_EXPECTED_TOP = {
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


def test_the_record_has_exactly_the_closed_set_of_keys() -> None:
    values = _values()

    assert set(values) == _EXPECTED_TOP
    assert set(values["batch"]) == {"state", "started_at", "completed_at", "window_seconds"}
    assert set(values["eligibility"]) == {"ELIGIBLE", "BLOCKED_BY_EXIT", "UNKNOWN"}
    assert set(values["unreadable"]) == {"record_read_failed", "recommendation_read_failed"}
    assert set(values["reasons"]) == {reason.value for reason in EligibilityReason}


def test_the_record_carries_the_fixed_identity_fields() -> None:
    values = _values()

    assert values["schema_version"] == RECORD_SCHEMA_VERSION == 1
    assert values["record_kind"] == RECORD_KIND == "capital_rotation_observation"
    assert values["contract_version"] == CONTRACT_VERSION


@pytest.mark.parametrize("status", list(DayStatus))
def test_every_day_status_serializes_to_json_with_only_text_and_integers(
    status: DayStatus,
) -> None:
    values = to_output_values(_every_status()[status])

    parsed = json.loads(json.dumps(values))

    assert parsed == values

    def leaves(node: Any) -> list[Any]:
        if isinstance(node, dict):
            return [x for v in node.values() for x in leaves(v)]
        return [node]

    for leaf in leaves(values):
        assert leaf is None or isinstance(leaf, (str, int)), type(leaf)
        assert not isinstance(leaf, float)


def test_no_key_contains_a_forbidden_word_at_any_depth() -> None:
    def keys(node: Any) -> list[str]:
        if isinstance(node, dict):
            return [str(k) for k in node] + [x for v in node.values() for x in keys(v)]
        return []

    for status, observation in _every_status().items():
        for key in keys(to_output_values(observation)):
            for word in FORBIDDEN_KEY_WORDS:
                assert word not in key.lower(), (status, key, word)


def test_the_forbidden_word_list_keeps_the_identifier_and_asset_words() -> None:
    """一覧の中身を固定する(1 語消えても気づけない穴を塞ぐ)。

    ★ 完全一致にしない ★(語を足すのは安全側)。
    """
    required = {"owner", "holding_id", "stock_code", "recommendation_id", "amount"}

    assert required <= set(FORBIDDEN_KEY_WORDS)


def test_a_sentinel_in_the_inputs_never_reaches_the_record() -> None:
    """識別子・推奨の値は集計の前に捨てる。入力に混ぜた目印が、記録のどこにも出ない。"""
    serialized = json.dumps(_values(), ensure_ascii=False)

    assert _SENTINEL not in serialized
    assert "SENTINEL" not in serialized


def test_the_times_are_written_in_jst_with_an_offset() -> None:
    values = _values()

    assert values["batch"]["started_at"] == "2026-10-14T08:00:41+09:00"
    assert values["batch"]["completed_at"] == "2026-10-14T08:02:41+09:00"
    assert values["batch"]["window_seconds"] == 120


def test_a_utc_time_is_written_in_jst() -> None:
    start = dt.datetime(2026, 10, 13, 23, 30, tzinfo=dt.UTC)
    end = dt.datetime(2026, 10, 13, 23, 40, tzinfo=dt.UTC)
    observation = observe_day(target_date=_DAY, batch=BatchFacts(start, end), holdings=[])

    batch = to_output_values(observation)["batch"]

    assert batch["started_at"] == "2026-10-14T08:30:00+09:00"
    assert batch["window_seconds"] == 600


def test_the_non_observed_days_carry_no_counts_and_no_stale_times() -> None:
    days = _every_status()

    unreadable = to_output_values(days[DayStatus.READ_FAILED])
    assert unreadable["batch"] == {
        "state": "UNREADABLE",
        "started_at": None,
        "completed_at": None,
        "window_seconds": None,
    }
    incomplete = to_output_values(days[DayStatus.BATCH_NOT_COMPLETED_IN_WINDOW])
    assert incomplete["batch"]["completed_at"] is None and incomplete["batch"]["started_at"]
    for status in (
        DayStatus.READ_FAILED,
        DayStatus.BATCH_NOT_COMPLETED_IN_WINDOW,
        DayStatus.INPUT_LIMIT_EXCEEDED,
    ):
        values = to_output_values(days[status])
        assert values["holdings_total"] == 0
        assert set(values["reasons"].values()) == {0}


def test_the_writer_rejects_something_that_is_not_an_observation() -> None:
    with pytest.raises(RotationRecordError):
        to_output_values({"holdings_total": 1})  # type: ignore[arg-type]


# =================================================================================================
# 2 厳格な読み(ラウンドトリップと拒否)
# =================================================================================================


@pytest.mark.parametrize("status", list(DayStatus))
def test_every_day_round_trips_exactly(status: DayStatus) -> None:
    observation = _every_status()[status]

    again = observation_from_output_values(json.loads(json.dumps(to_output_values(observation))))

    assert again == observation


def _mutated(path: list[str], value: Any) -> dict[str, Any]:
    values = json.loads(json.dumps(_values()))
    node = values
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return values


def _without(path: list[str]) -> dict[str, Any]:
    values = json.loads(json.dumps(_values()))
    node = values
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    return values


def _with_extra(path: list[str], key: str) -> dict[str, Any]:
    values = json.loads(json.dumps(_values()))
    node = values
    for step in path:
        node = node[step]
    node[key] = 0
    return values


@pytest.mark.parametrize("key", sorted(_EXPECTED_TOP))
def test_a_missing_top_level_key_is_rejected(key: str) -> None:
    with pytest.raises(RotationRecordError):
        observation_from_output_values(_without([key]))


@pytest.mark.parametrize("path", [[], ["batch"], ["eligibility"], ["reasons"], ["unreadable"]])
def test_an_extra_key_at_any_level_is_rejected(path: list[str]) -> None:
    with pytest.raises(RotationRecordError):
        observation_from_output_values(_with_extra(path, "extra_field"))


@pytest.mark.parametrize(
    "word", ["owner", "stock_code", "holding_id", "recommendation_id", "amount"]
)
def test_a_forbidden_identifier_or_asset_key_is_rejected(word: str) -> None:
    with pytest.raises(RotationRecordError):
        observation_from_output_values(_with_extra([], word))


@pytest.mark.parametrize("reason", [r.value for r in EligibilityReason])
def test_a_missing_reason_key_is_rejected_because_the_vocabulary_is_closed(reason: str) -> None:
    with pytest.raises(RotationRecordError):
        observation_from_output_values(_without(["reasons", reason]))


@pytest.mark.parametrize(
    "path,value",
    [
        (["schema_version"], 2),
        (["schema_version"], 0),
        (["schema_version"], True),
        (["schema_version"], "1"),
        (["record_kind"], "other"),
        (["contract_version"], "P604-v0"),
        (["target_date"], "2026-13-40"),
        (["target_date"], 20261014),
        (["target_date"], "2026-10-14T00:00:00"),
        (["day_status"], "DONE"),
        (["day_status"], "UNREADABLE"),
        (["batch", "state"], "FINISHED"),
        (["batch", "started_at"], "2026-10-14T08:00:41"),  # offset が無い
        (["batch", "started_at"], "yesterday"),
        (["batch", "started_at"], 12345),
        (["batch", "window_seconds"], 121),  # 時刻と食い違う
        (["batch", "window_seconds"], None),
        (["holdings_total"], 4),
        (["holdings_total"], True),
        (["holdings_total"], 3.0),
        (["holdings_total"], "3"),
        (["holdings_total"], -3),
        (["eligibility", "UNKNOWN"], 99),
        (["eligibility", "ELIGIBLE"], -1),
        (["reasons", "ATTENTION"], 1),  # 総数と合わない
        (["reasons", "ATTENTION"], True),
        (["unreadable", "record_read_failed"], 4),  # 総数を超える
        (["unreadable", "recommendation_read_failed"], -1),
    ],
)
def test_a_wrong_value_is_rejected(path: list[str], value: Any) -> None:
    with pytest.raises(RotationRecordError):
        observation_from_output_values(_mutated(path, value))


@pytest.mark.parametrize("bad", [None, [], "text", 7, {}, {"schema_version": 1}])
def test_something_that_is_not_a_full_mapping_is_rejected(bad: Any) -> None:
    with pytest.raises(RotationRecordError):
        observation_from_output_values(bad)


def test_an_inconsistent_status_and_batch_is_rejected() -> None:
    values = _mutated(["day_status"], "BATCH_NOT_COMPLETED_IN_WINDOW")

    with pytest.raises(RotationRecordError):
        observation_from_output_values(values)


@pytest.mark.parametrize(
    "path",
    [
        ["target_date"],
        ["day_status"],
        ["batch", "state"],
        ["batch", "started_at"],
        ["holdings_total"],
        ["reasons", "ATTENTION"],
    ],
)
def test_a_rejection_never_echoes_the_offending_value(path: list[str]) -> None:
    with pytest.raises(RotationRecordError) as caught:
        observation_from_output_values(_mutated(path, _SENTINEL))

    assert _SENTINEL not in str(caught.value)
    assert _SENTINEL not in repr(caught.value)


def test_a_forbidden_key_rejection_does_not_echo_the_key() -> None:
    with pytest.raises(RotationRecordError) as caught:
        observation_from_output_values(_with_extra([], _SENTINEL.lower() + "_owner"))

    assert _SENTINEL.lower() not in str(caught.value)


def test_the_forbidden_word_scan_reaches_nested_keys_and_lists() -> None:
    with pytest.raises(RotationRecordError):
        assert_no_forbidden_words({"a": {"b": [{"holding_id": 1}]}})
    assert_no_forbidden_words({"a": {"b": [{"count": 1}]}})


# =================================================================================================
# 3 語彙が増減したら schema_version を上げる
# =================================================================================================


def test_the_current_schema_version_vocabulary_is_exactly_the_eligibility_reasons() -> None:
    """★ P-a の理由コードを足す・消すと、ここが落ちる = 版を上げることを忘れない ★。"""
    assert RECORD_SCHEMA_VERSION in _SCHEMA_VOCABULARY
    assert _SCHEMA_VOCABULARY[RECORD_SCHEMA_VERSION] == {r.value for r in EligibilityReason}
    assert len(_SCHEMA_VOCABULARY[RECORD_SCHEMA_VERSION]) == 13


def test_every_listed_schema_version_is_an_integer_and_the_current_one_is_the_newest() -> None:
    assert all(isinstance(v, int) and not isinstance(v, bool) for v in _SCHEMA_VOCABULARY)
    assert max(_SCHEMA_VOCABULARY) == RECORD_SCHEMA_VERSION


def test_a_record_with_a_changed_vocabulary_is_not_accepted_under_the_same_version() -> None:
    """語彙の増減は非破壊のキー追加にならない(欠落も余りも拒否する形のため)。"""
    extra = _with_extra(["reasons"], "A_NEW_REASON")
    missing = _without(["reasons", "ATTENTION"])

    for values in (extra, missing):
        with pytest.raises(RotationRecordError):
            observation_from_output_values(values)


def test_the_reader_checks_the_version_own_vocabulary_and_not_only_the_current_enum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """版が持つ語彙(`_SCHEMA_VOCABULARY`)との一致を、読み手自身が見る(多重の網)。

    現在の語彙と同じ形の記録でも、その版の語彙から 1 語が外れていれば拒否する。
    """
    narrowed = frozenset(_SCHEMA_VOCABULARY[RECORD_SCHEMA_VERSION] - {"ATTENTION"})
    monkeypatch.setitem(module._SCHEMA_VOCABULARY, RECORD_SCHEMA_VERSION, narrowed)

    with pytest.raises(RotationRecordError):
        observation_from_output_values(_values())


def test_the_module_documents_the_version_bump_rule() -> None:
    doc = module.__doc__ or ""

    assert "schema_version" in doc and "版を上げ" in doc


# =================================================================================================
# 4 決定的な記録の ID
# =================================================================================================


def test_only_an_observed_day_uses_the_id_that_pins_the_day() -> None:
    days = _every_status()

    assert audit_id_for(days[DayStatus.OBSERVED]) == "capital_rotation_observation:2026-10-14"
    for status in (
        DayStatus.BATCH_NOT_COMPLETED_IN_WINDOW,
        DayStatus.INPUT_LIMIT_EXCEEDED,
        DayStatus.READ_FAILED,
    ):
        assert audit_id_for(days[status]) == "capital_rotation_observation_gap:2026-10-14"


def test_the_two_id_families_are_distinct_and_deterministic() -> None:
    first = audit_id_for(_observed())
    second = audit_id_for(_observed())

    assert first == second
    assert OBSERVATION_ID_PREFIX != GAP_ID_PREFIX
    assert not GAP_ID_PREFIX.startswith(OBSERVATION_ID_PREFIX + ":")
    assert first.split(":")[0] == OBSERVATION_ID_PREFIX


def test_the_id_depends_only_on_the_day_and_the_kind_not_on_the_counts() -> None:
    other = observe_day(
        target_date=_DAY, batch=BatchFacts(_START, _END), holdings=[HoldingInput(None, {})]
    )

    assert audit_id_for(other) == audit_id_for(_observed())


def test_the_id_carries_no_owner_or_holding_text() -> None:
    for observation in _every_status().values():
        audit_id = audit_id_for(observation)
        assert _SENTINEL not in audit_id
        assert audit_id.startswith("capital_rotation_observation")


# =================================================================================================
# 5 純粋性(AST)・dormant
# =================================================================================================


def _tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def _imports() -> list[str]:
    imported: list[str] = []
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
    return imported


def test_the_module_imports_only_the_standard_library_and_the_domain() -> None:
    for name in _imports():
        assert name.split(".")[0] in {
            "datetime",
            "collections",
            "typing",
            "__future__",
            "jstock_advisor",
        }, name
        if name.startswith("jstock_advisor"):
            assert name.startswith("jstock_advisor.domain."), name
    for forbidden in ("services", "infrastructure", "line_notification", "boto", "config", "os"):
        assert not [m for m in _imports() if forbidden in m.lower().split(".")], forbidden


def test_the_module_has_no_write_port_no_clock_no_environment_no_io() -> None:
    called = {
        node.func.attr
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    names = {node.id for node in ast.walk(_tree()) if isinstance(node, ast.Name)}

    assert not called & {"now", "utcnow", "today", "time", "sleep", "open", "read_text", "getenv"}
    assert not called & {"put_item", "get_item", "query", "scan", "batch_get_item", "invoke"}
    assert not names & {"print", "open", "input", "logger", "logging", "environ"}


def test_nothing_in_src_imports_the_record_module_yet() -> None:
    """dormant: P-b3 の runner だけが import する(配線は P-b4。import 文を AST で見る)。"""
    importers = _importers_of("rotation_observation_record", _MODULE_PATH)

    assert importers == ["services/rotation_observation_runner.py"]
