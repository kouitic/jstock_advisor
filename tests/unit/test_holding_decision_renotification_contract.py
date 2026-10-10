"""保有判断の再通知条件 R1〜R5(Issue #890 PR-1。dormant)の契約テスト。

固定するもの
  AC-R1 スコアの悪化(前回の配信時点との累積比較・境界・改善・モデル版違い)
  AC-R2 判定の変化(変化すべて / 悪化のみ)
  AC-R3 新たに確認済みの hard gate(確認状態 4 値 × 方針・集合差・キーワード一致のみ)
  AC-R4 決算後(期末の大小・鮮度 STALE・二度成立しない・使わない方式)
  AC-R5 売却目安価格の変化(境界・種類違い・現在値へのフォールバックの有無)
  共通  決定性・5 条件の独立・配信されなかった日は翌日も成立・配信後は同じ状態で成立しない・
        前回が使えないときの理由・periodic の扱い・保存形式の読み書き・入力の検証
  meta  dormant(どこからも import されない)・標準ライブラリのみ・時計を使わない・
        policy / config に既定値が無い・module に事業上の数値リテラルが無い

時間意味論: R4 は日付の大小で分岐する(T3)。テストは固定の日付リテラルのみを使い、
wall clock・freezegun・営業日カレンダーを使わない(C-BS: 実際に分岐する状態のみ)。
"""

from __future__ import annotations

import ast
import itertools
import math
from datetime import date
from pathlib import Path

import pytest

from jstock_advisor.domain.signals import holding_decision_renotification as mod
from jstock_advisor.domain.signals.holding_decision_renotification import (
    HD_RENOTIFY_STATE_KEY,
    STATE_VERSION,
    Condition,
    ConditionStatus,
    DecisionChangeScope,
    EarningsDataFreshness,
    EarningsMode,
    GateConfirmation,
    HdNotifyState,
    KeywordOnlyHandling,
    PeriodicPolicy,
    Reason,
    RenotificationConfig,
    RenotificationPolicy,
    ScoreBasis,
    SellPriceReference,
    SellReference,
    StateUnavailable,
    decide_hd_renotification,
    extract_hd_state,
    serialize_hd_state,
)

R1 = Condition.R1_SCORE_DETERIORATION
R2 = Condition.R2_DECISION_CHANGE
R3 = Condition.R3_NEW_HARD_GATE
R4 = Condition.R4_AFTER_EARNINGS
R5 = Condition.R5_SELL_PRICE_CHANGE

# config/holding_decision_rules.yaml の `renotification` の値(呼び出し側が渡す形)。
CFG = RenotificationConfig(
    score_deterioration=10.0,
    on_decision_change=True,
    on_new_hard_gate=True,
    after_earnings=True,
    sell_price_change_pct=5.0,
)
POLICY = RenotificationPolicy(
    periodic=PeriodicPolicy.KEEP,
    decision_change_scope=DecisionChangeScope.ANY_CHANGE,
    earnings_mode=EarningsMode.FIRST_EVALUATION_AFTER_EARNINGS,
    score_basis=ScoreBasis.BASE_SCORE,
    keyword_only=KeywordOnlyHandling.NOT_COUNTED,
    sell_price_reference=SellPriceReference.TARGET_PRICE_ONLY,
)

_Q1 = date(2026, 3, 31)
_Q2 = date(2026, 6, 30)
_Q0 = date(2025, 12, 31)


def state(**overrides: object) -> HdNotifyState:
    base: dict[str, object] = {
        "scoring_model_version": "m1",
        "base_score": -20.0,
        "final_score": -20.0,
        "recommendation_type": "SELL_CONSIDERATION",
        "category": "WATCH",
        "decision_severity": 1,
        "gate_confirmations": frozenset(),
        "earnings_key": _Q1,
        "earnings_freshness": EarningsDataFreshness.FRESH,
        "sell_reference": SellReference("stop_review", 1000.0),
        "market_price": 1200.0,
    }
    base.update(overrides)
    return HdNotifyState(**base)  # type: ignore[arg-type]


def gates(**codes: GateConfirmation) -> frozenset[tuple[str, GateConfirmation]]:
    return frozenset(codes.items())


def decide(
    current: HdNotifyState,
    previous: HdNotifyState | StateUnavailable,
    *,
    config: RenotificationConfig = CFG,
    policy: RenotificationPolicy = POLICY,
    periodic_due: bool = False,
) -> mod.HdRenotifyDecision:
    return decide_hd_renotification(current, previous, config, policy, periodic_due=periodic_due)


def status_of(decision: mod.HdRenotifyDecision, condition: Condition) -> ConditionStatus:
    return decision.result_of(condition).status


