import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import (
    CorporateActionType,
    DividendComparisonOutcome,
    DividendPeriodEndBasis,
    RecordDateUnknownReason,
)
from jstock_advisor.interfaces.types import CorporateActionEvent
from jstock_advisor.providers.dividend_data.yfinance_impl import YFinanceDividendDataProvider
from jstock_advisor.services.corporate_action_service import CorporateActionService

_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)


class _FakeTicker:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.info = {"regularMarketPrice": 1000, "dividendRate": 50}
        self.dividends: dict[object, float] = {}


class _NoOpCorporateActionProvider:
    """株式分割等が一切発生しないことを明示するためのフェイク
    (コードレビュー修正2: corporate_action_serviceを必須依存にしたことに伴い、
    分割調整が不要なテストでも暗黙のNoneではなく明示的な「分割無し」を表現する)。"""

    def get_corporate_actions(self, stock_code: str, since: dt.date) -> list[CorporateActionEvent]:
        return []


def _no_op_corporate_action_service() -> CorporateActionService:
    return CorporateActionService(_NoOpCorporateActionProvider(), now=_NOW)


def test_corporate_action_service_is_a_required_dependency() -> None:
    """【コードレビュー修正2】corporate_action_serviceを渡さずに
    YFinanceDividendDataProviderを生成しようとするとTypeErrorになること。

    分割調整を一切行っていない生値を「基準日へ正規化済み」とAPI上誤って
    表現してしまう余地を、実行時のNoneチェックではなく型(必須引数)で
    構造的に無くすための確認(推奨案A)。"""
    with pytest.raises(TypeError):
        YFinanceDividendDataProvider(now=_NOW)  # type: ignore[call-arg]


def test_get_dividend_info_marks_record_date_as_permanently_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jstock_advisor.providers.dividend_data.yfinance_impl as module

    monkeypatch.setattr(module.yf, "Ticker", _FakeTicker)
    provider = YFinanceDividendDataProvider(
        now=_NOW, corporate_action_service=_no_op_corporate_action_service()
    )

    info = provider.get_dividend_info("7203")

    assert info is not None
    assert info.dividend_record_date is None
    assert info.dividend_ex_date is None
    assert info.dividend_record_date_unknown_reason == RecordDateUnknownReason.DATA_PROVIDER_MISSING


def test_inferred_decrease_never_sets_official_dividend_cut_announced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # yfinance単独の年間配当合計比較から推測される減少は、あくまでinferredであり、
    # official_dividend_cut_announced(一次情報での公式発表)には絶対にしない(要求仕様§11・§12)。
    import jstock_advisor.providers.dividend_data.yfinance_impl as module

    class _TickerWithDividends(_FakeTicker):
        def __init__(self, symbol: str) -> None:
            super().__init__(symbol)
            self.info = {"regularMarketPrice": 1000, "dividendRate": 50}
            self.dividends = {
                dt.datetime(2024, 6, 27): 50.0,
                dt.datetime(2024, 12, 27): 50.0,
                dt.datetime(2025, 6, 27): 30.0,
                dt.datetime(2025, 12, 29): 30.0,
            }

    monkeypatch.setattr(module.yf, "Ticker", _TickerWithDividends)
    provider = YFinanceDividendDataProvider(
        now=_NOW, corporate_action_service=_no_op_corporate_action_service()
    )

    info = provider.get_dividend_info("4631")

    assert info is not None
    assert info.inferred_dividend_decrease is True
    assert info.official_dividend_cut_announced is False
    assert info.dividend_breakdown_confirmed is False


class _FixedCorporateActionProvider:
    def __init__(self, events: list[CorporateActionEvent]) -> None:
        self._events = events

    def get_corporate_actions(self, stock_code: str, since: dt.date) -> list[CorporateActionEvent]:
        return [e for e in self._events if e.stock_code == stock_code]


