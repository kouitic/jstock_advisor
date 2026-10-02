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
from enum import StrEnum

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


class BasisDateConsistency(StrEnum):
    """価格側・財務指標側の基準日整合性の判定結果(要求仕様3節、Issue #698)。"""

    CONSISTENT = "CONSISTENT"
    UNDETERMINED = "UNDETERMINED"
    DETECTED = "DETECTED"


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

    def classify_basis_date_consistency(
        self,
        stock_code: str,
        price_basis_date: dt.date,
        fundamental_basis_date: dt.date,
        events: list[CorporateActionEvent] | None = None,
    ) -> BasisDateConsistency:
        """価格とEPS/BPS/DPS等の1株当たり指標が、同一の分割・併合・無償割当の
        基準の上で組み合わされているかを判定する(Issue #698)。

        価格(market_data provider経由)は問い合わせ時点に関わらず常に最新の
        分割基準へ遡及調整されることを実測で確認済みだが、財務指標
        (EPS/BPS/DPS)が同様に遡及調整されるかはprovider・取得時点によって
        保証されない。

        本関数はprice_basis_dateとfundamental_basis_dateの間(どちらが古いか
        を問わない)に、1株当たり指標の調整対象イベント(SPLIT/REVERSE_SPLIT/
        FREE_ALLOTMENT)の効力発生日が**取得できた範囲で**1件でも存在するか
        どうかだけで判定する(要求仕様12節: 取得できない情報を推測で補完
        しない)。★比率の積ではなく件数で判定する: 例えば2:1分割と1:2併合が
        同一窓に入ると比率の積は1になるが、財務指標側が片方のイベントだけ
        遡及調整済みという状態はこの積だけでは区別できない。fail-safeとして
        安全な側(件数ベース)に倒すため、積ではなく該当イベントの有無で判定する
        (レビュー指摘。issuecomment-5957234551 S-2)。

        provider側で日付・比率を解析できなかった行は`get_effective_events()`
        から事実上欠落するため(既存の`cumulative_split_factor()`と7消費箇所が
        共有する既存挙動。本関数のLOCK_LEVEL_1契約では変更しない)、実在する
        分割・併合が取得漏れの場合はfactorが1のままとなりCONSISTENTを誤って
        返しうる。この残存リスクは#698 PR-2/PR-3の設計で引き続き検討する
        (レビュー指摘。issuecomment-5957234551 S-1)。

        該当イベントが(取得できた範囲で)1件も無ければCONSISTENT(両者の
        基準がそもそもずれる余地が無い)。1件でもあればUNDETERMINED(財務
        指標側が遡及調整済みかどうかを本関数だけでは確認できないため、
        安全側へ倒す。USER決定OD-3: 不整合の有無を判定できない場合も抑止
        対象とする)。

        DETECTED(実際の不整合を確認できた)は本関数では返さない。確定検出には
        実測値の比較(算出結果が分割比率で説明できる水準まで乖離している等、
        日付情報だけでは判定できない根拠)が必要であり、呼び出し側が本関数の
        UNDETERMINED結果と実測値の異常検知を組み合わせて最終的な判定を行う
        (#698 PR-2/PR-3で実装予定)。
        """
        if price_basis_date == fundamental_basis_date:
            return BasisDateConsistency.CONSISTENT
        lo, hi = sorted((price_basis_date, fundamental_basis_date))
        if events is None:
            events = self.get_effective_events(stock_code, lo)
        for event in self.get_ratio_adjustment_events(events):
            if event.effective_date is None:
                continue  # is_per_share_adjustment_eventで除外済みのはずだが型上はOptional
            if lo < event.effective_date <= hi:
                return BasisDateConsistency.UNDETERMINED
        return BasisDateConsistency.CONSISTENT

    def require_matching_basis_dates(self, *values: AdjustedDecimal | AdjustedShares) -> None:
        """基準日が異なる調整済み値同士の計算・比較を禁止する。"""
        dates = {v.adjustment_basis_date for v in values}
        if len(dates) > 1:
            raise MismatchedAdjustmentBasisDateError(
                f"基準日が異なる値同士は計算できません: {sorted(dates)}"
            )
