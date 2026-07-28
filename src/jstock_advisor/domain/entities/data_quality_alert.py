"""データ品質アラート(要求仕様3節・11節・17節・18節)。

分割整合性チェック・異常値検知・判定/価格整合性検証のいずれかで問題が
検出された場合、通常の売買推奨通知の代わりにこのアラートを送信する。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.base import ImmutableSnapshot


class DataQualityAlert(ImmutableSnapshot):
    stock_code: str
    detected_at: dt.datetime
    process: str  # 検出元処理(例: "profit_taking", "sell_signal")
    contradictions: list[str]  # 検出した矛盾・異常
    suppressed_values: dict[str, str]  # 使用を停止した値(フィールド名 -> 値の文字列表現)
    recalculation_result: str | None = None  # 再計算結果(可能な場合)
    action_required: bool = True  # 対応要否
