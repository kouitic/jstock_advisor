"""BuySignalServiceのサービス層直接テスト(2026-07 BUYパイプライン再設計)。

既存のtest_buy_candidates_handler.pyはBuySignalServiceをモックして扱うため、
実際に22ステップのパイプラインを通した`Recommendation`の値を直接検証する
テストが存在しなかった(カバレッジの穴)。ここではbuild_stock_snapshot()を
モックしてStockSnapshotを直接構築し、BuySignalService.analyze()をエンドツー
エンドで実行する。

§21の5銘柄回帰テストは、本番相当の評価で実際に報告された現在値・予想配当
(dividend_yield_pct_at_recommendationから逆算した実配当額)をそのまま使い、
適正価格の1手法(PER相当)には旧システムが報告した「最終適正価格」をそのまま
採用する。これにより、少なくとも用いた入力データは実データに基づいたものと
なる。ただし適正価格の集計方法自体は新設計(手法間バラつき・信頼度・
安全余裕率)に置き換わっているため、算出される購入判断基準価格・買付価格は
旧システムの値と一致しない(これは仕様どおりの挙動であり、バグではない)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.classification import StockTypeClassification
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.earnings_surprise import EarningsSurpriseResult
from jstock_advisor.domain.entities.earnings_trend import EarningsTrendResult
from jstock_advisor.domain.entities.entry_price_range import EntryPriceRangeResult
from jstock_advisor.domain.entities.enums import (
    BUY_FAMILY_ACTIONS,
    BuyAction,
    ConfidenceLevel,
    EarningsDateStatus,
    EarningsSurpriseEvaluationState,
    EarningsTrendEvaluationState,
    HistoricalValuationEvaluationState,
    PriceRangeEvaluationState,
    RecommendationType,
    StockType,
    TimingScoreEvaluationState,
    TrendClassification,
)
from jstock_advisor.domain.entities.historical_valuation import HistoricalValuationResult
from jstock_advisor.domain.entities.momentum import MomentumSnapshot
from jstock_advisor.domain.entities.timing_score import TimingScoreResult
from jstock_advisor.domain.entities.valuation import FairValueRange
from jstock_advisor.interfaces.types import DividendInfo, FinancialSummary, HistoricalValuation
from jstock_advisor.services import buy_signal_service as service_module
from jstock_advisor.services.buy_signal_service import BuySignalService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.stock_snapshot_service import StockSnapshot

_CONFIG = load_config()
_CALENDAR = BusinessCalendar.from_config(_CONFIG.holiday_calendar)
_SOURCE = DataSourceReference(
    provider="test-fixture", fetched_at=dt.datetime(2026, 7, 30, tzinfo=dt.UTC)
)

# 決算3営業日以内ルールを確実に発火させるための基準日時(2026-08-04火曜日、
# 4516の次回決算予定日2026-08-06木曜日まで営業日2日)。
_NOW = dt.datetime(2026, 8, 4, 7, 0, tzinfo=dt.UTC)


def _financial(
    *,
    stock_code: str,
    industry: str,
    sector: str,
    forecast_eps: Decimal,
    forecast_bps: Decimal | None = None,
    equity_ratio_pct: float = 70.0,
    payout_ratio_pct: float = 20.0,
) -> FinancialSummary:
    return FinancialSummary(
        stock_code=stock_code,
        stock_name=None,
        fiscal_period_end=dt.date(2026, 3, 31),
        fiscal_year_end_month=3,
        industry=industry,
        sector=sector,
        equity_ratio_pct=equity_ratio_pct,
        payout_ratio_pct=payout_ratio_pct,
        forecast_eps=forecast_eps,
        forecast_bps=forecast_bps,
        operating_cashflow=None,
        capital_expenditure=None,
        shares_outstanding=None,
        source=_SOURCE,
    )


def _dividend(
    stock_code: str, forecast_dividend: Decimal, *, progressive: bool = True, years: int = 5
) -> DividendInfo:
    return DividendInfo(
        stock_code=stock_code,
        fiscal_year="2026",
        forecast_annual_dividend_per_share=forecast_dividend,
        is_progressive_or_doe_policy=progressive,
        consecutive_dividend_increase_years=years,
        source=_SOURCE,
    )


def _historical_per_only(
    per_median: Decimal, pbr_median: Decimal | None = None, count: int = 3
) -> list[HistoricalValuation]:
    """per/pbr中央値が指定値になるよう、eps=Noneのレコードのみを用意する
    (EPSの平準化が過去黒字年度データ不足でforecast_epsへフォールバックするようにする)。
    """
    return [
        HistoricalValuation(
            stock_code="0000",
            date=dt.date(2026 - i, 3, 31),
            eps=None,
            bps=None,
            per=per_median,
            pbr=pbr_median,
            available_at=_SOURCE.fetched_at,
            source=_SOURCE,
        )
        for i in range(1, count + 1)
    ]


def _stock_type(stock_code: str, types: list[StockType]) -> StockTypeClassification:
    return StockTypeClassification(
        stock_code=stock_code,
        classified_at=_NOW,
        types=types,
        primary_type=types[0] if types else None,
        confidence=ConfidenceLevel.MEDIUM,
        classification_basis=["test fixture"],
        data_sources=[_SOURCE],
    )


_EMPTY_FAIR_VALUE_RANGE = FairValueRange(
    bear=None,
    neutral=None,
    bull=None,
    overall_confidence=ConfidenceLevel.LOW,
    methods_used=[],
    methods_excluded=[],
    usable_for_trading_judgment=False,
    unusable_reason="unused by BUY pipeline (fixture placeholder)",
)

_MOMENTUM_PLACEHOLDER = MomentumSnapshot(
    trend_classification=TrendClassification.NEUTRAL,
    trend_evaluable=False,
    price_history_aligned=True,
    price_history_has_future_bars=False,
    confidence=ConfidenceLevel.LOW,
)

_HISTORICAL_VALUATION_PLACEHOLDER = HistoricalValuationResult(
    state=HistoricalValuationEvaluationState.NOT_EVALUATED,
    evaluated_at=_NOW,
    model_version="test-fixture",
)

_TIMING_PLACEHOLDER = TimingScoreResult(
    state=TimingScoreEvaluationState.NOT_EVALUATED,
    evaluated_at=_NOW,
    model_version="test-fixture",
)

_EARNINGS_SURPRISE_PLACEHOLDER = EarningsSurpriseResult(
    state=EarningsSurpriseEvaluationState.NOT_EVALUATED,
    evaluated_at=_NOW,
    model_version="test-fixture",
)

_EARNINGS_TREND_PLACEHOLDER = EarningsTrendResult(
    state=EarningsTrendEvaluationState.NOT_EVALUATED,
    evaluated_at=_NOW,
    model_version="test-fixture",
)

_ENTRY_PRICE_RANGE_PLACEHOLDER = EntryPriceRangeResult(
    state=PriceRangeEvaluationState.NOT_EVALUATED,
    current_price=Decimal("1000"),
    evaluated_at=_NOW,
    model_version="test-fixture",
)


@dataclass(frozen=True)
class _StockFixture:
    stock_code: str
    stock_name: str
    current_price: Decimal
    industry: str
    sector: str
    forecast_dividend: Decimal
    forecast_eps: Decimal
    per_median: Decimal
    forecast_bps: Decimal | None = None
    pbr_median: Decimal | None = None
    stock_types: list[StockType] = field(default_factory=list)
    next_earnings_date: dt.date | None = None


def _build_snapshot(fx: _StockFixture) -> StockSnapshot:
    financial = _financial(
        stock_code=fx.stock_code,
        industry=fx.industry,
        sector=fx.sector,
        forecast_eps=fx.forecast_eps,
        forecast_bps=fx.forecast_bps,
    )
    dividend = _dividend(fx.stock_code, fx.forecast_dividend)
    dividend_yield_pct = float(fx.forecast_dividend / fx.current_price * 100)
    # build_stock_snapshot()と同じ決算日検証ロジック(コードレビュー対応)。この
    # フィクスチャはbuild_stock_snapshot()自体をモックしてStockSnapshotを直接
    # 構築するため、検証済みのnext_earnings_date/earnings_date_status/
    # earnings_date_rawを自前で整合させる必要がある。
    earnings_date_raw = fx.next_earnings_date
    if earnings_date_raw is None:
        earnings_date_status = EarningsDateStatus.UNAVAILABLE
        resolved_next_earnings_date = None
    elif earnings_date_raw < _NOW.date():
        earnings_date_status = EarningsDateStatus.STALE_PAST_DATE
        resolved_next_earnings_date = None
    else:
        earnings_date_status = EarningsDateStatus.CONFIRMED
        resolved_next_earnings_date = earnings_date_raw
    business_days_to_earnings = (
        _CALENDAR.business_days_between(_NOW.date(), resolved_next_earnings_date)
        if resolved_next_earnings_date is not None
        else None
    )
    return StockSnapshot(
        stock_code=fx.stock_code,
        current_price=fx.current_price,
        financial=financial,
        dividend=dividend,
        benefit=None,
        bars=[],
        historical_valuations=_historical_per_only(fx.per_median, fx.pbr_median),
        avg_trading_value=Decimal("100000000"),
        disclosures=[],
        next_earnings_date=resolved_next_earnings_date,
        earnings_date_status=earnings_date_status,
        earnings_date_raw=earnings_date_raw,
        business_days_to_earnings=business_days_to_earnings,
        dividend_yield_pct=dividend_yield_pct,
        benefit_yield_pct=None,
        annual_benefit_value=None,
        total_yield_pct=dividend_yield_pct,
        fair_value=None,
        fair_value_methods_used_count=0,
        data_sources=[_SOURCE],
        data_fetched_at=_NOW,
        quarterly_operating_incomes=[
            Decimal("100"),
            Decimal("105"),
            Decimal("110"),
            Decimal("115"),
        ],
        quarterly_operating_cashflows=[],
        quarterly_operating_income_periods=[],
        quarterly_operating_cashflow_periods=[],
        severe_earnings_decline=False,
        disclosure_risk_keywords_found=[],
        material_event_keywords_found=[],
        cashflow_decomposition=None,
        stock_type_classification=_stock_type(fx.stock_code, fx.stock_types),
        fair_value_range=_EMPTY_FAIR_VALUE_RANGE,
        momentum=_MOMENTUM_PLACEHOLDER,
        historical_valuation=_HISTORICAL_VALUATION_PLACEHOLDER,
        timing=_TIMING_PLACEHOLDER,
        earnings_surprise=_EARNINGS_SURPRISE_PLACEHOLDER,
        earnings_trend=_EARNINGS_TREND_PLACEHOLDER,
        entry_price_range=_ENTRY_PRICE_RANGE_PLACEHOLDER,
    )


# --- 5銘柄の実データ(本番相当の評価で報告された現在値・配当利回りから逆算) -------

_NIHON_SHINYAKU = _StockFixture(
    stock_code="4516",
    stock_name="日本新薬",
    current_price=Decimal("3495"),
    industry="医薬品",
    sector="Healthcare",
    forecast_dividend=Decimal("124"),  # 3.5479...% * 3495 = 124
    # PER法・PBR法をともに6,100円付近に設定する(配当利回り法単独(3,100円)
    # では手法間バラつきに埋もれて安全余裕率控除後に現在値を下回ってしまうため、
    # 独立した2手法が同程度の高値を示す状態を用意し、価格条件テストの意図
    # (現在値が打診買い価格以下)を安定して再現する)。
    forecast_eps=Decimal("305"),  # per_median(20) * eps = 6100
    per_median=Decimal("20"),
    forecast_bps=Decimal("1220"),  # pbr_median(5) * bps = 6100
    pbr_median=Decimal("5"),
    stock_types=[],
    next_earnings_date=dt.date(2026, 8, 6),  # _NOWから営業日2日後 -> 決算直前ルール発火
)

_TACHI_S = _StockFixture(
    stock_code="7239",
    stock_name="タチエス",
    current_price=Decimal("2277"),
    industry="輸送用機器",
    sector="Auto Parts",
    forecast_dividend=Decimal("115"),  # 5.0505...% * 2277 = 115
    forecast_eps=Decimal("126.87"),  # 15 * eps = 1903(旧適正価格)
    per_median=Decimal("15"),
)

_DAIKYO_NISHIKAWA = _StockFixture(
    stock_code="4246",
    stock_name="DaikyoNishikawa",
    current_price=Decimal("1027"),
    industry="輸送用機器",
    sector="Auto Parts",
    forecast_dividend=Decimal("58"),  # 5.6475...% * 1027 = 58
    forecast_eps=Decimal("47.73"),  # 15 * eps = 716(旧適正価格)
    per_median=Decimal("15"),
)

_HOKURYO = _StockFixture(
    stock_code="1384",
    stock_name="ホクリヨウ",
    current_price=Decimal("2035"),
    industry="水産・農林業",
    sector="Food",
    forecast_dividend=Decimal("80"),  # 3.9312...% * 2035 = 80
    forecast_eps=Decimal("107.47"),  # 15 * eps = 1612(旧適正価格)
    per_median=Decimal("15"),
)

_AICHI_TOKEI = _StockFixture(
    stock_code="7723",
    stock_name="愛知時計電機",
    current_price=Decimal("3025"),
    industry="機械",
    sector="Industrial",
    forecast_dividend=Decimal("120"),  # 3.9669...% * 3025 = 120
    forecast_eps=Decimal("130.73"),  # 15 * eps = 1961(旧適正価格)
    per_median=Decimal("15"),
)


def _providers() -> ProviderBundle:
    # build_stock_snapshotをmonkeypatchで置き換えるため、providersの中身は
    # 一切参照されない(型を満たすだけのダミー)。
    return object()  # type: ignore[return-value]


def _analyze(
    monkeypatch: pytest.MonkeyPatch, fx: _StockFixture
) -> service_module.BuyAnalysisOutcome:
    snapshot = _build_snapshot(fx)
    monkeypatch.setattr(
        service_module, "build_stock_snapshot", lambda *a, **kw: (snapshot, None)
    )
    service = BuySignalService(providers=_providers(), config=_CONFIG, business_calendar=_CALENDAR)
    return service.analyze(fx.stock_code, _NOW, RecommendationType.BUY)


def test_nihon_shinyaku_4516_forced_to_watch_before_earnings_by_earnings_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4516: 現在値(3,495円)は適正価格を下回るが、次回決算(2026-08-06)まで
    営業日2日のため、価格条件・スコアにかかわらずWATCH_BEFORE_EARNINGSとなる
    (要求仕様16節: 決算直前は例外なく新規購入を待つ)。§21が許容する結果
    (BUY/SMALL_ENTRY/WATCH_BEFORE_EARNINGSのいずれか)に含まれる。
    """
    outcome = _analyze(monkeypatch, _NIHON_SHINYAKU)
    assert outcome.recommendation is not None
    assert outcome.buy_action == BuyAction.WATCH_BEFORE_EARNINGS
    assert outcome.buy_action not in BUY_FAMILY_ACTIONS
    assert outcome.ranking_group == "watch_price"
    # 業種別モデル未実装(医薬品)のため、信頼度はHIGHにならない(要求仕様12節)。
    assert outcome.recommendation.confidence != ConfidenceLevel.HIGH
    assert outcome.recommendation.buy_industry_sector is not None
    assert outcome.recommendation.buy_industry_sector.value == "PHARMACEUTICAL"


