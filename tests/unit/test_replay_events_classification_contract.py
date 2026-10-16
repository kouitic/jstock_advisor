"""比較の単位・母集団の勘定・同時発火率・OLD / NEW の分類・再現性の契約テスト(Issue #880 R1a)。

対象: `replay_events`(a-1・a-2・a-5)・`replay_classification`(a-3)・`replay_reproducibility`(a-8)。
fixture は合成のみ(実在の銘柄・保有・価格・値を使わない)。

  E1  母集団の勘定: 全行 = 除外(理由別・0 件も)+ 症例 + 分析対象。除外が症例より先
  E2  event: 連続した観測を 1 件にまとめ、登録された間隔を超えたら切る。行を独立にしない
  E3  3 層の件数(観測 / event / 保有)
  E4  同時発火率: 2×2 + UNDETERMINED を別に数える(欠損として落とさない)
  C1  分類 5 種 + UNDETERMINED(HOLD にも一致にも数えない)
  C2  遷移行列(全 9 マス + UNDETERMINED は別)・理由コードの件数(認識できないコードも数える)
  R1  再現性: 一致率・不一致の内訳・対が 0 件なら進めない
"""

from __future__ import annotations

import itertools
import random
from fractions import Fraction

import pytest

from jstock_advisor.domain.backtest.errors import ReplayInputError
from jstock_advisor.domain.backtest.replay_classification import (
    Difference,
    Judgement,
    ReplayAction,
    classify_pair,
    difference_counts,
    reason_counts,
    transition_label,
    transition_matrix,
)
from jstock_advisor.domain.backtest.replay_events import (
    EXCLUSION_ORDER,
    CoFireCounts,
    PopulationExclusion,
    Fire,
    Observation,
    account_population,
    build_events,
    co_fire_counts,
    layer_counts,
    phi_coefficient,
)
from jstock_advisor.domain.backtest.replay_reproducibility import (
    may_proceed,
    reproducibility,
)

_KIND = "PRICE_REGIME"


def _obs(ref: str, day: int, key: str | None = "peak-a", **kw: object) -> Observation:
    return Observation(ref, day, _KIND, key, **kw)  # type: ignore[arg-type]


# =================================================================================================
# E1 母集団の勘定
# =================================================================================================


def test_the_population_account_sums_and_keeps_zero_reasons() -> None:
    rows = [
        _obs("h-1", 1),
        _obs("h-1", 2, is_first_evaluation=True),
        _obs("h-2", 1, exclusion=PopulationExclusion.LEGACY),
        _obs("h-2", 2, exclusion=PopulationExclusion.LEGACY),
        _obs("h-2", 3, exclusion=PopulationExclusion.BASELINE_INTEGRITY_ERROR),
        _obs("h-3", 1, held_out=True),
        _obs("h-3", 2, held_out=True, exclusion=PopulationExclusion.DATA_INSUFFICIENT),  # 除外が先
    ]

    account = account_population(rows)

    assert account.total == 7
    assert account.analyzed == 2 and account.first_evaluations == 1
    assert account.held_out == 1
    assert [r for r, _ in account.excluded] == list(EXCLUSION_ORDER)  # 固定の順序
    assert dict(account.excluded) == {
        PopulationExclusion.LEGACY: 2,
        PopulationExclusion.EVALUATED_AT_MISMATCH: 0,  # 件数 0 も省略しない
        PopulationExclusion.BEFORE_PLAN: 0,
        PopulationExclusion.BASELINE_INTEGRITY_ERROR: 1,
        PopulationExclusion.DATA_INSUFFICIENT: 1,
    }
    assert account.total == account.excluded_total + account.held_out + account.analyzed


def test_the_population_account_of_nothing_is_all_zero() -> None:
    account = account_population([])

    assert (account.total, account.analyzed, account.held_out, account.excluded_total) == (
        0,
        0,
        0,
        0,
    )
    assert len(account.excluded) == len(PopulationExclusion)


