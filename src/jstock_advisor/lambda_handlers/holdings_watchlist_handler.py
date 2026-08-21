"""保有銘柄分析Lambda(schedule.yaml daily_holdings_watchlist_analysis、平日08:00)。

【実行時刻の変更について(2026-07-31改訂)】
以前は平日16:30(当日終値)に実行していたが、買い候補分析(buy_candidates_handler.py、
平日08:00)と処理条件・通知タイミングを揃えるため08:00へ変更した。両ジョブとも
実行時点で取得できる最新の終値(前営業日終値)を基準に評価する。

CLIの`jstock analyze holdings --source real --notify`と同じロジックを
EventBridge Scheduler経由で自動実行する薄いアダプタ。

【統合BUY候補パイプライン(2026-07)への移行について】
以前はウォッチリスト銘柄の買いシグナル評価(task="watchlist")もこのハンドラが
別経路(recommendation_type=WATCH_BUY、ランキングなしの個別即時通知)として
実施していたが、`buy_candidates_handler.py`の統合BUY候補パイプラインへ
吸収・一本化したため、この経路(および対応するdispatch)は廃止した。
本ハンドラが担うのは保有銘柄の**売却・利確判定**(task="holding")のみで、
ウォッチリストのCRUD(services/watchlist_service.py)・保有銘柄の**買い増し判定**
(buy_candidates_handler.py側)には一切影響しない。`RecommendationType.WATCH_BUY`/
`NotificationType.WATCHLIST_BUY_SIGNAL`のenum値自体は過去データとの後方互換の
ため残しているが、新規発行はもう行われない。

銘柄単位のファンアウト(_fanout.py)を採用しており、通常のスケジュール起動では
保有銘柄一覧を取得して銘柄ごとに自分自身を非同期再帰呼び出しするだけで即座に戻る。
実際のデータ取得・判定・通知は、"task"付きで再帰呼び出しされた各インスタンスが
1銘柄のみを担当して行う。

個別のデータ取得エラー・データ品質アラートはLINEへ配信せず、全銘柄の処理が
完了した時点で全体件数・区分別内訳のサマリーを1通だけ送信する
(batch_tracker.pyのDynamoDB原子カウンタで完了を検知する。要求仕様§12・§13)。

保有銘柄の集中リスク判定(要求仕様§14)のため、ディスパッチ元でポートフォリオ
全体の時価総額・取得価格総額を軽量な価格取得(get_latest_price、フルスナップショットは
取得しない)で概算し、各銘柄ワーカーへ渡す。
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.classification.financial_industry import classify_industry
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    DecisionType,
    EvaluationStatus,
    HoldingSummaryAction,
    NotificationCategory,
    NotificationIntent,
    NotificationStatus,
    RecommendationType,
    resolve_holding_summary_action,
)
from jstock_advisor.domain.entities.evaluation_audit import HoldingEvaluationAudit, summary_category
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holding_decision import HoldingDecisionResult
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.exit_price_range import evaluate_exit_price_range
from jstock_advisor.domain.signals.holding_decision_execution_plan import (
    resolve_execution_plan,
    resolve_financial_deferred_policy,
)
from jstock_advisor.domain.signals.portfolio_concentration import evaluate_portfolio_concentration
from jstock_advisor.infrastructure.aws.batch_tracker import record_result, start_batch
from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.holding_decision_result_repository import (
    HoldingDecisionResultRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers._execution_mode import resolve_execution_context
from jstock_advisor.lambda_handlers._fanout import dispatch_async, resolve_function_name
from jstock_advisor.services.buy_signal_service import RULE_VERSION_PLACEHOLDER
from jstock_advisor.services.decision_snapshot_service import save_decision_snapshot_safely
from jstock_advisor.services.holding_decision_notification_builder import (
    build_holding_decision_recommendation,
)
from jstock_advisor.services.holding_decision_runtime_config_service import (
    HoldingDecisionRuntimeConfigService,
)
from jstock_advisor.services.holding_decision_service import HoldingDecisionService
from jstock_advisor.services.line_notification_service import (
    LineNotificationService,
    NotificationOutcome,
    resolve_attention_origin_for_recommendation,
    resolve_notification_category,
    resolve_notification_intent_for_recommendation,
)
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.provider_factory import build_real_provider_bundle
from jstock_advisor.services.rule_version_service import RuleVersionService
from jstock_advisor.services.sell_signal_service import SellSignalService
from jstock_advisor.services.shareholder_benefit_registry_service import check_registry_health
from jstock_advisor.services.stock_snapshot_service import StockSnapshot, build_stock_snapshot
from jstock_advisor.services.trade_cooldown_service import TradeCooldownService
from jstock_advisor.services.watch_state_service import WatchStateService

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_PROCESS_NAME = "保有銘柄分析"
# handler()は常に明示的にexecution_contextを渡す(NORMAL/VALIDATIONを問わず)。
# このデフォルトは内部関数を直接呼ぶ既存テストコード(白箱テスト)向けの
# 後方互換専用で、本番の呼び出し経路では使われない。
_DEFAULT_EXECUTION_CONTEXT = ExecutionContext.normal()


@dataclass(frozen=True)
class _HoldingResult:
    recommended: bool
    notified: bool
    succeeded: bool
    category: str
    audit: HoldingEvaluationAudit
    # コードレビュー対応(2026-08、LINE通知アクション限定化): バッチサマリーの
    # 送信済みアクション3分類(一部売却/全部売却/売却)集計向け。実際に通知が
    # 送信された場合のみ値を持つ。WATCH/MANUAL_REVIEWはNON_ACTIONABLEとして
    # ここへ到達する前にゲートされるため、送信済みならこの2つにはならない。
    notification_category: NotificationCategory | None = None
    # NotificationCategory.SELLはFULL_PROFIT_TAKEとSELL/SELL_CONSIDERATION等を
    # 区別しない表示用の分類のため、「一部売却/全部売却/売却」を分離集計する
    # にはrecommendation_type自体が必要(§13)。
    # 再コードレビュー対応(2026-08、detected/sent一元化): この値は「実際に
    # LINE個別通知が送信された」場合のみ設定する(sent集計・CloudWatch Logs用の
    # 内部監査値。ユーザー向けサマリーの表示にはdetected_recommendation_typeを
    # 使う、下記)。
    recommendation_type_at_send: RecommendationType | None = None
    # 再コードレビュー対応(2026-08、detected/sent一元化・追加修正1): 「有効な
    # アクション検出」の唯一の判定基準。DataQuality安全ゲートでブロックされな
    # かった(outcome.data_quality_blocked=False)場合のみ、送信結果(outcome.sent)
    # に関わらずrecommendation.recommendation_typeを設定する。TradeCooldown・
    # CrossPipelinePriority・resend/event dedup・kill switchによる個別通知抑止は
    # この値を減らさない。DataQuality BLOCKED時はNoneのままとし、既存の
    # summary_category()による「要確認」区分側の集計に委ねる(二重計上しない)。
    detected_recommendation_type: RecommendationType | None = None
    # 通知意図3段階化(2026-08)。Profit Protection ATTENTIONとして検出された
    # 銘柄か(上記detected_recommendation_typeと同じDataQuality境界を適用する)。
    attention_detected: bool = False
    # 再コードレビュー対応(2026-08、追加修正2): ATTENTIONが実際にLINE個別送信
    # できた場合のみTrue(attention_detectedとは別に明示的に保持し、将来別の
    # WATCH系通知が追加されてもRecommendationType.WATCHからの推測に依存しない)。
    attention_sent: bool = False


_KILL_SWITCH_SUPPRESSED_OUTCOME = NotificationOutcome(
    status=NotificationStatus.KILL_SWITCH_SUPPRESSED, sent=False, data_quality_blocked=False
)


def _send_or_suppress_notification(
    recommendation: Recommendation,
    notification_enabled: bool,
    notification_service: LineNotificationService,
    now: dt.datetime,
) -> NotificationOutcome:
    """kill switch(コードレビュー対応)。Recommendationの生成・保存はkill switchの
    影響を受けず常に継続する(呼び出し側で保証する)。この関数はLINE送信の可否のみを
    制御する。旧売却・新保有判断・利確・ポートフォリオ集中リスクの4経路すべてが
    この1関数を経由することで、抑止結果(KILL_SWITCH_SUPPRESSED)を統一する。
    """
    if not notification_enabled:
        logger.info(
            "kill_switch_suppressed: stock_code=%s recommendation_id=%s "
            "recommendation_type=%s notification_enabled=False",
            recommendation.stock_code,
            recommendation.recommendation_id,
            recommendation.recommendation_type.value,
        )
        return _KILL_SWITCH_SUPPRESSED_OUTCOME
    return notification_service.notify_recommendation_with_status(recommendation, now)


def _resolve_suppression_reason(outcome: NotificationOutcome) -> str | None:
    """監査用のnotification_suppression_reasonを、可能な限り具体的な理由で
    解決する(コードレビュー対応2026-08、指摘2)。

    優先順位: 1. block_reason(TRADE_COOLDOWN/TRADE_DETECTION_IN_PROGRESS/
    LOW_PRIORITY/DUPLICATE_STOCK_NOTIFICATION等、check_*_eligibility()由来の
    具体的な理由) 2. block_category.value(block_reasonが無い場合の分類名)
    3. status.value(具体的理由が無い通常のNOT_REQUIRED等)。送信済みの場合は
    Noneを返す(既存動作どおり)。BUY候補側の監査(_record_notification_
    outcome_audit)がblock_reasonを最優先で使うのと意味を揃えている。
    """
    if outcome.sent:
        return None
    if outcome.block_reason:
        return outcome.block_reason
    if outcome.block_category is not None:
        return outcome.block_category.value
    return outcome.status.value


def _evaluate_portfolio_concentration_and_notify(
    holding: Holding,
    current_price: Decimal,
    portfolio_total_market_value: Decimal | None,
    portfolio_total_acquisition_cost: Decimal | None,
    config: AppConfig,
    recommendation_repo: RecommendationRepository,
    notification_service: LineNotificationService,
    rule_version_service: RuleVersionService,
    now: dt.datetime,
    notification_enabled: bool,
    execution_context: ExecutionContext = _DEFAULT_EXECUTION_CONTEXT,
) -> None:
    """企業価値判断とは独立に、ポートフォリオ内保有比率が高い場合に別途通知する
    (要求仕様§14)。銘柄単体の判定結果には影響しない(常に別のRecommendationとして扱う)。

    kill switch(notification_enabled=False)中でもRecommendationの生成・保存は継続し、
    LINE送信のみを止める(コードレビュー対応)。
    """
    holding_market_value = current_price * holding.shares
    portfolio_weight_pct = (
        float(holding_market_value / portfolio_total_market_value * 100)
        if portfolio_total_market_value and portfolio_total_market_value > 0
        else None
    )
    acquisition_cost_weight_pct = (
        float(holding.total_purchase_amount / portfolio_total_acquisition_cost * 100)
        if portfolio_total_acquisition_cost and portfolio_total_acquisition_cost > 0
        else None
    )
    result = evaluate_portfolio_concentration(
        portfolio_weight_pct,
        acquisition_cost_weight_pct,
        config.portfolio_concentration.single_stock_weight_threshold_pct,
    )
    if not result.is_concentrated:
        return

    recommendation = Recommendation(
        recommendation_id=str(uuid.uuid4()),
        stock_code=holding.stock_code,
        stock_name=holding.stock_name,
        recommended_at=now,
        recommendation_type=RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW,
        price_at_recommendation=current_price,
        shares_at_recommendation=holding.shares,
        average_purchase_price_at_recommendation=holding.average_purchase_price,
        reasons=result.reasons,
        # 保有比率自体は直接計算できる事実値であり、モデル推定を伴わないためHIGHとする。
        confidence=ConfidenceLevel.HIGH,
        rule_version=rule_version_service.get_active_version_or(RULE_VERSION_PLACEHOLDER),
        portfolio_weight_pct=result.portfolio_weight_pct,
        portfolio_acquisition_cost_weight_pct=result.acquisition_cost_weight_pct,
    )
    # 通知検証モード機能(2026-08追加): VALIDATIONでは通常運用の判定履歴を
    # 汚さないため保存自体をスキップする(kill switchとは独立した別の抑止軸)。
    if not execution_context.is_validation:
        recommendation_repo.save(recommendation)
    _send_or_suppress_notification(recommendation, notification_enabled, notification_service, now)


def _notify_legacy_sell_and_build_result(
    holding: Holding,
    now: dt.datetime,
    recommendation: Recommendation,
    recommendation_repo: RecommendationRepository,
    notification_service: LineNotificationService,
    notification_enabled: bool,
    execution_context: ExecutionContext = _DEFAULT_EXECUTION_CONTEXT,
) -> _HoldingResult:
    """Recommendation保存はkill switchの影響を受けず常に行う(コードレビュー対応)。
    LINE送信のみ`notification_enabled`で制御する。通知検証モード機能(2026-08追加)
    ではkill switchとは独立に、Recommendation/DecisionSnapshot保存自体をスキップする。
    """
    if not execution_context.is_validation:
        recommendation_repo.save(recommendation)
        # 判定精度向上機能Phase A: DecisionSnapshotを記録する(スコア項目はPhase Bまで
        # 全てNone)。失敗しても既存の通知・戻り値には一切影響しない。
        save_decision_snapshot_safely(
            DecisionSnapshotRepository(), recommendation, DecisionType.SELL, logger
        )
    outcome = _send_or_suppress_notification(
        recommendation, notification_enabled, notification_service, now
    )
    audit = HoldingEvaluationAudit(
        stock_code=holding.stock_code,
        evaluated_at=now,
        evaluation_status=(
            EvaluationStatus.DATA_QUALITY_BLOCKED
            if outcome.data_quality_blocked
            else EvaluationStatus.COMPLETED
        ),
        raw_sell_recommendation_type=recommendation.raw_recommendation_type,
        raw_profit_recommendation_type=None,
        final_recommendation_type=recommendation.recommendation_type,
        notification_status=outcome.status,
        notification_suppression_reason=_resolve_suppression_reason(outcome),
        sell_signal_status="TRIGGERED",
        profit_taking_status="NOT_EVALUATED",
        fair_value_status="NOT_AVAILABLE",
        data_quality_status="BLOCKED" if outcome.data_quality_blocked else "OK",
        confidence=recommendation.confidence,
        error_code=None,
    )
    return _HoldingResult(
        recommended=True,
        notified=outcome.sent,
        succeeded=True,
        category=summary_category(audit),
        audit=audit,
        notification_category=(
            resolve_notification_category(recommendation) if outcome.sent else None
        ),
        recommendation_type_at_send=(
            recommendation.recommendation_type
            if outcome.sent and not outcome.data_quality_blocked
            else None
        ),
        detected_recommendation_type=(
            recommendation.recommendation_type if not outcome.data_quality_blocked else None
        ),
    )


def _notify_holding_decision_and_build_result(
    holding: Holding,
    now: dt.datetime,
    result: HoldingDecisionResult,
    snapshot: StockSnapshot,
    config: AppConfig,
    holding_decision_result_repo: HoldingDecisionResultRepository,
    recommendation_repo: RecommendationRepository,
    notification_service: LineNotificationService,
    notification_enabled: bool,
    execution_context: ExecutionContext = _DEFAULT_EXECUTION_CONTEXT,
) -> tuple[_HoldingResult, HoldingDecisionResult]:
    """保有判断スコアの通知を行う。

    戻り値は(_HoldingResult, 保存用に更新したHoldingDecisionResult)。
    Recommendation生成・保存・recommendation_id設定はkill switchの影響を受けず常に行う
    (コードレビュー対応)。LINE送信のみ`notification_enabled`で制御する。
    """
    recommendation_id = str(uuid.uuid4())
    # 判定精度向上機能次フェーズSTEP2: Exit Price Range(Shadow計測)。
    # HoldingDecisionパイプラインではここ(holdingとsnapshotが揃う唯一の
    # 箇所)で1回だけ計算し、Builderへ渡す(Builder自身は算出しない)。
    exit_price_range = evaluate_exit_price_range(
        snapshot.fair_value_range,
        snapshot.historical_valuation,
        snapshot.timing,
        holding.average_purchase_price,
        snapshot.current_price,
        now,
        config.entry_exit_price.exit,
    )
    recommendation = build_holding_decision_recommendation(
        holding,
        result,
        snapshot,
        str(config.holding_decision.scoring_model_version),
        config,
        exit_price_range,
        recommendation_id=recommendation_id,
    )
    linked_result = result.model_copy(update={"recommendation_id": recommendation_id})
    # 通知検証モード機能(2026-08追加): kill switchとは独立に、VALIDATIONでは
    # Recommendation/DecisionSnapshot保存自体をスキップする。
    if not execution_context.is_validation:
        recommendation_repo.save(recommendation)
        # 判定精度向上機能Phase A: DecisionSnapshotを記録する(スコア項目はPhase Bまで
        # 全てNone)。失敗しても既存の通知・戻り値には一切影響しない。
        save_decision_snapshot_safely(
            DecisionSnapshotRepository(), recommendation, DecisionType.HOLDING_DECISION, logger
        )
    outcome = _send_or_suppress_notification(
        recommendation, notification_enabled, notification_service, now
    )
    audit = HoldingEvaluationAudit(
        stock_code=holding.stock_code,
        evaluated_at=now,
        evaluation_status=(
            EvaluationStatus.DATA_QUALITY_BLOCKED
            if outcome.data_quality_blocked
            else EvaluationStatus.COMPLETED
        ),
        raw_sell_recommendation_type=None,
        raw_profit_recommendation_type=None,
        final_recommendation_type=recommendation.recommendation_type,
        notification_status=outcome.status,
        notification_suppression_reason=_resolve_suppression_reason(outcome),
        sell_signal_status="TRIGGERED",
        profit_taking_status="NOT_EVALUATED",
        fair_value_status="NOT_AVAILABLE",
        data_quality_status="BLOCKED" if outcome.data_quality_blocked else "OK",
        confidence=recommendation.confidence,
        error_code=None,
    )
    holding_result = _HoldingResult(
        recommended=True,
        notified=outcome.sent,
        succeeded=True,
        category=summary_category(audit),
        audit=audit,
        notification_category=(
            resolve_notification_category(recommendation) if outcome.sent else None
        ),
        recommendation_type_at_send=(
            recommendation.recommendation_type
            if outcome.sent and not outcome.data_quality_blocked
            else None
        ),
        detected_recommendation_type=(
            recommendation.recommendation_type if not outcome.data_quality_blocked else None
        ),
    )
    return holding_result, linked_result


def _analyze_one_holding(
    holding: Holding,
    now: dt.datetime,
    providers: ProviderBundle,
    config: AppConfig,
    profit_service: ProfitTakingService,
    sell_service: SellSignalService,
    holding_decision_service: HoldingDecisionService,
    runtime_config_service: HoldingDecisionRuntimeConfigService,
    holding_decision_result_repo: HoldingDecisionResultRepository,
    recommendation_repo: RecommendationRepository,
    notification_service: LineNotificationService,
    rule_version_service: RuleVersionService,
    portfolio_total_market_value: Decimal | None,
    portfolio_total_acquisition_cost: Decimal | None,
    execution_context: ExecutionContext = _DEFAULT_EXECUTION_CONTEXT,
) -> _HoldingResult:
    """1銘柄を判定・通知する。

    sell_signal/profit_takingは同一銘柄のデータを必要とするため、
    stock_snapshotを一度だけ取得して両方に渡す(実データ取得の重複を避ける)。
    """
    snapshot, error = build_stock_snapshot(providers, holding.stock_code, now, config)
    if snapshot is None:
        notification_service.notify_data_error(
            holding.stock_code, error or "データ取得エラー", now, stock_name=holding.stock_name
        )
        audit = HoldingEvaluationAudit(
            stock_code=holding.stock_code,
            evaluated_at=now,
            evaluation_status=EvaluationStatus.DATA_INSUFFICIENT,
            raw_sell_recommendation_type=None,
            raw_profit_recommendation_type=None,
            final_recommendation_type=None,
            notification_status=NotificationStatus.DATA_INSUFFICIENT,
            notification_suppression_reason=error,
            sell_signal_status="NOT_EVALUATED",
            profit_taking_status="NOT_EVALUATED",
            fair_value_status="NOT_AVAILABLE",
            data_quality_status="NOT_EVALUATED",
            confidence=None,
            error_code="DATA_FETCH_FAILED",
        )
        return _HoldingResult(
            recommended=False,
            notified=False,
            succeeded=False,
            category="data_insufficient",
            audit=audit,
        )

    # kill switchは緊急停止用途のため、mode等のTTLキャッシュを経由せず毎回
    # 最新値を取得する(実装プラン修正2)。ポートフォリオ集中リスク通知より前に取得し、
    # このサイクル内のすべての通知経路(集中リスク・旧売却・新保有判断・利確)へ
    # 同一の値を渡す(コードレビュー対応: kill switchの適用範囲を保有銘柄分析の
    # 全通知経路へ拡張する)。
    notification_enabled = runtime_config_service.get_notification_enabled()

    _evaluate_portfolio_concentration_and_notify(
        holding,
        snapshot.current_price,
        portfolio_total_market_value,
        portfolio_total_acquisition_cost,
        config,
        recommendation_repo,
        notification_service,
        rule_version_service,
        now,
        notification_enabled,
        execution_context,
    )

    # --- 新旧エンジンの排他制御(実装プラン11節) ---------------------------
    runtime_lookup = runtime_config_service.get_config(now)
    industry = classify_industry(snapshot.financial.sector, snapshot.financial.industry)
    financial_deferred_policy = resolve_financial_deferred_policy(
        runtime_lookup.config, config.industry_scoring_policy.financial_industry_policy
    )
    plan = resolve_execution_plan(
        runtime_lookup.config.mode,
        industry.classification,
        industry.financial_category,
        financial_deferred_policy,
        notification_enabled=notification_enabled,
    )
    # kill switchの影響を受けない「modeが今回の通知担当をどちらに割り当てたか」を得る
    # (コードレビュー対応: kill switch中でもRecommendationの生成・保存自体は継続する
    # ため、その要否判定はkill switch適用前の値で行う。notification_enabled=Trueの
    # 場合はplanと完全に同一になる)。run_*/execution_reasonはnotification_enabledの
    # 値に関わらず同一のためplan側を使い続ける。
    mode_plan = resolve_execution_plan(
        runtime_lookup.config.mode,
        industry.classification,
        industry.financial_category,
        financial_deferred_policy,
        notification_enabled=True,
    )

    legacy_result: _HoldingResult | None = None
    legacy_reason_codes: tuple[str, ...] = ()
    if plan.run_legacy_sell_evaluation:
        sell_outcome = sell_service.analyze(holding, now, snapshot=snapshot)
        legacy_reason_codes = sell_outcome.triggered_rule_names
        if sell_outcome.recommendation is not None and mode_plan.allow_legacy_sell_notification:
            legacy_result = _notify_legacy_sell_and_build_result(
                holding,
                now,
                sell_outcome.recommendation,
                recommendation_repo,
                notification_service,
                notification_enabled,
                execution_context,
            )

    holding_decision_result_notified: _HoldingResult | None = None
    if plan.run_holding_decision_evaluation:
        hd_outcome = holding_decision_service.evaluate(
            holding,
            now,
            plan.execution_reason,
            snapshot=snapshot,
            runtime_config_version=runtime_lookup.effective_runtime_config_version,
            legacy_reason_codes=legacy_reason_codes,
            financial_model_version_used=(
                config.industry_scoring_policy.financial_industry_policy.financial_model_version
            ),
        )
        if hd_outcome.integrity_error:
            integrity_audit = HoldingEvaluationAudit(
                stock_code=holding.stock_code,
                evaluated_at=now,
                evaluation_status=EvaluationStatus.ANALYSIS_FAILED,
                raw_sell_recommendation_type=None,
                raw_profit_recommendation_type=None,
                final_recommendation_type=None,
                notification_status=NotificationStatus.ANALYSIS_FAILED,
                notification_suppression_reason="DATA_INTEGRITY_ERROR",
                sell_signal_status="NOT_EVALUATED",
                profit_taking_status="NOT_EVALUATED",
                fair_value_status="NOT_AVAILABLE",
                data_quality_status="NOT_EVALUATED",
                confidence=None,
                error_code="DATA_INTEGRITY_ERROR",
            )
            if legacy_result is None:
                return _HoldingResult(
                    recommended=False,
                    notified=False,
                    succeeded=False,
                    category=summary_category(integrity_audit),
                    audit=integrity_audit,
                )
            # 旧エンジンが既にこのサイクルの通知を確定させている場合、新エンジン側の
            # shadow計算失敗によってその成功結果を上書きしない(ログにのみ残す)。
            logger.warning(
                "holding_decision DATA_INTEGRITY_ERROR (shadow, legacy already notified) "
                "stock_code=%s",
                holding.stock_code,
            )
        elif hd_outcome.result is not None:
            hd_result = hd_outcome.result
            if mode_plan.allow_holding_decision_notification and hd_result.should_notify:
                notify_fn = _notify_holding_decision_and_build_result
                holding_decision_result_notified, hd_result = notify_fn(
                    holding,
                    now,
                    hd_result,
                    snapshot,
                    config,
                    holding_decision_result_repo,
                    recommendation_repo,
                    notification_service,
                    notification_enabled,
                    execution_context,
                )
            # 通知検証モード機能(2026-08追加): VALIDATIONでは通常運用の判定履歴を
            # 汚さないため保存自体をスキップする。
            if not execution_context.is_validation:
                holding_decision_result_repo.save(hd_result)

    if legacy_result is not None:
        return legacy_result
    if holding_decision_result_notified is not None:
        return holding_decision_result_notified

    if not plan.run_profit_taking_when_no_sell_notification:
        audit = HoldingEvaluationAudit(
            stock_code=holding.stock_code,
            evaluated_at=now,
            evaluation_status=EvaluationStatus.COMPLETED,
            raw_sell_recommendation_type=None,
            raw_profit_recommendation_type=None,
            final_recommendation_type=None,
            notification_status=NotificationStatus.NOT_REQUIRED,
            notification_suppression_reason=None,
            sell_signal_status="NO_SIGNAL",
            profit_taking_status="NOT_EVALUATED",
            fair_value_status="NOT_AVAILABLE",
            data_quality_status="OK",
            confidence=None,
            error_code=None,
        )
        return _HoldingResult(
            recommended=False, notified=False, succeeded=True, category="hold", audit=audit
        )

    pt_outcome = profit_service.analyze(holding, now, snapshot=snapshot)
    if pt_outcome.recommendation is not None:
        # 通知検証モード機能(2026-08追加): kill switchとは独立に、VALIDATIONでは
        # Recommendation/DecisionSnapshot保存自体をスキップする。
        if not execution_context.is_validation:
            recommendation_repo.save(pt_outcome.recommendation)
            # 判定精度向上機能Phase A: DecisionSnapshotを記録する(スコア項目は
            # Phase Bまで全てNone)。失敗しても既存の通知・戻り値には一切影響しない。
            save_decision_snapshot_safely(
                DecisionSnapshotRepository(),
                pt_outcome.recommendation,
                DecisionType.PROFIT_TAKING,
                logger,
            )
        outcome = _send_or_suppress_notification(
            pt_outcome.recommendation, notification_enabled, notification_service, now
        )
        # 通知意図3段階化(2026-08): kill switch抑止時はoutcome.notification_intentが
        # 設定されない(_KILL_SWITCH_SUPPRESSED_OUTCOMEは判定自体を行わない)ため、
        # 「検出」件数(attention_detected_count、個別送信の成否を問わない)は
        # Recommendationから直接再計算する。実送信経路(evaluate_notification_
        # status内)と同じ唯一の正本(resolve_notification_intent_for_recommendation)
        # を使うため判定基準の重複は生じない。
        detected_intent = resolve_notification_intent_for_recommendation(pt_outcome.recommendation)
        attention_origin = (
            resolve_attention_origin_for_recommendation(pt_outcome.recommendation)
            if detected_intent is NotificationIntent.ATTENTION
            else None
        )
        audit = HoldingEvaluationAudit(
            stock_code=holding.stock_code,
            evaluated_at=now,
            evaluation_status=(
                EvaluationStatus.DATA_QUALITY_BLOCKED
                if outcome.data_quality_blocked
                else EvaluationStatus.COMPLETED
            ),
            raw_sell_recommendation_type=None,
            raw_profit_recommendation_type=pt_outcome.recommendation.raw_recommendation_type,
            final_recommendation_type=pt_outcome.recommendation.recommendation_type,
            notification_status=outcome.status,
            notification_suppression_reason=_resolve_suppression_reason(outcome),
            sell_signal_status="NO_SIGNAL",
            profit_taking_status="TRIGGERED",
            fair_value_status=(
                pt_outcome.recommendation.fair_value_overall_confidence.value
                if pt_outcome.recommendation.fair_value_overall_confidence
                else "NOT_AVAILABLE"
            ),
            data_quality_status="BLOCKED" if outcome.data_quality_blocked else "OK",
            confidence=pt_outcome.recommendation.confidence,
            error_code=None,
            notification_intent=detected_intent,
            attention_origin=attention_origin,
        )
        # 再コードレビュー対応(2026-08、追加修正1): 「有効なアクション検出」は
        # DataQuality安全ゲートでブロックされなかった場合のみ成立する
        # (DataQuality BLOCKED時はrecommendation_type自体は変わらないが、その
        # アクション判定をユーザー向けとして扱ってよい品質かは否定されているため、
        # detected/attention_detected/attention_sentのいずれからも除外し、既存の
        # summary_category()による「要確認」区分側の集計に委ねる)。kill switch
        # 抑止時はoutcome.data_quality_blocked=False(DataQuality判定自体が未実行)
        # のため、detectedはそのままカウントされる(kill switchは送信のみを止める
        # 緊急停止であり、判定の信頼性自体を否定するものではないため意図した挙動)。
        data_quality_ok = not outcome.data_quality_blocked
        return _HoldingResult(
            recommended=True,
            notified=outcome.sent,
            succeeded=True,
            category=summary_category(audit),
            audit=audit,
            attention_detected=data_quality_ok and detected_intent is NotificationIntent.ATTENTION,
            attention_sent=(
                outcome.sent and data_quality_ok and detected_intent is NotificationIntent.ATTENTION
            ),
            notification_category=(
                resolve_notification_category(pt_outcome.recommendation) if outcome.sent else None
            ),
            recommendation_type_at_send=(
                pt_outcome.recommendation.recommendation_type
                if outcome.sent and data_quality_ok
                else None
            ),
            detected_recommendation_type=(
                pt_outcome.recommendation.recommendation_type if data_quality_ok else None
            ),
        )

    audit = HoldingEvaluationAudit(
        stock_code=holding.stock_code,
        evaluated_at=now,
        evaluation_status=EvaluationStatus.COMPLETED,
        raw_sell_recommendation_type=None,
        raw_profit_recommendation_type=None,
        final_recommendation_type=None,
        notification_status=NotificationStatus.NOT_REQUIRED,
        notification_suppression_reason=None,
        sell_signal_status="NO_SIGNAL",
        profit_taking_status="NO_SIGNAL",
        fair_value_status="NOT_AVAILABLE",
        data_quality_status="OK",
        confidence=None,
        error_code=None,
    )
    return _HoldingResult(
        recommended=False, notified=False, succeeded=True, category="hold", audit=audit
    )


def _count_holding_summary_actions(entries: list[str]) -> dict[HoldingSummaryAction, int]:
    """保有株サマリーの4分類(一部売却/全部売却/売却/緊急確認)の件数集計。

    再コードレビュー対応(2026-08、追加修正4): 分類ロジックの唯一の正本
    resolve_holding_summary_action()(domain/entities/enums.py)のみを使い、
    detected集計・sent集計の両方でこの関数を共通利用する(分類の二重実装を
    避ける)。entriesは"{RecommendationType.value}|{stock_code}"形式
    (progress.detected_categories/notification_categoriesと同じ形式)。
    """
    counts: dict[HoldingSummaryAction, int] = {action: 0 for action in HoldingSummaryAction}
    for entry in entries:
        raw_type = entry.split("|", 1)[0]
        action = resolve_holding_summary_action(RecommendationType(raw_type))
        if action is not None:
            counts[action] += 1
    return counts


def _finish_batch_item(
    batch_id: str | None,
    category: str,
    stock_code: str,
    now: dt.datetime,
    notification_service: LineNotificationService,
    runtime_config_service: HoldingDecisionRuntimeConfigService,
    recommendation_type: RecommendationType | None = None,
    detected_recommendation_type: RecommendationType | None = None,
    attention_detected: bool = False,
    attention_sent: bool = False,
) -> None:
    """バッチ進捗の確定(record_result)はkill switchの影響を受けず常に行う。
    最終1件目の完了によるバッチサマリーLINE送信のみ、その時点のkill switch状態で
    ガードする(コードレビュー対応: 通知抑止がバッチ完了判定へ影響しないことを保証する)。

    再コードレビュー対応(2026-08、detected/sent一元化): サマリーのユーザー向け
    表示は「有効なアクション検出件数」(detected_recommendation_type、DataQuality
    安全ゲートを通過していればTradeCooldown等による個別送信抑止でも減らない)を
    使う。recommendation_type(実際にLINE送信された場合のみ呼び出し元が渡す、
    以前からの引数)はsent集計用としてそのまま残し、CloudWatch Logsでの監査・
    Issue #16評価用途にのみ使う(ユーザー向けサマリーには表示しない)。
    """
    if batch_id is None:
        return
    needs_code = category in ("data_insufficient", "failed")
    notification_category_entry = (
        f"{recommendation_type.value}|{stock_code}" if recommendation_type is not None else None
    )
    detected_category_entry = (
        f"{detected_recommendation_type.value}|{stock_code}"
        if detected_recommendation_type is not None
        else None
    )
    progress = record_result(
        batch_id,
        category,
        stock_code=stock_code if needs_code else None,
        notification_category_entry=notification_category_entry,
        detected_category_entry=detected_category_entry,
        attention_detected_stock_code=stock_code if attention_detected else None,
        attention_sent_stock_code=stock_code if attention_sent else None,
    )
    if progress is None or not progress.is_complete:
        return
    if not runtime_config_service.get_notification_enabled():
        logger.info(
            "kill_switch_suppressed: batch_summary batch_id=%s notification_enabled=False",
            batch_id,
        )
        return
    detected_counts = _count_holding_summary_actions(progress.detected_categories)
    sent_counts = _count_holding_summary_actions(progress.notification_categories)
    attention_detected_n = len(progress.attention_detected_stock_codes)
    attention_sent_n = len(progress.attention_sent_stock_codes)
    # 再コードレビュー対応(2026-08、指摘1・8): ユーザー向けサマリーには表示
    # しないdetected/sentの内訳をCloudWatch Logsへ構造化出力する(Issue #16の
    # 「1日平均ATTENTION判定数/個別通知数」評価、および将来のPARTIAL/FULL/SELL/
    # CRITICAL運用評価に使う)。
    logger.info(
        "holdings_summary_action_counts batch_id=%s "
        "partial_detected=%d partial_sent=%d "
        "full_detected=%d full_sent=%d "
        "sell_detected=%d sell_sent=%d "
        "critical_detected=%d critical_sent=%d "
        "attention_detected=%d attention_sent=%d",
        batch_id,
        detected_counts[HoldingSummaryAction.PARTIAL],
        sent_counts[HoldingSummaryAction.PARTIAL],
        detected_counts[HoldingSummaryAction.FULL],
        sent_counts[HoldingSummaryAction.FULL],
        detected_counts[HoldingSummaryAction.SELL],
        sent_counts[HoldingSummaryAction.SELL],
        detected_counts[HoldingSummaryAction.CRITICAL],
        sent_counts[HoldingSummaryAction.CRITICAL],
        attention_detected_n,
        attention_sent_n,
    )
    notification_service.notify_batch_summary(
        _PROCESS_NAME,
        progress.total,
        progress.category_counts,
        now,
        data_insufficient_stock_codes=progress.data_insufficient_stock_codes,
        failed_stock_codes=progress.failed_stock_codes,
        partial_sell_detected_count=detected_counts[HoldingSummaryAction.PARTIAL],
        full_sell_detected_count=detected_counts[HoldingSummaryAction.FULL],
        sell_detected_count=detected_counts[HoldingSummaryAction.SELL],
        critical_risk_detected_count=detected_counts[HoldingSummaryAction.CRITICAL],
        attention_detected_count=attention_detected_n,
        display_title="保有株チェック",
    )


def _process_single_holding(
    stock_code: str,
    batch_id: str | None,
    now: dt.datetime,
    providers: ProviderBundle,
    config: AppConfig,
    recommendation_repo: RecommendationRepository,
    notification_service: LineNotificationService,
    rule_version_service: RuleVersionService,
    portfolio_total_market_value: Decimal | None,
    portfolio_total_acquisition_cost: Decimal | None,
    execution_context: ExecutionContext = _DEFAULT_EXECUTION_CONTEXT,
) -> dict[str, Any]:
    runtime_config_service = HoldingDecisionRuntimeConfigService(
        cache_ttl_seconds=config.holding_decision.runtime_config_cache_ttl_seconds
    )
    holding = PortfolioService().get_holding(stock_code)
    if holding is None:
        logger.warning("dispatched holding not found stock_code=%s", stock_code)
        _finish_batch_item(
            batch_id, "failed", stock_code, now, notification_service, runtime_config_service
        )
        return {"stock_code": stock_code, "recommended": False, "notified": False, "found": False}

    profit_service = ProfitTakingService(
        providers=providers, config=config, execution_context=execution_context
    )
    sell_service = SellSignalService(
        providers=providers, config=config, execution_context=execution_context
    )
    holding_decision_service = HoldingDecisionService(
        providers,
        config,
        runtime_config_service=runtime_config_service,
        execution_context=execution_context,
    )
    holding_decision_result_repo = HoldingDecisionResultRepository()
    try:
        result = _analyze_one_holding(
            holding,
            now,
            providers,
            config,
            profit_service,
            sell_service,
            holding_decision_service,
            runtime_config_service,
            holding_decision_result_repo,
            recommendation_repo,
            notification_service,
            rule_version_service,
            portfolio_total_market_value,
            portfolio_total_acquisition_cost,
            execution_context,
        )
    except Exception:  # noqa: BLE001 - 1銘柄の想定外エラーで再帰呼び出し全体を落とさない
        logger.exception("holding analysis failed unexpectedly stock_code=%s", stock_code)
        _finish_batch_item(
            batch_id, "failed", stock_code, now, notification_service, runtime_config_service
        )
        return {"stock_code": stock_code, "recommended": False, "notified": False, "failed": True}

    logger.info("holding_evaluation_audit: %s", result.audit)
    _finish_batch_item(
        batch_id,
        result.category,
        stock_code,
        now,
        notification_service,
        runtime_config_service,
        recommendation_type=result.recommendation_type_at_send,
        detected_recommendation_type=result.detected_recommendation_type,
        attention_detected=result.attention_detected,
        attention_sent=result.attention_sent,
    )
    return {
        "stock_code": stock_code,
        "recommended": result.recommended,
        "notified": result.notified,
        "evaluation_status": result.audit.evaluation_status.value,
        "notification_status": result.audit.notification_status.value,
    }


def _estimate_portfolio_totals(
    holdings: list[Holding], providers: ProviderBundle
) -> tuple[Decimal | None, Decimal | None]:
    """ポートフォリオ全体の時価総額・取得価格総額を概算する(要求仕様§14)。

    フルスナップショット(財務・適正価格等)は取得コストが高いため、時価総額の
    概算には現在株価のみを取得する軽量なget_latest_priceを使う。1銘柄でも
    価格取得に失敗した場合、時価総額ベースの比率は算出不能(None)とする
    (一部の銘柄を除外した不正確な合計を「全体」として扱わない)。
    """
    total_acquisition_cost = sum((h.total_purchase_amount for h in holdings), start=Decimal("0"))
    total_market_value: Decimal | None = Decimal("0")
    for holding in holdings:
        try:
            snap = providers.market_data.get_latest_price(holding.stock_code)
        except Exception:  # noqa: BLE001 - 1銘柄の株価取得エラーでバッチ全体を落とさない
            logger.exception(
                "portfolio total estimation: price fetch failed stock_code=%s", holding.stock_code
            )
            snap = None
        if snap is None:
            total_market_value = None
            continue
        if total_market_value is not None:
            total_market_value += snap.close_price * holding.shares
    return total_market_value, total_acquisition_cost


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    # 通知検証モード機能(2026-08追加)。不正なexecution_modeは他の一切の処理より
    # 前にここで例外を送出し、Lambda呼び出し自体を失敗させる(NORMALへフォール
    # バックしない)。
    execution_context = resolve_execution_context(event)
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    providers = build_real_provider_bundle(now, config)
    # 常に本番テーブル(同一実行内でRecommendationを再読込みする経路が無いため)
    recommendation_repo = RecommendationRepository()
    # BUY候補裾野拡大機能(2026-08、§5-1): 子Lambda(task=holding)は親Lambdaが
    # detect_and_apply()の結果をイベントペイロード経由で伝播した
    # trade_detection_confirmedをそのまま使う。
    trade_detection_confirmed = event.get("trade_detection_confirmed", True)
    notification_service = LineNotificationService(
        line_client=build_line_client_from_env(),
        notification_log_repository=NotificationLogRepository(),
        recommendation_repository=recommendation_repo,
        config=config,
        execution_context=execution_context,
        trade_detection_confirmed=trade_detection_confirmed,
    )
    rule_version_service = RuleVersionService()

    task = event.get("task")
    if task == "holding":
        # 子Lambda: batch_idはevent由来なのでこの時点で既に確定している。
        if execution_context.is_validation:
            logger.info(
                "VALIDATION MODE task=holding execution_mode=VALIDATION "
                "validation_run_id=%s stock_code=%s",
                event.get("batch_id"),
                event["stock_code"],
            )
        portfolio_total_market_value = (
            Decimal(event["portfolio_total_market_value"])
            if event.get("portfolio_total_market_value") is not None
            else None
        )
        portfolio_total_acquisition_cost = (
            Decimal(event["portfolio_total_acquisition_cost"])
            if event.get("portfolio_total_acquisition_cost") is not None
            else None
        )
        result = _process_single_holding(
            event["stock_code"],
            event.get("batch_id"),
            now,
            providers,
            config,
            recommendation_repo,
            notification_service,
            rule_version_service,
            portfolio_total_market_value,
            portfolio_total_acquisition_cost,
            execution_context,
        )
        logger.info("holdings_watchlist_handler single holding done: %s", result)
        return result

    # 通常のスケジュール起動(ディスパッチのみ行い、銘柄ごとの実処理は非同期の
    # 自己再帰呼び出しに委ねる。全銘柄を直列処理するとLambdaの最大タイムアウト
    # (900秒)を超えうるため)。ウォッチリストの買いシグナル評価はbuy_candidates_
    # handler.pyの統合BUY候補パイプラインへ一本化済みのため、ここでは保有銘柄の
    # 売却・利確判定のみをdispatchする。
    # 株主優待レジストリの読み込み件数チェックは、銘柄ごとのワーカー呼び出しでは
    # なくバッチ開始時(ここ)で1回だけ行う(2026-07仕様レビュー対応)。
    check_registry_health(
        config.notification.operations.shareholder_benefit_registry_min_expected_entries
    )
    function_name = resolve_function_name(context, os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""))
    holdings = PortfolioService().list_holdings()

    # --- BUY候補裾野拡大機能(2026-08、§5-1・§5-2): 売買イベント検知を
    # BUY候補Lambda・保有銘柄Lambdaの起動順序に依存させない。両ハンドラの
    # 入口でTradeCooldownService.detect_and_apply()を呼ぶ(冪等・PROCESSING/
    # COMPLETEDロックにより当日1回だけ実際の検知処理が走る)。
    calendar = BusinessCalendar.from_config(config.holiday_calendar)
    trade_cooldown_service = TradeCooldownService(
        business_calendar=calendar,
        config=config.notification.trade_cooldown,
        execution_context=execution_context,
    )
    current_holdings_by_code = {h.stock_code: h for h in holdings}
    detection_outcome = trade_cooldown_service.detect_and_apply(current_holdings_by_code, now)
    if detection_outcome.confirmed:
        watch_state_service = WatchStateService(
            business_calendar=calendar, execution_context=execution_context
        )
        watch_state_service.end_for_trade_events(detection_outcome.events, now.date())
    else:
        logger.warning(
            "holdings_watchlist_handler: trade detection not confirmed this run "
            "(TRADE_DETECTION_IN_PROGRESSとして通常通知をfail-closedする)"
        )

    total = len(holdings)
    batch_id = f"holdings-watchlist-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    start_batch(batch_id, total, now)
    if execution_context.is_validation:
        # 通知検証モード機能(2026-08追加): batch_idはここで初めて確定するため、
        # イベント解析直後ではなくこの時点でVALIDATION開始ログを出す。
        logger.info(
            "VALIDATION MODE START execution_mode=VALIDATION validation_run_id=%s target_count=%d",
            batch_id,
            total,
        )

    portfolio_total_market_value, portfolio_total_acquisition_cost = _estimate_portfolio_totals(
        holdings, providers
    )

    for holding in holdings:
        dispatch_async(
            function_name,
            {
                "task": "holding",
                "stock_code": holding.stock_code,
                "batch_id": batch_id,
                "portfolio_total_market_value": (
                    str(portfolio_total_market_value)
                    if portfolio_total_market_value is not None
                    else None
                ),
                "portfolio_total_acquisition_cost": str(portfolio_total_acquisition_cost),
                "execution_mode": execution_context.mode.value,
                "trade_detection_confirmed": detection_outcome.confirmed,
            },
        )

    logger.info(
        "holdings_watchlist_handler dispatched: holdings=%d batch_id=%s",
        len(holdings),
        batch_id,
    )
    return {"dispatched_holdings": len(holdings)}
