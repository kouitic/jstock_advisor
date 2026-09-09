"""Issue #270(横断監査 N-02): 週次レビューの評価対象型に新エンジンの売却系を含める。

```
問題  `_EXIT_TYPES` が手書きの tuple で、保有判断エンジンが出す売却系
      （SELL_CONSIDERATION / STRONG_SELL_CONSIDERATION）に**追随していなかった**。

      -> これらの推奨は `determine_evaluation_label()` で常に **INCONCLUSIVE** になり、
         ★ 週次改善レビューが「評価定義が未整備」の GitHub Issue を**毎週自動起票**する。
         成績が測れないため較正（#28）とルール改善の入力も欠ける。
```

```
方式 O-2（USER の DESIGN_GATE 承認）+ 管理者判断 A-1 の最小形
  (1) SELL_CONSIDERATION / STRONG_SELL_CONSIDERATION を EXIT へ追加
  (2) URGENT_HOLDING_REVIEW は「**確認**という状態」であり方向性を持たないため
      **評価対象外が仕様として妥当**。除外集合へ入れ、自動起票を止める
  (3) 残りは「評価基準がまだ決まっていない」型として分類し、
      ★ **#25 の設計判断を先取りしない**（従来どおり起票が続く）
```

```
★ 本 Issue の欠陥の本質は「型が増えたのに分類が追随しなかったこと」である。
  したがって**再発防止の中心は網羅テスト**（T-4）であり、
  型を 1 つ足して分類し忘れれば CI が落ちる。
```

```
★ 値はすべて架空値。実在の銘柄コード・所有者名は使わない。
★ Production への注入は行わない（判定関数を直接呼ぶ）。
★ 過去に評価済みのレコードは**再集計しない**（backfill は行わない）。
```
"""

from __future__ import annotations

import datetime as dt

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
    PROBLEM_CATEGORY_PERFORMANCE_DEGRADED,
    ImprovementCandidate,
)
from jstock_advisor.domain.evaluation_rules import (
    _ENTRY_TYPES,
    _EVALUATION_UNDEFINED_TYPES,
    _EXCLUDED_TYPES,
    _EXIT_TYPES,
    determine_evaluation_label,
    is_evaluation_excluded_type,
    is_exit_type,
    is_performance_evaluated_type,
)
from jstock_advisor.services.weekly_improvement_review_service import (
    WeeklyImprovementReviewService,
)

_CONFIG = load_config().evaluation
_NOW = dt.datetime(2026, 9, 9, 8, 0, tzinfo=dt.UTC)

#: 本 Issue で EXIT へ加えた保有判断エンジンの売却系。
_NEW_EXIT_TYPES = (
    RecommendationType.SELL_CONSIDERATION,
    RecommendationType.STRONG_SELL_CONSIDERATION,
)


# =============================================================================
# A) 目的 — 売却系が評価対象になる
# =============================================================================


@pytest.mark.parametrize("recommendation_type", _NEW_EXIT_TYPES, ids=lambda t: t.value)
def test_t1_new_sell_types_are_evaluated_not_inconclusive(
    recommendation_type: RecommendationType,
) -> None:
    """★ T-1: 新エンジンの売却系が **INCONCLUSIVE 以外**になること。

    **修正前は常に INCONCLUSIVE だった**（`_EXIT_TYPES` に無かったため）。
    本 Issue の回帰の本体である。
    """
    label, _ = determine_evaluation_label(
        recommendation_type,
        price_return_pct=-20.0,  # 大きく下落 = 売却の警告が当たった
        excess_return_pct=None,
        max_drawdown_pct=None,
        config=_CONFIG,
    )

    assert label is not EvaluationLabel.INCONCLUSIVE
    assert label is EvaluationLabel.SUCCESS


@pytest.mark.parametrize("recommendation_type", _NEW_EXIT_TYPES, ids=lambda t: t.value)
def test_t1b_new_sell_types_can_fail_too(recommendation_type: RecommendationType) -> None:
    """逆側: 上昇していれば **SELL_TOO_SENSITIVE**（過敏だった）になること。

    ★ 「INCONCLUSIVE でなくなった」だけでなく、**成否の両方が付きうる**ことを見る。
      片側しか出ないなら成績として機能しない。
    """
    label, _ = determine_evaluation_label(
        recommendation_type,
        price_return_pct=30.0,
        excess_return_pct=None,
        max_drawdown_pct=None,
        config=_CONFIG,
    )

    assert label is EvaluationLabel.SELL_TOO_SENSITIVE


