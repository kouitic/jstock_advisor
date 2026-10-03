"""Issue #698 PR-A: BUY候補経路の基準整合判定(MATCH / MISMATCH / UNKNOWN)。

事故(2026-09-29の山九): 分割調整後の株価に、分割前の基準のEPS・BPSから算出した
PER 3.3・PBR 0.27が組み合わされ、適正価格・買付価格3段階・BUY判定まで伝播した。
本PRは、価格履歴の応答に含まれる分割(追加のprovider呼び出しなし)から、財務指標の基準日
以降に分割があった銘柄を検出し、(UNKNOWN)BUY系を「到達」「通常買い」「積極買い」へ
昇格させない(WATCH_FOR_PRICEへ格下げ)。不整合を確認できた(MISMATCH)銘柄は提示を抑止する。

期待値はすべて固定値(ハードコード)で持つ。山九の再現は、実際の`build_stock_snapshot()`
(mock providerの束に、株価・財務指標・分割を観測した値で上書きしたもの)と
`BuySignalService.analyze()`を通す(snapshotを直接組み立てない)。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceLevel
from jstock_advisor.domain.entities.enums import (
    BUY_FAMILY_ACTIONS,
    BuyAction,
    RecommendationType,
    WatchTransitionType,
)
from jstock_advisor.domain.signals import buy_basis_consistency as detector_module
from jstock_advisor.domain.signals.buy_basis_consistency import (
    BASIS_UNKNOWN_CAP_REASON_CODE,
    BasisConsistency,
    BasisMismatchEvidence,
    BasisReasonCode,
    assess_basis_consistency,
)
from jstock_advisor.domain.signals.buy_decision import decide_buy_action
from jstock_advisor.interfaces.provider_errors import ProviderDataError, ProviderFailureCategory
from jstock_advisor.interfaces.types import PriceBar, PriceHistory, PriceSnapshot, PriceSplit
from jstock_advisor.providers.market_data import yfinance_impl
from jstock_advisor.providers.market_data.yfinance_impl import YFinanceMarketDataProvider
from jstock_advisor.services import buy_signal_service as service_module
from jstock_advisor.services.buy_signal_service import BuySignalService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.run_scoped_market_data import RunScopedMarketDataCache
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot
from jstock_advisor.services.watch_state_service import WatchStateService

_NOW = dt.datetime(2026, 8, 9, tzinfo=dt.UTC)  # 日曜。mockの最新の足は2026-08-07(金)
_CONFIG = load_config()
_CALENDAR = BusinessCalendar.from_config(_CONFIG.holiday_calendar)
_SPLIT_DATE = dt.date(2026, 8, 7)  # mockの最新の足の日(= 評価日基準の最新の価格日)
_FUNDAMENTAL_END = dt.date(2026, 3, 31)
_MOCK_CODES = ("2914", "9861", "8136", "8306")


# --- 検出器(純粋関数。値で固定) --------------------------------------------------

_PRICE_DATE = dt.date(2026, 9, 29)
_PERIOD_END = dt.date(2026, 3, 31)
_HISTORY_START = dt.date(2023, 9, 29)


def _split(date: dt.date, ratio: str = "5") -> PriceSplit:
    return PriceSplit(date=date, ratio=Decimal(ratio))


def _assess(
    *,
    splits: list[PriceSplit] | None,
    period_end: dt.date | None = _PERIOD_END,
    history_start: dt.date | None = _HISTORY_START,
    bars_available: bool = True,
    evidence: BasisMismatchEvidence | None = None,
):
    return assess_basis_consistency(
        price_as_of_date=_PRICE_DATE,
        fundamental_period_end=period_end,
        history_start=history_start,
        bars_available=bars_available,
        splits=splits,
        mismatch_evidence=evidence,
    )


def test_d1_no_split_is_match() -> None:
    result = _assess(splits=[])
    assert (result.status, result.reason_code) == (
        BasisConsistency.MATCH,
        BasisReasonCode.NO_EVENT,
    )
    assert result.events == ()


def test_d2_split_on_the_price_date_is_unknown_event_in_window() -> None:
    """山九: 権利落ち日2026-09-29 = 評価日2026-09-29は窓の内。"""
    result = _assess(splits=[_split(_PRICE_DATE)])
    assert (result.status, result.reason_code) == (
        BasisConsistency.UNKNOWN,
        BasisReasonCode.EVENT_IN_WINDOW,
    )
    assert result.events == (_split(_PRICE_DATE),)


@pytest.mark.parametrize(
    ("split_date", "expected"),
    [
        (_PERIOD_END - dt.timedelta(days=1), BasisConsistency.MATCH),
        (_PERIOD_END, BasisConsistency.MATCH),  # 財務指標の基準日当日は窓の外
        (_PERIOD_END + dt.timedelta(days=1), BasisConsistency.UNKNOWN),  # 翌日から内
        (_PRICE_DATE - dt.timedelta(days=1), BasisConsistency.UNKNOWN),
        (_PRICE_DATE, BasisConsistency.UNKNOWN),  # 評価日当日は窓の内
    ],
)
def test_d2_window_boundaries(split_date: dt.date, expected: BasisConsistency) -> None:
    assert _assess(splits=[_split(split_date)]).status is expected


def test_d2_ratio_one_is_not_a_split() -> None:
    assert _assess(splits=[_split(_PRICE_DATE, "1")]).status is BasisConsistency.MATCH


def test_d2_reverse_split_is_also_an_event() -> None:
    result = _assess(splits=[_split(_PRICE_DATE, "0.2")])
    assert result.status is BasisConsistency.UNKNOWN
    assert result.reason_code is BasisReasonCode.EVENT_IN_WINDOW


def test_d2_only_in_window_events_are_reported() -> None:
    old, new = _split(dt.date(2025, 1, 6), "2"), _split(dt.date(2026, 9, 1), "5")
    result = _assess(splits=[old, new])
    assert result.status is BasisConsistency.UNKNOWN
    assert result.events == (new,)


def test_d3_splits_not_reported_is_unknown_never_match() -> None:
    result = _assess(splits=None)
    assert (result.status, result.reason_code) == (
        BasisConsistency.UNKNOWN,
        BasisReasonCode.SPLIT_DATA_NOT_REPORTED,
    )


def test_d3_splits_not_reported_without_bars_is_history_unavailable() -> None:
    result = _assess(splits=None, bars_available=False)
    assert (result.status, result.reason_code) == (
        BasisConsistency.UNKNOWN,
        BasisReasonCode.HISTORY_UNAVAILABLE,
    )


def test_d4_unknown_fundamental_date() -> None:
    result = _assess(splits=[], period_end=None)
    assert (result.status, result.reason_code) == (
        BasisConsistency.UNKNOWN,
        BasisReasonCode.FUNDAMENTAL_DATE_UNKNOWN,
    )


@pytest.mark.parametrize(
    "history_start",
    [None, _PERIOD_END + dt.timedelta(days=1)],
    ids=["start-unknown", "period-end-before-window-start"],
)
def test_d4_window_not_covered_is_unknown_never_match(history_start: dt.date | None) -> None:
    result = _assess(splits=[], history_start=history_start)
    assert (result.status, result.reason_code) == (
        BasisConsistency.UNKNOWN,
        BasisReasonCode.WINDOW_NOT_COVERED,
    )


def test_d4_period_end_equal_to_window_start_is_covered() -> None:
    assert _assess(splits=[], history_start=_PERIOD_END).status is BasisConsistency.MATCH


def test_d5_mismatch_evidence_wins_over_every_other_condition() -> None:
    evidence = BasisMismatchEvidence(source="test-registry")
    for kwargs in (
        {"splits": []},
        {"splits": None},
        {"splits": [_split(_PRICE_DATE)]},
        {"splits": [], "period_end": None},
    ):
        result = _assess(evidence=evidence, **kwargs)  # type: ignore[arg-type]
        assert (result.status, result.reason_code) == (
            BasisConsistency.MISMATCH,
            BasisReasonCode.MISMATCH_EVIDENCE,
        )
        assert result.mismatch_evidence_source == "test-registry"


def test_facts_are_plain_values() -> None:
    facts = _assess(splits=[_split(_PRICE_DATE)]).to_facts()
    assert facts == {
        "status": "UNKNOWN",
        "reason_code": "BASIS_EVENT_IN_WINDOW",
        "fundamental_period_end": "2026-03-31",
        "price_as_of_date": "2026-09-29",
        "history_start": "2023-09-29",
        "events": [{"date": "2026-09-29", "ratio": "5"}],
        "mismatch_evidence_source": None,
    }


def test_reason_codes_are_distinct_values() -> None:
    values = [code.value for code in BasisReasonCode]
    assert len(values) == len(set(values))
    assert BASIS_UNKNOWN_CAP_REASON_CODE not in values


# --- provider: 価格履歴の応答からの分割の取り出し -----------------------------------


class _FakeTicker:
    frames: dict[str, pd.DataFrame] = {}

    def __init__(self, symbol: str) -> None:
        self._symbol = symbol

    def history(self, **_kwargs: Any) -> pd.DataFrame:
        return self.frames[self._symbol]


def _frame(
    rows: list[tuple[str, float, float]], *, with_splits_column: bool = True
) -> pd.DataFrame:
    """rows = (日付, 終値, Stock Splits)。"""
    index = pd.DatetimeIndex([pd.Timestamp(d, tz="Asia/Tokyo") for d, _, _ in rows])
    data: dict[str, list[float]] = {
        "Open": [c for _, c, _ in rows],
        "High": [c for _, c, _ in rows],
        "Low": [c for _, c, _ in rows],
        "Close": [c for _, c, _ in rows],
        "Volume": [1000.0 for _ in rows],
    }
    if with_splits_column:
        data["Stock Splits"] = [s for _, _, s in rows]
    return pd.DataFrame(data, index=index)


def _provider_with(
    monkeypatch: pytest.MonkeyPatch, frame: pd.DataFrame
) -> YFinanceMarketDataProvider:
    _FakeTicker.frames = {"9065.T": frame}
    monkeypatch.setattr(yfinance_impl.yf, "Ticker", _FakeTicker)
    return YFinanceMarketDataProvider(now=dt.datetime(2026, 9, 29, tzinfo=dt.UTC))


def test_p1_stock_splits_column_is_read_with_date_and_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider_with(
        monkeypatch,
        _frame(
            [("2026-09-28", 1633.0, 0.0), ("2026-09-29", 1567.0, 5.0), ("2026-09-30", 1603.0, 0.0)]
        ),
    )
    history = provider.get_price_history("9065", dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert history is not None
    assert history.splits == [PriceSplit(date=dt.date(2026, 9, 29), ratio=Decimal("5.0"))]
    assert [bar.close for bar in history.bars] == [
        Decimal("1633.0"),
        Decimal("1567.0"),
        Decimal("1603.0"),
    ]


def test_p1_all_zero_column_reports_an_empty_list_not_none(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider_with(monkeypatch, _frame([("2026-09-28", 1633.0, 0.0)]))
    history = provider.get_price_history("9065", dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert history is not None
    assert history.splits == []


def test_p1_reverse_split_ratio_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider_with(
        monkeypatch, _frame([("2026-09-28", 100.0, 0.0), ("2026-09-29", 500.0, 0.2)])
    )
    history = provider.get_price_history("9065", dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert history is not None
    assert history.splits == [PriceSplit(date=dt.date(2026, 9, 29), ratio=Decimal("0.2"))]


def test_p1_missing_column_is_none_not_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider_with(
        monkeypatch, _frame([("2026-09-28", 1633.0, 0.0)], with_splits_column=False)
    )
    history = provider.get_price_history("9065", dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert history is not None
    assert history.splits is None


def test_p1_nan_in_the_column_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider_with(
        monkeypatch, _frame([("2026-09-28", 1633.0, 0.0), ("2026-09-29", 1567.0, float("nan"))])
    )
    history = provider.get_price_history("9065", dt.date(2026, 9, 1), dt.date(2026, 9, 30))
    assert history is not None
    assert history.splits is None


def test_p1_benchmark_history_passes_splits_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeTicker.frames = {"1306.T": _frame([("2026-09-28", 3000.0, 0.0)])}
    monkeypatch.setattr(yfinance_impl.yf, "Ticker", _FakeTicker)
    provider = YFinanceMarketDataProvider(now=dt.datetime(2026, 9, 29, tzinfo=dt.UTC))
    history = provider.get_benchmark_price_history(
        "TOPIX", dt.date(2026, 9, 1), dt.date(2026, 9, 30)
    )
    assert history is not None
    assert history.splits == []


# --- provider: run-scope cache は分割を落とさない ----------------------------------


class _InnerMarketData:
    def __init__(self, splits: list[PriceSplit] | None) -> None:
        self._splits = splits
        self.calls = 0

    def get_price_history(
        self, stock_code: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        self.calls += 1
        bar_dates = [dt.date(2026, 9, 28), dt.date(2026, 9, 29), dt.date(2026, 9, 30)]
        bars = [
            PriceBar(
                date=d,
                open=Decimal(100),
                high=Decimal(100),
                low=Decimal(100),
                close=Decimal(100),
                volume=1,
            )
            for d in bar_dates
            if start <= d <= end
        ]
        if not bars:
            return None
        return PriceHistory(
            symbol=stock_code,
            bars=bars,
            source=_source(),
            splits=self._splits,
        )

    def get_benchmark_price_history(self, symbol: str, start: dt.date, end: dt.date):  # type: ignore[no-untyped-def]
        return None

    def get_latest_price(self, stock_code: str):  # type: ignore[no-untyped-def]
        return None

    def get_average_trading_value(self, stock_code: str, business_days: int):  # type: ignore[no-untyped-def]
        return None


def _source():  # type: ignore[no-untyped-def]
    from jstock_advisor.domain.entities.common import DataSourceReference

    return DataSourceReference(provider="test", fetched_at=_NOW)


def test_p2_run_scoped_cache_slices_splits_to_the_requested_range() -> None:
    inner = _InnerMarketData([_split(dt.date(2026, 9, 29)), _split(dt.date(2026, 9, 30), "2")])
    cache = RunScopedMarketDataCache(inner, upper_bound=dt.date(2026, 9, 30))  # type: ignore[arg-type]
    history = cache.get_price_history("9065", dt.date(2026, 9, 28), dt.date(2026, 9, 29))
    assert history is not None
    assert history.splits == [_split(dt.date(2026, 9, 29))]
    assert [bar.date for bar in history.bars] == [dt.date(2026, 9, 28), dt.date(2026, 9, 29)]


def test_p2_run_scoped_cache_keeps_not_reported_as_none() -> None:
    inner = _InnerMarketData(None)
    cache = RunScopedMarketDataCache(inner, upper_bound=dt.date(2026, 9, 30))  # type: ignore[arg-type]
    history = cache.get_price_history("9065", dt.date(2026, 9, 28), dt.date(2026, 9, 30))
    assert history is not None
    assert history.splits is None


# --- decide_buy_action: UNKNOWNの暫定gate -----------------------------------------


def _decide(action_price: str, basis: BasisConsistency | None):
    """現在値と買付価格3段階から、価格条件だけで指定のBuyActionになる入力を作る。"""
    levels = BuyPriceLevels(
        entry=PriceLevel(price=Decimal("3000"), rationale="test entry"),
        standard=PriceLevel(price=Decimal("2500"), rationale="test standard"),
        strong=PriceLevel(price=Decimal("2000"), rationale="test strong"),
    )
    return decide_buy_action(
        current_price=Decimal(action_price),
        buy_price_levels=levels,
        company_quality_score=95.0,
        business_days_to_earnings=None,
        valuation_dispersion_ratio=None,
        basis_consistency=basis,
        config=_CONFIG.buy_decision,
    )


@pytest.mark.parametrize(
    ("price", "raw"),
    [("1500", BuyAction.STRONG_BUY), ("2200", BuyAction.BUY), ("2800", BuyAction.SMALL_ENTRY)],
)
def test_gate_unknown_caps_every_buy_family_action_to_watch_for_price(
    price: str, raw: BuyAction
) -> None:
    baseline = _decide(price, None)
    assert baseline.action is raw  # fixtureが端で満たされていること(未評価では通常どおりBUY系)
    capped = _decide(price, BasisConsistency.UNKNOWN)
    assert capped.action is BuyAction.WATCH_FOR_PRICE
    assert capped.raw_action is raw  # 価格条件のみの仮判定は残る
    assert capped.action not in BUY_FAMILY_ACTIONS
    assert BASIS_UNKNOWN_CAP_REASON_CODE in [r.code for r in capped.reasons]


@pytest.mark.parametrize("basis", [None, BasisConsistency.MATCH])
@pytest.mark.parametrize(
    ("price", "raw"),
    [("1500", BuyAction.STRONG_BUY), ("2200", BuyAction.BUY), ("2800", BuyAction.SMALL_ENTRY)],
)
def test_gate_match_and_not_assessed_do_not_change_the_decision(
    price: str, raw: BuyAction, basis: BasisConsistency | None
) -> None:
    decision = _decide(price, basis)
    assert decision.action is raw
    assert BASIS_UNKNOWN_CAP_REASON_CODE not in [r.code for r in decision.reasons]


def test_gate_unknown_does_not_touch_actions_outside_the_buy_family() -> None:
    decision = _decide("3500", BasisConsistency.UNKNOWN)  # entryを上回る = 価格待ち
    assert decision.action is BuyAction.WATCH_FOR_PRICE
    assert BASIS_UNKNOWN_CAP_REASON_CODE not in [r.code for r in decision.reasons]


def test_gate_capping_to_small_entry_would_not_stop_the_promotion_reaching_the_buy_family() -> None:
    """SMALL_ENTRYを上限にする案を採らない根拠(設計 issuecomment-5969186943 §3の事実)。
    PROMOTED_TO_BUYはBUY系なら立つため、SMALL_ENTRYに残ると「到達」が出る。"""
    assert BuyAction.SMALL_ENTRY in BUY_FAMILY_ACTIONS
    assert BuyAction.WATCH_FOR_PRICE not in BUY_FAMILY_ACTIONS


# --- 実際の build_stock_snapshot / BUY 経路(山九の再現) ----------------------------


class _OverlayMarketData:
    """mockの市場データに、株価水準と価格履歴の分割報告を上書きする(観測した値の再現用)。"""

    def __init__(
        self, inner: Any, *, splits: list[PriceSplit] | None, price: Decimal | None = None
    ) -> None:
        self._inner = inner
        self._splits = splits
        self._price = price

    def _factor(self, stock_code: str) -> Decimal:
        snap = self._inner.get_latest_price(stock_code)
        assert snap is not None
        return Decimal(1) if self._price is None else self._price / snap.close_price

    def get_latest_price(self, stock_code: str) -> PriceSnapshot | None:
        snap = self._inner.get_latest_price(stock_code)
        if snap is None or self._price is None:
            return snap
        return snap.model_copy(update={"close_price": self._price})

    def get_price_history(
        self, stock_code: str, start: dt.date, end: dt.date
    ) -> PriceHistory | None:
        history = self._inner.get_price_history(stock_code, start, end)
        if history is None:
            return None
        factor = self._factor(stock_code)
        bars = [
            bar.model_copy(
                update={
                    "open": (bar.open * factor).quantize(Decimal("0.01")),
                    "high": (bar.high * factor).quantize(Decimal("0.01")),
                    "low": (bar.low * factor).quantize(Decimal("0.01")),
                    "close": (bar.close * factor).quantize(Decimal("0.01")),
                }
            )
            for bar in history.bars
        ]
        return history.model_copy(update={"bars": bars, "splits": self._splits})

    def get_benchmark_price_history(self, symbol: str, start: dt.date, end: dt.date) -> Any:
        return self._inner.get_benchmark_price_history(symbol, start, end)

    def get_average_trading_value(self, stock_code: str, business_days: int) -> Any:
        return self._inner.get_average_trading_value(stock_code, business_days)


class _OverlayFinancialData:
    def __init__(
        self,
        inner: Any,
        *,
        historical_basis: tuple[Decimal, Decimal, Decimal] | None = None,
        **financial_overrides: Any,
    ) -> None:
        self._inner = inner
        self._overrides = financial_overrides
        self._historical_basis = historical_basis

    def get_financial_summary(self, stock_code: str) -> Any:
        summary = self._inner.get_financial_summary(stock_code)
        return summary.model_copy(update=self._overrides) if summary is not None else None

    def get_historical_valuation(self, stock_code: str, years: int) -> Any:
        """過去のPER・PBR系列を、(株価, EPS, BPS)から再構成する(適正価格の各手法が
        互いに近い値になるよう、過去の水準を固定するため)。None = mockのまま。"""
        rows = self._inner.get_historical_valuation(stock_code, years)
        if self._historical_basis is None:
            return rows
        price, eps, bps = self._historical_basis
        return [
            row.model_copy(
                update={
                    "price": price,
                    "eps": eps,
                    "bps": bps,
                    "per": price / eps,
                    "pbr": price / bps,
                }
            )
            for row in rows
        ]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _bundle(
    *,
    splits: list[PriceSplit] | None,
    price: Decimal | None = None,
    historical_basis: tuple[Decimal, Decimal, Decimal] | None = None,
    **financial_overrides: Any,
) -> ProviderBundle:
    base = build_mock_provider_bundle(_NOW)
    return dataclasses.replace(
        base,
        market_data=_OverlayMarketData(base.market_data, splits=splits, price=price),
        financial_data=_OverlayFinancialData(
            base.financial_data,
            historical_basis=historical_basis,
            fiscal_period_end=_FUNDAMENTAL_END,
            **financial_overrides,
        ),
    )


def _service(bundle: ProviderBundle, **kwargs: Any) -> BuySignalService:
    return BuySignalService(providers=bundle, config=_CONFIG, business_calendar=_CALENDAR, **kwargs)


def _analyze(code: str, bundle: ProviderBundle, **service_kwargs: Any):
    return _service(bundle, **service_kwargs).analyze(code, _NOW, RecommendationType.BUY)


def test_snapshot_carries_the_reported_splits_and_the_window_start() -> None:
    splits = [_split(_SPLIT_DATE)]
    snapshot, error = build_stock_snapshot(_bundle(splits=splits), "2914", _NOW, _CONFIG)
    assert error is None and snapshot is not None
    assert snapshot.price_history_splits == splits
    # 窓の始端 = 評価日(JST)2026-08-09の1095日前(lookback_years 3年 × 365日)
    assert snapshot.price_history_start == dt.date(2023, 8, 10)


def test_mock_provider_reports_an_empty_split_list_so_existing_flows_stay_match() -> None:
    """mock providerは「期間内に分割は無い」(空リスト)を明示して報告する。Noneのままだと、
    mockを使う既存のBUY経路がすべてUNKNOWN(WATCH_FOR_PRICEへ格下げ)になる。"""
    base = build_mock_provider_bundle(_NOW)
    for code in _MOCK_CODES:
        history = base.market_data.get_price_history(
            code, dt.date(2023, 8, 10), dt.date(2026, 8, 7)
        )
        assert history is not None
        assert history.splits == []
        outcome = _analyze(code, base)
        assert outcome.recommendation is not None
        facts = outcome.recommendation.buy_score_input_facts
        assert facts is not None
        assert facts["basis_consistency"]["status"] == "MATCH"  # type: ignore[index]


def test_snapshot_keeps_not_reported_as_none() -> None:
    snapshot, _ = build_stock_snapshot(_bundle(splits=None), "2914", _NOW, _CONFIG)
    assert snapshot is not None
    assert snapshot.price_history_splits is None


# 山九の観測値: 分割後の株価1,633円 / forecast_eps 501.47(分割前の基準。PER 3.26)/
# bookValue 6,104(分割前の基準。PBR 0.27)。
_YAMAKYU_PRICE = Decimal("1633")
_YAMAKYU_EPS = Decimal("501.47")
_YAMAKYU_BPS = Decimal("6104")


# 過去のPER・PBRの水準: 株価4,800円 / 上記のEPS・BPS(= PER 9.57・PBR 0.79)。PER法・PBR法の
# 適正価格がともに4,800円付近になり、配当利回り法(mockの配当で約4,850円)と手法間の
# ばらつきが小さくなる。実事故(適正価格4,740円)と同程度の水準で、BUY系になる入力を作る。
_YAMAKYU_HISTORICAL_BASIS = (Decimal("4800"), _YAMAKYU_EPS, _YAMAKYU_BPS)


def _yamakyu_bundle(splits: list[PriceSplit] | None) -> ProviderBundle:
    return _bundle(
        splits=splits,
        price=_YAMAKYU_PRICE,
        historical_basis=_YAMAKYU_HISTORICAL_BASIS,
        forecast_eps=_YAMAKYU_EPS,
        forecast_bps=_YAMAKYU_BPS,
    )


def test_y1_accident_reproduces_a_buy_family_decision_when_no_split_is_reported() -> None:
    """是正前の失敗の固定: 分割が報告されない(MATCH)と、基準の混在した入力から
    BUY系の判定が出る。以降のgateの比較対象(fixtureが端で満たされていることの確認)。"""
    outcome = _analyze("2914", _yamakyu_bundle([]))
    assert outcome.recommendation is not None
    facts = outcome.recommendation.buy_score_input_facts
    assert facts is not None
    assert facts["basis_consistency"]["status"] == "MATCH"  # type: ignore[index]
    assert outcome.buy_action in BUY_FAMILY_ACTIONS
    assert outcome.recommendation.buy_action in BUY_FAMILY_ACTIONS


def test_y1_split_in_the_window_stops_the_buy_family_decision() -> None:
    """山九型: 同じ入力で、財務指標の基準日以降(権利落ち日)に分割が報告されると、
    「積極買い」「通常買い」「打診」のBUY系(= 通知の「到達」を含む)にならない。"""
    baseline = _analyze("2914", _yamakyu_bundle([]))
    outcome = _analyze("2914", _yamakyu_bundle([_split(_SPLIT_DATE)]))
    assert outcome.recommendation is not None and baseline.recommendation is not None
    assert outcome.buy_action is BuyAction.WATCH_FOR_PRICE
    assert outcome.buy_action not in BUY_FAMILY_ACTIONS
    assert outcome.ranking_group == "watch_price"
    # 価格条件のみの仮判定は、gate前の判定のまま残る(格下げの事実が読める)
    assert outcome.recommendation.raw_buy_action == baseline.recommendation.raw_buy_action
    assert outcome.recommendation.raw_buy_action in BUY_FAMILY_ACTIONS
    reason_codes = [r.code for r in outcome.recommendation.buy_decision_reasons]
    assert BASIS_UNKNOWN_CAP_REASON_CODE in reason_codes


def test_y1_the_decision_is_recorded_with_its_reason_and_events() -> None:
    outcome = _analyze("2914", _yamakyu_bundle([_split(_SPLIT_DATE)]))
    assert outcome.recommendation is not None
    facts = outcome.recommendation.buy_score_input_facts
    assert facts is not None
    assert facts["basis_consistency"] == {
        "status": "UNKNOWN",
        "reason_code": "BASIS_EVENT_IN_WINDOW",
        "fundamental_period_end": "2026-03-31",
        "price_as_of_date": "2026-08-07",
        "history_start": "2023-08-10",
        "events": [{"date": "2026-08-07", "ratio": "5"}],
        "mismatch_evidence_source": None,
    }


def test_y1_split_before_the_fundamental_period_end_is_match_and_unchanged() -> None:
    baseline = _analyze("2914", _yamakyu_bundle([]))
    outcome = _analyze("2914", _yamakyu_bundle([_split(dt.date(2026, 3, 31))]))
    assert outcome.recommendation is not None and baseline.recommendation is not None
    assert outcome.buy_action == baseline.buy_action
    facts = outcome.recommendation.buy_score_input_facts
    assert facts is not None and facts["basis_consistency"]["status"] == "MATCH"  # type: ignore[index]


def test_y1_not_reported_splits_are_unknown_and_capped() -> None:
    outcome = _analyze("2914", _yamakyu_bundle(None))
    assert outcome.recommendation is not None
    assert outcome.buy_action is BuyAction.WATCH_FOR_PRICE
    facts = outcome.recommendation.buy_score_input_facts
    assert facts is not None
    assert facts["basis_consistency"]["reason_code"] == "BASIS_SPLIT_DATA_NOT_REPORTED"  # type: ignore[index]


def test_u_unknown_is_a_downgrade_not_a_data_shortage() -> None:
    """UNKNOWNは格下げであり、データ不足ではない(#706の経緯)。handlerはoutcome.data_errorが
    あるときだけ「データ不足」へ数える(buy_candidates_handler.py)。UNKNOWNでは立たない。"""
    outcome = _analyze("2914", _yamakyu_bundle([_split(_SPLIT_DATE)]))
    assert outcome.data_error is None
    assert outcome.recommendation is not None
    assert outcome.buy_action is not BuyAction.DATA_INSUFFICIENT
    assert outcome.screening_passed is True


def test_u1_only_stocks_with_an_event_in_the_window_are_capped() -> None:
    """4銘柄(mockの束)のうち、窓に分割がある1銘柄(BUY系になる2914)だけがUNKNOWNになり、
    他の3銘柄の判定は分割が無い場合と完全に一致する(検査した4銘柄・この経路の範囲)。"""

    class _PerStockSplits(_OverlayMarketData):
        def __init__(self, inner: Any, *, split_codes: frozenset[str], price: Decimal) -> None:
            super().__init__(inner, splits=[], price=price)
            self._split_codes = split_codes

        def get_price_history(
            self, stock_code: str, start: dt.date, end: dt.date
        ) -> PriceHistory | None:
            self._splits = [_split(_SPLIT_DATE)] if stock_code in self._split_codes else []
            return super().get_price_history(stock_code, start, end)

    def _run(split_codes: frozenset[str]) -> dict[str, Any]:
        base = build_mock_provider_bundle(_NOW)
        bundle = dataclasses.replace(
            base,
            market_data=_PerStockSplits(
                base.market_data, split_codes=split_codes, price=_YAMAKYU_PRICE
            ),
            financial_data=_OverlayFinancialData(
                base.financial_data,
                historical_basis=_YAMAKYU_HISTORICAL_BASIS,
                fiscal_period_end=_FUNDAMENTAL_END,
                forecast_eps=_YAMAKYU_EPS,
                forecast_bps=_YAMAKYU_BPS,
            ),
        )
        return {code: _analyze(code, bundle) for code in _MOCK_CODES}

    baseline = _run(frozenset())
    mixed = _run(frozenset({"2914"}))

    assert baseline["2914"].buy_action in BUY_FAMILY_ACTIONS  # fixtureが端で満たされている
    for code in _MOCK_CODES:
        facts = mixed[code].recommendation.buy_score_input_facts
        assert facts is not None
        assert facts["basis_consistency"]["status"] == (  # type: ignore[index]
            "UNKNOWN" if code == "2914" else "MATCH"
        )
        if code != "2914":
            assert mixed[code].buy_action == baseline[code].buy_action
            assert mixed[code].recommendation.raw_buy_action == (
                baseline[code].recommendation.raw_buy_action
            )
    assert mixed["2914"].buy_action is BuyAction.WATCH_FOR_PRICE


def test_s1_shadow_candidate_chain_applies_the_same_gate_as_the_actual_decision() -> None:
    """valuation_confidence shadow(既定OFF)のcandidate再計算にも、actualと同じgateを適用する
    (適用しないと「gate適用後のactual」と「gate適用前のcandidate」を比べ、何も変わって
    いないのにbuy_action_changedが見かけ上発火する)。"""
    from jstock_advisor.services.valuation_confidence_shadow_service import _run_candidate_chain

    unknown = _analyze("2914", _yamakyu_bundle([_split(_SPLIT_DATE)]))
    match = _analyze("2914", _yamakyu_bundle([]))
    unknown_inputs = unknown.valuation_confidence_shadow_inputs
    match_inputs = match.valuation_confidence_shadow_inputs
    assert unknown_inputs is not None and match_inputs is not None
    assert unknown_inputs.basis_consistency is BasisConsistency.UNKNOWN
    assert match_inputs.basis_consistency is BasisConsistency.MATCH
    assert _run_candidate_chain(match_inputs).buy_action in BUY_FAMILY_ACTIONS  # 端の確認
    assert _run_candidate_chain(unknown_inputs).buy_action is BuyAction.WATCH_FOR_PRICE


# --- 監視(NEAR_BUY)・昇格 -------------------------------------------------------


def _spy_watch_state(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    original = WatchStateService.evaluate_and_update

    def _spy(self: WatchStateService, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        calls.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(WatchStateService, "evaluate_and_update", _spy)
    return calls


def test_w1_unknown_day_does_not_call_the_watch_state_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _spy_watch_state(monkeypatch)
    outcome = _analyze("2914", _yamakyu_bundle([_split(_SPLIT_DATE)]))
    assert outcome.recommendation is not None
    assert calls == []
    rec = outcome.recommendation
    assert rec.watch_type is None
    assert rec.watch_transition_type is None
    assert rec.near_buy_consecutive_business_days is None


def test_w1_match_day_still_calls_the_watch_state_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _spy_watch_state(monkeypatch)
    _analyze("2914", _yamakyu_bundle([]))
    assert len(calls) == 1


def test_w2_an_active_near_buy_watch_is_not_promoted_on_an_unknown_day() -> None:
    """監視中の銘柄が、UNKNOWNの日に価格条件を満たしても、PROMOTED_TO_BUY(「到達」)は
    立たず、監視状態も更新されない。同じ入力のMATCH日は昇格する(是正前の失敗の固定)。"""
    service_for_state = WatchStateService(business_calendar=_CALENDAR)
    today = dt.date(2026, 8, 7)
    config = _CONFIG.buy_decision.near_buy
    started = service_for_state.evaluate_and_update(
        stock_code="2914",
        buy_action=BuyAction.WATCH_FOR_PRICE,
        company_quality_score=95.0,
        required_decline_to_entry_pct=Decimal("5"),
        current_price=Decimal("2000"),
        entry_price=Decimal("1900"),
        today=today - dt.timedelta(days=1),
        config=config,
    )
    assert started.transition_type is WatchTransitionType.STARTED

    unknown = _analyze("2914", _yamakyu_bundle([_split(_SPLIT_DATE)]))
    assert unknown.recommendation is not None
    assert unknown.recommendation.watch_transition_type is None
    assert unknown.buy_action not in BUY_FAMILY_ACTIONS

    match = _analyze("2914", _yamakyu_bundle([]))
    assert match.recommendation is not None
    assert match.recommendation.watch_transition_type == "PROMOTED_TO_BUY"


# --- MISMATCH(本番では発火しない分岐。供給元が追加された時に止まることを固定) ----------


class _CapturingAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> None:
        self.records.append(kwargs)


def test_m1_mismatch_suppresses_the_buy_candidate_before_any_valuation() -> None:
    audit = _CapturingAudit()
    outcome = _analyze(
        "2914",
        _yamakyu_bundle([]),
        audit_service=audit,
        basis_mismatch_evidence_source=lambda code: BasisMismatchEvidence(source="test-registry"),
    )
    assert outcome.recommendation is None  # 価格・適正価格・買付価格が記録に残らない
    assert outcome.buy_action is BuyAction.DATA_INSUFFICIENT
    assert outcome.ranking_group is None
    assert outcome.screening_passed is True
    assert outcome.data_error == (
        "分析に必要なデータの整合を確認できなかったため評価できません"
        "(理由区分: BASIS_MISMATCH_EVIDENCE)"
    )
    assert len(audit.records) == 1
    output = audit.records[0]["output_values"]
    assert output["basis_consistency"]["status"] == "MISMATCH"
    assert output["basis_consistency"]["mismatch_evidence_source"] == "test-registry"
    assert output["final_buy_action"] == "DATA_INSUFFICIENT"
    assert output["notification_suppression_reason"] == "BASIS_MISMATCH"
    # 理由コードが記録に残る(UNKNOWNのBASIS_UNKNOWN_CAPとは別の値)
    assert BASIS_UNKNOWN_CAP_REASON_CODE not in str(output)


def test_m1_mismatch_does_not_depend_on_the_price_condition() -> None:
    """BUY系になる価格条件でも、そうでない価格条件でも、MISMATCHは提示されない。"""
    for price in (Decimal("1633"), Decimal("90000")):
        outcome = _analyze(
            "2914",
            _bundle(splits=[], price=price),
            basis_mismatch_evidence_source=lambda code: BasisMismatchEvidence(source="x"),
        )
        assert outcome.recommendation is None
        assert outcome.buy_action is BuyAction.DATA_INSUFFICIENT


def test_m2_without_an_evidence_source_mismatch_never_happens() -> None:
    """本番の構成(供給元なし)。窓に分割があってもMISMATCHにはならずUNKNOWNまで。"""
    outcome = _analyze("2914", _yamakyu_bundle([_split(_SPLIT_DATE)]))
    assert outcome.recommendation is not None
    facts = outcome.recommendation.buy_score_input_facts
    assert facts is not None and facts["basis_consistency"]["status"] == "UNKNOWN"  # type: ignore[index]


# --- fail-soft: 検出機構の失敗がBUY判定を止めない ----------------------------------


def test_f1_evidence_lookup_failure_does_not_stop_the_analysis() -> None:
    def _boom(code: str) -> BasisMismatchEvidence | None:
        raise RuntimeError("registry down")

    outcome = _analyze("2914", _yamakyu_bundle([]), basis_mismatch_evidence_source=_boom)
    assert outcome.recommendation is not None
    facts = outcome.recommendation.buy_score_input_facts
    assert facts is not None
    # 根拠を取得できなかった = MISMATCHにしない。日付・分割の判定へ進む(MATCH)
    assert facts["basis_consistency"]["status"] == "MATCH"  # type: ignore[index]
    assert outcome.buy_action in BUY_FAMILY_ACTIONS


def test_f1_assessment_failure_becomes_unknown_and_the_analysis_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("detector bug")

    monkeypatch.setattr(service_module, "assess_basis_consistency", _boom)
    outcome = _analyze("2914", _yamakyu_bundle([]))
    assert outcome.recommendation is not None  # 例外が伝播しない
    assert outcome.buy_action is BuyAction.WATCH_FOR_PRICE  # 確認できなかった = 昇格させない
    facts = outcome.recommendation.buy_score_input_facts
    assert facts is not None
    assert facts["basis_consistency"]["reason_code"] == "BASIS_ASSESSMENT_FAILED"  # type: ignore[index]


def test_f2_a_price_history_failure_is_still_a_snapshot_failure_not_a_new_unknown() -> None:
    """価格履歴の取得失敗は従来どおりsnapshot全体の失敗(例外)で、本機構が「分割の取得だけが
    失敗する日」を新たに作らない(分割は同じ応答から取る)。"""

    class _Failing(_OverlayMarketData):
        def get_price_history(self, stock_code: str, start: dt.date, end: dt.date):  # type: ignore[no-untyped-def]
            raise ProviderDataError(
                provider_name="test",
                operation="history",
                retryable=True,
                failure_category=ProviderFailureCategory.NON_RETRYABLE_PROVIDER_FAILURE,
                error_type="X",
                error_summary="x",
            )

    base = build_mock_provider_bundle(_NOW)
    bundle = dataclasses.replace(base, market_data=_Failing(base.market_data, splits=[]))
    with pytest.raises(ProviderDataError):
        build_stock_snapshot(bundle, "2914", _NOW, _CONFIG)


def test_detector_module_has_no_io_imports() -> None:
    """検出器は純粋関数(I/Oなし)。provider・repository・ネットワークをimportしない。"""
    import inspect

    source = inspect.getsource(detector_module)
    for forbidden in ("providers", "infrastructure", "yfinance", "boto3", "requests"):
        assert f"import {forbidden}" not in source
        assert f"from jstock_advisor.{forbidden}" not in source
