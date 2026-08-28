"""投資前提悪化による売却判定サービス(2026-07仕様: 判定エンジンの再設計)。

保有銘柄についてstock_snapshot_serviceでデータを取得し、sell_signalドメイン
ロジックで判定したうえでRecommendationスナップショットを生成する。
株価の下落そのものは判定材料に含めない。

信頼度はConfidenceLevel.HIGHを決め打ちせず、confidence_scoringで実際に算出する。
根拠がすべてyfinance等の二次情報のみの場合、SELL/URGENT_REVIEWをREVIEWへ
自動的に格下げする(要求仕様§12: yfinance単独で強い売却判定を出さない)。
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.classification.financial_industry import classify_industry
from jstock_advisor.domain.entities.common import SellPriceLevels
from jstock_advisor.domain.entities.enums import (
    IndustryClassification,
    RecommendationType,
    TriggerStatus,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.financial_decomposition import is_fundamentally_driven
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
from jstock_advisor.domain.signals.sector_environment import (
    sector_environment_config_values,
    sector_environment_result_to_metrics,
)
from jstock_advisor.domain.signals.sell_signal import (
    SellRuleEvaluation,
    SellSignalResult,
    build_sell_rule_inputs_from_data,
    evaluate_sell_signal,
)
from jstock_advisor.domain.signals.timing_score import (
    timing_score_config_values,
    timing_score_result_to_metrics,
)
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.buy_signal_service import RULE_VERSION_PLACEHOLDER
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.rule_version_service import RuleVersionService
from jstock_advisor.services.stock_snapshot_service import StockSnapshot, build_stock_snapshot

_STRONG_TYPES = (RecommendationType.SELL, RecommendationType.URGENT_REVIEW)

# 反対材料(counter_factors)の評価対象カテゴリー(2026-07仕様レビュー対応)。
# 実際に評価できたカテゴリーのみをcounter_factors_evaluatedの判定に使う
# (未評価のカテゴリーがある場合はTrueを固定しない)。
_COUNTER_FACTOR_CATEGORIES = (
    "earnings_improvement",
    "guidance_upgrade",
    "dividend_increase",
    "buyback",
    "dividend_policy_maintained",
    "financial_capacity",
    "bank_regulatory_capital_buffer",
    "one_time_factor",
    "momentum",
    "single_major_risk",
)


@dataclass(frozen=True)
class SellSignalOutcome:
    stock_code: str
    recommendation: Recommendation | None
    data_error: str | None
    # 保有判断スコアのShadow比較用監査情報(実装プラン15節)。旧エンジンが
    # 判定根拠とした個別ルール名(TRIGGERED分)。データ取得失敗時は空。
    triggered_rule_names: tuple[str, ...] = ()
    # Phase 2-B「銘柄分析」向け(2026-08): この判定サイクルで実際に書き込んだ
    # AuditLogEntryのID(judgment audit呼び出しに到達しなかった場合はNone)。
    # HoldingEvaluationRecord.authoritative_audit_log_idへ橋渡しするための参照。
    audit_id: str | None = None


def _evidence_detail_dict(e: SellRuleEvaluation) -> dict[str, object]:
    return {
        "rule_name": e.rule_name,
        "status": e.status.value,
        "severity": e.severity,
        "evidence_group": e.evidence_group.value,
        "is_immediate_critical": e.is_immediate_critical,
        "metric_name": e.metric_name,
        "current_value": e.current_value,
        "previous_value": e.previous_value,
        "threshold": e.threshold,
        "comparison_period": e.comparison_period,
        "primary_source_confirmed": e.primary_source_confirmed,
        "source": e.source,
        "explanation": e.explanation,
    }


def _build_action_summary(recommendation_type: RecommendationType) -> str:
    if recommendation_type == RecommendationType.URGENT_REVIEW:
        return "即時性の高い重大な悪化事象が検出されました。速やかに内容を確認してください。"
    if recommendation_type == RecommendationType.SELL:
        return "複数の独立した根拠に基づき投資前提の悪化が疑われます。売却を検討してください。"
    return (
        "投資前提の悪化を示唆する事象が検出されましたが、根拠が単一のため自動的な"
        "売却判断は行いません。内容を確認し、必要に応じて追加情報を収集してください。"
    )


def _evaluate_counter_factors(
    snapshot: StockSnapshot, triggered_count: int
) -> tuple[list[str], bool]:
    """反対材料(counter_factors)を評価する(2026-07仕様レビュー対応)。

    最低限、増益・業績上方修正・増配・自社株買い・配当方針維持・財務余力・
    銀行の規制資本余力・一過性要因・モメンタム・重大リスクが単一であること、
    の各カテゴリーを評価対象とする。評価できないカテゴリーが1件でもあれば
    counter_factors_evaluated=Falseとする(固定Trueにしない)。
    """
    factors: list[str] = []
    evaluated: dict[str, bool] = {}

    # 増益
    incomes = snapshot.quarterly_operating_income_periods
    if len(incomes) >= 2:
        evaluated["earnings_improvement"] = True
        if incomes[-1].value > incomes[-2].value:
            factors.append("直近期は営業利益が改善している")
    else:
        evaluated["earnings_improvement"] = False

    # 業績上方修正
    guidance_upgrade_found = any(
        "上方修正" in f"{d.title} {d.summary or ''}" for d in snapshot.disclosures
    )
    evaluated["guidance_upgrade"] = True  # 開示テキストは常に検索可能(該当なしも評価済み)
    if guidance_upgrade_found:
        factors.append("業績予想の上方修正が確認されている")

    # 増配
    dividend = snapshot.dividend
    increase_years = dividend.consecutive_dividend_increase_years
    evaluated["dividend_increase"] = increase_years is not None
    if increase_years is not None and increase_years > 0:
        factors.append(f"配当は{increase_years}期連続増配中")

    # 自社株買い
    buyback_found = any(
        "自己株式取得" in f"{d.title} {d.summary or ''}"
        or "自社株買い" in f"{d.title} {d.summary or ''}"
        for d in snapshot.disclosures
    )
    evaluated["buyback"] = True
    if buyback_found:
        factors.append("自社株買いの実施が確認されている")

    # 配当方針維持
    # Issue #30 Phase 1: is_progressive_or_doe_policyの3状態化(bool | None)に伴う
    # 型整合のため `is True` を明示する。SELL側の評価coverage semanticsは不変
    # (旧実装はFalse(当時は「方針なし」と「取得不能」の混在)を「評価できず」
    # 扱いにしており、None(UNKNOWN)も同じ結果になる。挙動完全保存)。
    evaluated["dividend_policy_maintained"] = dividend.has_dividend_floor_policy is not None or (
        dividend.is_progressive_or_doe_policy is True
    )
    if dividend.is_progressive_or_doe_policy or dividend.has_dividend_floor_policy:
        factors.append("累進的配当方針・配当下限方針が維持されている")

    # 財務余力(一般事業会社のみ評価可能)
    industry = classify_industry(snapshot.financial.sector, snapshot.financial.industry)
    if industry.classification == IndustryClassification.GENERAL_CORPORATE:
        evaluated["financial_capacity"] = snapshot.financial.equity_ratio_pct is not None
        if (
            snapshot.financial.equity_ratio_pct is not None
            and snapshot.financial.equity_ratio_pct >= 40.0
        ):
            factors.append(f"自己資本比率({snapshot.financial.equity_ratio_pct:.1f}%)は良好な水準")
    elif industry.classification == IndustryClassification.FINANCIAL:
        # 銀行の規制資本余力: データソースが無いため常に未評価
        evaluated["bank_regulatory_capital_buffer"] = False

    # 一過性要因
    fundamentally_driven = is_fundamentally_driven(snapshot.cashflow_decomposition)
    evaluated["one_time_factor"] = fundamentally_driven is not None

    # モメンタム
    evaluated["momentum"] = snapshot.momentum.ma20 is not None

    # 重大リスクが単一であること(常に評価可能。TRIGGERED件数から直接判定できる)
    evaluated["single_major_risk"] = True
    if triggered_count <= 1:
        factors.append("検出された重大な懸念事項は1件のみ")

    counter_factors_evaluated = all(evaluated.values())
    return factors, counter_factors_evaluated


def _build_next_review_conditions(
    evidence_details: list[SellRuleEvaluation], next_earnings_date: dt.date | None
) -> list[str]:
    conditions: list[str] = []
    if next_earnings_date is not None:
        conditions.append(f"次回決算発表({next_earnings_date})後に本判定を再評価する")
    if any(
        e.rule_name in ("financial_health_severe_deterioration", "regulatory_capital_breach")
        and e.status == TriggerStatus.NOT_EVALUATED
        for e in evidence_details
    ):
        conditions.append("銀行・保険等専用の財務健全性指標が取得可能になり次第、再評価する")
    return conditions


def _build_holding_risks(evidence_details: list[SellRuleEvaluation]) -> list[str]:
    return [
        e.explanation
        for e in evidence_details
        if e.status == TriggerStatus.TRIGGERED
        and e.severity in ("critical", "major")
        and e.explanation
    ]


_DEFAULT_EXECUTION_CONTEXT = ExecutionContext.normal()


class SellSignalService:
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

    def _compute_confidence_level(
        self,
        result: SellSignalResult,
        snapshot: StockSnapshot,
        now: dt.datetime,
        counter_factors_evaluated: bool,
    ) -> ConfidenceScoreResult:
        industry_unevaluated = any(
            e.rule_name in ("financial_health_severe_deterioration", "regulatory_capital_breach")
            and e.status == TriggerStatus.NOT_EVALUATED
            for e in result.evidence_details
        )
        data_freshness_days = (now - snapshot.data_fetched_at).days
        # 営業日ベース(要求仕様レビュー対応。土日祝日を除く)。デプロイ前対応で
        # snapshot側の一元計算値(JST基準)を使用する。
        days_to_earnings = snapshot.business_days_to_earnings

        triggered = [e for e in result.evidence_details if e.status == TriggerStatus.TRIGGERED]
        primary_source_fetch_rate = (
            sum(1 for e in triggered if e.primary_source_confirmed) / len(triggered)
            if triggered
            else None
        )

        factors = ConfidenceFactors(
            data_freshness_days=data_freshness_days,
            primary_source_fetch_rate=primary_source_fetch_rate,
            days_to_next_earnings_business_days=days_to_earnings,
            latest_quarter_fetched=bool(snapshot.quarterly_operating_income_periods),
            record_date_known=snapshot.dividend.dividend_record_date is not None,
            key_metric_missing=snapshot.financial.equity_ratio_pct is None,
            independent_evidence_group_count=result.independent_evidence_group_count,
            industry_specific_model_unavailable=industry_unevaluated,
            evidence_sourced_from_yfinance_only=result.all_evidence_yfinance_only,
            dividend_breakdown_confirmed=snapshot.dividend.dividend_breakdown_confirmed,
            counter_factors_evaluated=counter_factors_evaluated,
        )
        return compute_confidence(factors, self._config.confidence)

    def analyze(
        self, holding: Holding, now: dt.datetime, snapshot: StockSnapshot | None = None
    ) -> SellSignalOutcome:
        """snapshotを渡すと再取得を省略する(profit_takingと同一銘柄を二重に取得する
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
                decision_type="sell_signal",
                stock_code=holding.stock_code,
                input_values={},
                calculation_formulas={},
                output_values={"data_error": error},
                data_sources=[],
                rule_version=self._active_rule_version(),
                timestamp=now,
            )
            return SellSignalOutcome(holding.stock_code, None, error)

        inputs = build_sell_rule_inputs_from_data(
            dividend=snapshot.dividend,
            financial=snapshot.financial,
            benefit=snapshot.benefit,
            quarterly_operating_income_periods=snapshot.quarterly_operating_income_periods,
            quarterly_operating_cashflow_periods=snapshot.quarterly_operating_cashflow_periods,
            disclosure_risk_keywords_found=snapshot.disclosure_risk_keywords_found,
            config=self._config.sell,
            cashflow_decomposition=snapshot.cashflow_decomposition,
            material_event_keywords_found=snapshot.material_event_keywords_found,
        )

        result = evaluate_sell_signal(inputs, snapshot.current_price, self._config.sell)
        raw_recommendation_type = result.recommendation_type

        recommendation_type = result.recommendation_type
        downgraded_reason: str | None = None
        sell_prices = SellPriceLevels(
            immediate_execution_price=result.immediate_execution_price,
            stop_review_price=result.stop_review_price,
        )
        if recommendation_type in _STRONG_TYPES and result.all_evidence_yfinance_only:
            # 要求仕様§12: 根拠がすべてyfinance等の二次情報のみの場合、SELL/URGENT_REVIEWを
            # 出さずREVIEWへ格下げする。格下げ後は即時執行を意味する価格フィールドを
            # 必ずnullにする(レビュー対応: 格下げ後に強い行動提案の価格だけが
            # 残る矛盾を防ぐ)。
            downgraded_reason = (
                f"{recommendation_type.value}の根拠がすべて一次情報未確認のためREVIEWへ格下げ"
            )
            recommendation_type = RecommendationType.REVIEW
            sell_prices = SellPriceLevels(
                immediate_execution_price=None,
                recommended_limit_price=None,
                stop_review_price=None,
            )

        triggered_count = sum(
            1 for e in result.evidence_details if e.status == TriggerStatus.TRIGGERED
        )
        counter_factors, counter_factors_evaluated = _evaluate_counter_factors(
            snapshot, triggered_count
        )
        confidence_result = self._compute_confidence_level(
            result, snapshot, now, counter_factors_evaluated
        )

        audit_entry = self._audit.record(
            decision_type="sell_signal",
            stock_code=holding.stock_code,
            input_values={
                **inputs.as_dict(),
                # Phase 2-B「銘柄分析」向け(2026-08、証跡拡張): 真偽値だけでなく
                # 各ルールの構造化評価(現行値・閾値・説明文等)を、TRIGGERED/
                # NOT_TRIGGERED/NOT_EVALUATEDを問わず全17ルール分保存する
                # (既存のas_dict()は後方互換のため維持)。HOLD時もこの時点で
                # 記録されるため、以前は失われていた個別ルールの事実が残る。
                "rule_evidence_details": [
                    _evidence_detail_dict(e) for e in inputs.evaluations.values()
                ],
            },
            calculation_formulas={
                "judgment": (
                    "即時性criticalが1件以上 -> URGENT_REVIEW; "
                    "独立major2件以上 or 独立critical2件以上 or "
                    "(critical+独立major1件以上) -> SELL; "
                    "major/criticalいずれか1件以上 -> REVIEW; それ以外 -> HOLD; "
                    "根拠が全てyfinance等の二次情報のみの場合はSELL/URGENT_REVIEWをREVIEWへ格下げ"
                ),
            },
            output_values={
                "recommendation_type": recommendation_type.value,
                "raw_recommendation_type": result.recommendation_type.value,
                "downgraded_reason": downgraded_reason,
                "triggered_rules": result.triggered_rules,
                "reasons": result.reasons,
                "independent_evidence_group_count": result.independent_evidence_group_count,
                "confidence": confidence_result.level.value,
                "confidence_score": confidence_result.score,
                "confidence_reasons": confidence_result.reasons_not_high,
            },
            data_sources=list(snapshot.data_sources),
            rule_version=self._active_rule_version(),
            timestamp=now,
        )

        if recommendation_type == RecommendationType.HOLD:
            return SellSignalOutcome(
                holding.stock_code,
                None,
                None,
                tuple(result.triggered_rules),
                audit_id=audit_entry.audit_id,
            )

        evidence_details = result.evidence_details

        # 判定精度向上機能次フェーズSTEP2: Exit Price Range(Shadow計測)。
        # SELL(legacy)判定が実際にRecommendationを構築すると決まった時点
        # (HOLD等の早期returnより後)でのみ計算する。既存の早期return・
        # 判定条件分岐の実行順は一切変更しない。Builder(holding_decision_
        # notification_builder.py)は算出せずコピーのみ行う設計のため、ここが
        # 唯一の算出箇所。
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
            owner=holding.owner,
            holding_id=holding.holding_id,
            stock_code=holding.stock_code,
            stock_name=snapshot.financial.stock_name or holding.stock_name,
            recommended_at=now,
            recommendation_type=recommendation_type,
            raw_recommendation_type=raw_recommendation_type,
            sell_prices=sell_prices,
            price_at_recommendation=snapshot.current_price,
            average_purchase_price_at_recommendation=holding.average_purchase_price,
            shares_at_recommendation=holding.shares,
            dividend_yield_pct_at_recommendation=snapshot.dividend_yield_pct,
            shareholder_benefit_yield_pct_at_recommendation=snapshot.benefit_yield_pct,
            total_yield_pct_at_recommendation=snapshot.total_yield_pct,
            fair_value_at_recommendation=snapshot.fair_value,
            reasons=result.reasons,
            counter_factors=counter_factors,
            key_risks=[f"該当ルール: {', '.join(result.triggered_rules)}"],
            confidence=confidence_result.level,
            next_earnings_date=snapshot.next_earnings_date,
            dividend_record_date=snapshot.dividend.dividend_record_dates[0]
            if snapshot.dividend.dividend_record_dates
            else None,
            benefit_record_date=snapshot.benefit.benefit_record_dates[0]
            if snapshot.benefit is not None and snapshot.benefit.benefit_record_dates
            else None,
            rule_version=self._active_rule_version(),
            config_values_used={
                "triggered_rules": result.triggered_rules,
                "independent_evidence_group_count": result.independent_evidence_group_count,
                "downgraded_reason": downgraded_reason,
                "raw_recommendation_type": raw_recommendation_type.value,
                "counter_factors_evaluated": counter_factors_evaluated,
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
            },
            data_sources=list(snapshot.data_sources),
            recommended_action_summary=_build_action_summary(recommendation_type),
            next_review_conditions=_build_next_review_conditions(
                evidence_details, snapshot.next_earnings_date
            ),
            holding_risks=_build_holding_risks(evidence_details),
            evidence_details=[_evidence_detail_dict(e) for e in evidence_details],
            independent_evidence_group_count=result.independent_evidence_group_count,
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
        return SellSignalOutcome(
            holding.stock_code,
            recommendation,
            None,
            tuple(result.triggered_rules),
            audit_id=audit_entry.audit_id,
        )