def with_policy(**overrides: object) -> RenotificationPolicy:
    fields = {
        "periodic": POLICY.periodic,
        "decision_change_scope": POLICY.decision_change_scope,
        "earnings_mode": POLICY.earnings_mode,
        "score_basis": POLICY.score_basis,
        "keyword_only": POLICY.keyword_only,
        "sell_price_reference": POLICY.sell_price_reference,
    }
    fields.update(overrides)
    return RenotificationPolicy(**fields)  # type: ignore[arg-type]


def with_config(**overrides: object) -> RenotificationConfig:
    fields = {
        "score_deterioration": CFG.score_deterioration,
        "on_decision_change": CFG.on_decision_change,
        "on_new_hard_gate": CFG.on_new_hard_gate,
        "after_earnings": CFG.after_earnings,
        "sell_price_change_pct": CFG.sell_price_change_pct,
    }
    fields.update(overrides)
    return RenotificationConfig(**fields)  # type: ignore[arg-type]


# ===========================================================================
# (1) R1 スコアの悪化
# ===========================================================================


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (-20.0, -29.99, ConditionStatus.NOT_MET),  # AC-R1-1 境界の直前(差 9.99)
        (-20.0, -30.0, ConditionStatus.MET),  # 境界ちょうど(差 10.0)は成立(≧)
        (-20.0, -30.01, ConditionStatus.MET),
        (-20.1, -30.1, ConditionStatus.MET),  # 浮動小数点で 9.999… になりうる組み合わせ
        (-31.8, -41.8, ConditionStatus.MET),  # 2 進の厳密値では 9.999999999999996 になる組み合わせ
        (-20.0, -10.0, ConditionStatus.NOT_MET),  # AC-R1-3 改善では成立しない
        (-20.0, -20.0, ConditionStatus.NOT_MET),
    ],
)
def test_r1_boundary(before: float, after: float, expected: ConditionStatus) -> None:
    result = decide(state(base_score=after), state(base_score=before))
    assert status_of(result, R1) is expected


def test_r1_is_cumulative_from_the_last_delivered_state_not_day_over_day() -> None:
    """AC-R1-2: 前回 -20 → 翌日 -25 → 翌々日 -31。前日比では不成立の悪化を、累積で検出する。"""
    delivered = state(base_score=-20.0)
    day2 = decide(state(base_score=-25.0), delivered)
    day3 = decide(state(base_score=-31.0), delivered)  # 前回の配信は -20 のまま(進んでいない)
    assert status_of(day2, R1) is ConditionStatus.NOT_MET
    assert status_of(day3, R1) is ConditionStatus.MET
    # 前日比(-25 → -31)なら 6 点で不成立のはずの悪化
    assert status_of(decide(state(base_score=-31.0), state(base_score=-25.0)), R1) is (
        ConditionStatus.NOT_MET
    )


def test_r1_score_basis_is_a_policy_not_a_default() -> None:
    """D-4: hard gate で final が頭打ちのとき、base なら悪化が見え、final なら見えない。"""
    previous = state(base_score=-20.0, final_score=-30.0)
    current = state(base_score=-35.0, final_score=-30.0)
    assert status_of(
        decide(current, previous, policy=with_policy(score_basis=ScoreBasis.BASE_SCORE)), R1
    ) is (ConditionStatus.MET)
    assert status_of(
        decide(current, previous, policy=with_policy(score_basis=ScoreBasis.FINAL_SCORE)), R1
    ) is (ConditionStatus.NOT_MET)


def test_r1_model_version_mismatch_is_not_evaluable() -> None:
    """点数の尺度が違う前回とは比べない(点を捏造しない)。"""
    result = decide(state(base_score=-90.0), state(base_score=-20.0, scoring_model_version="m0"))
    assert result.result_of(R1) == mod.ConditionResult(
        ConditionStatus.NOT_EVALUABLE, Reason.MODEL_VERSION_MISMATCH
    )
    assert R1 not in result.conditions_met


def test_r1_disabled_when_threshold_is_none() -> None:
    result = decide(
        state(base_score=-90.0),
        state(base_score=-20.0),
        config=with_config(score_deterioration=None),
    )
    assert result.result_of(R1) == mod.ConditionResult(ConditionStatus.NOT_MET, Reason.DISABLED)


# ===========================================================================
# (2) R2 判定の変化
# ===========================================================================


def test_r2_any_change() -> None:
    previous = state()
    assert status_of(
        decide(state(recommendation_type="STRONG_SELL_CONSIDERATION"), previous), R2
    ) is (ConditionStatus.MET)  # AC-R2-1 種別が違う
    assert (
        status_of(decide(state(category="AVOID"), previous), R2) is ConditionStatus.MET
    )  # AC-R2-2
    assert status_of(decide(state(), previous), R2) is ConditionStatus.NOT_MET  # AC-R2-3


