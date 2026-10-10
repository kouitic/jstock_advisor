"""L3 PRICE_REGIME(Issue #877 PR-1。#846 の N1。dormant)の契約テスト。

## 何を固定するか(#877 の受入条件 AC-1〜AC-8)

    AC-1 入口 gate が無い: 含み益(cushion)の水準で状態が決まらない
    AC-2 単調: 下落を増やしたとき状態は軽くならない(窓が空になる区間が無い)。
         信頼性による抑制は、理由つきで suppressed に残る
    AC-3 二重 count が無い: 価格由来の facts は regime の 1 票(root = PRICE_PATH)
    AC-4 dormant: どこからも import されない(C0 の AST テストが package 全体を対象にする)
    AC-6 症例固有の分岐・既定の閾値が無い
    AC-7 履歴不足は『悪化と判定しない』(fail-safe)。遷移は同じ入力から再計算できる
    AC-8 状態の重さの順と『悪化したか』が、N3 の入力にできる形で固定されている

## 閾値について

ここで使う数値は、**このテストの中だけの例示値**であり、運用の閾値ではない(値は事前登録 ->
shadow -> replay / backtest -> USER 承認で確定する。module は既定値を持たない)。

## 何を固定しないか

AC-5(baseline の既知の穴が新定義でどう扱われるかの記述的な報告)は、保存データの読取を要する
ため N4(replay)の範囲。arbiter・FULL の判定は N2、再通知は N3、audit 記録は PR-2。
"""

from __future__ import annotations

import ast
import dataclasses
import itertools
import math
from pathlib import Path

import pytest

from jstock_advisor.domain.exit_architecture import price_regime
from jstock_advisor.domain.exit_architecture.decision import (
    ContractViolationError,
    FullEvidence,
)
from jstock_advisor.domain.exit_architecture.determination import Determination
from jstock_advisor.domain.exit_architecture.evidence import (
    Evidence,
    dedupe_by_fact_key,
    distinct_roots,
)
from jstock_advisor.domain.exit_architecture.price_regime import (
    REGIME_ORDER,
    TREND_STATE,
    PriceFacts,
    PriceRegimeResult,
    RegimeThresholds,
    SuppressedRegime,
    TrendReading,
    classify_price_regime,
    giveback_ratio_pct,
    is_worsened,
    regime_votes,
    severity_rank,
    to_regime_verdict,
)
from jstock_advisor.domain.exit_architecture.verdicts import RegimeVerdict
from jstock_advisor.domain.exit_architecture.vocabulary import (
    FullEvidenceKind,
    RegimeState,
    ReliabilityClass,
    RootFactor,
    SuppressionReason,
    UndeterminedReason,
)

#: このテストの中だけの例示値(運用の閾値ではない)
_TH = RegimeThresholds(
    peak_warning_drawdown_pct=5.0,
    downtrend_confirmed_drawdown_pct=10.0,
    breakdown_drawdown_pct=20.0,
)
_ALL_STATES = list(RegimeState)
_SRC = Path(price_regime.__file__)


def _d(value: float | None) -> Determination[float]:
    if value is None:
        return Determination.undetermined(UndeterminedReason.INPUT_MISSING)
    return Determination.of(value)


def _trend(value: TrendReading | None) -> Determination[TrendReading]:
    if value is None:
        return Determination.undetermined(UndeterminedReason.COVERAGE_INSUFFICIENT)
    return Determination.of(value)


def _facts(
    drawdown: float | None = 0.0,
    *,
    peak_gain: float | None = 30.0,
    current_gain: float | None = 30.0,
    trend: TrendReading | None = TrendReading.NOT_DOWN,
) -> PriceFacts:
    return PriceFacts(
        drawdown_from_peak_pct=_d(drawdown),
        peak_gain_pct=_d(peak_gain),
        current_gain_pct=_d(current_gain),
        trend=_trend(trend),
    )


