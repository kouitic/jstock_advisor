"""financial_data_provider の yfinance実装。

quarterly_*系のデータは超大型株を除きほとんど空であることを実測で確認しているため、
年次データ(annual)を基本とする。自己資本比率は貸借対照表の総資産・自己資本から
自前で計算する(EDINETの経営指標サマリーは連結/個別の基準が銘柄により不統一で
あることを実測で確認済みのため、要素を自前で組み合わせる方が信頼できる)。

社名はyfinanceからは英語名(longName/shortName)しか取得できないため(実測確認済み)、
EDINET提出書類のfilerName(日本語の正式社名)が利用できる場合はそちらを優先する。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import yfinance as yf

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import RecentPeriodsSource, ValuationBasis
from jstock_advisor.infrastructure.edinet.client import EdinetClient
from jstock_advisor.infrastructure.edinet.document_finder import (
    EdinetFilingCacheRepository,
    find_latest_filings,
)
from jstock_advisor.infrastructure.local_repository.stock_name_override_repository import (
    StockNameOverrideRepository,
)
from jstock_advisor.interfaces.types import (
    CashflowDecomposition,
    FinancialSummary,
    HistoricalValuation,
    QuarterlyFinancials,
)

_CORPORATE_SUFFIX_PATTERN = re.compile(r"株式会社")


def _strip_corporate_suffix(name: str) -> str:
    return _CORPORATE_SUFFIX_PATTERN.sub("", name).strip()


_PROVIDER_NAME = "yfinance"
_TICKER_SUFFIX = ".T"

# yfinance(Yahoo! Finance)のquoteTypeからsecurity_typeへの対応。
# J-REITはYahoo上でも"EQUITY"と分類されることが多く判別できないため、既知の限界とする。
_QUOTE_TYPE_TO_SECURITY_TYPE = {
    "EQUITY": "STOCK",
    "ETF": "ETF",
}


@dataclass(frozen=True)
class _RecentPeriodsResult:
    """_recent_periods()の戻り値(由来精緻化対応)。"""

    periods: list[QuarterlyFinancials]
    source: RecentPeriodsSource


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
    def __init__(
        self,
        now: dt.datetime | None = None,
        edinet_client: EdinetClient | None = None,
        edinet_cache_repository: EdinetFilingCacheRepository | None = None,
        stock_name_override_repository: StockNameOverrideRepository | None = None,
    ) -> None:
        self._now = now or dt.datetime.now(dt.UTC)
        self._edinet_client = edinet_client
        self._edinet_cache_repo = edinet_cache_repository
        # 銘柄名の手動オーバーライド(2026-07 BUYパイプライン第2次修正・要求仕様19節)。
        # EDINET filerNameが取得できない、または表記の見直しが必要な銘柄のみ、
        # 運用者が手動登録した日本語社名を最優先で使う。
        self._stock_name_override_repo = (
            stock_name_override_repository or StockNameOverrideRepository()
        )

    def _source(self) -> DataSourceReference:
        return DataSourceReference(provider=_PROVIDER_NAME, fetched_at=self._now)

    def _resolve_japanese_stock_name(self, stock_code: str) -> str | None:
        override = self._stock_name_override_repo.get(stock_code)
        if override is not None:
            return override
        if self._edinet_client is None or self._edinet_cache_repo is None:
            return None
        if not self._edinet_client.is_configured:
            return None
        filing = find_latest_filings(
            self._edinet_client, self._edinet_cache_repo, stock_code, self._now
        )
        if filing is None or filing.filer_name is None:
            return None
        return _strip_corporate_suffix(filing.filer_name)

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
        capital_expenditure = self._latest_annual_value(ticker, "cashflow", "Capital Expenditure")
        operating_income = self._latest_annual_value(ticker, "income_stmt", "Operating Income")
        net_income = self._latest_annual_value(ticker, "income_stmt", "Net Income")

        forecast_eps = _to_decimal(info.get("forwardEps"))
        forecast_bps = _to_decimal(info.get("bookValue"))
        # 判定精度向上機能Phase B(Historical Valuation Score)専用。既存の
        # forecast_eps(forwardEps、FORWARD basis)とは別に、実績(trailing)EPSを
        # 取得する(get_historical_valuation()が返す過去PER系列はtrailing basis
        # であり、現在値も同一basisで比較する必要があるため)。
        trailing_eps = _to_decimal(info.get("trailingEps"))
        shares_outstanding = _to_decimal(info.get("sharesOutstanding"))
        payout_ratio_pct = None
        if info.get("payoutRatio") is not None:
            try:
                payout_ratio_pct = round(float(info["payoutRatio"]) * 100, 2)
            except (TypeError, ValueError):
                payout_ratio_pct = None

        security_type = _QUOTE_TYPE_TO_SECURITY_TYPE.get(str(info.get("quoteType")), "STOCK")

        is_deficit = net_income is not None and net_income < 0
        is_debt_excess = equity_ratio_pct is not None and equity_ratio_pct < 0

        stock_name = (
            self._resolve_japanese_stock_name(stock_code)
            or info.get("longName")
            or info.get("shortName")
        )

        # デプロイ前対応: 年次決算期末を取得できない場合、データ取得日時を代替値
        # として使わない(Noneのまま。決算反映確認の誤判定を防止)。
        fiscal_period_end = self._latest_annual_period_end(ticker)
        fiscal_year_end_month = self._fiscal_year_end_month(info, ticker)
        recent_periods_result = self._recent_periods(ticker, stock_code)

        return FinancialSummary(
            stock_code=stock_code,
            stock_name=stock_name,
            fiscal_period_end=fiscal_period_end,
            fiscal_year_end_month=fiscal_year_end_month,
            security_type=security_type,
            market_segment=None,  # yfinanceは市場区分(プライム/スタンダード等)を提供しない
            industry=info.get("industry"),
            sector=info.get("sector"),
            equity_ratio_pct=equity_ratio_pct,
            payout_ratio_pct=payout_ratio_pct,
            operating_cashflow=operating_cashflow,
            capital_expenditure=capital_expenditure,
            net_income=net_income,
            operating_income=operating_income,
            ordinary_income=None,  # 経常利益はJP-GAAP独自概念でyfinanceに対応項目なし
            interest_bearing_debt=None,
            forecast_eps=forecast_eps,
            forecast_bps=forecast_bps,
            trailing_eps=trailing_eps,
            shares_outstanding=shares_outstanding,
            is_going_concern_doubt=False,  # yfinanceからは判定不可(既知の限界)
            is_deficit=is_deficit,
            is_debt_excess=is_debt_excess,
            recent_quarters=recent_periods_result.periods,
            recent_periods_source=recent_periods_result.source,
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

    def _latest_annual_period_end(self, ticker: yf.Ticker) -> dt.date | None:
        """直近の年次決算(income_stmt)の対象期間末日(2026-07仕様レビュー対応)。

        以前はfiscal_period_endにデータ取得日時(self._now)をそのまま設定しており、
        データ鮮度判定が常に「最新」と誤判定される不具合があったため、実際の
        開示期間末日を取得する。
        """
        try:
            df = ticker.income_stmt
        except Exception:  # noqa: BLE001
            return None
        if df is None or df.empty or len(df.columns) == 0:
            return None
        try:
            period_end: dt.date = df.columns[0].date()
        except (AttributeError, TypeError):
            return None
        return period_end

    def _fiscal_year_end_month(self, info: dict[str, Any], ticker: yf.Ticker) -> int | None:
        """企業の正式な決算期末月(2026-07仕様レビュー対応)。

        yfinanceのlastFiscalYearEnd(直近の確定済み決算期末、unixタイムスタンプ)を
        優先的に使う。取得できない場合のみ、年次決算(income_stmt)の最新列の月を
        代替として使う(いずれも直近四半期末ではなく年次決算の期末という点で
        配当基準日推定の目的に沿う)。
        """
        raw = info.get("lastFiscalYearEnd")
        if raw is not None:
            try:
                return dt.datetime.fromtimestamp(float(raw), dt.UTC).date().month
            except (TypeError, ValueError, OSError):
                pass
        period_end = self._latest_annual_period_end(ticker)
        return period_end.month if period_end is not None else None

    def _recent_periods(self, ticker: yf.Ticker, stock_code: str) -> _RecentPeriodsResult:
        """直近の期別(四半期が取得できない銘柄では年次)営業利益・営業CFの推移。

        戻り値のsourceは、実際にquarterly_income_stmtから生成できたか、
        年次income_stmtへフォールバックしたかを区別する(由来精緻化対応)。
        """
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
        recent_periods_source = RecentPeriodsSource.QUARTERLY
        if not has_quarterly:
            recent_periods_source = RecentPeriodsSource.ANNUAL_FALLBACK
            try:
                income_df = ticker.income_stmt
                cf_df = ticker.cashflow
            except Exception:  # noqa: BLE001
                income_df = None
                cf_df = None

        if income_df is None or income_df.empty or "Operating Income" not in income_df.index:
            return _RecentPeriodsResult(periods=[], source=RecentPeriodsSource.UNAVAILABLE)

        columns = sorted(income_df.columns)
        results: list[QuarterlyFinancials] = []
        for column in columns:
            # デプロイ前対応: 列から実際の期間末日を取得できない場合はこの期を
            # スキップする(データ取得日時を代替の期末日として使わない)。
            if not hasattr(column, "date"):
                continue
            operating_income = _to_decimal(income_df.loc["Operating Income", column])
            operating_cashflow = None
            if (
                cf_df is not None
                and not cf_df.empty
                and "Operating Cash Flow" in cf_df.index
                and column in cf_df.columns
            ):
                operating_cashflow = _to_decimal(cf_df.loc["Operating Cash Flow", column])
            period_end = column.date()
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
        if not results:
            return _RecentPeriodsResult(periods=[], source=RecentPeriodsSource.UNAVAILABLE)
        return _RecentPeriodsResult(periods=results, source=recent_periods_source)

    def get_historical_valuation(self, stock_code: str, years: int) -> list[HistoricalValuation]:
        """過去(通常4年分程度、yfinanceが提供する年次決算の範囲)のEPS/BPS/株価から
        PER/PBRを算出する(要求仕様8節)。

        PER=株価/EPS、PBR=株価/BPSはいずれも比率であり、株式分割が発生しても
        分子(株価)・分母(EPS/BPS)が同じ比率で変化するため、この比率自体は
        分割の影響を受けない(分割調整不要)。ただし、BPSは各期の発行済株式数の
        履歴が安定して取得できないため、現在の発行済株式数で近似する
        (自己株買い・増資等で株式数が変動している場合は誤差が生じうる既知の制約。
        confidenceをMEDIUM上限とする理由となる)。株式分割の未検知・一時的
        特殊要因(減損等)による異常値は、無償データから確実には検知できないため
        v1では能動的な検知を行わない(判定精度向上機能Phase Bコードレビュー対応、
        domain/signals/historical_valuation.pyの外れ値フィルタで部分的に緩和する)。

        eps/equity(bps算出元)はいずれもticker.income_stmt/balance_sheetの
        既に確定した決算実績値であり、予想値ではない(TRAILING basis)。
        判定精度向上機能Phase Bコードレビュー対応で、per_basis/pbr_basisを
        明示的にTRAILINGとして記録する(現在値と比較する側がbasis一致を検証する)。
        """
        ticker = yf.Ticker(f"{stock_code}{_TICKER_SUFFIX}")
        try:
            income_df = ticker.income_stmt
            balance_df = ticker.balance_sheet
        except Exception:  # noqa: BLE001
            return []

        if income_df is None or income_df.empty:
            return []
        eps_row = next(
            (r for r in ("Diluted EPS", "Basic EPS") if r in income_df.index), None
        )
        if eps_row is None or balance_df is None or balance_df.empty:
            return []
        if "Stockholders Equity" not in balance_df.index:
            return []

        try:
            info = ticker.info or {}
            shares_outstanding = _to_decimal(info.get("sharesOutstanding"))
        except Exception:  # noqa: BLE001
            shares_outstanding = None

        start = self._now.date() - dt.timedelta(days=365 * years + 30)
        try:
            price_history = ticker.history(start=start, end=self._now.date(), interval="1d")
        except Exception:  # noqa: BLE001
            price_history = None

        source = self._source()
        results: list[HistoricalValuation] = []
        for column in sorted(income_df.columns):
            period_end = column.date() if hasattr(column, "date") else None
            if period_end is None or period_end < start:
                continue
            eps = _to_decimal(income_df.loc[eps_row, column])
            equity = (
                _to_decimal(balance_df.loc["Stockholders Equity", column])
                if column in balance_df.columns
                else None
            )
            bps = (
                equity / shares_outstanding
                if equity is not None and shares_outstanding is not None and shares_outstanding > 0
                else None
            )
            price = self._nearest_price(price_history, period_end)
            per = price / eps if price is not None and eps is not None and eps > 0 else None
            pbr = price / bps if price is not None and bps is not None and bps > 0 else None
            if per is None and pbr is None:
                continue
            results.append(
                HistoricalValuation(
                    stock_code=stock_code,
                    date=period_end,
                    eps=eps,
                    bps=bps,
                    price=price,
                    per=per,
                    pbr=pbr,
                    per_basis=ValuationBasis.TRAILING,
                    pbr_basis=ValuationBasis.TRAILING,
                    source=source,
                )
            )
        return results

    @staticmethod
    def _nearest_price(price_history: Any, target_date: dt.date) -> Decimal | None:
        if price_history is None or price_history.empty:
            return None
        best_price: Decimal | None = None
        best_diff: int | None = None
        for index, row in price_history.iterrows():
            index_date = index.date() if hasattr(index, "date") else None
            if index_date is None:
                continue
            diff = abs((index_date - target_date).days)
            if diff > 14:
                continue
            if best_diff is None or diff < best_diff:
                close = _to_decimal(row.get("Close"))
                if close is not None:
                    best_price = close
                    best_diff = diff
        return best_price

    def get_cashflow_decomposition(self, stock_code: str) -> CashflowDecomposition | None:
        """直近期の営業キャッシュフロー要因分解(要求仕様4節)。

        yfinanceのcashflow/income_stmt行の有無は銘柄により大きく異なる
        (特に大型株以外は多くの項目が欠損する)ため、pretax_income(判定の
        基準値)すら取得できない場合はNoneを返す。「一過性要因」に対応する
        単独の行はyfinanceに存在しないため、one_time_itemsは常にNone
        (既知の制約、捏造しない)。
        """
        ticker = yf.Ticker(f"{stock_code}{_TICKER_SUFFIX}")
        pretax_income = self._latest_value(ticker, "income_stmt", "Pretax Income")
        if pretax_income is None:
            return None

        period_end = self._now.date()
        try:
            income_df = ticker.income_stmt
            if income_df is not None and not income_df.empty:
                latest_column = sorted(income_df.columns)[-1]
                if hasattr(latest_column, "date"):
                    period_end = latest_column.date()
        except Exception:  # noqa: BLE001
            pass

        return CashflowDecomposition(
            stock_code=stock_code,
            period_end=period_end,
            pretax_income=pretax_income,
            depreciation_amortization=self._latest_value(
                ticker, "cashflow", "Depreciation And Amortization"
            ),
            receivables_change=self._latest_value(ticker, "cashflow", "Change In Receivables"),
            inventory_change=self._latest_value(ticker, "cashflow", "Change In Inventory"),
            payables_change=self._latest_value(
                ticker, "cashflow", "Change In Payables And Accrued Expense"
            ),
            tax_paid=self._latest_value(
                ticker, "cashflow", "Income Tax Paid Supplemental Data"
            ),
            one_time_items=None,  # yfinanceに対応する行が無い(既知の制約)
            ma_related_items=self._latest_value(ticker, "cashflow", "Purchase Of Business"),
            other_working_capital=self._latest_value(
                ticker, "cashflow", "Change In Working Capital"
            ),
            source=self._source(),
        )
