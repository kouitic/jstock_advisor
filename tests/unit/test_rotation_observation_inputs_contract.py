"""資金入替の観測の『入力の組み立て』の契約テスト(Issue #604 P-b2。dormant)。

対象: `services/rotation_observation_inputs`。

設計: #604 P-b 設計 rev6(…6169320796)・rev8(…6169691749)。

  1  組み立て: 保有の記録と推奨から `HoldingInput` を作る。★ 保有 ID は結果に持たない ★
  2  ★ 失敗の隔離 ★: 保有 1 件の失敗・推奨の失敗が他へ伝わらない / ports が例外を出しても隔離
  3  ★ 上限 ★: 200 件は読む・201 件は 1 件も読まずに上限超過
  4  ★ 総予算 ★: 尽きたら部分結果にせず None(読取の失敗)/ 境界
  5  P-b1 の `observe_day` と組み合わせた結果
  6  純粋性(AST)・dormant
"""

from __future__ import annotations

import ast
import datetime as dt
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.signals.rotation_observation import (
    MAX_HOLDINGS,
    BatchFacts,
    DayStatus,
    HoldingInput,
    observe_day,
)
from jstock_advisor.domain.signals.rotation_observation_reads import (
    RecommendationsRead,
    RecordRead,
    RecordStatus,
    RotationInputsContractError,
)
from jstock_advisor.domain.signals.rotation_source_eligibility import (
    EligibilityWindow,
    HoldingEvaluationFacts,
    RecommendationFacts,
)
from jstock_advisor.services import rotation_observation_inputs as module
from jstock_advisor.services.rotation_observation_inputs import assemble_holding_inputs
from tests.unit.test_rotation_observation_contract import _importers_of
from tests.unit.test_rotation_source_eligibility_contract import facts

_REPO = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO / "src" / "jstock_advisor"
_MODULE_PATH = _PACKAGE / "services" / "rotation_observation_inputs.py"

_JST = dt.timezone(dt.timedelta(hours=9))
_DAY = dt.date(2026, 10, 14)
_START = dt.datetime(2026, 10, 14, 8, 0, 41, tzinfo=_JST)
_END = dt.datetime(2026, 10, 14, 8, 2, 41, tzinfo=_JST)
_INSIDE = dt.datetime(2026, 10, 14, 8, 1, tzinfo=_JST)
_WINDOW = EligibilityWindow(_START, _END)
_SENTINEL = "SENTINEL-OWNER-HOLDING-777"


def _facts(**overrides: Any) -> HoldingEvaluationFacts:
    return facts(evaluated_at=_INSIDE, **overrides)


class _Ids:
    def __init__(self, ids: Sequence[str] | None, *, raises: bool = False) -> None:
        self._ids = ids
        self._raises = raises
        self.calls = 0

    def holding_ids(self) -> Sequence[str] | None:
        self.calls += 1
        if self._raises:
            raise RuntimeError(_SENTINEL)
        return self._ids


class _Records:
    def __init__(
        self, reads: dict[str, RecordRead] | None = None, raises_for: set[str] | None = None
    ) -> None:
        self._reads = reads or {}
        self._raises_for = raises_for or set()
        self.calls: list[str] = []

    def latest_in_window(self, holding_id: str, window: EligibilityWindow) -> RecordRead:
        self.calls.append(holding_id)
        assert window == _WINDOW
        if holding_id in self._raises_for:
            raise RuntimeError(_SENTINEL)
        return self._reads.get(holding_id, RecordRead(RecordStatus.NOT_FOUND))


class _Recs:
    def __init__(self, result: RecommendationsRead | None = None, raises: bool = False) -> None:
        self._result = result or RecommendationsRead()
        self._raises = raises
        self.calls: list[list[str]] = []

    def get_many(self, ids: Sequence[str]) -> RecommendationsRead:
        self.calls.append(list(ids))
        if self._raises:
            raise RuntimeError(_SENTINEL)
        return self._result


def _clock(*values: float) -> Any:
    """残り時間を順に返す fake(尽きたら最後の値を返し続ける)。呼ばれた回数も数える。"""

    class _Remaining:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> float:
            value = values[min(self.calls, len(values) - 1)]
            self.calls += 1
            return value

    return _Remaining()


def _assemble(
    ids: Any,
    records: Any = None,
    recs: Any = None,
    remaining: Any = None,
    reserve: float = 10.0,
) -> tuple[HoldingInput, ...] | None:
    return assemble_holding_inputs(
        window=_WINDOW,
        holding_ids=ids if hasattr(ids, "holding_ids") else _Ids(ids),
        records=records if records is not None else _Records(),
        recommendations=recs if recs is not None else _Recs(),
        remaining_seconds=remaining if remaining is not None else (lambda: 1000.0),
        reserve_seconds=reserve,
    )