def _classify(
    facts: PriceFacts, reliability: ReliabilityClass = ReliabilityClass.RELIABLE
) -> PriceRegimeResult:
    return classify_price_regime(facts, _TH, reliability)


def _state(result: PriceRegimeResult) -> RegimeState | None:
    return result.state.value


def _current_gain_after_drawdown(peak_gain: float, drawdown: float) -> float:
    """peak の含み益率 g と下落率 d から、現在の含み益率: (1+g)(1-d)-1(%)。"""
    return ((1 + peak_gain / 100) * (1 - drawdown / 100) - 1) * 100


# ---------------------------------------------------------------------------
# (1) 語彙・順序・閾値(案)の固定
# ---------------------------------------------------------------------------


def test_regime_order_covers_every_state_once_from_light_to_heavy() -> None:
    assert REGIME_ORDER == (
        RegimeState.HEALTHY,
        RegimeState.PEAK_WARNING,
        RegimeState.DOWNTREND_CONFIRMED,
        RegimeState.BREAKDOWN,
    )
    assert set(REGIME_ORDER) == set(RegimeState)
    assert [severity_rank(s) for s in REGIME_ORDER] == [0, 1, 2, 3]


def test_trend_reading_and_its_state_mapping_are_fixed() -> None:
    assert {t.name: int(t) for t in TrendReading} == {"NOT_DOWN": 0, "DOWN": 1}
    assert TREND_STATE == {
        TrendReading.NOT_DOWN: RegimeState.HEALTHY,
        TrendReading.DOWN: RegimeState.DOWNTREND_CONFIRMED,
    }
    assert set(TREND_STATE) == set(TrendReading)


def test_a_worse_trend_never_maps_to_a_lighter_state() -> None:
    ranks = [severity_rank(TREND_STATE[t]) for t in sorted(TrendReading)]
    assert ranks == sorted(ranks)


def test_trend_alone_cannot_reach_the_heaviest_state() -> None:
    """BREAKDOWN は peak からの下落でのみ到達する(トレンドだけで最上位にしない)。"""
    assert RegimeState.BREAKDOWN not in set(TREND_STATE.values())


def test_thresholds_have_no_default_values() -> None:
    """値は事前登録で確定する。module は既定の閾値を持たない(AC-6)。"""
    for field in dataclasses.fields(RegimeThresholds):
        assert field.default is dataclasses.MISSING
        assert field.default_factory is dataclasses.MISSING


def test_the_module_defines_no_thresholds_instance() -> None:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            assert not (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "RegimeThresholds"
            )


@pytest.mark.parametrize(
    ("warning", "downtrend", "breakdown"),
    [
        (0.0, 10.0, 20.0),  # 0 以下
        (-1.0, 10.0, 20.0),
        (10.0, 10.0, 20.0),  # 同値
        (10.0, 5.0, 20.0),  # 逆順
        (5.0, 20.0, 10.0),
        (5.0, 10.0, 100.5),  # 100 超
        (float("nan"), 10.0, 20.0),
        (5.0, float("inf"), 20.0),
        (5.0, 10.0, float("nan")),
    ],
)
def test_invalid_thresholds_are_rejected(
    warning: float, downtrend: float, breakdown: float
) -> None:
    with pytest.raises(ValueError):
        RegimeThresholds(warning, downtrend, breakdown)


def test_thresholds_up_to_one_hundred_are_accepted() -> None:
    assert RegimeThresholds(1.0, 50.0, 100.0).breakdown_drawdown_pct == 100.0


def test_every_drawdown_boundary_belongs_to_the_heavier_state() -> None:
    cases = [
        (0.0, RegimeState.HEALTHY),
        (4.99, RegimeState.HEALTHY),
        (5.0, RegimeState.PEAK_WARNING),
        (9.99, RegimeState.PEAK_WARNING),
        (10.0, RegimeState.DOWNTREND_CONFIRMED),
        (19.99, RegimeState.DOWNTREND_CONFIRMED),
        (20.0, RegimeState.BREAKDOWN),
        (100.0, RegimeState.BREAKDOWN),
    ]
    for drawdown, expected in cases:
        assert _state(_classify(_facts(drawdown))) is expected, drawdown


