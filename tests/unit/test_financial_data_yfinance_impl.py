import datetime as dt
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from jstock_advisor.domain.entities.enums import RecentPeriodsSource
from jstock_advisor.infrastructure.edinet.document_finder import EdinetFilingCacheRepository
from jstock_advisor.infrastructure.local_repository.stock_name_override_repository import (
    StockNameOverrideRepository,
)
from jstock_advisor.providers.financial_data.yfinance_impl import (
    YFinanceFinancialDataProvider,
    _strip_corporate_suffix,
)

_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("株式会社サンリオ", "サンリオ"),
        ("新明和工業株式会社", "新明和工業"),
        ("トヨタ自動車", "トヨタ自動車"),
    ],
)
def test_strip_corporate_suffix(raw: str, expected: str) -> None:
    assert _strip_corporate_suffix(raw) == expected


class _NotConfiguredDocumentSource:
    is_configured = False

    def list_documents(self, date: dt.date) -> list[dict[str, object]]:
        return []

    def download_document_zip(self, doc_id: str) -> bytes | None:
        return None


def test_resolve_japanese_stock_name_returns_none_without_edinet_document_source(
    tmp_path: Path,
) -> None:
    provider = YFinanceFinancialDataProvider(
        now=_NOW,
        stock_name_override_repository=StockNameOverrideRepository(store_dir=tmp_path),
    )
    assert provider._resolve_japanese_stock_name("8136") is None  # noqa: SLF001


def test_resolve_japanese_stock_name_returns_none_when_edinet_not_configured(
    tmp_path: Path,
) -> None:
    provider = YFinanceFinancialDataProvider(
        now=_NOW,
        edinet_document_source=_NotConfiguredDocumentSource(),  # type: ignore[arg-type]
        edinet_cache_repository=EdinetFilingCacheRepository(store_dir=tmp_path),
        stock_name_override_repository=StockNameOverrideRepository(store_dir=tmp_path),
    )
    assert provider._resolve_japanese_stock_name("8136") is None  # noqa: SLF001


def test_resolve_japanese_stock_name_prefers_manual_override_over_edinet(
    tmp_path: Path,
) -> None:
    # BUYパイプライン第2次修正(2026-07)。要求仕様19節: 手動オーバーライドは
    # EDINET filerNameより優先される。
    override_repo = StockNameOverrideRepository(store_dir=tmp_path)
    override_repo.save("4246", "ダイキョーニシカワ")
    provider = YFinanceFinancialDataProvider(
        now=_NOW,
        edinet_document_source=_NotConfiguredDocumentSource(),  # type: ignore[arg-type]
        edinet_cache_repository=EdinetFilingCacheRepository(store_dir=tmp_path),
        stock_name_override_repository=override_repo,
    )
    assert provider._resolve_japanese_stock_name("4246") == "ダイキョーニシカワ"  # noqa: SLF001


def test_latest_price_on_or_before_picks_exact_match_when_available() -> None:
    index = pd.to_datetime(["2026-03-30", "2026-03-31", "2026-04-01"])
    price_history = pd.DataFrame({"Close": [1000.0, 1010.0, 1020.0]}, index=index)
    price = YFinanceFinancialDataProvider._latest_price_on_or_before(  # noqa: SLF001
        price_history, dt.date(2026, 3, 31)
    )
    assert price == Decimal("1010")


def test_latest_price_on_or_before_returns_none_when_no_date_within_window() -> None:
    index = pd.to_datetime(["2026-01-01"])
    price_history = pd.DataFrame({"Close": [1000.0]}, index=index)
    price = YFinanceFinancialDataProvider._latest_price_on_or_before(  # noqa: SLF001
        price_history, dt.date(2026, 3, 31)
    )
    assert price is None


def test_latest_price_on_or_before_returns_none_for_empty_history() -> None:
    price = YFinanceFinancialDataProvider._latest_price_on_or_before(  # noqa: SLF001
        None, dt.date(2026, 3, 31)
    )
    assert price is None


# ===== look-ahead bias防止(コードレビュー第3回対応) =====


def test_latest_price_on_or_before_never_uses_future_price() -> None:
    """レビュー指摘の具体例(3/29金曜1000円・3/31期末日(日曜)・4/1月曜1200円)を
    そのまま再現する。旧実装(絶対日数最近傍)では4/1(1日差)の方が3/29(2日差)
    より近く1200円が採用されてしまっていたが、期末日より後の株価は使用しない。
    """
    index = pd.to_datetime(["2026-03-27", "2026-03-29", "2026-04-01"])
    price_history = pd.DataFrame({"Close": [990.0, 1000.0, 1200.0]}, index=index)
    price = YFinanceFinancialDataProvider._latest_price_on_or_before(  # noqa: SLF001
        price_history, dt.date(2026, 3, 31)  # 日曜(非営業日)
    )
    assert price == Decimal("1000")  # 直前営業日(3/29金曜)の終値


