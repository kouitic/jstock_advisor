"""利確判定サービス(要求仕様3節 profit_taking_service、2026-07仕様レビュー対応)。

保有銘柄についてstock_snapshot_serviceでデータを取得し、profit_takingドメイン
ロジックで判定したうえでRecommendationスナップショットを生成する。

信頼度はfair_value_methods_used_countだけで決め打ちせず、sell_signalと共通の
confidence_scoring機構を使って算出する。
"""

from __future__ import annotations

import calendar
import datetime as dt
import uuid
from dataclasses import dataclass

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.classification.financial_industry import classify_industry
from jstock_advisor.domain.classification.profit_taking_industry import (
    classify_profit_taking_industry_sector,
    industry_model_missing_reason,
)
from jstock_advisor.domain.entities.common import SellPriceLevels
from jstock_advisor.domain.entities.enums import (
    AccountType,
    ConfidenceLevel,
    DividendComparisonOutcome,
    EarningsDecisionRelevance,
    EarningsReleaseConfirmationState,
    ProfitTakingIndustrySector,
    RecommendationType,
    StockType,
    TrendClassification,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.financial_decomposition import (
    has_guidance_revision_disclosure,
    is_fundamentally_driven,
)
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.domain.signals.confidence_scoring import (
    ConfidenceFactors,
    ConfidenceScoreResult,
    compute_confidence,
)
from jstock_advisor.domain.signals.earnings_surprise import (
    earnings_surprise_config_values,
    earnings_surprise_result_to_metrics,
)
from jstock_advisor.domain.signals.earnings_trend import (
    earnings_trend_config_values,
    earnings_trend_result_to_metrics,
)
from jstock_advisor.domain.signals.earnings_window import (
    resolve_earnings_decision_relevance,
    resolve_earnings_release_confirmation,
    resolve_latest_financial_period_end,
)
from jstock_advisor.domain.signals.entry_price_range import (
    entry_price_range_config_values,
    entry_price_range_result_to_metrics,
)
from jstock_advisor.domain.signals.environment import (
    environment_config_values,
    environment_result_to_metrics,
)
from jstock_advisor.domain.signals.exit_price_range import (
    evaluate_exit_price_range,
    exit_price_range_config_values,
    exit_price_range_result_to_metrics,
)
from jstock_advisor.domain.signals.historical_valuation import (
    historical_valuation_config_values,
    historical_valuation_result_to_metrics,
)
from jstock_advisor.domain.signals.market_environment import (
    market_environment_config_values,
    market_environment_result_to_metrics,
)
from jstock_advisor.domain.signals.profit_taking import (
    MitigatingFactorInputs,
    ProfitTakingConditionInputs,
    ProfitTakingResult,
    evaluate_profit_taking,
)
from jstock_advisor.domain.signals.record_date_resolution import (
    resolve_benefit_record_date_recurring_label,
    resolve_benefit_record_date_source_type,
    resolve_dividend_record_date_recurring_label,
    resolve_dividend_record_date_source_type,
)
from jstock_advisor.domain.signals.sector_environment import (
    sector_environment_config_values,
    sector_environment_result_to_metrics,
)
from jstock_advisor.domain.signals.timing_score import (
    timing_score_config_values,
    timing_score_result_to_metrics,
)
from jstock_advisor.domain.signals.trading_unit_feasibility import (
    TradingUnitFeasibility,
    evaluate_trading_unit_feasibility,
)
from jstock_advisor.interfaces.types import DividendInfo, ShareholderBenefit
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.buy_signal_service import RULE_VERSION_PLACEHOLDER
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.rule_version_service import RuleVersionService
from jstock_advisor.services.stock_snapshot_service import StockSnapshot, build_stock_snapshot

# 決算直前は原則として通常のPARTIAL/FULL_PROFIT_TAKE提案を保留する(要求仕様§4)。
_EARNINGS_SUPPRESSIBLE_TO_REVIEW = (
    RecommendationType.PARTIAL_PROFIT_TAKE,
    RecommendationType.FULL_PROFIT_TAKE,
)


@dataclass(frozen=True)
class ProfitTakingOutcome:
    stock_code: str
    recommendation: Recommendation | None
    data_error: str | None


def _dividend_decrease_explanation(
    dividend: DividendInfo, forecast_increase: bool | None
) -> str | None:
    """今期の予想配当が前期実績を下回る場合の表示文言を、確定情報と推定情報を
    区別して構築する(要求仕様§9)。特別配当剥落を通常の減配として表示しない。
    """
    if forecast_increase is not False:
        return None
    if dividend.official_dividend_cut_announced:
        return "普通配当が公式に減額されており、今期は普通配当の減配予想"
    if dividend.dividend_breakdown_confirmed and dividend.special_dividend_expired is True:
        return "前期の特別配当終了により総額が減少しており、普通配当の減配ではない"
    return (
        "年間配当総額の減少候補です(内訳が確認できないため、"
        "普通配当の減配か特別配当の剥落かは未確認)"
    )


def _build_next_review_conditions(
    result: ProfitTakingResult,
    next_earnings_date: dt.date | None,
    release_confirmation_state: EarningsReleaseConfirmationState = (
        EarningsReleaseConfirmationState.NOT_APPLICABLE
    ),
) -> list[str]:
    conditions: list[str] = []
    sp = result.sell_prices
    if sp.recommended_limit_price is not None:
        p = sp.recommended_limit_price
        if p.price_low is not None and p.price_high is not None:
            conditions.append(f"{p.price_low}〜{p.price_high}円到達時に一部利確の要否を再評価")
        else:
            conditions.append(f"{p.price}円到達時に一部利確の要否を再評価")
    conditions.append("業績下方修正")
    conditions.append("EPS成長率鈍化")
    if next_earnings_date is not None:
        conditions.append(f"次回決算({next_earnings_date})後に適正価格を再計算")
    elif release_confirmation_state == EarningsReleaseConfirmationState.DELAYED:
        conditions.append(
            "決算発表予定日を経過し、最新財務データの反映確認が長引いています。"
            "確認でき次第、適正価格を再計算します"
        )
    elif release_confirmation_state == EarningsReleaseConfirmationState.AWAITING_CONFIRMATION:
        conditions.append(
            "決算発表予定日を経過していますが、無償データから実際の発表状況を確認できて"
            "いません。最新財務データの更新を確認後に適正価格を再計算します"
        )
    else:
        conditions.append("次回決算後に適正価格を再計算")
    return conditions


_INDUSTRY_SECTOR_LABELS: dict[ProfitTakingIndustrySector, str] = {
    ProfitTakingIndustrySector.BANKING: "銀行業",
    ProfitTakingIndustrySector.LEASING_FINANCE: "リース・金融業",
    ProfitTakingIndustrySector.FOOD: "食品業",
    ProfitTakingIndustrySector.CHEMICAL: "化学業",
    ProfitTakingIndustrySector.GAS_UTILITY: "ガス・公益業",
    ProfitTakingIndustrySector.SMALL_GROWTH: "小型成長株",
    ProfitTakingIndustrySector.GENERAL: "一般事業会社",
    ProfitTakingIndustrySector.UNKNOWN: "業種不明",
}


def _build_not_yet_action_reasons(
    result: ProfitTakingResult,
    config: AppConfig,
    fair_value_overall_confidence: ConfidenceLevel | None,
    industry_sector: ProfitTakingIndustrySector,
    industry_model_applied: bool,
    days_to_next_earnings_business_days: int | None,
    trading_unit_feasibility: TradingUnitFeasibility,
    has_strong_counter_material: bool,
    is_uptrend: bool,
) -> list[str]:
    """「直ちに利確しない理由」を、最終判定の種類からではなく実際に評価した
    数値条件から構築する(要求仕様§2)。MUFGのように含み益率が閾値以上でも
    最終判定がWATCHになりうる(業種別モデル未対応等が理由の)ケースで、
    誤って「含み益率が閾値未満」と表示しないようにする。
    """
    t = config.profit_taking.thresholds
    reasons: list[str] = []
    if result.pnl.unrealized_pnl_pct < t.unrealized_gain_partial_pct:
        reasons.append(f"含み益率は一部利確基準({t.unrealized_gain_partial_pct:.0f}%)未満")
    if fair_value_overall_confidence == ConfidenceLevel.MEDIUM:
        reasons.append("適正価格モデルの信頼度がMEDIUM")
    if not industry_model_applied:
        # 利用者向け通知では内部設計用語(「専用モデルが未適用」)をそのまま使わず、
        # 業種名が安全に取得できる場合だけそれを含めた自然な文言にする
        # (2026-07仕様レビュー対応)。Recommendation.industry_sector/
        # industry_model_appliedという構造化フィールド自体は変更しないため、
        # 監査ログ側の詳細な内部理由は引き続き追跡できる。
        if industry_sector in (
            ProfitTakingIndustrySector.GENERAL,
            ProfitTakingIndustrySector.UNKNOWN,
        ):
            reasons.append(
                "現在の適正価格は汎用モデルによる参考値です"
                if industry_sector == ProfitTakingIndustrySector.GENERAL
                else "業種特性を反映した専用評価モデルではありません"
            )
        else:
            label = _INDUSTRY_SECTOR_LABELS[industry_sector]
            reasons.append(f"{label}の事業特性を十分に反映した専用評価モデルではありません")
    if (
        days_to_next_earnings_business_days is not None
        and days_to_next_earnings_business_days
        <= config.earnings_window.profit_taking_suppression_business_days
    ):
        reasons.append(f"次回決算まで{days_to_next_earnings_business_days}営業日")
    if not trading_unit_feasibility.partial_sale_executable:
        reasons.append(
            f"保有株数が売買単位({trading_unit_feasibility.trading_unit}株)に届かず"
            "一部売却が実行できない"
        )
    if has_strong_counter_material:
        reasons.append("増益・増配などの反対材料がある")
    if is_uptrend:
        reasons.append("強い上昇トレンドが継続")
    if result.fair_value_used_as_sole_strong_basis:
        reasons.append(
            "適正価格モデルの手法間一致度・強気適正価格との関係が強い確信の水準に達していない"
        )
    if not reasons:
        reasons.append("適正価格モデルには手法間のばらつき等の不確実性がある")
    return reasons


_DEFAULT_EXECUTION_CONTEXT = ExecutionContext.normal()


class ProfitTakingService:
    def __init__(
        self,
        providers: ProviderBundle,
        config: AppConfig,
        audit_service: AuditService | None = None,
        rule_version_service: RuleVersionService | None = None,
        business_calendar: BusinessCalendar | None = None,
        execution_context: ExecutionContext = _DEFAULT_EXECUTION_CONTEXT,
    ) -> None:
        self._providers = providers
        self._config = config
        self._audit = audit_service or AuditService(execution_context=execution_context)
        self._rule_version_service = rule_version_service or RuleVersionService()
        self._calendar = business_calendar or BusinessCalendar.from_config(config.holiday_calendar)

    def _active_rule_version(self) -> str:
        return self._rule_version_service.get_active_version_or(RULE_VERSION_PLACEHOLDER)

    def _fair_value_reflects_latest_earnings(self, snapshot: StockSnapshot) -> bool | None:
        """適正価格算出の入力が最新決算を反映しているかの簡易判定(要求仕様レビュー対応)。

        手法別の入力日付を個別に厳密照合する手段が無いため、決算期末
        (fiscal_period_end)が一定期間内(データ鮮度の許容日数を年換算した目安)で
        あることを代理指標とする。fiscal_period_endが取得できない場合は判定不能。
        """
        if not snapshot.fair_value_range.methods_used:
            return None
        fiscal_period_end = snapshot.financial.fiscal_period_end
        if fiscal_period_end is None:
            return None
        age_days = (snapshot.data_fetched_at.date() - fiscal_period_end).days
        return 0 <= age_days <= 400

    def _compute_confidence(
        self, result: ProfitTakingResult, snapshot: StockSnapshot, now: dt.datetime
    ) -> ConfidenceScoreResult:
        fv_range = snapshot.fair_value_range
        spread_ratio = (
            float(fv_range.bull / fv_range.bear)
            if fv_range.bear is not None and fv_range.bull is not None and fv_range.bear > 0
            else None
        )
        days_to_earnings = snapshot.business_days_to_earnings
        is_benefit_eligible = snapshot.benefit is not None
        benefit_value_missing = is_benefit_eligible and snapshot.annual_benefit_value is None

        factors = ConfidenceFactors(
            data_freshness_days=(now - snapshot.data_fetched_at).days,
            fair_value_methods_used_count=len(fv_range.methods_used),
            fair_value_method_spread_ratio=spread_ratio,
            days_to_next_earnings_business_days=days_to_earnings,
            latest_quarter_fetched=bool(snapshot.quarterly_operating_incomes),
            record_date_known=(
                snapshot.dividend.dividend_record_date is not None
                or (snapshot.benefit is not None and snapshot.benefit.benefit_ex_date is not None)
            ),
            key_metric_missing=snapshot.fair_value is None,
            one_time_factors_identified=is_fundamentally_driven(snapshot.cashflow_decomposition),
            counter_factors_evaluated=True,
            benefit_eligible_but_value_unavailable=benefit_value_missing,
            fair_value_is_sole_strong_basis=result.fair_value_used_as_sole_strong_basis,
        )
        return compute_confidence(factors, self._config.confidence)

    def analyze(
        self, holding: Holding, now: dt.datetime, snapshot: StockSnapshot | None = None
    ) -> ProfitTakingOutcome:
        """snapshotを渡すと再取得を省略する(sell_signalと同一銘柄を二重に取得する
        無駄を避けるため、呼び出し側で一度だけ取得して両方に渡すことを想定)。"""
        error: str | None = None
        if snapshot is None:
            snapshot, error = build_stock_snapshot(
                self._providers,
                holding.stock_code,
                now,
                self._config,
                business_calendar=self._calendar,
            )
        if snapshot is None:
            self._audit.record(
                decision_type="profit_taking",
                stock_code=holding.stock_code,
                input_values={},
                calculation_formulas={},
                output_values={"data_error": error},
                data_sources=[],
                rule_version=self._active_rule_version(),
                timestamp=now,
            )
            return ProfitTakingOutcome(holding.stock_code, None, error)

        mitigating_inputs = MitigatingFactorInputs(
            fair_value_rising_with_earnings_growth=(
                snapshot.fair_value is not None
                and not snapshot.severe_earnings_decline
                and all(
                    snapshot.quarterly_operating_incomes[i]
                    >= snapshot.quarterly_operating_incomes[i - 1]
                    for i in range(1, len(snapshot.quarterly_operating_incomes))
                )
                if len(snapshot.quarterly_operating_incomes) >= 2
                else False
            ),
            continuous_dividend_increase_years=(
                snapshot.dividend.consecutive_dividend_increase_years or 0
            ),
            is_progressive_or_doe_policy=snapshot.dividend.is_progressive_or_doe_policy,
            long_term_holding_benefit_imminent=_is_long_term_benefit_imminent(
                holding, snapshot.benefit, now, self._config
            ),
            few_reinvestment_alternatives=False,  # 将来: 買い候補件数から動的算出する拡張ポイント
            is_nisa_account=holding.account_type == AccountType.NISA,
        )

        days_to_earnings = snapshot.business_days_to_earnings

        trading_unit_config = self._config.profit_taking.trading_unit
        trading_unit_feasibility = evaluate_trading_unit_feasibility(
            shares=holding.shares,
            trading_unit=trading_unit_config.default_trading_unit,
            odd_lot_trading_available=trading_unit_config.default_odd_lot_trading_available,
        )

        is_growth_stock = StockType.GROWTH in snapshot.stock_type_classification.types
        industry_sector = classify_profit_taking_industry_sector(
            snapshot.financial.industry, snapshot.financial.sector, is_growth_stock
        )
        # 現行データソースでは業種別専用モデル(CET1比率・DOE等)を安定取得できないため、
        # 常にFalse(要求仕様§7: 未対応の場合はHIGH信頼度・適正価格単独でのPARTIAL以上を禁止)。
        industry_model_applied = False
        # 再コードレビュー対応(2026-08、指摘5): ceiling_price利用可否の金融業ゲート
        # (_fair_value_action_usable())には、profit_taking_industry.py(銀行・
        # リース金融のみ識別)ではなく、既存のfinancial_industry.py(保険・証券等
        # 金融業全般をキーワードで識別し、未知の値は安全側にUNKNOWNとする三値分類)を
        # 使う。industry_sector(表示・既存ProfitTaking用の業種区分)自体は変更しない。
        industry_classification = classify_industry(
            snapshot.financial.sector, snapshot.financial.industry
        ).classification

        has_strong_counter_material = (
            snapshot.dividend.dividend_comparison_outcome
            == DividendComparisonOutcome.DIVIDEND_INCREASE
            or (snapshot.dividend.consecutive_dividend_increase_years or 0) >= 2
        )

        condition_inputs = ProfitTakingConditionInputs(
            stock_types=snapshot.stock_type_classification.types,
            fair_value_range=snapshot.fair_value_range,
            momentum=snapshot.momentum,
            dividend_comparison_outcome=snapshot.dividend.dividend_comparison_outcome,
            cashflow_fundamentally_driven=is_fundamentally_driven(snapshot.cashflow_decomposition),
            guidance_revision_disclosed=has_guidance_revision_disclosure(snapshot.disclosures),
            severe_earnings_decline=snapshot.severe_earnings_decline,
            profit_target_price=holding.profit_target_price,
            profit_target_rate=holding.profit_target_rate,
            fair_value_reflects_latest_earnings=self._fair_value_reflects_latest_earnings(snapshot),
            industry_model_applied=industry_model_applied,
            industry_sector=industry_sector,
            industry_classification=industry_classification,
            partial_sale_executable=trading_unit_feasibility.partial_sale_executable,
            days_to_next_earnings_business_days=days_to_earnings,
            has_strong_counter_material=has_strong_counter_material,
        )

        is_benefit_eligible = snapshot.benefit is not None
        result = evaluate_profit_taking(
            current_price=snapshot.current_price,
            average_purchase_price=holding.average_purchase_price,
            shares=holding.shares,
            total_purchase_amount=holding.total_purchase_amount,
            cumulative_dividend_received=holding.cumulative_dividend_received,
            cumulative_benefit_value_received=holding.cumulative_benefit_value_received,
            current_total_yield_pct=snapshot.total_yield_pct,
            forecast_annual_dividend_per_share=snapshot.dividend.forecast_annual_dividend_per_share,
            mitigating_inputs=mitigating_inputs,
            config=self._config.profit_taking,
            condition_inputs=condition_inputs,
            annual_benefit_value_at_min_lot=snapshot.annual_benefit_value,
            benefit_min_shares_required=(
                snapshot.benefit.min_shares_required if snapshot.benefit is not None else None
            ),
            is_benefit_eligible=is_benefit_eligible,
        )

        # 決算直前の判定抑制(要求仕様§4)。上場廃止決定・債務超過公式確認等の
        # 一次情報確認済みの即時criticalが検出されている場合は抑制しない。
        is_confirmed_critical = (
            condition_inputs.accounting_or_scandal_or_delisting_risk
            or condition_inputs.investment_premise_broken
        )
        suppression_days = self._config.earnings_window.profit_taking_suppression_business_days
        # 決算発表確認待ち(コードレビュー対応: 明治ホールディングス(2269)事例)。
        # 決算予定日を経過したが無償データで発表実績を確認できない期間は、
        # 財務データが更新されるまで通常のPARTIAL/FULL_PROFIT_TAKE提案を保留する。
        # 評価日(JST)は1回だけ計算し、期間末解決・関連性判定の両方で使い回す
        # (デプロイ前対応: 各所で個別にnow.date()/evaluation_date_jst(now)を
        # 再計算しない)。
        evaluation_date = evaluation_date_jst(now)
        # 決算反映確認には年次のfiscal_period_endではなく、recent_quartersを
        # 優先した最新財務期間末を使う(デプロイ前対応: 四半期決算の反映を
        # 検知できないバグの修正)。評価日より未来のperiod_endは候補から除外する。
        resolved_period = resolve_latest_financial_period_end(snapshot.financial, evaluation_date)
        release_confirmation_state = resolve_earnings_release_confirmation(
            snapshot.earnings_date_status,
            snapshot.earnings_date_raw,
            resolved_period.period_end,
            snapshot.financial.source.fetched_at,
            now,
            self._config.earnings_window,
        )
        # 過去の決算予定日が現在の判断にまだ関連するか(デプロイ前対応: 何か月も
        # 前の過去日で無期限に通常判定を止めないための安全策)。
        decision_relevance = resolve_earnings_decision_relevance(
            snapshot.earnings_date_status,
            snapshot.earnings_date_raw,
            release_confirmation_state,
            evaluation_date,
            self._config.earnings_window,
        )
        effective_recommendation_type = result.recommendation_type
        effective_sell_prices = result.sell_prices
        if (
            not is_confirmed_critical
            and days_to_earnings is not None
            and days_to_earnings <= suppression_days
        ):
            if effective_recommendation_type in _EARNINGS_SUPPRESSIBLE_TO_REVIEW:
                effective_recommendation_type = RecommendationType.REVIEW_BEFORE_EARNINGS
                effective_sell_prices = SellPriceLevels()
            elif effective_recommendation_type == RecommendationType.WATCH:
                effective_recommendation_type = RecommendationType.WATCH_BEFORE_EARNINGS
        elif (
            not is_confirmed_critical
            and release_confirmation_state
            in (
                EarningsReleaseConfirmationState.AWAITING_CONFIRMATION,
                EarningsReleaseConfirmationState.DELAYED,
            )
            and decision_relevance == EarningsDecisionRelevance.RELEVANT
            and effective_recommendation_type in _EARNINGS_SUPPRESSIBLE_TO_REVIEW
        ):
            effective_recommendation_type = RecommendationType.REVIEW_AFTER_EARNINGS
            effective_sell_prices = SellPriceLevels()

        confidence_result = self._compute_confidence(result, snapshot, now)

        self._audit.record(
            decision_type="profit_taking",
            stock_code=holding.stock_code,
            input_values={
                "current_price": str(snapshot.current_price),
                "average_purchase_price": str(holding.average_purchase_price),
                "shares": holding.shares,
                "fair_value": (
                    str(snapshot.fair_value) if snapshot.fair_value is not None else None
                ),
                "current_total_yield_pct": snapshot.total_yield_pct,
                "consecutive_dividend_increase_years": (
                    mitigating_inputs.continuous_dividend_increase_years
                ),
                "is_progressive_or_doe_policy": mitigating_inputs.is_progressive_or_doe_policy,
                "is_nisa_account": mitigating_inputs.is_nisa_account,
            },
            calculation_formulas={
                "unrealized_pnl_pct": "(current_price / average_purchase_price - 1) * 100",
                "total_return_pct": (
                    "(unrealized_pnl + cumulative_dividend + cumulative_benefit) "
                    "/ total_purchase_amount * 100"
                ),
            },
            output_values={
                "recommendation_type": result.recommendation_type.value,
                "effective_recommendation_type": effective_recommendation_type.value,
                "fundamental_action": result.fundamental_action.value,
                "timing_action": result.timing_action.value,
                "final_action": result.final_action.value,
                "industry_sector": industry_sector.value,
                "days_to_next_earnings_business_days": days_to_earnings,
                "trading_unit_partial_sale_executable": (
                    trading_unit_feasibility.partial_sale_executable
                ),
                "current_price_vs_neutral_fair_value_pct": (
                    result.current_price_vs_neutral_fair_value_pct
                ),
                "current_price_vs_bull_fair_value_pct": (
                    result.current_price_vs_bull_fair_value_pct
                ),
                "triggered_reasons": result.triggered_reasons,
                "mitigating_factors_applied": result.mitigating_factors_applied,
                "unrealized_pnl_pct": result.pnl.unrealized_pnl_pct,
                "total_return_pct": result.pnl.total_return_pct,
                "confidence": confidence_result.level.value,
                "confidence_score": confidence_result.score,
                "confidence_reasons": confidence_result.reasons_not_high,
                # --- デプロイ前対応: 決算反映確認の由来を監査可能にする ---
                "resolved_financial_period_end": (
                    resolved_period.period_end.isoformat()
                    if resolved_period.period_end is not None
                    else None
                ),
                "financial_period_end_source": resolved_period.source.value,
                "recent_periods_source": snapshot.financial.recent_periods_source.value,
                "earnings_date_raw": (
                    snapshot.earnings_date_raw.isoformat()
                    if snapshot.earnings_date_raw is not None
                    else None
                ),
                "financial_fetched_at": snapshot.financial.source.fetched_at.isoformat(),
                "release_confirmation_state": release_confirmation_state.value,
                "earnings_decision_relevance": decision_relevance.value,
            },
            data_sources=list(snapshot.data_sources),
            rule_version=self._active_rule_version(),
            timestamp=now,
            fair_value_results=[
                {
                    "method": m.method,
                    "fair_value": str(m.fair_value) if m.fair_value is not None else None,
                    "confidence": m.confidence.value,
                    "exclusion_reason": m.exclusion_reason,
                }
                for m in snapshot.fair_value_range.methods_used
            ],
            triggered_rules=result.triggered_reasons,
            suppressed_rules=result.mitigating_factors_applied,
        )

        if effective_recommendation_type == RecommendationType.HOLD:
            return ProfitTakingOutcome(holding.stock_code, None, None)

        dividend = snapshot.dividend
        forecast_increase = None
        forecast_increase_rate = None
        if (
            dividend.forecast_annual_dividend_per_share is not None
            and dividend.actual_annual_dividend_per_share is not None
            and dividend.actual_annual_dividend_per_share > 0
        ):
            forecast_increase = (
                dividend.forecast_annual_dividend_per_share
                > dividend.actual_annual_dividend_per_share
            )
            forecast_increase_rate = (
                float(
                    dividend.forecast_annual_dividend_per_share
                    / dividend.actual_annual_dividend_per_share
                    - 1
                )
                * 100
            )
        dividend_decrease_explanation = _dividend_decrease_explanation(dividend, forecast_increase)

        fv_range = snapshot.fair_value_range
        spread_ratio = (
            float(fv_range.bull / fv_range.bear)
            if fv_range.bear is not None and fv_range.bull is not None and fv_range.bear > 0
            else None
        )

        # 判定精度向上機能次フェーズSTEP2: Exit Price Range(Shadow計測)。
        # ProfitTaking判定が実際にRecommendationを構築すると決まった時点
        # (HOLD等の早期returnより後)でのみ計算する。既存の早期return・
        # 判定条件分岐の実行順は一切変更しない。
        exit_price_range = evaluate_exit_price_range(
            snapshot.fair_value_range,
            snapshot.historical_valuation,
            snapshot.timing,
            holding.average_purchase_price,
            snapshot.current_price,
            now,
            self._config.entry_exit_price.exit,
        )

        recommendation = Recommendation(
            recommendation_id=str(uuid.uuid4()),
            stock_code=holding.stock_code,
            stock_name=snapshot.financial.stock_name or holding.stock_name,
            recommended_at=now,
            recommendation_type=effective_recommendation_type,
            raw_recommendation_type=result.fundamental_action,
            sell_prices=effective_sell_prices,
            price_at_recommendation=snapshot.current_price,
            average_purchase_price_at_recommendation=holding.average_purchase_price,
            shares_at_recommendation=holding.shares,
            dividend_yield_pct_at_recommendation=snapshot.dividend_yield_pct,
            shareholder_benefit_yield_pct_at_recommendation=snapshot.benefit_yield_pct,
            total_yield_pct_at_recommendation=snapshot.total_yield_pct,
            fair_value_at_recommendation=snapshot.fair_value,
            reasons=result.triggered_reasons,
            counter_factors=result.mitigating_factors_applied,
            key_risks=[
                f"含み損益率{result.pnl.unrealized_pnl_pct:.1f}%",
                f"配当・優待込み累計利益率{result.pnl.total_return_pct:.1f}%",
            ],
            confidence=confidence_result.level,
            next_earnings_date=snapshot.next_earnings_date,
            earnings_date_status=snapshot.earnings_date_status,
            earnings_date_raw=snapshot.earnings_date_raw,
            earnings_release_confirmation_state=release_confirmation_state,
            earnings_decision_relevance=decision_relevance,
            dividend_record_date=snapshot.dividend.dividend_record_dates[0]
            if snapshot.dividend.dividend_record_dates
            else None,
            benefit_record_date=snapshot.benefit.benefit_record_dates[0]
            if snapshot.benefit is not None and snapshot.benefit.benefit_record_dates
            else None,
            rule_version=self._active_rule_version(),
            config_values_used={
                "unrealized_gain_full_pct": (
                    self._config.profit_taking.thresholds.unrealized_gain_full_pct
                ),
                "total_yield_strong_caution_pct": (
                    self._config.profit_taking.thresholds.total_yield_strong_caution_pct
                ),
                "independent_condition_count": result.independent_condition_count,
                "fair_value_used_as_sole_strong_basis": (
                    result.fair_value_used_as_sole_strong_basis
                ),
                "historical_valuation": historical_valuation_config_values(
                    self._config.historical_valuation
                ),
                "timing_score": timing_score_config_values(self._config.timing_score),
                "earnings_surprise": earnings_surprise_config_values(
                    self._config.earnings_surprise
                ),
                "earnings_trend": earnings_trend_config_values(self._config.earnings_trend),
                "entry_price_range": entry_price_range_config_values(
                    self._config.entry_exit_price.entry
                ),
                "exit_price_range": exit_price_range_config_values(
                    self._config.entry_exit_price.exit
                ),
                "market_environment": market_environment_config_values(
                    self._config.market_sector_environment.market
                ),
                "sector_environment": sector_environment_config_values(
                    self._config.market_sector_environment.sector
                ),
                "environment": environment_config_values(
                    self._config.market_sector_environment.environment
                ),
                # コードレビュー対応(2026-08、上値余地の導入): 判定当時に実際に
                # 使用したprice_position設定値と、算出したceiling_price/upside_pct/
                # fair_value_action_usableを記録する(§18)。
                "price_position": self._config.profit_taking.price_position.model_dump(),
                "ceiling_price": (
                    float(result.ceiling_price) if result.ceiling_price is not None else None
                ),
                "upside_pct": result.upside_pct,
                "fair_value_action_usable": result.fair_value_action_usable,
            },
            data_sources=list(snapshot.data_sources),
            next_review_conditions=_build_next_review_conditions(
                result, snapshot.next_earnings_date, release_confirmation_state
            ),
            not_yet_action_reasons=_build_not_yet_action_reasons(
                result,
                self._config,
                fv_range.overall_confidence,
                industry_sector,
                industry_model_applied,
                days_to_earnings,
                trading_unit_feasibility,
                has_strong_counter_material,
                snapshot.momentum.trend_classification
                in (TrendClassification.UPTREND, TrendClassification.STRONG_UPTREND),
            ),
            current_price_vs_neutral_fair_value_pct=(
                result.current_price_vs_neutral_fair_value_pct
            ),
            current_price_vs_bull_fair_value_pct=result.current_price_vs_bull_fair_value_pct,
            profit_taking_origin=result.origin,
            profit_taking_ceiling_price=result.ceiling_price,
            profit_taking_upside_pct=result.upside_pct,
            trading_unit=trading_unit_feasibility.trading_unit,
            minimum_sellable_shares=trading_unit_feasibility.minimum_sellable_shares,
            partial_sale_executable=trading_unit_feasibility.partial_sale_executable,
            suggested_sell_shares=trading_unit_feasibility.suggested_sell_shares,
            odd_lot_trading_available=trading_unit_feasibility.odd_lot_trading_available,
            industry_sector=industry_sector,
            industry_model_applied=industry_model_applied,
            industry_model_missing_reason=industry_model_missing_reason(industry_sector),
            dividend_decrease_explanation=dividend_decrease_explanation,
            fair_value_bear=fv_range.bear,
            fair_value_neutral=fv_range.neutral,
            fair_value_bull=fv_range.bull,
            fair_value_overall_confidence=fv_range.overall_confidence,
            fair_value_methods=[
                {
                    "method": m.method,
                    "fair_value": str(m.fair_value) if m.fair_value is not None else None,
                    "confidence": m.confidence.value,
                    "exclusion_reason": m.exclusion_reason,
                }
                for m in (fv_range.methods_used + fv_range.methods_excluded)
            ],
            fair_value_spread_ratio=spread_ratio,
            consecutive_actual_dividend_increase_years=(
                dividend.consecutive_dividend_increase_years
            ),
            forecast_dividend_increase=forecast_increase,
            forecast_dividend_increase_rate=forecast_increase_rate,
            dividend_record_date_recurring_label=resolve_dividend_record_date_recurring_label(
                dividend, snapshot.financial.fiscal_year_end_month
            ),
            benefit_record_date_recurring_label=resolve_benefit_record_date_recurring_label(
                snapshot.benefit, snapshot.financial.fiscal_year_end_month
            ),
            dividend_record_date_source_type=resolve_dividend_record_date_source_type(dividend),
            benefit_record_date_source_type=resolve_benefit_record_date_source_type(
                snapshot.benefit
            ),
            business_days_to_earnings=days_to_earnings,
            # 判定精度向上機能Phase B: DecisionSnapshot記録専用(Shadow計測)。
            historical_valuation_score=snapshot.historical_valuation.score,
            historical_valuation_confidence=snapshot.historical_valuation.confidence,
            historical_valuation_coverage=snapshot.historical_valuation.coverage,
            historical_valuation_reason_codes=snapshot.historical_valuation.reason_codes,
            historical_valuation_metrics=historical_valuation_result_to_metrics(
                snapshot.historical_valuation
            ),
            # 判定精度向上機能Phase B第二弾: DecisionSnapshot記録専用(Shadow計測)。
            timing_score=snapshot.timing.score,
            timing_confidence=snapshot.timing.confidence,
            timing_coverage=snapshot.timing.coverage,
            timing_reason_codes=snapshot.timing.reason_codes,
            timing_metrics=timing_score_result_to_metrics(
                snapshot.timing, snapshot.momentum, snapshot.current_price
            ),
            # 判定精度向上機能Phase C: DecisionSnapshot記録専用(Shadow計測)。
            earnings_surprise_score=snapshot.earnings_surprise.score,
            earnings_surprise_confidence=snapshot.earnings_surprise.confidence,
            earnings_surprise_coverage=snapshot.earnings_surprise.coverage,
            earnings_surprise_reason_codes=snapshot.earnings_surprise.reason_codes,
            earnings_surprise_metrics=earnings_surprise_result_to_metrics(
                snapshot.earnings_surprise
            ),
            earnings_trend_score=snapshot.earnings_trend.score,
            earnings_trend_confidence=snapshot.earnings_trend.confidence,
            earnings_trend_coverage=snapshot.earnings_trend.coverage,
            earnings_trend_reason_codes=snapshot.earnings_trend.reason_codes,
            earnings_trend_metrics=earnings_trend_result_to_metrics(snapshot.earnings_trend),
            # 判定精度向上機能次フェーズSTEP2: DecisionSnapshot記録専用
            # (Shadow計測)。Entryはsnapshot算出済みの値をそのままコピー、
            # Exitは上記で算出済みのexit_price_rangeをコピーする。
            entry_price_range_state=snapshot.entry_price_range.state,
            entry_price_range_confidence=snapshot.entry_price_range.confidence,
            entry_price_range_coverage=snapshot.entry_price_range.coverage,
            entry_price_range_reason_codes=snapshot.entry_price_range.reason_codes,
            entry_price_range_metrics=entry_price_range_result_to_metrics(
                snapshot.entry_price_range,
                snapshot.fair_value_range,
                snapshot.historical_valuation,
                snapshot.timing,
                snapshot.momentum,
                self._config.entry_exit_price.entry,
            ),
            entry_price_range_starter_price=snapshot.entry_price_range.starter_entry_price,
            entry_price_range_preferred_price=snapshot.entry_price_range.preferred_entry_price,
            entry_price_range_strong_price=snapshot.entry_price_range.strong_entry_price,
            entry_price_range_max_price=snapshot.entry_price_range.max_entry_price,
            entry_price_range_stop_review_price=snapshot.entry_price_range.stop_review_price,
            exit_price_range_state=exit_price_range.state,
            exit_price_range_confidence=exit_price_range.confidence,
            exit_price_range_coverage=exit_price_range.coverage,
            exit_price_range_reason_codes=exit_price_range.reason_codes,
            exit_price_range_metrics=exit_price_range_result_to_metrics(
                exit_price_range,
                snapshot.fair_value_range,
                snapshot.historical_valuation,
                snapshot.timing,
                holding.average_purchase_price,
                self._config.entry_exit_price.exit,
            ),
            exit_price_range_partial_low_price=exit_price_range.partial_profit_take_low_price,
            exit_price_range_partial_high_price=exit_price_range.partial_profit_take_high_price,
            exit_price_range_strong_price=exit_price_range.strong_profit_take_price,
            exit_price_range_downside_review_price=exit_price_range.downside_review_price,
            exit_price_range_exit_review_price=exit_price_range.exit_review_price,
            # 判定精度向上機能Phase D: DecisionSnapshot記録専用(Shadow計測)。
            market_score=snapshot.market_environment.score,
            market_confidence=snapshot.market_environment.confidence,
            market_coverage=snapshot.market_environment.coverage,
            market_reason_codes=snapshot.market_environment.reason_codes,
            market_metrics=market_environment_result_to_metrics(snapshot.market_environment),
            sector_score=snapshot.sector_environment.score,
            sector_confidence=snapshot.sector_environment.confidence,
            sector_coverage=snapshot.sector_environment.coverage,
            sector_reason_codes=snapshot.sector_environment.reason_codes,
            sector_metrics=sector_environment_result_to_metrics(snapshot.sector_environment),
            environment_score=snapshot.environment.score,
            environment_confidence=snapshot.environment.confidence,
            environment_coverage=snapshot.environment.coverage,
            environment_reason_codes=snapshot.environment.reason_codes,
            environment_metrics=environment_result_to_metrics(
                snapshot.environment, snapshot.market_environment, snapshot.sector_environment
            ),
        )
        return ProfitTakingOutcome(holding.stock_code, recommendation, None)


def _is_long_term_benefit_imminent(
    holding: Holding,
    benefit: ShareholderBenefit | None,
    now: dt.datetime,
    config: AppConfig,
) -> bool:
    """保有銘柄が優待の長期保有条件をまもなく満たすかどうかを判定する。

    ShareholderBenefit.benefits内のlong_term_holding_condition_monthsと
    Holding.first_purchase_dateから、条件達成日が設定のwithin_business_days以内かを見る。
    """
    if benefit is None:
        return False

    mitigating = config.profit_taking.mitigating_factors.long_term_holding_benefit_imminent
    within_days = mitigating.within_business_days
    if within_days is None:
        return False

    for detail in benefit.benefits:
        months = detail.long_term_holding_condition_months
        if months is None:
            continue
        qualify_date = _add_months(holding.first_purchase_date, months)
        days_remaining = (qualify_date - now.date()).days
        if 0 <= days_remaining <= within_days * 2:  # 営業日ベースの概算(週末考慮の簡易マージン)
            return True
    return False


def _add_months(date: dt.date, months: int) -> dt.date:
    month_index = date.month - 1 + months
    year = date.year + month_index // 12
    month = month_index % 12 + 1
    day = min(date.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)