# ---------------------------------------------------------------------------
# (2) 入力の検証(AC-7 の前提: 壊れた値を状態にしない)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1, 100.1])
def test_a_drawdown_outside_zero_to_one_hundred_is_rejected(bad: float) -> None:
    with pytest.raises(ValueError):
        _facts(bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -100.1, -250.0])
def test_a_gain_that_is_not_finite_or_below_minus_one_hundred_is_rejected(bad: float) -> None:
    with pytest.raises(ValueError):
        _facts(peak_gain=bad)
    with pytest.raises(ValueError):
        _facts(current_gain=bad)


def test_a_gain_of_minus_one_hundred_is_the_floor_a_price_of_zero() -> None:
    assert _facts(peak_gain=-100.0, current_gain=-100.0).current_gain_pct.unwrap() == -100.0


def test_undetermined_facts_are_accepted_and_never_validated_as_numbers() -> None:
    facts = _facts(None, peak_gain=None, current_gain=None, trend=None)
    assert not facts.drawdown_from_peak_pct.is_determined


# ---------------------------------------------------------------------------
# (3) AC-1 入口 gate が無い: 含み益の水準で状態が決まらない
# ---------------------------------------------------------------------------

_GAINS = [-99.0, -50.0, -1.0, 0.0, 0.1, 5.0, 19.99, 20.0, 25.0, 30.0, 100.0, 1000.0]


@pytest.mark.parametrize("drawdown", [0.0, 5.0, 12.0, 25.0])
@pytest.mark.parametrize("trend", [TrendReading.NOT_DOWN, TrendReading.DOWN])
def test_the_state_does_not_depend_on_the_current_gain(
    drawdown: float, trend: TrendReading
) -> None:
    states = {
        _state(_classify(_facts(drawdown, current_gain=gain, trend=trend)))
        for gain in [*_GAINS, None]
    }
    assert len(states) == 1


@pytest.mark.parametrize("drawdown", [0.0, 5.0, 12.0, 25.0])
def test_the_state_does_not_depend_on_the_peak_gain(drawdown: float) -> None:
    """peak の含み益は吐き出し率の導出にだけ使う。状態の入力ではない。"""
    states = {_state(_classify(_facts(drawdown, peak_gain=gain))) for gain in [*_GAINS[3:], None]}
    assert len(states) == 1


def test_the_old_gate_hole_does_not_exist_when_the_current_gain_is_below_the_old_floor() -> None:
    """現行は『現在の含み益が下限未満』だと信号が消える。ここでは重い下落は重い状態になる。"""
    low_gain = _facts(25.0, peak_gain=30.0, current_gain=_current_gain_after_drawdown(30.0, 25.0))
    assert low_gain.current_gain_pct.unwrap() < 20.0
    assert _state(_classify(low_gain)) is RegimeState.BREAKDOWN


# ---------------------------------------------------------------------------
# (4) AC-2 単調性: 下落を増やしたとき、状態は軽くならない(窓が空にならない)
# ---------------------------------------------------------------------------

_DRAWDOWNS = [i / 2 for i in range(0, 201)]  # 0.0 ... 100.0(0.5 刻み。境界を含む)
_TRENDS = [TrendReading.NOT_DOWN, TrendReading.DOWN, None]