# =================================================================================================
# 1 組み立て
# =================================================================================================


def test_a_found_record_becomes_an_input_with_its_pointed_recommendations() -> None:
    pointed = _facts(authoritative_recommendation_id="r1", profit_taking_recommendation_id="r2")
    records = _Records({"h1": RecordRead(RecordStatus.FOUND, pointed)})
    recs = _Recs(
        RecommendationsRead(
            {
                "r1": RecommendationFacts("HOLD"),
                "r2": RecommendationFacts("WATCH"),
                "r9": RecommendationFacts("SELL"),
            }
        )
    )

    result = _assemble(["h1"], records, recs)

    assert result is not None and len(result) == 1
    assert result[0].facts == pointed
    assert set(result[0].recommendations) == {"r1", "r2"}  # 指していない r9 は載せない
    assert not result[0].record_read_failed and not result[0].recommendation_read_failed


def test_a_missing_record_becomes_an_input_without_facts_and_without_a_failure_flag() -> None:
    result = _assemble(["h1"])

    assert result == (HoldingInput(None, {}),)


def test_the_recommendation_ids_are_requested_once_sorted_and_unique() -> None:
    first = _facts(authoritative_recommendation_id="r2", legacy_sell_recommendation_id="r1")
    second = _facts(authoritative_recommendation_id="r1", profit_taking_recommendation_id="r3")
    records = _Records(
        {"h1": RecordRead(RecordStatus.FOUND, first), "h2": RecordRead(RecordStatus.FOUND, second)}
    )
    recs = _Recs()

    _assemble(["h1", "h2"], records, recs)

    assert recs.calls == [["r1", "r2", "r3"]]


def test_the_recommendation_port_is_not_called_when_nothing_is_pointed_to() -> None:
    recs = _Recs()

    _assemble(["h1"], _Records({"h1": RecordRead(RecordStatus.FOUND, _facts())}), recs)

    assert recs.calls == []


def test_the_result_carries_no_holding_identifier() -> None:
    """保有 ID は引くキーとしてだけ使う。結果・repr のどこにも出ない。"""
    records = _Records({_SENTINEL: RecordRead(RecordStatus.FOUND, _facts())})

    result = _assemble([_SENTINEL], records)

    assert result is not None
    assert _SENTINEL not in repr(result)
    assert records.calls == [_SENTINEL]


def test_the_order_of_the_results_follows_the_order_of_the_ids() -> None:
    records = _Records({"b": RecordRead(RecordStatus.FOUND, _facts())})

    result = _assemble(["a", "b", "c"], records)

    assert result is not None
    assert [bool(item.facts) for item in result] == [False, True, False]


def test_zero_holdings_gives_an_empty_tuple_not_a_failure() -> None:
    assert _assemble([]) == ()


# =================================================================================================
# 2 失敗の隔離
# =================================================================================================


def test_one_failed_record_does_not_spread_to_the_others() -> None:
    records = _Records(
        {
            "h1": RecordRead(RecordStatus.FAILED),
            "h2": RecordRead(RecordStatus.FOUND, _facts()),
        }
    )

    result = _assemble(["h1", "h2"], records)

    assert result is not None
    assert result[0] == HoldingInput(None, {}, record_read_failed=True)
    assert result[1].facts is not None and not result[1].record_read_failed


def test_a_port_that_raises_is_isolated_as_one_failed_record() -> None:
    records = _Records({"h2": RecordRead(RecordStatus.FOUND, _facts())}, raises_for={"h1"})

    result = _assemble(["h1", "h2"], records)

    assert result is not None
    assert result[0].record_read_failed and result[1].facts is not None


def test_a_port_that_returns_a_wrong_type_is_isolated_as_a_failure() -> None:
    class _Bad:
        def latest_in_window(self, holding_id: str, window: EligibilityWindow) -> Any:
            return {"not": "a RecordRead"}

    result = _assemble(["h1"], _Bad())

    assert result == (HoldingInput(None, {}, record_read_failed=True),)