def test_the_four_t1_exclusion_groups_and_data_shortage_are_the_whole_vocabulary() -> None:
    assert {r.value for r in PopulationExclusion} == {
        "LEGACY",
        "EVALUATED_AT_MISMATCH",
        "BEFORE_PLAN",
        "BASELINE_INTEGRITY_ERROR",
        "DATA_INSUFFICIENT",
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"holding_ref": "", "day_index": 1, "phase_kind": _KIND},
        {"holding_ref": "h", "day_index": -1, "phase_kind": _KIND},
        {"holding_ref": "h", "day_index": True, "phase_kind": _KIND},
        {"holding_ref": "h", "day_index": 1, "phase_kind": ""},
        {"holding_ref": "h", "day_index": 1, "phase_kind": _KIND, "phase_key": ""},
        {"holding_ref": "h", "day_index": 1, "phase_kind": _KIND, "exclusion": "LEGACY"},
        {"holding_ref": "h", "day_index": 1, "phase_kind": _KIND, "held_out": 1},
    ],
)
def test_an_invalid_observation_is_refused_without_echoing_values(bad: dict[str, object]) -> None:
    with pytest.raises(ReplayInputError) as raised:
        Observation(**bad)  # type: ignore[arg-type]
    assert "h-" not in str(raised.value)


# =================================================================================================
# E2 event
# =================================================================================================


def test_consecutive_observations_of_one_phase_are_one_event() -> None:
    rows = [_obs("h-1", d) for d in (3, 4, 5, 6)]

    [event] = build_events(rows, max_gap=1)

    assert (event.first_day_index, event.last_day_index, event.observations) == (3, 6, 4)


def test_a_gap_larger_than_the_registered_value_splits_the_event() -> None:
    rows = [_obs("h-1", d) for d in (1, 2, 5, 6)]

    assert [(e.first_day_index, e.last_day_index) for e in build_events(rows, max_gap=1)] == [
        (1, 2),
        (5, 6),
    ]
    assert len(build_events(rows, max_gap=3)) == 1  # 登録された間隔が違えば、結果も違う(既定なし)


def test_different_phases_and_holdings_never_merge() -> None:
    rows = [_obs("h-1", 1, "a"), _obs("h-1", 2, "b"), _obs("h-2", 1, "a"), _obs("h-1", 3, "a")]

    events = build_events(rows, max_gap=2)

    assert sorted((e.holding_ref, e.phase_key) for e in events) == [
        ("h-1", "a"),
        ("h-1", "b"),
        ("h-2", "a"),
    ]
    assert next(e for e in events if (e.holding_ref, e.phase_key) == ("h-1", "a")).observations == 2


def test_excluded_held_out_and_phaseless_rows_make_no_event() -> None:
    rows = [
        _obs("h-1", 1, exclusion=PopulationExclusion.LEGACY),
        _obs("h-1", 2, held_out=True),
        _obs("h-1", 3, key=None),
    ]

    assert build_events(rows, max_gap=1) == ()


def test_the_events_do_not_depend_on_the_order_of_the_rows() -> None:
    rows = [_obs(f"h-{h}", d, k) for h in (1, 2) for d in (1, 2, 4, 8) for k in ("a", "b")]
    expected = build_events(rows, max_gap=2)
    shuffler = random.Random(7)  # 合成の固定 seed(時計を使わない)

    for _ in range(25):
        shuffled = list(rows)
        shuffler.shuffle(shuffled)
        assert build_events(shuffled, max_gap=2) == expected


@pytest.mark.parametrize("gap", [0, -1, True, "1", 1.5, None])
def test_the_gap_is_a_required_positive_integer(gap: object) -> None:
    with pytest.raises(ReplayInputError):
        build_events([_obs("h-1", 1)], max_gap=gap)  # type: ignore[arg-type]


def test_the_gap_has_no_default() -> None:
    with pytest.raises(TypeError):
        build_events([_obs("h-1", 1)])  # type: ignore[call-arg]


# =================================================================================================
# E3 3 層の件数
# =================================================================================================


def test_the_three_layers_are_counted_separately() -> None:
    rows = [_obs("h-1", d) for d in (1, 2, 3)] + [_obs("h-2", 1), _obs("h-3", 1, held_out=True)]

    counts = layer_counts(rows, build_events(rows, max_gap=1))

    assert (counts.observations, counts.events, counts.holdings) == (4, 2, 2)
    # 観測 ≥ event ≥ 保有(行を独立のサンプルにしない・保有数は小さい)
    assert counts.observations >= counts.events >= counts.holdings


# =================================================================================================
# E4 同時発火率
# =================================================================================================

_F, _N, _U = Fire.FIRED, Fire.NOT_FIRED, Fire.UNDETERMINED


def test_the_co_fire_table_counts_all_four_cells_and_undetermined_separately() -> None:
    pairs = [
        (_F, _F),
        (_F, _F),
        (_F, _N),
        (_N, _F),
        (_N, _N),
        (_N, _N),
        (_N, _N),
        (_U, _F),
        (_F, _U),
    ]

    counts = co_fire_counts(pairs)

    assert counts == CoFireCounts(both=2, only_first=1, only_second=1, neither=3, undetermined=2)
    assert counts.total == len(pairs)  # 1 件も落とさない(UNDETERMINED は欠損にしない)
    assert counts.determined == 7