@pytest.mark.parametrize("reliability", list(ReliabilityClass))
@pytest.mark.parametrize("trend", _TRENDS)
@pytest.mark.parametrize("peak_gain", [None, 0.0, 8.0, 30.0, 400.0])
def test_a_deeper_drawdown_never_gives_a_lighter_state(
    reliability: ReliabilityClass, trend: TrendReading | None, peak_gain: float | None
) -> None:
    previous: RegimeState | None = None
    for drawdown in _DRAWDOWNS:
        current_gain = (
            None if peak_gain is None else _current_gain_after_drawdown(peak_gain, drawdown)
        )
        result = _classify(
            _facts(drawdown, peak_gain=peak_gain, current_gain=current_gain, trend=trend),
            reliability,
        )
        state = _state(result)
        if previous is not None:
            # 一度確定した状態は、下落が深くなっても UNDETERMINED には戻らず、軽くもならない
            assert state is not None, (drawdown, reliability)
            assert severity_rank(state) >= severity_rank(previous), drawdown
        if state is not None:
            previous = state


@pytest.mark.parametrize("trend", _TRENDS)
@pytest.mark.parametrize("peak_gain", [None, 8.0, 30.0])
def test_the_suppressed_candidate_under_unusable_reliability_follows_the_drawdown(
    trend: TrendReading | None, peak_gain: float | None
) -> None:
    """UNUSABLE では状態が常に UNDETERMINED になる。監査に残るのは suppressed の候補なので、
    その候補が(信頼性が使えるときの状態と同じで)下落に対して単調であることを固定する。"""
    previous: RegimeState | None = None
    for drawdown in _DRAWDOWNS:
        current_gain = (
            None if peak_gain is None else _current_gain_after_drawdown(peak_gain, drawdown)
        )
        facts = _facts(drawdown, peak_gain=peak_gain, current_gain=current_gain, trend=trend)
        suppressed = _classify(facts, ReliabilityClass.UNUSABLE).suppressed
        usable_state = _state(_classify(facts, ReliabilityClass.RELIABLE))
        if usable_state is None or usable_state is RegimeState.HEALTHY:
            # 一度候補を残したあとで、候補が消える(軽くなる)ことはない
            assert previous is None, drawdown
            assert suppressed == ()
            continue
        assert len(suppressed) == 1, drawdown
        assert suppressed[0].reason is SuppressionReason.RELIABILITY_CAP
        # 抑えた候補は、信頼性が使えるときの状態そのもの(一段軽くして残さない)
        assert suppressed[0].candidate is usable_state, drawdown
        if previous is not None:
            assert severity_rank(suppressed[0].candidate) >= severity_rank(previous), drawdown
        previous = suppressed[0].candidate
    assert previous is RegimeState.BREAKDOWN


def test_the_state_reaches_the_heaviest_state_after_the_gain_is_gone() -> None:
    sequence = [
        _state(
            _classify(_facts(d, peak_gain=30.0, current_gain=_current_gain_after_drawdown(30.0, d)))
        )
        for d in (0.0, 6.0, 12.0, 25.0, 60.0)
    ]
    assert sequence == [
        RegimeState.HEALTHY,
        RegimeState.PEAK_WARNING,
        RegimeState.DOWNTREND_CONFIRMED,
        RegimeState.BREAKDOWN,
        RegimeState.BREAKDOWN,
    ]


def test_a_down_trend_never_lightens_the_state() -> None:
    for drawdown in _DRAWDOWNS:
        up = _state(_classify(_facts(drawdown, trend=TrendReading.NOT_DOWN)))
        down = _state(_classify(_facts(drawdown, trend=TrendReading.DOWN)))
        assert up is not None
        assert down is not None
        assert severity_rank(down) >= severity_rank(up), drawdown


def test_giveback_ratio_grows_with_the_drawdown_for_a_fixed_peak_gain() -> None:
    for peak_gain in (1.0, 8.0, 30.0, 400.0):
        values = [giveback_ratio_pct(_d(peak_gain), _d(d)).unwrap() for d in _DRAWDOWNS]
        assert values == sorted(values)
        assert len(set(values)) == len(values)


# ---------------------------------------------------------------------------
# (5) 吐き出し率は従属量(独立な票にしない)
# ---------------------------------------------------------------------------