def test_r2_disabled_by_config() -> None:
    """AC-R2-4: config false なら、種別が違っても成立にならない。"""
    result = decide(
        state(recommendation_type="STRONG_SELL_CONSIDERATION"),
        state(),
        config=with_config(on_decision_change=False),
    )
    assert result.result_of(R2) == mod.ConditionResult(ConditionStatus.NOT_MET, Reason.DISABLED)


@pytest.mark.parametrize(
    ("previous_severity", "current_severity", "expected"),
    [
        (1, 2, ConditionStatus.MET),  # 悪化
        (2, 1, ConditionStatus.NOT_MET),  # 改善
        (1, 1, ConditionStatus.NOT_MET),
    ],
)
def test_r2_worsening_only(
    previous_severity: int, current_severity: int, expected: ConditionStatus
) -> None:
    policy = with_policy(decision_change_scope=DecisionChangeScope.WORSENING_ONLY)
    result = decide(
        state(recommendation_type="X", decision_severity=current_severity),
        state(decision_severity=previous_severity),
        policy=policy,
    )
    assert status_of(result, R2) is expected


@pytest.mark.parametrize(
    ("previous_severity", "current_severity"), [(None, 2), (1, None), (None, None)]
)
def test_r2_worsening_only_without_severity_is_not_evaluable(
    previous_severity: int | None, current_severity: int | None
) -> None:
    policy = with_policy(decision_change_scope=DecisionChangeScope.WORSENING_ONLY)
    result = decide(
        state(recommendation_type="X", decision_severity=current_severity),
        state(decision_severity=previous_severity),
        policy=policy,
    )
    assert result.result_of(R2) == mod.ConditionResult(
        ConditionStatus.NOT_EVALUABLE, Reason.DECISION_SEVERITY_UNAVAILABLE
    )


# ===========================================================================
# (3) R3 新たに確認済みの hard gate
# ===========================================================================

_C = GateConfirmation.CONFIRMED
_B = GateConfirmation.BASELINE_CONFIRMED
_K = GateConfirmation.KEYWORD_ONLY
_U = GateConfirmation.UNVERIFIED


def test_r3_first_confirmed_gate_is_met() -> None:
    """AC-R3-1: 前回 hard gate 無し → 今回 CONFIRMED の理由コードあり。"""
    assert status_of(decide(state(gate_confirmations=gates(A=_C)), state()), R3) is (
        ConditionStatus.MET
    )


def test_r3_set_difference_against_the_last_delivered_state() -> None:
    previous = state(gate_confirmations=gates(A=_C))
    assert status_of(decide(state(gate_confirmations=gates(A=_C, B=_C)), previous), R3) is (
        ConditionStatus.MET  # AC-R3-2 前回 {A} → 今回 {A, B}
    )
    assert status_of(decide(state(gate_confirmations=gates(A=_C)), previous), R3) is (
        ConditionStatus.NOT_MET  # AC-R3-3 同じ集合
    )
    assert status_of(decide(state(), previous), R3) is ConditionStatus.NOT_MET  # 消えた


def test_r3_a_code_that_disappeared_and_returned_is_new_if_never_delivered_in_between() -> None:
    """前回の配信時の集合に無ければ新規(間に配信が無いなら、消えて戻っても通知が消えない)。"""
    assert status_of(decide(state(gate_confirmations=gates(A=_C)), state()), R3) is (
        ConditionStatus.MET
    )


@pytest.mark.parametrize("kind", [_K, _U])
def test_r3_unconfirmed_only_is_not_evaluable_and_not_met(kind: GateConfirmation) -> None:
    """AC-R3-4: キーワード一致のみ(D-5 の USER 指定値)・出所不明のみは数えない。"""
    result = decide(state(gate_confirmations=gates(A=kind)), state())
    assert result.result_of(R3) == mod.ConditionResult(
        ConditionStatus.NOT_EVALUABLE, Reason.HARD_GATE_UNCONFIRMED_ONLY
    )
    assert R3 not in result.conditions_met


def test_r3_baseline_confirmed_counts() -> None:
    assert status_of(decide(state(gate_confirmations=gates(THESIS=_B)), state()), R3) is (
        ConditionStatus.MET
    )


def test_r3_keyword_only_alongside_an_already_delivered_confirmed_gate_is_not_met() -> None:
    previous = state(gate_confirmations=gates(A=_C))
    current = state(gate_confirmations=gates(A=_C, K=_K))
    assert status_of(decide(current, previous), R3) is ConditionStatus.NOT_MET


