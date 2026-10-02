"""Issue #25: 方向性を持たないRecommendationTypeの評価対象除外(6型の意味論による分類)。

USER決定(2026-10-02): 現在設計されている6型を、意味論に基づいて一括整理する。
分類の根拠は「現在INCONCLUSIVEだから除外」ではなく、
**そのRecommendationTypeに、価格の上昇/下落という評価方向性が存在するか**である。

```
方向性が存在しない(判断の保留・確認の要請・安全弁) -> EXCLUDED
  WATCH_BEFORE_EARNINGS / REVIEW_BEFORE_EARNINGS / REVIEW_AFTER_EARNINGS /
  MANUAL_REVIEW_REQUIRED / PORTFOLIO_CONCENTRATION_REVIEW
方向性が存在する(「一部売却して下落リスクを避ける」助言)       -> EXIT
  PARTIAL_RISK_REDUCTION
```

型ごとの根拠は`domain/evaluation_rules.py`のモジュールdocstringに記録している。
本テストは、(1)分類結果、(2)各分類の**挙動**(INCONCLUSIVE / 起票されない /
EXIT型と同じ判定)、(3)既存のENTRY/EXIT型の挙動が変わっていないこと、を固定する。

★ 値はすべて架空値。Productionへの注入は行わない(判定関数を直接呼ぶ)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    EvaluationLabel,
    ImprovementAction,
    ImprovementPriority,
    RecommendationType,
)
from jstock_advisor.domain.entities.improvement import (
    PROBLEM_CATEGORY_EVALUATION_CRITERIA_UNDEFINED,
    ImprovementCandidate,
)
from jstock_advisor.domain.evaluation_rules import (
    _ENTRY_TYPES,
    _EVALUATION_UNDEFINED_TYPES,
    _EXCLUDED_TYPES,
    _EXIT_TYPES,
    determine_evaluation_label,
    is_entry_type,
    is_evaluation_excluded_type,
    is_exit_type,
    is_performance_evaluated_type,
)
from jstock_advisor.services.weekly_improvement_review_service import (
    WeeklyImprovementReviewService,
)

_CONFIG = load_config()
_EVAL = _CONFIG.evaluation
_NOW = dt.datetime(2026, 10, 2, 8, 0, tzinfo=dt.UTC)


@dataclass(frozen=True)
class _Semantics:
    """型ごとの分類結果と根拠(評価方向性の有無)。"""

    classification: str  # "EXCLUDED" / "EXIT"
    has_price_direction: bool
    basis: str


#: #25が判断した6型の分類結果。根拠は`domain/evaluation_rules.py`のdocstringと同じ内容。
_SIX_TYPES: dict[RecommendationType, _Semantics] = {
    RecommendationType.WATCH_BEFORE_EARNINGS: _Semantics(
        "EXCLUDED", False, "決算直前の方向判断の保留(#10 / #241の先例)"
    ),
    RecommendationType.REVIEW_BEFORE_EARNINGS: _Semantics(
        "EXCLUDED", False, "利確水準到達後、決算内容の確認まで提案を保留(売却価格ごと空にする)"
    ),
    RecommendationType.REVIEW_AFTER_EARNINGS: _Semantics(
        "EXCLUDED", False, "決算発表の確認待ち・猶予期間の抑制(方向判断を出していない)"
    ),
    RecommendationType.MANUAL_REVIEW_REQUIRED: _Semantics(
        "EXCLUDED", False, "自動判定の安全条件を満たさないときの安全弁(生成元なし)"
    ),
    RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW: _Semantics(
        "EXCLUDED", False, "保有比率の集中度の確認要請(株価の上昇/下落は成否の基準にならない)"
    ),
    RecommendationType.PARTIAL_RISK_REDUCTION: _Semantics(
        "EXIT",
        True,
        "PARTIAL_PROFIT_TAKEの表示ラベル差し替え。価格計算経路は同一で、下落リスクを避ける助言",
    ),
}

_EXCLUDED_SIX = tuple(t for t, s in _SIX_TYPES.items() if s.classification == "EXCLUDED")
_GRID = (-30.0, -10.0, -5.0, 0.0, 5.0, 9.9, 10.0, 15.0, 30.0)


# =============================================================================
# A) 分類結果 — 6型すべてが意味論に基づいて分類され、「未定」が残らない
# =============================================================================


@pytest.mark.parametrize("recommendation_type", tuple(_SIX_TYPES), ids=lambda t: t.value)
def test_six_types_are_classified_by_price_direction(
    recommendation_type: RecommendationType,
) -> None:
    """分類は「価格の上昇/下落という評価方向性があるか」と一致する(根拠方針の固定)。

    方向性が**ない**型だけがEXCLUDEDであり、**ある**型はEXCLUDEDへ入らない。
    (現在INCONCLUSIVEだからという理由で除外していないことの固定)
    """
    semantics = _SIX_TYPES[recommendation_type]

    in_excluded = recommendation_type in _EXCLUDED_TYPES
    in_exit = recommendation_type in _EXIT_TYPES

    assert in_excluded is (not semantics.has_price_direction), semantics.basis
    assert in_exit is semantics.has_price_direction, semantics.basis
    assert (semantics.classification == "EXCLUDED") is in_excluded
    assert (semantics.classification == "EXIT") is in_exit
    assert recommendation_type not in _EVALUATION_UNDEFINED_TYPES


def test_no_type_is_left_undefined_after_the_six_are_decided() -> None:
    """当時の6型はすべて判断済みで、`_EVALUATION_UNDEFINED_TYPES`は空である。"""
    assert _EVALUATION_UNDEFINED_TYPES == ()
    for recommendation_type in _SIX_TYPES:
        assert recommendation_type not in _EVALUATION_UNDEFINED_TYPES


def test_every_recommendation_type_is_still_classified_exactly_once() -> None:
    """全RecommendationTypeが、4つの集合のちょうど1つに属する(#270の網羅テストの再確認)。"""
    all_types = set(RecommendationType)
    sets = (
        set(_ENTRY_TYPES),
        set(_EXIT_TYPES),
        set(_EXCLUDED_TYPES),
        set(_EVALUATION_UNDEFINED_TYPES),
    )
    assert set().union(*sets) == all_types
    assert sum(len(s) for s in sets) == len(all_types)  # 重複なし


def test_classification_is_semantic_not_a_per_type_hardcode_of_inconclusive() -> None:
    """EXCLUDEDの型は、価格の動きがどうであっても常にINCONCLUSIVEである(方向性を持たない)。
    ただし分類の根拠はそれではなく方向性の有無である(上のテスト)。
    """
    for recommendation_type in _EXCLUDED_TYPES:
        assert is_evaluation_excluded_type(recommendation_type) is True
        assert is_performance_evaluated_type(recommendation_type) is False
        assert is_entry_type(recommendation_type) is False
        assert is_exit_type(recommendation_type) is False


# =============================================================================
# B) EXCLUDEDの挙動 — INCONCLUSIVE・自動起票されない
# =============================================================================


@pytest.mark.parametrize("recommendation_type", _EXCLUDED_SIX, ids=lambda t: t.value)
@pytest.mark.parametrize("price_return_pct", _GRID)
def test_excluded_types_are_always_inconclusive(
    recommendation_type: RecommendationType, price_return_pct: float
) -> None:
    label, _ = determine_evaluation_label(
        recommendation_type,
        price_return_pct=price_return_pct,
        excess_return_pct=None,
        max_drawdown_pct=None,
        config=_EVAL,
    )

    assert label is EvaluationLabel.INCONCLUSIVE


def _undefined_candidate(recommendation_type: RecommendationType) -> ImprovementCandidate:
    """「評価定義が未整備」の改善候補(週次レビューが生成する形)。"""
    return ImprovementCandidate(
        candidate_id="cand-0025",
        candidate_key="key-0025",
        recommendation_type=recommendation_type,
        rule_version="v1-mvp",
        segment_key=None,
        review_week="2026-W40",
        evaluation_period_start=_NOW.date(),
        evaluation_period_end=_NOW.date(),
        sample_count=10,
        conclusive_count=0,
        success_rate_pct=None,
        average_return_pct=None,
        average_excess_return_pct=None,
        previous_success_rate_pct=None,
        success_rate_change_points=None,
        consecutive_bad_weeks=0,
        priority=ImprovementPriority.B,
        problem_category=PROBLEM_CATEGORY_EVALUATION_CRITERIA_UNDEFINED,
        reason_codes=("EVALUATION_CRITERIA_UNDEFINED",),
        expected_improvement_pct=None,
        recommended_action=ImprovementAction.DEFINE_EVALUATION_CRITERIA,
        evidence=("架空の根拠",),
        is_current_rule_version=True,
    )


@pytest.mark.parametrize("recommendation_type", _EXCLUDED_SIX, ids=lambda t: t.value)
def test_excluded_types_do_not_create_github_issues(
    recommendation_type: RecommendationType,
) -> None:
    """方向性を持たない型は「評価定義が未整備」のIssueを自動起票しない
    (直しようのない未整備ではないため。#10が自動再生成される経路を塞ぐ)。
    """
    eligible = WeeklyImprovementReviewService._is_issue_eligible(
        None,  # type: ignore[arg-type]  # self は使わない
        _undefined_candidate(recommendation_type),
    )

    assert eligible is False


# =============================================================================
# C) EXITへ分類した型 — PARTIAL_PROFIT_TAKEと同じ評価を受ける
# =============================================================================


def test_partial_risk_reduction_is_an_exit_type() -> None:
    t = RecommendationType.PARTIAL_RISK_REDUCTION

    assert is_exit_type(t) is True
    assert is_entry_type(t) is False
    assert is_performance_evaluated_type(t) is True
    assert is_evaluation_excluded_type(t) is False


@pytest.mark.parametrize("price_return_pct", _GRID)
def test_partial_risk_reduction_is_evaluated_exactly_like_partial_profit_take(
    price_return_pct: float,
) -> None:
    """PARTIAL_RISK_REDUCTIONはPARTIAL_PROFIT_TAKEの表示ラベル違いであり、
    同じ入力に対して**同じラベル・同じ理由文**になる(推奨後の下落=当たり、上昇=利確が早すぎた)。
    """
    kwargs = dict(
        price_return_pct=price_return_pct,
        excess_return_pct=None,
        max_drawdown_pct=None,
        config=_EVAL,
    )
    reduction = determine_evaluation_label(RecommendationType.PARTIAL_RISK_REDUCTION, **kwargs)  # type: ignore[arg-type]
    profit_take = determine_evaluation_label(RecommendationType.PARTIAL_PROFIT_TAKE, **kwargs)  # type: ignore[arg-type]

    assert reduction == profit_take


def test_partial_risk_reduction_decline_is_success_and_rally_is_too_early() -> None:
    decline, _ = determine_evaluation_label(
        RecommendationType.PARTIAL_RISK_REDUCTION, -10.0, None, None, _EVAL
    )
    rally, _ = determine_evaluation_label(
        RecommendationType.PARTIAL_RISK_REDUCTION, 30.0, None, None, _EVAL
    )

    assert decline is EvaluationLabel.SUCCESS
    assert rally is EvaluationLabel.PROFIT_TAKE_TOO_EARLY


# =============================================================================
# D) 既存のENTRY/EXIT型の評価挙動は変わらない(デグレ無し)
# =============================================================================

_EXISTING_EXIT = (
    RecommendationType.PARTIAL_PROFIT_TAKE,
    RecommendationType.FULL_PROFIT_TAKE,
    RecommendationType.SELL,
    RecommendationType.URGENT_REVIEW,
    RecommendationType.WATCH,
    RecommendationType.REVIEW,
    RecommendationType.SELL_CONSIDERATION,
    RecommendationType.STRONG_SELL_CONSIDERATION,
)
_PROFIT_TAKE_TWO = (RecommendationType.PARTIAL_PROFIT_TAKE, RecommendationType.FULL_PROFIT_TAKE)


@pytest.mark.parametrize("recommendation_type", _EXISTING_EXIT, ids=lambda t: t.value)
def test_existing_exit_types_keep_their_labels(recommendation_type: RecommendationType) -> None:
    exit_cfg = _EVAL.exit_evaluation
    decline = exit_cfg.decline_confirms_good_call_pct
    rally = exit_cfg.rally_flags_too_early_or_too_sensitive_pct

    assert is_exit_type(recommendation_type) is True
    good, _ = determine_evaluation_label(recommendation_type, decline, None, None, _EVAL)
    middle, _ = determine_evaluation_label(
        recommendation_type, (decline + rally) / 2, None, None, _EVAL
    )
    high, _ = determine_evaluation_label(recommendation_type, rally, None, None, _EVAL)

    assert good is EvaluationLabel.SUCCESS
    assert middle is EvaluationLabel.ACCEPTABLE
    expected_high = (
        EvaluationLabel.PROFIT_TAKE_TOO_EARLY
        if recommendation_type in _PROFIT_TAKE_TWO
        else EvaluationLabel.SELL_TOO_SENSITIVE
    )
    assert high is expected_high


@pytest.mark.parametrize("recommendation_type", _ENTRY_TYPES, ids=lambda t: t.value)
def test_entry_types_keep_their_labels(recommendation_type: RecommendationType) -> None:
    success, _ = determine_evaluation_label(recommendation_type, 10.0, 3.0, None, _EVAL)
    acceptable, _ = determine_evaluation_label(recommendation_type, 10.0, -1.0, None, _EVAL)
    too_high, _ = determine_evaluation_label(recommendation_type, -2.0, None, None, _EVAL)

    assert is_entry_type(recommendation_type) is True
    assert success is EvaluationLabel.SUCCESS
    assert acceptable is EvaluationLabel.ACCEPTABLE
    assert too_high is EvaluationLabel.PRICE_TOO_HIGH


def test_existing_exit_and_entry_sets_only_gained_partial_risk_reduction() -> None:
    """ENTRY/EXITの構成は、PARTIAL_RISK_REDUCTIONを1つ加えた以外に変わっていない。"""
    assert set(_ENTRY_TYPES) == {
        RecommendationType.BUY,
        RecommendationType.WATCH_BUY,
        RecommendationType.HOLD,
    }
    assert set(_EXIT_TYPES) == set(_EXISTING_EXIT) | {RecommendationType.PARTIAL_RISK_REDUCTION}


def test_types_with_a_success_rate_threshold_are_performance_evaluated() -> None:
    """成功率の閾値(review_improvement.yaml)を持つ型は、すべて成績評価の対象である
    (評価対象外へ移した型が、成績悪化の閾値判定の対象から落ちていないことの確認)。
    """
    thresholds = _CONFIG.review_improvement.min_success_rate_pct
    for name in thresholds:
        assert is_performance_evaluated_type(RecommendationType(name)) is True, name
