"""複数サービスで共有する銘柄分析スナップショット構築処理。

buy_signal_service / profit_taking_service / sell_signal_service はいずれも
現在株価・財務・配当・優待・適正価格といった同じ基礎データを必要とするため、
取得と適正価格算出をここに集約する。データが取得できない場合はエラーメッセージを
返し、推測で補完しない。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.classification.stock_type import classify_stock_type
from jstock_advisor.domain.entities.classification import StockTypeClassification
from jstock_advisor.domain.entities.common import (
    BenefitUtilityCoefficients,
    DataSourceReference,
)
from jstock_advisor.domain.entities.earnings_surprise import EarningsSurpriseResult
from jstock_advisor.domain.entities.earnings_trend import EarningsTrendResult
from jstock_advisor.domain.entities.entry_price_range import EntryPriceRangeResult
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EarningsDateStatus,
    EarningsDecisionRelevance,
    EarningsReleaseConfirmationState,
    ValuationBasis,
)
from jstock_advisor.domain.entities.environment import EnvironmentResult
from jstock_advisor.domain.entities.financial_input_provenance import (
    FinancialInputProvenance,
    FinancialValueProvenance,
    FinancialValueSourceType,
)
from jstock_advisor.domain.entities.historical_valuation import HistoricalValuationResult
from jstock_advisor.domain.entities.market_environment import MarketEnvironmentResult
from jstock_advisor.domain.entities.momentum import MomentumSnapshot
from jstock_advisor.domain.entities.sector_environment import SectorEnvironmentResult
from jstock_advisor.domain.entities.timing_score import TimingScoreResult
from jstock_advisor.domain.entities.valuation import FairValueMethodResult, FairValueRange
from jstock_advisor.domain.financial_series import (
    FinancialPeriodValue,
    build_financial_period_series,
    to_seasonally_adjusted_series,
)
from jstock_advisor.domain.jst import evaluation_date_jst, require_timezone_aware
from jstock_advisor.domain.screening.rules import (
    detect_disclosure_risk_keywords,
    detect_material_event_keywords,
)
from jstock_advisor.domain.signals.buy_signal import has_severe_earnings_decline
from jstock_advisor.domain.signals.earnings_surprise import evaluate_earnings_surprise
from jstock_advisor.domain.signals.earnings_trend import evaluate_earnings_trend
from jstock_advisor.domain.signals.earnings_window import (
    resolve_earnings_decision_relevance,
    resolve_earnings_release_confirmation,
    resolve_latest_financial_period_end,
)
from jstock_advisor.domain.signals.entry_price_range import evaluate_entry_price_range
from jstock_advisor.domain.signals.environment import evaluate_environment
from jstock_advisor.domain.signals.historical_valuation import evaluate_historical_valuation
from jstock_advisor.domain.signals.market_environment import evaluate_market_environment
from jstock_advisor.domain.signals.momentum import compute_momentum_snapshot
from jstock_advisor.domain.signals.sector_environment import evaluate_sector_environment
from jstock_advisor.domain.signals.timing_score import evaluate_timing_score
from jstock_advisor.domain.valuation.fair_value import (
    aggregate_fair_value,
    compute_dcf_price,
    compute_historical_range_price,
    compute_pbr_price,
    compute_per_price,
    compute_target_yield_price,
    median_historical_pbr,
    median_historical_per,
)
from jstock_advisor.domain.valuation.fair_value_usability import build_fair_value_range
from jstock_advisor.domain.valuation.yield_calc import (
    compute_annual_benefit_valuation,
    compute_benefit_yield_pct,
    compute_dividend_yield_pct,
    compute_total_yield_pct,
)
from jstock_advisor.interfaces.types import (
    CashflowDecomposition,
    Disclosure,
    DividendInfo,
    FinancialSummary,
    HistoricalValuation,
    PriceBar,
    ShareholderBenefit,
)
from jstock_advisor.services.provider_bundle import ProviderBundle


@dataclass(frozen=True)
class StockSnapshot:
    stock_code: str
    current_price: Decimal
    financial: FinancialSummary
    dividend: DividendInfo
    benefit: ShareholderBenefit | None
    bars: list[PriceBar]
    historical_valuations: list[HistoricalValuation]
    avg_trading_value: Decimal | None
    disclosures: list[Disclosure]
    # next_earnings_dateは検証済みの値(過去日・取得不能時はNone)。生値は
    # earnings_date_rawで別途保持する(コードレビュー対応: 明治HD事例、
    # データ提供元の更新遅延により過去日がそのまま返ってくることがあるため、
    # buy/sell/profit_takingの3消費者すべてで一元的に検証する)。
    next_earnings_date: dt.date | None
    earnings_date_status: EarningsDateStatus
    earnings_date_raw: dt.date | None
    # 次回決算までの営業日数(JST暦日基準、決算日修正デプロイ前対応で新設)。
    # buy/sell/profit_takingの3消費者が個別に再計算していたのをここへ一元化する
    # (計算元を1か所にすることで、UTC/JST境界の誤判定を1箇所の修正で解消できる)。
    business_days_to_earnings: int | None
    dividend_yield_pct: float | None
    benefit_yield_pct: float | None
    annual_benefit_value: Decimal | None
    # Issue #55 Phase B-1: Noneは「判定時点で総合利回りを確定できなかった」を表す
    # (配当データが取得できない、または優待の評価額が不明)。0.0(=確定0%)と
    # 混同してはならない。判定側はNOT_EVALUATEDとして扱い、coverageを下げる。
    total_yield_pct: float | None
    fair_value: Decimal | None
    fair_value_methods_used_count: int
    data_sources: list[DataSourceReference]
    data_fetched_at: dt.datetime
    quarterly_operating_incomes: list[Decimal]
    quarterly_operating_cashflows: list[Decimal]
    # --- 財務期間の構造化(2026-07仕様レビュー対応)。sell_signal専用に、
    # period_type(QUARTER/YTD/TTM/ANNUAL)を明示したうえで継続悪化判定を行う ---
    quarterly_operating_income_periods: list[FinancialPeriodValue]
    quarterly_operating_cashflow_periods: list[FinancialPeriodValue]
    severe_earnings_decline: bool
    disclosure_risk_keywords_found: list[str]
    material_event_keywords_found: list[str]
    cashflow_decomposition: CashflowDecomposition | None
    stock_type_classification: StockTypeClassification
    fair_value_range: FairValueRange
    momentum: MomentumSnapshot
    # --- 判定精度向上機能Phase B: Historical Valuation Score(2026-08追加) ---
    # 銘柄自身の過去PER/PBR水準に対する現在値のランクベース評価結果
    # (score/confidence/coverage/内訳を含む構造化Result)。DecisionSnapshot
    # 記録専用のShadow計測であり、BUY候補判定・保有判断スコア・旧売却判定・
    # ProfitTaking判定・LINE通知など既存の判定ロジックからは一切参照されない。
    historical_valuation: HistoricalValuationResult
    # --- 判定精度向上機能Phase B第二弾: Timing Score(2026-08追加) ---
    # momentum(上記)を基にしたモメンタムベースの技術的タイミング評価結果。
    # 同じくDecisionSnapshot記録専用のShadow計測であり、既存の判定ロジックには
    # 一切影響しない。
    timing: TimingScoreResult
    # --- 判定精度向上機能Phase C: Earnings Surprise/Trend Score(2026-08追加、
    # コードレビュー対応でv3へ再設計) ---
    # 直近確定四半期のYahoo Finance Earnings Historyが返すEPS実績/予想値の
    # 乖離(Analyst Consensus Surprise単一成分)を基にした決算サプライズ評価
    # 結果、および営業利益/営業CFトレンド+配当方向を基にした業績トレンド評価
    # 結果。いずれもDecisionSnapshot記録専用のShadow計測であり、既存の判定
    # ロジックには一切影響しない。
    earnings_surprise: EarningsSurpriseResult
    earnings_trend: EarningsTrendResult
    # --- 判定精度向上機能次フェーズSTEP2: Entry Price Range Shadow(2026-08
    # 追加) ---
    # fair_value_range.neutralを絶対上限とした4段階の目安買付価格帯。
    # DecisionSnapshot記録専用のShadow計測であり、既存のBUY候補判定・
    # entry_buy_price/standard_buy_price/strong_buy_price・保有判断スコア・
    # 旧売却判定・ProfitTaking判定・LINE通知など既存の判定ロジックからは
    # 一切参照されない。
    entry_price_range: EntryPriceRangeResult
    # --- 判定精度向上機能Phase D: Market/Sector Environment Shadow(2026-08
    # 追加) ---
    # TOPIX/所属セクターETFの地合いをそれぞれ独立に評価したShadow計測結果、
    # および両者を統合したEnvironment Composite。DecisionSnapshot記録専用で
    # あり、既存のBUY候補判定・保有判断スコア・旧売却判定・ProfitTaking判定・
    # LINE通知・Entry/Exit Price Rangeからは一切参照されない。
    market_environment: MarketEnvironmentResult
    sector_environment: SectorEnvironmentResult
    environment: EnvironmentResult
    # --- Issue #20 Phase B2-A(2026-08追加) ---
    # 判定入力financial dataのprovenance(期間・provider・観測時点・値種別)。
    # build_stock_snapshot()がsnapshot構築時点の事実からのみ組み立てる
    # (新たなprovider呼び出し・推測なし)。判定ロジックからは一切参照されない
    # 観測専用フィールドで、BUY/SELL両パイプラインがRecommendationへ転記する。
    # テスト等で手動構築されたsnapshotではNone(NOT_CAPTURED相当)になりうる。
    financial_input_provenance: FinancialInputProvenance | None = None


def build_financial_input_provenance(
    financial: FinancialSummary, dividend: DividendInfo
) -> FinancialInputProvenance:
    """snapshot構築時点の事実のみからprovenanceを組み立てる(Issue #20 B2-A)。

    新たなprovider呼び出し・推測・現在値からの補完は行わない。想定内の入力
    欠損(providerが値を提供しなかった)はavailable=False(NOT_AVAILABLE)と
    して正常に表現し、想定外のprogramming errorは握り潰さない(包括的
    try/exceptを置かない)。

    - latest_quarter_endはrecent_quarters中の最大期末日の生値。未来日ガード等の
      判定はearnings_window側(resolve_latest_financial_period_end)の既存責務の
      ままで、ここで再判定・推測しない。
    - 予想EPS/予想配当は、providerが予想の出所(会社予想か推定か)を明示しない
      ためPROVIDER_FORECAST_UNSPECIFIED(根拠なくCOMPANY_FORECASTとしない)。
    - 予想BPSは、pipelineが予想として扱う一方で実態はprovider提供のtrailing
      bookValueである可能性が実装コメント上既知であり、provider仕様として
      種別を保証できないためUNKNOWN(実績とも予想とも断定しない)。
    """
    quarter_ends = [q.quarter_end for q in financial.recent_quarters]
    financial_provider = financial.source.provider
    financial_observed_at = financial.source.fetched_at

    def _financial_forecast(value: object) -> FinancialValueProvenance:
        return FinancialValueProvenance(
            source_type=FinancialValueSourceType.PROVIDER_FORECAST_UNSPECIFIED,
            provider=financial_provider,
            observed_at=financial_observed_at,
            available=value is not None,
        )

    return FinancialInputProvenance(
        fiscal_period_end=financial.fiscal_period_end,
        fiscal_year_end_month=financial.fiscal_year_end_month,
        latest_quarter_end=max(quarter_ends) if quarter_ends else None,
        recent_periods_source=financial.recent_periods_source,
        financial_provider=financial_provider,
        financial_observed_at=financial_observed_at,
        forecast_eps_source=_financial_forecast(financial.forecast_eps),
        forecast_bps_source=FinancialValueProvenance(
            source_type=FinancialValueSourceType.UNKNOWN,
            provider=financial_provider,
            observed_at=financial_observed_at,
            available=financial.forecast_bps is not None,
        ),
        forecast_dividend_source=FinancialValueProvenance(
            source_type=FinancialValueSourceType.PROVIDER_FORECAST_UNSPECIFIED,
            provider=dividend.source.provider,
            observed_at=dividend.source.fetched_at,
            available=dividend.forecast_annual_dividend_per_share is not None,
        ),
        actual_dividend_source=FinancialValueProvenance(
            source_type=FinancialValueSourceType.ACTUAL,
            provider=dividend.source.provider,
            observed_at=dividend.source.fetched_at,
            available=dividend.actual_annual_dividend_per_share is not None,
        ),
    )


def build_stock_snapshot(
    providers: ProviderBundle,
    stock_code: str,
    now: dt.datetime,
    config: AppConfig,
    business_calendar: BusinessCalendar | None = None,
) -> tuple[StockSnapshot | None, str | None]:
    # 決算日修正デプロイ前対応: nowはtimezone-aware必須(naiveを暗黙にUTC扱いしない)。
    require_timezone_aware(now)
    calendar = business_calendar or BusinessCalendar.from_config(config.holiday_calendar)
    # 決算日関連の暦日比較は必ずJST基準で行う(UTC-awareなnowに.date()を直接呼ぶと、
    # JST 00:00-09:00の間は前日のUTC日付になり誤判定する)。
    evaluation_date = evaluation_date_jst(now)

    snap = providers.market_data.get_latest_price(stock_code)
    if snap is None:
        return None, "株価データを取得できません"

    financial = providers.financial_data.get_financial_summary(stock_code)
    if financial is None:
        return None, "財務データを取得できません"

    dividend = providers.dividend_data.get_dividend_info(
        stock_code, fiscal_year_end_month=financial.fiscal_year_end_month
    )
    if dividend is None:
        return None, (
            "配当データを取得できません"
            "(データ提供元(yfinance)から取得できなかったか、yfinanceとEDINETの配当額が"
            "株式分割等で説明できない水準まで乖離しており自動判定できないため除外しています。"
            "後者の場合、詳細はCloudWatch Logsの該当銘柄のwarningログをご確認ください)"
        )

    benefit = providers.shareholder_benefit.get_shareholder_benefit(stock_code)
    current_price = snap.close_price

    history_start = now.date() - dt.timedelta(
        days=365 * config.valuation.historical_range_method.lookback_years
    )
    history = providers.market_data.get_price_history(stock_code, history_start, now.date())
    bars = history.bars if history is not None else []

    topix_history = providers.market_data.get_benchmark_price_history(
        "TOPIX", history_start, now.date()
    )
    topix_bars = topix_history.bars if topix_history is not None else []
    sector_etf = config.momentum.sector_etf_map.get(financial.industry or "")
    sector_history = (
        providers.market_data.get_benchmark_price_history(sector_etf, history_start, now.date())
        if sector_etf
        else None
    )
    sector_bars = sector_history.bars if sector_history is not None else []

    historical_valuations = providers.financial_data.get_historical_valuation(
        stock_code, config.valuation.per_method.lookback_years_primary
    )
    avg_trading_value = providers.market_data.get_average_trading_value(stock_code, 20)
    disclosures = providers.disclosure.get_disclosures(
        stock_code, now.date() - dt.timedelta(days=30)
    )
    # 決算日の妥当性検証(コードレビュー対応: 明治HD事例)。データ提供元(yfinance等)の
    # 更新遅延により、評価日より過去の日付が「次回決算予定日」として返ってくることが
    # ある。過去日をそのまま次回決算日として使わず、検証済みの値のみをnext_earnings_date
    # へ格納する(buy/sell/profit_takingの3消費者すべてがここで一元的に検証される)。
    earnings_date_raw = providers.disclosure.get_next_earnings_date(stock_code)
    if earnings_date_raw is None:
        earnings_date_status = EarningsDateStatus.UNAVAILABLE
        next_earnings_date = None
    elif earnings_date_raw < evaluation_date:
        earnings_date_status = EarningsDateStatus.STALE_PAST_DATE
        next_earnings_date = None
    else:
        earnings_date_status = EarningsDateStatus.CONFIRMED
        next_earnings_date = earnings_date_raw
    # 次回決算までの営業日数(JST暦日基準)。ここで1回だけ計算し、buy/sell/
    # profit_takingは全てsnapshot.business_days_to_earningsを読むだけにする
    # (デプロイ前対応: 計算元の分散によるUTC/JST境界の誤判定を防止)。
    business_days_to_earnings = (
        calendar.business_days_between(evaluation_date, next_earnings_date)
        if next_earnings_date is not None
        else None
    )

    coefficients = BenefitUtilityCoefficients(
        **config.scoring.shareholder_benefit_value.utility_coefficients_default.model_dump()
    )
    dividend_yield_pct = compute_dividend_yield_pct(
        dividend.forecast_annual_dividend_per_share, current_price
    )
    # Issue #55 Phase B-1: 優待は「制度なし(寄与0で確定)」「評価可能」「評価不能(unknown)」
    # の3状態を区別する。従来は評価不能も年間評価額0円として扱われ、
    # 総合利回りが「0%と確定」してしまっていた。
    benefit_valuation = compute_annual_benefit_valuation(benefit, coefficients)
    annual_benefit_value = benefit_valuation.annual_value
    min_shares_required = benefit.min_shares_required if benefit is not None else 100
    benefit_yield_pct = compute_benefit_yield_pct(
        annual_benefit_value, min_shares_required, current_price
    )
    total_yield_pct = compute_total_yield_pct(
        dividend_yield_pct, benefit_yield_pct, benefit_state=benefit_valuation.state
    )

    target_price = compute_target_yield_price(
        dividend.forecast_annual_dividend_per_share,
        config.valuation.target_yield_method.target_dividend_yield_pct,
    )
    per_median = median_historical_per(historical_valuations)
    pbr_median = median_historical_pbr(historical_valuations)
    per_price = compute_per_price(financial.forecast_eps, per_median)
    pbr_price = compute_pbr_price(financial.forecast_bps, pbr_median)
    range_price = compute_historical_range_price(
        bars,
        now.date(),
        config.valuation.historical_range_method.lookback_years,
        config.valuation.historical_range_method.use_52_week_low,
    )
    dcf_price = compute_dcf_price(
        financial.operating_cashflow,
        financial.capital_expenditure,
        financial.shares_outstanding,
        config.valuation.dcf_method.discount_rate_pct,
        config.valuation.dcf_method.terminal_growth_rate_pct,
        config.valuation.dcf_method.projection_years,
    )
    fair_value_candidates = {
        "target_yield": target_price,
        "per": per_price,
        "pbr": pbr_price,
        "historical_range": range_price,
        "dcf": dcf_price,
    }
    fair_value_methods_used_count = sum(1 for v in fair_value_candidates.values() if v is not None)
    fair_value = aggregate_fair_value(
        fair_value_candidates,
        config.valuation.fair_value_methods.aggregation_method,
        config.valuation.fair_value_methods.method_weights,
    )

    method_confidence = {
        "target_yield": ConfidenceLevel.HIGH,
        "per": ConfidenceLevel.MEDIUM,
        "pbr": ConfidenceLevel.MEDIUM,
        "historical_range": ConfidenceLevel.MEDIUM,
        "dcf": ConfidenceLevel.MEDIUM,  # 固定割引率のためHIGHにはしない(要求仕様8節)
    }
    method_exclusion_reason = {
        "target_yield": "予想配当が取得できないため算出不可",
        "per": "予想EPSまたは過去PER中央値が取得できないため算出不可",
        "pbr": "予想BPSまたは過去PBR中央値が取得できないため算出不可",
        "historical_range": "過去株価データが取得できないため算出不可",
        "dcf": "営業CF・設備投資・発行済株式数のいずれかが取得できない、"
        "またはFCFが負のため算出不可",
    }
    fair_value_method_results = [
        FairValueMethodResult(
            method=name,
            fair_value=value,
            confidence=method_confidence[name],
            exclusion_reason=None if value is not None else method_exclusion_reason[name],
        )
        for name, value in fair_value_candidates.items()
    ]
    fair_value_range = build_fair_value_range(
        fair_value_method_results,
        config.valuation.fair_value_methods.aggregation_method,
        config.valuation.fair_value_methods.method_weights,
        config.valuation.fair_value_usability,
    )

    data_sources = [snap.source, financial.source, dividend.source]
    if benefit is not None:
        data_sources.append(benefit.source)
    data_fetched_at = min(s.fetched_at for s in data_sources)

    keywords_found = detect_disclosure_risk_keywords(
        disclosures, config.sell.disclosure_risk_keywords
    )
    material_event_keywords_found = detect_material_event_keywords(disclosures)

    period_ends = [q.quarter_end for q in financial.recent_quarters]
    raw_operating_incomes = [q.operating_income for q in financial.recent_quarters]
    raw_operating_cashflows = [q.operating_cashflow for q in financial.recent_quarters]

    # 四半期粒度のデータは直近12ヶ月移動合計(TTM)に変換し、季節性(業種特有の
    # 繁閑差)による誤検知を防ぐ。年次粒度はそのまま(恒等変換)。
    adjusted_operating_incomes = to_seasonally_adjusted_series(raw_operating_incomes, period_ends)
    adjusted_operating_cashflows = to_seasonally_adjusted_series(
        raw_operating_cashflows, period_ends
    )

    quarterly_operating_incomes = [v for v in adjusted_operating_incomes if v is not None]
    quarterly_operating_cashflows = [v for v in adjusted_operating_cashflows if v is not None]
    severe_earnings_decline = has_severe_earnings_decline(quarterly_operating_incomes)
    quarterly_operating_income_periods = build_financial_period_series(
        raw_operating_incomes, period_ends, source=financial.source.provider
    )
    quarterly_operating_cashflow_periods = build_financial_period_series(
        raw_operating_cashflows, period_ends, source=financial.source.provider
    )
    cashflow_decomposition = providers.financial_data.get_cashflow_decomposition(stock_code)
    stock_type_classification = classify_stock_type(
        financial=financial,
        dividend_yield_pct=dividend_yield_pct,
        current_price=current_price,
        quarterly_operating_incomes=quarterly_operating_incomes,
        disclosures=disclosures,
        now=now,
        config=config.stock_classification,
        data_sources=data_sources,
        dividend=dividend,
    )
    momentum_snapshot = compute_momentum_snapshot(
        bars,
        current_price,
        # コードレビュー対応(Timing Score v3): current_priceの実際のas-of日付
        # (snap.as_of_date)を渡す。get_latest_price()由来のsnapとget_price_history()
        # 由来のbarsは別Provider呼び出しであり時点一致の保証が無いため、
        # compute_momentum_snapshot()内でbars[-1].dateとの整合性を確認する。
        snap.as_of_date,
        config.momentum,
        benchmark_bars=topix_bars or None,
        sector_bars=sector_bars or None,
    )

    # 判定精度向上機能Phase B: Historical Valuation Score(Shadow計測)。
    # buy_signal_service.py等が使う既存のcurrent_per/current_pbr計算式
    # (current_price / forecast_eps・forecast_bps)とは独立に、ここでのみ
    # 再計算する(既存のBUYスクリーニング・銘柄分類ロジックには一切触れない)。
    # コードレビュー対応(basis整合性): get_historical_valuation()が返す過去PER
    # 系列はTRAILING(実績)basisのため、現在PERも同一basisのtrailing_eps
    # (forecast_eps=forwardEpsとは別物)から算出する。PBRはforecast_bpsの実体が
    # 既にtrailing bookValueであるため、そのままTRAILING basisとして扱う。
    current_per = (
        current_price / financial.trailing_eps
        if financial.trailing_eps is not None and financial.trailing_eps > 0
        else None
    )
    current_per_basis = (
        ValuationBasis.TRAILING if current_per is not None else ValuationBasis.UNKNOWN
    )
    current_pbr = (
        current_price / financial.forecast_bps
        if financial.forecast_bps is not None and financial.forecast_bps > 0
        else None
    )
    current_pbr_basis = (
        ValuationBasis.TRAILING if current_pbr is not None else ValuationBasis.UNKNOWN
    )
    historical_valuation = evaluate_historical_valuation(
        historical_valuations,
        stock_code,
        current_per,
        current_per_basis,
        current_pbr,
        current_pbr_basis,
        now,
        config.historical_valuation,
    )

    # 判定精度向上機能Phase B第二弾: Timing Score(Shadow計測)。既に計算済みの
    # momentum_snapshotを基に算出する派生値であり、既存のBUY/保有/売却/
    # ProfitTaking判定・LINE通知には一切影響しない。
    timing = evaluate_timing_score(momentum_snapshot, current_price, now, config.timing_score)

    # 判定精度向上機能Phase C: Earnings Surprise/Trend Score(Shadow計測)。
    # 決算反映確認(EarningsReleaseConfirmationState)はprofit_taking_service.py
    # と同じ関数呼び出しで独立に解決する(既存パイプラインの計算経路には触れず、
    # 副作用の無い純関数呼び出しをこちらでも行うのみ。同じ入力からは同じ結果に
    # なるため、既存のprofit_taking判定結果には一切影響しない)。
    resolved_period = resolve_latest_financial_period_end(financial, evaluation_date)
    release_confirmation_state = resolve_earnings_release_confirmation(
        earnings_date_status,
        earnings_date_raw,
        resolved_period.period_end,
        financial.source.fetched_at,
        now,
        config.earnings_window,
    )
    # コードレビュー対応(v3): 古い決算予定日が現在の判断にまだ関連するかを
    # profit_taking_service.pyと全く同じ関数・同じ引数で解決する(既存の
    # 無期限停止防止設計をPhase Cでも踏襲する。呼び出しを分けても副作用の
    # 無い純関数のため、既存のProfitTaking側の判定結果には一切影響しない)。
    decision_relevance = resolve_earnings_decision_relevance(
        earnings_date_status,
        earnings_date_raw,
        release_confirmation_state,
        evaluation_date,
        config.earnings_window,
    )
    # コードレビュー対応(第3回): release_confirmation_state/decision_relevanceの
    # 組み合わせがevaluate_earnings_surprise()自身のNOT_APPLICABLE条件と完全に
    # 一致する場合、Earnings Surpriseは必ずNOT_APPLICABLEになることが呼び出し前
    # から分かっている。この場合のみ、不要なYahoo Finance問い合わせ(既存
    # Lambdaのレイテンシ・Provider負荷・タイムアウトリスクに影響しうる外部I/O)
    # を省略する(判定ロジックの最適化ではなく、外部I/O呼び出しの抑止のみが
    # 目的。NOT_APPLICABLE判定そのものの正はevaluate_earnings_surprise()に
    # 置いたまま変更しない。条件式はevaluate_earnings_surprise()内の判定式と
    # 完全に同じものを保つこと)。
    phase_c_earnings_blocked = (
        release_confirmation_state
        in (
            EarningsReleaseConfirmationState.AWAITING_CONFIRMATION,
            EarningsReleaseConfirmationState.DELAYED,
        )
        and decision_relevance == EarningsDecisionRelevance.RELEVANT
    )
    earnings_surprise_history = (
        []
        if phase_c_earnings_blocked
        else providers.financial_data.get_earnings_surprise_history(stock_code)
    )
    # コードレビュー対応(v2): Dividend Revisionは意味の異なるデータ
    # (前年度実績 vs 現在予想の比較)であるためEarnings Surpriseからは
    # 除外した(dividend_comparison_outcomeを渡さない)。Earnings Trend側の
    # dividend_directionとしては引き続き渡す。
    earnings_surprise = evaluate_earnings_surprise(
        earnings_surprise_history,
        resolved_period.period_end,
        release_confirmation_state,
        decision_relevance,
        now,
        config.earnings_surprise,
    )
    earnings_trend = evaluate_earnings_trend(
        # コードレビュー対応(v3): 値とperiod_end/period_typeの対応を
        # indexに依存させないよう、裸のlist[Decimal]ではなく
        # FinancialPeriodValueの系列を渡す。
        quarterly_operating_income_periods,
        quarterly_operating_cashflow_periods,
        dividend.dividend_comparison_outcome,
        # コードレビュー対応(v2): 四半期実績由来か年次決算へのフォール
        # バック由来かをconfidence算出へ反映する。
        financial.recent_periods_source,
        release_confirmation_state,
        decision_relevance,
        now,
        config.earnings_trend,
    )

    # 判定精度向上機能次フェーズSTEP2: Entry Price Range(Shadow計測)。
    # fair_value_range/historical_valuation/timing/momentumは全て既に算出済みの
    # 値をそのまま使う(新規Provider呼び出しは行わない)。既存のBUY候補判定・
    # entry_buy_price/standard_buy_price/strong_buy_price・保有判断スコア・
    # 旧売却判定・ProfitTaking判定・LINE通知には一切影響しない。
    entry_price_range = evaluate_entry_price_range(
        fair_value_range,
        historical_valuation,
        timing,
        momentum_snapshot,
        current_price,
        now,
        config.entry_exit_price.entry,
    )

    # 判定精度向上機能Phase D: Market/Sector Environment Score(Shadow計測)。
    # topix_bars/sector_barsは既にmomentum_snapshot算出のために取得済みの
    # ものをそのまま使う(新規Provider呼び出しは行わない)。既存のBUY候補判定・
    # 保有判断スコア・旧売却判定・ProfitTaking判定・LINE通知・Entry/Exit
    # Price Rangeには一切影響しない。
    market_environment = evaluate_market_environment(
        topix_bars, snap.as_of_date, now, config.market_sector_environment.market, calendar
    )
    sector_environment = evaluate_sector_environment(
        sector_bars or None,
        topix_bars,
        sector_etf,
        snap.as_of_date,
        now,
        config.market_sector_environment.sector,
        calendar,
    )
    environment = evaluate_environment(
        market_environment, sector_environment, now, config.market_sector_environment.environment
    )

    financial_input_provenance = build_financial_input_provenance(financial, dividend)

    snapshot = StockSnapshot(
        stock_code=stock_code,
        current_price=current_price,
        financial=financial,
        dividend=dividend,
        benefit=benefit,
        bars=bars,
        historical_valuations=historical_valuations,
        avg_trading_value=avg_trading_value,
        disclosures=disclosures,
        next_earnings_date=next_earnings_date,
        earnings_date_status=earnings_date_status,
        earnings_date_raw=earnings_date_raw,
        business_days_to_earnings=business_days_to_earnings,
        dividend_yield_pct=dividend_yield_pct,
        benefit_yield_pct=benefit_yield_pct,
        annual_benefit_value=annual_benefit_value,
        total_yield_pct=total_yield_pct,
        fair_value=fair_value,
        fair_value_methods_used_count=fair_value_methods_used_count,
        data_sources=data_sources,
        data_fetched_at=data_fetched_at,
        quarterly_operating_incomes=quarterly_operating_incomes,
        quarterly_operating_cashflows=quarterly_operating_cashflows,
        quarterly_operating_income_periods=quarterly_operating_income_periods,
        quarterly_operating_cashflow_periods=quarterly_operating_cashflow_periods,
        severe_earnings_decline=severe_earnings_decline,
        disclosure_risk_keywords_found=keywords_found,
        material_event_keywords_found=material_event_keywords_found,
        cashflow_decomposition=cashflow_decomposition,
        stock_type_classification=stock_type_classification,
        fair_value_range=fair_value_range,
        momentum=momentum_snapshot,
        historical_valuation=historical_valuation,
        timing=timing,
        earnings_surprise=earnings_surprise,
        earnings_trend=earnings_trend,
        entry_price_range=entry_price_range,
        market_environment=market_environment,
        sector_environment=sector_environment,
        environment=environment,
        financial_input_provenance=financial_input_provenance,
    )
    return snapshot, None