def test_giveback_ratio_follows_the_formula_d_times_one_plus_g_over_g() -> None:
    # g = 50%, d = 10%: 0.10 * 1.5 / 0.5 = 0.30
    assert giveback_ratio_pct(_d(50.0), _d(10.0)).unwrap() == pytest.approx(30.0)
    # d = 0 なら 0、peak の含み益をすべて失う下落(現在の含み益 = 0)なら 100
    assert giveback_ratio_pct(_d(50.0), _d(0.0)).unwrap() == 0.0
    drawdown_to_zero_gain = 100 * 0.5 / 1.5
    assert giveback_ratio_pct(_d(50.0), _d(drawdown_to_zero_gain)).unwrap() == pytest.approx(100.0)


def test_giveback_ratio_is_consistent_with_the_current_gain() -> None:
    for peak_gain, drawdown in itertools.product((10.0, 30.0, 400.0), (0.0, 5.0, 12.0, 40.0)):
        current = _current_gain_after_drawdown(peak_gain, drawdown)
        expected = (peak_gain - current) / peak_gain * 100
        assert giveback_ratio_pct(_d(peak_gain), _d(drawdown)).unwrap() == pytest.approx(expected)


@pytest.mark.parametrize("peak_gain", [None, 0.0, -10.0])
def test_giveback_ratio_is_undetermined_without_a_profit_to_give_back(
    peak_gain: float | None,
) -> None:
    ratio = giveback_ratio_pct(_d(peak_gain), _d(10.0))
    assert not ratio.is_determined
    assert ratio.value is None


def test_giveback_ratio_is_undetermined_without_a_drawdown() -> None:
    assert not giveback_ratio_pct(_d(30.0), _d(None)).is_determined


def test_an_undetermined_giveback_ratio_does_not_make_the_state_undetermined() -> None:
    result = _classify(_facts(12.0, peak_gain=None))
    assert not result.giveback_ratio_pct.is_determined
    assert _state(result) is RegimeState.DOWNTREND_CONFIRMED


def test_the_result_carries_the_giveback_ratio_as_a_derived_value() -> None:
    result = _classify(_facts(10.0, peak_gain=50.0))
    assert result.giveback_ratio_pct.unwrap() == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# (6) 三値: 値を捏造しない・UNDETERMINED は悪化と数えない
# ---------------------------------------------------------------------------


def test_no_determined_dimension_gives_an_undetermined_state() -> None:
    result = _classify(_facts(None, trend=None))
    assert not result.state.is_determined
    assert result.state.reason is UndeterminedReason.NOT_EVALUATED


@pytest.mark.parametrize(
    ("drawdown", "trend"),
    [(None, TrendReading.NOT_DOWN), (0.0, None), (4.0, None)],
)
def test_healthy_needs_every_dimension_to_be_determined(
    drawdown: float | None, trend: TrendReading | None
) -> None:
    result = _classify(_facts(drawdown, trend=trend))
    assert not result.state.is_determined
    assert result.state.reason is UndeterminedReason.COVERAGE_INSUFFICIENT


@pytest.mark.parametrize(
    ("drawdown", "trend", "expected"),
    [
        (None, TrendReading.DOWN, RegimeState.DOWNTREND_CONFIRMED),
        (5.0, None, RegimeState.PEAK_WARNING),
        (25.0, None, RegimeState.BREAKDOWN),
    ],
)
def test_a_known_deterioration_stands_even_if_another_dimension_is_unknown(
    drawdown: float | None, trend: TrendReading | None, expected: RegimeState
) -> None:
    assert _state(_classify(_facts(drawdown, trend=trend))) is expected


def test_an_undetermined_state_cannot_be_read_as_a_boolean_or_unwrapped() -> None:
    result = _classify(_facts(None, trend=None))
    with pytest.raises(TypeError):
        bool(result.state)
    assert result.state.value is None