def test_a_failed_recommendation_flags_only_the_holdings_that_point_to_it() -> None:
    pointing = _facts(authoritative_recommendation_id="r1")
    other = _facts(authoritative_recommendation_id="r2")
    records = _Records(
        {
            "h1": RecordRead(RecordStatus.FOUND, pointing),
            "h2": RecordRead(RecordStatus.FOUND, other),
        }
    )
    recs = _Recs(RecommendationsRead({"r2": RecommendationFacts("HOLD")}, frozenset({"r1"})))

    result = _assemble(["h1", "h2"], records, recs)

    assert result is not None
    assert result[0].recommendation_read_failed and not result[0].recommendations
    assert not result[1].recommendation_read_failed and set(result[1].recommendations) == {"r2"}


def test_a_recommendation_port_that_raises_fails_every_pointing_holding_but_keeps_records() -> None:
    pointing = _facts(authoritative_recommendation_id="r1")
    records = _Records(
        {
            "h1": RecordRead(RecordStatus.FOUND, pointing),
            "h2": RecordRead(RecordStatus.FOUND, _facts()),
        }
    )

    result = _assemble(["h1", "h2"], records, _Recs(raises=True))

    assert result is not None
    assert result[0].recommendation_read_failed and result[0].facts == pointing
    assert not result[1].recommendation_read_failed


def test_a_recommendation_that_does_not_exist_is_not_a_failure() -> None:
    pointing = _facts(authoritative_recommendation_id="r1")
    records = _Records({"h1": RecordRead(RecordStatus.FOUND, pointing)})

    result = _assemble(["h1"], records, _Recs(RecommendationsRead()))

    assert result is not None
    assert not result[0].recommendation_read_failed and not result[0].recommendations


def test_the_enumeration_failing_or_raising_is_a_failed_read_not_an_empty_day() -> None:
    assert _assemble(None) is None
    assert _assemble(_Ids(None, raises=True)) is None


def test_an_exception_message_never_reaches_the_result() -> None:
    result = _assemble(["h1"], _Records(raises_for={"h1"}), _Recs())

    assert result is not None and _SENTINEL not in repr(result)


# =================================================================================================
# 3 上限
# =================================================================================================


def test_two_hundred_holdings_are_all_read() -> None:
    records = _Records()
    ids = [f"h{i}" for i in range(MAX_HOLDINGS)]

    result = _assemble(ids, records)

    assert result is not None and len(result) == MAX_HOLDINGS
    assert records.calls == ids


def test_over_the_limit_nothing_is_read_and_the_day_becomes_input_limit_exceeded() -> None:
    records, recs = _Records(), _Recs()
    ids = [f"h{i}" for i in range(MAX_HOLDINGS + 1)]

    result = _assemble(ids, records, recs)

    assert records.calls == [] and recs.calls == []
    assert result is not None and len(result) == MAX_HOLDINGS + 1
    observation = observe_day(target_date=_DAY, batch=BatchFacts(_START, _END), holdings=result)
    assert observation.day_status is DayStatus.INPUT_LIMIT_EXCEEDED
    assert observation.holdings_total == 0


def test_a_huge_list_does_not_allocate_a_huge_result() -> None:
    result = _assemble([f"h{i}" for i in range(50_000)])

    assert result is not None and len(result) == MAX_HOLDINGS + 1


# =================================================================================================
# 4 総予算
# =================================================================================================


def test_no_budget_at_the_start_reads_nothing() -> None:
    ids, records = _Ids(["h1"]), _Records()

    result = _assemble(ids, records, remaining=lambda: 10.0, reserve=10.0)  # 残り = 予約

    assert result is None
    assert ids.calls == 0 and records.calls == []


def test_one_second_more_than_the_reserve_is_enough() -> None:
    result = _assemble(["h1"], remaining=lambda: 10.001, reserve=10.0)

    assert result is not None


def test_the_budget_running_out_midway_gives_none_not_a_partial_result() -> None:
    records = _Records({f"h{i}": RecordRead(RecordStatus.FOUND, _facts()) for i in range(5)})
    # 開始の確認 + 保有 h0・h1 の読取の前までは十分、h2 の前で尽きる
    remaining = _clock(100.0, 100.0, 100.0, 5.0)

    result = _assemble([f"h{i}" for i in range(5)], records, remaining=remaining)

    assert result is None
    assert records.calls == ["h0", "h1"]


def test_the_budget_running_out_before_the_recommendation_read_gives_none() -> None:
    pointing = _facts(authoritative_recommendation_id="r1")
    records = _Records({"h1": RecordRead(RecordStatus.FOUND, pointing)})
    recs = _Recs()
    remaining = _clock(100.0, 100.0, 5.0)  # 開始・h1 の前は十分。推奨の前で尽きる

    result = _assemble(["h1"], records, recs, remaining=remaining)

    assert result is None and recs.calls == []


