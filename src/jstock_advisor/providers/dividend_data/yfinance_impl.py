"""dividend_data_provider の yfinance実装。

yfinanceは配当の「権利確定日」を提供しないため(取得できるのは配当イベント日のみ、
支払日そのものではない可能性がある。yfinance仕様の断定はできていないため中立的な
呼称に留める)、dividend_record_datesは常に空リストとする(推測で補完しない)。

年間配当実績は、企業の正式な決算期末月(fiscal_year_end_month、呼び出し元が
FinancialSummary.fiscal_year_end_monthを渡す)に基づき決算期単位で集計する
(配当データクロスバリデーション根本修正)。fiscal_year_end_monthが渡されない
場合のみ、従来通り暦年(1月〜12月)集計へフォールバックする
(DividendInfo.calendar_year_fallback_used=Trueで明示)。
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import yfinance as yf

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import (
    DividendComparisonOutcome,
    DividendPeriodEndBasis,
    RecordDateUnknownReason,
)
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.domain.signals.dividend_cut_analysis import classify_dividend_change
from jstock_advisor.interfaces.types import AnnualDividendActual, DividendInfo
from jstock_advisor.services.corporate_action_service import CorporateActionService

_PROVIDER_NAME = "yfinance"
_TICKER_SUFFIX = ".T"


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    try:
        return Decimal(str(round(f, 2)))
    except InvalidOperation:
        return None


def _fiscal_year_label(event_date: dt.date, fiscal_year_end_month: int) -> int:
    """配当イベント日が属する決算期のラベル(その決算期が終了する年)を返す。

    例: fiscal_year_end_month=3(3月決算)の場合、2025-06-27はFY2026
    (2025/04-2026/03)に属する。fiscal_year_end_month=12(暦年フォールバック)の
    場合、この関数は常にevent_date.yearを返し、従来の暦年集計と数学的に完全一致する。
    """
    return event_date.year if event_date.month <= fiscal_year_end_month else event_date.year + 1


def _fiscal_year_period(fy_label: int, fiscal_year_end_month: int) -> tuple[dt.date, dt.date]:
    """決算期ラベルから(期首, 期末)の日付を算出する。"""
    end_day = calendar.monthrange(fy_label, fiscal_year_end_month)[1]
    end_date = dt.date(fy_label, fiscal_year_end_month, end_day)
    start_month = fiscal_year_end_month - 11
    start_year = fy_label
    if start_month <= 0:
        start_month += 12
        start_year -= 1
    return dt.date(start_year, start_month, 1), end_date


@dataclass(frozen=True)
class _FiscalYearTotal:
    raw: float
    normalized: float


class YFinanceDividendDataProvider:
    def __init__(
        self,
        now: dt.datetime | None = None,
        corporate_action_service: CorporateActionService | None = None,
    ) -> None:
        """corporate_action_serviceを渡すと、決算期集計の前に各配当イベントを
        評価日(JST)基準へ分割調整する(渡さない場合は従来通り無調整の生値を集計する)。
        分割が決算期の途中で発生した場合、無調整の集計は分割前後の額面が
        同一決算期内に混在するため、誤った減配判定を招く(根本原因レポート原因1)。
        この個々のイベント単位での正規化は、決算期の集計方法(暦年→決算期単位)を
        変更した後も変わらず維持する(配当データクロスバリデーション根本修正)。
        """
        self._now = now or dt.datetime.now(dt.UTC)
        self._corporate_action = corporate_action_service

    def _source(self) -> DataSourceReference:
        return DataSourceReference(provider=_PROVIDER_NAME, fetched_at=self._now)

    def get_dividend_info(
        self, stock_code: str, fiscal_year_end_month: int | None = None
    ) -> DividendInfo | None:
        ticker = yf.Ticker(f"{stock_code}{_TICKER_SUFFIX}")
        try:
            info: dict[str, Any] = ticker.info or {}
        except Exception:  # noqa: BLE001 - 非公式ライブラリのため例外種別を限定できない
            info = {}

        if not info or info.get("regularMarketPrice") is None:
            return None

        try:
            dividends = ticker.dividends
        except Exception:  # noqa: BLE001
            dividends = None

        calendar_year_fallback_used = fiscal_year_end_month is None
        effective_fiscal_year_end_month = fiscal_year_end_month or 12
        evaluation_date = evaluation_date_jst(self._now)

        yearly_totals = self._sum_by_fiscal_year(
            dividends, stock_code, effective_fiscal_year_end_month
        )
        actual_annual = None
        previous_annual = None
        actual_fiscal_year: int | None = None
        consecutive_increase_years = None
        annual_dividend_actuals: list[AnnualDividendActual] = []

        if yearly_totals:
            years_sorted = sorted(yearly_totals.keys())
            # 確定済み(=決算期末日が評価日より前)の決算期のみを対象とする。当日中の
            # 決算期末は「その日が終わったとは限らない」ため確定済みに含めない。
            complete_years = [
                y
                for y in years_sorted
                if _fiscal_year_period(y, effective_fiscal_year_end_month)[1] < evaluation_date
            ]
            for y in complete_years:
                period_start, period_end = _fiscal_year_period(y, effective_fiscal_year_end_month)
                totals = yearly_totals[y]
                annual_dividend_actuals.append(
                    AnnualDividendActual(
                        period_end=period_end,
                        period_end_basis=DividendPeriodEndBasis.DERIVED_FROM_FISCAL_YEAR_END,
                        period_start=period_start,
                        period_start_is_estimated=False,
                        raw_dividend_per_share=Decimal(str(round(totals.raw, 2))),
                        normalized_dividend_per_share=Decimal(str(round(totals.normalized, 2))),
                        normalization_basis_date=evaluation_date,
                    )
                )
            if complete_years:
                actual_fiscal_year = complete_years[-1]
                actual_annual = Decimal(str(round(yearly_totals[actual_fiscal_year].normalized, 2)))
                if len(complete_years) >= 2:
                    previous_annual = Decimal(
                        str(round(yearly_totals[complete_years[-2]].normalized, 2))
                    )
                consecutive_increase_years = self._count_consecutive_increases(
                    [yearly_totals[y].normalized for y in complete_years]
                )

        forecast_annual = _to_decimal(info.get("dividendRate"))
        if forecast_annual is None:
            forecast_annual = _to_decimal(info.get("trailingAnnualDividendRate"))

        source = self._source()
        is_dividend_omission_announced = (
            forecast_annual is not None and forecast_annual == 0 and actual_annual is not None
            and actual_annual > 0
        )

        comparison_outcome = None
        comparison_source_period = None
        comparison_target_period = None
        cut_pct = None
        is_dividend_cut_announced = False
        if not is_dividend_omission_announced and actual_fiscal_year is not None:
            comparison = classify_dividend_change(
                stock_code=stock_code,
                source_dps_raw=actual_annual,
                # actual_annualは_sum_by_fiscal_yearの時点で各支払いごとに
                # 既にevaluation_date基準へ分割調整済みのため、ここでの
                # source_dateもevaluation_dateを渡す(実際の支払年の期末日を
                # 渡すと、既に調整済みの値へさらに分割係数を掛けてしまい、
                # 実際の減配が見かけ上の増配として隠れるバグになる)。
                source_date=evaluation_date,
                source_period_label=f"{actual_fiscal_year}年(実績)",
                target_dps_raw=forecast_annual,
                target_date=evaluation_date,
                target_period_label="予想(現在)",
                is_forecast_comparison=True,
                source_ref=source,
                corporate_action_service=self._corporate_action,
            )
            comparison_outcome = comparison.outcome
            comparison_source_period = comparison.comparison_source_period
            comparison_target_period = comparison.comparison_target_period
            cut_pct = comparison.cut_pct
            is_dividend_cut_announced = comparison.outcome in (
                DividendComparisonOutcome.FORECAST_DIVIDEND_CUT,
                DividendComparisonOutcome.ACTUAL_DIVIDEND_CUT,
            )

        return DividendInfo(
            stock_code=stock_code,
            fiscal_year=str(actual_fiscal_year) if actual_fiscal_year is not None else str(
                self._now.year
            ),
            forecast_annual_dividend_per_share=forecast_annual,
            actual_annual_dividend_per_share=actual_annual,
            previous_fiscal_year_dividend_per_share=previous_annual,
            # is_dividend_cut_announcedは後方互換のため従来通りyfinanceの数値比較結果を
            # 保持する(買い候補スクリーニングの弱いフィルタとして使用: screening/rules.py)。
            # SELL/URGENT_REVIEWの根拠にはofficial_dividend_cut_announced(常にFalse)を
            # 使うこと(要求仕様§11・§12: yfinance単独の推測を「公式発表」扱いしない)。
            is_dividend_cut_announced=is_dividend_cut_announced,
            is_dividend_omission_announced=is_dividend_omission_announced,
            is_progressive_or_doe_policy=False,  # yfinanceからは判定不可(既知の限界)
            dividend_policy_note=None,
            dividend_record_dates=[],  # yfinanceは権利確定日を取得不可
            consecutive_dividend_increase_years=consecutive_increase_years,
            source=source,
            comparison_source_fiscal_year=comparison_source_period,
            comparison_target_fiscal_year=comparison_target_period,
            dividend_comparison_outcome=comparison_outcome,
            dividend_cut_pct=cut_pct,
            dividend_record_date=None,
            dividend_ex_date=None,
            # yfinanceは権利確定日・権利落ち日いずれも提供しない(恒久的な制約)
            dividend_record_date_unknown_reason=RecordDateUnknownReason.DATA_PROVIDER_MISSING,
            # yfinanceは配当の内訳(普通/特別/記念/臨時)を提供しないため常に未確定
            # (恒久的な制約)。年間合計の単純比較から推測される減少シグナルのみ
            # inferred_dividend_decreaseとして保持し、official_dividend_cut_announcedは
            # 一次情報での確認が取れるまで常にFalseとする。
            dividend_breakdown_confirmed=False,
            official_dividend_cut_announced=False,
            inferred_dividend_decrease=is_dividend_cut_announced,
            total_dividend_decrease_detected=is_dividend_cut_announced,
            # yfinanceの予想配当率=0のみからの推測であり、公式な無配転落発表の
            # 確認ではない(恒久的な制約。一次情報ソースが無い)。
            official_dividend_omission_announced=False,
            inferred_dividend_omission=is_dividend_omission_announced,
            annual_dividend_actuals=annual_dividend_actuals,
            calendar_year_fallback_used=calendar_year_fallback_used,
        )

    def _sum_by_fiscal_year(
        self, dividends: Any, stock_code: str, fiscal_year_end_month: int
    ) -> dict[int, _FiscalYearTotal]:
        if dividends is None or len(dividends) == 0:
            return {}
        basis_date = evaluation_date_jst(self._now)
        source = self._source()
        raw_totals: dict[int, float] = {}
        normalized_totals: dict[int, float] = {}
        for index, value in dividends.items():
            dividend_event_date = index.date() if hasattr(index, "date") else None
            if dividend_event_date is None:
                continue
            fy_label = _fiscal_year_label(dividend_event_date, fiscal_year_end_month)
            amount = float(value)
            raw_totals[fy_label] = raw_totals.get(fy_label, 0.0) + amount

            normalized_amount = amount
            if self._corporate_action is not None:
                adjusted = self._corporate_action.adjust_per_share_metric(
                    Decimal(str(amount)), stock_code, dividend_event_date, basis_date, source
                )
                normalized_amount = float(adjusted.adjusted_value)
            normalized_totals[fy_label] = normalized_totals.get(fy_label, 0.0) + normalized_amount
        return {
            fy: _FiscalYearTotal(raw=raw_totals[fy], normalized=normalized_totals[fy])
            for fy in raw_totals
        }

    @staticmethod
    def _count_consecutive_increases(values_oldest_to_newest: list[float]) -> int:
        count = 0
        for i in range(len(values_oldest_to_newest) - 1, 0, -1):
            if values_oldest_to_newest[i] > values_oldest_to_newest[i - 1]:
                count += 1
            else:
                break
        return count