def test_per_dimension_states_are_reported_for_audit() -> None:
    result = _classify(_facts(12.0, trend=TrendReading.NOT_DOWN))
    assert result.drawdown_state.unwrap() is RegimeState.DOWNTREND_CONFIRMED
    assert result.trend_state.unwrap() is RegimeState.HEALTHY
    assert result.state.unwrap() is RegimeState.DOWNTREND_CONFIRMED


# ---------------------------------------------------------------------------
# (7) 信頼性による抑制は別に扱い、理由を残す(UJ-2 の条件)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reliability", [ReliabilityClass.RELIABLE, ReliabilityClass.DEGRADED])
def test_only_unusable_reliability_changes_the_state(reliability: ReliabilityClass) -> None:
    for drawdown in (0.0, 6.0, 12.0, 25.0):
        reliable = _classify(_facts(drawdown), ReliabilityClass.RELIABLE)
        other = _classify(_facts(drawdown), reliability)
        assert other == reliable
        assert other.suppressed == ()


@pytest.mark.parametrize(
    ("drawdown", "candidate"),
    [
        (6.0, RegimeState.PEAK_WARNING),
        (12.0, RegimeState.DOWNTREND_CONFIRMED),
        (25.0, RegimeState.BREAKDOWN),
    ],
)
def test_unusable_reliability_suppresses_the_state_and_records_the_candidate(
    drawdown: float, candidate: RegimeState
) -> None:
    result = _classify(_facts(drawdown), ReliabilityClass.UNUSABLE)
    assert not result.state.is_determined
    assert result.state.reason is UndeterminedReason.RELIABILITY_UNUSABLE
    assert result.suppressed == (SuppressedRegime(candidate, SuppressionReason.RELIABILITY_CAP),)


def test_unusable_reliability_records_nothing_when_there_was_nothing_to_suppress() -> None:
    healthy = _classify(_facts(0.0), ReliabilityClass.UNUSABLE)
    assert healthy.suppressed == ()
    assert healthy.state.reason is UndeterminedReason.RELIABILITY_UNUSABLE
    unknown = _classify(_facts(None, trend=None), ReliabilityClass.UNUSABLE)
    assert unknown.suppressed == ()


def test_a_suppressed_regime_is_never_turned_into_a_vote() -> None:
    result = _classify(_facts(25.0), ReliabilityClass.UNUSABLE)
    assert regime_votes(result) == ()


# ---------------------------------------------------------------------------
# (8) AC-3 価格由来の facts は regime の 1 票
# ---------------------------------------------------------------------------


def test_every_price_fact_that_fires_collapses_into_one_vote_with_the_price_path_root() -> None:
    # 下落(BREAKDOWN)・トレンド(DOWN)・吐き出し率が、すべて成立している
    result = _classify(_facts(25.0, peak_gain=30.0, current_gain=0.0, trend=TrendReading.DOWN))
    votes = regime_votes(result)
    assert len(votes) == 1
    assert votes[0].root_factor is RootFactor.PRICE_PATH
    assert distinct_roots(votes) == frozenset({RootFactor.PRICE_PATH})
    assert dedupe_by_fact_key(votes + votes) == votes


@pytest.mark.parametrize("drawdown", _DRAWDOWNS)
@pytest.mark.parametrize("trend", _TRENDS)
def test_the_vote_count_never_exceeds_one(drawdown: float, trend: TrendReading | None) -> None:
    votes = regime_votes(_classify(_facts(drawdown, trend=trend)))
    assert len(votes) <= 1
    assert {v.root_factor for v in votes} <= {RootFactor.PRICE_PATH}


def test_healthy_and_undetermined_states_cast_no_vote() -> None:
    assert regime_votes(_classify(_facts(0.0))) == ()
    assert regime_votes(_classify(_facts(None, trend=None))) == ()
    assert regime_votes(_classify(_facts(0.0, trend=None))) == ()