def test_r3_keyword_only_counted_by_policy() -> None:
    """D-5 を『数える』にした場合: キーワード一致のみでも新規なら成立する。"""
    policy = with_policy(keyword_only=KeywordOnlyHandling.COUNTED)
    assert status_of(decide(state(gate_confirmations=gates(A=_K)), state(), policy=policy), R3) is (
        ConditionStatus.MET
    )
    # 前回に同じコードが(キーワード一致のみで)配信済みなら、数える方針では新規ではない
    assert status_of(
        decide(
            state(gate_confirmations=gates(A=_K)),
            state(gate_confirmations=gates(A=_K)),
            policy=policy,
        ),
        R3,
    ) is (ConditionStatus.NOT_MET)
    # 出所不明は方針に関わらず数えない
    assert status_of(decide(state(gate_confirmations=gates(A=_U)), state(), policy=policy), R3) is (
        ConditionStatus.NOT_EVALUABLE
    )


@pytest.mark.parametrize(
    ("current_kind", "previous_kind", "counted_met", "keyword_met"),
    [
        (_C, None, True, True),
        (_B, None, True, True),
        (_K, None, False, True),
        (_U, None, False, False),
        (_C, _C, False, False),
        (_C, _K, True, False),  # 前回キーワード一致のみ → 今回確認済み: 数えない方針では新規
        (_K, _C, False, False),
    ],
)
def test_r3_confirmation_by_policy_grid(
    current_kind: GateConfirmation,
    previous_kind: GateConfirmation | None,
    counted_met: bool,
    keyword_met: bool,
) -> None:
    previous = state(gate_confirmations=gates(A=previous_kind) if previous_kind else frozenset())
    current = state(gate_confirmations=gates(A=current_kind))
    for handling, expected in (
        (KeywordOnlyHandling.NOT_COUNTED, counted_met),
        (KeywordOnlyHandling.COUNTED, keyword_met),
    ):
        result = decide(current, previous, policy=with_policy(keyword_only=handling))
        assert (R3 in result.conditions_met) is expected, (handling, current_kind, previous_kind)


def test_r3_disabled_by_config() -> None:
    result = decide(
        state(gate_confirmations=gates(A=_C)), state(), config=with_config(on_new_hard_gate=False)
    )
    assert result.result_of(R3) == mod.ConditionResult(ConditionStatus.NOT_MET, Reason.DISABLED)


def test_r3_same_gate_set_in_any_input_order_gives_the_same_result() -> None:
    items = [("A", _C), ("B", _C), ("C", _B)]
    results = {
        decide(
            state(gate_confirmations=frozenset(order)), state(gate_confirmations=gates(A=_C))
        ).evaluations
        for order in itertools.permutations(items)
    }
    assert len(results) == 1


# ===========================================================================
# (4) R4 決算後
# ===========================================================================


def test_r4_newer_period_end_is_met_once() -> None:
    """AC-R4-1 期末が前回より新しい(鮮度が STALE でない)→ 成立 / AC-R4-2 同じ期末 → 不成立。"""
    delivered = state(earnings_key=_Q1)
    today = state(earnings_key=_Q2)
    assert status_of(decide(today, delivered), R4) is ConditionStatus.MET
    # 配信された後は、前回の状態が今回の状態になり、同じ期末では二度成立しない
    assert status_of(decide(today, today), R4) is ConditionStatus.NOT_MET


def test_r4_blocked_delivery_keeps_the_condition_for_the_next_day() -> None:
    """遮られて配信されなかった日は前回の状態が進まず、翌日も成立する(通知が消えない)。"""
    delivered = state(earnings_key=_Q1)
    assert status_of(decide(state(earnings_key=_Q2), delivered), R4) is ConditionStatus.MET
    assert status_of(decide(state(earnings_key=_Q2), delivered), R4) is ConditionStatus.MET


def test_r4_older_period_end_is_not_met() -> None:
    """AC-R4-3 提供元の都合で期末が戻っても成立しない。"""
    assert status_of(decide(state(earnings_key=_Q0), state(earnings_key=_Q1)), R4) is (
        ConditionStatus.NOT_MET
    )


def test_r4_stale_data_is_not_met_with_a_reason() -> None:
    """AC-R4-4 鮮度が STALE(期限を過ぎても旧期のまま等)なら成立にしない。"""
    result = decide(
        state(earnings_key=_Q2, earnings_freshness=EarningsDataFreshness.STALE),
        state(earnings_key=_Q1),
    )
    assert result.result_of(R4) == mod.ConditionResult(
        ConditionStatus.NOT_MET, Reason.EARNINGS_DATA_STALE
    )


@pytest.mark.parametrize("freshness", [EarningsDataFreshness.FRESH, EarningsDataFreshness.UNKNOWN])
def test_r4_fresh_or_unknown_freshness_is_met(freshness: EarningsDataFreshness) -> None:
    result = decide(state(earnings_key=_Q2, earnings_freshness=freshness), state(earnings_key=_Q1))
    assert status_of(result, R4) is ConditionStatus.MET