def test_real_dividend_cut_is_not_hidden_by_double_split_adjustment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_sum_by_calendar_yearが各支払いを既にself._now.date()基準へ調整済みの値を、
    classify_dividend_change呼び出し時にさらに調整してしまう回帰テスト(修正前は
    分割係数が二重に適用され、実際の減配が見かけ上の増配として隠れていた)。

    FY2025の配当実績(分割前基準)は25円×2回=50円。2026年4月1日に1:5分割が
    発生しているため、分割後基準では50/5=10円に相当する。予想配当(分割後基準、
    yfinanceの現在値なので既に分割後)が9円なら、これは約10%の実質減配であり、
    増配(DIVIDEND_INCREASE)と誤判定されてはならない。
    """
    import jstock_advisor.providers.dividend_data.yfinance_impl as module

    class _TickerWithSplitStraddlingDividends(_FakeTicker):
        def __init__(self, symbol: str) -> None:
            super().__init__(symbol)
            self.info = {"regularMarketPrice": 1000, "dividendRate": 9}
            self.dividends = {
                dt.datetime(2025, 6, 27): 25.0,
                dt.datetime(2025, 12, 29): 25.0,
            }

    monkeypatch.setattr(module.yf, "Ticker", _TickerWithSplitStraddlingDividends)

    split_event = CorporateActionEvent(
        stock_code="5401",
        event_type=CorporateActionType.SPLIT,
        announced_date=dt.date(2026, 4, 1),
        effective_date=dt.date(2026, 4, 1),
        ratio=Decimal("5"),
        source=DataSourceReference(provider="test", fetched_at=_NOW),
    )
    corporate_action = CorporateActionService(
        _FixedCorporateActionProvider([split_event]), now=_NOW
    )
    provider = YFinanceDividendDataProvider(now=_NOW, corporate_action_service=corporate_action)

    info = provider.get_dividend_info("5401")

    assert info is not None
    # 修正前は二重調整により src=2円(10円をさらに5で割った値)となり、
    # forecast(9円) > src(2円)でDIVIDEND_INCREASEに誤判定されていた。
    assert info.dividend_comparison_outcome == DividendComparisonOutcome.FORECAST_DIVIDEND_CUT
    assert info.inferred_dividend_decrease is True


def test_fiscal_year_aggregation_separates_periods_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3月決算企業(fiscal_year_end_month=3)で、暦年ではなく決算期
    (FY2025: 2024/04-2025/03、FY2026: 2025/04-2026/03)に正しく分離されること
    (配当データクロスバリデーション根本修正の中心テスト)。"""
    import jstock_advisor.providers.dividend_data.yfinance_impl as module

    class _TickerWithFiscalYearStraddlingDividends(_FakeTicker):
        def __init__(self, symbol: str) -> None:
            super().__init__(symbol)
            self.info = {"regularMarketPrice": 1000, "dividendRate": 20}
            self.dividends = {
                dt.datetime(2025, 3, 27): 30.0,  # FY2025(2024/04-2025/03)の期末配当
                dt.datetime(2025, 9, 26): 15.0,  # FY2026(2025/04-2026/03)の中間配当
                dt.datetime(2026, 3, 26): 18.0,  # FY2026の期末配当
            }

    monkeypatch.setattr(module.yf, "Ticker", _TickerWithFiscalYearStraddlingDividends)
    provider = YFinanceDividendDataProvider(
        now=_NOW, corporate_action_service=_no_op_corporate_action_service()
    )

    info = provider.get_dividend_info("4193", fiscal_year_end_month=3)

    assert info is not None
    by_end = {a.period_end: a for a in info.annual_dividend_actuals}
    assert by_end[dt.date(2025, 3, 31)].raw_dividend_per_share == Decimal("30")
    # 暦年集計なら2025年=30+15=45になってしまうが、決算期単位ではFY2026は33のみ
    assert by_end[dt.date(2026, 3, 31)].raw_dividend_per_share == Decimal("33")
    assert info.actual_annual_dividend_per_share == Decimal("33")
    assert info.calendar_year_fallback_used is False