def test_every_unhealthy_state_casts_exactly_one_vote_keyed_by_its_state() -> None:
    keys = set()
    for drawdown in (6.0, 12.0, 25.0):
        votes = regime_votes(_classify(_facts(drawdown)))
        assert len(votes) == 1
        keys.add(votes[0].fact_key)
    assert len(keys) == 3


@pytest.mark.parametrize("kind", list(FullEvidenceKind))
def test_the_regime_vote_alone_can_never_build_a_full_evidence(kind: FullEvidenceKind) -> None:
    """regime(PRICE_PATH)は、どの kind でも FULL の独立根拠にならない(OP-5)。"""
    votes = regime_votes(_classify(_facts(25.0, trend=TrendReading.DOWN)))
    assert len(votes) == 1
    with pytest.raises(ContractViolationError):
        FullEvidence(kind, votes)


def test_the_regime_vote_is_a_triggered_evidence_with_a_fixed_source() -> None:
    (vote,) = regime_votes(_classify(_facts(25.0)))
    assert isinstance(vote, Evidence)
    assert vote.source == "L3_PRICE_REGIME"
    assert vote.counts_as_independent


# ---------------------------------------------------------------------------
# (9) AC-7 / AC-8 遷移: 同じ入力から再計算でき、悪化は N3 の入力にできる
# ---------------------------------------------------------------------------


def _determined(state: RegimeState) -> Determination[RegimeState]:
    return Determination.of(state)


_UNKNOWN_STATE: Determination[RegimeState] = Determination.undetermined(
    UndeterminedReason.INPUT_MISSING
)


@pytest.mark.parametrize(("previous", "current"), list(itertools.product(_ALL_STATES, _ALL_STATES)))
def test_is_worsened_is_true_only_when_the_current_state_is_strictly_heavier(
    previous: RegimeState, current: RegimeState
) -> None:
    expected = severity_rank(current) > severity_rank(previous)
    assert is_worsened(_determined(previous), _determined(current)) is expected


@pytest.mark.parametrize("state", _ALL_STATES)
def test_a_missing_history_is_never_a_worsening(state: RegimeState) -> None:
    """履歴不足(以前が UNDETERMINED)・今日が UNDETERMINED は、悪化と判定しない(fail-safe)。"""
    assert is_worsened(_UNKNOWN_STATE, _determined(state)) is False
    assert is_worsened(_determined(state), _UNKNOWN_STATE) is False
    assert is_worsened(_UNKNOWN_STATE, _UNKNOWN_STATE) is False


def test_the_same_severity_continuing_or_easing_is_not_a_worsening() -> None:
    for state in _ALL_STATES:
        assert is_worsened(_determined(state), _determined(state)) is False
    assert (
        is_worsened(_determined(RegimeState.BREAKDOWN), _determined(RegimeState.HEALTHY)) is False
    )


def test_the_transition_is_recomputed_from_the_same_inputs_without_any_stored_state() -> None:
    yesterday = _facts(6.0)
    today = _facts(12.0)
    first = (_classify(yesterday), _classify(today))
    second = (_classify(yesterday), _classify(today))
    assert first == second
    assert is_worsened(first[0].state, first[1].state) is True


def test_a_worsening_within_the_same_peak_episode_is_detected() -> None:
    """同じ peak の局面で下落が深くなった(症例の型)ことが、状態の悪化として現れる。"""
    peak_gain = 40.0
    yesterday = _classify(
        _facts(6.0, peak_gain=peak_gain, current_gain=_current_gain_after_drawdown(peak_gain, 6.0))
    )
    today = _classify(
        _facts(
            15.0, peak_gain=peak_gain, current_gain=_current_gain_after_drawdown(peak_gain, 15.0)
        )
    )
    assert is_worsened(yesterday.state, today.state)


