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
from jstock_advisor.domain.entities.holding_evaluation_record import (
    HoldingEvaluationRecord,
    build_holding_evaluation_id,
)
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.domain.signals.exit_price_range import evaluate_exit_price_range
from jstock_advisor.domain.signals.holding_decision_execution_plan import (
    resolve_execution_plan,
    resolve_financial_deferred_policy,
)
from jstock_advisor.domain.signals.portfolio_concentration import evaluate_portfolio_concentration
from jstock_advisor.infrastructure.aws.batch_tracker import (
    mark_completion_finalize_completed,
    record_result,
    start_batch,
    try_acquire_completion_finalize,
)
from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.holding_decision_result_repository import (
    HoldingDecisionResultRepository,
)
from jstock_advisor.infrastructure.local_repository.holding_evaluation_record_repository import (
    HoldingEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.holding_repository import HoldingRepository
from jstock_advisor.infrastructure.local_repository.notification_claim_repository import (
    NotificationClaimRepository,
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
from jstock_advisor.services.yfinance_rate_limit import call_with_rate_limit_retry

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
    # Phase 2-B「銘柄分析」向け(2026-08): HoldingEvaluationRecordのauthoritative_
    # recommendation_idへそのまま渡すための、この結果を生んだRecommendationのID。
    # 通知の有無に関わらず、Recommendationが作成された場合のみ設定する。
    recommendation_id: str | None = None


_KILL_SWITCH_SUPPRESSED_OUTCOME = NotificationOutcome(
    status=NotificationStatus.KILL_SWITCH_SUPPRESSED, sent=False, data_quality_blocked=False
)


def _send_or_suppress_notification(
    recommendation: Recommendation,
    notification_enabled: bool,
    notification_service: LineNotificationService,
    now: dt.datetime,
) -> NotificationOutcome:
    """notification_enabled=False(kill switch)。Recommendationの生成・保存は
    notification_enabledの値に関わらず常に継続する(呼び出し側で保証する)。
    この関数はLINE送信の可否のみを制御する。旧売却・新保有判断・利確・ポートフォリオ
    集中リスクの4経路すべてがこの1関数を経由することで、抑止結果
    (KILL_SWITCH_SUPPRESSED)を統一する。

    再コードレビュー対応(2026-08、追加修正1): notification_enabled=Falseの間も
    LINE送信は一切行わないが、ユーザー向けサマリーの「検出」件数(detected_
    recommendation_type/attention_detected)に使うDataQuality判定(整合性検証・
    異常値検知)だけは、対象がACTIONABLE/ATTENTION(=保有株サマリーの一部売却・
    全部売却・売却・緊急確認・利益保全注意のいずれかとして計上され得る)の場合に
    限り、副作用の無い読み取り専用メソッドcheck_data_quality_eligibility()
    (BUY候補パイプラインで既に実運用されている、manual review LINE送信を
    一切発生させない判定専用メソッド)で行う。INTERNAL_ONLY(通常WATCH・決算待ち・
    ポートフォリオ集中レビュー等)はそもそも上記のどのサマリー分類の対象にもならない
    ため、detected判定目的でDataQuality評価を行う必要が無い(data_quality_blocked=
    Falseのまま、既存のNON_ACTIONABLE時のAudit上の意味は変更しない)。
    """
    if not notification_enabled:
        logger.info(
            "notification_disabled_suppressed: stock_code=%s recommendation_id=%s "
            "recommendation_type=%s notification_enabled=False",
            recommendation.stock_code,
            recommendation.recommendation_id,
            recommendation.recommendation_type.value,
        )
        intent = resolve_notification_intent_for_recommendation(recommendation)
        if intent is NotificationIntent.INTERNAL_ONLY:
            return _KILL_SWITCH_SUPPRESSED_OUTCOME
        eligibility = notification_service.check_data_quality_eligibility(recommendation, now)
        return NotificationOutcome(
            status=NotificationStatus.KILL_SWITCH_SUPPRESSED,
            sent=False,
            data_quality_blocked=not eligibility.eligible,
        )
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
        recommendation_id=recommendation.recommendation_id,
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
        recommendation_id=recommendation.recommendation_id,
    )
    return holding_result, linked_result


def _persist_holding_evaluation_record(
    holding_evaluation_record_repo: HoldingEvaluationRecordRepository,
    holding: Holding,
    now: dt.datetime,
    execution_context: ExecutionContext,
    rule_version: str,
    *,
    execution_plan_mode: str | None,
    execution_plan_reason: str | None,
    notification_enabled: bool | None,
    authoritative_engine: str | None,
    authoritative_outcome_category: str,
    authoritative_recommendation_id: str | None,
    authoritative_audit_log_id: str | None = None,
    authoritative_notification_sent: bool,
    legacy_sell_ran: bool,
    legacy_sell_recommendation_id: str | None,
    profit_taking_ran: bool,
    profit_taking_recommendation_id: str | None,
    holding_decision_ran: bool,
    holding_decision_result_id: str | None,
    holding_decision_notified: bool,
) -> None:
    """Phase 2-B「銘柄分析」向け(2026-08): 呼び出し側で評価本体の結果を一旦
    構造化した(このキーワード引数群)うえで、HoldingEvaluationRecordとして
    1件記録する。_analyze_one_holding()の全ての戻り経路(データ取得失敗・
    整合性エラー・各エンジンの通知確定・利確判定・純粋なHOLDの全て)から
    呼ばれ、必ず1件のレコードが残る。VALIDATIONでは既存のRecommendation等と
    同様、判定履歴を汚さないため保存自体をスキップする。参照用の補助レコード
    のため、保存に失敗しても既存の通知・戻り値には一切影響させない
    (save_decision_snapshot_safelyと同じfire-and-forget方針)。
    """
    if execution_context.is_validation:
        return
    record = HoldingEvaluationRecord(
        holding_evaluation_id=build_holding_evaluation_id(holding.holding_id, now),
        holding_id=holding.holding_id,
        owner=holding.owner,
        stock_code=holding.stock_code,
        evaluated_at=now,
        rule_version=rule_version,
        execution_plan_mode=execution_plan_mode,
        execution_plan_reason=execution_plan_reason,
        notification_enabled=notification_enabled,
        authoritative_engine=authoritative_engine,
        authoritative_outcome_category=authoritative_outcome_category,
        authoritative_recommendation_id=authoritative_recommendation_id,
        authoritative_audit_log_id=authoritative_audit_log_id,
        authoritative_notification_sent=authoritative_notification_sent,
        legacy_sell_ran=legacy_sell_ran,
        legacy_sell_recommendation_id=legacy_sell_recommendation_id,
        profit_taking_ran=profit_taking_ran,
        profit_taking_recommendation_id=profit_taking_recommendation_id,
        holding_decision_ran=holding_decision_ran,
        holding_decision_result_id=holding_decision_result_id,
        holding_decision_notified=holding_decision_notified,
    )
    try:
        holding_evaluation_record_repo.save(record)
    except Exception:  # noqa: BLE001 - 記録失敗で既存の通知・戻り値に影響させない
        logger.exception("holding_evaluation_record_save_failed holding_id=%s", holding.holding_id)


def _resolve_mode_designated_engine(
    mode_plan: Any,
) -> str | None:
    """kill switchの影響を受けないmode_plan(notification_enabled=True相当)を
    基準に「本来の判定担当」エンジンを決定する(コードレビュー対応: authoritative_
    engineはkill switch適用後のplan.allow_*_notificationだけから決定しない)。
    """
    if mode_plan.allow_legacy_sell_notification:
        return "LEGACY_SELL"
    if mode_plan.allow_holding_decision_notification:
        return "HOLDING_DECISION_SCORE"
    return None


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
    holding_evaluation_record_repo: HoldingEvaluationRecordRepository,
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
    # Issue #59 Phase B1: BUY経路と同じ理由で再試行ヘルパーで包む。再試行しても
    # 回復しない場合のみ例外が伝播し、呼び出し元の銘柄単位exceptで
    # EvaluationStatus.ANALYSIS_FAILEDとして記録される(取得失敗と
    # DATA_INSUFFICIENTを混同しない)。
    retry_result = call_with_rate_limit_retry(
        lambda: build_stock_snapshot(providers, holding.stock_code, now, config)
    )
    if retry_result.error is not None:
        raise retry_result.error
    assert retry_result.value is not None
    snapshot, error = retry_result.value
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
        result = _HoldingResult(
            recommended=False,
            notified=False,
            succeeded=False,
            category="data_insufficient",
            audit=audit,
        )
        _persist_holding_evaluation_record(
            holding_evaluation_record_repo,
            holding,
            now,
            execution_context,
            rule_version_service.get_active_version_or(RULE_VERSION_PLACEHOLDER),
            execution_plan_mode=None,
            execution_plan_reason=None,
            notification_enabled=None,
            authoritative_engine=None,
            authoritative_outcome_category=result.category,
            authoritative_recommendation_id=None,
            authoritative_notification_sent=False,
            legacy_sell_ran=False,
            legacy_sell_recommendation_id=None,
            profit_taking_ran=False,
            profit_taking_recommendation_id=None,
            holding_decision_ran=False,
            holding_decision_result_id=None,
            holding_decision_notified=False,
        )
        return result

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
                result = _HoldingResult(
                    recommended=False,
                    notified=False,
                    succeeded=False,
                    category=summary_category(integrity_audit),
                    audit=integrity_audit,
                )
                _persist_holding_evaluation_record(
                    holding_evaluation_record_repo,
                    holding,
                    now,
                    execution_context,
                    rule_version_service.get_active_version_or(RULE_VERSION_PLACEHOLDER),
                    execution_plan_mode=runtime_lookup.config.mode.value,
                    execution_plan_reason=plan.execution_reason.value,
                    notification_enabled=notification_enabled,
                    authoritative_engine="HOLDING_DECISION_SCORE",
                    authoritative_outcome_category=result.category,
                    authoritative_recommendation_id=None,
                    authoritative_notification_sent=False,
                    legacy_sell_ran=plan.run_legacy_sell_evaluation,
                    legacy_sell_recommendation_id=None,
                    profit_taking_ran=False,
                    profit_taking_recommendation_id=None,
                    holding_decision_ran=True,
                    holding_decision_result_id=None,
                    holding_decision_notified=False,
                )
                return result
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
        _persist_holding_evaluation_record(
            holding_evaluation_record_repo,
            holding,
            now,
            execution_context,
            rule_version_service.get_active_version_or(RULE_VERSION_PLACEHOLDER),
            execution_plan_mode=runtime_lookup.config.mode.value,
            execution_plan_reason=plan.execution_reason.value,
            notification_enabled=notification_enabled,
            authoritative_engine="LEGACY_SELL",
            authoritative_outcome_category=legacy_result.category,
            authoritative_recommendation_id=legacy_result.recommendation_id,
            authoritative_audit_log_id=sell_outcome.audit_id,
            authoritative_notification_sent=legacy_result.notified,
            legacy_sell_ran=True,
            legacy_sell_recommendation_id=legacy_result.recommendation_id,
            profit_taking_ran=False,
            profit_taking_recommendation_id=None,
            holding_decision_ran=plan.run_holding_decision_evaluation,
            holding_decision_result_id=None,
            holding_decision_notified=False,
        )
        return legacy_result
    if holding_decision_result_notified is not None:
        _persist_holding_evaluation_record(
            holding_evaluation_record_repo,
            holding,
            now,
            execution_context,
            rule_version_service.get_active_version_or(RULE_VERSION_PLACEHOLDER),
            execution_plan_mode=runtime_lookup.config.mode.value,
            execution_plan_reason=plan.execution_reason.value,
            notification_enabled=notification_enabled,
            authoritative_engine="HOLDING_DECISION_SCORE",
            authoritative_outcome_category=holding_decision_result_notified.category,
            authoritative_recommendation_id=holding_decision_result_notified.recommendation_id,
            authoritative_notification_sent=holding_decision_result_notified.notified,
            legacy_sell_ran=plan.run_legacy_sell_evaluation,
            legacy_sell_recommendation_id=None,
            profit_taking_ran=False,
            profit_taking_recommendation_id=None,
            holding_decision_ran=True,
            holding_decision_result_id=hd_result.holding_decision_result_id,
            holding_decision_notified=True,
        )
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
        result = _HoldingResult(
            recommended=False, notified=False, succeeded=True, category="hold", audit=audit
        )
        mode_designated_engine = _resolve_mode_designated_engine(mode_plan)
        _persist_holding_evaluation_record(
            holding_evaluation_record_repo,
            holding,
            now,
            execution_context,
            rule_version_service.get_active_version_or(RULE_VERSION_PLACEHOLDER),
            execution_plan_mode=runtime_lookup.config.mode.value,
            execution_plan_reason=plan.execution_reason.value,
            notification_enabled=notification_enabled,
            authoritative_engine=mode_designated_engine,
            authoritative_outcome_category=result.category,
            authoritative_recommendation_id=None,
            authoritative_audit_log_id=(
                sell_outcome.audit_id
                if mode_designated_engine == "LEGACY_SELL" and plan.run_legacy_sell_evaluation
                else None
            ),
            authoritative_notification_sent=False,
            legacy_sell_ran=plan.run_legacy_sell_evaluation,
            legacy_sell_recommendation_id=None,
            profit_taking_ran=False,
            profit_taking_recommendation_id=None,
            holding_decision_ran=plan.run_holding_decision_evaluation,
            holding_decision_result_id=None,
            holding_decision_notified=False,
        )
        return result

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
        # 通知意図3段階化(2026-08): _send_or_suppress_notification()が返す
        # outcome.notification_intentは設定されないため(値を使わない設計、
        # 上記関数のdocstring参照)、「検出」件数(attention_detected_count、
        # 個別送信の成否を問わない)の対象判定はRecommendationから直接再計算する。
        # 実送信経路(evaluate_notification_status内)と同じ唯一の正本
        # (resolve_notification_intent_for_recommendation)を使うため判定基準の
        # 重複は生じない。
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
        # summary_category()による「要確認」区分側の集計に委ねる)。
        # 再コードレビュー対応(2026-08、追加修正1・notification_enabled=False時の
        # DataQuality評価): notification_enabled=False中も、ATTENTION対象であれば
        # _send_or_suppress_notification()がcheck_data_quality_eligibility()経由で
        # DataQualityを正しく評価し、outcome.data_quality_blockedへ反映する
        # (notification_enabled=Falseは送信のみを止める仕組みであり、判定の
        # 信頼性自体を否定するものではないため)。
        data_quality_ok = not outcome.data_quality_blocked
        result = _HoldingResult(
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
            recommendation_id=pt_outcome.recommendation.recommendation_id,
        )
        _persist_holding_evaluation_record(
            holding_evaluation_record_repo,
            holding,
            now,
            execution_context,
            rule_version_service.get_active_version_or(RULE_VERSION_PLACEHOLDER),
            execution_plan_mode=runtime_lookup.config.mode.value,
            execution_plan_reason=plan.execution_reason.value,
            notification_enabled=notification_enabled,
            authoritative_engine="PROFIT_TAKING",
            authoritative_outcome_category=result.category,
            authoritative_recommendation_id=result.recommendation_id,
            authoritative_audit_log_id=pt_outcome.audit_id,
            authoritative_notification_sent=result.notified,
            legacy_sell_ran=plan.run_legacy_sell_evaluation,
            legacy_sell_recommendation_id=None,
            profit_taking_ran=True,
            profit_taking_recommendation_id=result.recommendation_id,
            holding_decision_ran=plan.run_holding_decision_evaluation,
            holding_decision_result_id=None,
            holding_decision_notified=False,
        )
        return result

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
    result = _HoldingResult(
        recommended=False, notified=False, succeeded=True, category="hold", audit=audit
    )
    mode_designated_engine = _resolve_mode_designated_engine(mode_plan)
    _persist_holding_evaluation_record(
        holding_evaluation_record_repo,
        holding,
        now,
        execution_context,
        rule_version_service.get_active_version_or(RULE_VERSION_PLACEHOLDER),
        execution_plan_mode=runtime_lookup.config.mode.value,
        execution_plan_reason=plan.execution_reason.value,
        notification_enabled=notification_enabled,
        authoritative_engine=mode_designated_engine,
        authoritative_outcome_category=result.category,
        authoritative_recommendation_id=None,
        authoritative_audit_log_id=(
            sell_outcome.audit_id
            if mode_designated_engine == "LEGACY_SELL" and plan.run_legacy_sell_evaluation
            else None
        ),
        authoritative_notification_sent=False,
        legacy_sell_ran=plan.run_legacy_sell_evaluation,
        legacy_sell_recommendation_id=None,
        profit_taking_ran=True,
        profit_taking_recommendation_id=None,
        holding_decision_ran=plan.run_holding_decision_evaluation,
        holding_decision_result_id=None,
        holding_decision_notified=False,
    )
    return result


def _count_holding_summary_actions(entries: list[str]) -> dict[HoldingSummaryAction, int]:
    """保有株サマリーの4分類(一部売却/全部売却/売却/緊急確認)の件数集計。

    再コードレビュー対応(2026-08、追加修正4): 分類ロジックの唯一の正本
    resolve_holding_summary_action()(domain/entities/enums.py)のみを使い、
    detected集計・sent集計の両方でこの関数を共通利用する(分類の二重実装を
    避ける)。entriesは"{RecommendationType.value}|{holding_id}"形式
    (progress.detected_categories/notification_categoriesと同じ形式。
    M3.1: このハンドラでは"|"より後ろはholding_idであり、件数集計自体は
    "|"より前(RecommendationType)しか見ないため計算結果に影響しない)。
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
    holding_id: str,
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

    M3.1(命名整理): このモジュールでは、record_result()/BatchProgress側の
    汎用的な"stock_code"引数・フィールド名(buy_candidates_handler.py側では
    文字どおりstock_code)へ、実際にはholding_id(= owner + "#" + stock_code)を
    渡している。DynamoDBの文字列セットは同一値の重複を許さないため、holding_id
    単位で渡すことで同一銘柄を複数ownerが保有していても正しく別件として集計
    される(機能はM3切替時から変更していない、命名・コメントの整理のみ)。
    holding_idの文字列自体がowner・stock_codeの両方を含むため、data_insufficient/
    failedのサマリー表示(下記notify_batch_summary呼び出し)は追加の変更なしで
    owner・stock_codeの両方を識別できる。
    """
    if batch_id is None:
        return
    needs_code = category in ("data_insufficient", "failed")
    notification_category_entry = (
        f"{recommendation_type.value}|{holding_id}" if recommendation_type is not None else None
    )
    detected_category_entry = (
        f"{detected_recommendation_type.value}|{holding_id}"
        if detected_recommendation_type is not None
        else None
    )
    progress = record_result(
        batch_id,
        category,
        # M3.1: 引数名はstock_codeだが、この保有銘柄パイプラインではholding_idを渡す
        # (上記docstring参照)。
        stock_code=holding_id if needs_code else None,
        notification_category_entry=notification_category_entry,
        detected_category_entry=detected_category_entry,
        attention_detected_stock_code=holding_id if attention_detected else None,
        attention_sent_stock_code=holding_id if attention_sent else None,
    )
    if progress is None or not progress.is_complete:
        return
    if not runtime_config_service.get_notification_enabled():
        logger.info(
            "kill_switch_suppressed: batch_summary batch_id=%s notification_enabled=False",
            batch_id,
        )
        return
    # Issue #31: completedカウンタは非冪等なADDのため、処理済みholding_idの
    # Lambda非同期retryでis_completeが再成立しうる。summary送信フローの実行権を
    # 原子的に取得し、取得できた1実行だけが以降を実行する(kill switch判定は
    # 副作用が無いためゲートの前に置き、抑止中はacquire自体を行わない=
    # 解除後の後続トリガーで従来どおり送信可能という既存意味論を維持)。
    finalize_token = try_acquire_completion_finalize(batch_id, now)
    if finalize_token is None:
        logger.info(
            "batch summary finalize skipped (already acquired or completed) batch_id=%s",
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
    # Issue #31: 完了処理の正常終了を記録する。これ以降、同一batch_idの
    # acquireは(経過時間に関わらず)永久に失敗する。summary送信が例外の場合は
    # ここへ到達せず、stale化(1200秒)後に後続トリガーがtakeoverして再実行できる。
    mark_completion_finalize_completed(batch_id, finalize_token, now)


def _process_single_holding(
    holding_id: str,
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
    """M3(保有銘柄オーナー機能): holding_id(= owner + "#" + stock_code)単位で
    対象Holdingを特定する。同一stock_codeでも複数ownerが保有する場合、
    それぞれ独立したworker呼び出しとして処理される。"""
    runtime_config_service = HoldingDecisionRuntimeConfigService(
        cache_ttl_seconds=config.holding_decision.runtime_config_cache_ttl_seconds
    )
    holding = HoldingRepository().get(holding_id)
    if holding is None:
        logger.warning("dispatched holding not found holding_id=%s", holding_id)
        _finish_batch_item(
            batch_id, "failed", holding_id, now, notification_service, runtime_config_service
        )
        return {"holding_id": holding_id, "recommended": False, "notified": False, "found": False}

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
    holding_evaluation_record_repo = HoldingEvaluationRecordRepository()
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
            holding_evaluation_record_repo,
            recommendation_repo,
            notification_service,
            rule_version_service,
            portfolio_total_market_value,
            portfolio_total_acquisition_cost,
            execution_context,
        )
    except Exception:  # noqa: BLE001 - 1銘柄の想定外エラーで再帰呼び出し全体を落とさない
        logger.exception("holding analysis failed unexpectedly holding_id=%s", holding_id)
        _finish_batch_item(
            batch_id, "failed", holding_id, now, notification_service, runtime_config_service
        )
        return {"holding_id": holding_id, "recommended": False, "notified": False, "failed": True}

    logger.info("holding_evaluation_audit: %s", result.audit)
    _finish_batch_item(
        batch_id,
        result.category,
        holding_id,
        now,
        notification_service,
        runtime_config_service,
        recommendation_type=result.recommendation_type_at_send,
        detected_recommendation_type=result.detected_recommendation_type,
        attention_detected=result.attention_detected,
        attention_sent=result.attention_sent,
    )
    return {
        "holding_id": holding_id,
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
        # LINE通知dedupの原子化(Issue #17): NORMAL実行の送信決定を原子的に
        # 一意化するclaimリポジトリ(VALIDATION/DRY_RUNでは使用されない)。
        notification_claim_repository=NotificationClaimRepository(),
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
                "notification_mode=%s event_notification_mode=%r is_dry_run=%s "
                "validation_run_id=%s holding_id=%s",
                execution_context.notification_mode.value,
                event.get("notification_mode"),
                execution_context.is_dry_run,
                event.get("batch_id"),
                event["holding_id"],
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
            event["holding_id"],
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
    current_holdings_by_id = {h.holding_id: h for h in holdings}
    detection_outcome = trade_cooldown_service.detect_and_apply(current_holdings_by_id, now)
    if detection_outcome.confirmed:
        watch_state_service = WatchStateService(
            business_calendar=calendar, execution_context=execution_context
        )
        # 再コードレビュー対応(2026-08、JST暦日境界修正・指摘4): TradeCooldownService
        # がevaluation_date_jst(now)を基準日として検知・記録するようになったため、
        # 同じ売買イベントに紐づくWatchState終了もこれと同一のJST暦日を使う
        # (ended_at/last_evaluated_atはWatchStateの営業日ベースの経過判定
        # (business_days_between等)に使われる値のため、TradeDetectionと
        # 異なる基準日にすると同一WatchStateの日付系列が矛盾する)。
        watch_state_service.end_for_trade_events(detection_outcome.events, evaluation_date_jst(now))
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
            "VALIDATION MODE START execution_mode=VALIDATION notification_mode=%s "
            "validation_run_id=%s target_count=%d",
            execution_context.notification_mode.value,
            batch_id,
            total,
        )

    portfolio_total_market_value, portfolio_total_acquisition_cost = _estimate_portfolio_totals(
        holdings, providers
    )

    for holding in holdings:
        child_payload: dict[str, Any] = {
            "task": "holding",
            "holding_id": holding.holding_id,
            "batch_id": batch_id,
            "portfolio_total_market_value": (
                str(portfolio_total_market_value)
                if portfolio_total_market_value is not None
                else None
            ),
            "portfolio_total_acquisition_cost": str(portfolio_total_acquisition_cost),
            "execution_mode": execution_context.mode.value,
            "trade_detection_confirmed": detection_outcome.confirmed,
        }
        # バグ修正(2026-08、通知ドライラン機能): notification_modeを子Lambdaへ
        # 伝播し忘れており、VALIDATION+DRY_RUNで起動しても子Lambda側は
        # notification_mode未指定→既定のSEND扱いとなり、実LINE送信が抑止
        # されない不備があった。NORMAL実行時はresolve_execution_context()が
        # execution_mode=NORMAL+notification_mode指定をエラーにするため、
        # VALIDATION時のみキー自体を追加する(NORMAL実行への影響を避ける)。
        if execution_context.is_validation:
            child_payload["notification_mode"] = execution_context.notification_mode.value
        dispatch_async(
            function_name,
            child_payload,
        )

    logger.info(
        "holdings_watchlist_handler dispatched: holdings=%d batch_id=%s",
        len(holdings),
        batch_id,
    )
    return {"dispatched_holdings": len(holdings)}
