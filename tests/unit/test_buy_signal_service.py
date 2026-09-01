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
from jstock_advisor.domain.classification.canonical_industry import JpxLookupStatus
from jstock_advisor.domain.entities.classification import StockTypeClassification
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.earnings_surprise import EarningsSurpriseResult
from jstock_advisor.domain.entities.earnings_trend import EarningsTrendResult
from jstock_advisor.domain.entities.entry_price_range import EntryPriceRangeResult
from jstock_advisor.domain.entities.enums import (
    BUY_FAMILY_ACTIONS,
    BuyAction,
    BuyIndustrySector,
    ConfidenceLevel,
    EarningsDateStatus,
    EarningsSurpriseEvaluationState,
    EarningsTrendEvaluationState,
    EnvironmentEvaluationState,
    HistoricalValuationEvaluationState,
    IndustryClassification,
    MarketEnvironmentEvaluationState,
    PriceRangeEvaluationState,
    ProfitTakingIndustrySector,
    RecommendationType,
    SectorEnvironmentEvaluationState,
    StockType,
    TimingScoreEvaluationState,
    TrendClassification,
)
from jstock_advisor.domain.entities.environment import EnvironmentResult
from jstock_advisor.domain.entities.historical_valuation import HistoricalValuationResult
from jstock_advisor.domain.entities.market_environment import MarketEnvironmentResult
from jstock_advisor.domain.entities.momentum import MomentumSnapshot
from jstock_advisor.domain.entities.sector_environment import SectorEnvironmentResult
from jstock_advisor.domain.entities.timing_score import TimingScoreResult
from jstock_advisor.domain.entities.valuation import FairValueRange
from jstock_advisor.interfaces.disclosure import (
    DisclosureAvailability,
    DisclosureUnavailableReason,
)
from jstock_advisor.interfaces.types import (
    Disclosure,
    DividendInfo,
    FinancialSummary,
    HistoricalValuation,
)
from jstock_advisor.services import buy_signal_service as service_module
from jstock_advisor.services.buy_signal_service import BuySignalService
from jstock_advisor.services.jpx_industry_source import (
    JpxIndustryEntry,
    JpxIndustryLookup,
    JpxIndustrySource,
)
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

_MARKET_ENVIRONMENT_PLACEHOLDER = MarketEnvironmentResult(
    state=MarketEnvironmentEvaluationState.NOT_EVALUATED,
    evaluated_at=_NOW,
    model_version="test-fixture",
)

_SECTOR_ENVIRONMENT_PLACEHOLDER = SectorEnvironmentResult(
    state=SectorEnvironmentEvaluationState.NOT_APPLICABLE,
    evaluated_at=_NOW,
    model_version="test-fixture",
)