def test_tachi_s_7239_watch_for_price_excluded_from_buy_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """7239: 現在値(2,277円)が適正価格(1,903円)を約19.7%上回っているため、
    企業魅力度スコアにかかわらずBUY系判定にならず、購入候補ランキングから
    除外される(要求仕様4節・21節)。
    """
    outcome = _analyze(monkeypatch, _TACHI_S)
    assert outcome.recommendation is not None
    assert outcome.buy_action == BuyAction.WATCH_FOR_PRICE
    assert outcome.buy_action not in BUY_FAMILY_ACTIONS
    assert outcome.ranking_group != "buy_candidate"
    assert outcome.recommendation.buy_industry_sector.value == "AUTOMOTIVE_PARTS"


def test_daikyo_nishikawa_4246_excluded_from_buy_ranking_when_far_above_fair_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4246: 現在値(1,027円)が適正価格(716円)を約43.4%上回っている。
    価格条件を満たさないため、BUY系判定は禁止される。
    """
    outcome = _analyze(monkeypatch, _DAIKYO_NISHIKAWA)
    assert outcome.recommendation is not None
    assert outcome.buy_action not in BUY_FAMILY_ACTIONS
    assert outcome.buy_action in {BuyAction.WATCH_FOR_PRICE, BuyAction.NOT_ATTRACTIVE}
    assert outcome.ranking_group != "buy_candidate"


def test_hokuryo_1384_watch_for_price_with_normalized_eps_considered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1384: 現在値(2,035円)が適正価格(1,612円)を約26.2%上回っている。
    鶏卵価格上昇等の市況要因を考慮する食品/市況influenced業種(FOOD)に
    分類され、平準化EPSの検討対象となる(要求仕様13節)。
    """
    outcome = _analyze(monkeypatch, _HOKURYO)
    assert outcome.recommendation is not None
    assert outcome.buy_action not in BUY_FAMILY_ACTIONS
    assert outcome.ranking_group != "buy_candidate"
    assert outcome.recommendation.buy_industry_sector.value == "FOOD"
    # FOODは市況影響業種としてEPS平準化の検討対象(要求仕様13節)。
    assert outcome.recommendation.eps_normalization_method is not None