def test_a_failed_read_day_is_what_observe_day_makes_of_a_none() -> None:
    observation = observe_day(target_date=_DAY, batch=BatchFacts(_START, _END), holdings=None)

    assert observation.day_status is DayStatus.READ_FAILED


@pytest.mark.parametrize("bad", [True, None, "10"])
def test_the_reserve_must_be_a_number(bad: Any) -> None:
    with pytest.raises(RotationInputsContractError):
        _assemble(["h1"], reserve=bad)


# =================================================================================================
# 5 P-b1 の観測との組み合わせ
# =================================================================================================


def test_the_assembled_inputs_feed_observe_day_end_to_end() -> None:
    exit_pointing = _facts(authoritative_recommendation_id="r1")
    records = _Records(
        {
            "h1": RecordRead(RecordStatus.FOUND, _facts()),
            "h2": RecordRead(RecordStatus.FOUND, exit_pointing),
            "h3": RecordRead(RecordStatus.FAILED),
        }
    )
    recs = _Recs(RecommendationsRead({"r1": RecommendationFacts("SELL")}))

    inputs = _assemble(["h1", "h2", "h3", "h4"], records, recs)
    observation = observe_day(target_date=_DAY, batch=BatchFacts(_START, _END), holdings=inputs)

    assert observation.day_status is DayStatus.OBSERVED
    assert observation.holdings_total == 4
    assert observation.reasons["NO_EXIT_EVIDENCE"] == 1
    assert observation.reasons["EXIT_RECOMMENDATION"] == 1
    assert observation.reasons["RECORD_MISSING"] == 2  # 失敗 1 + 記録なし 1
    assert observation.record_read_failed == 1


# =================================================================================================
# 読取の結果の型
# =================================================================================================


def test_a_record_read_has_facts_exactly_when_found() -> None:
    with pytest.raises(RotationInputsContractError):
        RecordRead(RecordStatus.FOUND)
    with pytest.raises(RotationInputsContractError):
        RecordRead(RecordStatus.FAILED, _facts())
    with pytest.raises(RotationInputsContractError):
        RecordRead("FOUND", _facts())  # type: ignore[arg-type]


def test_an_id_cannot_be_both_found_and_failed() -> None:
    with pytest.raises(RotationInputsContractError):
        RecommendationsRead({"r1": RecommendationFacts("HOLD")}, frozenset({"r1"}))
    with pytest.raises(RotationInputsContractError):
        RecommendationsRead({}, {"r1"})  # type: ignore[arg-type]


def test_the_found_mapping_is_read_only() -> None:
    read = RecommendationsRead({"r1": RecommendationFacts("HOLD")})

    with pytest.raises(TypeError):
        read.found["r2"] = RecommendationFacts("SELL")  # type: ignore[index]


# =================================================================================================
# 6 純粋性(AST)・dormant
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
        assert name.split(".")[0] in {"collections", "typing", "__future__", "jstock_advisor"}, name
        if name.startswith("jstock_advisor"):
            assert name.startswith("jstock_advisor.domain."), name


def test_the_module_never_imports_a_concrete_client_repository_or_aws_module() -> None:
    for forbidden in ("infrastructure", "boto", "services", "config", "line", "audit"):
        assert not [m for m in _imports() if forbidden in m.lower().split(".")[-2:]], forbidden


def test_the_module_reads_no_clock_no_environment_and_calls_no_aws_operation() -> None:
    called = {
        node.func.attr
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    names = {node.id for node in ast.walk(_tree()) if isinstance(node, ast.Name)}

    assert not called & {"now", "utcnow", "today", "time", "sleep", "open", "getenv"}
    assert not called & {"scan", "put_item", "get_item", "query", "batch_get_item", "invoke"}
    assert not names & {"print", "open", "input", "logger", "logging", "environ", "time"}


def test_nothing_in_src_imports_the_module_yet() -> None:
    """dormant: P-b3 の runner の配線(P-b4)まで、どこからも import されない。"""
    assert _importers_of("rotation_observation_inputs", _MODULE_PATH) == []


def test_the_module_is_importable_by_name() -> None:
    assert module.assemble_holding_inputs is assemble_holding_inputs


def test_the_read_result_types_are_imported_only_by_the_inputs_and_the_reader() -> None:
    reads_path = _PACKAGE / "domain" / "signals" / "rotation_observation_reads.py"

    assert _importers_of("rotation_observation_reads", reads_path) == [
        "infrastructure/aws/rotation_observation_reader.py",
        "services/rotation_observation_inputs.py",
    ]
