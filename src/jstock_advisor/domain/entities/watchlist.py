"""ウォッチリスト(要求仕様6節)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import Priority, WatchlistRegistrationSource


class WatchlistItem(Entity):
    stock_code: str
    stock_name: str | None = None
    reason: str | None = None
    desired_total_yield_pct: float | None = None
    desired_buy_price: Decimal | None = None
    benefit_interest: bool = False
    priority: Priority = Priority.MEDIUM
    notify_enabled: bool = True
    memo: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime

    # --- ウォッチリスト自動追加機能で追加。既存レコードには存在しないため、
    # 後方互換のためデフォルト値(手動登録)を持たせる ---
    registration_source: WatchlistRegistrationSource = WatchlistRegistrationSource.MANUAL
    registration_policy: str | None = None
    # --- 運用ハードニング第3弾2節で追加。AUTO_SCREENING追加時の発行元batch_id。
    # add_if_new()成功後・finalize側のrepository_results永続化前に障害が起きた
    # 場合、再試行時にadd_if_new()がFalseを返しても、この値が今回のbatch_idと
    # 一致すれば「このバッチ自身が過去の試行で既に追加していた」と判定でき、
    # 誤ってSKIPPED_EXISTING(＝通知対象漏れ)にしないための復元キー。既存レコード
    # には存在しないためNoneとして読み込める(手動登録・旧バージョンのレコード) ---
    registration_batch_id: str | None = None