def test_december_fiscal_year_end_matches_calendar_year_aggregation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """12月決算企業(fiscal_year_end_month=12)で従来通りの挙動(暦年集計と
    同じ結果)になること。"""
    import jstock_advisor.providers.dividend_data.yfinance_impl as module

    class _TickerWithCalendarYearDividends(_FakeTicker):
        def __init__(self, symbol: str) -> None:
            super().__init__(symbol)
            self.info = {"regularMarketPrice": 1000, "dividendRate": 20}
            self.dividends = {
                dt.datetime(2024, 6, 27): 10.0,
                dt.datetime(2024, 12, 27): 10.0,
                dt.datetime(2025, 6, 27): 12.0,
                dt.datetime(2025, 12, 29): 12.0,
            }

    monkeypatch.setattr(module.yf, "Ticker", _TickerWithCalendarYearDividends)
    provider = YFinanceDividendDataProvider(
        now=_NOW, corporate_action_service=_no_op_corporate_action_service()
    )

    info = provider.get_dividend_info("2914", fiscal_year_end_month=12)

    assert info is not None
    by_end = {a.period_end: a for a in info.annual_dividend_actuals}
    assert by_end[dt.date(2024, 12, 31)].raw_dividend_per_share == Decimal("20")
    assert by_end[dt.date(2025, 12, 31)].raw_dividend_per_share == Decimal("24")
    assert info.actual_annual_dividend_per_share == Decimal("24")


