"""R3 が確認の規則の版をまたいでも、版を見ずに比べることの契約テスト(Issue #890)。

固定するもの(現行の挙動の固定だけ。挙動は変えない)
  R3 は gate_confirmations(理由コード × 確認状態)だけを比べ、
  confirmation_rule_version を見ない(版を見ずに比べる〔(i)〕)。
  #889 PR-2 が CONFIRMATION_RULE_VERSION を 1 から 2 に上げる前に、
  版 1 の前回 × 版 2 の今回の場面を固定する。

場面(設計 rev1.4: https://github.com/kouitic/jstock_advisor/issues/889#issuecomment-6098535651)
  ① 同じ理由コード・同じ確認状態                -> 差なし -> NOT_MET
  ② 前回: 版 1 の弱い確認 / 今回: 是正で確認が外れた
     -> NOT_MET(今回が未確認のみなら NOT_EVALUABLE)
  ③ 会計: 前回 版 1 のキーワードのみ / 今回 版 2 の確認済み -> MET
  ④ 配信が起きない間(前回が版 1 のまま)に、本物の B が現れた日に成立する
  ⑤ 前回の版が不明(#897 の記録)でも結果は変わらない
  版の組み合わせ(前回・今回 × {不明, 1, 2})のすべてで結果が同じ(= 版を見ない)ことも固定する。

変更してはならないもの(変更したくなったときは、R3 の所有者の判断が要る)
  ・版が違えば NOT_EVALUABLE にする((ii)): 切替後に配信が起きない間は前回が
    版 1 のまま残り、その間に本物の B が現れても R3 が成立しない(見逃し側)。
  ・版が違えば確認の程度を無視して理由コードの有無だけで比べる((iii)):
    ③ を区別できず不成立になる。

時間意味論: 固定の日付リテラル(earnings_key)を入力に使うが、R4 の比較は前回・今回で
同じ値で、時計・営業日・timezone を扱わない。wall clock・freezegun は使わない
(TIME_SEMANTICS_IMPACT = NO)。
"""

from __future__ import annotations

from datetime import date

import pytest

from jstock_advisor.domain.signals.holding_decision_renotification import (
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
    decide_hd_renotification,
)

R3 = Condition.R3_NEW_HARD_GATE

_CONFIRMED = GateConfirmation.CONFIRMED
_KEYWORD = GateConfirmation.KEYWORD_ONLY

# 暫定の方針(services/hd_renotification_provisional_policy.py)と同じ値。D-5 = 数えない。
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

#: 前回・今回に取りうる版(None = 版不明。#897 で作った記録は版を持たない)。
_VERSIONS: tuple[int | None, ...] = (None, 1, 2)

Gates = frozenset[tuple[str, GateConfirmation]]


def gates(**codes: GateConfirmation) -> Gates:
    return frozenset(codes.items())


def state(gate_confirmations: Gates, version: int | None) -> HdNotifyState:
    return HdNotifyState(
        scoring_model_version="1",
        base_score=-20.0,
        final_score=-20.0,
        recommendation_type="URGENT_HOLDING_REVIEW",
        category="STRONG_SELL_CONSIDERATION",
        decision_severity=3,
        gate_confirmations=gate_confirmations,
        earnings_key=_Q1,
        earnings_freshness=EarningsDataFreshness.FRESH,
        sell_reference=None,
        market_price=1200.0,
        confirmation_rule_version=version,
    )


def r3_of(
    current: HdNotifyState, previous: HdNotifyState
) -> tuple[ConditionStatus, Reason | None, bool]:
    decision = decide_hd_renotification(current, previous, CFG, POLICY, periodic_due=False)
    result = decision.result_of(R3)
    return result.status, result.reason, R3 in decision.conditions_met


_MET = (ConditionStatus.MET, None, True)
_NOT_MET = (ConditionStatus.NOT_MET, None, False)
_UNCONFIRMED_ONLY = (
    ConditionStatus.NOT_EVALUABLE,
    Reason.HARD_GATE_UNCONFIRMED_ONLY,
    False,
)

