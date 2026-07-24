"""financial_data_provider の yfinance実装。

quarterly_*系のデータは超大型株を除きほとんど空であることを実測で確認しているため、
年次データ(annual)を基本とする。自己資本比率は貸借対照表の総資産・自己資本から
自前で計算する(EDINETの経営指標サマリーは連結/個別の基準が銘柄により不統一で
あることを実測で確認済みのため、要素を自前で組み合わせる方が信頼できる)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any

import yfinance as yf

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.interfaces.types import (
    FinancialSummary,
    HistoricalValuation,
    QuarterlyFinancials,
)

_PROVIDER_NAME = "yfinance"
_TICKER_SUFFIX = ".T"

# yfinance(Yahoo! Finance)のquoteTypeからsecurity_typeへの対応。
# J-REITはYahoo上でも"EQUITY"と分類されることが多く判別できないため、既知の限界とする。
_QUOTE_TYPE_TO_SECURITY_TYPE = {
    "EQUITY": "STOCK",
    "ETF": "ETF",
}


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


class YFinanceFinancialDataProvider:
    def __init__(self, now: dt.datetime | None = None) -> None:
        self._now = now or dt.datetime.now(dt.UTC)

    def _source(self) -> DataSourceReference:
        return DataSourceReference(provider=_PROVIDER_NAME, fetched_at=self._now)

    def get_financial_summary(self, stock_code: str) -> FinancialSummary | None:
        ticker = yf.Ticker(f"{stock_code}{_TICKER_SUFFIX}")
        try:
            info: dict[str, Any] = ticker.info or {}
        except Exception:  # noqa: BLE001 - 非公式ライブラリのため例外種別を限定できない
            info = {}

        if not info or info.get("regularMarketPrice") is None:
            return None

        equity_ratio_pct = self._compute_equity_ratio_pct(ticker)
        operating_cashflow = self._latest_annual_value(ticker, "cashflow", "Operating Cash Flow")
        operating_income = self._latest_annual_value(ticker, "income_stmt", "Operating Income")
        net_income = self._latest_annual_value(ticker, "income_stmt", "Net Income")

        forecast_eps = _to_decimal(info.get("forwardEps"))
        forecast_bps = _to_decimal(info.get("bookValue"))
        payout_ratio_pct = None
        if info.get("payoutRatio") is not None:
            try:
                payout_ratio_pct = round(float(info["payoutRatio"]) * 100, 2)
            except (TypeError, ValueError):
                payout_ratio_pct = None

        security_type = _QUOTE_TYPE_TO_SECURITY_TYPE.get(str(info.get("quoteType")), "STOCK")

        is_deficit = net_income is not None and net_income < 0
        is_debt_excess = equity_ratio_pct is not None and equity_ratio_pct < 0

        return FinancialSummary(
            stock_code=stock_code,
            stock_name=info.get("longName") or info.get("shortName"),
            fiscal_period_end=self._now.date(),
            security_type=security_type,
            market_segment=None,  # yfinanceは市場区分(プライム/スタンダード等)を提供しない
            industry=info.get("industry"),
            equity_ratio_pct=equity_ratio_pct,
            payout_ratio_pct=payout_ratio_pct,
            operating_cashflow=operating_cashflow,
            net_income=net_income,
            operating_income=operating_income,
            ordinary_income=None,  # 経常利益はJP-GAAP独自概念でyfinanceに対応項目なし
            interest_bearing_debt=None,
            forecast_eps=forecast_eps,
            forecast_bps=forecast_bps,
            is_going_concern_doubt=False,  # yfinanceからは判定不可(既知の限界)
            is_deficit=is_deficit,
            is_debt_excess=is_debt_excess,
            recent_quarters=self._recent_periods(ticker, stock_code),
            source=self._source(),
        )

    def _compute_equity_ratio_pct(self, ticker: yf.Ticker) -> float | None:
        equity = self._latest_value(ticker, "quarterly_balance_sheet", "Stockholders Equity")
        assets = self._latest_value(ticker, "quarterly_balance_sheet", "Total Assets")
        if equity is None or assets is None:
            equity = self._latest_value(ticker, "balance_sheet", "Stockholders Equity")
            assets = self._latest_value(ticker, "balance_sheet", "Total Assets")
        if equity is None or assets is None or assets == 0:
            return None
        return round(float(equity / assets) * 100, 2)

    def _latest_value(self, ticker: yf.Ticker, attr: str, row_name: str) -> Decimal | None:
        try:
            df = getattr(ticker, attr)
        except Exception:  # noqa: BLE001
            return None
        if df is None or df.empty or row_name not in df.index:
            return None
        for value in df.loc[row_name]:
            decimal_value = _to_decimal(value)
            if decimal_value is not None:
                return decimal_value
        return None

    def _latest_annual_value(self, ticker: yf.Ticker, attr: str, row_name: str) -> Decimal | None:
        return self._latest_value(ticker, attr, row_name)

    def _recent_periods(self, ticker: yf.Ticker, stock_code: str) -> list[QuarterlyFinancials]:
        """直近の期別(四半期が取得できない銘柄では年次)営業利益・営業CFの推移。"""
        source = self._source()
        try:
            income_df = ticker.quarterly_income_stmt
            cf_df = ticker.quarterly_cashflow
        except Exception:  # noqa: BLE001
            income_df = None
            cf_df = None

        has_quarterly = (
            income_df is not None and not income_df.empty and "Operating Income" in income_df.index
        )
        if not has_quarterly:
            try:
                income_df = ticker.income_stmt
                cf_df = ticker.cashflow
            except Exception:  # noqa: BLE001
                income_df = None
                cf_df = None

        if income_df is None or income_df.empty or "Operating Income" not in income_df.index:
            return []

        columns = sorted(income_df.columns)
        results: list[QuarterlyFinancials] = []
        for column in columns:
            operating_income = _to_decimal(income_df.loc["Operating Income", column])
            operating_cashflow = None
            if (
                cf_df is not None
                and not cf_df.empty
                and "Operating Cash Flow" in cf_df.index
                and column in cf_df.columns
            ):
                operating_cashflow = _to_decimal(cf_df.loc["Operating Cash Flow", column])
            period_end = column.date() if hasattr(column, "date") else self._now.date()
            results.append(
                QuarterlyFinancials(
                    stock_code=stock_code,
                    quarter_end=period_end,
                    operating_income=operating_income,
                    ordinary_income=None,
                    operating_cashflow=operating_cashflow,
                    source=source,
                )
            )
        return results

    def get_historical_valuation(self, stock_code: str, years: int) -> list[HistoricalValuation]:
        # yfinanceから過去のEPS/BPS時系列を安定して取得する手段が無いため、MVPでは未対応。
        # (PER/PBR中央値による適正価格算出は他方式で代替される)
        return []
