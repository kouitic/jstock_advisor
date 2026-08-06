"""profit_taking_service.pyの決算発表確認待ち抑制(REVIEW_AFTER_EARNINGS)の
結合テスト(コードレビュー対応: 明治ホールディングス(2269)事例)。

決算予定日を経過したが無償データで発表実績を確認できない間、通常の
PARTIAL/FULL_PROFIT_TAKE提案を保留してREVIEW_AFTER_EARNINGSへ切り替えることと、
過去の決算予定日がbusiness_days_between()へ渡されて負の営業日数になり
永久に決算前抑制へ入り込むバグが再発しないことを、実際のモックProvider経由の
build_stock_snapshot()パイプラインで確認する。

MockFinancialDataProvider.get_financial_summary()はfiscal_period_endを常に
評価時刻の日付で返すため、財務データ未更新の状況を再現するには別途ラップして
fiscal_period_endを固定する必要がある。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import PriceWithRationale, SellPriceLevels
from jstock_advisor.domain.entities.enums import AccountType, RecommendationType, TimingAction
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.signals.profit_taking import ProfitTakingResult, UnrealizedPnl
from jstock_advisor.interfaces.types import Disclosure, FinancialSummary
from jstock_advisor.providers.corporate_action.mock_impl import MockCorporateActionProvider
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.dividend_data.mock_impl import MockDividendDataProvider
from jstock_advisor.providers.financial_data.mock_impl import MockFinancialDataProvider
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.shareholder_benefit.mock_impl import MockShareholderBenefitProvider
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_bundle import ProviderBundle

_CONFIG = load_config()
_NOW = dt.datetime(2026, 8, 6, 7, 0, tzinfo=dt.UTC)  # 明治HD事例: 決算予定日(8/5)の翌日
_STALE_EARNINGS_DATE = dt.date(2026, 8, 5)


class _FixedEarningsDateDisclosureProvider:
    """次回決算予定日を固定値で返すフェイク(get_disclosuresは委譲元へ委譲)。"""

    def __init__(self, delegate: object, next_earnings_date: dt.date | None) -> None:
        self._delegate = delegate
        self._next_earnings_date = next_earnings_date

    def get_disclosures(self, stock_code: str, since: dt.date) -> list[Disclosure]:
        return self._delegate.get_disclosures(stock_code, since)  # type: ignore[attr-defined]

    def get_next_earnings_date(self, stock_code: str) -> dt.date | None:
        return self._next_earnings_date


class _FixedFiscalPeriodEndFinancialDataProvider:
    """fiscal_period_endだけを固定値で上書きするフェイク(他は委譲元へ委譲)。"""

    def __init__(self, delegate: object, fiscal_period_end: dt.date) -> None:
        self._delegate = delegate
        self._fiscal_period_end = fiscal_period_end

    def get_financial_summary(self, stock_code: str) -> FinancialSummary | None:
        summary = self._delegate.get_financial_summary(stock_code)  # type: ignore[attr-defined]
        if summary is None:
            return None
        return summary.model_copy(update={"fiscal_period_end": self._fiscal_period_end})

    def get_historical_valuation(self, stock_code: str, years: int) -> list[object]:
        return self._delegate.get_historical_valuation(stock_code, years)  # type: ignore[attr-defined]

    def get_cashflow_decomposition(self, stock_code: str) -> object | None:
        return self._delegate.get_cashflow_decomposition(stock_code)  # type: ignore[attr-defined]


def _providers(
    next_earnings_date: dt.date | None, fiscal_period_end: dt.date, now: dt.datetime = _NOW
) -> ProviderBundle:
    base = ProviderBundle(
        market_data=MockMarketDataProvider(now=now),
        financial_data=MockFinancialDataProvider(now=now),
        dividend_data=MockDividendDataProvider(now=now),
        shareholder_benefit=MockShareholderBenefitProvider(now=now),
        disclosure=MockDisclosureProvider(now=now),
        corporate_action=MockCorporateActionProvider(),
    )
    return dataclasses.replace(
        base,
        disclosure=_FixedEarningsDateDisclosureProvider(base.disclosure, next_earnings_date),
        financial_data=_FixedFiscalPeriodEndFinancialDataProvider(
            base.financial_data, fiscal_period_end
        ),
    )


def _holding(stock_code: str) -> Holding:
    return Holding(
        stock_code=stock_code,
        stock_name="テスト銘柄",
        shares=100,
        average_purchase_price=Decimal("4000"),
        total_purchase_amount=Decimal("400000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _canned_result(recommendation_type: RecommendationType) -> ProfitTakingResult:
    """evaluate_profit_taking()の結果をモックのファンダメンタルズに依存せず
    固定するためのフェイク結果(REVIEW_AFTER_EARNINGS分岐だけを検証したいため)。
    """
    return ProfitTakingResult(
        recommendation_type=recommendation_type,
        fundamental_action=recommendation_type,
        timing_action=TimingAction.NEUTRAL,
        final_action=recommendation_type,
        triggered_reasons=["含み益率が一部利確基準を超過"],
        mitigating_factors_applied=[],
        hold_reasons=[],
        sell_prices=SellPriceLevels(
            recommended_limit_price=PriceWithRationale(price=Decimal("5000"), rationale="test")
        ),
        pnl=UnrealizedPnl(
            unrealized_pnl=Decimal("100000"),
            unrealized_pnl_pct=25.0,
            total_return_including_income=Decimal("105000"),
            total_return_pct=26.25,
        ),
        independent_condition_count=1,
        fair_value_used_as_sole_strong_basis=False,
        current_price_vs_neutral_fair_value_pct=10.0,
        current_price_vs_bull_fair_value_pct=5.0,
    )


@pytest.mark.parametrize("stock_code", ["2914", "9861", "8136", "8306"])
def test_stale_earnings_date_with_unreflected_financials_becomes_review_after_earnings(
    monkeypatch: pytest.MonkeyPatch, stock_code: str
) -> None:
    """明治HD回帰(財務データ未更新ケース): 決算予定日を経過し、fiscal_period_end
    が想定報告ラグより前(=財務データ未更新)のとき、PARTIAL_PROFIT_TAKEは
    REVIEW_AFTER_EARNINGSへ切り替わり、sell_pricesは空になる。銘柄コードを
    変えても同じ結果になることを確認し、コード固有の分岐が無いことを示す。
    """
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE),
    )
    providers = _providers(_STALE_EARNINGS_DATE, dt.date(2026, 3, 31))
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding(stock_code), _NOW)

    assert outcome.data_error is None
    assert outcome.recommendation is not None
    rec = outcome.recommendation
    assert rec.recommendation_type == RecommendationType.REVIEW_AFTER_EARNINGS
    assert rec.sell_prices == SellPriceLevels()
    assert rec.next_earnings_date is None
    # 過去日をbusiness_days_between()へ渡さないため、負の営業日数にならない
    # (次回決算日がNoneのため決算前抑制の分岐自体に入らない)
    assert rec.business_days_to_earnings is None
    assert any("決算発表予定日を経過" in c for c in rec.next_review_conditions)


def test_stale_earnings_date_with_reflected_financials_keeps_original_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """明治HD回帰(財務データ更新済みケース): fiscal_period_endが想定報告ラグ以内
    まで進んでいれば、財務データが最新決算を反映したとみなし、通常の
    PARTIAL_PROFIT_TAKE判定をそのまま使う(REVIEW_AFTER_EARNINGSへ切り替えない)。
    """
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE),
    )
    # 決算予定日8/5から60日以内(想定報告ラグ既定値)のfiscal_period_end
    providers = _providers(_STALE_EARNINGS_DATE, dt.date(2026, 6, 30))
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.data_error is None
    assert outcome.recommendation is not None
    rec = outcome.recommendation
    assert rec.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE
    assert rec.sell_prices.recommended_limit_price is not None


def test_far_past_earnings_date_does_not_trigger_before_earnings_suppression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """business_days_between()の負数バグの回帰: 決算予定日がかなり過去でも
    (STALE_PAST_DATEによりnext_earnings_date=Noneとなるため)、決算直前の
    WATCH_BEFORE_EARNINGS抑制には入らない。
    """
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: _canned_result(RecommendationType.WATCH),
    )
    far_past = _NOW.date() - dt.timedelta(days=30)
    providers = _providers(far_past, dt.date(2026, 3, 31))
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.data_error is None
    assert outcome.recommendation is not None
    rec = outcome.recommendation
    assert rec.recommendation_type == RecommendationType.WATCH
    assert rec.business_days_to_earnings is None


def test_future_earnings_date_within_suppression_window_still_suppresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既存の決算直前抑制(REVIEW_BEFORE_EARNINGS)は今回の変更で退行していない
    ことの確認(未来の確定日はCONFIRMEDのままbusiness_days_betweenへ渡る)。
    """
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE),
    )
    near_future = _NOW.date() + dt.timedelta(days=1)
    providers = _providers(near_future, dt.date(2026, 3, 31))
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.data_error is None
    assert outcome.recommendation is not None
    rec = outcome.recommendation
    assert rec.recommendation_type == RecommendationType.REVIEW_BEFORE_EARNINGS
    assert rec.business_days_to_earnings is not None
    assert rec.business_days_to_earnings >= 0