@pytest.mark.parametrize(("current_key", "previous_key"), [(None, _Q1), (_Q2, None), (None, None)])
def test_r4_missing_period_end_is_not_evaluable(
    current_key: date | None, previous_key: date | None
) -> None:
    result = decide(state(earnings_key=current_key), state(earnings_key=previous_key))
    assert result.result_of(R4) == mod.ConditionResult(
        ConditionStatus.NOT_EVALUABLE, Reason.EARNINGS_KEY_UNAVAILABLE
    )


def test_r4_not_used_when_policy_or_config_says_so() -> None:
    newer, older = state(earnings_key=_Q2), state(earnings_key=_Q1)
    off_by_policy = decide(newer, older, policy=with_policy(earnings_mode=EarningsMode.DISABLED))
    off_by_config = decide(newer, older, config=with_config(after_earnings=False))
    for result in (off_by_policy, off_by_config):
        assert result.result_of(R4) == mod.ConditionResult(ConditionStatus.NOT_MET, Reason.DISABLED)


# ===========================================================================
# (5) R5 売却目安価格の変化
# ===========================================================================


def _ref(price: float, kind: str = "stop_review") -> SellReference:
    return SellReference(kind, price)


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (1000.0, 1049.9, ConditionStatus.NOT_MET),  # AC-R5-1 4.99% は不成立
        (1000.0, 1050.0, ConditionStatus.MET),  # ちょうど 5.0% は成立(上)
        (1000.0, 950.0, ConditionStatus.MET),  # ちょうど 5.0%(下)
        (1000.0, 950.1, ConditionStatus.NOT_MET),  # 4.99%(下)
        (1000.0, 1000.0, ConditionStatus.NOT_MET),
        (1000.0, 1200.0, ConditionStatus.MET),
        (0.3, 0.315, ConditionStatus.MET),  # 浮動小数点で 4.999… になりうる組み合わせ
    ],
)
def test_r5_boundary(before: float, after: float, expected: ConditionStatus) -> None:
    result = decide(state(sell_reference=_ref(after)), state(sell_reference=_ref(before)))
    assert status_of(result, R5) is expected


def test_r5_current_price_move_alone_does_not_fire_with_target_price_only() -> None:
    """AC-R5-2: 目安価格が不変で現在値だけが動いても成立しない(現在値へのフォールバックなし)。"""
    result = decide(state(market_price=2000.0), state(market_price=1000.0))
    assert status_of(result, R5) is ConditionStatus.NOT_MET


@pytest.mark.parametrize(
    ("current_ref", "previous_ref"),
    [
        (None, _ref(1000.0)),  # AC-R5-4 価格が無い
        (_ref(1000.0), None),
        (None, None),  # URGENT 等の参照価格なし
        (_ref(1000.0, "full_profit"), _ref(1000.0, "stop_review")),  # 種類が違う
    ],
)
def test_r5_not_comparable_is_not_evaluable(
    current_ref: SellReference | None, previous_ref: SellReference | None
) -> None:
    result = decide(state(sell_reference=current_ref), state(sell_reference=previous_ref))
    assert result.result_of(R5) == mod.ConditionResult(
        ConditionStatus.NOT_EVALUABLE, Reason.PRICE_NOT_COMPARABLE
    )


def test_r5_include_current_price_falls_back_only_when_no_comparable_target_price() -> None:
    """D-6: 現在値も含める方針。目安価格が比べられるときは目安価格を優先する。"""
    policy = with_policy(sell_price_reference=SellPriceReference.INCLUDE_CURRENT_PRICE)
    # 目安価格が比べられない → 現在値で比べる
    fallback = decide(
        state(sell_reference=None, market_price=1100.0),
        state(sell_reference=None, market_price=1000.0),
        policy=policy,
    )
    assert status_of(fallback, R5) is ConditionStatus.MET
    # 目安価格が比べられる → 現在値の大きな動きは見ない
    preferred = decide(
        state(sell_reference=_ref(1000.0), market_price=2000.0),
        state(sell_reference=_ref(1000.0), market_price=1000.0),
        policy=policy,
    )
    assert status_of(preferred, R5) is ConditionStatus.NOT_MET
    # 現在値も無い → 比べられない
    nothing = decide(
        state(sell_reference=None, market_price=None),
        state(sell_reference=None, market_price=1000.0),
        policy=policy,
    )
    assert status_of(nothing, R5) is ConditionStatus.NOT_EVALUABLE


def test_r5_disabled_when_threshold_is_none() -> None:
    result = decide(
        state(sell_reference=_ref(2000.0)),
        state(sell_reference=_ref(1000.0)),
        config=with_config(sell_price_change_pct=None),
    )
    assert result.result_of(R5) == mod.ConditionResult(ConditionStatus.NOT_MET, Reason.DISABLED)