# (名前, 前回の gate_confirmations, 今回の gate_confirmations, 期待する R3)。
# 期待値は、この PR の前の main の出力と一致する(src は変更していない = 現行の挙動の固定)。
SCENARIOS = [
    (
        "① 同じ理由コード・同じ確認状態",
        gates(BANKRUPTCY_FILING=_CONFIRMED),
        gates(BANKRUPTCY_FILING=_CONFIRMED),
        _NOT_MET,
    ),
    (
        "② 版 1 の弱い確認が、是正で hard gate ごと外れた",
        gates(BANKRUPTCY_FILING=_CONFIRMED),
        gates(),
        _NOT_MET,
    ),
    (
        "②' 同上で、今回は未確認(キーワードのみ)の gate だけが残る",
        gates(BANKRUPTCY_FILING=_CONFIRMED),
        gates(BANKRUPTCY_FILING=_KEYWORD),
        _UNCONFIRMED_ONLY,
    ),
    (
        "③ 会計: 版 1 のキーワードのみ -> 版 2 の確認済み",
        gates(ACCOUNTING_FRAUD=_KEYWORD),
        gates(ACCOUNTING_FRAUD=_CONFIRMED),
        _MET,
    ),
    (
        "③' 会計: 版 1 のキーワードのみ -> 版 2 でも未確認のまま",
        gates(ACCOUNTING_FRAUD=_KEYWORD),
        gates(ACCOUNTING_FRAUD=_KEYWORD),
        _UNCONFIRMED_ONLY,
    ),
    (
        "③'' 別の理由コードが新たに確認済みになる(確認済みの既存は変わらない)",
        gates(BANKRUPTCY_FILING=_CONFIRMED),
        gates(BANKRUPTCY_FILING=_CONFIRMED, ACCOUNTING_FRAUD=_CONFIRMED),
        _MET,
    ),
]


@pytest.mark.parametrize("previous_version", _VERSIONS)
@pytest.mark.parametrize("current_version", _VERSIONS)
@pytest.mark.parametrize(
    ("name", "previous_gates", "current_gates", "expected"),
    SCENARIOS,
    ids=[scenario[0] for scenario in SCENARIOS],
)
def test_r3_does_not_look_at_the_confirmation_rule_version(
    name: str,
    previous_gates: Gates,
    current_gates: Gates,
    expected: tuple[ConditionStatus, Reason | None, bool],
    previous_version: int | None,
    current_version: int | None,
) -> None:
    """前回・今回の版のどの組み合わせでも、R3 の結果は同じ(版を見ない)。"""
    actual = r3_of(state(current_gates, current_version), state(previous_gates, previous_version))
    assert actual == expected, (name, previous_version, current_version)


def test_the_crossing_scenarios_are_exactly_version_1_to_version_2() -> None:
    """設計 rev1.4 の ①〜③ は『前回 = 版 1 / 今回 = 版 2』の組を名指しで固定する。"""
    for name, previous_gates, current_gates, expected in SCENARIOS:
        assert r3_of(state(current_gates, 2), state(previous_gates, 1)) == expected, name


def test_a_real_b_confirmation_is_met_while_the_previous_stays_at_version_1() -> None:
    """④ 切替後に配信が起きない間(前回が版 1 のまま)に、本物の B が現れた日に成立する。

    『版が違えば NOT_EVALUABLE』にすると、前回が版 1 のまま残る間は 3 日目も成立しない(見逃し)。
    """
    previous = state(gates(), 1)  # 版 1・hard gate なしの配信済みの状態。以降の日は配信されない
    day1 = r3_of(state(gates(), 2), previous)
    day2 = r3_of(state(gates(ACCOUNTING_FRAUD=_KEYWORD), 2), previous)
    day3 = r3_of(state(gates(ACCOUNTING_FRAUD=_CONFIRMED), 2), previous)
    assert (day1, day2, day3) == (_NOT_MET, _UNCONFIRMED_ONLY, _MET)


def test_the_previous_state_with_an_unknown_version_gives_the_same_result() -> None:
    """⑤ 前回の版が不明(#897 の記録)でも、版 2 の今回との比較は ③ と同じ。"""
    assert (
        r3_of(
            state(gates(ACCOUNTING_FRAUD=_CONFIRMED), 2),
            state(gates(ACCOUNTING_FRAUD=_KEYWORD), None),
        )
        == _MET
    )