def test_interim_and_final_dividend_not_merged_by_calendar_year(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5195パターン回帰: 前期末配当と当期中間配当が「同じ暦年」というだけの
    理由で誤って合算されないこと。"""
    import jstock_advisor.providers.dividend_data.yfinance_impl as module

    class _TickerWithSameCalendarYearButDifferentFiscalYears(_FakeTicker):
        def __init__(self, symbol: str) -> None:
            super().__init__(symbol)
            self.info = {"regularMarketPrice": 1000, "dividendRate": 20}
            self.dividends = {
                dt.datetime(2025, 2, 1): 50.0,  # FY2025(3月決算)の期末配当
                dt.datetime(2025, 8, 1): 30.0,  # FY2026(3月決算)の中間配当。同じ暦年だが別決算期
            }

    monkeypatch.setattr(module.yf, "Ticker", _TickerWithSameCalendarYearButDifferentFiscalYears)
    provider = YFinanceDividendDataProvider(
        now=_NOW, corporate_action_service=_no_op_corporate_action_service()
    )

    info = provider.get_dividend_info("5195", fiscal_year_end_month=3)

    assert info is not None
    by_end = {a.period_end: a for a in info.annual_dividend_actuals}
    # 暦年集計なら2025年=50+30=80になってしまうが、決算期単位では両者は別期
    assert by_end[dt.date(2025, 3, 31)].raw_dividend_per_share == Decimal("50")
    assert by_end[dt.date(2026, 3, 31)].raw_dividend_per_share == Decimal("30")


def test_none_fiscal_year_end_month_falls_back_to_calendar_year(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fiscal_year_end_month=None(未指定)の場合、従来の暦年集計と完全に
    同じ結果を返し、calendar_year_fallback_used=Trueが設定されること。"""
    import jstock_advisor.providers.dividend_data.yfinance_impl as module

    class _TickerWithDividends(_FakeTicker):
        def __init__(self, symbol: str) -> None:
            super().__init__(symbol)
            self.info = {"regularMarketPrice": 1000, "dividendRate": 20}
            self.dividends = {
                dt.datetime(2024, 6, 27): 10.0,
                dt.datetime(2024, 12, 27): 10.0,
                dt.datetime(2025, 6, 27): 12.0,
                dt.datetime(2025, 12, 29): 12.0,
            }

    monkeypatch.setattr(module.yf, "Ticker", _TickerWithDividends)
    provider_without = YFinanceDividendDataProvider(
        now=_NOW, corporate_action_service=_no_op_corporate_action_service()
    )
    provider_with_12 = YFinanceDividendDataProvider(
        now=_NOW, corporate_action_service=_no_op_corporate_action_service()
    )

    info_without = provider_without.get_dividend_info("2914")
    info_with_12 = provider_with_12.get_dividend_info("2914", fiscal_year_end_month=12)

    assert info_without is not None
    assert info_with_12 is not None
    assert info_without.calendar_year_fallback_used is True
    assert info_with_12.calendar_year_fallback_used is False
    assert (
        info_without.actual_annual_dividend_per_share
        == info_with_12.actual_annual_dividend_per_share
    )
    without_by_end = {
        a.period_end: a.raw_dividend_per_share for a in info_without.annual_dividend_actuals
    }
    with_12_by_end = {
        a.period_end: a.raw_dividend_per_share for a in info_with_12.annual_dividend_actuals
    }
    assert without_by_end == with_12_by_end


def test_annual_dividend_actual_period_boundaries_are_correct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """annual_dividend_actualsの各period_start/period_end/period_end_basisが
    正しいこと。"""
    import jstock_advisor.providers.dividend_data.yfinance_impl as module

    class _TickerWithDividends(_FakeTicker):
        def __init__(self, symbol: str) -> None:
            super().__init__(symbol)
            self.info = {"regularMarketPrice": 1000, "dividendRate": 20}
            self.dividends = {dt.datetime(2025, 9, 26): 15.0, dt.datetime(2026, 3, 26): 18.0}

    monkeypatch.setattr(module.yf, "Ticker", _TickerWithDividends)
    provider = YFinanceDividendDataProvider(
        now=_NOW, corporate_action_service=_no_op_corporate_action_service()
    )

    info = provider.get_dividend_info("4193", fiscal_year_end_month=3)

    assert info is not None
    assert len(info.annual_dividend_actuals) == 1
    actual = info.annual_dividend_actuals[0]
    assert actual.period_start == dt.date(2025, 4, 1)
    assert actual.period_end == dt.date(2026, 3, 31)
    assert actual.period_end_basis == DividendPeriodEndBasis.DERIVED_FROM_FISCAL_YEAR_END
    assert actual.period_start_is_estimated is False


def test_raw_and_normalized_values_differ_without_double_adjustment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """raw_dividend_per_share(正規化前)とnormalized_dividend_per_share(正規化後)が
    分割発生年度で正しく異なり、二重補正が起きないこと。"""
    import jstock_advisor.providers.dividend_data.yfinance_impl as module

    class _TickerWithSplitStraddlingDividends(_FakeTicker):
        def __init__(self, symbol: str) -> None:
            super().__init__(symbol)
            self.info = {"regularMarketPrice": 1000, "dividendRate": 9}
            self.dividends = {
                dt.datetime(2025, 6, 27): 25.0,
                dt.datetime(2025, 12, 29): 25.0,
            }

    monkeypatch.setattr(module.yf, "Ticker", _TickerWithSplitStraddlingDividends)

    split_event = CorporateActionEvent(
        stock_code="5401",
        event_type=CorporateActionType.SPLIT,
        announced_date=dt.date(2026, 4, 1),
        effective_date=dt.date(2026, 4, 1),
        ratio=Decimal("5"),
        source=DataSourceReference(provider="test", fetched_at=_NOW),
    )
    corporate_action = CorporateActionService(
        _FixedCorporateActionProvider([split_event]), now=_NOW
    )
    provider = YFinanceDividendDataProvider(now=_NOW, corporate_action_service=corporate_action)

    info = provider.get_dividend_info("5401", fiscal_year_end_month=12)

    assert info is not None
    actual = info.annual_dividend_actuals[-1]
    assert actual.raw_dividend_per_share == Decimal("50")  # 分割調整前の生値(25+25)
    assert actual.normalized_dividend_per_share == Decimal("10")  # 分割後基準(50/5)
    assert actual.normalization_basis_date is not None