def test_phi_is_none_when_a_margin_is_zero_and_signed_otherwise() -> None:
    assert phi_coefficient(co_fire_counts([(_F, _F), (_N, _N)])) == 1.0
    assert phi_coefficient(co_fire_counts([(_F, _N), (_N, _F)])) == -1.0
    assert phi_coefficient(co_fire_counts([(_F, _F), (_F, _F)])) is None  # 分散なし
    assert phi_coefficient(co_fire_counts([])) is None
    mixed = phi_coefficient(co_fire_counts([(_F, _F), (_F, _N), (_N, _F), (_N, _N), (_N, _N)]))
    assert mixed is not None and -1 < mixed < 1


@pytest.mark.parametrize("bad", [[(_F,)], [(_F, "FIRED")], ["FF"], [(1, 2)]])
def test_a_malformed_pair_is_refused(bad: list[object]) -> None:
    with pytest.raises(ReplayInputError):
        co_fire_counts(bad)  # type: ignore[arg-type]


# =================================================================================================
# C1 分類
# =================================================================================================

_HOLD = Judgement(ReplayAction.HOLD)
_PARTIAL_VALUE = Judgement(ReplayAction.PARTIAL, "VALUE_EXIT")
_PARTIAL_PROFIT = Judgement(ReplayAction.PARTIAL, "PROFIT_PROTECTION")
_FULL_VALUE = Judgement(ReplayAction.FULL, "VALUE_EXIT")
_UNDETERMINED = Judgement(ReplayAction.HOLD, undetermined=True)


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (_HOLD, _HOLD, Difference.MATCH),
        (_PARTIAL_VALUE, _PARTIAL_VALUE, Difference.MATCH),
        (_FULL_VALUE, _FULL_VALUE, Difference.MATCH),
        (_HOLD, _PARTIAL_VALUE, Difference.NEW_ONLY),
        (_HOLD, _FULL_VALUE, Difference.NEW_ONLY),
        (_PARTIAL_VALUE, _HOLD, Difference.OLD_ONLY),
        (_FULL_VALUE, _HOLD, Difference.OLD_ONLY),
        (_FULL_VALUE, _PARTIAL_VALUE, Difference.STRENGTH_DIFF),
        (_PARTIAL_VALUE, _FULL_VALUE, Difference.STRENGTH_DIFF),
        (_PARTIAL_VALUE, _PARTIAL_PROFIT, Difference.CLASS_DIFF),
        (_HOLD, _UNDETERMINED, Difference.UNDETERMINED),
        (_FULL_VALUE, _UNDETERMINED, Difference.UNDETERMINED),
    ],
)
def test_each_pair_has_exactly_the_documented_difference(
    old: Judgement, new: Judgement, expected: Difference
) -> None:
    assert classify_pair(old, new) is expected


def test_every_action_pair_is_classified() -> None:
    judgements = [
        Judgement(a, None if a is ReplayAction.HOLD else "VALUE_EXIT") for a in ReplayAction
    ]
    seen = {classify_pair(o, n) for o, n in itertools.product(judgements, judgements)}

    assert seen == {
        Difference.MATCH,
        Difference.NEW_ONLY,
        Difference.OLD_ONLY,
        Difference.STRENGTH_DIFF,
    }


def test_an_undetermined_new_is_never_counted_as_hold_or_match() -> None:
    assert classify_pair(_HOLD, _UNDETERMINED) is not Difference.MATCH
    assert transition_label(_FULL_VALUE, _UNDETERMINED) == "UNDETERMINED"
    assert transition_label(_FULL_VALUE, _PARTIAL_VALUE) == "FULL->PARTIAL"


def test_a_recorded_old_judgement_cannot_be_undetermined() -> None:
    with pytest.raises(ReplayInputError):
        classify_pair(_UNDETERMINED, _HOLD)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "HOLD"},
        {"action": ReplayAction.FULL, "undetermined": True},
        {"action": ReplayAction.HOLD, "undetermined": 1},
        {"action": ReplayAction.FULL, "exit_class": ""},
    ],
)
def test_an_invalid_judgement_is_refused(kwargs: dict[str, object]) -> None:
    with pytest.raises(ReplayInputError):
        Judgement(**kwargs)  # type: ignore[arg-type]


