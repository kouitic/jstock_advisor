"""買い判定サービス(2026-07 BUYパイプライン再設計、および第2次修正)。

「企業として投資候補になり得るか(company_quality_score)」と「現在の株価で
実際に購入すべきか(purchase_attractiveness_score + BuyAction)」を分離した
3段階パイプラインをオーケストレーションする。処理順序は以下の22ステップ
(第2次修正で決算日stale判定・下方外れ値除外・買付価格信頼性ゲートを追加):

1. データ品質検証(決算日の妥当性検証を含む) 2. 投資対象スクリーニング
3. 業種分類 4. 利益/EPSの平準化 5. 各方式の適正価格算出
6. 不適用方式と外れ値の除外(DCF上方乖離+下方外れ値フィルタ) 7. 適正価格のばらつき判定
8. valuation_anchor算出 9. 適正価格信頼度決定 10. 必要安全余裕率算出(カテゴリ集約方式)
10.5. 買付価格信頼性ゲート 11. 3段階買付価格算出 12. company_quality_score算出
13. purchase_attractiveness_score算出 14. 現在価格によるBuyAction仮判定
15. スコアによる格下げ 16. 決算直前調整 16.5. 買付価格信頼性による格下げ
17. データ品質・業種モデルによる格下げ(margin加算に反映済み) 18. 整合性検証
19-20. 購入候補/価格待ちランキング用の情報確定 21. 通知生成(通知層) 22. 監査ログ保存
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.classification.buy_industry import (
    CYCLICAL_SECTORS,
    buy_industry_model_missing_reason,
    classify_buy_industry_sector,
)
from jstock_advisor.domain.classification.canonical_industry import classify_canonical_industry
from jstock_advisor.domain.classification.financial_industry import classify_industry
from jstock_advisor.domain.classification.profit_taking_industry import (
    classify_profit_taking_industry_sector,
)
from jstock_advisor.domain.entities.buy_decision import BuyDecisionReason
from jstock_advisor.domain.entities.enums import (
    BUY_FAMILY_ACTIONS,
    BuyAction,
    BuyIndustrySector,
    ConfidenceLevel,
    RecommendationType,
    StockType,
    WatchTransitionType,
    WatchType,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.valuation import FairValueMethodResult
from jstock_advisor.domain.financial_series import FinancialPeriodValue
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.domain.scoring.score import compute_score
from jstock_advisor.domain.scoring.undervaluation_categories import (
    UndervaluationCategoryDetail,
    build_undervaluation_category_details,
)
from jstock_advisor.domain.screening.rules import evaluate_screening
from jstock_advisor.domain.signals.buy_consistency import validate_buy_recommendation
from jstock_advisor.domain.signals.buy_decision import (
    compute_purchase_attractiveness_score,
    decide_buy_action,
    screen_investment_universe,
)
from jstock_advisor.domain.signals.buy_signal import (
    compute_drawdown_from_52w_high_pct,
    compute_recent_price_change_pct,
    compute_undervaluation_signals,
    estimate_historical_average_dividend_yield_pct,
    is_earnings_trend_non_decreasing,
    score_areas,
    undervaluation_signal_threshold_values,
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
from jstock_advisor.domain.signals.eps_normalization import normalize_eps
from jstock_advisor.domain.signals.historical_valuation import (
    historical_valuation_config_values,
    historical_valuation_result_to_metrics,
)
from jstock_advisor.domain.signals.market_environment import (
    market_environment_config_values,
    market_environment_result_to_metrics,
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
from jstock_advisor.domain.valuation.buy_price_levels import compute_buy_price_levels
from jstock_advisor.domain.valuation.buy_price_reliability import determine_buy_price_reliability
from jstock_advisor.domain.valuation.fair_value import (
    compute_52_week_low,
    compute_dcf_price,
    compute_historical_range_price,
    compute_pbr_price,
    compute_per_price,
    compute_target_yield_price,
    median_historical_pbr,
    median_historical_per,
)
from jstock_advisor.domain.valuation.margin_of_safety import compute_margin_of_safety
from jstock_advisor.domain.valuation.valuation_confidence import determine_valuation_confidence
from jstock_advisor.domain.valuation.valuation_methods import (
    apply_dcf_divergence_filter,
    build_valuation_summary,
    compute_valuation_anchor,
    determine_dispersion_band,
)
from jstock_advisor.infrastructure.local_repository.holdings_snapshot_repository import (
    HoldingsSnapshotRepository,
)
from jstock_advisor.interfaces.disclosure import DisclosureAvailability
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.jpx_industry_source import (
    JpxIndustrySource,
    get_default_jpx_industry_source,
)
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.rule_version_service import RuleVersionService
from jstock_advisor.services.stock_snapshot_service import StockSnapshot, build_stock_snapshot
from jstock_advisor.services.watch_state_service import WatchStateService

# アクティブなRuleVersionが未登録の場合(初期運用時)のフォールバック値
RULE_VERSION_PLACEHOLDER = "v1-mvp"
_DEFAULT_EXECUTION_CONTEXT = ExecutionContext.normal()

_STRONG_SCORE_RATIO = 0.7
_WEAK_SCORE_RATIO = 0.3

# --- Issue #22 Phase 3.5(2026-08-28): 観測用snapshotの正式schema versioning ---
# buy_score_input_factsのschema versionはこのPhase 3.5から正式に開始する。
# このキーを持たない既存Recommendation(2026-08-28以前)はLEGACY_UNVERSIONED
# として扱う(キー数から世代を推測しない。backfillもしない)。optional keyの
# 追加だけで互換性を壊さない場合は必ずしもversionを上げる必要はない。
FACTS_SCHEMA_VERSION = "v1"

# 観測用に保存する財務時系列(営業利益・営業CF・EPS)の1系列あたり保存上限
# (直近N期のみ保存)。providerが将来取得期間を拡大してもRecommendation
# payloadが無制限に増加しないための上限であり、以下の実際の利用要件から決定:
# - cf_streak(保有判断側config cf_streak_quartersのfull_at=4)は4期で飽和
#   → 8期あれば飽和+観測余裕を持って streak を判別できる
# - eps_stability(holding_decision_ratio_rules.yaml
#   min_periods_for_stability_score=3)は最低3期
# - TTM変換(financial_series._TTM_WINDOW=4)は4期。将来providerが四半期粒度を
#   返せるようになった場合でも8四半期=TTM系列5点分を確保できる
# - 現行yfinance providerは年次4〜5期しか返さないため、8は現状の実データを
#   一切切り捨てない
_FACTS_SERIES_MAX_PERIODS = 8

# 割安シグナルが「評価の結果False/None」ではなく「上位ルール(重大業績悪化)に
# より抑止された」ことを表すreason_code(観測用。stateには混ぜず、stateは
# EVALUATED/NOT_EVALUATED/NOT_APPLICABLEの3値に統一する)。
#
# 正式な観測仕様として、抑止は以下の2形式を区別する(v1の
# compute_undervaluation_signals()の実際の挙動と一致させた定義。
# コードレビューPASS_WITH_CONDITIONS対応、2026-08-28):
#   形式1: value=False + 本reason_code
#     入力から本来のbool評価が可能だったが、上位ルールによりFalseへ強制された
#     (drawdown_from_52w_high / below_fair_value が該当。v1は一度bool値を
#      算出した後、severe_earnings_decline=TrueならFalseで上書きする)
#   形式2: value=None + 本reason_code
#     必要入力自体は揃っていたが、上位ルールによりシグナル評価そのものを
#     実施しなかった(price_down_despite_stable_earnings が該当。v1は
#     severe_earnings_decline=Trueの場合、評価式自体へ入らずNoneのままとする)
# いずれの形式でも、reason_codeが無いvalue=Noneは「必要入力の不足による
# 判定不能」を意味する(抑止とは区別される)。
_REASON_SUPPRESSED_BY_SEVERE_EARNINGS_DECLINE = "SUPPRESSED_BY_SEVERE_EARNINGS_DECLINE"


def _serialize_period_series(periods: list[FinancialPeriodValue]) -> list[dict[str, object]]:
    """FinancialPeriodValue系列を観測用snapshotへ直列化する(直近N期のみ)。

    period_typeを必ず併存保存する(ANNUAL_FALLBACK環境では「期」が年を意味し、
    「4期連続」を四半期と断定できないため。FinancialPeriodValueのdocstring参照)。
    """
    ordered = sorted(periods, key=lambda p: p.period_end)[-_FACTS_SERIES_MAX_PERIODS:]
    return [
        {
            "value": str(p.value),
            "period_end": p.period_end.isoformat(),
            "period_type": p.period_type.value,
        }
        for p in ordered
    ]


def _trailing_positive_streak(periods: list[FinancialPeriodValue]) -> int:
    """直近から連続で正値が続く期数(company_quality_scoring._trailing_positive_streak
    と同一ロジック。保有判断側のprivate関数のため、観測用に同じ4行をここへ持つ。
    判定ロジックには使用しない観測専用)。
    """
    ordered = sorted(periods, key=lambda p: p.period_end)
    streak = 0
    for period in reversed(ordered):
        if period.value > 0:
            streak += 1
        else:
            break
    return streak


def _serialize_undervaluation_categories(
    details: list[UndervaluationCategoryDetail],
    suppressed_signal_reasons: dict[str, str],
) -> list[dict[str, object]]:
    """割安度4カテゴリの判定時点明細を観測用snapshotへ直列化する。

    signal_resultsは{"value": bool|None, "reason_code": str|None}形式。
    抑止(SUPPRESSED_*)は「value=Falseへの強制(形式1)」と「評価自体の
    未実施によるvalue=None(形式2)」の2形式があり、valueだけでは
    抑止の有無を判別できない。意味定義の詳細は
    _REASON_SUPPRESSED_BY_SEVERE_EARNINGS_DECLINEのコメント参照
    (Issue #22 Phase 3.5)。
    """
    payload: list[dict[str, object]] = []
    for detail in details:
        reason_codes: list[str] = []
        if detail.signals_available == 0:
            reason_codes.append("NO_SIGNALS_AVAILABLE")
        elif detail.signals_available < detail.signals_defined:
            reason_codes.append("PARTIAL_SIGNAL_COVERAGE")
        if any(name in suppressed_signal_reasons for name in detail.signal_results):
            reason_codes.append(_REASON_SUPPRESSED_BY_SEVERE_EARNINGS_DECLINE)
        signal_results = {
            name: {
                "value": value,
                "reason_code": suppressed_signal_reasons.get(name),
            }
            for name, value in detail.signal_results.items()
        }
        payload.append(
            {
                "category": detail.category,
                "score": detail.score,
                "cap": detail.cap,
                "signals_met": detail.signals_met,
                "signals_available": detail.signals_available,
                "signals_defined": detail.signals_defined,
                "state": detail.state.value,
                "reason_codes": reason_codes,
                "signal_results": signal_results,
            }
        )
    return payload


logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class BuyAnalysisOutcome:
    stock_code: str
    recommendation: Recommendation | None
    screening_passed: bool
    exclusion_reasons: list[str]
    data_error: str | None
    # --- BUYパイプライン再設計(2026-07)で追加 ---
    buy_action: BuyAction | None = None
    # "buy_candidate" | "watch_price" | "excluded" | None(データ不足等)
    ranking_group: str | None = None


class BuySignalService:
    def __init__(
        self,
        providers: ProviderBundle,
        config: AppConfig,
        business_calendar: BusinessCalendar,
        audit_service: AuditService | None = None,
        rule_version_service: RuleVersionService | None = None,
        execution_context: ExecutionContext = _DEFAULT_EXECUTION_CONTEXT,
        watch_state_service: WatchStateService | None = None,
        holdings_snapshot_repository: HoldingsSnapshotRepository | None = None,
        jpx_industry_source: JpxIndustrySource | None = None,
    ) -> None:
        self._providers = providers
        self._config = config
        self._calendar = business_calendar
        self._audit = audit_service or AuditService(execution_context=execution_context)
        self._rule_version_service = rule_version_service or RuleVersionService()
        # --- BUY候補裾野拡大機能(2026-08) ---
        self._watch_state_service = watch_state_service or WatchStateService(
            business_calendar=business_calendar, execution_context=execution_context
        )
        self._holdings_snapshot_repo = (
            holdings_snapshot_repository
            or HoldingsSnapshotRepository.for_execution_context(execution_context)
        )
        # --- Issue #54 Phase B-1(2026-08-29): 業種分類canonical観測用 ---
        # 観測専用。解決できなくても判定は従来どおり継続する。
        self._jpx_industry_source = jpx_industry_source or get_default_jpx_industry_source()

    def _observe_canonical_industry(
        self,
        stock_code: str,
        snapshot: StockSnapshot,
        buy_industry_sector: BuyIndustrySector,
        is_growth_stock: bool,
    ) -> dict[str, object]:
        """業種分類のcanonical観測(Issue #54 Phase B-1、**判定へは一切影響しない**)。

        同一銘柄に対して、canonical(JPX 33業種)と既存4分類器が実際に何を返したかを
        並べて記録する。目的は次の2点をProductionデータで確認することであり、
        ここで分類を是正することではない。

          1. JPXでcanonical業種を解決できる銘柄の割合(BUY経路はJPX universeを
             通らないため、キャッシュ経由で引けるかどうかが未知)
          2. 既存分類器どうしの不一致・死んだ判定(CYCLICAL/DEFENSIVE)の実際の発生率

        観測の失敗は判定を止めない(取得できなければUNAVAILABLEとして記録する)。
        """
        jpx_entry = self._jpx_industry_source.get(stock_code)
        canonical = classify_canonical_industry(
            industry_33_code=jpx_entry.industry_33_code if jpx_entry else None,
            industry_33_name=jpx_entry.industry_33_name if jpx_entry else None,
            market_segment=jpx_entry.market_segment if jpx_entry else None,
            fallback_sector=snapshot.financial.sector,
            fallback_industry=snapshot.financial.industry,
        )
        financial_result = classify_industry(
            snapshot.financial.sector, snapshot.financial.industry
        )
        return {
            "canonical_industry_33_code": canonical.industry_33_code,
            "canonical_industry_33_name": canonical.industry_33_name,
            "canonical_security_type": canonical.security_type.value,
            "canonical_source": canonical.source.value,
            "provider_sector": canonical.fallback_sector,
            "provider_industry": canonical.fallback_industry,
            # 既存分類器が同一入力に対して実際に返した値(是正はしない)。
            "financial_industry_classification": financial_result.classification.value,
            "financial_industry_category": (
                financial_result.financial_category.value
                if financial_result.financial_category is not None
                else None
            ),
            "buy_industry_sector": buy_industry_sector.value,
            # 利確側の分類器は保有経路の責務だが、同じsector/industryからの純粋関数
            # であるため、ここで同一入力に対する結果を並べて不一致率を観測できる
            # (保有経路のコードは変更しない)。
            "profit_taking_industry_sector": classify_profit_taking_industry_sector(
                snapshot.financial.industry,
                snapshot.financial.sector,
                is_growth_stock,
            ).value,
            # CYCLICAL/DEFENSIVEは現状ほぼ付与されない(日本語キーワードを英語GICSへ
            # 部分一致させているため)。実際の発生率を観測する。
            "stock_type_cyclical_or_defensive": [
                stock_type.value
                for stock_type in snapshot.stock_type_classification.types
                if stock_type in (StockType.CYCLICAL, StockType.DEFENSIVE)
            ],
            # providerが生成したsecurity_type / market_segment。yfinance実装は
            # market_segment=None・security_type既定値"STOCK"を返すため、REIT除外が
            # 到達不能である現状をProductionデータで裏づけるために記録する。
            "provider_security_type": snapshot.financial.security_type,
            "provider_market_segment": snapshot.financial.market_segment,
        }

    def _active_rule_version(self) -> str:
        return self._rule_version_service.get_active_version_or(RULE_VERSION_PLACEHOLDER)

    def analyze(
        self,
        stock_code: str,
        now: dt.datetime,
        recommendation_type: RecommendationType = RecommendationType.BUY,
        snapshot: StockSnapshot | None = None,
    ) -> BuyAnalysisOutcome:
        """snapshotを渡すと再取得を省略する(SellSignalService/ProfitTakingServiceと
        同じ規約。統合BUY候補パイプラインで保有銘柄を評価する際、同一銘柄の売却・
        利確判定と現在値・財務データを完全に一致させるために使う)。
        """
        # --- 1. データ品質検証(スナップショット取得) ---
        error: str | None = None
        if snapshot is None:
            snapshot, error = build_stock_snapshot(
                self._providers, stock_code, now, self._config, business_calendar=self._calendar
            )
        if snapshot is None:
            self._audit.record(
                decision_type="buy_signal",
                stock_code=stock_code,
                input_values={},
                calculation_formulas={},
                output_values={
                    "data_error": error,
                    "final_buy_action": BuyAction.DATA_INSUFFICIENT.value,
                },
                data_sources=[],
                rule_version=self._active_rule_version(),
                timestamp=now,
            )
            return BuyAnalysisOutcome(
                stock_code,
                None,
                False,
                [],
                error,
                buy_action=BuyAction.DATA_INSUFFICIENT,
                ranking_group=None,
            )

        # Issue #53 Phase B2: 開示情報を調査できなかった場合は、リスク開示の有無を
        # 判断できない。「開示リスクを検出した(DISCLOSURE_RISK)」でも「問題なし」でも
        # なく、評価不能(DATA_INSUFFICIENT)として扱い新規買い候補から外す
        # (「不祥事を発見した」と「開示情報を調査できなかった」を混同しない)。
        if snapshot.disclosure_availability is DisclosureAvailability.UNAVAILABLE:
            reason = (
                "開示情報を取得できなかったため評価できません"
                f"(理由区分: {snapshot.disclosure_unavailable_reason})"
            )
            logger.warning(
                "buy_signal disclosure unavailable stock_code=%s reason=%s",
                stock_code,
                snapshot.disclosure_unavailable_reason,
            )
            self._audit.record(
                decision_type="buy_signal",
                stock_code=stock_code,
                input_values={},
                calculation_formulas={},
                output_values={
                    "data_error": reason,
                    "disclosure_availability": snapshot.disclosure_availability.value,
                    "final_buy_action": BuyAction.DATA_INSUFFICIENT.value,
                },
                data_sources=list(snapshot.data_sources),
                rule_version=self._active_rule_version(),
                timestamp=now,
            )
            return BuyAnalysisOutcome(
                stock_code,
                None,
                False,
                [],
                reason,
                buy_action=BuyAction.DATA_INSUFFICIENT,
                ranking_group=None,
            )

        # Issue #23(2026-08-28): JPX BusinessCalendarへ渡すdateは「JPX営業日を
        # 表すJST calendar date」とする(domain/screening/rules.pyの同一計算と
        # 同じ理由・同じ基準。両端を必ずJST暦日へ揃える)。この値はPhase 3.5の
        # 観測snapshot(facts["data_age_business_days"])へもそのまま保存される
        # (「実際に判定に使用した値を保存する」というPhase 3.5の設計原則どおり)。
        data_age_days = self._calendar.business_days_between(
            evaluation_date_jst(snapshot.data_fetched_at), evaluation_date_jst(now)
        )
        has_stale_data_warning = data_age_days > 1

        # --- 2. 投資対象スクリーニング(第1段階) ---
        screening_result = evaluate_screening(
            financial=snapshot.financial,
            dividend=snapshot.dividend,
            average_trading_value_yen=snapshot.avg_trading_value,
            disclosure_risk_keywords_found=snapshot.disclosure_risk_keywords_found,
            data_fetched_at=snapshot.data_fetched_at,
            now=now,
            business_calendar=self._calendar,
            config=self._config.screening,
        )
        screening_outcome = screen_investment_universe(
            screening_result, snapshot.severe_earnings_decline, snapshot.benefit
        )

        if not screening_outcome.passed:
            self._audit.record(
                decision_type="buy_signal",
                stock_code=stock_code,
                input_values={"current_price": str(snapshot.current_price)},
                calculation_formulas={},
                output_values={
                    "screening_passed": False,
                    "exclusion_reasons": screening_outcome.exclusion_reasons,
                    "final_buy_action": BuyAction.EXCLUDED.value,
                    "ranking_group": "excluded",
                    "notification_suppression_reason": "SCREENING_EXCLUDED",
                },
                data_sources=list(snapshot.data_sources),
                rule_version=self._active_rule_version(),
                timestamp=now,
            )
            return BuyAnalysisOutcome(
                stock_code,
                None,
                screening_result.passed,
                screening_outcome.exclusion_reasons,
                None,
                buy_action=BuyAction.EXCLUDED,
                ranking_group="excluded",
            )

        financial = snapshot.financial
        current_price = snapshot.current_price

        # --- 3. 業種分類 ---
        is_growth_stock = StockType.GROWTH in snapshot.stock_type_classification.types
        buy_industry_sector = classify_buy_industry_sector(
            financial.industry, financial.sector, is_growth_stock
        )
        # 専用の多変量モデルは未実装のため常にFalse(推測で補完しない方針、
        # profit_taking_industry.pyと同じ設計)。
        industry_model_applied = False
        is_cyclical_industry = (
            buy_industry_sector in CYCLICAL_SECTORS
            or StockType.CYCLICAL in snapshot.stock_type_classification.types
        )

        # --- 4. 利益/EPSの平準化 ---
        eps_result = normalize_eps(
            financial.forecast_eps, snapshot.historical_valuations, is_cyclical_industry
        )

        # --- 5. 各方式の適正価格算出(PERは平準化EPS対応) ---
        per_median = median_historical_per(snapshot.historical_valuations)
        pbr_median = median_historical_pbr(snapshot.historical_valuations)
        target_price = compute_target_yield_price(
            snapshot.dividend.forecast_annual_dividend_per_share,
            self._config.valuation.target_yield_method.target_dividend_yield_pct,
        )
        per_price = compute_per_price(eps_result.normalized_eps, per_median)
        pbr_price = compute_pbr_price(financial.forecast_bps, pbr_median)
        range_price = compute_historical_range_price(
            snapshot.bars,
            now.date(),
            self._config.valuation.historical_range_method.lookback_years,
            self._config.valuation.historical_range_method.use_52_week_low,
        )
        dcf_price = compute_dcf_price(
            financial.operating_cashflow,
            financial.capital_expenditure,
            financial.shares_outstanding,
            self._config.valuation.dcf_method.discount_rate_pct,
            self._config.valuation.dcf_method.terminal_growth_rate_pct,
            self._config.valuation.dcf_method.projection_years,
        )

        method_results = [
            FairValueMethodResult(
                method="target_yield",
                fair_value=target_price,
                confidence=ConfidenceLevel.HIGH,
                exclusion_reason=None
                if target_price is not None
                else "予想配当が取得できないため算出不可",
                source_date=financial.fiscal_period_end,
            ),
            FairValueMethodResult(
                method="per",
                fair_value=per_price,
                confidence=ConfidenceLevel.MEDIUM,
                exclusion_reason=None
                if per_price is not None
                else "平準化EPSまたは過去PER中央値が取得できない、もしくはEPSが負数のため算出不可",
                source_date=financial.fiscal_period_end,
            ),
            FairValueMethodResult(
                method="pbr",
                fair_value=pbr_price,
                confidence=ConfidenceLevel.MEDIUM,
                exclusion_reason=None
                if pbr_price is not None
                else "予想BPSまたは過去PBR中央値が取得できないため算出不可",
                source_date=financial.fiscal_period_end,
            ),
            FairValueMethodResult(
                method="historical_range",
                fair_value=range_price,
                confidence=ConfidenceLevel.MEDIUM,
                exclusion_reason=None
                if range_price is not None
                else "過去株価データが取得できないため算出不可",
            ),
            FairValueMethodResult(
                method="dcf",
                fair_value=dcf_price,
                # 固定割引率の簡易DCFのためMEDIUM上限(要求仕様8節・10節)。
                confidence=ConfidenceLevel.MEDIUM,
                exclusion_reason=None
                if dcf_price is not None
                else (
                    "営業CF・設備投資・発行済株式数のいずれかが取得できない、"
                    "またはFCFが負のため算出不可"
                ),
            ),
            # 業種別方式: 専用モデル未実装のため常に不適用(要求仕様9節・12節、正直に記録)。
            FairValueMethodResult(
                method="industry",
                fair_value=None,
                confidence=ConfidenceLevel.LOW,
                applicable=False,
                exclusion_reason=buy_industry_model_missing_reason(buy_industry_sector),
            ),
        ]

        # --- 6. 不適用方式と外れ値の除外(DCFの上方乖離フィルタ) ---
        dcf_result = next(r for r in method_results if r.method == "dcf")
        other_results = [r for r in method_results if r.method != "dcf"]
        filtered_dcf = apply_dcf_divergence_filter(dcf_result, other_results)
        method_results = [filtered_dcf if r.method == "dcf" else r for r in method_results]

        low_52_week = compute_52_week_low(snapshot.bars, now.date())
        valuation_summary = build_valuation_summary(
            method_results,
            self._config.valuation.fair_value_methods.aggregation_method,
            self._config.valuation.fair_value_methods.method_weights,
            self._config.valuation.fair_value_usability,
            current_price=current_price,
            low_52_week=low_52_week,
        )

        # --- 7. 適正価格のばらつき判定 ---
        dispersion_band = determine_dispersion_band(
            valuation_summary.valuation_dispersion_ratio,
            self._config.buy_decision.valuation_dispersion,
        )

        # --- 9. 適正価格信頼度決定(anchor算出より先に必要) ---
        valuation_confidence_result = determine_valuation_confidence(
            methods_used_count=valuation_summary.methods_used_count or 0,
            dispersion_ratio=valuation_summary.valuation_dispersion_ratio,
            dispersion_medium_max=self._config.buy_decision.valuation_dispersion.medium_max,
            dispersion_auto_buy_block=self._config.buy_decision.valuation_dispersion.auto_buy_block,
            industry_model_applied=industry_model_applied,
            uses_simplified_dcf=filtered_dcf.applicable,
            normalized_eps_confidence=eps_result.confidence if is_cyclical_industry else None,
        )
        valuation_confidence = valuation_confidence_result.level

        # --- 8. valuation_anchor算出 ---
        anchor_result = compute_valuation_anchor(
            valuation_summary,
            valuation_confidence,
            dispersion_band,
            self._config.valuation.fair_value_methods.method_weights,
        )
        valuation_anchor = anchor_result.anchor
        # レビュー対応(2026-08、NO_VALUATION_ANCHOR表示不備の是正): valuation_
        # confidence==LOWによる打ち切り理由はvaluation_confidence_result側に、
        # confidence自体はLOWでないのにweighted_median算出が失敗した場合の理由は
        # anchor_result側に格納される(排他的にどちらか一方のみ設定される)。
        # 表示層(StockAnalysisViewService)がこの原因を再判定しなくて済むよう、
        # 判定時点にどちらが発火したかをそのままbuy_score_input_factsへ
        # スナップショットする(下記929行付近参照)。
        no_valuation_anchor_blocking_reason = (
            valuation_confidence_result.blocking_reason or anchor_result.blocking_reason
        )

        # --- 決算日の妥当性検証(要求仕様12節)。コードレビュー対応により
        # build_stock_snapshot()へ一元化された(過去日はnext_earnings_date=None
        # として既に検証済み)。ここではsnapshotの検証済みフィールドをそのまま使う ---
        earnings_date_raw = snapshot.earnings_date_raw
        earnings_date_status = snapshot.earnings_date_status
        resolved_next_earnings_date = snapshot.next_earnings_date

        # 次回決算までの営業日数(§16、デプロイ前対応でsnapshot側の一元計算値を使用)
        business_days_to_earnings = snapshot.business_days_to_earnings
        data_quality_warning = has_stale_data_warning or business_days_to_earnings is None

        avg_trading_value = snapshot.avg_trading_value
        small_cap_or_low_liquidity = avg_trading_value is not None and avg_trading_value < Decimal(
            2
        ) * Decimal(str(self._config.screening.universe.min_avg_trading_value_20d_yen))
        earnings_trend_non_decreasing = is_earnings_trend_non_decreasing(
            snapshot.quarterly_operating_incomes
        )
        volatile_earnings = earnings_trend_non_decreasing is False
        temporary_earnings_boost_risk = (
            is_cyclical_industry
            and eps_result.normalized_eps is not None
            and financial.forecast_eps is not None
            and eps_result.normalized_eps < financial.forecast_eps * Decimal("0.95")
        )
        # 主要顧客への依存は自動車部品業種に構造的な特徴として一律加算する
        # (個社別の依存度データが無いため、業種特性に基づく判断に留める)。
        major_customer_dependency = buy_industry_sector == BuyIndustrySector.AUTOMOTIVE_PARTS

        # --- 10. 必要安全余裕率算出 ---
        adjustment_codes: list[str] = []
        earnings_config = self._config.buy_decision.earnings_window
        if business_days_to_earnings is not None:
            if business_days_to_earnings <= earnings_config.block_buy_business_days:
                adjustment_codes.append("earnings_within_3_business_days")
            elif business_days_to_earnings <= earnings_config.add_margin_business_days:
                adjustment_codes.append("earnings_within_7_business_days")
        dispersion_auto_block = self._config.buy_decision.valuation_dispersion.auto_buy_block
        if dispersion_band == "HIGH":
            if (
                valuation_summary.valuation_dispersion_ratio is not None
                and valuation_summary.valuation_dispersion_ratio > dispersion_auto_block
            ):
                adjustment_codes.append("very_high_valuation_dispersion")
            else:
                adjustment_codes.append("high_valuation_dispersion")
        # BUY候補裾野拡大機能(2026-08): 業種別モデルは現状1つも実装されておらず
        # 「適用可能なのに適用できなかった」ケースが存在しないため、全銘柄一律の
        # +5%安全余裕率加算はしない(industry_model_appliedは他の用途
        # (undervaluation_signals/compute_score/Recommendation記録)では引き続き
        # 使用する)。MarginAdjustments.industry_model_not_appliedの設定値自体は、
        # 将来実際に業種別モデルを実装した際に再利用できるよう残す。
        if is_cyclical_industry:
            adjustment_codes.append("cyclical_industry")
        if small_cap_or_low_liquidity:
            adjustment_codes.append("small_cap_or_low_liquidity")
        if volatile_earnings:
            adjustment_codes.append("volatile_earnings")
        if temporary_earnings_boost_risk:
            adjustment_codes.append("temporary_earnings_boost_risk")
        if major_customer_dependency:
            adjustment_codes.append("major_customer_dependency")
        if data_quality_warning:
            adjustment_codes.append("data_quality_warning")

        margin_result = compute_margin_of_safety(
            valuation_confidence, adjustment_codes, self._config.buy_decision.margin_of_safety
        )

        # --- 買付価格の信頼性ゲート(要求仕様6節)。機械的に算出した買付価格
        # をそのまま購入判断に使ってよいか怪しい場合はLOWとし、後続のBuyAction
        # 判定でBUY系への昇格を禁止する ---
        excluded_outlier_count = sum(
            1 for m in valuation_summary.methods_excluded if m.exclusion_detail is not None
        )
        # レビュー対応(2026-08、commit f546473再レビューで発覚): Recommendation.
        # valuation_methods(下のtuple(method_results))は、apply_outlier_filters()
        # 適用「前」のオブジェクトである(build_valuation_summary()内部でmodel_copy()
        # されたコピーにのみ外れ値フィルタのexclusion_detail/exclusion_reasonが
        # 設定され、method_results自体は書き換わらない)。そのためVALUATION_
        # OUTLIER_EXCLUDED concernの表示時にRecommendation.valuation_methodsの
        # exclusion_reasonを参照すると、外れ値以外の理由(算出不能・業種モデル
        # 未実装等)を外れ値理由として誤って表示しうる不備があった。実際に外れ値
        # フィルタで除外された方式・理由(exclusion_detailが設定されている
        # ものだけ)を判定時点のスナップショットとして別途保存する。
        valuation_outlier_exclusions: list[dict[str, object]] = [
            {
                "method": m.method,
                "code": m.exclusion_detail.code,
                "message": m.exclusion_detail.message,
                "actual_value": (
                    str(m.exclusion_detail.actual_value)
                    if m.exclusion_detail.actual_value is not None
                    else None
                ),
                "reference_value": (
                    str(m.exclusion_detail.reference_value)
                    if m.exclusion_detail.reference_value is not None
                    else None
                ),
            }
            for m in valuation_summary.methods_excluded
            if m.exclusion_detail is not None
        ]
        # レビュー対応(2026-08、NO_VALUATION_ANCHOR表示不備の是正): valuation_anchor
        # がNoneの場合(=BuyDecisionReason.code="NO_VALUATION_ANCHOR"が必ず発火する)、
        # その直接原因(方式間の乖離が大きすぎた/有効な方式が無かった等)を判定時点の
        # 実測値・基準値ごと構造化してスナップショットする。標準5方式の
        # exclusion_reasonだけでは説明できない集約段階の失敗(全方式が個別には
        # 有効なのに乖離が大きすぎてアンカーを一本化できない等)を、表示層が
        # 現在configを再取得せずに正しく説明できるようにするため。
        no_valuation_anchor_reason: dict[str, object] | None = None
        if valuation_anchor is None and no_valuation_anchor_blocking_reason is not None:
            no_valuation_anchor_reason = {
                "code": no_valuation_anchor_blocking_reason.code,
                "actual_value": (
                    str(no_valuation_anchor_blocking_reason.actual_value)
                    if no_valuation_anchor_blocking_reason.actual_value is not None
                    else None
                ),
                "threshold_value": (
                    str(no_valuation_anchor_blocking_reason.threshold_value)
                    if no_valuation_anchor_blocking_reason.threshold_value is not None
                    else None
                ),
            }
        reliability_result = determine_buy_price_reliability(
            margin_result=margin_result,
            maximum_entry_margin=self._config.buy_decision.margin_of_safety.maximum_margin.entry,
            valuation_dispersion_ratio=valuation_summary.valuation_dispersion_ratio,
            dispersion_medium_max=self._config.buy_decision.valuation_dispersion.medium_max,
            methods_used_count=valuation_summary.methods_used_count,
            data_quality_warning=data_quality_warning,
            earnings_date_status=earnings_date_status,
            excluded_outlier_count=excluded_outlier_count,
            outlier_filter_blocking_reason=valuation_summary.outlier_filter_blocking_reason,
        )
        buy_price_reliability = reliability_result.reliability

        # --- 11. 3段階買付価格算出 ---
        buy_price_levels = compute_buy_price_levels(valuation_anchor, margin_result)

        # --- 12. company_quality_score算出(第2段階: 企業魅力度) ---
        current_per = (
            current_price / financial.forecast_eps
            if financial.forecast_eps is not None and financial.forecast_eps > 0
            else None
        )
        current_pbr = (
            current_price / financial.forecast_bps
            if financial.forecast_bps is not None and financial.forecast_bps > 0
            else None
        )
        historical_avg_dividend_yield_pct = estimate_historical_average_dividend_yield_pct(
            snapshot.dividend.previous_fiscal_year_dividend_per_share, snapshot.bars
        )
        drawdown_pct = compute_drawdown_from_52w_high_pct(current_price, snapshot.bars, now.date())
        recent_price_change_pct = compute_recent_price_change_pct(snapshot.bars, now.date(), 60)
        undervaluation_signals = compute_undervaluation_signals(
            current_price=current_price,
            current_per=current_per,
            historical_per_median=per_median,
            current_pbr=current_pbr,
            historical_pbr_median=pbr_median,
            current_dividend_yield_pct=snapshot.dividend_yield_pct,
            historical_average_dividend_yield_pct=historical_avg_dividend_yield_pct,
            drawdown_from_52w_high_pct=drawdown_pct,
            valuation_anchor=valuation_anchor,
            recent_price_change_pct=recent_price_change_pct,
            earnings_trend_non_decreasing=earnings_trend_non_decreasing,
            severe_earnings_decline=snapshot.severe_earnings_decline,
        )
        score_result = compute_score(
            # Issue #55 Phase B-1: 買い側のスコア意味論は変更しない(本Issueのスコープ外)。
            # snapshot.total_yield_pctがOptionalになったため、買い側では従来どおり
            # 「不明は0%として採点する」挙動を呼び出し側で明示的に維持する
            # (domain/scoring/score.pyのtotal_yield component_state=EVALUATEDの前提)。
            # この非対称の理由と、変更する場合の影響はscore.pyのコメントを参照。
            total_yield_pct=(
                snapshot.total_yield_pct if snapshot.total_yield_pct is not None else 0.0
            ),
            dividend=snapshot.dividend,
            financial=financial,
            undervaluation_signals=undervaluation_signals,
            benefit_yield_pct=snapshot.benefit_yield_pct,
            quarterly_operating_incomes=snapshot.quarterly_operating_incomes,
            price_bars=snapshot.bars,
            min_equity_ratio_pct=self._config.screening.financial_health.min_equity_ratio_pct,
            max_payout_ratio_pct=self._config.screening.financial_health.max_payout_ratio_pct,
            config=self._config.scoring,
            undervaluation_category_caps=self._config.buy_decision.undervaluation_category_caps,
        )
        company_quality_score = score_result.breakdown.total

        # --- Issue #22 Phase 3.5(2026-08-28): 割安シグナルの抑止注記(観測用) ---
        # compute_undervaluation_signals()はsevere_earnings_decline時に、
        # drawdown_from_52w_high/below_fair_valueは「一度bool評価した値を
        # Falseへ強制」(→観測上は形式1: value=False+SUPPRESSED_*)、
        # price_down_despite_stable_earningsは「評価式自体へ入らずNoneのまま」
        # (→観測上は形式2: value=None+SUPPRESSED_*)とする(buy_signal.py参照。
        # 2形式の正式な意味定義は_REASON_SUPPRESSED_BY_SEVERE_EARNINGS_DECLINE
        # のコメント参照)。保存されたFalse/Noneだけでは「評価の結果」と
        # 「ルールによる抑止」を事後に区別できないため、必要入力が実在した
        # (=抑止が実際に作用した)場合のみreason_codeとして記録する
        # (推測ではなく判定時点の事実からの確定)。
        suppressed_signal_reasons: dict[str, str] = {}
        if snapshot.severe_earnings_decline:
            if drawdown_pct is not None:
                suppressed_signal_reasons["drawdown_from_52w_high"] = (
                    _REASON_SUPPRESSED_BY_SEVERE_EARNINGS_DECLINE
                )
            if valuation_anchor is not None:
                suppressed_signal_reasons["below_fair_value"] = (
                    _REASON_SUPPRESSED_BY_SEVERE_EARNINGS_DECLINE
                )
            if recent_price_change_pct is not None and earnings_trend_non_decreasing is not None:
                suppressed_signal_reasons["price_down_despite_stable_earnings"] = (
                    _REASON_SUPPRESSED_BY_SEVERE_EARNINGS_DECLINE
                )
        undervaluation_category_details = build_undervaluation_category_details(
            undervaluation_signals, self._config.buy_decision.undervaluation_category_caps
        )
        cf_periods = snapshot.quarterly_operating_cashflow_periods
        cf_periods_ordered = sorted(cf_periods, key=lambda p: p.period_end)
        eps_periods = sorted(
            (hv for hv in snapshot.historical_valuations if hv.eps is not None),
            key=lambda hv: hv.date,
        )[-_FACTS_SERIES_MAX_PERIODS:]

        # Phase 2-B「銘柄分析」向け(2026-08): compute_score()内部では取得できない
        # (buy_signal_service.py側でのみ計算される)PER/PBR関連の判定時点入力事実を
        # score_result.input_facts(compute_score()自身が保持する分)へ合流させる。
        # 投資判断ロジックには一切使用しない、表示専用の記録。
        buy_score_input_facts: dict[str, object] = {
            **score_result.input_facts,
            # レビュー対応(2026-08): current_per/current_pbr自体は既に保存しているが、
            # それらの算出根拠(judgment時点のforecast_eps/forecast_bps)自体は
            # どこにも保存されていなかった。「表示に必要か」ではなく「判定時点の
            # 計算を事後に監査できるか」を基準に、current_per=current_price/
            # forecast_eps・current_pbr=current_price/forecast_bpsを後から検証
            # できるよう、両者の入力(forecast_eps/forecast_bps)を追加保存する
            # (current_priceはprice_at_recommendationとして既存保存済み)。
            # なお、UndervaluationSignals算出に使うvaluation_anchorは既存の
            # fair_value_at_recommendation、current_dividend_yield_pctは既存の
            # dividend_yield_pct_at_recommendationで既に保存済みのため重複保存
            # しない。earnings_trend_non_decreasing/severe_earnings_declineは
            # いずれもquarterly_operating_incomes(既にinput_facts内に保存済み)
            # のみから再計算可能な純粋関数の出力のため、同じ理由で重複保存しない。
            "forecast_eps": (
                str(financial.forecast_eps) if financial.forecast_eps is not None else None
            ),
            "forecast_bps": (
                str(financial.forecast_bps) if financial.forecast_bps is not None else None
            ),
            "current_per": str(current_per) if current_per is not None else None,
            "current_pbr": str(current_pbr) if current_pbr is not None else None,
            "historical_per_median": str(per_median) if per_median is not None else None,
            "historical_pbr_median": str(pbr_median) if pbr_median is not None else None,
            "historical_average_dividend_yield_pct": historical_avg_dividend_yield_pct,
            "drawdown_from_52w_high_pct": drawdown_pct,
            "recent_price_change_pct": recent_price_change_pct,
            "score_formulas": dict(score_result.formulas),
            # レビュー対応(2026-08、BUY_PRICE_RELIABILITY_LOW具体的理由表示):
            # determine_buy_price_reliability()が実際に発火させたconcerns自体は
            # 従来どこにも保存されておらず、AuditLog(audit_id未捕捉のため事後に
            # 参照不能)にしか残らなかった。concernsをそのまま判定時点事実として
            # 保存し、表示層(StockAnalysisViewService)がこれを唯一のauthoritative
            # な発火結果として扱えるようにする(表示層で発火有無を再判定しない)。
            # あわせて、determine_buy_price_reliability()の入力のうち他のどこにも
            # 判定時点値が残らないものも保存する(entry_margin_before_cap・
            # data_age_business_days・outlier_filter_blocking_reason・
            # methods_used_count・excluded_outlier_count)。methods_used_count/
            # excluded_outlier_countは、valuation_methodsから件数を数え直すと
            # 判定時の集計方法(methods_used/methods_excluded)と完全一致する保証が
            # ないため、判定に実際に使われた値をそのまま保存する(再集計しない)。
            # valuation_dispersion_ratio・earnings_date_status・
            # business_days_to_earningsは既存のRecommendation専用フィールドに
            # 既に保存済みのため重複保存しない。
            "buy_price_reliability_concerns": list(reliability_result.concerns),
            "entry_margin_before_cap": (
                str(margin_result.entry_margin_before_cap)
                if margin_result.entry_margin_before_cap is not None
                else None
            ),
            "data_age_business_days": data_age_days,
            "outlier_filter_blocking_reason": valuation_summary.outlier_filter_blocking_reason,
            "valuation_methods_used_count": valuation_summary.methods_used_count,
            "valuation_excluded_outlier_count": excluded_outlier_count,
            # レビュー対応(2026-08、commit f546473再レビュー): VALUATION_OUTLIER_
            # EXCLUDED concernの表示専用。Recommendation.valuation_methodsは
            # 外れ値フィルタ適用前のオブジェクトのため、実際に外れ値として除外
            # された方式・理由はここへ別途スナップショットする(上記コメント参照)。
            "valuation_outlier_exclusions": valuation_outlier_exclusions,
            # レビュー対応(2026-08、NO_VALUATION_ANCHOR表示不備の是正): BuyDecisionReason
            # (code="NO_VALUATION_ANCHOR")が発火した場合の直接原因を判定時点の実測値・
            # 基準値ごとスナップショットする(511行付近のコメント参照)。valuation_anchor
            # が算出できた場合はNone。
            "no_valuation_anchor_reason": no_valuation_anchor_reason,
            # --- Issue #22 Phase 3.5(2026-08-28): 観測用snapshot ---
            # 以下はすべて将来のスコア責務再設計(C4)のshadow検証用の観測データで
            # あり、v1の判定ロジック・スコア・BuyActionからは一切参照されない。
            # このキーが無い既存RecommendationはLEGACY_UNVERSIONEDとして扱う
            # (モジュール冒頭のFACTS_SCHEMA_VERSIONコメント参照)。
            "buy_score_input_facts_schema_version": FACTS_SCHEMA_VERSION,
            # Common Quality候補の本来値(判定時点にsnapshotへ算出済みだが従来
            # 未保存だったもの。暫定代替ではなく本来値をそのまま保存する)。
            # net_incomeを併存保存するのは、is_deficitがnet_income=Noneのとき
            # Falseへ潰れる(黒字と欠測を区別できない)ため。
            "net_income": (
                str(financial.net_income) if financial.net_income is not None else None
            ),
            "is_deficit": financial.is_deficit,
            "is_debt_excess": financial.is_debt_excess,
            "latest_operating_income": (
                str(financial.operating_income)
                if financial.operating_income is not None
                else None
            ),
            "latest_operating_cashflow": (
                str(financial.operating_cashflow)
                if financial.operating_cashflow is not None
                else None
            ),
            "trailing_eps": (
                str(financial.trailing_eps) if financial.trailing_eps is not None else None
            ),
            # 財務時系列(直近_FACTS_SERIES_MAX_PERIODS期のみ、上限の根拠は
            # モジュール冒頭コメント参照)。period_type付き構造化系列であり、
            # 既存キーquarterly_operating_incomes(期ラベル無しの素の値列)は
            # 互換のため変更せず並置する。
            "operating_income_periods": _serialize_period_series(
                snapshot.quarterly_operating_income_periods
            ),
            "operating_cashflow_periods": _serialize_period_series(cf_periods),
            # 営業CF連続黒字期数。ANNUAL_FALLBACK環境では「期」=「年」であり
            # 四半期ではないため、period_type/recent_periods_sourceを必ず併存
            # 保存する(「4期連続四半期黒字」と断定させないため)。
            "operating_cf_positive_streak": {
                "streak": _trailing_positive_streak(cf_periods),
                "periods_available": len(cf_periods),
                "latest_period_type": (
                    cf_periods_ordered[-1].period_type.value if cf_periods_ordered else None
                ),
                "recent_periods_source": financial.recent_periods_source.value,
            },
            # EPS系列。歴史的PER/PBR算出用のhistorical_valuationsから抽出した
            # ものであり「完全なEPS履歴」ではない(PER/PBR算出不能の期はEPS値が
            # 存在しても系列から落ち得る)。この制約をデータ自体に明示する。
            "historical_valuation_eps_periods": {
                "source": "historical_valuations",
                "coverage_limitation": "VALUATION_DATA_DEPENDENT",
                "periods": [
                    {"date": hv.date.isoformat(), "eps": str(hv.eps)} for hv in eps_periods
                ],
            },
            # 割安度4カテゴリの判定時点明細(得点/満点/state/シグナル別値/
            # 判定可否・抑止理由)。従来はカテゴリ合計値のみ保存されており、
            # quality/valuation責務分離のshadow検証が保存データだけでは
            # 不可能だったことへの直接の是正。
            "undervaluation_categories": _serialize_undervaluation_categories(
                undervaluation_category_details, suppressed_signal_reasons
            ),
            # 7componentの判定時点評価状況(VALID相当=EVALUATED / DATA_MISSING
            # 相当=NOT_EVALUATED / NOT_APPLICABLE の3値。v1では推測を伴う
            # NOT_APPLICABLEを生成しない。score.py参照)。
            "component_states": score_result.component_states,
            # --- Issue #54 Phase B-1(2026-08-29): 業種分類のcanonical観測 ---
            # JPX 33業種(canonical)と、既存4分類器が同一銘柄に対して実際に
            # 出した分類を判定時点の事実として並べて記録する。**判定・スコア・
            # BuyActionからは一切参照されない観測専用**であり、死んでいる判定
            # (CYCLICAL/DEFENSIVE・REIT除外)の復活はPhase B-2で、この観測結果を
            # 確認したうえで実施する(適正価格と対象母集団が変わるため)。
            "canonical_industry_observation": self._observe_canonical_industry(
                stock_code, snapshot, buy_industry_sector, is_growth_stock
            ),
        }

        # --- 13. purchase_attractiveness_score算出 ---
        purchase_attractiveness_score = compute_purchase_attractiveness_score(
            current_price=current_price,
            buy_price_levels=buy_price_levels,
            valuation_confidence=valuation_confidence,
            dispersion_band=dispersion_band,
            business_days_to_earnings=business_days_to_earnings,
            recent_price_change_pct=recent_price_change_pct,
            industry_model_applied=industry_model_applied,
            data_quality_warning=data_quality_warning,
            config=self._config.buy_decision,
        )

        # --- 14〜17. BuyAction決定(価格条件→スコア格下げ→決算調整→分散度格下げ) ---
        decision = decide_buy_action(
            current_price=current_price,
            buy_price_levels=buy_price_levels,
            company_quality_score=company_quality_score,
            business_days_to_earnings=business_days_to_earnings,
            valuation_dispersion_ratio=valuation_summary.valuation_dispersion_ratio,
            buy_price_reliability=buy_price_reliability,
            config=self._config.buy_decision,
        )
        buy_action = decision.action
        raw_buy_action = decision.raw_action
        buy_decision_reasons = list(decision.reasons)

        # --- 18. 整合性検証(二重の安全策) ---
        violations = validate_buy_recommendation(
            action=buy_action,
            current_price=current_price,
            entry_price=buy_price_levels.entry.price if buy_price_levels.entry else None,
            standard_price=buy_price_levels.standard.price if buy_price_levels.standard else None,
            strong_price=buy_price_levels.strong.price if buy_price_levels.strong else None,
            confidence=valuation_confidence,
            business_days_to_earnings=business_days_to_earnings,
            valuation_dispersion_ratio=valuation_summary.valuation_dispersion_ratio,
            config=self._config.buy_decision,
        )
        if violations:
            buy_action = BuyAction.MANUAL_REVIEW
            buy_decision_reasons.append(
                BuyDecisionReason(
                    code="CONSISTENCY_VIOLATION",
                    message="; ".join(v.message for v in violations),
                )
            )

        # --- 19〜20. ランキング区分の確定 ---
        if buy_action in BUY_FAMILY_ACTIONS:
            ranking_group = "buy_candidate"
        elif buy_action in {BuyAction.WATCH_FOR_PRICE, BuyAction.WATCH_BEFORE_EARNINGS}:
            ranking_group = "watch_price"
        else:
            ranking_group = "excluded"

        # recommendation_typeは常に呼び出し元の文脈(BUY/WATCH_BUY)のまま保つ。
        # RecommendationType.WATCH_BEFORE_EARNINGSは利確判定エンジンのWATCH抑制専用
        # として既に使われており、ここで転用すると通知テンプレート・再通知抑止の
        # 判定キー(notification_type)が衝突する。BUYパイプライン側の決算待ち表示は
        # buy_action(BuyAction.WATCH_BEFORE_EARNINGS)のみで判別する
        # (line_notification_service.py側もbuy_actionを優先して分岐する)。

        positive_reasons = [
            f"{area}が高評価"
            for area in score_areas(
                score_result, self._config.scoring, _STRONG_SCORE_RATIO, above=True
            )
        ]
        counter_factors = list(screening_result.warnings)
        if snapshot.benefit is not None and snapshot.benefit.is_major_downgrade:
            counter_factors.append("株主優待の内容が改悪された可能性がある")
        counter_factors.extend(
            f"{area}が弱い"
            for area in score_areas(
                score_result, self._config.scoring, _WEAK_SCORE_RATIO, above=False
            )
        )

        dividend = snapshot.dividend
        benefit = snapshot.benefit
        current_vs_valuation_pct = (
            (current_price / valuation_anchor - 1) * 100 if valuation_anchor else None
        )
        current_vs_entry_price_pct = (
            (current_price / buy_price_levels.entry.price - 1) * 100
            if buy_price_levels.entry is not None
            else None
        )
        # 「打診買い価格まで、あと何%下落が必要か」(current_vs_entry_price_pctとは
        # 意味が異なる別指標。current_vs_entry_price_pctは「現在値がentryを何%
        # 上回っているか」であり、通知の「まで」という接近方向の文言には使えない)。
        required_decline_to_entry_pct = (
            (1 - buy_price_levels.entry.price / current_price) * 100
            if buy_price_levels.entry is not None and current_price > 0
            else None
        )

        # --- NEAR BUY判定(BUY候補裾野拡大機能2026-08、要求仕様§5)。
        # decide_buy_action()自体は変更しない。売買クールダウン中は
        # WatchStateServiceを一切呼ばない(新規作成・consecutive_business_days
        # 増加・best_distance_pct更新のいずれも停止する。§5-2)。クールダウンの
        # 発生自体(TradeCooldownService.detect_and_apply)はハンドラ側の入口で
        # 既に完了している前提で、ここではHoldingsSnapshotEntryを読むだけ ---
        # 再コードレビュー対応(2026-08、JST暦日境界修正・指摘4): cooldown_until_date
        # との比較、およびWatchStateServiceの「当日」(営業日ベースの経過判定
        # business_days_between等に使う基準日)は、line_notification_service.
        # check_trade_cooldown_eligibility()・TradeCooldownService.detect_and_apply()
        # と同じevaluation_date_jst(now)を使う(生成側・比較側の基準日を統一する)。
        watch_type: WatchType | None = None
        near_buy_consecutive_business_days: int | None = None
        watch_transition_type: str | None = None
        watch_previous_consecutive_business_days: int | None = None
        watch_end_reason: str | None = None
        evaluation_date = evaluation_date_jst(now)
        # M3: BUY候補側は特定ownerを知らないため、TradeCooldownService.is_in_cooldown()
        # と同じくstock_codeに対するowner横断検索で判定する(1人でもクールダウン中
        # ならBUY候補としての通知は抑止する安全側の設計)。
        in_trade_cooldown = any(
            entry.cooldown_until_date is not None and evaluation_date <= entry.cooldown_until_date
            for entry in self._holdings_snapshot_repo.list_by_stock(stock_code)
        )
        if not in_trade_cooldown:
            transition = self._watch_state_service.evaluate_and_update(
                stock_code=stock_code,
                buy_action=buy_action,
                company_quality_score=company_quality_score,
                required_decline_to_entry_pct=required_decline_to_entry_pct,
                current_price=current_price,
                entry_price=buy_price_levels.entry.price if buy_price_levels.entry else None,
                today=evaluation_date,
                config=self._config.buy_decision.near_buy,
            )
            # 現在アクティブに監視中(=NEAR BUY通知の対象)なのはSTARTED/
            # CONTINUED/RESUMEDのみ。PROMOTED_TO_BUY/ENDEDでは監視は終了済み
            # だが、その旨(watch_transition_type等)はRecommendationへ残し、
            # 通知層が「4日監視後にBUY到達」「6日継続して終了」を表示できる
            # ようにする(コードレビュー対応2026-08)。
            if transition.transition_type in (
                WatchTransitionType.STARTED,
                WatchTransitionType.CONTINUED,
                WatchTransitionType.RESUMED,
            ):
                watch_type = transition.watch_type
                near_buy_consecutive_business_days = transition.consecutive_business_days
            if transition.transition_type is not WatchTransitionType.NONE:
                watch_transition_type = transition.transition_type.value
                watch_previous_consecutive_business_days = (
                    transition.previous_consecutive_business_days
                )
                watch_end_reason = transition.end_reason

        # --- 22. 監査ログ保存(買い候補にならなかった銘柄も含め全件記録) ---
        self._audit.record(
            decision_type="buy_signal",
            stock_code=stock_code,
            input_values={
                "current_price": str(current_price),
                "forecast_eps": str(financial.forecast_eps) if financial.forecast_eps else None,
                "normalized_eps": str(eps_result.normalized_eps)
                if eps_result.normalized_eps is not None
                else None,
                "equity_ratio_pct": financial.equity_ratio_pct,
                "total_yield_pct": snapshot.total_yield_pct,
                "data_age_business_days": data_age_days,
            },
            calculation_formulas={
                "eps_normalization_method": eps_result.method,
                **score_result.formulas,
            },
            output_values={
                "raw_company_quality_score": company_quality_score,
                "raw_purchase_attractiveness_score": purchase_attractiveness_score,
                "raw_buy_action": raw_buy_action.value,
                "final_buy_action": buy_action.value,
                "action_adjustment_reasons": [r.message for r in buy_decision_reasons],
                "valuation_anchor": str(valuation_anchor) if valuation_anchor is not None else None,
                "valuation_min": str(valuation_summary.valuation_min)
                if valuation_summary.valuation_min is not None
                else None,
                "valuation_max": str(valuation_summary.valuation_max)
                if valuation_summary.valuation_max is not None
                else None,
                "valuation_dispersion_ratio": valuation_summary.valuation_dispersion_ratio,
                "decision_valuation_min": str(valuation_summary.decision_valuation_min)
                if valuation_summary.decision_valuation_min is not None
                else None,
                "decision_valuation_max": str(valuation_summary.decision_valuation_max)
                if valuation_summary.decision_valuation_max is not None
                else None,
                "excluded_outlier_methods": [
                    m.method
                    for m in valuation_summary.methods_excluded
                    if m.exclusion_detail is not None
                ],
                "buy_price_reliability": buy_price_reliability.value,
                "buy_price_reliability_concerns": reliability_result.concerns,
                "earnings_date_raw": earnings_date_raw.isoformat()
                if earnings_date_raw is not None
                else None,
                "earnings_date_resolved": resolved_next_earnings_date.isoformat()
                if resolved_next_earnings_date is not None
                else None,
                "earnings_date_status": earnings_date_status.value,
                "earnings_date_source": "yfinance_calendar",
                "earnings_date_retrieved_at": snapshot.data_fetched_at.isoformat(),
                "entry_buy_price": str(buy_price_levels.entry.price)
                if buy_price_levels.entry
                else None,
                "standard_buy_price": str(buy_price_levels.standard.price)
                if buy_price_levels.standard
                else None,
                "strong_buy_price": str(buy_price_levels.strong.price)
                if buy_price_levels.strong
                else None,
                "required_margin_of_safety": {
                    "entry": str(margin_result.entry_margin)
                    if margin_result.entry_margin
                    else None,
                    "standard": str(margin_result.standard_margin)
                    if margin_result.standard_margin
                    else None,
                    "strong": str(margin_result.strong_margin)
                    if margin_result.strong_margin
                    else None,
                },
                "margin_adjustments": [
                    {"code": a.code, "adjustment": str(a.adjustment), "reason": a.reason}
                    for a in margin_result.adjustments
                ],
                "business_days_to_earnings": business_days_to_earnings,
                "industry_model_applied": industry_model_applied,
                "industry_model_name": buy_industry_sector.value,
                "valuation_confidence": valuation_confidence.value,
                "ranking_group": ranking_group,
                "notification_suppression_reason": (
                    None
                    if ranking_group == "buy_candidate"
                    else (
                        "CURRENT_PRICE_ABOVE_ENTRY_PRICE"
                        if buy_action == BuyAction.WATCH_FOR_PRICE
                        else buy_action.value
                    )
                ),
            },
            data_sources=list(snapshot.data_sources),
            rule_version=self._active_rule_version(),
            timestamp=now,
        )

        recommendation = Recommendation(
            recommendation_id=str(uuid.uuid4()),
            stock_code=stock_code,
            stock_name=financial.stock_name or stock_code,
            recommended_at=now,
            recommendation_type=recommendation_type,
            raw_recommendation_type=recommendation_type,
            buy_prices=buy_price_levels,
            price_at_recommendation=current_price,
            dividend_yield_pct_at_recommendation=snapshot.dividend_yield_pct,
            shareholder_benefit_yield_pct_at_recommendation=snapshot.benefit_yield_pct,
            total_yield_pct_at_recommendation=snapshot.total_yield_pct,
            fair_value_at_recommendation=valuation_anchor,
            total_score=company_quality_score,
            score_breakdown=score_result.breakdown,
            buy_score_input_facts=buy_score_input_facts,
            reasons=positive_reasons,
            counter_factors=counter_factors,
            key_risks=counter_factors,
            confidence=valuation_confidence,
            next_earnings_date=resolved_next_earnings_date,
            dividend_record_date=dividend.dividend_record_dates[0]
            if dividend.dividend_record_dates
            else None,
            benefit_record_date=benefit.benefit_record_dates[0]
            if benefit is not None and benefit.benefit_record_dates
            else None,
            dividend_comparison_source_fiscal_year=dividend.comparison_source_fiscal_year,
            dividend_comparison_target_fiscal_year=dividend.comparison_target_fiscal_year,
            dividend_comparison_outcome=dividend.dividend_comparison_outcome,
            dividend_record_date_unknown_reason=dividend.dividend_record_date_unknown_reason,
            benefit_record_date_unknown_reason=(
                benefit.benefit_record_date_unknown_reason if benefit is not None else None
            ),
            rule_version=self._active_rule_version(),
            config_values_used={
                "min_total_yield_pct": self._config.screening.total_yield.min_total_yield_pct,
                "aggregation_method": self._config.valuation.fair_value_methods.aggregation_method,
                # score_breakdown算出に実際に使ったScoreWeights(2026-08-25
                # コードレビュー対応)。過去の判定を後から説明する際、config
                # 変更後の"現在の"weightsで誤って再解釈しないため、判定時点の
                # weightsをここに記録する(watchlist_judgment_summary_formatter
                # 参照)。
                "scoring_weights": self._config.scoring.weights.model_dump(),
                # Phase 2-B「銘柄分析」向け(2026-08): raw_buy_action→buy_actionの
                # 格下げ(SCORE_BELOW_THRESHOLD)がどの閾値を跨いだかを事後に一意
                # 特定できるよう、判定時点の4閾値をスナップショットする
                # (BuyDecisionReason.threshold_valueは常にwatch閾値のみを記録する
                # 精度限界があるため、company_quality_scoreとこのスナップショットの
                # 突き合わせで判定する。domain/signals/buy_decision.py参照)。
                "score_thresholds": self._config.buy_decision.score_thresholds.model_dump(),
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
                "market_environment": market_environment_config_values(
                    self._config.market_sector_environment.market
                ),
                "sector_environment": sector_environment_config_values(
                    self._config.market_sector_environment.sector
                ),
                "environment": environment_config_values(
                    self._config.market_sector_environment.environment
                ),
                # レビュー対応(2026-08、BUY_PRICE_RELIABILITY_LOW具体的理由表示):
                # determine_buy_price_reliability()が参照するconfig由来の閾値の
                # 判定時点スナップショット(score_thresholds等と同じ理由: 表示側が
                # "現在の"configで誤って再解釈しないようにする)。_MIN_CONCERNS_
                # FOR_LOW(2件、buy_price_reliability.py内のハードコード定数で
                # config化されていない)以外に、この関数がconfigから受け取る値は
                # この2つのみ。
                "maximum_entry_margin": (
                    self._config.buy_decision.margin_of_safety.maximum_margin.entry
                ),
                "valuation_dispersion_medium_max": (
                    self._config.buy_decision.valuation_dispersion.medium_max
                ),
                # Issue #22 Phase 3.5(2026-08-28): 割安度カテゴリ上限点と
                # UndervaluationSignals算出のモジュール定数閾値の判定時点
                # スナップショット(buy_score_input_facts.undervaluation_categories
                # を事後に"現在の"config・定数で誤って再解釈しないため。
                # score_thresholds等と同じ理由)。
                "undervaluation_category_caps": {
                    "valuation_multiple": (
                        self._config.buy_decision.undervaluation_category_caps.valuation_multiple
                    ),
                    "yield": self._config.buy_decision.undervaluation_category_caps.yield_,
                    "fair_value": (
                        self._config.buy_decision.undervaluation_category_caps.fair_value
                    ),
                    "market_price_action": (
                        self._config.buy_decision.undervaluation_category_caps.market_price_action
                    ),
                },
                "undervaluation_signal_thresholds": undervaluation_signal_threshold_values(),
            },
            data_sources=list(snapshot.data_sources),
            industry_model_applied=industry_model_applied,
            industry_model_missing_reason=buy_industry_model_missing_reason(buy_industry_sector),
            buy_action=buy_action,
            raw_buy_action=raw_buy_action,
            company_quality_score=company_quality_score,
            purchase_attractiveness_score=purchase_attractiveness_score,
            valuation_anchor=valuation_anchor,
            valuation_min=valuation_summary.valuation_min,
            valuation_max=valuation_summary.valuation_max,
            valuation_dispersion_ratio=Decimal(str(valuation_summary.valuation_dispersion_ratio))
            if valuation_summary.valuation_dispersion_ratio is not None
            else None,
            entry_buy_price=buy_price_levels.entry.price if buy_price_levels.entry else None,
            standard_buy_price=buy_price_levels.standard.price
            if buy_price_levels.standard
            else None,
            strong_buy_price=buy_price_levels.strong.price if buy_price_levels.strong else None,
            current_vs_valuation_pct=current_vs_valuation_pct,
            current_vs_entry_price_pct=current_vs_entry_price_pct,
            required_decline_to_entry_pct=required_decline_to_entry_pct,
            stock_types=list(snapshot.stock_type_classification.types),
            watch_type=watch_type,
            near_buy_consecutive_business_days=near_buy_consecutive_business_days,
            watch_transition_type=watch_transition_type,
            watch_previous_consecutive_business_days=watch_previous_consecutive_business_days,
            watch_end_reason=watch_end_reason,
            buy_price_reliability=buy_price_reliability,
            decision_valuation_min=valuation_summary.decision_valuation_min,
            decision_valuation_max=valuation_summary.decision_valuation_max,
            earnings_date_status=earnings_date_status,
            earnings_date_raw=earnings_date_raw,
            required_margin_of_safety_entry=margin_result.entry_margin,
            required_margin_of_safety_standard=margin_result.standard_margin,
            required_margin_of_safety_strong=margin_result.strong_margin,
            margin_adjustments=tuple(margin_result.adjustments),
            business_days_to_earnings=business_days_to_earnings,
            buy_industry_sector=buy_industry_sector,
            forecast_eps=financial.forecast_eps,
            normalized_eps=eps_result.normalized_eps,
            eps_normalization_method=eps_result.method,
            valuation_methods=tuple(method_results),
            # Issue #20 Phase B2-A: 判定入力financial dataのprovenance
            # (snapshot構築時点の事実の転記のみ。観測専用・判定不変)。
            financial_input_provenance=snapshot.financial_input_provenance,
            buy_decision_reasons=tuple(buy_decision_reasons),
            dividend_record_date_recurring_label=resolve_dividend_record_date_recurring_label(
                dividend, financial.fiscal_year_end_month
            ),
            benefit_record_date_recurring_label=resolve_benefit_record_date_recurring_label(
                benefit, financial.fiscal_year_end_month
            ),
            dividend_record_date_source_type=resolve_dividend_record_date_source_type(dividend),
            benefit_record_date_source_type=resolve_benefit_record_date_source_type(benefit),
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
            # 判定精度向上機能次フェーズSTEP2: DecisionSnapshot記録専用(Shadow
            # 計測)。BUYパイプラインはExit Price Rangeを計算しないため
            # exit_price_range_*は全てNoneのまま(デフォルト)。
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

        return BuyAnalysisOutcome(
            stock_code,
            recommendation,
            True,
            [],
            None,
            buy_action=buy_action,
            ranking_group=ranking_group,
        )