# ===========================================================================
# (6) 共通の性質
# ===========================================================================

_UNAVAILABLE = [
    StateUnavailable(Reason.NO_PREVIOUS_HD_STATE),
    StateUnavailable(Reason.PREVIOUS_IS_LEGACY),
    StateUnavailable(Reason.STATE_VERSION_UNKNOWN),
    StateUnavailable(Reason.STATE_MALFORMED),
]


@pytest.mark.parametrize("previous", _UNAVAILABLE)
def test_unavailable_previous_makes_every_enabled_condition_not_evaluable(
    previous: StateUnavailable,
) -> None:
    result = decide(state(base_score=-99.0, gate_confirmations=gates(A=_C)), previous)
    assert result.conditions_met == frozenset()
    assert not result.send_by_policy  # 既存の判定(種別の変化・日数など)に任せる
    for condition in Condition:
        assert result.result_of(condition) == mod.ConditionResult(
            ConditionStatus.NOT_EVALUABLE, previous.reason
        )


def test_all_five_conditions_can_be_met_at_once_and_are_reported_in_fixed_order() -> None:
    previous = state()
    current = state(
        base_score=-40.0,
        recommendation_type="STRONG_SELL_CONSIDERATION",
        gate_confirmations=gates(A=_C),
        earnings_key=_Q2,
        sell_reference=_ref(1200.0),
    )
    result = decide(current, previous)
    assert result.conditions_met == frozenset(Condition)
    assert [name for name, _ in result.evaluations] == list(mod.CONDITION_ORDER)
    assert result.send_by_policy


def test_each_condition_is_independent_of_the_others_config_switches() -> None:
    previous = state()
    current = state(
        base_score=-40.0,
        recommendation_type="STRONG_SELL_CONSIDERATION",
        gate_confirmations=gates(A=_C),
        earnings_key=_Q2,
        sell_reference=_ref(1200.0),
    )
    full = decide(current, previous)
    switches = {
        R1: {"score_deterioration": None},
        R2: {"on_decision_change": False},
        R3: {"on_new_hard_gate": False},
        R4: {"after_earnings": False},
        R5: {"sell_price_change_pct": None},
    }
    for off_condition, override in switches.items():
        partial = decide(current, previous, config=with_config(**override))
        assert partial.result_of(off_condition).reason is Reason.DISABLED
        for other in Condition:
            if other is not off_condition:
                assert partial.result_of(other) == full.result_of(other)


def test_delivered_state_never_fires_again_on_the_same_state_idempotent() -> None:
    """配信後は前回 = 今回になる。すべての条件が同じ状態では成立しない。"""
    today = state(
        base_score=-40.0,
        gate_confirmations=gates(A=_C),
        earnings_key=_Q2,
        sell_reference=_ref(1200.0),
    )
    result = decide(today, today)
    assert result.conditions_met == frozenset()
    assert not result.send_by_policy


def test_blocked_delivery_keeps_every_condition_for_the_next_day() -> None:
    delivered = state()
    today = state(
        base_score=-40.0,
        recommendation_type="STRONG_SELL_CONSIDERATION",
        gate_confirmations=gates(A=_C),
        earnings_key=_Q2,
        sell_reference=_ref(1200.0),
    )
    assert decide(today, delivered) == decide(today, delivered)
    assert decide(today, delivered).conditions_met == frozenset(Condition)


@pytest.mark.parametrize(
    ("periodic", "periodic_due", "expected_due", "expected_send"),
    [
        (PeriodicPolicy.KEEP, True, True, True),
        (PeriodicPolicy.KEEP, False, False, False),
        (PeriodicPolicy.STOP, True, False, False),
        (PeriodicPolicy.STOP, False, False, False),
    ],
)
def test_periodic_policy_only_matters_when_no_condition_is_met(
    periodic: PeriodicPolicy, periodic_due: bool, expected_due: bool, expected_send: bool
) -> None:
    """D-1: 条件が何も成立していないとき、周期の再送を保有判断に残すか止めるかで send が変わる。"""
    result = decide(
        state(), state(), policy=with_policy(periodic=periodic), periodic_due=periodic_due
    )
    assert result.periodic_due is expected_due
    assert result.send_by_policy is expected_send


def test_periodic_can_request_a_send_even_when_no_condition_is_evaluable() -> None:
    """send_by_policy = (成立が空でない) または (periodic_due)。前回が使えなくても効く。"""
    previous = StateUnavailable(Reason.NO_PREVIOUS_HD_STATE)
    kept = decide(
        state(), previous, policy=with_policy(periodic=PeriodicPolicy.KEEP), periodic_due=True
    )
    assert kept.conditions_met == frozenset()
    assert kept.send_by_policy
    stopped = decide(
        state(), previous, policy=with_policy(periodic=PeriodicPolicy.STOP), periodic_due=True
    )
    assert not stopped.send_by_policy