def test_difference_counts_keep_every_kind_in_a_fixed_order() -> None:
    pairs = [(_HOLD, _HOLD), (_HOLD, _FULL_VALUE), (_HOLD, _UNDETERMINED), (_FULL_VALUE, _HOLD)]

    counts = difference_counts(pairs)

    assert [d for d, _ in counts] == list(Difference)
    assert dict(counts) == {
        Difference.MATCH: 1,
        Difference.NEW_ONLY: 1,
        Difference.OLD_ONLY: 1,
        Difference.STRENGTH_DIFF: 0,
        Difference.CLASS_DIFF: 0,
        Difference.UNDETERMINED: 1,
    }


# =================================================================================================
# C2 遷移行列・理由コード
# =================================================================================================


def test_the_transition_matrix_has_all_nine_cells_and_keeps_undetermined_apart() -> None:
    pairs = [
        (_FULL_VALUE, _PARTIAL_VALUE),
        (_FULL_VALUE, _PARTIAL_VALUE),
        (_HOLD, _HOLD),
        (_HOLD, _UNDETERMINED),
    ]

    matrix = transition_matrix(pairs)

    assert len(matrix.cells) == 9
    assert {(o, n): c for o, n, c in matrix.cells}[(ReplayAction.FULL, ReplayAction.PARTIAL)] == 2
    assert {(o, n): c for o, n, c in matrix.cells}[(ReplayAction.HOLD, ReplayAction.HOLD)] == 1
    assert matrix.undetermined == 1  # HOLD の列へ入れない
    assert matrix.total == len(pairs)


def test_reason_codes_are_counted_and_unknown_codes_are_not_dropped() -> None:
    vocabulary = frozenset({"EXHAUSTION_ONLY", "MITIGATION", "GUARD_UNMET"})

    result = reason_counts(
        ["MITIGATION", "MITIGATION", "SURPRISE", "EXHAUSTION_ONLY", 5, None], vocabulary
    )  # type: ignore[list-item]

    assert dict(result.counts) == {"EXHAUSTION_ONLY": 1, "GUARD_UNMET": 0, "MITIGATION": 2}
    assert result.unrecognized == 3  # 落とさない
    assert [c for c, _ in result.counts] == sorted(vocabulary)


def test_the_vocabulary_must_be_registered_by_the_caller() -> None:
    with pytest.raises(ReplayInputError):
        reason_counts([], {"A"})  # type: ignore[arg-type]  # frozenset でない
    with pytest.raises(TypeError):
        reason_counts([])  # type: ignore[call-arg]  # 既定なし


# =================================================================================================
# R1 再現性
# =================================================================================================


def test_reproducibility_counts_matches_and_groups_mismatches() -> None:
    pairs = [("HOLD", "HOLD")] * 8 + [("FULL", "PARTIAL")] * 2 + [("HOLD", "PARTIAL")]

    result = reproducibility(pairs)

    assert (result.total, result.matched, result.mismatched) == (11, 8, 3)
    assert result.rate == Fraction(8, 11)
    assert result.mismatches == (("FULL", "PARTIAL", 2), ("HOLD", "PARTIAL", 1))


def test_a_pair_that_differs_but_starts_the_same_is_still_a_mismatch() -> None:
    result = reproducibility([("FULL", "FULL_NEW"), ("HOLD", "HOLD")])

    assert (result.total, result.matched) == (2, 1)
    assert result.mismatches == (("FULL", "FULL_NEW", 1),)


def test_proceeding_requires_the_registered_agreement_and_never_an_empty_set() -> None:
    perfect = reproducibility([("A", "A")] * 4)
    imperfect = reproducibility([("A", "A")] * 3 + [("A", "B")])

    assert may_proceed(perfect, required=Fraction(1))
    assert not may_proceed(imperfect, required=Fraction(1))  # 不一致なら比較に進まない(C-2)
    assert may_proceed(imperfect, required=Fraction(3, 4))
    assert reproducibility([]).rate is None
    assert not may_proceed(reproducibility([]), required=Fraction(0))  # 空を合格にしない


@pytest.mark.parametrize("required", [0.9, 1, -Fraction(1, 2), Fraction(3, 2), None])
def test_the_required_agreement_is_a_registered_fraction(required: object) -> None:
    with pytest.raises(ReplayInputError):
        may_proceed(reproducibility([("A", "A")]), required=required)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [[("A",)], [("A", "")], [("A", 1)], ["AB"]])
def test_a_malformed_reproducibility_pair_is_refused(bad: list[object]) -> None:
    with pytest.raises(ReplayInputError):
        reproducibility(bad)  # type: ignore[arg-type]
