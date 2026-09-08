"""Issue #249 O-1'': baseline比較不能の項目をスコアの分母から除く。

買った直後の初回評価では、baselineが無いため「前回と比べて崩れていないか」を
見る3項目(優待条件・利益CF前提・財務前提)を判定できない。これを0点として
分母に数えていたため、**初回評価だけスコアが不当に低く出ていた**。

比較できないことは「悪い」ことではないため、NOT_APPLICABLE(評価対象外)と
同じくスコアの分母から外す。ただし**coverage_ratioの分母は据え置き**、
「評価できなかった」事実は残す(点数だけを直し、確認できていないことを隠さない)。

```
★ Issue #55 Phase A Decision 3 は変更しない。
  データ欠測(total_yield / custom_conditions)のNOT_EVALUATEDは分母に残す。
  test_missing_yield_semantics.py の契約テストは書き換えていない。
  本ファイルのT-2はその契約がO-1''の実装でも保たれることを独立に固定する。
```

```
本ファイルの値はすべて架空値である(config由来の配点と架空の利回りのみ)。
実在の銘柄コード・所有者名・保有データは含まない。
```
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import InvestmentThesisWeights
from jstock_advisor.domain.entities.enums import BaselineOrigin, EvidenceCoverageStatus
from jstock_advisor.domain.signals.investment_thesis_scoring import (
    InvestmentThesisInputs,
    score_investment_thesis,
)

_CFG = load_config()
_WEIGHTS = _CFG.holding_decision.investment_thesis_weights
_TEMPLATE = _CFG.investment_thesis_template
_FRESH = _CFG.holding_decision.fresh_within_days
_STALE = _CFG.holding_decision.stale_after_days
_NOW = dt.datetime(2026, 9, 7, tzinfo=dt.UTC)

_BASELINE_ITEMS = frozenset({"benefit_condition", "profit_cf_premise", "financial_premise"})


def _inputs(**overrides: Any) -> InvestmentThesisInputs:
    """2回目以降(baselineあり・すべて維持)を既定とする。"""
    base: dict[str, Any] = {
        "current_total_yield_pct": _TEMPLATE.min_total_yield_pct,
        "has_shareholder_benefit": True,
        "benefit_abolished_or_downgraded": False,
        "dividend_cut_or_omission_confirmed": False,
        "profit_cf_premise_broken": False,
        "financial_premise_broken": False,
        "thesis": None,
    }
    base.update(overrides)
    return InvestmentThesisInputs(**base)


def _first_evaluation(**overrides: Any) -> InvestmentThesisInputs:
    """初回評価(baseline比較3項目がNone)。"""
    return _inputs(
        benefit_abolished_or_downgraded=None,
        profit_cf_premise_broken=None,
        financial_premise_broken=None,
        **overrides,
    )


def _score(inputs: InvestmentThesisInputs, weights: InvestmentThesisWeights = _WEIGHTS):
    return score_investment_thesis(inputs, weights, _TEMPLATE, _FRESH, _STALE, _NOW)


def _item(result: Any, item_code: str) -> Any:
    return next(i for i in result.items if i.item_code == item_code)


# --- T-1  初回と2回目が一致する（本Issueの目的） ---------------------------


def test_first_evaluation_scores_the_same_as_the_second() -> None:
    """★ 同じ状態なら、初回でも2回目でもスコアが一致する。

    2回目は3項目が「維持されている」= 満点であり、初回は「比較できない」。
    比較できない項目を分母から外すと、残った項目の達成率でスコアが決まるため、
    どちらも満点率となり一致する。

    ★ これが本Issueの目的である(初回だけ低く出ていたのを直す)。
    """
    first = _score(_first_evaluation())
    second = _score(_inputs())

    assert first.score == second.score
    assert second.score == pytest.approx(50.0)


def test_first_evaluation_was_previously_lower_than_the_second() -> None:
    """★ 修正前の式なら初回が低くなっていたことを、同じ入力で示す。

    修正前 = raw_points / available_weight * 50（比較不能を分母に残す）
    この値を再現し、修正後のスコアが**それより高い**ことを固定する。
    回帰が入って分母の除外が効かなくなれば、この差が消えて落ちる。
    """
    first = _score(_first_evaluation())
    available = sum(
        i.weight for i in first.items if i.status != EvidenceCoverageStatus.NOT_APPLICABLE
    )
    raw = sum(i.points_earned for i in first.items)
    legacy_score = raw / available * 50.0

    assert legacy_score < first.score
    assert legacy_score == pytest.approx(27.777, abs=0.01)


# --- T-2  #55 Decision 3 の回帰（最重要） ------------------------------------


def test_missing_total_yield_stays_in_the_denominator_issue_55_decision_3() -> None:
    """★ データ欠測は従来どおり分母に残る（Issue #55 Phase A Decision 3）。

    「確定0%」と「欠測」でスコアが同一になり、差はcoverageにだけ出る、という
    既存契約。本Issueは**この契約に触れない**。
    total_yieldのNOT_EVALUATEDには理由を付けないため、分母の除外対象にならない。

    ★ 同じ主張を test_missing_yield_semantics.py が固定しているが、
      あちらは書き換えていない。ここでは O-1'' の実装でも保たれることを
      独立に固定する（片方が消えてももう片方が気づく）。
    """
    zero = _score(_inputs(current_total_yield_pct=0.0))
    unknown = _score(_inputs(current_total_yield_pct=None))

    assert zero.score == unknown.score
    assert unknown.coverage_ratio < zero.coverage_ratio


def test_missing_total_yield_is_not_marked_baseline_not_comparable() -> None:
    """欠測と比較不能が reason で区別されていること（取り違えの防止）。"""
    unknown = _score(_inputs(current_total_yield_pct=None))

    assert _item(unknown, "total_yield").status == EvidenceCoverageStatus.NOT_EVALUATED
    assert _item(unknown, "total_yield").reason != "BASELINE_NOT_COMPARABLE"


# --- T-3  NOT_APPLICABLE との一貫性 -----------------------------------------


def test_baseline_not_comparable_is_excluded_like_not_applicable() -> None:
    """★ 比較不能は、評価対象外(NOT_APPLICABLE)と**同じく分母から外れる**。

    優待非保有(NOT_APPLICABLE)と、優待はあるが比較相手が無い(比較不能)で、
    スコアが一致することで示す。どちらも「その項目で減点しない」が正しい。
    """
    not_applicable = _score(
        _inputs(has_shareholder_benefit=False, benefit_abolished_or_downgraded=None)
    )
    not_comparable = _score(_inputs(benefit_abolished_or_downgraded=None))

    assert not_applicable.score == not_comparable.score


# --- T-4  coverage は下がったまま -------------------------------------------


def test_coverage_ratio_still_drops_on_first_evaluation() -> None:
    """★ スコアは直るが「評価できなかった」事実はcoverageに残る。

    ここが O-1''（分母を2つに分ける）の要点である。coverageまで1.0にすると
    coverage gateが素通りし、確認できていないことを隠すことになる。
    """
    first = _score(_first_evaluation())
    second = _score(_inputs())

    assert first.coverage_ratio < 1.0
    assert second.coverage_ratio == pytest.approx(1.0)
    assert first.score == second.score, "スコアだけが一致し、coverageは一致しない"


# --- T-5  reason に理由が残る -----------------------------------------------


def test_baseline_not_comparable_reason_is_recorded_for_audit() -> None:
    """★ 監査記録から「なぜ評価されなかったか」が読めること。

    statusはNOT_EVALUATEDのまま（新しいenum値を足さない = 保存形式を変えない）
    ため、区別はreasonにしか出ない。集計側はreasonを見る必要がある。
    """
    first = _score(_first_evaluation())

    for item_code in sorted(_BASELINE_ITEMS):
        item = _item(first, item_code)
        assert item.status == EvidenceCoverageStatus.NOT_EVALUATED
        assert item.reason == "BASELINE_NOT_COMPARABLE", item_code


def test_second_evaluation_has_no_not_comparable_items() -> None:
    """2回目以降は比較不能が1件も無い = 分母が変わらない（式による保証の確認）。"""
    second = _score(_inputs())

    assert [i.item_code for i in second.items if i.reason == "BASELINE_NOT_COMPARABLE"] == []


# --- T-6  分母 0 の境界（fail-closed） ---------------------------------------


def test_zero_denominator_is_fail_closed() -> None:
    """★ 分母が0なら score 0.0 かつ **coverage_ratio も 0.0**。

    現行configでは到達しない（dividend_policyはbaseline不要で常に評価される）が、
    weightsのvalidatorは合計50点しか検査しないため、構造的には起こりうる。

    scoreだけを0.0にしてcoverageを据え置くと、「評価できていない」のに
    coverageが高いままとなりgateをすり抜ける。0点は最も低い評価とも
    区別できない。そのため**両方を0.0にする**。
    """
    weights = InvestmentThesisWeights(
        dividend_policy=0.0,
        total_yield=0.0,
        benefit_condition=10.0,
        profit_cf_premise=20.0,
        financial_premise=20.0,
        custom_conditions=0.0,
    )

    result = _score(_first_evaluation(), weights)

    assert result.score == 0.0
    assert result.coverage_ratio == 0.0


def test_current_config_never_reaches_zero_denominator() -> None:
    """現行configでは分母0へ到達しないこと（dividend_policyが常に残る）。"""
    first = _score(_first_evaluation())

    assert _item(first, "dividend_policy").status == EvidenceCoverageStatus.EVALUATED
    assert _WEIGHTS.dividend_policy > 0
    assert first.score > 0.0


# --- T-7  hard gate は不変 ---------------------------------------------------


def test_score_only_moves_upward_so_the_hard_gate_cannot_newly_fire() -> None:
    """★ hard gate（投資ストーリー崩壊）の発火条件へ近づかないこと。

    hard gate = baseline.origin == HUMAN_APPROVED かつ score < 5.0。
    本変更はスコアを**下げない**（分母だけが小さくなり、raw_pointsは不変）。
    したがって新たに5.0を下回ることはない。

    ★ さらに初回評価のbaselineはSYSTEM_INITIALIZEDであり、
      第1条件（HUMAN_APPROVED）を満たさない。
    """
    first = _score(_first_evaluation())
    available = sum(
        i.weight for i in first.items if i.status != EvidenceCoverageStatus.NOT_APPLICABLE
    )
    legacy_score = sum(i.points_earned for i in first.items) / available * 50.0

    assert first.score >= legacy_score, "スコアは下がらない"
    assert first.score >= 5.0

    with_origin = score_investment_thesis(
        _first_evaluation(),
        _WEIGHTS,
        _TEMPLATE,
        _FRESH,
        _STALE,
        _NOW,
        baseline_origin=BaselineOrigin.SYSTEM_INITIALIZED,
    )
    assert with_origin.baseline_origin == BaselineOrigin.SYSTEM_INITIALIZED


def test_broken_premises_are_still_scored_zero_not_excluded() -> None:
    """★ 「崩れた」と判定できた項目は分母に残り0点になる（除外しない）。

    除外するのは**比較できなかった**ものだけである。ここを取り違えると
    「悪い状態を無かったことにする」実装になり、判定が甘くなる。
    """
    broken = _score(
        _inputs(
            benefit_abolished_or_downgraded=True,
            profit_cf_premise_broken=True,
            financial_premise_broken=True,
        )
    )

    for item_code in sorted(_BASELINE_ITEMS):
        item = _item(broken, item_code)
        assert item.status == EvidenceCoverageStatus.EVALUATED, item_code
        assert item.points_earned == 0.0, item_code
    assert broken.score < _score(_first_evaluation()).score
