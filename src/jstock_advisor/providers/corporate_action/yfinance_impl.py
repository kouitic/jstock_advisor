"""corporate_action_provider の yfinance実装。株式分割・併合情報を取得する。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

import yfinance as yf

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import CorporateActionType
from jstock_advisor.interfaces.types import CorporateActionEvent
from jstock_advisor.providers._failure import raise_provider_data_error

_PROVIDER_NAME = "yfinance"
_TICKER_SUFFIX = ".T"


class YFinanceCorporateActionProvider:
    def __init__(self, now: dt.datetime | None = None) -> None:
        self._now = now or dt.datetime.now(dt.UTC)

    def get_corporate_actions(self, stock_code: str, since: dt.date) -> list[CorporateActionEvent]:
        try:
            ticker = yf.Ticker(f"{stock_code}{_TICKER_SUFFIX}")
            splits = ticker.splits
        except Exception as exc:  # noqa: BLE001 - 非公式ライブラリのため例外種別を限定できない
            # Issue #59 Phase B2 / E-4: 取得失敗を空リストへ潰さない。潰すと
            # 「コーポレートアクションなし」と「確認できなかった」が同値になり、
            # 配当のクロスバリデーション補正が無言でスキップされる。
            # 失敗は例外契約で表現できるため、専用のResult型は導入しない。
            raise_provider_data_error(exc, provider_name=_PROVIDER_NAME, operation="splits")

        if splits is None or splits.empty:
            # 応答は成立したがイベントが無い(SUCCESS + no event)。
            return []

        source = DataSourceReference(provider=_PROVIDER_NAME, fetched_at=self._now)
        events: list[CorporateActionEvent] = []
        for index, ratio in splits.items():
            event_date = index.date() if hasattr(index, "date") else index
            if event_date < since:
                continue
            try:
                ratio_decimal = Decimal(str(round(float(ratio), 4)))
            except (InvalidOperation, ValueError, TypeError):
                continue
            event_type = (
                CorporateActionType.SPLIT
                if ratio_decimal >= 1
                else CorporateActionType.REVERSE_SPLIT
            )
            events.append(
                CorporateActionEvent(
                    stock_code=stock_code,
                    event_type=event_type,
                    announced_date=event_date,
                    effective_date=event_date,
                    ratio=ratio_decimal,
                    detail=f"分割比率(新株/旧株): {ratio_decimal}",
                    source=source,
                )
            )
        return events
