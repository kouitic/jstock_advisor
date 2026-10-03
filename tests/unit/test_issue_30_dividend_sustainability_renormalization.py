"""Issue #30 案C: 配当持続性(S-09 `score_dividend_sustainability`)の再正規化。

USER決定(2026-10-03、#122 issuecomment-5962362666): 累進配当/DOE方針が
registry未登録のためNone(不明)の銘柄は、方針項(0.4)を0点へ落とさず、残る2要素
(連続増配年数・配当性向の余力)を2/3:1/3へ再正規化する。registry既知(True/False)は
従来の3項式(0.4/0.4/0.2)から変えない。閾値の再調整は#28の範囲で本Issueに含めない。

期待値はすべて固定値(ハードコード)で持つ。BEFORE(従来の3項式)の値は、本変更前の
`score_dividend_sustainability()`を実際に呼んで得た値であり、実装の式を再現して
期待値を作ることはしない(式を取り違えても同じ取り違えで通ってしまうため)。
"""

from __future__ import annotations

import dataclasses

import pytest

from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.scoring.score import (
    SUSTAINABILITY_METHOD_RENORMALIZED_TWO_FACTOR,
    SUSTAINABILITY_METHOD_THREE_FACTOR,
    score_dividend_sustainability,
)
from jstock_advisor.services import buy_signal_service as service_module
from jstock_advisor.services.buy_signal_service import BuySignalService
from tests.unit.test_buy_signal_service import (
    _CALENDAR,
    _CONFIG,
    _NIHON_SHINYAKU,
    _NOW,
    _build_snapshot,
    _providers,
)
from tests.unit.test_shareholder_return_policy import (
    _compute,
    _dividend,
    _financial,
)

_MAX_PAYOUT = 70.0
_WEIGHT = 20.0


def _score(
    policy: bool | None, years: int | None, payout: float | None, *, max_payout: float = _MAX_PAYOUT
) -> tuple[float, str]:
    return score_dividend_sustainability(
        _dividend(is_progressive_or_doe_policy=policy, consecutive_dividend_increase_years=years),
        _financial(payout_ratio_pct=payout),
        max_payout_ratio_pct=max_payout,
        weight=_WEIGHT,
    )


# 連続増配年数 × 配当性向(None / 35% = 余力0.5 / 0% = 余力満点)ごとの値。
# BEFORE = 変更前の関数の実行結果(weight 20、payout上限70%)。
# None側のAFTER = BEFORE ÷ 0.6(式の係数比 0.4:0.2 -> 2/3:1/3)。
_COMBOS = [
    (0, None),
    (0, 35.0),
    (0, 0.0),
    (2, None),
    (2, 35.0),
    (2, 0.0),
    (5, None),
    (5, 35.0),
    (5, 0.0),
]
_BEFORE_FALSE = [0.0, 2.0, 4.0, 3.2, 5.2, 7.2, 8.0, 10.0, 12.0]
_BEFORE_TRUE = [8.0, 10.0, 12.0, 11.2, 13.2, 15.2, 16.0, 18.0, 20.0]
_AFTER_NONE = [
    0.0,
    3.3333333333,
    6.6666666667,
    5.3333333333,
    8.6666666667,
    12.0,
    13.3333333333,
    16.6666666667,
    20.0,
]


# --- T1/T2/T5: registry不明(None)は2要素を2/3:1/3へ再正規化する ------------------


@pytest.mark.parametrize(("combo", "expected"), list(zip(_COMBOS, _AFTER_NONE, strict=True)))
def test_unknown_policy_renormalizes_the_two_remaining_factors(
    combo: tuple[int, float | None], expected: float
) -> None:
    years, payout = combo
    score, _ = _score(None, years, payout)
    assert score == pytest.approx(expected, abs=1e-6)


def test_unknown_policy_can_reach_the_full_weight() -> None:
    """連続増配5年・配当性向の余力満点なら満点(従来のNoneは12点止まりだった)。"""
    score, _ = _score(None, 5, 0.0)
    assert score == pytest.approx(20.0)


def test_unknown_policy_with_only_the_payout_headroom_is_one_third_of_it() -> None:
    """連続増配0年・余力0.5 -> (1/3)*0.5*20 = 3.33(従来は(0.2)*0.5*20 = 2.0)。"""
    score, _ = _score(None, 0, 35.0)
    assert score == pytest.approx(20.0 * 0.5 / 3)
    assert score != pytest.approx(2.0)


def test_missing_data_conventions_are_the_same_as_before() -> None:
    """欠測規約は両式で同じ: 年数None=0年扱い、配当性向None・上限<=0なら余力項は0。"""
    assert _score(None, None, 35.0)[0] == pytest.approx(_score(None, 0, 35.0)[0])
    assert _score(None, 5, None)[0] == pytest.approx(20.0 * 2 / 3)
    assert _score(None, 5, 35.0, max_payout=0.0)[0] == pytest.approx(20.0 * 2 / 3)
    assert _score(None, 5, 35.0, max_payout=-1.0)[0] == pytest.approx(20.0 * 2 / 3)
    # 配当性向が上限を超えても余力は0(負にならない)
    assert _score(None, 5, 90.0)[0] == pytest.approx(20.0 * 2 / 3)
    # 年数は5年で頭打ち
    assert _score(None, 9, 0.0)[0] == pytest.approx(20.0)