def test_latest_price_on_or_before_uses_same_day_price_when_present() -> None:
    index = pd.to_datetime(["2026-03-30", "2026-03-31", "2026-04-01"])
    price_history = pd.DataFrame({"Close": [1000.0, 1010.0, 1200.0]}, index=index)
    price = YFinanceFinancialDataProvider._latest_price_on_or_before(  # noqa: SLF001
        price_history, dt.date(2026, 3, 31)
    )
    assert price == Decimal("1010")


def test_latest_price_on_or_before_returns_none_when_only_future_prices_exist() -> None:
    index = pd.to_datetime(["2026-04-01", "2026-04-02"])
    price_history = pd.DataFrame({"Close": [1200.0, 1210.0]}, index=index)
    price = YFinanceFinancialDataProvider._latest_price_on_or_before(  # noqa: SLF001
        price_history, dt.date(2026, 3, 31)
    )
    assert price is None


def test_latest_price_on_or_before_returns_none_when_past_price_exceeds_lookback_window() -> None:
    # 3/31の15日以上前(3/15)のPriceBarしかない場合はNone(未来側で補完しない)。
    index = pd.to_datetime(["2026-03-15"])
    price_history = pd.DataFrame({"Close": [900.0]}, index=index)
    price = YFinanceFinancialDataProvider._latest_price_on_or_before(  # noqa: SLF001
        price_history, dt.date(2026, 3, 31)
    )
    assert price is None


def test_latest_price_on_or_before_picks_most_recent_among_multiple_past_prices() -> None:
    index = pd.to_datetime(["2026-03-25", "2026-03-27", "2026-03-29"])
    price_history = pd.DataFrame({"Close": [950.0, 970.0, 990.0]}, index=index)
    price = YFinanceFinancialDataProvider._latest_price_on_or_before(  # noqa: SLF001
        price_history, dt.date(2026, 3, 31)
    )
    assert price == Decimal("990")


# ===== デプロイ前対応: 年次期末取得不能時に取得日時を代替値として使わないこと =====


class _FakeTickerFinancials:
    def __init__(
        self,
        symbol: str,
        income_stmt: pd.DataFrame | None = None,
        quarterly_income_stmt: pd.DataFrame | None = None,
        cashflow: pd.DataFrame | None = None,
        quarterly_cashflow: pd.DataFrame | None = None,
        balance_sheet: pd.DataFrame | None = None,
        quarterly_balance_sheet: pd.DataFrame | None = None,
        info: dict[str, object] | None = None,
    ) -> None:
        self.symbol = symbol
        self.income_stmt = income_stmt if income_stmt is not None else pd.DataFrame()
        self.quarterly_income_stmt = (
            quarterly_income_stmt if quarterly_income_stmt is not None else pd.DataFrame()
        )
        self.cashflow = cashflow if cashflow is not None else pd.DataFrame()
        self.quarterly_cashflow = (
            quarterly_cashflow if quarterly_cashflow is not None else pd.DataFrame()
        )
        self.balance_sheet = balance_sheet if balance_sheet is not None else pd.DataFrame()
        self.quarterly_balance_sheet = (
            quarterly_balance_sheet if quarterly_balance_sheet is not None else pd.DataFrame()
        )
        self.info = info if info is not None else {"regularMarketPrice": 1000.0}


def test_get_financial_summary_leaves_fiscal_period_end_none_when_annual_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """デプロイ前対応の回帰: 年次決算期末(income_stmt)を取得できない場合、
    データ取得日時(self._now)を代替のfiscal_period_endとして使わない(None)。
    """
    import jstock_advisor.providers.financial_data.yfinance_impl as module

    fake_ticker = _FakeTickerFinancials("7203.T")  # income_stmt空(取得不能を模擬)
    monkeypatch.setattr(module.yf, "Ticker", lambda symbol: fake_ticker)
    provider = YFinanceFinancialDataProvider(
        now=_NOW, stock_name_override_repository=StockNameOverrideRepository(store_dir=tmp_path)
    )

    summary = provider.get_financial_summary("7203")

    assert summary is not None
    assert summary.fiscal_period_end is None