def test_to_regime_verdict_carries_the_state_the_previous_state_and_the_cushion() -> None:
    today = _classify(_facts(12.0, peak_gain=30.0, current_gain=-5.0))
    previous = _determined(RegimeState.PEAK_WARNING)
    verdict = to_regime_verdict(today, previous)
    assert isinstance(verdict, RegimeVerdict)
    assert verdict.state == today.state
    assert verdict.previous_state == previous
    assert verdict.current_gain_pct.unwrap() == -5.0
    assert verdict.peak_gain_pct.unwrap() == 30.0


def test_to_regime_verdict_keeps_unknown_cushion_values_unknown() -> None:
    today = _classify(_facts(12.0, peak_gain=None, current_gain=None))
    verdict = to_regime_verdict(today, _UNKNOWN_STATE)
    assert not verdict.peak_gain_pct.is_determined
    assert not verdict.current_gain_pct.is_determined
    assert not verdict.previous_state.is_determined


# ---------------------------------------------------------------------------
# (10) AC-6 症例固有の分岐・既定の閾値が無い / dormant
# ---------------------------------------------------------------------------


def test_the_module_has_no_case_specific_code_or_literal_thresholds() -> None:
    source = _SRC.read_text(encoding="utf-8")
    assert "9536" not in source
    tree = ast.parse(source)
    # 閾値になりうる小数のリテラルが無い(整数は 0 / 1 / 2 の添字と、% <-> 比率の 100 だけ)
    floats = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    ]
    assert floats == []
    integers = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    }
    assert integers <= {0, 1, 2, 100}


def test_the_module_does_not_read_the_clock_or_the_environment() -> None:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported <= {"__future__", "math", "dataclasses", "enum", "jstock_advisor"}


def test_every_public_result_is_immutable() -> None:
    result = _classify(_facts(12.0))
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.state = Determination.of(RegimeState.HEALTHY)  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        _TH.breakdown_drawdown_pct = 1.0  # type: ignore[misc]


def test_results_are_finite_numbers_only() -> None:
    for drawdown in _DRAWDOWNS:
        ratio = giveback_ratio_pct(_d(30.0), _d(drawdown))
        assert math.isfinite(ratio.unwrap())


# 公開関数 giveback_ratio_pct は、PriceFacts を通らない入力からも『確定した値』を捏造しない


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -100.1, -1e9])
def test_giveback_ratio_rejects_a_peak_gain_that_is_not_finite_or_out_of_range(bad: float) -> None:
    with pytest.raises(ValueError):
        giveback_ratio_pct(_d(bad), _d(10.0))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -0.1, 100.1, 250.0])
def test_giveback_ratio_rejects_a_drawdown_that_is_not_finite_or_out_of_range(bad: float) -> None:
    with pytest.raises(ValueError):
        giveback_ratio_pct(_d(30.0), _d(bad))


@pytest.mark.parametrize("tiny", [1e-310, 5e-324])
def test_giveback_ratio_is_undetermined_when_it_cannot_be_represented(tiny: float) -> None:
    """peak の含み益が極小の正の値だと桁があふれる。inf を『確定した値』にしない。"""
    ratio = giveback_ratio_pct(_d(tiny), _d(10.0))
    assert not ratio.is_determined
    assert ratio.reason is UndeterminedReason.GUARD_NOT_MET


def test_giveback_ratio_never_returns_a_determined_non_finite_value() -> None:
    peak_gains = [None, -100.0, -50.0, 0.0, 5e-324, 1e-300, 1e-9, 1.0, 30.0, 1e6, 1e300]
    drawdowns = [None, 0.0, 1e-300, 0.5, 50.0, 100.0]
    for peak_gain, drawdown in itertools.product(peak_gains, drawdowns):
        ratio = giveback_ratio_pct(_d(peak_gain), _d(drawdown))
        if ratio.is_determined:
            assert math.isfinite(ratio.unwrap()), (peak_gain, drawdown)
            assert ratio.unwrap() >= 0, (peak_gain, drawdown)
