"""corporate_action_provider の統合実装(要求仕様2節)。

yfinanceの自動取得(SPLIT/REVERSE_SPLIT)と、手動登録レジストリ(無償割当・
スピンオフ・銘柄コード変更・合併・上場廃止・配当基準変更)を統合して返す。
(stock_code, event_type, effective_date)が重複するイベントは1件に
まとめる(手動登録側を優先する。運用者が一次情報を確認した値のため)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.interfaces.corporate_action import CorporateActionProvider
from jstock_advisor.interfaces.types import CorporateActionEvent


def _dedup_key(event: CorporateActionEvent) -> tuple[str, str, dt.date | None]:
    return (event.stock_code, event.event_type.value, event.effective_date)


class MergedCorporateActionProvider:
    def __init__(
        self,
        auto_provider: CorporateActionProvider,
        manual_provider: CorporateActionProvider,
    ) -> None:
        self._auto = auto_provider
        self._manual = manual_provider

    def get_corporate_actions(self, stock_code: str, since: dt.date) -> list[CorporateActionEvent]:
        manual_events = self._manual.get_corporate_actions(stock_code, since)
        manual_keys = {_dedup_key(e) for e in manual_events}
        auto_events = [
            e
            for e in self._auto.get_corporate_actions(stock_code, since)
            if _dedup_key(e) not in manual_keys
        ]
        return sorted(
            [*manual_events, *auto_events],
            key=lambda e: e.effective_date or dt.date.min,
        )