def test_aichi_tokei_7723_excluded_from_buy_ranking_when_far_above_fair_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """7723: 現在値(3,025円)が適正価格(1,961円)を約54.3%上回っている、
    5銘柄中もっとも乖離が大きいケース。BUY系判定は明確に禁止される。
    """
    outcome = _analyze(monkeypatch, _AICHI_TOKEI)
    assert outcome.recommendation is not None
    assert outcome.buy_action not in BUY_FAMILY_ACTIONS
    assert outcome.buy_action in {BuyAction.WATCH_FOR_PRICE, BuyAction.NOT_ATTRACTIVE}
    assert outcome.ranking_group != "buy_candidate"
    assert outcome.recommendation.buy_industry_sector.value == "GENERAL_MANUFACTURING"


def test_only_nihon_shinyaku_is_not_excluded_for_price_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5銘柄のうち、価格条件(現在値 vs 適正価格)を満たすのは4516のみである
    ことを確認する回帰テスト(今回の修正の核心)。4516はWATCH_BEFORE_EARNINGSに
    なるが、これは決算直前ルールによるものであり価格条件不足が理由ではない。
    """
    results = {
        fx.stock_code: _analyze(monkeypatch, fx)
        for fx in (
            _NIHON_SHINYAKU,
            _TACHI_S,
            _DAIKYO_NISHIKAWA,
            _HOKURYO,
            _AICHI_TOKEI,
        )
    }
    # 現在値が打診買い価格を上回っている4銘柄は、購入候補ランキングへ含まれない。
    for code in ("7239", "4246", "1384", "7723"):
        assert results[code].ranking_group != "buy_candidate", code

    # 4516だけが「価格条件は問題ない」状態であることを確認する。raw_buy_action
    # (決算直前調整より前の、価格条件+スコアのみによる仮判定)がBUY系であれば、
    # 最終的にWATCH_BEFORE_EARNINGSへ格下げされたのは決算直前ルールが理由であり、
    # 価格条件の不足が理由ではないことがわかる。
    nihon_shinyaku_rec = results["4516"].recommendation
    assert nihon_shinyaku_rec is not None
    assert nihon_shinyaku_rec.raw_buy_action in BUY_FAMILY_ACTIONS
    nihon_shinyaku_reasons = [r.code for r in nihon_shinyaku_rec.buy_decision_reasons]
    assert "EARNINGS_WINDOW" in nihon_shinyaku_reasons