@pytest.mark.parametrize("recommendation_type", _NEW_EXIT_TYPES, ids=lambda t: t.value)
def test_t1c_new_sell_types_are_exit_types(recommendation_type: RecommendationType) -> None:
    """EXIT として扱われること（ENTRY 側の超過リターン判定へ回らない）。"""
    assert is_exit_type(recommendation_type) is True
    assert is_performance_evaluated_type(recommendation_type) is True


# =============================================================================
# B) 境界（DoD 項目 1・2）
# =============================================================================


@pytest.mark.parametrize(
    ("price_return_pct", "expected"),
    [
        (-5.0, EvaluationLabel.SUCCESS),  # 閾値ちょうど
        (-4.9, EvaluationLabel.ACCEPTABLE),  # 手前
        (9.9, EvaluationLabel.ACCEPTABLE),  # 手前
        (10.0, EvaluationLabel.SELL_TOO_SENSITIVE),  # 閾値ちょうど
    ],
    ids=["success_exactly", "just_inside", "just_below_rally", "rally_exactly"],
)
def test_t2_threshold_boundaries(price_return_pct: float, expected: EvaluationLabel) -> None:
    """境界: `<= -5.0`（SUCCESS）と `>= +10.0`（TOO_SENSITIVE）のちょうど / 手前。

    ★ 閾値そのものは本 Issue で **1 つも変えていない**（単一閾値 = USER 承認の H-2）。
      新しく対象になった型でも**同じ閾値が同じように効く**ことを固定する。
    """
    label, _ = determine_evaluation_label(
        RecommendationType.SELL_CONSIDERATION,
        price_return_pct=price_return_pct,
        excess_return_pct=None,
        max_drawdown_pct=None,
        config=_CONFIG,
    )

    assert label is expected


def test_t2b_monotonic_direction() -> None:
    """単調性: 下落が大きいほど良い側へ向かい、逆転しないこと。"""
    labels = [
        determine_evaluation_label(
            RecommendationType.STRONG_SELL_CONSIDERATION,
            price_return_pct=pct,
            excess_return_pct=None,
            max_drawdown_pct=None,
            config=_CONFIG,
        )[0]
        for pct in (30.0, 0.0, -30.0)
    ]

    assert labels == [
        EvaluationLabel.SELL_TOO_SENSITIVE,
        EvaluationLabel.ACCEPTABLE,
        EvaluationLabel.SUCCESS,
    ]


# =============================================================================
# C) 既存の評価が 1 つも壊れないこと（受入条件 A-3）
# =============================================================================


_EXISTING_EXIT_TYPES = (
    RecommendationType.PARTIAL_PROFIT_TAKE,
    RecommendationType.FULL_PROFIT_TAKE,
    RecommendationType.SELL,
    RecommendationType.URGENT_REVIEW,
    RecommendationType.WATCH,
    RecommendationType.REVIEW,
)


@pytest.mark.parametrize("recommendation_type", _EXISTING_EXIT_TYPES, ids=lambda t: t.value)
def test_t3_existing_exit_types_are_not_lost(recommendation_type: RecommendationType) -> None:
    """★ A-3: 既存 EXIT 6 型が **1 つも失われない**こと。

    `_EXIT_TYPES = SELL_LIKE_RECOMMENDATION_TYPES` という単純置換を行うと、
    利確 2 型と WATCH が**評価対象から外れる**。その回帰を直接固定する。
    """
    assert is_exit_type(recommendation_type) is True