def test_zero_threshold_is_rejected_and_none_is_the_way_to_disable() -> None:
    with pytest.raises(ValueError):
        with_config(score_deterioration=0.0)
    disabled = with_config(score_deterioration=None, sell_price_change_pct=None)
    result = decide(state(base_score=-99.0), state(base_score=-20.0), config=disabled)
    assert result.result_of(R1).reason is Reason.DISABLED
    assert result.result_of(R5).reason is Reason.DISABLED


def test_a_met_condition_sends_regardless_of_the_periodic_policy() -> None:
    result = decide(
        state(base_score=-40.0), state(), policy=with_policy(periodic=PeriodicPolicy.STOP)
    )
    assert result.send_by_policy


def test_decision_is_deterministic_and_pure() -> None:
    current = state(base_score=-40.0, gate_confirmations=gates(A=_C, B=_B))
    previous = state()
    first = decide(current, previous)
    for _ in range(3):
        assert decide(current, previous) == first


# 単調性(メタモルフィック): 他の入力を固定して悪化を増やしたとき、成立が外れない。
def test_r1_is_monotone_in_the_deterioration() -> None:
    previous = state(base_score=-20.0)
    seen_met = False
    for after in [-20.0 - step * 0.5 for step in range(0, 80)]:
        met = R1 in decide(state(base_score=after), previous).conditions_met
        if seen_met:
            assert met, after
        seen_met = seen_met or met
    assert seen_met


def test_r5_is_monotone_in_the_absolute_price_change_for_both_directions() -> None:
    previous = state(sell_reference=_ref(1000.0))
    for sign in (1, -1):
        seen_met = False
        for pct_tenths in range(0, 200):
            after = 1000.0 * (1 + sign * pct_tenths / 1000)
            met = R5 in decide(state(sell_reference=_ref(after)), previous).conditions_met
            if seen_met:
                assert met, (sign, pct_tenths)
            seen_met = seen_met or met
        assert seen_met


# ===========================================================================
# (7) 入力の検証
# ===========================================================================


@pytest.mark.parametrize(
    "factory",
    [
        lambda: state(base_score=math.nan),
        lambda: state(final_score=math.inf),
        lambda: state(scoring_model_version=""),
        lambda: state(recommendation_type=""),
        lambda: state(category=""),
        lambda: state(market_price=0.0),
        lambda: state(market_price=-1.0),
        lambda: state(gate_confirmations=frozenset({("A", _C), ("A", _K)})),
        lambda: SellReference("", 1.0),
        lambda: SellReference("k", 0.0),
        lambda: SellReference("k", math.nan),
        lambda: with_config(score_deterioration=-1.0),
        lambda: with_config(score_deterioration=0.0),  # 0 は常時成立になるため受け付けない
        lambda: with_config(sell_price_change_pct=0.0),
        lambda: with_config(sell_price_change_pct=math.inf),
    ],
)
def test_invalid_inputs_are_rejected(factory: object) -> None:
    with pytest.raises(ValueError):
        factory()  # type: ignore[operator]


# ===========================================================================
# (8) 保存形式の読み書き(書く側は PR-2。本 PR は形式の定義と読む側)
# ===========================================================================


def test_serialize_and_extract_round_trip() -> None:
    original = state(
        base_score=-25.5,
        gate_confirmations=gates(A=_C, B=_K),
        sell_reference=_ref(1234.5),
        decision_severity=None,
    )
    stored = {HD_RENOTIFY_STATE_KEY: serialize_hd_state(original)}
    assert extract_hd_state(stored) == original
    assert serialize_hd_state(original)["state_version"] == STATE_VERSION


def test_round_trip_without_optional_parts() -> None:
    original = state(earnings_key=None, sell_reference=None, market_price=None)
    assert extract_hd_state({HD_RENOTIFY_STATE_KEY: serialize_hd_state(original)}) == original


def test_serialization_is_deterministic_regardless_of_gate_order() -> None:
    a = serialize_hd_state(state(gate_confirmations=gates(B=_C, A=_C)))
    b = serialize_hd_state(state(gate_confirmations=gates(A=_C, B=_C)))
    assert list(a["gate_confirmations"]) == list(b["gate_confirmations"])  # type: ignore[call-overload]


def test_extract_none_means_no_previous_delivery() -> None:
    assert extract_hd_state(None) == StateUnavailable(Reason.NO_PREVIOUS_HD_STATE)


