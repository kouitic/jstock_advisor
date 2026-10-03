"""Issue #30 是正: 配当持続性(S-09 `score_dividend_sustainability`)の「不明は0点」。

USER決定(#122 issuecomment-5968256759 §3): 累進配当/DOE方針がregistry未登録のため
None(不明)の銘柄は、方針項を0点とする(未確認を加点しない)。他の係数(0.4 / 0.4 / 0.2)は
変えず、#760が入れた再正規化(None のとき 2/3 : 1/3)は撤回した。不明は、確認済み・方針なし
(False)と同点で、確認済み・方針あり(True)を上回らない。閾値の再調整は#28の範囲。

期待値はすべて固定値(ハードコード)で持つ。値は#760より前の
`score_dividend_sustainability()`(4866437の第1親のソース)を実際に呼んで得た27組で、
実装の式を再現して期待値を作ることはしない(式を取り違えても同じ取り違えで通るため)。
"""

from __future__ import annotations

import dataclasses

import pytest

from jstock_advisor.domain.entities.enums import RecommendationType
from jstock_advisor.domain.scoring import score as score_module
from jstock_advisor.domain.scoring.score import score_dividend_sustainability
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


# 連続増配年数 × 配当性向(None / 35% = 余力0.5 / 0% = 余力満点)ごとの値(weight 20、payout上限70%)。
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
# #760より前の値。None(不明)の期待値は False と同じ(= 是正後の値)。
_PRE_760_FALSE = [0.0, 2.0, 4.0, 3.2, 5.2, 7.2, 8.0, 10.0, 12.0]
_PRE_760_NONE = _PRE_760_FALSE
_PRE_760_TRUE = [8.0, 10.0, 12.0, 11.2, 13.2, 15.2, 16.0, 18.0, 20.0]
# #760の再正規化(撤回済み)がNoneに与えていた値。是正後はどの組でもこの値にならない
# (0.0 の組は再正規化しても0のまま同値のため比較しない)。
_RENORMALIZED_NONE_RETRACTED = [
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


# --- T1/T2: 不明(None)は方針項0点。#760前の値と9組すべてで一致し、再正規化されない ---------


@pytest.mark.parametrize(("combo", "expected"), list(zip(_COMBOS, _PRE_760_NONE, strict=True)))
def test_unknown_policy_scores_the_policy_term_as_zero(
    combo: tuple[int, float | None], expected: float
) -> None:
    years, payout = combo
    score, _ = _score(None, years, payout)
    assert score == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize(
    ("combo", "retracted"), list(zip(_COMBOS, _RENORMALIZED_NONE_RETRACTED, strict=True))
)
def test_unknown_policy_is_not_renormalized_any_more(
    combo: tuple[int, float | None], retracted: float
) -> None:
    years, payout = combo
    score, _ = _score(None, years, payout)
    if retracted == 0.0:
        assert score == pytest.approx(0.0)
    else:
        assert score != pytest.approx(retracted, abs=1e-6)


def test_unknown_policy_cannot_reach_the_full_weight_without_a_confirmed_policy() -> None:
    """連続増配5年・配当性向の余力満点でも、方針が未確認なら12点止まり(満点20点には届かない)。"""
    assert _score(None, 5, 0.0)[0] == pytest.approx(12.0)
    assert _score(True, 5, 0.0)[0] == pytest.approx(20.0)


# --- T3: 不明 ≤ 確認済み・方針なし < 確認済み・方針あり(9組すべて。向きを固定する) ----------


@pytest.mark.parametrize("combo", _COMBOS)
def test_unknown_is_never_above_confirmed_no_policy(combo: tuple[int, float | None]) -> None:
    years, payout = combo
    unknown = _score(None, years, payout)[0]
    confirmed_no = _score(False, years, payout)[0]
    assert unknown <= confirmed_no + 1e-9
    assert unknown == pytest.approx(confirmed_no)  # 同点(減点もしない)


@pytest.mark.parametrize("combo", _COMBOS)
def test_unknown_is_below_confirmed_policy(combo: tuple[int, float | None]) -> None:
    years, payout = combo
    unknown = _score(None, years, payout)[0]
    confirmed_yes = _score(True, years, payout)[0]
    assert unknown < confirmed_yes
    assert confirmed_yes - unknown == pytest.approx(8.0)  # 方針項 0.4 × 20 点


# --- T4: 確認済み(True/False)は#760前と一致(不変) -----------------------------------


@pytest.mark.parametrize(("combo", "expected"), list(zip(_COMBOS, _PRE_760_FALSE, strict=True)))
def test_confirmed_no_policy_is_unchanged(combo: tuple[int, float | None], expected: float) -> None:
    years, payout = combo
    assert _score(False, years, payout)[0] == pytest.approx(expected)


@pytest.mark.parametrize(("combo", "expected"), list(zip(_COMBOS, _PRE_760_TRUE, strict=True)))
def test_confirmed_policy_is_unchanged(combo: tuple[int, float | None], expected: float) -> None:
    years, payout = combo
    assert _score(True, years, payout)[0] == pytest.approx(expected)


def test_missing_data_conventions_are_unchanged_for_unknown_policy() -> None:
    """欠測規約: 年数None=0年扱い、配当性向None・上限<=0なら余力項は0。年数は5年で頭打ち。"""
    assert _score(None, None, 35.0)[0] == pytest.approx(_score(None, 0, 35.0)[0])
    assert _score(None, 5, None)[0] == pytest.approx(8.0)
    assert _score(None, 5, 35.0, max_payout=0.0)[0] == pytest.approx(8.0)
    assert _score(None, 5, 35.0, max_payout=-1.0)[0] == pytest.approx(8.0)
    # 配当性向が上限を超えても余力は0(負にならない)
    assert _score(None, 5, 90.0)[0] == pytest.approx(8.0)
    assert _score(None, 9, 0.0)[0] == pytest.approx(12.0)


# --- T5: formula文字列(監査記録 calculation_formulas)と、不明の判別性 -----------------


def test_confirmed_policy_formula_text_is_byte_identical_to_before() -> None:
    _, formula = _score(True, 5, 35.0)
    assert formula == (
        "配当持続性係数0.90(累進配当/DOE方針(+0.4), 連続増配5年評価(+0.40), "
        "配当性向の余力評価(+0.10)) × 配点20.0点"
    )
    _, formula = _score(False, 5, 35.0)
    assert formula == (
        "配当持続性係数0.50(連続増配5年評価(+0.40), 配当性向の余力評価(+0.10)) × 配点20.0点"
    )


def test_unknown_policy_formula_states_the_policy_term_is_zero() -> None:
    _, formula = _score(None, 5, 35.0)
    assert formula == (
        "配当持続性係数0.50(累進配当/DOE方針は未確認のため方針項は0点(加点しない), "
        "連続増配5年評価(+0.40), 配当性向の余力評価(+0.10)) × 配点20.0点"
    )
    assert "再正規化" not in formula
    assert "累進配当/DOE方針(+0.4)" not in formula


# --- T6: 観測記録(reason_code / input_facts) ---------------------------------------


def _reason_codes(policy: bool | None) -> list[str]:
    state = _compute(_dividend(is_progressive_or_doe_policy=policy)).component_states[
        "dividend_sustainability"
    ]
    assert isinstance(state, dict)
    codes = state["reason_codes"]
    assert isinstance(codes, list)
    return codes


def test_reason_codes_distinguish_unknown_from_confirmed_no_policy() -> None:
    unknown = _reason_codes(None)
    assert "POLICY_STATUS_UNKNOWN" in unknown
    assert "POLICY_NONE_CONFIRMED" not in unknown
    confirmed_none = _reason_codes(False)
    assert "POLICY_NONE_CONFIRMED" in confirmed_none
    assert "POLICY_STATUS_UNKNOWN" not in confirmed_none
    confirmed_yes = _reason_codes(True)
    assert "POLICY_STATUS_UNKNOWN" not in confirmed_yes
    assert "POLICY_NONE_CONFIRMED" not in confirmed_yes
    # #760が足した再正規化の観測記録は、再正規化と一緒に撤回した
    for codes in (unknown, confirmed_none, confirmed_yes):
        assert "POLICY_UNKNOWN_RENORMALIZED" not in codes


def test_input_facts_still_discriminate_unknown_and_drop_the_method_key() -> None:
    """不明の判別は既存の status(UNKNOWN / CONFIRMED)と is_progressive_or_doe_policy(None)で
    残る。#760が足した `dividend_sustainability_method` は、再正規化の別を表す値だったため撤回した
    (#760は未deployで、保存済みレコードに存在しない)。"""
    unknown = _compute(_dividend(is_progressive_or_doe_policy=None)).input_facts
    assert unknown["shareholder_return_policy_status"] == "UNKNOWN"
    assert unknown["is_progressive_or_doe_policy"] is None
    assert "dividend_sustainability_method" not in unknown
    for policy in (True, False):
        facts = _compute(_dividend(is_progressive_or_doe_policy=policy)).input_facts
        assert facts["shareholder_return_policy_status"] == "CONFIRMED"
        assert "dividend_sustainability_method" not in facts


def test_renormalization_api_is_removed_from_the_scoring_module() -> None:
    for name in (
        "dividend_sustainability_method",
        "SUSTAINABILITY_METHOD_THREE_FACTOR",
        "SUSTAINABILITY_METHOD_RENORMALIZED_TWO_FACTOR",
    ):
        assert not hasattr(score_module, name)


def test_component_is_still_evaluated_and_in_the_denominator() -> None:
    state = _compute(_dividend()).component_states["dividend_sustainability"]
    assert isinstance(state, dict)
    assert state["excluded_from_denominator"] is False
    assert str(state["state"]) == "EVALUATED"


def test_compute_score_breakdown_scores_unknown_like_confirmed_no_policy() -> None:
    dividend_unknown = _dividend(
        is_progressive_or_doe_policy=None, consecutive_dividend_increase_years=5
    )
    dividend_false = _dividend(
        is_progressive_or_doe_policy=False, consecutive_dividend_increase_years=5
    )
    unknown = _compute(dividend_unknown)
    confirmed_no = _compute(dividend_false)
    # _compute()の財務: payout 45% / 上限70% -> 余力 1 - 45/70 = 0.357142...
    # 係数 = 0.4 + 0.2 * 0.357142... = 0.471428... -> 9.43点(#760の再正規化なら15.71点だった)
    assert unknown.breakdown.dividend_sustainability == pytest.approx(9.43)
    assert confirmed_no.breakdown.dividend_sustainability == pytest.approx(9.43)
    assert unknown.breakdown.total == pytest.approx(confirmed_no.breakdown.total)
    assert "未確認のため方針項は0点" in unknown.formulas["dividend_sustainability"]
    assert "未確認" not in confirmed_no.formulas["dividend_sustainability"]


# --- T7: 実際のpipeline(BuySignalService.analyze)を通した保存値 -----------------------


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


def test_pipeline_stores_the_pre_760_score_and_marks_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fixtureのscoreを直接組み立てず、実際の`BuySignalService.analyze()`が保存する
    score_breakdown・buy_score_input_facts(score_formulas / component_states / 入力事実)で確認する。

    fixture: 連続増配5年・配当性向20%。上限は設定値(下で70%を確認)。
    余力 = 1 - 20/70 = 0.714285...
      None  : 0.4 + 0.2*0.7143 = 0.542857 -> 10.86点(#760の前と同じ。#760の再正規化なら18.10点)
      True  : 0.4 + 0.4 + 0.2*0.7143 = 0.942857 -> 18.86点(不変)
      False : 0.4 + 0.2*0.7143 = 0.542857 -> 10.86点(不変)
    """
    assert _CONFIG.screening.financial_health.max_payout_ratio_pct == _MAX_PAYOUT

    unknown = _analyze_with_policy(monkeypatch, None)
    assert unknown.score_breakdown is not None
    assert unknown.score_breakdown.dividend_sustainability == pytest.approx(10.86)
    facts = unknown.buy_score_input_facts
    assert facts is not None
    assert "dividend_sustainability_method" not in facts
    assert "未確認のため方針項は0点" in facts["score_formulas"]["dividend_sustainability"]  # type: ignore[index]
    state = facts["component_states"]["dividend_sustainability"]  # type: ignore[index]
    assert "POLICY_STATUS_UNKNOWN" in state["reason_codes"]
    assert "POLICY_UNKNOWN_RENORMALIZED" not in state["reason_codes"]

    confirmed = _analyze_with_policy(monkeypatch, True)
    assert confirmed.score_breakdown is not None
    assert confirmed.score_breakdown.dividend_sustainability == pytest.approx(18.86)

    none_confirmed = _analyze_with_policy(monkeypatch, False)
    assert none_confirmed.score_breakdown is not None
    assert none_confirmed.score_breakdown.dividend_sustainability == pytest.approx(10.86)
    assert (
        "未確認"
        not in none_confirmed.buy_score_input_facts["score_formulas"][  # type: ignore[index]
            "dividend_sustainability"
        ]
    )


def test_pipeline_unknown_total_equals_confirmed_no_policy_and_other_components_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不明の総点は確認済み・方針なしと同じ(未確認が加点されない)。他componentも不変。"""
    unknown = _analyze_with_policy(monkeypatch, None)
    false = _analyze_with_policy(monkeypatch, False)
    assert unknown.score_breakdown is not None and false.score_breakdown is not None
    assert unknown.score_breakdown.total == pytest.approx(false.score_breakdown.total)
    for name in (
        "total_yield_attractiveness",
        "dividend_sustainability",
        "financial_health",
        "undervaluation",
        "shareholder_benefit_value",
        "earnings_stability",
        "price_stability",
    ):
        assert getattr(unknown.score_breakdown, name) == getattr(false.score_breakdown, name)
