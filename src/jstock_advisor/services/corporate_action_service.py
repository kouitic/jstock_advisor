"""企業行動(株式分割等)調整サービス(要求仕様3節)。

株価・平均取得単価・保有株数・EPS・BPS・DPS・配当履歴・適正価格・利確価格・
PER/PBR計算・株主優待の必要株数など、企業行動の影響を受ける全ての値を、
指定した基準日(adjustment_basis_date)へ揃えるための一元的な計算機構。

比較基準日が異なる値同士の計算は、require_matching_basis_datesで明示的に
禁止する(要求仕様3節: 「基準日が異なる値同士の計算を禁止」)。
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.corporate_action import AdjustedDecimal, AdjustedShares
from jstock_advisor.domain.entities.enums import CorporateActionType
from jstock_advisor.interfaces.corporate_action import CorporateActionProvider
from jstock_advisor.interfaces.types import CorporateActionEvent

_RATIO_EVENT_TYPES = frozenset(
    {
        CorporateActionType.SPLIT,
        CorporateActionType.REVERSE_SPLIT,
        CorporateActionType.FREE_ALLOTMENT,
    }
)


class NonIntegerShareAdjustmentError(ValueError):
    """分割比率で株数を調整した結果が整数にならない場合(データ不整合の疑い)。"""


class MismatchedAdjustmentBasisDateError(ValueError):
    """基準日が異なる調整済み値同士を計算・比較しようとした場合。"""


class CorporateActionService:
    def __init__(self, provider: CorporateActionProvider, now: dt.datetime) -> None:
        self._provider = provider
        self._now = now

    def get_effective_events(self, stock_code: str, since: dt.date) -> list[CorporateActionEvent]:
        return self._provider.get_corporate_actions(stock_code, since)

    def is_per_share_adjustment_event(self, event: CorporateActionEvent) -> bool:
        """1株当たり指標(株価・EPS・BPS・DPS・平均取得単価等)の基準日調整対象と
        なるイベントか判定する。cumulative_split_factor()が対象とするSPLIT/
        REVERSE_SPLIT/FREE_ALLOTMENTのみを対象とし、それ以外(MERGER等)は
        ratioを保持していても対象外とする(判定定義をここへ一元化し、呼び出し側が
        独自にratio有無だけで分類しないようにするため)。
        """
        return (
            event.event_type in _RATIO_EVENT_TYPES
            and event.ratio is not None
            and event.effective_date is not None
        )

    def get_ratio_adjustment_events(
        self, events: list[CorporateActionEvent]
    ) -> list[CorporateActionEvent]:
        """与えられたイベント群から、1株当たり指標の調整対象となるものだけを抽出する。"""
        return [e for e in events if self.is_per_share_adjustment_event(e)]

    def cumulative_split_factor(
        self,
        stock_code: str,
        from_date: dt.date,
        to_date: dt.date,
        events: list[CorporateActionEvent] | None = None,
    ) -> Decimal:
        """from_date時点の値をto_date時点の基準へ揃えるための累積分割係数。

        from_dateとto_dateの間(from_date除く、to_date含む、またはその逆順)に
        効力が発生した分割・併合・無償割当の比率を掛け合わせる。
        1:5分割ならratio=5.0であり、この期間をまたぐ値はraw_value/factorで
        新基準に変換する(株数はraw_value*factor)。
        """
        if from_date == to_date:
            return Decimal("1")
        forward = from_date < to_date
        lo, hi = (from_date, to_date) if forward else (to_date, from_date)
        if events is None:
            events = self.get_effective_events(stock_code, lo)
        factor = Decimal("1")
        for event in self.get_ratio_adjustment_events(events):
            if event.effective_date is None or event.ratio is None:
                continue  # is_per_share_adjustment_eventで除外済みのはずだが型上はOptional
            if lo < event.effective_date <= hi:
                factor *= event.ratio
        # from_date > to_date(過去の基準日へ逆方向に調整する)場合、raw_value/factorが
        # 正しく機能するよう係数を反転する(例: 分割後の値を分割前基準へ戻す場合は
        # raw_value * ratio が正しく、raw_value / (1/ratio) と等価にする必要がある)。
        return factor if forward else (Decimal("1") / factor)

    def adjust_price(
        self,
        raw: Decimal,
        stock_code: str,
        value_date: dt.date,
        basis_date: dt.date,
        source: DataSourceReference,
        corporate_action_type: CorporateActionType | None = None,
        corporate_action_effective_date: dt.date | None = None,
        events: list[CorporateActionEvent] | None = None,
    ) -> AdjustedDecimal:
        """株価・EPS・BPS・DPS・平均取得単価等、1株当たり指標の基準日調整。"""
        factor = self.cumulative_split_factor(stock_code, value_date, basis_date, events)
        adjusted = raw / factor if factor != 0 else raw
        return AdjustedDecimal(
            raw_value=raw,
            adjusted_value=adjusted,
            adjustment_factor=factor,
            adjustment_basis_date=basis_date,
            corporate_action_type=corporate_action_type,
            corporate_action_effective_date=corporate_action_effective_date,
            source=source,
            source_timestamp=self._now,
        )

    # EPS/BPS/DPS/平均取得単価は株価と同じ方向(1株当たり指標)で調整するため、
    # adjust_priceの別名として提供する(呼び出し側の意図を明確にする)。
    adjust_per_share_metric = adjust_price

    def adjust_total_metric(
        self,
        raw: Decimal,
        source: DataSourceReference,
        basis_date: dt.date,
    ) -> AdjustedDecimal:
        """営業利益・営業CF等、企業全体の総額指標。

        株式分割・併合は発行済株式数を変えるだけで企業全体の価値・利益総額には
        影響しないため、常にadjustment_factor=1(無調整)。1株当たり指標との
        混同を防ぐため、adjust_priceと明確に別関数として定義する。
        """
        return AdjustedDecimal(
            raw_value=raw,
            adjusted_value=raw,
            adjustment_factor=Decimal("1"),
            adjustment_basis_date=basis_date,
            source=source,
            source_timestamp=self._now,
        )

    def adjust_shares(
        self,
        raw: int,
        stock_code: str,
        value_date: dt.date,
        basis_date: dt.date,
        source: DataSourceReference,
        corporate_action_type: CorporateActionType | None = None,
        corporate_action_effective_date: dt.date | None = None,
        events: list[CorporateActionEvent] | None = None,
    ) -> AdjustedShares:
        """保有株数・株主優待必要株数等の基準日調整。株価とは逆方向(raw*factor)。"""
        factor = self.cumulative_split_factor(stock_code, value_date, basis_date, events)
        raw_decimal = Decimal(raw)
        adjusted_decimal = raw_decimal * factor
        adjusted_int = int(adjusted_decimal.to_integral_value(rounding=ROUND_HALF_UP))
        if adjusted_decimal != adjusted_int:
            raise NonIntegerShareAdjustmentError(
                f"{stock_code}: 株数{raw}を係数{factor}で調整した結果が整数になりません"
                f"({adjusted_decimal})。分割比率データの誤りの可能性があります。"
            )
        return AdjustedShares(
            raw_value=raw,
            adjusted_value=adjusted_int,
            adjustment_factor=factor,
            adjustment_basis_date=basis_date,
            corporate_action_type=corporate_action_type,
            corporate_action_effective_date=corporate_action_effective_date,
            source=source,
            source_timestamp=self._now,
        )

    def require_matching_basis_dates(self, *values: AdjustedDecimal | AdjustedShares) -> None:
        """基準日が異なる調整済み値同士の計算・比較を禁止する。"""
        dates = {v.adjustment_basis_date for v in values}
        if len(dates) > 1:
            raise MismatchedAdjustmentBasisDateError(
                f"基準日が異なる値同士は計算できません: {sorted(dates)}"
            )
