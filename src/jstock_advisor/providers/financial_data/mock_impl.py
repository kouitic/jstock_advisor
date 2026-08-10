"""financial_data_provider のモック実装(開発・テスト用の合成データ)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import RecentPeriodsSource, ValuationBasis
from jstock_advisor.interfaces.types import (
    CashflowDecomposition,
    EarningsSurpriseRecord,
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

    def get_earnings_surprise_history(self, stock_code: str) -> list[EarningsSurpriseRecord]:
        """判定精度向上機能Phase C用の合成データ。実データ実装
        (yfinance_impl.py)は直近4四半期分のみ返すため、同じ件数に揃える。
        quarter_endはget_financial_summary()のrecent_quartersと同一の期末日
        規約(90日刻み)で算出し、突合可能にする。eps_actual/eps_estimateは
        既存のordinary_incomeプロファイルから合成し、四半期ごとに交互で
        プラス/マイナスのサプライズが生じるようにする(テストデータの多様性
        確保が目的であり、実在企業の実績値ではない)。"""
        profile = MOCK_STOCKS.get(stock_code)
        if profile is None or not profile.quarters:
            return []

        source = self._source()
        recent = profile.quarters[-4:]
        offset = len(profile.quarters) - len(recent)
        latest_ordinary_income = profile.quarters[-1].ordinary_income
        results: list[EarningsSurpriseRecord] = []
        for i, q in enumerate(recent):
            quarter_index = offset + i
            quarter_end = self._now.date() - dt.timedelta(
                days=90 * (len(profile.quarters) - quarter_index)
            )
            if latest_ordinary_income == 0:
                eps_actual = None
            else:
                eps_actual = (
                    profile.forecast_eps / 4 * (q.ordinary_income / latest_ordinary_income)
                )
            eps_estimate = None
            surprise_pct = None
            if eps_actual is not None:
                # 偶数四半期はプラスサプライズ(実績>予想)、奇数四半期はマイナス
                # サプライズ(実績<予想)となるよう合成する。
                ratio = Decimal("0.92") if quarter_index % 2 == 0 else Decimal("1.05")
                eps_estimate = eps_actual * ratio
                if eps_estimate != 0:
                    surprise_pct = float((eps_actual - eps_estimate) / eps_estimate)
            results.append(
                EarningsSurpriseRecord(
                    stock_code=stock_code,
                    quarter_end=quarter_end,
                    eps_actual=eps_actual,
                    eps_estimate=eps_estimate,
                    surprise_pct=surprise_pct,
                    source=source,
                )
            )
        return results