def test_get_financial_summary_recent_quarters_reflect_newer_quarterly_period(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """四半期(quarterly_income_stmt)に年次(income_stmt)より新しい列がある場合、
    recent_quartersの最新quarter_endはその日付を反映する(fiscal_period_end
    (年次)自体は変わらない)。
    """
    import jstock_advisor.providers.financial_data.yfinance_impl as module

    annual_income_stmt = pd.DataFrame({pd.Timestamp("2026-03-31"): {"Operating Income": 1000.0}})
    quarterly_income_stmt = pd.DataFrame(
        {
            pd.Timestamp("2026-03-31"): {"Operating Income": 300.0},
            pd.Timestamp("2026-06-30"): {"Operating Income": 320.0},
        }
    )
    fake_ticker = _FakeTickerFinancials(
        "7203.T", income_stmt=annual_income_stmt, quarterly_income_stmt=quarterly_income_stmt
    )
    monkeypatch.setattr(module.yf, "Ticker", lambda symbol: fake_ticker)
    provider = YFinanceFinancialDataProvider(
        now=_NOW, stock_name_override_repository=StockNameOverrideRepository(store_dir=tmp_path)
    )

    summary = provider.get_financial_summary("7203")

    assert summary is not None
    assert summary.fiscal_period_end == dt.date(2026, 3, 31)
    quarter_ends = [q.quarter_end for q in summary.recent_quarters]
    assert max(quarter_ends) == dt.date(2026, 6, 30)
    assert summary.recent_periods_source == RecentPeriodsSource.QUARTERLY


def test_get_financial_summary_recent_periods_source_is_annual_fallback_when_quarterly_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """由来精緻化対応: quarterly_income_stmtが取得できず年次income_stmtへ
    フォールバックした場合、recent_periods_sourceはANNUAL_FALLBACKとなり、
    QUARTERLYと誤表示しない。
    """
    import jstock_advisor.providers.financial_data.yfinance_impl as module

    annual_income_stmt = pd.DataFrame(
        {
            pd.Timestamp("2025-03-31"): {"Operating Income": 900.0},
            pd.Timestamp("2026-03-31"): {"Operating Income": 1000.0},
        }
    )
    fake_ticker = _FakeTickerFinancials("7203.T", income_stmt=annual_income_stmt)
    monkeypatch.setattr(module.yf, "Ticker", lambda symbol: fake_ticker)
    provider = YFinanceFinancialDataProvider(
        now=_NOW, stock_name_override_repository=StockNameOverrideRepository(store_dir=tmp_path)
    )

    summary = provider.get_financial_summary("7203")

    assert summary is not None
    assert summary.recent_periods_source == RecentPeriodsSource.ANNUAL_FALLBACK
    quarter_ends = [q.quarter_end for q in summary.recent_quarters]
    assert max(quarter_ends) == dt.date(2026, 3, 31)


def test_get_historical_valuation_never_uses_future_price_for_per(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """look-ahead bias回帰確認(コードレビュー第3回対応): 決算期末日
    (2026-03-31、非営業日の日曜)より後の株価(4/1の1200円)ではなく、
    直前営業日(3/29の1000円)がget_historical_valuation()経由でPER算出に
    使われることを確認する(per = price / epsという算出式自体は無変更)。
    """
    import jstock_advisor.providers.financial_data.yfinance_impl as module

    income_stmt = pd.DataFrame({pd.Timestamp("2026-03-31"): {"Diluted EPS": 100.0}})
    balance_sheet = pd.DataFrame({pd.Timestamp("2026-03-31"): {"Stockholders Equity": 500000.0}})
    fake_ticker = _FakeTickerFinancials(
        "7203.T",
        income_stmt=income_stmt,
        balance_sheet=balance_sheet,
        info={"sharesOutstanding": 1000.0},
    )
    price_index = pd.to_datetime(["2026-03-27", "2026-03-29", "2026-04-01"])
    price_history = pd.DataFrame({"Close": [990.0, 1000.0, 1200.0]}, index=price_index)
    fake_ticker.history = lambda **_kwargs: price_history  # type: ignore[attr-defined]
    monkeypatch.setattr(module.yf, "Ticker", lambda symbol: fake_ticker)
    provider = YFinanceFinancialDataProvider(
        now=_NOW, stock_name_override_repository=StockNameOverrideRepository(store_dir=tmp_path)
    )

    results = provider.get_historical_valuation("7203", years=1)

    assert len(results) == 1
    assert results[0].price == Decimal("1000")
    assert results[0].per == Decimal("10")  # 1000 / 100(未来側の1200円は使わない)
    assert results[0].available_at.tzinfo is not None  # timezone-aware必須(コードレビュー対応)


def test_recent_periods_skips_columns_without_valid_date() -> None:
    """デプロイ前対応の回帰: 列(period)が実際の日付を持たない場合、その期を
    スキップする(データ取得日時self._now.date()で代替しない)。
    """
    quarterly_income_stmt = pd.DataFrame(
        {"period-1": {"Operating Income": 100.0}, "period-2": {"Operating Income": 200.0}}
    )
    fake_ticker = _FakeTickerFinancials("7203.T", quarterly_income_stmt=quarterly_income_stmt)
    provider = YFinanceFinancialDataProvider(now=_NOW)

    results = provider._recent_periods(fake_ticker, "7203")  # noqa: SLF001

    assert results.periods == []
    assert results.source == RecentPeriodsSource.UNAVAILABLE


# ===== 判定精度向上機能Phase C: get_earnings_surprise_history() =====


def test_get_earnings_surprise_history_parses_earnings_history_dataframe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import jstock_advisor.providers.financial_data.yfinance_impl as module

    earnings_history = pd.DataFrame(
        {
            "epsActual": {
                pd.Timestamp("2025-09-30"): 71.51,
                pd.Timestamp("2025-12-31"): 96.48,
            },
            "epsEstimate": {
                pd.Timestamp("2025-09-30"): 50.08,
                pd.Timestamp("2025-12-31"): 74.05,
            },
            "surprisePercent": {
                pd.Timestamp("2025-09-30"): 0.428,
                pd.Timestamp("2025-12-31"): 0.303,
            },
        }
    )
    fake_ticker = _FakeTickerFinancials("7203.T")
    fake_ticker.get_earnings_history = lambda: earnings_history  # type: ignore[attr-defined]
    monkeypatch.setattr(module.yf, "Ticker", lambda symbol: fake_ticker)
    provider = YFinanceFinancialDataProvider(
        now=_NOW, stock_name_override_repository=StockNameOverrideRepository(store_dir=tmp_path)
    )

    results = provider.get_earnings_surprise_history("7203")

    assert len(results) == 2
    assert results[0].quarter_end == dt.date(2025, 9, 30)
    assert results[0].eps_actual == Decimal("71.51")
    assert results[0].eps_estimate == Decimal("50.08")
    assert results[0].surprise_pct == pytest.approx(0.428)


def test_get_earnings_surprise_history_handles_missing_estimate_columns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """アナリストカバレッジが薄い銘柄はepsEstimate/surprisePercent列自体が
    存在しないことがある(実測確認済み)。エラーにせずeps_estimate/surprise_pct
    をNoneのままにする。"""
    import jstock_advisor.providers.financial_data.yfinance_impl as module

    earnings_history = pd.DataFrame({"epsActual": {pd.Timestamp("2025-06-30"): 14.31}})
    fake_ticker = _FakeTickerFinancials("3900.T")
    fake_ticker.get_earnings_history = lambda: earnings_history  # type: ignore[attr-defined]
    monkeypatch.setattr(module.yf, "Ticker", lambda symbol: fake_ticker)
    provider = YFinanceFinancialDataProvider(
        now=_NOW, stock_name_override_repository=StockNameOverrideRepository(store_dir=tmp_path)
    )

    results = provider.get_earnings_surprise_history("3900")

    assert len(results) == 1
    assert results[0].eps_actual == Decimal("14.31")
    assert results[0].eps_estimate is None
    assert results[0].surprise_pct is None


def test_get_earnings_surprise_history_returns_empty_when_dataframe_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import jstock_advisor.providers.financial_data.yfinance_impl as module

    fake_ticker = _FakeTickerFinancials("0000.T")
    fake_ticker.get_earnings_history = lambda: pd.DataFrame()  # type: ignore[attr-defined]
    monkeypatch.setattr(module.yf, "Ticker", lambda symbol: fake_ticker)
    provider = YFinanceFinancialDataProvider(
        now=_NOW, stock_name_override_repository=StockNameOverrideRepository(store_dir=tmp_path)
    )

    assert provider.get_earnings_surprise_history("0000") == []


def test_get_earnings_surprise_history_raises_on_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #59 Phase B1: 取得失敗を空リスト(欠測)へ潰さず、ProviderDataErrorとして
    伝播する。従来は例外時に[]を返しており、「取得できて0件」と区別できなかった
    (再試行・障害率の安全弁も例外を観測できなかった)。"""
    import jstock_advisor.providers.financial_data.yfinance_impl as module
    from jstock_advisor.interfaces.provider_errors import ProviderDataError

    class _RaisingTicker:
        def get_earnings_history(self) -> pd.DataFrame:
            raise RuntimeError("boom")

    monkeypatch.setattr(module.yf, "Ticker", lambda symbol: _RaisingTicker())
    provider = YFinanceFinancialDataProvider(
        now=_NOW, stock_name_override_repository=StockNameOverrideRepository(store_dir=tmp_path)
    )

    with pytest.raises(ProviderDataError) as excinfo:
        provider.get_earnings_surprise_history("7203")

    assert excinfo.value.operation == "get_earnings_history"
    assert isinstance(excinfo.value.__cause__, RuntimeError)