@pytest.mark.parametrize(
    "recommendation_type",
    (RecommendationType.PARTIAL_PROFIT_TAKE, RecommendationType.FULL_PROFIT_TAKE),
    ids=lambda t: t.value,
)
def test_t3b_profit_take_too_early_is_still_reachable(
    recommendation_type: RecommendationType,
) -> None:
    """★★ A-3: **PROFIT_TAKE_TOO_EARLY が到達可能なまま**であること。

    利確が早すぎたことを検出する**唯一の経路**であり、
    単純置換ではここが到達不能になる（Phase A が名指しした回帰）。
    """
    label, _ = determine_evaluation_label(
        recommendation_type,
        price_return_pct=30.0,
        excess_return_pct=None,
        max_drawdown_pct=None,
        config=_CONFIG,
    )

    assert label is EvaluationLabel.PROFIT_TAKE_TOO_EARLY


@pytest.mark.parametrize("recommendation_type", _ENTRY_TYPES, ids=lambda t: t.value)
def test_t3c_entry_types_are_unchanged(recommendation_type: RecommendationType) -> None:
    """ENTRY 3 型の判定が 1 つも変わらないこと。"""
    label, _ = determine_evaluation_label(
        recommendation_type,
        price_return_pct=10.0,
        excess_return_pct=3.0,
        max_drawdown_pct=None,
        config=_CONFIG,
    )

    assert label is EvaluationLabel.SUCCESS


# =============================================================================
# D) ★ 網羅テスト（受入条件 A-2）— 本 Issue の再発防止の中心
# =============================================================================


def test_t4_every_recommendation_type_is_classified_exactly_once() -> None:
    """★★ 全 RecommendationType が **4 つの集合のちょうど 1 つ**に属すること。

    本 Issue の欠陥は「新しい型が増えたのに `_EXIT_TYPES` が追随せず、
    **静かに INCONCLUSIVE になり続けた**」ことだった。
    型を足して分類し忘れれば、このテストが **CI で落ちて知らせる**。

    ★ 件数を直書きしない。`RecommendationType` 全件との**集合一致**で見るため、
      将来の型追加にも自動的に追随する。
    """
    all_types = set(RecommendationType)
    entry = set(_ENTRY_TYPES)
    exit_ = set(_EXIT_TYPES)
    excluded = set(_EXCLUDED_TYPES)
    undefined = set(_EVALUATION_UNDEFINED_TYPES)

    union = entry | exit_ | excluded | undefined
    unclassified = all_types - union
    assert not unclassified, (
        f"分類されていない RecommendationType があります: "
        f"{sorted(t.value for t in unclassified)}。"
        "ENTRY / EXIT / EXCLUDED / EVALUATION_UNDEFINED のいずれかへ必ず分類すること(#270)"
    )

    unknown = union - all_types
    assert not unknown, f"RecommendationType に無い値が分類に含まれています: {unknown}"

    # 重複が無いこと（ちょうど 1 つに属する）
    for name_a, set_a in (
        ("ENTRY", entry),
        ("EXIT", exit_),
        ("EXCLUDED", excluded),
        ("UNDEFINED", undefined),
    ):
        for name_b, set_b in (
            ("ENTRY", entry),
            ("EXIT", exit_),
            ("EXCLUDED", excluded),
            ("UNDEFINED", undefined),
        ):
            if name_a >= name_b:
                continue
            overlap = set_a & set_b
            assert not overlap, (
                f"{name_a} と {name_b} が重複しています: "
                f"{sorted(t.value for t in overlap)}"
            )


def test_t4b_non_evaluated_types_are_inconclusive() -> None:
    """★ EXCLUDED と UNDEFINED は **どちらも常に INCONCLUSIVE** であること。

    2 つの集合を分けたのは**週次レビューの扱い**を変えるためであり、
    `determine_evaluation_label()` の挙動は**同一**である。
    ここが食い違うと「分類だけの違い」という前提が崩れる。
    """
    for recommendation_type in (*_EXCLUDED_TYPES, *_EVALUATION_UNDEFINED_TYPES):
        label, _ = determine_evaluation_label(
            recommendation_type,
            price_return_pct=-30.0,
            excess_return_pct=None,
            max_drawdown_pct=None,
            config=_CONFIG,
        )
        assert label is EvaluationLabel.INCONCLUSIVE, recommendation_type
        assert is_performance_evaluated_type(recommendation_type) is False


