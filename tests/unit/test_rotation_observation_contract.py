"""資金入替の観測の純粋部分 `rotation_observation` の契約テスト(Issue #604 P-b1。dormant)。

設計: #604 P-b 設計 rev4(…6168001528)・rev5(…6168319086)・設計レビュー PASS(…6168292046)。

  1  語彙: 理由コード 13 種 → 適格性 3 値の対応が、P-a の導出の全経路と一致する
  2  バッチ完了条件(★ fail-closed ★): 欠落・逆転・日跨ぎ・時刻の型・UTC と JST の日付のずれ
  3  日の集計: 件数の和・保有の識別子を持たない入力・上限(200)の境界・部分結果にしない
  4  1 件の失敗の隔離: 保有 1 件の読取の失敗が他へ伝わらない
  5  観測の型の整合: 整合しない値は作れない(件数の和・日の状態と時刻)
  6  純粋性(AST)・dormant(どこからも import されない)
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import random
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.signals import rotation_observation as module
from jstock_advisor.domain.signals.capital_rotation import SourceEligibility
from jstock_advisor.domain.signals.rotation_observation import (
    ELIGIBILITY_VALUES,
    MAX_HOLDINGS,
    REASON_ELIGIBILITY,
    BatchCompletionState,
    BatchFacts,
    BatchJudgement,
    CapitalRotationObservation,
    DayStatus,
    HoldingInput,
    RotationObservationError,
    judge_batch_completion,
    observe_day,
)
from jstock_advisor.domain.signals.rotation_source_eligibility import (
    NON_EXIT_CLASSIFICATION,
    EligibilityReason,
    EligibilityWindow,
    RecommendationClass,
    RecommendationFacts,
    derive_source_eligibility,
)
from tests.unit.test_rotation_source_eligibility_contract import facts, of_type

_REPO = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO / "src" / "jstock_advisor"
_MODULE_PATH = _PACKAGE / "domain" / "signals" / "rotation_observation.py"

_JST = dt.timezone(dt.timedelta(hours=9))
_DAY = dt.date(2026, 10, 14)
_START = dt.datetime(2026, 10, 14, 8, 0, 41, tzinfo=_JST)
_END = dt.datetime(2026, 10, 14, 8, 2, 41, tzinfo=_JST)
_INSIDE = dt.datetime(2026, 10, 14, 8, 1, tzinfo=_JST)
_OUTSIDE = dt.datetime(2026, 10, 14, 12, 30, tzinfo=_JST)  # 同日の手動実行(窓の外)
_COMPLETE = BatchFacts(_START, _END)
_WINDOW = EligibilityWindow(_START, _END)

R = EligibilityReason


def _holding(record: Any = ..., recommendations: Any = None, **flags: bool) -> HoldingInput:
    record = facts(evaluated_at=_INSIDE) if record is ... else record
    return HoldingInput(record, recommendations or {}, **flags)


# 13 種の理由コードへ到達する保有(P-a の導出の全経路)。(理由, 記録, 推奨)
def _class_member(kind: RecommendationClass) -> RecommendationType:
    return next(t for t, c in NON_EXIT_CLASSIFICATION.items() if c is kind)


def _reaching(reason: EligibilityReason) -> HoldingInput:
    pointed = {"authoritative_recommendation_id": "r1"}
    table: dict[EligibilityReason, HoldingInput] = {
        R.RECORD_MISSING: HoldingInput(None, {}),
        R.OUTSIDE_WINDOW: _holding(facts(evaluated_at=_OUTSIDE)),
        R.CATEGORY_UNKNOWN: _holding(
            facts(evaluated_at=_INSIDE, authoritative_outcome_category="bogus")
        ),
        R.EVALUATION_NOT_COMPLETED: _holding(
            facts(evaluated_at=_INSIDE, authoritative_outcome_category="review")
        ),
        R.ENGINE_UNKNOWN: _holding(facts(evaluated_at=_INSIDE, authoritative_engine=None)),
        R.ENGINE_NOT_AUTHORITATIVE: _holding(
            facts(evaluated_at=_INSIDE, authoritative_engine="HOLDING_DECISION_SCORE")
        ),
        R.RECOMMENDATION_MISSING: _holding(facts(evaluated_at=_INSIDE, **pointed)),
        R.TYPE_UNCLASSIFIED: _holding(
            facts(evaluated_at=_INSIDE, **pointed), of_type(RecommendationType.BUY)
        ),
        R.EXIT_RECOMMENDATION: _holding(
            facts(evaluated_at=_INSIDE, **pointed), of_type(RecommendationType.SELL)
        ),
        R.REVIEW_RECOMMENDATION: _holding(
            facts(evaluated_at=_INSIDE, **pointed),
            of_type(_class_member(RecommendationClass.REVIEW)),
        ),
        R.ATTENTION: _holding(
            facts(evaluated_at=_INSIDE, **pointed),
            of_type(_class_member(RecommendationClass.ATTENTION)),
        ),
        R.NOTIFIED_OR_SUPPRESSED: _holding(
            facts(evaluated_at=_INSIDE, authoritative_outcome_category="suppressed")
        ),
        R.NO_EXIT_EVIDENCE: _holding(),
    }
    return table[reason]


def _day(holdings: Any, batch: BatchFacts | None = _COMPLETE) -> CapitalRotationObservation:
    return observe_day(target_date=_DAY, batch=batch, holdings=holdings)


# =================================================================================================
# 1 語彙
# =================================================================================================


def test_every_reason_belongs_to_exactly_one_eligibility_group() -> None:
    assert set(REASON_ELIGIBILITY) == set(EligibilityReason)
    assert set(REASON_ELIGIBILITY.values()) <= set(ELIGIBILITY_VALUES)


def test_the_three_eligibility_values_are_the_source_eligibility_values() -> None:
    assert set(ELIGIBILITY_VALUES) == {e.value for e in SourceEligibility}
    assert len(ELIGIBILITY_VALUES) == 3


@pytest.mark.parametrize("reason", list(EligibilityReason))
def test_each_reason_is_reached_by_the_derivation_with_the_group_the_table_says(
    reason: EligibilityReason,
) -> None:
    holding = _reaching(reason)

    result = derive_source_eligibility(holding.facts, holding.recommendations, _WINDOW)

    assert result.reason is reason
    assert result.eligibility.value == REASON_ELIGIBILITY[reason]


def test_the_grouping_has_8_unknown_4_blocked_and_1_eligible_reason() -> None:
    groups = Counter(REASON_ELIGIBILITY.values())

    assert groups == {"UNKNOWN": 8, "BLOCKED_BY_EXIT": 4, "ELIGIBLE": 1}


# =================================================================================================
# 2 バッチ完了条件
# =================================================================================================


def _judge(batch: BatchFacts | None, day: dt.date = _DAY) -> BatchJudgement:
    return judge_batch_completion(batch, day)


def test_a_batch_with_ordered_times_on_the_target_day_is_complete_with_its_window() -> None:
    judgement = _judge(_COMPLETE)

    assert judgement.state is BatchCompletionState.COMPLETE
    assert judgement.window == EligibilityWindow(_START, _END)


def test_the_window_includes_both_ends_for_the_holding_records() -> None:
    at_start = _holding(facts(evaluated_at=_START))
    at_end = _holding(facts(evaluated_at=_END))
    just_after = _holding(facts(evaluated_at=_END + dt.timedelta(seconds=1)))

    observation = _day([at_start, at_end, just_after])

    assert observation.reasons["NO_EXIT_EVIDENCE"] == 2
    assert observation.reasons["OUTSIDE_WINDOW"] == 1


def test_a_batch_without_a_completion_time_is_not_complete() -> None:
    judgement = _judge(BatchFacts(_START, None))

    assert judgement.state is BatchCompletionState.NOT_COMPLETE
    assert judgement.window is None


@pytest.mark.parametrize(
    "batch",
    [
        None,
        BatchFacts(None, None),
        BatchFacts(None, _END),
        BatchFacts(_END, _START),  # 逆転
        BatchFacts(  # 日跨ぎ(開始は対象日・完了は翌日)
            dt.datetime(2026, 10, 14, 23, 59, tzinfo=_JST),
            dt.datetime(2026, 10, 15, 0, 1, tzinfo=_JST),
        ),
        BatchFacts(  # 別の日のバッチ(前日)
            dt.datetime(2026, 10, 13, 8, 0, tzinfo=_JST),
            dt.datetime(2026, 10, 13, 8, 2, tzinfo=_JST),
        ),
        BatchFacts(dt.datetime(2026, 10, 13, 8, 0, tzinfo=_JST), None),  # 前日の未完了の行
    ],
)
def test_a_missing_reversed_or_other_day_batch_is_unreadable_never_complete(
    batch: BatchFacts | None,
) -> None:
    judgement = _judge(batch)

    assert judgement.state is BatchCompletionState.UNREADABLE
    assert judgement.window is None


def test_a_zero_width_window_is_complete() -> None:
    judgement = _judge(BatchFacts(_START, _START))

    assert judgement.state is BatchCompletionState.COMPLETE


def test_the_target_day_is_the_jst_day_not_the_utc_day() -> None:
    """08:30 JST の 10/14 = UTC では 10/13 23:30。UTC の暦日で比べると『別の日』と誤る。"""
    start = dt.datetime(2026, 10, 13, 23, 30, tzinfo=dt.UTC)
    end = dt.datetime(2026, 10, 13, 23, 40, tzinfo=dt.UTC)

    assert _judge(BatchFacts(start, end)).state is BatchCompletionState.COMPLETE
    assert _judge(BatchFacts(start, end), dt.date(2026, 10, 13)).state is (
        BatchCompletionState.UNREADABLE
    )


def test_late_evening_utc_that_is_the_next_jst_day_is_judged_by_jst() -> None:
    start = dt.datetime(2026, 10, 14, 15, 30, tzinfo=dt.UTC)  # = 10/15 00:30 JST
    end = dt.datetime(2026, 10, 14, 15, 40, tzinfo=dt.UTC)

    assert _judge(BatchFacts(start, end)).state is BatchCompletionState.UNREADABLE
    assert _judge(BatchFacts(start, end), dt.date(2026, 10, 15)).state is (
        BatchCompletionState.COMPLETE
    )


def test_a_naive_time_is_a_contract_error_not_a_silent_default() -> None:
    with pytest.raises(RotationObservationError):
        BatchFacts(dt.datetime(2026, 10, 14, 8, 0), None)
    with pytest.raises(RotationObservationError):
        BatchFacts(_START, dt.datetime(2026, 10, 14, 8, 2))


@pytest.mark.parametrize("bad", [dt.datetime(2026, 10, 14, 0, 0, tzinfo=_JST), "2026-10-14", None])
def test_the_target_day_must_be_a_date(bad: Any) -> None:
    with pytest.raises(RotationObservationError):
        judge_batch_completion(_COMPLETE, bad)


def test_a_judgement_has_a_window_exactly_when_it_is_complete() -> None:
    with pytest.raises(RotationObservationError):
        BatchJudgement(BatchCompletionState.COMPLETE, None)
    with pytest.raises(RotationObservationError):
        BatchJudgement(BatchCompletionState.NOT_COMPLETE, _WINDOW)


# =================================================================================================
# 3 日の集計
# =================================================================================================


def test_an_observed_day_counts_every_reason_and_every_eligibility() -> None:
    holdings = [_reaching(reason) for reason in EligibilityReason]

    observation = _day(holdings)

    assert observation.day_status is DayStatus.OBSERVED
    assert observation.holdings_total == 13
    assert dict(observation.reasons) == {reason.value: 1 for reason in EligibilityReason}
    assert dict(observation.eligibility) == {
        "ELIGIBLE": 1,
        "BLOCKED_BY_EXIT": 4,
        "UNKNOWN": 8,
    }


def test_the_counts_add_up_over_a_random_mixture_and_match_an_independent_count() -> None:
    rng = random.Random(604)  # noqa: S311 - テストの再現性のための固定 seed
    reasons = list(EligibilityReason)
    drawn = [rng.choice(reasons) for _ in range(120)]
    holdings = [_reaching(reason) for reason in drawn]

    observation = _day(holdings)

    expected = Counter(reason.value for reason in drawn)
    assert {k: v for k, v in observation.reasons.items() if v} == dict(expected)
    assert observation.holdings_total == 120
    assert sum(observation.eligibility.values()) == 120
    for group in ELIGIBILITY_VALUES:
        assert observation.eligibility[group] == sum(
            n for r, n in expected.items() if REASON_ELIGIBILITY[EligibilityReason(r)] == group
        )


def test_zero_holdings_on_a_complete_day_is_an_observed_empty_day() -> None:
    observation = _day([])

    assert observation.day_status is DayStatus.OBSERVED
    assert observation.holdings_total == 0
    assert set(observation.reasons.values()) == {0}


def test_the_reasons_always_carry_all_thirteen_keys_including_zeros() -> None:
    observation = _day([_reaching(R.NO_EXIT_EVIDENCE)])

    assert len(observation.reasons) == 13
    assert set(observation.reasons) == {r.value for r in EligibilityReason}
    assert observation.reasons["EXIT_RECOMMENDATION"] == 0


def test_the_holding_input_carries_no_identifier_so_none_can_reach_the_record() -> None:
    names = {f.name for f in dataclasses.fields(HoldingInput)}

    assert names == {"facts", "recommendations", "record_read_failed", "recommendation_read_failed"}
    obs_names = {f.name for f in dataclasses.fields(CapitalRotationObservation)}
    for word in ("owner", "holding_id", "stock", "code", "amount", "shares", "recommendation_id"):
        assert not [n for n in obs_names if word in n], word


def test_the_limit_is_200_and_two_hundred_holdings_are_still_observed() -> None:
    assert MAX_HOLDINGS == 200

    observation = _day([_reaching(R.NO_EXIT_EVIDENCE)] * 200)

    assert observation.day_status is DayStatus.OBSERVED
    assert observation.holdings_total == 200


def test_over_the_limit_is_never_a_partial_result() -> None:
    observation = _day([_reaching(R.NO_EXIT_EVIDENCE)] * 201)

    assert observation.day_status is DayStatus.INPUT_LIMIT_EXCEEDED
    assert observation.holdings_total == 0
    assert set(observation.reasons.values()) == {0}
    assert set(observation.eligibility.values()) == {0}
    assert observation.batch_state is BatchCompletionState.COMPLETE


def test_an_incomplete_batch_day_observes_nothing_and_says_so() -> None:
    observation = _day([_reaching(R.NO_EXIT_EVIDENCE)], BatchFacts(_START, None))

    assert observation.day_status is DayStatus.BATCH_NOT_COMPLETED_IN_WINDOW
    assert observation.batch_state is BatchCompletionState.NOT_COMPLETE
    assert observation.batch_started_at == _START
    assert observation.batch_completed_at is None
    assert observation.holdings_total == 0


@pytest.mark.parametrize("batch", [None, BatchFacts(None, None), BatchFacts(_END, _START)])
def test_an_unreadable_batch_day_is_a_failed_read_with_no_times_and_no_counts(
    batch: BatchFacts | None,
) -> None:
    observation = _day([_reaching(R.NO_EXIT_EVIDENCE)], batch)

    assert observation.day_status is DayStatus.READ_FAILED
    assert observation.batch_state is BatchCompletionState.UNREADABLE
    assert observation.batch_started_at is None and observation.batch_completed_at is None
    assert observation.holdings_total == 0


def test_a_failed_enumeration_of_the_holdings_is_a_failed_read_not_an_empty_day() -> None:
    observation = _day(None)

    assert observation.day_status is DayStatus.READ_FAILED
    assert observation.batch_state is BatchCompletionState.COMPLETE
    assert observation.batch_completed_at == _END
    assert observation.holdings_total == 0


def test_the_window_seconds_are_the_batch_duration() -> None:
    assert _day([]).window_seconds == 120
    assert _day(None).window_seconds == 120
    assert _day([], BatchFacts(_START, None)).window_seconds is None
    assert _day([], None).window_seconds is None


# =================================================================================================
# 4 失敗の隔離
# =================================================================================================


def test_one_unreadable_record_does_not_spread_to_the_other_holdings() -> None:
    failed = HoldingInput(None, {}, record_read_failed=True)
    ok = _reaching(R.NO_EXIT_EVIDENCE)

    observation = _day([failed, ok, ok])

    assert observation.day_status is DayStatus.OBSERVED
    assert observation.reasons["RECORD_MISSING"] == 1
    assert observation.reasons["NO_EXIT_EVIDENCE"] == 2
    assert observation.record_read_failed == 1
    assert observation.recommendation_read_failed == 0


def test_one_unreadable_recommendation_makes_only_that_holding_unknown() -> None:
    pointed = facts(evaluated_at=_INSIDE, authoritative_recommendation_id="r1")
    failed = HoldingInput(pointed, {}, recommendation_read_failed=True)

    observation = _day([failed, _reaching(R.NO_EXIT_EVIDENCE)])

    assert observation.reasons["RECOMMENDATION_MISSING"] == 1
    assert observation.reasons["NO_EXIT_EVIDENCE"] == 1
    assert observation.recommendation_read_failed == 1
    assert observation.eligibility["ELIGIBLE"] == 1


def test_a_record_that_failed_to_read_cannot_also_carry_facts() -> None:
    with pytest.raises(RotationObservationError):
        HoldingInput(facts(evaluated_at=_INSIDE), {}, record_read_failed=True)


def test_a_read_failure_is_never_counted_as_eligible() -> None:
    failed = [HoldingInput(None, {}, record_read_failed=True)] * 5

    observation = _day(failed)

    assert observation.eligibility["ELIGIBLE"] == 0
    assert observation.eligibility["UNKNOWN"] == 5


def test_a_non_holding_input_is_a_contract_error() -> None:
    with pytest.raises(RotationObservationError):
        _day([object()])


# =================================================================================================
# 5 観測の型の整合
# =================================================================================================


def _valid_kwargs(**overrides: Any) -> dict[str, Any]:
    base = _day([_reaching(R.NO_EXIT_EVIDENCE), _reaching(R.EXIT_RECOMMENDATION)])
    values: dict[str, Any] = {
        "target_date": base.target_date,
        "batch_state": base.batch_state,
        "batch_started_at": base.batch_started_at,
        "batch_completed_at": base.batch_completed_at,
        "holdings_total": base.holdings_total,
        "eligibility": dict(base.eligibility),
        "reasons": dict(base.reasons),
        "record_read_failed": 0,
        "recommendation_read_failed": 0,
        "day_status": base.day_status,
    }
    values.update(overrides)
    return values


def test_the_valid_baseline_builds() -> None:
    assert CapitalRotationObservation(**_valid_kwargs()).holdings_total == 2


def _reasons_with(**changes: int) -> dict[str, int]:
    reasons = dict(_valid_kwargs()["reasons"])
    reasons.update(changes)
    return reasons


@pytest.mark.parametrize(
    "overrides",
    [
        {"holdings_total": 3},  # 件数の和が合わない
        {"eligibility": {"ELIGIBLE": 2, "BLOCKED_BY_EXIT": 0, "UNKNOWN": 0}},  # 理由と食い違う
        {"reasons": _reasons_with(NO_EXIT_EVIDENCE=0, OUTSIDE_WINDOW=1)},  # 群の件数が食い違う
        {"eligibility": {"ELIGIBLE": 1, "BLOCKED_BY_EXIT": 1}},  # キーの欠落
        {"eligibility": {"ELIGIBLE": 1, "BLOCKED_BY_EXIT": 1, "UNKNOWN": 0, "x": 0}},  # 余り
        {"reasons": {k: v for k, v in _reasons_with().items() if k != "ATTENTION"}},
        {"reasons": {**_reasons_with(), "SOMETHING_NEW": 0}},  # 語彙外のキー
        {"holdings_total": -1},
        {"holdings_total": True},
        {"holdings_total": 2.0},
        {"record_read_failed": 3},  # 総数を超える
        {"recommendation_read_failed": -1},
        {"target_date": dt.datetime(2026, 10, 14, tzinfo=_JST)},
        {"target_date": "2026-10-14"},
        {"batch_state": "COMPLETE"},  # enum ではない
        {"day_status": "OBSERVED"},
        {"batch_started_at": dt.datetime(2026, 10, 14, 8, 0)},  # naive
        {"batch_completed_at": _START - dt.timedelta(seconds=1)},  # 逆転
        {"batch_started_at": None},  # 完了なのに開始が無い
        {"batch_completed_at": None},  # 完了なのに完了時刻が無い
    ],
)
def test_an_inconsistent_observation_cannot_be_built(overrides: dict[str, Any]) -> None:
    with pytest.raises(RotationObservationError):
        CapitalRotationObservation(**_valid_kwargs(**overrides))


_ZERO_ELIGIBILITY = dict.fromkeys(ELIGIBILITY_VALUES, 0)
_ZERO_REASONS = {r.value: 0 for r in EligibilityReason}


def _no_counts(**overrides: Any) -> dict[str, Any]:
    values = _valid_kwargs(
        holdings_total=0, eligibility=dict(_ZERO_ELIGIBILITY), reasons=dict(_ZERO_REASONS)
    )
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    "overrides",
    [
        # 完了前の日は、完了前のバッチ・集計 0 だけ
        {"day_status": DayStatus.BATCH_NOT_COMPLETED_IN_WINDOW},  # バッチが完了のまま
        {
            "day_status": DayStatus.BATCH_NOT_COMPLETED_IN_WINDOW,
            "batch_state": BatchCompletionState.NOT_COMPLETE,
            "batch_completed_at": None,
            "record_read_failed": 1,  # 0 でない
        },
        # 読めない日は、集計を持たない・完了前を読取の失敗と呼ばない
        {
            "day_status": DayStatus.READ_FAILED,
            "batch_state": BatchCompletionState.UNREADABLE,
        },  # 時刻が残っている
        {
            "day_status": DayStatus.READ_FAILED,
            "batch_state": BatchCompletionState.NOT_COMPLETE,
            "batch_completed_at": None,
        },
        # 上限超過の日は、完了したバッチ・集計 0 だけ
        {
            "day_status": DayStatus.INPUT_LIMIT_EXCEEDED,
            "batch_state": BatchCompletionState.UNREADABLE,
            "batch_started_at": None,
            "batch_completed_at": None,
        },
        # 観測の日は、完了したバッチだけ
        {
            "day_status": DayStatus.OBSERVED,
            "batch_state": BatchCompletionState.NOT_COMPLETE,
            "batch_completed_at": None,
        },
    ],
)
def test_the_day_status_must_agree_with_the_batch_and_the_counts(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(RotationObservationError):
        CapitalRotationObservation(**_no_counts(**overrides))


def _one_holding_counts() -> dict[str, Any]:
    reasons = dict(_ZERO_REASONS)
    reasons["NO_EXIT_EVIDENCE"] = 1
    return {
        "holdings_total": 1,
        "eligibility": {"ELIGIBLE": 1, "BLOCKED_BY_EXIT": 0, "UNKNOWN": 0},
        "reasons": reasons,
    }


def test_the_one_holding_baseline_is_consistent() -> None:
    assert CapitalRotationObservation(**_valid_kwargs(**_one_holding_counts())).holdings_total == 1


def test_an_incomplete_batch_day_with_consistent_counts_is_still_rejected() -> None:
    """件数が整合していても、完了前の日は集計を持てない(日の状態と件数の一致)。"""
    with pytest.raises(RotationObservationError):
        CapitalRotationObservation(
            **_valid_kwargs(
                **_one_holding_counts(),
                day_status=DayStatus.BATCH_NOT_COMPLETED_IN_WINDOW,
                batch_state=BatchCompletionState.NOT_COMPLETE,
                batch_completed_at=None,
            )
        )


@pytest.mark.parametrize(
    "field", ["holdings_total", "record_read_failed", "recommendation_read_failed"]
)
def test_a_bool_is_not_a_count_even_when_it_would_add_up(field: str) -> None:
    """True == 1。1 件の日に True を件数として渡しても、整合して見えるだけで受け入れない。"""
    with pytest.raises(RotationObservationError):
        CapitalRotationObservation(**_valid_kwargs(**{**_one_holding_counts(), field: True}))


def test_a_bool_inside_the_count_mappings_is_not_a_count() -> None:
    counts = _one_holding_counts()
    counts["eligibility"] = {"ELIGIBLE": True, "BLOCKED_BY_EXIT": 0, "UNKNOWN": 0}
    with pytest.raises(RotationObservationError):
        CapitalRotationObservation(**_valid_kwargs(**counts))
    counts = _one_holding_counts()
    counts["reasons"] = {**counts["reasons"], "NO_EXIT_EVIDENCE": True}
    with pytest.raises(RotationObservationError):
        CapitalRotationObservation(**_valid_kwargs(**counts))


def test_a_failed_read_or_over_limit_day_with_counts_is_rejected() -> None:
    for status in (DayStatus.READ_FAILED, DayStatus.INPUT_LIMIT_EXCEEDED):
        with pytest.raises(RotationObservationError):
            CapitalRotationObservation(**_valid_kwargs(day_status=status))


def test_the_error_message_never_contains_the_offending_value() -> None:
    sentinel = "SENTINEL-VALUE-123"

    with pytest.raises(RotationObservationError) as caught:
        CapitalRotationObservation(**_valid_kwargs(holdings_total=sentinel))

    assert sentinel not in str(caught.value)


def test_the_observation_does_not_alias_its_input_counts() -> None:
    eligibility = dict(_valid_kwargs()["eligibility"])
    observation = CapitalRotationObservation(**_valid_kwargs(eligibility=eligibility))

    eligibility["ELIGIBLE"] = 99

    assert observation.eligibility["ELIGIBLE"] == 1
    with pytest.raises(TypeError):
        observation.eligibility["ELIGIBLE"] = 5  # type: ignore[index]


# =================================================================================================
# 決定性
# =================================================================================================


def test_the_observation_does_not_depend_on_the_order_of_the_holdings() -> None:
    holdings = [_reaching(reason) for reason in EligibilityReason] * 3
    shuffled = list(holdings)
    random.Random(7).shuffle(shuffled)  # noqa: S311

    assert _day(holdings) == _day(shuffled)


def test_the_same_input_gives_the_same_output_and_the_inputs_are_not_mutated() -> None:
    recommendations = {"r1": RecommendationFacts("SELL")}
    holding = _holding(
        facts(evaluated_at=_INSIDE, authoritative_recommendation_id="r1"), recommendations
    )
    before = dict(recommendations)

    first, second = _day([holding]), _day([holding])

    assert first == second
    assert recommendations == before


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
        assert name.split(".")[0] in {
            "datetime",
            "collections",
            "dataclasses",
            "enum",
            "types",
            "typing",
            "__future__",
            "jstock_advisor",
        }, name
        if name.startswith("jstock_advisor"):
            assert name.startswith("jstock_advisor.domain."), name
    for forbidden in ("services", "infrastructure", "line_notification", "boto", "config", "os"):
        assert not [m for m in _imports() if forbidden in m.lower().split(".")], forbidden


def test_the_module_does_not_import_the_capital_rotation_evaluator() -> None:
    """P-b は evaluate_capital_rotation を呼ばない(rev4 差分 2)。型も import しない。"""
    assert not [m for m in _imports() if m.endswith("capital_rotation")]
    names = {node.id for node in ast.walk(_tree()) if isinstance(node, ast.Name)}
    assert "evaluate_capital_rotation" not in names


def test_the_module_reads_no_clock_no_environment_and_does_no_io() -> None:
    called = {
        node.func.attr
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    names = {node.id for node in ast.walk(_tree()) if isinstance(node, ast.Name)}

    assert not called & {"now", "utcnow", "today", "time", "sleep", "open", "read_text", "getenv"}
    assert not names & {"print", "open", "input", "logger", "logging", "environ"}


def _importers_of(target: str, own: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if path == own or target not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *[f"{node.module}.{a.name}" for a in node.names]]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            else:
                continue
            if any(n.endswith("." + target) for n in names):
                found.append(path.relative_to(_PACKAGE).as_posix())
                break
    return found


def test_only_the_p_b_modules_import_it_so_it_is_dormant() -> None:
    """dormant: P-b の記録の形・入力の組み立て・reader だけ。配線は P-b3・P-b4。"""
    assert _importers_of("rotation_observation", _MODULE_PATH) == [
        "domain/signals/rotation_observation_record.py",
        "infrastructure/aws/rotation_observation_reader.py",
        "services/rotation_observation_inputs.py",
    ]


def test_the_module_is_importable_by_name() -> None:
    assert module.MAX_HOLDINGS == MAX_HOLDINGS