def test_extract_without_the_key_means_legacy() -> None:
    """AC(層 4): 旧形式の Recommendation(キー無し)を読んで、比べられない。"""
    assert extract_hd_state({"other": 1}) == StateUnavailable(Reason.PREVIOUS_IS_LEGACY)
    assert extract_hd_state({HD_RENOTIFY_STATE_KEY: None}) == StateUnavailable(
        Reason.PREVIOUS_IS_LEGACY
    )


def test_extract_unknown_version() -> None:
    raw = serialize_hd_state(state())
    raw["state_version"] = STATE_VERSION + 1
    assert extract_hd_state({HD_RENOTIFY_STATE_KEY: raw}) == StateUnavailable(
        Reason.STATE_VERSION_UNKNOWN
    )


def _malformed_cases() -> list[object]:
    good = serialize_hd_state(state())

    def mutate(**changes: object) -> dict[str, object]:
        raw = dict(good)
        raw.update(changes)
        return raw

    def drop(key: str) -> dict[str, object]:
        raw = dict(good)
        del raw[key]
        return raw

    return [
        "not a mapping",
        mutate(state_version=True),
        mutate(state_version="1"),
        drop("state_version"),
        drop("base_score"),
        drop("gate_confirmations"),
        mutate(base_score="x"),
        mutate(base_score=True),
        mutate(base_score=math.nan),
        mutate(final_score=math.inf),
        mutate(scoring_model_version=1),
        mutate(scoring_model_version=""),
        mutate(decision_severity="1"),
        mutate(decision_severity=True),
        mutate(gate_confirmations=["A"]),
        mutate(gate_confirmations={"A": "NOT_A_STATE"}),
        mutate(gate_confirmations={1: "CONFIRMED"}),
        mutate(earnings_key="2026/03/31"),
        mutate(earnings_key=20260331),
        mutate(earnings_freshness="SOON"),
        mutate(sell_reference="x"),
        mutate(sell_reference={"kind": "k"}),
        mutate(sell_reference={"kind": "k", "price": -1.0}),
        mutate(market_price=0),
    ]


@pytest.mark.parametrize("raw", _malformed_cases())
def test_extract_malformed_state_is_unavailable_not_an_exception(raw: object) -> None:
    result = extract_hd_state({HD_RENOTIFY_STATE_KEY: raw})
    assert result == StateUnavailable(Reason.STATE_MALFORMED)


# ===========================================================================
# (9) meta: dormant・標準ライブラリのみ・時計なし・既定値なし・数値リテラル
# ===========================================================================

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"
_MODULE_PATH = _SRC_ROOT / "domain" / "signals" / "holding_decision_renotification.py"
_MODULE_NAME = "holding_decision_renotification"


def _module_tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def test_module_is_dormant_nothing_in_src_imports_it() -> None:
    """どこからも import されない(配線は PR-3 の範囲)。"""
    offenders = [
        str(path.relative_to(_SRC_ROOT))
        for path in _SRC_ROOT.rglob("*.py")
        if path != _MODULE_PATH and _MODULE_NAME in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_module_imports_only_the_standard_library() -> None:
    imported: set[str] = set()
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= {
        "__future__",
        "math",
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
    }


def test_module_does_not_read_the_clock() -> None:
    forbidden_calls = {"now", "today", "utcnow", "time", "monotonic", "sleep"}
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_calls, node.func.attr
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            module = getattr(node, "module", None)
            assert "time" not in names and module != "time"
            assert module != "zoneinfo" and "zoneinfo" not in names


@pytest.mark.parametrize("class_name", ["RenotificationPolicy", "RenotificationConfig"])
def test_policy_and_config_have_no_default_values(class_name: str) -> None:
    """D-1〜D-6 と閾値は、呼び出す側が必ず明示する(PR-1 が暗黙に一つへ決めない)。"""
    cls = next(
        node
        for node in ast.walk(_module_tree())
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    fields = [node for node in cls.body if isinstance(node, ast.AnnAssign)]
    assert fields
    assert all(node.value is None for node in fields), class_name


def test_module_has_no_business_numeric_literals() -> None:
    """閾値・日数は引数で受け取る。module に置く数値は 0 / 1(版・比較の基準)と 100(%→比)だけ。"""
    allowed = {0, 1, 100}
    literals = [
        node.value
        for node in ast.walk(_module_tree())
        if isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ]
    assert set(literals) <= allowed, sorted(set(literals) - allowed)


def test_every_config_key_has_one_condition() -> None:
    """config の `renotification` の 5 項目と Condition の値が 1 対 1(名前の取り違えを防ぐ)。"""
    config_text = (
        Path(__file__).resolve().parents[2] / "config" / "holding_decision_rules.yaml"
    ).read_text(encoding="utf-8")
    for condition in Condition:
        assert f"  {condition.value}:" in config_text, condition.value
    assert len(list(Condition)) == 5
