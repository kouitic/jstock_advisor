"""NEAR BUYの継続監視状態(BUY候補裾野拡大機能2026-08)。

`BuyAction.WATCH_FOR_PRICE`のうち、積極監視・毎営業日通知の対象となって
いる銘柄の継続状態を保持する。「通知履歴」とは別物であり、日次通知上限で
その日のLINE通知から除外された銘柄でも、この状態(特にconsecutive_
business_days)は継続して更新される(watch_state_service.py参照)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import WatchType


def build_watch_id(stock_code: str, watch_type: WatchType) -> str:
    return f"{stock_code}:{watch_type.value}"


class WatchState(Entity):
    watch_id: str
    stock_code: str
    watch_type: WatchType

    # 初回開始日。評価不能(DATA_INSUFFICIENT)で連続日数がリセットされても
    # 不変(WatchState全体としては継続しているとみなすため)。
    started_at: dt.date
    # 連続営業日数へ寄与した、直近の一致営業日(Issue #166で定義を明確化)。
    # 非営業日(週末・平日に当たる祝日)や同一営業日の再評価では更新しない。
    # 営業日計算の起点となるため、ここが非営業日になってはならない。
    last_matched_at: dt.date
    # 直近で評価処理を行った日(Issue #166で定義を明確化)。評価不能だった日も、
    # 非営業日に実行した日も含む。判定には使わず、実行事実の記録として持つ。
    last_evaluated_at: dt.date
    # 表示用の連続営業日数。評価不能を挟んだ場合は1へリセットする
    # (near_buy.pyのgapロジック参照)。営業日が1日も経過していない評価では
    # 加算しない(Issue #166)。
    consecutive_business_days: int

    last_current_price: Decimal | None = None
    last_entry_price: Decimal | None = None
    # 監視期間中に観測した最小のrequired_decline_to_entry_pct(最も買い水準に
    # 近づいた実績)。
    best_distance_pct: Decimal | None = None

    ended_at: dt.date | None = None
    # 終了理由: PRICE_OUT_OF_RANGE(継続条件から外れた) / PROMOTED_TO_BUY
    # (BUY家族へ昇格) / TRADE_EVENT(売買イベント検知) / NOT_ATTRACTIVE
    # (BUY候補外へ遷移) / STALE(評価不能が続き安全弁が発動)。
    end_reason: str | None = None