_ENVIRONMENT_PLACEHOLDER = EnvironmentResult(
    state=EnvironmentEvaluationState.NOT_EVALUATED,
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


def _build_snapshot(
    fx: _StockFixture,
    disclosure_availability: DisclosureAvailability = DisclosureAvailability.AVAILABLE,
    disclosure_unavailable_reason: DisclosureUnavailableReason | None = None,
    disclosures: list[Disclosure] | None = None,
    disclosure_risk_keywords_found: list[str] | None = None,
) -> StockSnapshot:
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
        disclosures=disclosures or [],
        disclosure_availability=disclosure_availability,
        disclosure_unavailable_reason=disclosure_unavailable_reason,
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
        disclosure_risk_keywords_found=disclosure_risk_keywords_found or [],
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
        market_environment=_MARKET_ENVIRONMENT_PLACEHOLDER,
        sector_environment=_SECTOR_ENVIRONMENT_PLACEHOLDER,
        environment=_ENVIRONMENT_PLACEHOLDER,
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

# レビュー対応(2026-08、commit f546473再レビュー、VALUATION_OUTLIER_EXCLUDED
# 実データ検証用): target_yield法(配当基準価格=1円÷4.0%=25円)が現在値1000円の
# 10%(=100円)を大きく下回るよう設計し、apply_outlier_filters()の
# EXTREME_LOW_RELATIVE_TO_CURRENT_PRICE検知を実際に発火させる。per法・pbr法は
# いずれも1500円で一致させ、外れ値除外後も_MIN_REMAINING_METHODS_AFTER_FILTER
# (2件)を満たし、除外結果がそのまま採用されるようにする。
_OUTLIER_EXCLUSION_STOCK = _StockFixture(
    stock_code="1111",
    stock_name="テスト外れ値銘柄",
    current_price=Decimal("1000"),
    industry="医薬品",
    sector="Healthcare",
    forecast_dividend=Decimal("1"),  # target_yield価格 = 1 / 0.04 = 25円(現在値の2.5%)
    forecast_eps=Decimal("100"),  # per価格 = 15 * 100 = 1500円
    per_median=Decimal("15"),
    forecast_bps=Decimal("500"),  # pbr価格 = 3 * 500 = 1500円
    pbr_median=Decimal("3"),
)

# レビュー対応(2026-08、NO_VALUATION_ANCHOR表示不備の是正、必須テスト1・2):
# target_yield(1000円)・per(1300円)・pbr(2200円)の3方式がいずれも個別には
# 有効(outlier filterでも除外されない、いずれの方式も他方式中央値の40%〜250%の
# 範囲内・現在値の10%以上)だが、方式間の乖離(2200/1000=2.2倍)が
# dispersion.auto_buy_block(2.00倍)を超えるよう設計し、determine_valuation_
# confidence()のVALUATION_DISPERSION_TOO_HIGH分岐(valuation_confidence.py)を
# 実際に発火させる。DCF・価格レンジ法は本フィクスチャ共通の仕様上
# (operating_cashflow=None・bars=[])常に算出不可のため対象外(3方式のみ)。
_DISPERSION_STOCK = _StockFixture(
    stock_code="2222",
    stock_name="テスト乖離銘柄",
    current_price=Decimal("1000"),
    industry="小売業",
    sector="Retail",
    forecast_dividend=Decimal("40"),  # target_yield価格 = 40 / 0.04 = 1000円
    forecast_eps=Decimal("100"),  # per価格 = 13 * 100 = 1300円
    per_median=Decimal("13"),
    forecast_bps=Decimal("200"),  # pbr価格 = 11 * 200 = 2200円
    pbr_median=Decimal("11"),
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


def test_buy_score_input_facts_includes_forecast_eps_and_bps_for_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """レビュー対応(2026-08、修正条件2): current_per/current_pbrは判定時点の
    forecast_eps/forecast_bpsから算出された導出値であり、算出そのものを事後に
    監査・再現するには元のforecast_eps/forecast_bps自体も判定時点入力として
    保存されている必要がある。「表示に必要か」ではなく「判定時点の計算を
    監査可能か」を基準に、buy_score_input_factsへ両者が保存されることを確認
    する(current_per/current_pbrの値自体もforecast_eps/forecast_bpsと現在値
    から算出した値と整合すること、すなわちforecast_eps/forecast_bpsが捏造値
    ではなく実際にcurrent_per/current_pbr算出へ使われた入力そのものであること
    も合わせて検証する)。
    """
    outcome = _analyze(monkeypatch, _NIHON_SHINYAKU)
    rec = outcome.recommendation
    assert rec is not None
    facts = rec.buy_score_input_facts
    assert facts is not None
    assert facts["forecast_eps"] == str(_NIHON_SHINYAKU.forecast_eps)
    assert facts["forecast_bps"] == str(_NIHON_SHINYAKU.forecast_bps)
    # current_per = current_price / forecast_eps であることの整合性確認
    # (forecast_epsが実際にcurrent_per算出へ使われた値と一致することの検証)。
    expected_current_per = _NIHON_SHINYAKU.current_price / _NIHON_SHINYAKU.forecast_eps
    assert facts["current_per"] == str(expected_current_per)
    expected_current_pbr = _NIHON_SHINYAKU.current_price / _NIHON_SHINYAKU.forecast_bps
    assert facts["current_pbr"] == str(expected_current_pbr)


def test_buy_price_reliability_facts_and_config_snapshot_are_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """レビュー対応(2026-08、BUY_PRICE_RELIABILITY_LOW具体的理由表示、必須
    テストA/B): determine_buy_price_reliability()の判定に実際に使用した
    入力事実(concerns自体含む)がbuy_score_input_facts、config閾値の判定
    時点スナップショットがconfig_values_usedへ、それぞれ実際の判定時点値
    で保存されることを確認する(LOW/OKいずれの判定結果でも、facts自体は
    常に保存される)。
    """
    outcome = _analyze(monkeypatch, _NIHON_SHINYAKU)
    rec = outcome.recommendation
    assert rec is not None
    facts = rec.buy_score_input_facts
    assert facts is not None

    # A: buy_score_input_facts(concerns自体と、他のどこにも判定時点値が
    # 残らない入力事実)。
    assert isinstance(facts["buy_price_reliability_concerns"], list)
    assert facts["data_age_business_days"] == 0
    assert "entry_margin_before_cap" in facts
    assert "outlier_filter_blocking_reason" in facts
    assert "valuation_methods_used_count" in facts
    assert "valuation_excluded_outlier_count" in facts

    # B: config_values_used(determine_buy_price_reliability()が参照する
    # config由来の値の判定時点スナップショット)。
    config_snapshot = rec.config_values_used
    assert config_snapshot is not None
    assert config_snapshot["maximum_entry_margin"] == pytest.approx(
        _CONFIG.buy_decision.margin_of_safety.maximum_margin.entry
    )
    assert config_snapshot["valuation_dispersion_medium_max"] == pytest.approx(
        _CONFIG.buy_decision.valuation_dispersion.medium_max
    )


def test_valuation_outlier_exclusions_captures_actual_outlier_filtered_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """レビュー対応(2026-08、commit f546473再レビュー、必須テスト1): 実際に
    BuySignalServiceを通し、外れ値フィルタ(apply_outlier_filters())が1方式
    (target_yield)を実際に除外するケースで、その方式・除外理由(exclusion_
    detail相当)がbuy_score_input_facts["valuation_outlier_exclusions"]へ
    正しく保存されることを確認する。Recommendation.valuation_methods自体は
    外れ値フィルタ適用前のオブジェクトのため、この方式のexclusion_reasonは
    Noneのままであること(=valuation_methodsからは復元できないこと)も
    あわせて確認する。
    """
    outcome = _analyze(monkeypatch, _OUTLIER_EXCLUSION_STOCK)
    rec = outcome.recommendation
    assert rec is not None
    facts = rec.buy_score_input_facts
    assert facts is not None

    exclusions = facts["valuation_outlier_exclusions"]
    assert isinstance(exclusions, list)
    assert len(exclusions) == 1
    assert exclusions[0]["method"] == "target_yield"
    assert "外れ値" in exclusions[0]["message"]
    assert exclusions[0]["actual_value"] is not None
    assert exclusions[0]["reference_value"] is not None

    # Recommendation.valuation_methods(外れ値フィルタ適用前)では、この方式の
    # exclusion_reasonがNoneのままであることを確認する(=このフィールドからは
    # 外れ値除外の事実を復元できないことの実証)。
    target_yield_method = next(m for m in rec.valuation_methods if m.method == "target_yield")
    assert target_yield_method.exclusion_reason is None


def test_no_valuation_anchor_reason_captures_valuation_dispersion_too_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """必須テスト1・2: 実際にBuySignalServiceを通し、標準方式(target_yield/
    per/pbr)は3件とも個別には有効(outlier filterでも除外されない)だが、
    方式間の乖離がauto_buy_blockを超えてvaluation_anchorがNoneになる
    (=BuyDecisionReason.code="NO_VALUATION_ANCHOR"が発火する)ケースで、
    buy_score_input_facts["no_valuation_anchor_reason"]へ直接原因
    (VALUATION_DISPERSION_TOO_HIGH)が、判定時点の実測値(dispersion_ratio)・
    実際に使用した基準値(auto_buy_block)ごと構造化して保存されることを
    確認する。"""
    outcome = _analyze(monkeypatch, _DISPERSION_STOCK)
    rec = outcome.recommendation
    assert rec is not None
    assert any(r.code == "NO_VALUATION_ANCHOR" for r in rec.buy_decision_reasons)
    assert rec.valuation_anchor is None

    facts = rec.buy_score_input_facts
    assert facts is not None
    reason = facts["no_valuation_anchor_reason"]
    assert isinstance(reason, dict)
    assert reason["code"] == "VALUATION_DISPERSION_TOO_HIGH"
    assert reason["actual_value"] is not None
    assert float(reason["actual_value"]) > 2.0
    assert reason["threshold_value"] == "2.0"

    # 標準3方式はいずれも個別には有効であり(exclusion_reasonが無い)、この
    # 事実だけからは方式間乖離が原因だったことを復元できない(=表示層が新規
    # スナップショットを参照する必要があることの実証)。
    for method_name in ("target_yield", "per", "pbr"):
        method = next(m for m in rec.valuation_methods if m.method == method_name)
        assert method.exclusion_reason is None


# ===== 再々コードレビュー対応(2026-08、JST暦日境界修正・指摘4):
# in_trade_cooldown判定(cooldown_until_date比較)とWatchStateService.
# evaluate_and_update()への「当日」がJST暦日基準になっていることの回帰。
# evaluate_and_update()自体の呼び出し有無はbuy_actionの値に関わらず
# in_trade_cooldown(cooldown_until_dateとの比較)だけで決まるため、
# WatchStateService.evaluate_and_update()をspyし呼び出し有無で検証する
# (実際にNEAR BUY監視が開始されるかどうかの判定条件には依存しない)。


def _analyze_with_cooldown_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    fx: _StockFixture,
    now: dt.datetime,
    cooldown_until_date: dt.date,
) -> list[dt.date]:
    import dataclasses

    from jstock_advisor.domain.entities.holdings_snapshot import HoldingsSnapshotEntry
    from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
    from jstock_advisor.infrastructure.local_repository.holdings_snapshot_repository import (
        HoldingsSnapshotRepository,
    )
    from jstock_advisor.services.watch_state_service import WatchStateService

    # data_fetched_atをnowに揃える(このテストの関心事(cooldown判定のJST基準日)とは
    # 無関係なデータ鮮度ゲートが、_NOWから離れたnowにより誤って発火しないようにする)。
    snapshot = dataclasses.replace(_build_snapshot(fx), data_fetched_at=now)
    monkeypatch.setattr(service_module, "build_stock_snapshot", lambda *a, **kw: (snapshot, None))

    calls: list[dt.date] = []
    original_evaluate_and_update = WatchStateService.evaluate_and_update

    def _spy_evaluate_and_update(self, *args, **kwargs):
        calls.append(kwargs["today"])
        return original_evaluate_and_update(self, *args, **kwargs)

    monkeypatch.setattr(WatchStateService, "evaluate_and_update", _spy_evaluate_and_update)

    holdings_snapshot_repo = HoldingsSnapshotRepository(store_dir=tmp_path)
    holdings_snapshot_repo.upsert(
        HoldingsSnapshotEntry(
            owner=DEFAULT_OWNER,
            holding_id=build_holding_id(DEFAULT_OWNER, fx.stock_code),
            stock_code=fx.stock_code,
            shares=100,
            average_purchase_price=Decimal("1000"),
            recorded_at=cooldown_until_date - dt.timedelta(days=10),
            cooldown_until_date=cooldown_until_date,
            active_holding=True,
        )
    )
    service = BuySignalService(
        providers=_providers(),
        config=_CONFIG,
        business_calendar=_CALENDAR,
        holdings_snapshot_repository=holdings_snapshot_repo,
    )
    service.analyze(fx.stock_code, now, RecommendationType.BUY)
    return calls


def test_in_trade_cooldown_uses_jst_business_date_not_utc(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """指摘4回帰: cooldown_until_date=2026-08-20の銘柄について、
    2026-08-20 23:30 UTC(=2026-08-21 08:30 JST)時点の評価では、
    JST暦日(2026-08-21)がcooldown_until_dateを超えているため、既に解除済みと
    判定されWatchStateService.evaluate_and_update()が呼ばれること(UTC暦日
    (2026-08-20)のままであれば誤ってまだクールダウン中と判定され、呼ばれない)。
    """
    now = dt.datetime(2026, 8, 20, 23, 30, tzinfo=dt.UTC)
    calls = _analyze_with_cooldown_entry(monkeypatch, tmp_path, _TACHI_S, now, dt.date(2026, 8, 20))
    assert calls == [dt.date(2026, 8, 21)]


def test_in_trade_cooldown_still_blocks_watch_state_within_jst_business_date(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """指摘4回帰(対照ケース): cooldown_until_date=2026-08-21の銘柄について、
    同じく2026-08-20 23:30 UTC(=2026-08-21 08:30 JST)時点では、JST暦日
    (2026-08-21)がまだcooldown_until_date以下のため、引き続きクールダウン中と
    判定されWatchStateService.evaluate_and_update()が呼ばれないこと。"""
    now = dt.datetime(2026, 8, 20, 23, 30, tzinfo=dt.UTC)
    calls = _analyze_with_cooldown_entry(monkeypatch, tmp_path, _TACHI_S, now, dt.date(2026, 8, 21))
    assert calls == []


# --- Issue #22 Phase 3.5(2026-08-28): 観測用snapshotの保存 -------------------


def test_phase35_observation_snapshot_stored_with_schema_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3.5の観測用snapshotが正式schema version("v1")付きで、判定時点値
    のまま保存されることを確認する。このキーを持たない既存レコードは
    LEGACY_UNVERSIONEDとして扱う(キー数から世代を推測しない)。"""
    outcome = _analyze(monkeypatch, _NIHON_SHINYAKU)
    rec = outcome.recommendation
    assert rec is not None
    facts = rec.buy_score_input_facts
    assert facts is not None

    assert facts["buy_score_input_facts_schema_version"] == "v1"
    # Common Quality候補の本来値(fixtureではいずれも未設定=判定時点の事実)
    assert facts["net_income"] is None
    assert facts["is_deficit"] is False
    assert facts["is_debt_excess"] is False
    assert facts["latest_operating_cashflow"] is None
    assert facts["trailing_eps"] is None
    # 時系列(fixtureは空系列。空でもキー自体は必ず保存される)
    assert facts["operating_income_periods"] == []
    assert facts["operating_cashflow_periods"] == []
    assert facts["operating_cf_positive_streak"] == {
        "streak": 0,
        "periods_available": 0,
        "latest_period_type": None,
        "recent_periods_source": "UNAVAILABLE",
    }
    # EPS系列: source/coverage_limitationがデータ自体に明示される
    eps_payload = facts["historical_valuation_eps_periods"]
    assert eps_payload["source"] == "historical_valuations"
    assert eps_payload["coverage_limitation"] == "VALUATION_DATA_DEPENDENT"
    assert eps_payload["periods"] == []  # fixtureのhistorical_valuationsはeps=None
    # 割安度4カテゴリ明細と7component状況
    assert len(facts["undervaluation_categories"]) == 4
    assert set(facts["component_states"].keys()) == {
        "total_yield_attractiveness",
        "dividend_sustainability",
        "financial_health",
        "undervaluation",
        "shareholder_benefit_value",
        "earnings_stability",
        "price_stability",
    }
    # config_values_used: 割安度カテゴリ上限点とモジュール定数閾値のスナップショット
    caps_snapshot = rec.config_values_used["undervaluation_category_caps"]
    assert caps_snapshot == {
        "valuation_multiple": _CONFIG.buy_decision.undervaluation_category_caps.valuation_multiple,
        "yield": _CONFIG.buy_decision.undervaluation_category_caps.yield_,
        "fair_value": _CONFIG.buy_decision.undervaluation_category_caps.fair_value,
        "market_price_action": (
            _CONFIG.buy_decision.undervaluation_category_caps.market_price_action
        ),
    }
    thresholds_snapshot = rec.config_values_used["undervaluation_signal_thresholds"]
    assert thresholds_snapshot["drawdown_from_high_threshold_pct"] == -15.0
    assert thresholds_snapshot["price_down_despite_stable_earnings_threshold_pct"] == -10.0
    assert thresholds_snapshot["earnings_severe_decline_threshold_pct"] == -30.0


def test_phase35_recommendation_carries_default_score_model_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 3.5ではcompany_quality_score_model_versionのread/write互換のみを
    先行導入し、書き込み値は"v1"のまま("v2"の書き込みはPhase 4以降)。"""
    outcome = _analyze(monkeypatch, _NIHON_SHINYAKU)
    rec = outcome.recommendation
    assert rec is not None
    assert rec.company_quality_score_model_version == "v1"


def test_phase35_period_series_capped_at_max_periods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """時系列snapshotは直近_FACTS_SERIES_MAX_PERIODS(8)期のみ保存される
    (providerが取得期間を拡大してもpayloadが無制限に増加しないための上限)。
    streakとperiod_type/recent_periods_sourceの併存保存も確認する。"""
    import dataclasses

    from jstock_advisor.domain.entities.enums import PeriodType, RecentPeriodsSource
    from jstock_advisor.domain.financial_series import FinancialPeriodValue

    cf_periods = [
        FinancialPeriodValue(
            value=Decimal("50") if i != 9 else Decimal("-10"),  # 最古期のみ赤字
            period_end=dt.date(2026 - i, 3, 31),
            period_type=PeriodType.ANNUAL,
        )
        for i in range(10)  # 10期分(上限8を超える)を用意
    ]
    snapshot = _build_snapshot(_NIHON_SHINYAKU)
    snapshot = dataclasses.replace(
        snapshot,
        quarterly_operating_cashflow_periods=cf_periods,
        # FinancialSummaryはPydanticモデル(ImmutableSnapshot)のためmodel_copyを使う
        financial=snapshot.financial.model_copy(
            update={"recent_periods_source": RecentPeriodsSource.ANNUAL_FALLBACK}
        ),
    )
    monkeypatch.setattr(
        service_module, "build_stock_snapshot", lambda *a, **kw: (snapshot, None)
    )
    service = BuySignalService(providers=_providers(), config=_CONFIG, business_calendar=_CALENDAR)
    outcome = service.analyze(_NIHON_SHINYAKU.stock_code, _NOW, RecommendationType.BUY)
    rec = outcome.recommendation
    assert rec is not None
    facts = rec.buy_score_input_facts
    assert facts is not None

    series = facts["operating_cashflow_periods"]
    assert len(series) == 8  # 10期 -> 直近8期のみ
    assert series[0]["period_end"] == "2019-03-31"  # 最古2期(2017/2018)は切り捨て
    assert series[-1]["period_end"] == "2026-03-31"
    assert all(entry["period_type"] == "ANNUAL" for entry in series)

    streak_payload = facts["operating_cf_positive_streak"]
    # 最古期(2017-03-31)のみ赤字 -> 直近から9期連続黒字(streak自体は全期間で計算)
    assert streak_payload["streak"] == 9
    assert streak_payload["periods_available"] == 10
    assert streak_payload["latest_period_type"] == "ANNUAL"
    assert streak_payload["recent_periods_source"] == "ANNUAL_FALLBACK"


def test_phase35_suppression_is_reason_code_not_state() -> None:
    """抑止(severe_earnings_decline)はstateではなくreason_codeとして保存される
    (stateの語彙はEVALUATED/NOT_EVALUATED/NOT_APPLICABLEの3種に統一)。

    正式な観測仕様として、抑止は以下の2形式を区別する(v1の
    compute_undervaluation_signals()の実際の挙動と一致した定義。
    コードレビューPASS_WITH_CONDITIONS対応):
      形式1: value=False + SUPPRESSED_*
        入力から本来のbool評価が可能だったが、上位ルールによりFalseへ
        強制された(drawdown_from_52w_high / below_fair_value)
      形式2: value=None + SUPPRESSED_*
        必要入力自体は揃っていたが、上位ルールによりシグナル評価そのものを
        実施しなかった(price_down_despite_stable_earnings)
    reason_codeが無いvalue=Noneは「必要入力の不足による判定不能」であり、
    上記2形式(抑止)とは区別される。"""
    from jstock_advisor.domain.scoring.score import UndervaluationSignals
    from jstock_advisor.domain.scoring.undervaluation_categories import (
        build_undervaluation_category_details,
    )
    from jstock_advisor.services.buy_signal_service import (
        _serialize_undervaluation_categories,
    )

    # severe_earnings_decline時のcompute_undervaluation_signals()出力を再現:
    # below_fair_valueは一度bool評価された後にFalseへ強制(形式1)、
    # price_down_despite_stable_earningsは評価式自体へ入らずNoneのまま(形式2)
    signals = UndervaluationSignals(
        per_below_median=True,
        below_fair_value=False,
        price_down_despite_stable_earnings=None,
    )
    details = build_undervaluation_category_details(
        signals, _CONFIG.buy_decision.undervaluation_category_caps
    )
    payload = _serialize_undervaluation_categories(
        details,
        suppressed_signal_reasons={
            "below_fair_value": "SUPPRESSED_BY_SEVERE_EARNINGS_DECLINE",
            "price_down_despite_stable_earnings": "SUPPRESSED_BY_SEVERE_EARNINGS_DECLINE",
        },
    )
    by_category = {entry["category"]: entry for entry in payload}

    fair_value = by_category["fair_value"]
    # 【形式1】value=False + SUPPRESSED_*: bool評価が可能だったがFalseへ強制。
    # 抑止されてもstateはEVALUATED(値Falseとして評価に使われた事実)のまま
    assert fair_value["state"] == "EVALUATED"
    assert "SUPPRESSED_BY_SEVERE_EARNINGS_DECLINE" in fair_value["reason_codes"]
    assert fair_value["signal_results"]["below_fair_value"] == {
        "value": False,
        "reason_code": "SUPPRESSED_BY_SEVERE_EARNINGS_DECLINE",
    }

    market = by_category["market_price_action"]
    # 【形式2】value=None + SUPPRESSED_*: 必要入力は揃っていたが、上位ルール
    # によりシグナル評価そのものを実施しなかった(Falseへの強制ではない)
    assert market["signal_results"]["price_down_despite_stable_earnings"] == {
        "value": None,
        "reason_code": "SUPPRESSED_BY_SEVERE_EARNINGS_DECLINE",
    }
    # 【抑止ではないNone】必要入力の不足による判定不能 -> reason_codeなし。
    # 形式2(value=None+SUPPRESSED_*)とはreason_codeの有無で区別される
    assert market["signal_results"]["drawdown_from_52w_high"] == {
        "value": None,
        "reason_code": None,
    }

    # 抑止と無関係なカテゴリにはSUPPRESSEDが付かない
    vm = by_category["valuation_multiple"]
    assert vm["state"] == "EVALUATED"
    assert "SUPPRESSED_BY_SEVERE_EARNINGS_DECLINE" not in vm["reason_codes"]
    # 全カテゴリでstateは3値語彙のみ
    assert all(
        entry["state"] in {"EVALUATED", "NOT_EVALUATED", "NOT_APPLICABLE"}
        for entry in payload
    )


def test_phase35_v1_score_and_actions_unchanged_by_observation_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """観測用フィールドの追加がv1の判定結果へ一切影響しないことの回帰。
    既存テスト(§21の5銘柄回帰等)が判定結果自体を固定しているため、ここでは
    「観測フィールドを除いた既存facts/score構造がそのまま維持されている」
    ことを確認する。"""
    outcome = _analyze(monkeypatch, _NIHON_SHINYAKU)
    rec = outcome.recommendation
    assert rec is not None
    # 既存キーが従来どおりの形式で残っている(観測キー追加による破壊なし)
    facts = rec.buy_score_input_facts
    assert facts is not None
    assert facts["forecast_eps"] == str(_NIHON_SHINYAKU.forecast_eps)
    assert isinstance(facts["quarterly_operating_incomes"], list)
    assert all(isinstance(v, str) for v in facts["quarterly_operating_incomes"])
    # score_breakdownの7component構造・合計は不変
    assert rec.score_breakdown is not None
    assert rec.company_quality_score == rec.score_breakdown.total


def test_phase35_no_suppression_reason_codes_in_normal_case() -> None:
    """severe declineでない通常ケースでは、いかなるsignal_resultsにも
    SUPPRESSED_*が付かず、reason_codeはすべてNoneのまま(保存内容不変)。
    value=Noneは「必要入力の不足による判定不能」のみを意味する。"""
    from jstock_advisor.domain.scoring.score import UndervaluationSignals
    from jstock_advisor.domain.scoring.undervaluation_categories import (
        build_undervaluation_category_details,
    )
    from jstock_advisor.services.buy_signal_service import (
        _serialize_undervaluation_categories,
    )

    signals = UndervaluationSignals(
        per_below_median=True,
        below_fair_value=False,  # 通常の評価結果としてのFalse(強制ではない)
        drawdown_from_52w_high=None,  # 入力不足による判定不能
    )
    details = build_undervaluation_category_details(
        signals, _CONFIG.buy_decision.undervaluation_category_caps
    )
    payload = _serialize_undervaluation_categories(details, suppressed_signal_reasons={})

    for entry in payload:
        assert "SUPPRESSED_BY_SEVERE_EARNINGS_DECLINE" not in entry["reason_codes"]
        for signal in entry["signal_results"].values():
            assert signal["reason_code"] is None
    by_category = {entry["category"]: entry for entry in payload}
    # 通常の評価結果としてのFalseにはreason_codeが付かない(形式1と区別される)
    assert by_category["fair_value"]["signal_results"]["below_fair_value"] == {
        "value": False,
        "reason_code": None,
    }

def test_issue23_data_age_business_days_uses_jst_calendar_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #23: buy_signal_service側のデータ鮮度計算(data_age_business_days)も
    JPX営業日計算の両端をJST暦日で行う。fetched=JST月曜23:00(UTC月曜14:00)、
    now=JST火曜08:30(UTC月曜23:30)ならJST基準で1営業日(修正前はUTC暦日同士で
    0営業日と数えていた)。Phase 3.5の観測snapshotには判定に実際に使用した値が
    そのまま保存される。"""
    import dataclasses

    fetched = dt.datetime(2026, 8, 3, 14, 0, tzinfo=dt.UTC)  # JST 08-03(月)23:00
    now = dt.datetime(2026, 8, 3, 23, 30, tzinfo=dt.UTC)  # JST 08-04(火)08:30
    snapshot = _build_snapshot(_TACHI_S)
    snapshot = dataclasses.replace(snapshot, data_fetched_at=fetched)
    monkeypatch.setattr(
        service_module, "build_stock_snapshot", lambda *a, **kw: (snapshot, None)
    )
    service = BuySignalService(providers=_providers(), config=_CONFIG, business_calendar=_CALENDAR)
    outcome = service.analyze(_TACHI_S.stock_code, now, RecommendationType.BUY)
    rec = outcome.recommendation
    assert rec is not None
    facts = rec.buy_score_input_facts
    assert facts is not None
    assert facts["data_age_business_days"] == 1


# --- Issue #53 Phase B2: 開示情報の取得可否によるBUY判定ポリシー ---------------
# 「開示リスクを検出した」と「開示情報を調査できなかった」を混同しないこと。


def _analyze_with_disclosure(
    monkeypatch: pytest.MonkeyPatch,
    fx: _StockFixture,
    availability: DisclosureAvailability,
    unavailable_reason: DisclosureUnavailableReason | None = None,
    disclosure_risk_keywords_found: list[str] | None = None,
) -> service_module.BuyAnalysisOutcome:
    snapshot = _build_snapshot(
        fx,
        disclosure_availability=availability,
        disclosure_unavailable_reason=unavailable_reason,
        disclosure_risk_keywords_found=disclosure_risk_keywords_found,
    )
    monkeypatch.setattr(
        service_module, "build_stock_snapshot", lambda *a, **kw: (snapshot, None)
    )
    service = BuySignalService(providers=_providers(), config=_CONFIG, business_calendar=_CALENDAR)
    return service.analyze(fx.stock_code, _NOW, RecommendationType.BUY)


def test_buy_available_empty_disclosure_is_not_excluded_for_disclosure_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AVAILABLE + 開示0件は「開示リスクなし」。開示を理由に除外されない。"""
    outcome = _analyze_with_disclosure(
        monkeypatch, _NIHON_SHINYAKU, DisclosureAvailability.AVAILABLE
    )

    assert outcome.buy_action is not BuyAction.DATA_INSUFFICIENT
    assert not any("開示" in reason for reason in outcome.exclusion_reasons)


def test_buy_available_risky_disclosure_is_excluded_as_disclosure_risk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AVAILABLE + リスクキーワード検出は従来どおり開示リスクで除外される。"""
    outcome = _analyze_with_disclosure(
        monkeypatch,
        _NIHON_SHINYAKU,
        DisclosureAvailability.AVAILABLE,
        disclosure_risk_keywords_found=["上場廃止"],
    )

    assert outcome.buy_action == BuyAction.EXCLUDED
    assert any("リスクキーワード" in reason for reason in outcome.exclusion_reasons)
    # 「調査できなかった」ではなく「検出した」ため、評価不能にはしない
    assert outcome.buy_action is not BuyAction.DATA_INSUFFICIENT


def test_buy_unavailable_disclosure_is_data_insufficient_not_disclosure_risk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNAVAILABLEは評価不能(DATA_INSUFFICIENT)。開示リスク検出とは別物。"""
    outcome = _analyze_with_disclosure(
        monkeypatch,
        _NIHON_SHINYAKU,
        DisclosureAvailability.UNAVAILABLE,
        unavailable_reason=DisclosureUnavailableReason.TEMPORARY_FAILURE,
    )

    assert outcome.buy_action == BuyAction.DATA_INSUFFICIENT
    assert outcome.buy_action not in BUY_FAMILY_ACTIONS
    assert outcome.recommendation is None
    assert outcome.ranking_group is None
    # 「リスクキーワードを検出した」という表現にはならない
    assert not any("リスクキーワード" in reason for reason in outcome.exclusion_reasons)
    assert outcome.data_error is not None
    assert "取得できなかった" in outcome.data_error


def test_buy_unavailable_disclosure_does_not_pass_as_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNAVAILABLEを「問題開示なし」として通過させない。"""
    outcome = _analyze_with_disclosure(
        monkeypatch,
        _NIHON_SHINYAKU,
        DisclosureAvailability.UNAVAILABLE,
        unavailable_reason=DisclosureUnavailableReason.NOT_CONFIGURED,
    )

    assert outcome.buy_action not in BUY_FAMILY_ACTIONS
    assert outcome.recommendation is None


# --- Issue #54 Phase B-1(2026-08-29): 業種分類canonical観測(shadow) -----------
#
# 本節のテストが固定する最重要契約は「**観測はBUY判定を一切変えない**」ことである。
# CYCLICAL/DEFENSIVEやREIT除外といった死んだ判定の復活は Phase B-2 の範囲であり、
# B-1では観測のみを行う(適正価格と対象母集団が変わるため、観測結果を確認せずに
# 復活させない)。


def _jpx_source(entries: dict[str, JpxIndustryEntry]) -> JpxIndustrySource:
    """一覧を読み込み済みのJpxIndustrySource(キャッシュ読み取りを行わない)。

    空dictを渡した場合は「一覧は読めたが当該銘柄が無い」= `NOT_FOUND` になる。
    「一覧そのものを読めない」= `SOURCE_UNAVAILABLE` は `_UnavailableJpxSource` を使う。
    """
    source = JpxIndustrySource()
    source._map = entries  # noqa: SLF001 - テスト用に読み込み済み状態を直接構成する
    return source


class _UnavailableJpxSource(JpxIndustrySource):
    """JPXキャッシュを読めない状態のソース(例外は送出しない)。"""

    def lookup(self, stock_code: str) -> JpxIndustryLookup:
        return JpxIndustryLookup(status=JpxLookupStatus.SOURCE_UNAVAILABLE)


def _analyze_with_jpx(
    monkeypatch: pytest.MonkeyPatch,
    fx: _StockFixture,
    jpx_industry_source: JpxIndustrySource | None,
) -> service_module.BuyAnalysisOutcome:
    snapshot = _build_snapshot(fx)
    monkeypatch.setattr(service_module, "build_stock_snapshot", lambda *a, **kw: (snapshot, None))
    service = BuySignalService(
        providers=_providers(),
        config=_CONFIG,
        business_calendar=_CALENDAR,
        jpx_industry_source=jpx_industry_source,
    )
    return service.analyze(fx.stock_code, _NOW, RecommendationType.BUY)


_JPX_ENTRY = JpxIndustryEntry(
    industry_33_code="3050",
    industry_33_name="医薬品",
    market_segment="プライム（内国株式）",
)


def test_canonical_industry_observation_records_jpx_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JPXで解決できた場合、33業種コードと証券種別が観測として保存される。"""
    outcome = _analyze_with_jpx(
        monkeypatch,
        _NIHON_SHINYAKU,
        _jpx_source({_NIHON_SHINYAKU.stock_code: _JPX_ENTRY}),
    )
    rec = outcome.recommendation
    assert rec is not None
    facts = rec.buy_score_input_facts
    assert facts is not None

    observation = facts["canonical_industry_observation"]
    assert observation["canonical_industry_33_code"] == "3050"
    assert observation["canonical_industry_33_name"] == "医薬品"
    assert observation["canonical_security_type"] == "COMMON_STOCK"
    assert observation["canonical_source"] == "JPX_TSE33"
    assert observation["jpx_lookup_status"] == "RESOLVED"


def test_canonical_industry_observation_records_unresolved_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JPXで解決できない銘柄はUNKNOWNのまま記録し、provider値で埋めない。

    Phase B-2の判断材料は「JPX解決率」であるため、解決できなかったことを
    yfinanceの値で塗りつぶしてはならない。
    """
    outcome = _analyze_with_jpx(monkeypatch, _NIHON_SHINYAKU, _jpx_source({}))
    rec = outcome.recommendation
    assert rec is not None
    facts = rec.buy_score_input_facts
    assert facts is not None

    observation = facts["canonical_industry_observation"]
    assert observation["canonical_industry_33_code"] is None
    assert observation["canonical_security_type"] == "UNKNOWN"
    assert observation["canonical_source"] == "YFINANCE_FALLBACK"
    # 「一覧は読めたが当該銘柄が無い」ことを、読めなかった場合と区別して記録する。
    assert observation["jpx_lookup_status"] == "NOT_FOUND"
    # 観測のために見た値はそのまま残す(canonicalへは昇格させない)。
    assert observation["provider_sector"] == _NIHON_SHINYAKU.sector
    assert observation["provider_industry"] == _NIHON_SHINYAKU.industry


def test_canonical_industry_observation_records_existing_classifier_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既存4分類器が同一入力に対して実際に返した値を、是正せず並記する。

    yfinanceは市場区分を提供せず(`market_segment=None`)、`security_type`も
    既定値"STOCK"のままであるため、REIT除外が到達不能である現状もここに記録される。
    """
    outcome = _analyze_with_jpx(
        monkeypatch,
        _NIHON_SHINYAKU,
        _jpx_source({_NIHON_SHINYAKU.stock_code: _JPX_ENTRY}),
    )
    rec = outcome.recommendation
    assert rec is not None
    facts = rec.buy_score_input_facts
    assert facts is not None

    observation = facts["canonical_industry_observation"]
    assert observation["financial_industry_classification"] in {
        classification.value for classification in IndustryClassification
    }
    assert observation["buy_industry_sector"] in {sector.value for sector in BuyIndustrySector}
    assert observation["profit_taking_industry_sector"] in {
        sector.value for sector in ProfitTakingIndustrySector
    }
    assert observation["stock_type_cyclical_or_defensive"] == []
    assert observation["provider_security_type"] == "STOCK"
    assert observation["provider_market_segment"] is None


def test_canonical_industry_observation_does_not_change_buy_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**shadow性の証明**: JPXで解決できてもできなくても判定結果が変わらない。

    B-1で判定が動いてしまうと、Phase B-2で「観測結果を見てから復活させる」という
    段階分けが成立しない。
    """
    resolved = _analyze_with_jpx(
        monkeypatch,
        _NIHON_SHINYAKU,
        _jpx_source({_NIHON_SHINYAKU.stock_code: _JPX_ENTRY}),
    )
    unresolved = _analyze_with_jpx(monkeypatch, _NIHON_SHINYAKU, _jpx_source({}))

    assert resolved.buy_action == unresolved.buy_action
    assert resolved.ranking_group == unresolved.ranking_group
    assert resolved.exclusion_reasons == unresolved.exclusion_reasons
    assert resolved.recommendation is not None
    assert unresolved.recommendation is not None
    assert resolved.recommendation.total_score == unresolved.recommendation.total_score
    assert (
        resolved.recommendation.fair_value_at_recommendation
        == unresolved.recommendation.fair_value_at_recommendation
    )
    assert resolved.recommendation.buy_prices == unresolved.recommendation.buy_prices
    assert resolved.recommendation.score_breakdown == unresolved.recommendation.score_breakdown
    assert resolved.recommendation.reasons == unresolved.recommendation.reasons


def test_reit_security_type_does_not_change_buy_decision_in_phase_b1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REITと観測されてもB-1では除外しない(除外の復活はPhase B-2)。"""
    reit_entry = JpxIndustryEntry(
        industry_33_code="8050",
        industry_33_name="不動産業",
        market_segment="REIT・ベンチャーファンド・カントリーファンド・インフラファンド",
    )
    reit = _analyze_with_jpx(
        monkeypatch, _NIHON_SHINYAKU, _jpx_source({_NIHON_SHINYAKU.stock_code: reit_entry})
    )
    baseline = _analyze_with_jpx(monkeypatch, _NIHON_SHINYAKU, _jpx_source({}))

    assert reit.recommendation is not None
    observation = reit.recommendation.buy_score_input_facts["canonical_industry_observation"]
    assert observation["canonical_security_type"] == "REIT"
    # 観測はREITだが、判定は従来どおり。
    assert reit.buy_action == baseline.buy_action
    assert reit.exclusion_reasons == baseline.exclusion_reasons


def test_jpx_source_failure_does_not_break_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**JPXキャッシュを読めなくてもBUY判定は一切変わらない**。

    B-1はshadow observationであり、観測のためにBUYパイプラインを止めてはならない
    (#59 の provider failure contract は外部provider取得に対する契約であり、
    ローカルcacheを読む観測専用sourceの失敗をBUY失敗へ昇格させる必要はない)。
    """
    failing = _analyze_with_jpx(monkeypatch, _NIHON_SHINYAKU, _UnavailableJpxSource())
    baseline = _analyze_with_jpx(monkeypatch, _NIHON_SHINYAKU, _jpx_source({}))

    assert failing.buy_action == baseline.buy_action
    assert failing.ranking_group == baseline.ranking_group
    assert failing.exclusion_reasons == baseline.exclusion_reasons
    assert failing.data_error == baseline.data_error
    assert failing.recommendation is not None
    assert baseline.recommendation is not None
    assert failing.recommendation.total_score == baseline.recommendation.total_score
    assert (
        failing.recommendation.fair_value_at_recommendation
        == baseline.recommendation.fair_value_at_recommendation
    )
    assert failing.recommendation.buy_prices == baseline.recommendation.buy_prices


def test_source_unavailable_is_recorded_distinctly_from_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**観測データ上で「一覧に無い」と「一覧を読めない」が別値になること**。

    ここが潰れると、JPX解決率の低さが銘柄側の事情なのかキャッシュ障害なのかを
    区別できず、Phase B-2 の実施可否を判断できない。
    """
    unavailable = _analyze_with_jpx(monkeypatch, _NIHON_SHINYAKU, _UnavailableJpxSource())
    not_found = _analyze_with_jpx(monkeypatch, _NIHON_SHINYAKU, _jpx_source({}))

    assert unavailable.recommendation is not None
    assert not_found.recommendation is not None
    unavailable_obs = unavailable.recommendation.buy_score_input_facts[
        "canonical_industry_observation"
    ]
    not_found_obs = not_found.recommendation.buy_score_input_facts[
        "canonical_industry_observation"
    ]

    assert unavailable_obs["jpx_lookup_status"] == "SOURCE_UNAVAILABLE"
    assert not_found_obs["jpx_lookup_status"] == "NOT_FOUND"
    # canonical_sourceは同値であり、これだけでは区別できない。
    assert unavailable_obs["canonical_source"] == not_found_obs["canonical_source"]


def test_all_jpx_lookup_states_yield_identical_buy_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #116: 3つのshadow状態すべてでBUY判定が完全に一致することを固定する。

    #116 は infra 配線を直して `jpx_lookup_status` を
    `SOURCE_UNAVAILABLE` 一色から実際の分布へ変える修正であり、
    **観測精度だけを変え、判定は変えない**。本テストはその不変条件を
    `RESOLVED` / `NOT_FOUND` / `SOURCE_UNAVAILABLE` の3値で同時に固定する
    (既存テストは2値ずつの比較であり、3値同時かつ `screening_passed` を含む
    形では固定されていなかった)。
    """
    outcomes = {
        "RESOLVED": _analyze_with_jpx(
            monkeypatch,
            _NIHON_SHINYAKU,
            _jpx_source({_NIHON_SHINYAKU.stock_code: _JPX_ENTRY}),
        ),
        "NOT_FOUND": _analyze_with_jpx(monkeypatch, _NIHON_SHINYAKU, _jpx_source({})),
        "SOURCE_UNAVAILABLE": _analyze_with_jpx(
            monkeypatch, _NIHON_SHINYAKU, _UnavailableJpxSource()
        ),
    }

    # 観測値としては3状態が区別されている(潰れていない)ことを先に確認する。
    statuses = {
        name: outcome.recommendation.buy_score_input_facts["canonical_industry_observation"][
            "jpx_lookup_status"
        ]
        for name, outcome in outcomes.items()
        if outcome.recommendation is not None
    }
    assert statuses == {
        "RESOLVED": "RESOLVED",
        "NOT_FOUND": "NOT_FOUND",
        "SOURCE_UNAVAILABLE": "SOURCE_UNAVAILABLE",
    }

    baseline = outcomes["RESOLVED"]
    assert baseline.recommendation is not None
    for name, outcome in outcomes.items():
        assert outcome.recommendation is not None, name
        assert outcome.screening_passed == baseline.screening_passed, name
        assert outcome.buy_action == baseline.buy_action, name
        assert outcome.ranking_group == baseline.ranking_group, name
        assert outcome.exclusion_reasons == baseline.exclusion_reasons, name
        assert outcome.data_error == baseline.data_error, name
        assert outcome.recommendation.total_score == baseline.recommendation.total_score, name
        assert outcome.recommendation.buy_prices == baseline.recommendation.buy_prices, name
        assert (
            outcome.recommendation.fair_value_at_recommendation
            == baseline.recommendation.fair_value_at_recommendation
        ), name
        assert (
            outcome.recommendation.score_breakdown == baseline.recommendation.score_breakdown
        ), name


def test_observation_key_is_additive_and_does_not_bump_facts_schema_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """観測キーの追加はoptional key追加であり、既存レコードとの互換性を壊さない。

    `buy_score_input_facts` は判定に使わない観測用snapshotであり、消費者
    (calibration dataset等)は必要なキーを個別に取り出す。したがって
    schema versionの引き上げもbackfillも不要である
    (`FACTS_SCHEMA_VERSION` の方針コメント参照)。この判断をテストで固定する。
    """
    outcome = _analyze_with_jpx(monkeypatch, _NIHON_SHINYAKU, _jpx_source({}))
    rec = outcome.recommendation
    assert rec is not None
    facts = rec.buy_score_input_facts
    assert facts is not None

    assert facts["buy_score_input_facts_schema_version"] == "v1"
    # 観測キーを取り除いた状態(=既存レコード)でも、他のキーは何も変わらない。
    legacy_view = {k: v for k, v in facts.items() if k != "canonical_industry_observation"}
    assert "canonical_industry_observation" not in legacy_view
    assert legacy_view["buy_score_input_facts_schema_version"] == "v1"
