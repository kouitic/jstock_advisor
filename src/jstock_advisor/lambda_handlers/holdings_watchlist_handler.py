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
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    EvaluationStatus,
    NotificationStatus,
    RecommendationType,
)
from jstock_advisor.domain.entities.evaluation_audit import HoldingEvaluationAudit, summary_category
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.portfolio_concentration import evaluate_portfolio_concentration
from jstock_advisor.infrastructure.aws.batch_tracker import record_result, start_batch
from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers._fanout import dispatch_async, resolve_function_name
from jstock_advisor.services.buy_signal_service import RULE_VERSION_PLACEHOLDER
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.provider_factory import build_real_provider_bundle
from jstock_advisor.services.rule_version_service import RuleVersionService
from jstock_advisor.services.sell_signal_service import SellSignalService
from jstock_advisor.services.shareholder_benefit_registry_service import check_registry_health
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_PROCESS_NAME = "保有銘柄分析"


@dataclass(frozen=True)
class _HoldingResult:
    recommended: bool
    notified: bool
    succeeded: bool
    category: str
    audit: HoldingEvaluationAudit


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
) -> None:
    """企業価値判断とは独立に、ポートフォリオ内保有比率が高い場合に別途通知する
    (要求仕様§14)。銘柄単体の判定結果には影響しない(常に別のRecommendationとして扱う)。
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
    recommendation_repo.save(recommendation)
    notification_service.notify_recommendation(recommendation, now)


def _analyze_one_holding(
    holding: Holding,
    now: dt.datetime,
    providers: ProviderBundle,
    config: AppConfig,
    profit_service: ProfitTakingService,
    sell_service: SellSignalService,
    recommendation_repo: RecommendationRepository,
    notification_service: LineNotificationService,
    rule_version_service: RuleVersionService,
    portfolio_total_market_value: Decimal | None,
    portfolio_total_acquisition_cost: Decimal | None,
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
            recommended=False, notified=False, succeeded=False, category="data_insufficient",
            audit=audit,
        )

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
    )

    sell_outcome = sell_service.analyze(holding, now, snapshot=snapshot)
    if sell_outcome.recommendation is not None:
        recommendation_repo.save(sell_outcome.recommendation)
        outcome = notification_service.notify_recommendation_with_status(
            sell_outcome.recommendation, now
        )
        audit = HoldingEvaluationAudit(
            stock_code=holding.stock_code,
            evaluated_at=now,
            evaluation_status=(
                EvaluationStatus.DATA_QUALITY_BLOCKED
                if outcome.data_quality_blocked
                else EvaluationStatus.COMPLETED
            ),
            raw_sell_recommendation_type=sell_outcome.recommendation.raw_recommendation_type,
            raw_profit_recommendation_type=None,
            final_recommendation_type=sell_outcome.recommendation.recommendation_type,
            notification_status=outcome.status,
            notification_suppression_reason=None if outcome.sent else outcome.status.value,
            sell_signal_status="TRIGGERED",
            profit_taking_status="NOT_EVALUATED",
            fair_value_status="NOT_AVAILABLE",
            data_quality_status="BLOCKED" if outcome.data_quality_blocked else "OK",
            confidence=sell_outcome.recommendation.confidence,
            error_code=None,
        )
        return _HoldingResult(
            recommended=True,
            notified=outcome.sent,
            succeeded=True,
            category=summary_category(audit),
            audit=audit,
        )

    pt_outcome = profit_service.analyze(holding, now, snapshot=snapshot)
    if pt_outcome.recommendation is not None:
        recommendation_repo.save(pt_outcome.recommendation)
        outcome = notification_service.notify_recommendation_with_status(
            pt_outcome.recommendation, now
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
            notification_suppression_reason=None if outcome.sent else outcome.status.value,
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
        )
        return _HoldingResult(
            recommended=True,
            notified=outcome.sent,
            succeeded=True,
            category=summary_category(audit),
            audit=audit,
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


def _finish_batch_item(
    batch_id: str | None,
    category: str,
    stock_code: str,
    now: dt.datetime,
    notification_service: LineNotificationService,
) -> None:
    if batch_id is None:
        return
    needs_code = category in ("data_insufficient", "failed")
    progress = record_result(batch_id, category, stock_code=stock_code if needs_code else None)
    if progress is not None and progress.is_complete:
        notification_service.notify_batch_summary(
            _PROCESS_NAME,
            progress.total,
            progress.category_counts,
            now,
            data_insufficient_stock_codes=progress.data_insufficient_stock_codes,
            failed_stock_codes=progress.failed_stock_codes,
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
) -> dict[str, Any]:
    holding = PortfolioService().get_holding(stock_code)
    if holding is None:
        logger.warning("dispatched holding not found stock_code=%s", stock_code)
        _finish_batch_item(batch_id, "failed", stock_code, now, notification_service)
        return {"stock_code": stock_code, "recommended": False, "notified": False, "found": False}

    profit_service = ProfitTakingService(providers=providers, config=config)
    sell_service = SellSignalService(providers=providers, config=config)
    try:
        result = _analyze_one_holding(
            holding,
            now,
            providers,
            config,
            profit_service,
            sell_service,
            recommendation_repo,
            notification_service,
            rule_version_service,
            portfolio_total_market_value,
            portfolio_total_acquisition_cost,
        )
    except Exception:  # noqa: BLE001 - 1銘柄の想定外エラーで再帰呼び出し全体を落とさない
        logger.exception("holding analysis failed unexpectedly stock_code=%s", stock_code)
        _finish_batch_item(batch_id, "failed", stock_code, now, notification_service)
        return {"stock_code": stock_code, "recommended": False, "notified": False, "failed": True}

    logger.info("holding_evaluation_audit: %s", result.audit)
    _finish_batch_item(batch_id, result.category, stock_code, now, notification_service)
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
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    providers = build_real_provider_bundle(now, config)
    recommendation_repo = RecommendationRepository()
    notification_service = LineNotificationService(
        line_client=build_line_client_from_env(),
        notification_log_repository=NotificationLogRepository(),
        recommendation_repository=recommendation_repo,
        config=config,
    )
    rule_version_service = RuleVersionService()

    task = event.get("task")
    if task == "holding":
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
    total = len(holdings)
    batch_id = f"holdings-watchlist-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    start_batch(batch_id, total, now)

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
            },
        )

    logger.info(
        "holdings_watchlist_handler dispatched: holdings=%d batch_id=%s",
        len(holdings),
        batch_id,
    )
    return {"dispatched_holdings": len(holdings)}
