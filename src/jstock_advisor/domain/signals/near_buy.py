"""NEAR BUY(BuyAction.WATCH_FOR_PRICEのうち積極監視対象)の開始/継続条件を
判定する純粋関数群(BUY候補裾野拡大機能2026-08)。

`decide_buy_action()`は変更せず、その結果(BuyAction)・company_quality_score・
required_decline_to_entry_pctを入力として、NEAR BUYへ格上げすべきかだけを
判定する上位レイヤーの関数群。状態(WatchState)の永続化は
services/watch_state_service.pyが担当する(このモジュールは純粋関数のみ)。
"""

from __future__ import annotations

from decimal import Decimal

from jstock_advisor.config.models import NearBuyConfig
from jstock_advisor.domain.entities.enums import BuyAction


def meets_near_buy_start_conditions(
    buy_action: BuyAction,
    company_quality_score: float | None,
    required_decline_to_entry_pct: Decimal | None,
    config: NearBuyConfig,
) -> bool:
    if buy_action != BuyAction.WATCH_FOR_PRICE:
        return False
    if company_quality_score is None or company_quality_score < config.min_company_quality_score:
        return False
    if required_decline_to_entry_pct is None:
        return False
    return float(required_decline_to_entry_pct) <= config.start_required_decline_pct


def meets_near_buy_continue_conditions(
    buy_action: BuyAction,
    required_decline_to_entry_pct: Decimal | None,
    config: NearBuyConfig,
) -> bool:
    """開始条件よりゆるいcontinue_required_decline_pctを使うヒステリシスにより、
    start/continueの境界付近での通知フラッピングを防ぐ(company_quality_scoreは
    継続判定では再チェックしない。開始時点で一定の企業品質を確認済みのため)。
    """
    if buy_action != BuyAction.WATCH_FOR_PRICE:
        return False
    if required_decline_to_entry_pct is None:
        return False
    return float(required_decline_to_entry_pct) <= config.continue_required_decline_pct


def compute_consecutive_business_days(
    business_days_since_last_matched: int,
    previous_consecutive_business_days: int,
) -> int:
    """連続営業日数を更新する(Issue #166)。

    business_days_since_last_matched は前回last_matched_atからtodayまでの営業日数
    (BusinessCalendar.business_days_betweenの戻り値をそのまま渡す)。同関数は
    「起点の翌日からtodayまで」を数えるため、値の意味は次のとおり。

    - 0 … 前回一致日から営業日が1日も経過していない。同一営業日の再評価、
          週末、平日に当たる祝日(schedulerはMON-FRIで発火するが東証は休場)が
          すべてここへ入る。**営業日は進んでいないため据え置く。**
    - 1 … 真に連続する営業日。インクリメントする。
    - 2以上 … 評価不能日を挟んだ。1へリセットする(WatchState自体は維持するが
          表示上の連続日数はリセットする)。

    Issue #166 以前は 0 と 1 をまとめて「<= 1」でインクリメントしていたため、
    営業日が1日も経過していないのに加算されていた(非営業日の実行・同一日の
    複数回実行のいずれでも発生し、Productionで実測された)。この関数は連続
    「評価回数」ではなく連続「営業日数」を表すため、0での加算は契約違反である。
    """
    if business_days_since_last_matched <= 0:
        return previous_consecutive_business_days
    if business_days_since_last_matched == 1:
        return previous_consecutive_business_days + 1
    return 1


def compute_best_distance_pct(
    previous_best: Decimal | None, required_decline_to_entry_pct: Decimal
) -> Decimal:
    if previous_best is None:
        return required_decline_to_entry_pct
    return min(previous_best, required_decline_to_entry_pct)


def evaluate_stale(
    business_days_since_last_matched: int, max_stale_business_days: int
) -> bool:
    """評価不能(DATA_INSUFFICIENT)が続き、安全弁(max_stale_business_days)を
    超えた場合にTrueを返す(WatchState強制終了の判定に使う)。"""
    return business_days_since_last_matched > max_stale_business_days


__all__ = [
    "compute_best_distance_pct",
    "compute_consecutive_business_days",
    "evaluate_stale",
    "meets_near_buy_continue_conditions",
    "meets_near_buy_start_conditions",
]
