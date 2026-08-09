"""financial_data_provider のモック実装(開発・テスト用の合成データ)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import RecentPeriodsSource, ValuationBasis
from jstock_advisor.interfaces.types import (
    CashflowDecomposition,
    FinancialSummary,
    HistoricalValuation,
    QuarterlyFinancials,
)
from jstock_advisor.providers.mock_fixtures import MOCK_STOCKS, get_price_volume_series

_PROVIDER_NAME = "mock_financial_data"


class MockFinancialDataProvider:
    def __init__(self, now: dt.datetime | None = None) -> None:
        self._now = now or dt.datetime.now(dt.UTC)

    def _source(self) -> DataSourceReference:
        return DataSourceReference(provider=_PROVIDER_NAME, fetched_at=self._now)

    def get_financial_summary(self, stock_code: str) -> FinancialSummary | None:
        profile = MOCK_STOCKS.get(stock_code)
        if profile is None:
            return None

        source = self._source()
        quarters = [
            QuarterlyFinancials(
                stock_code=stock_code,
                quarter_end=self._now.date() - dt.timedelta(days=90 * (len(profile.quarters) - i)),
                operating_income=q.operating_income,
                ordinary_income=q.ordinary_income,
                operating_cashflow=q.operating_cashflow,
                source=source,
            )
            for i, q in enumerate(profile.quarters)
        ]
        latest_quarter = profile.quarters[-1] if profile.quarters else None
        net_income = latest_quarter.ordinary_income * Decimal("0.7") if latest_quarter else None

        return FinancialSummary(
            stock_code=stock_code,
            stock_name=profile.stock_name,
            fiscal_period_end=self._now.date(),
            market_segment=profile.market_segment,
            industry=profile.industry,
            equity_ratio_pct=profile.equity_ratio_pct,
            payout_ratio_pct=profile.payout_ratio_pct,
            operating_cashflow=latest_quarter.operating_cashflow if latest_quarter else None,
            net_income=net_income,
            operating_income=latest_quarter.operating_income if latest_quarter else None,
            ordinary_income=latest_quarter.ordinary_income if latest_quarter else None,
            interest_bearing_debt=None,
            forecast_eps=profile.forecast_eps,
            forecast_bps=profile.forecast_bps,
            # モックはforecast/trailingを区別する実データを持たないため、
            # 判定精度向上機能Phase B用にforecast_epsをそのまま流用する
            # (基本設計上の簡略化。実データ(yfinance_impl.py)ではtrailingEpsを
            # 別途取得する)。
            trailing_eps=profile.forecast_eps,
            is_going_concern_doubt=False,
            is_deficit=False,
            is_debt_excess=False,
            recent_quarters=quarters,
            recent_periods_source=(
                RecentPeriodsSource.QUARTERLY if quarters else RecentPeriodsSource.UNAVAILABLE
            ),
            source=source,
        )

    def get_historical_valuation(self, stock_code: str, years: int) -> list[HistoricalValuation]:
        profile = MOCK_STOCKS.get(stock_code)
        series = get_price_volume_series(stock_code)
        if profile is None or not series:
            return []

        source = self._source()
        start = self._now.date() - dt.timedelta(days=365 * years)
        result: list[HistoricalValuation] = []
        for d in sorted(series):
            if d < start or d > self._now.date():
                continue
            close, _volume = series[d]
            price = Decimal(str(close))
            eps = profile.forecast_eps
            bps = profile.forecast_bps
            per = price / eps if eps and eps > 0 else None
            pbr = price / bps if bps and bps > 0 else None
            result.append(
                HistoricalValuation(
                    stock_code=stock_code,
                    date=d,
                    eps=eps,
                    bps=bps,
                    price=price,
                    per=per,
                    pbr=pbr,
                    per_basis=ValuationBasis.TRAILING,
                    pbr_basis=ValuationBasis.TRAILING,
                    # look-ahead bias防止(コードレビュー対応): 実データ実装と同様、
                    # source.fetched_atを保守的なavailable_atとして使う。
                    available_at=source.fetched_at,
                    # モックは固定フィクスチャ値であり、実データの株式数近似という
                    # 制約自体をモデル化していないための簡略化(Falseのまま)。
                    pbr_is_approximate=False,
                    source=source,
                )
            )
        return result

    def get_cashflow_decomposition(self, stock_code: str) -> CashflowDecomposition | None:
        # モックフィクスチャは営業CFの要因分解に必要な内訳データ(運転資本の
        # 各項目等)を保持していないため、未対応(None)とする。
        return None