# --- T3: registry既知(True/False)は従来の3項式のまま -----------------------------


@pytest.mark.parametrize(("combo", "expected"), list(zip(_COMBOS, _BEFORE_FALSE, strict=True)))
def test_confirmed_no_policy_is_unchanged_from_the_three_factor_formula(
    combo: tuple[int, float | None], expected: float
) -> None:
    years, payout = combo
    assert _score(False, years, payout)[0] == pytest.approx(expected)


@pytest.mark.parametrize(("combo", "expected"), list(zip(_COMBOS, _BEFORE_TRUE, strict=True)))
def test_confirmed_policy_is_unchanged_from_the_three_factor_formula(
    combo: tuple[int, float | None], expected: float
) -> None:
    years, payout = combo
    assert _score(True, years, payout)[0] == pytest.approx(expected)


def test_known_policy_formula_text_is_byte_identical_to_before() -> None:
    _, formula = _score(True, 5, 35.0)
    assert formula == (
        "配当持続性係数0.90(累進配当/DOE方針(+0.4), 連続増配5年評価(+0.40), "
        "配当性向の余力評価(+0.10)) × 配点20.0点"
    )
    _, formula = _score(False, 5, 35.0)
    assert formula == (
        "配当持続性係数0.50(連続増配5年評価(+0.40), 配当性向の余力評価(+0.10)) × 配点20.0点"
    )


def test_unknown_policy_is_now_higher_than_confirmed_no_policy_for_the_same_inputs() -> None:
    """事実の固定(決定の変更ではない): 案Cの帰結として、同じ年数・配当性向なら
    registry不明(None)が確認済み・方針なし(False)より高得点になる。現在のregistryは
    空のためFalseは発生しないが、将来Falseの登録が入ると「不明の方が高い」逆転が生じる
    (#30 PR本文のIMPACTに記載。USERへの共有要否はMANAGER判断)。"""
    none_score, _ = _score(None, 5, 35.0)
    false_score, _ = _score(False, 5, 35.0)
    assert none_score == pytest.approx(16.6666666667, abs=1e-6)
    assert false_score == pytest.approx(10.0)
    assert none_score > false_score


# --- T4: formula文字列(監査記録 calculation_formulas) -----------------------------


def test_unknown_policy_formula_states_that_it_was_renormalized() -> None:
    _, formula = _score(None, 5, 35.0)
    assert formula == (
        "配当持続性係数0.83(registry未登録のため累進/DOE方針を除く2要素で再正規化: "
        "連続増配5年評価(+0.67), 配当性向の余力評価(+0.17)) × 配点20.0点"
    )
    assert "累進配当/DOE方針(+0.4)" not in formula


# --- T6/T7: 再正規化を使ったことの観測記録 -----------------------------------------


def _reason_codes(policy: bool | None) -> list[str]:
    state = _compute(_dividend(is_progressive_or_doe_policy=policy)).component_states[
        "dividend_sustainability"
    ]
    assert isinstance(state, dict)
    codes = state["reason_codes"]
    assert isinstance(codes, list)
    return codes


def test_reason_code_marks_renormalization_only_for_unknown_policy() -> None:
    unknown = _reason_codes(None)
    # 既存のPOLICY_STATUS_UNKNOWNは残し(加法的)、再正規化を使ったことを別codeで追加する
    assert "POLICY_STATUS_UNKNOWN" in unknown
    assert "POLICY_UNKNOWN_RENORMALIZED" in unknown
    confirmed_none = _reason_codes(False)
    assert "POLICY_NONE_CONFIRMED" in confirmed_none
    assert "POLICY_UNKNOWN_RENORMALIZED" not in confirmed_none
    assert "POLICY_UNKNOWN_RENORMALIZED" not in _reason_codes(True)


def test_component_is_still_in_the_denominator_when_renormalized() -> None:
    """再正規化はcomponent内のsub-factorの話で、componentを総点の分母から外す意味ではない
    (excluded_from_denominatorは従来どおりFalse)。"""
    state = _compute(_dividend()).component_states["dividend_sustainability"]
    assert isinstance(state, dict)
    assert state["excluded_from_denominator"] is False
    assert str(state["state"]) == "EVALUATED"


def test_input_facts_record_the_scoring_method_by_value() -> None:
    assert (
        _compute(_dividend(is_progressive_or_doe_policy=None)).input_facts[
            "dividend_sustainability_method"
        ]
        == SUSTAINABILITY_METHOD_RENORMALIZED_TWO_FACTOR
        == "RENORMALIZED_TWO_FACTOR"
    )
    for policy in (True, False):
        assert (
            _compute(_dividend(is_progressive_or_doe_policy=policy)).input_facts[
                "dividend_sustainability_method"
            ]
            == SUSTAINABILITY_METHOD_THREE_FACTOR
            == "THREE_FACTOR"
        )