def test_t4c_excluded_set_contains_only_the_approved_type() -> None:
    """★ 除外集合は **URGENT_HOLDING_REVIEW のみ**であること（USER の O-2 承認の範囲）。

    ここへ型を足すことは「その型の評価定義を**不要と決めた**」という
    設計判断であり、#25 が型ごとに判断する。**本 Issue で先取りしない。**
    """
    assert set(_EXCLUDED_TYPES) == {RecommendationType.URGENT_HOLDING_REVIEW}
    assert is_evaluation_excluded_type(RecommendationType.URGENT_HOLDING_REVIEW) is True
    assert is_evaluation_excluded_type(RecommendationType.WATCH_BEFORE_EARNINGS) is False


# =============================================================================
# E) 自動起票が止まること（受入条件 A-4）
# =============================================================================


def _undefined_candidate(recommendation_type: RecommendationType) -> ImprovementCandidate:
    """「評価定義が未整備」の改善候補（週次レビューが生成する形）。"""
    return ImprovementCandidate(
        candidate_id="cand-0001",
        candidate_key="key-0001",
        recommendation_type=recommendation_type,
        rule_version="v1-mvp",
        segment_key=None,
        review_week="2026-W37",
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


def test_t5_excluded_type_does_not_create_a_github_issue() -> None:
    """★ A-4: 除外集合の型は「評価定義が未整備」の Issue を**起票しない**こと。

    起票しても直しようがなく（直すべき未整備ではない）、
    同じ Issue が**毎週立ち続ける**だけになる（#10 / #241 がその実例）。
    """
    eligible = WeeklyImprovementReviewService._is_issue_eligible(
        None,  # type: ignore[arg-type]  # self は使わない
        _undefined_candidate(RecommendationType.URGENT_HOLDING_REVIEW),
    )

    assert eligible is False


@pytest.mark.parametrize(
    "recommendation_type", _EVALUATION_UNDEFINED_TYPES, ids=lambda t: t.value
)
def test_t5b_undefined_types_still_create_issues(
    recommendation_type: RecommendationType,
) -> None:
    """★ 逆側: 「まだ決めていない」型は**従来どおり起票される**こと。

    除外を広げすぎていないことの確認。ここを止めてしまうと
    **未整備が仕様として固定**され、#25 が扱うべき対象が見えなくなる。
    """
    eligible = WeeklyImprovementReviewService._is_issue_eligible(
        None,  # type: ignore[arg-type]
        _undefined_candidate(recommendation_type),
    )

    assert eligible is True


def test_t5c_performance_candidates_are_unaffected() -> None:
    """成績系の候補の起票条件が 1 つも変わらないこと（本変更は未整備系のみに効く）。"""
    candidate = _undefined_candidate(RecommendationType.URGENT_HOLDING_REVIEW).model_copy(
        update={
            "problem_category": PROBLEM_CATEGORY_PERFORMANCE_DEGRADED,
            "reason_codes": ("CRITICAL_DROP",),
        }
    )

    assert (
        WeeklyImprovementReviewService._is_issue_eligible(None, candidate)  # type: ignore[arg-type]
        is True
    )


# =============================================================================
# F) negative check — 修正が戻ったら落ちること
# =============================================================================


def test_t6_sell_like_set_is_not_reused_as_the_evaluation_target() -> None:
    """★ `SELL_LIKE_RECOMMENDATION_TYPES` で置き換えられていないことを固定する。

    あちらは**通知側の概念**であり、利確 2 型と WATCH を含まない。
    置換すると評価対象からそれらが外れ、PROFIT_TAKE_TOO_EARLY が到達不能になる。
    """
    from jstock_advisor.domain.entities.enums import SELL_LIKE_RECOMMENDATION_TYPES

    assert set(_EXIT_TYPES) != set(SELL_LIKE_RECOMMENDATION_TYPES)
    # 置換していたら失われるはずの 3 型が EXIT に残っていること
    for recommendation_type in (
        RecommendationType.PARTIAL_PROFIT_TAKE,
        RecommendationType.FULL_PROFIT_TAKE,
        RecommendationType.WATCH,
    ):
        assert recommendation_type in _EXIT_TYPES