def test_compute_score_breakdown_uses_the_renormalized_score() -> None:
    """compute_score()の合成(breakdown・formulas)にも再正規化が反映される。"""
    dividend = _dividend(is_progressive_or_doe_policy=None, consecutive_dividend_increase_years=5)
    result = _compute(dividend)
    # _compute()の財務: payout 45% / 上限70% -> 余力 1 - 45/70 = 0.357142...
    # 係数 = 2/3 + 1/3 * 0.357142... = 0.785714... -> 15.71点
    # (従来は 0.4 + 0.2*0.3571 = 0.4714 -> 9.43点)
    assert result.breakdown.dividend_sustainability == pytest.approx(15.71)
    assert "再正規化" in result.formulas["dividend_sustainability"]


# --- T8: 実際のpipeline(BuySignalService.analyze)を通した保存値 ---------------------


def _analyze_with_policy(monkeypatch: pytest.MonkeyPatch, policy: bool | None):
    snapshot = _build_snapshot(_NIHON_SHINYAKU)
    snapshot = dataclasses.replace(
        snapshot,
        dividend=snapshot.dividend.model_copy(update={"is_progressive_or_doe_policy": policy}),
    )
    monkeypatch.setattr(service_module, "build_stock_snapshot", lambda *a, **kw: (snapshot, None))
    service = BuySignalService(providers=_providers(), config=_CONFIG, business_calendar=_CALENDAR)
    outcome = service.analyze(_NIHON_SHINYAKU.stock_code, _NOW, RecommendationType.BUY)
    assert outcome.recommendation is not None
    return outcome.recommendation


def test_pipeline_stores_the_renormalized_score_and_how_it_was_computed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fixtureのscoreを直接組み立てず、実際の`BuySignalService.analyze()`が保存する
    score_breakdown・buy_score_input_facts(score_formulas / component_states / 入力事実)で確認する。

    fixture: 連続増配5年・配当性向20%。上限は設定値(下で70%を確認)。
    余力 = 1 - 20/70 = 0.714285...
      None  : 2/3 + 1/3 * 0.714285... = 0.904761... -> 18.10点
              (従来 0.4 + 0.2*0.7143 = 0.5429 -> 10.86点)
      True  : 0.4 + 0.4 + 0.2*0.7143 = 0.942857 -> 18.86点(不変)
      False : 0.4 + 0.2*0.7143 = 0.542857 -> 10.86点(不変)
    """
    assert _CONFIG.screening.financial_health.max_payout_ratio_pct == _MAX_PAYOUT

    unknown = _analyze_with_policy(monkeypatch, None)
    assert unknown.score_breakdown is not None
    assert unknown.score_breakdown.dividend_sustainability == pytest.approx(18.10)
    facts = unknown.buy_score_input_facts
    assert facts is not None
    assert facts["dividend_sustainability_method"] == "RENORMALIZED_TWO_FACTOR"
    assert "再正規化" in facts["score_formulas"]["dividend_sustainability"]  # type: ignore[index]
    state = facts["component_states"]["dividend_sustainability"]  # type: ignore[index]
    assert "POLICY_UNKNOWN_RENORMALIZED" in state["reason_codes"]
    assert "POLICY_STATUS_UNKNOWN" in state["reason_codes"]

    confirmed = _analyze_with_policy(monkeypatch, True)
    assert confirmed.score_breakdown is not None
    assert confirmed.score_breakdown.dividend_sustainability == pytest.approx(18.86)
    assert confirmed.buy_score_input_facts["dividend_sustainability_method"] == "THREE_FACTOR"  # type: ignore[index]

    none_confirmed = _analyze_with_policy(monkeypatch, False)
    assert none_confirmed.score_breakdown is not None
    assert none_confirmed.score_breakdown.dividend_sustainability == pytest.approx(10.86)
    assert (
        "再正規化"
        not in none_confirmed.buy_score_input_facts["score_formulas"][  # type: ignore[index]
            "dividend_sustainability"
        ]
    )


def test_pipeline_leaves_every_other_score_component_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """変わるのは配当持続性のみ(総点の差 = 配当持続性の差)。閾値・他componentは不変。"""
    unknown = _analyze_with_policy(monkeypatch, None)
    false = _analyze_with_policy(monkeypatch, False)
    assert unknown.score_breakdown is not None and false.score_breakdown is not None
    diff = unknown.score_breakdown.total - false.score_breakdown.total
    sustainability_diff = (
        unknown.score_breakdown.dividend_sustainability
        - false.score_breakdown.dividend_sustainability
    )
    assert diff == pytest.approx(sustainability_diff)
    for name in (
        "total_yield_attractiveness",
        "financial_health",
        "undervaluation",
        "shareholder_benefit_value",
        "earnings_stability",
        "price_stability",
    ):
        assert getattr(unknown.score_breakdown, name) == getattr(false.score_breakdown, name)
