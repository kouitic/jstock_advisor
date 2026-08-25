"""買い候補分析Lambda(schedule.yaml daily_buy_candidates_analysis、平日08:00)。

CLIの`jstock analyze buy-candidates --source real --notify`と同じロジックを
EventBridge Scheduler経由で自動実行する薄いアダプタ。

【気になる銘柄と保有銘柄を統合したBUY候補パイプライン(2026-07)】
「気になる銘柄(ウォッチリスト)」と「保有銘柄」を銘柄コード単位で統合し
(両方に登録されている銘柄は1回だけ評価する)、共通の購入判断ロジック
(BuySignalService)をそのまま両方に適用する。保有銘柄については、
「保有しているから買う」を許さず、共通購入判断がBUY系判定を出した場合のみ
追加で買い増し固有リスク(銘柄集中・業種集中・売却判定との競合・保有データの
整合性)を確認する(domain/signals/add_on_risk.py)。統合ランキング・再送防止・
最大5件のLINE通知は気になる銘柄と保有銘柄で分離しない(保有銘柄であること
自体を優遇・冷遇しない)。

以前は保有銘柄の買いシグナル評価をholdings_watchlist_handler.py側でも
別経路(recommendation_type=WATCH_BUY、ランキングなしの個別即時通知)として
実施しており、同一銘柄が同日に二重通知されうる不具合があった。統合後は
その経路を廃止し、本ハンドラへ一本化している(要求仕様§16)。

銘柄単位のファンアウト(_fanout.py)を採用しており、通常のスケジュール起動では
対象銘柄一覧を取得して銘柄ごとに自分自身を非同期再帰呼び出しするだけで
即座に戻る。

【購入候補のみをランキング・通知】
「企業として投資候補になり得るか」と「現在の株価で実際に購入すべきか」を分離した
ため、監視継続・購入見送り・要確認・データ不足・対象外はLINE通知しない
(分析結果・監査ログへの記録はBuySignalService.analyze()側で全銘柄について既に
完了しており、本ハンドラもすべての評価対象についてunified_buy_candidate_
evaluation監査を追加で記録する)。各ワーカーはBuyAction判定が確定した時点で、
購入候補(STRONG_BUY/BUY/SMALL_ENTRY)のみをランキング候補としてバッチトラッカーへ
登録する(価格待ちは件数カウントのみ行い、送信対象のランキングには載せない)。
全銘柄の処理が完了した時点(最後のワーカーが検知)で、購入候補ランキング順に
1件ずつ以下の順序でゲートを評価し(要求仕様§2の11ステップ)、
条件を満たしたものを最大5件(気になる銘柄・保有銘柄の合計)に達するまで、
または全件評価し終えるまで繰り上げながら集める:
  1. データ品質  2. 保有銘柄固有ゲート(売却競合→保有データ整合性→
  ポートフォリオデータ信頼性→銘柄集中→業種集中)  3. 再送防止  4. 最大5件判定
ランキング上位であっても、いずれかのゲートで弾かれた場合はOUTSIDE_TOP_5には
ならず、本来のブロック理由(SECTOR_CONCENTRATION等)が監査へ記録される。
OUTSIDE_TOP_5は全ゲートを通過したうえで6位以下だった場合のみ付与される。

購入候補が1件も無い場合、config.notification.send_empty_summaryがfalseなら
バッチ完了サマリー自体を送信しない(要求仕様16節: 無理に候補を作らず、
何も無い日は通知しない)。

個別のデータ取得エラーは既定でLINEへ配信せず(config.notification.
buy_candidates.notify_data_errorsで制御)、CloudWatch警告ログとバッチサマリーの
data_insufficient件数にのみ記録する。全銘柄の処理が完了した時点で全体件数・
正常件数・異常件数のサマリーを1通だけ送信する(batch_tracker.pyのDynamoDB
原子カウンタで完了を検知する)。
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import uuid
from decimal import Decimal
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.buy_candidate_batch_pointer import (
    LatestBuyCandidateBatchPointer,
)
from jstock_advisor.domain.entities.buy_candidate_evaluation_record import (
    BuyCandidateEvaluationRecord,
    build_evaluation_id,
)
from jstock_advisor.domain.entities.buy_evaluation_target import BuyEvaluationTarget
from jstock_advisor.domain.entities.enums import (
    BUY_FAMILY_ACTIONS,
    AddOnEligibility,
    BuyAction,
    BuyIndustrySector,
    CandidateSource,
    DecisionType,
    EligibilityBlockCategory,
    ExecutionMode,
    NotificationContext,
    PortfolioValuationBasis,
    PurchaseCategory,
    RecommendationType,
    WatchTransitionType,
    WatchType,
    resolve_purchase_category,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.notification_eligibility import NotificationEligibility
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.jst import evaluation_date_jst
from jstock_advisor.domain.signals.add_on_risk import evaluate_add_on_eligibility
from jstock_advisor.infrastructure.aws.batch_tracker import (
    MAX_SECTOR_ENTRIES,
    MAX_SECTOR_ENTRY_BYTES,
    BatchProgress,
    record_result,
    start_batch,
)
from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.infrastructure.local_repository.buy_candidate_evaluation_record_repository import (  # noqa: E501
    BuyCandidateEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.latest_buy_candidate_batch_pointer_repository import (  # noqa: E501
    LatestBuyCandidateBatchPointerRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers._execution_mode import resolve_execution_context
from jstock_advisor.lambda_handlers._fanout import dispatch_async, resolve_function_name
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.buy_signal_service import RULE_VERSION_PLACEHOLDER, BuySignalService
from jstock_advisor.services.decision_snapshot_service import save_decision_snapshot_safely
from jstock_advisor.services.line_notification_service import (
    LineNotificationService,
    notification_priority_for_recommendation,
)
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.provider_factory import build_real_provider_bundle
from jstock_advisor.services.rule_version_service import RuleVersionService
from jstock_advisor.services.sell_signal_service import SellSignalService
from jstock_advisor.services.shareholder_benefit_registry_service import check_registry_health
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot
from jstock_advisor.services.trade_cooldown_service import TradeCooldownService
from jstock_advisor.services.watch_state_service import (
    WATCH_END_NOTIFIABLE_REASONS,
    WatchStateService,
)
from jstock_advisor.services.watchlist_service import WatchlistService

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_PROCESS_NAME = "買い候補分析"
# handler()は常に明示的にexecution_contextを渡す(NORMAL/VALIDATIONを問わず)。
# このデフォルトは_process_single_candidate/_finalize_batchを直接呼ぶ既存テスト
# コード(白箱テスト)向けの後方互換専用で、本番の呼び出し経路では使われない。
_DEFAULT_EXECUTION_CONTEXT = ExecutionContext.normal()
_RANKING_ENTRY_DELIMITER = "|"
_SECTOR_ENTRY_DELIMITER = "|"
_STOCK_CODE_PATTERN = re.compile(r"^[0-9]{4,5}$")

# 購入候補ランキングの第一ソートキー(BuyActionの強さ。数値が大きいほど優先)。
_ACTION_PRIORITY: dict[BuyAction, int] = {
    BuyAction.SMALL_ENTRY: 0,
    BuyAction.BUY: 1,
    BuyAction.STRONG_BUY: 2,
}


def _encode_buy_ranking_entry(recommendation: Recommendation) -> str:
    """購入候補ランキングキー(要求仕様15節): action_priority(BuyActionの強さ)
    → purchase_attractiveness_score → company_quality_score →
    現在値が標準買い価格をどれだけ下回るか、の降順。同点時は銘柄コード昇順で
    決定性を確保する(_finalize_batch側でタプルの最後の要素として比較される)。

    価格待ち(WATCH_FOR_PRICE/WATCH_BEFORE_EARNINGS)はLINE通知対象外のため
    ランキング登録自体を行わない。保有銘柄であること自体はソートキーに含めない
    (統合BUY候補パイプライン2026-07: 保有銘柄を優遇・冷遇しない)。
    """
    action_priority = _ACTION_PRIORITY.get(recommendation.buy_action, 0)  # type: ignore[arg-type]
    purchase_score = recommendation.purchase_attractiveness_score or 0.0
    quality_score = recommendation.company_quality_score or 0.0
    standard_price = (
        recommendation.standard_buy_price if recommendation.standard_buy_price is not None else None
    )
    discount_to_standard_pct = (
        float((standard_price - recommendation.price_at_recommendation) / standard_price * 100)
        if standard_price is not None and standard_price > 0
        else 0.0
    )
    return _RANKING_ENTRY_DELIMITER.join(
        [
            str(action_priority),
            str(purchase_score),
            str(quality_score),
            str(discount_to_standard_pct),
            recommendation.stock_code,
            recommendation.recommendation_id,
        ]
    )


def _decode_buy_ranking_entry(entry: str) -> tuple[tuple[float, ...], str, str]:
    action_priority, purchase_score, quality_score, discount_pct, stock_code, recommendation_id = (
        entry.split(_RANKING_ENTRY_DELIMITER)
    )
    sort_key = (
        float(action_priority),
        float(purchase_score),
        float(quality_score),
        float(discount_pct),
    )
    return sort_key, stock_code, recommendation_id


def _encode_near_buy_ranking_entry(recommendation: Recommendation) -> str:
    """NEAR BUY/WATCH_BEFORE_EARNINGS用のランキングキー(BUY候補裾野拡大機能2026-08、
    要求仕様: NEAR BUY日次上限の超過分選択は1st distance_pct昇順、2nd
    company_quality_score降順)。WATCH_BEFORE_EARNINGSは日次上限の対象外だが、
    同じランキング機構を共用するためここでもエンコードする。
    """
    distance_pct = (
        float(recommendation.required_decline_to_entry_pct)
        if recommendation.required_decline_to_entry_pct is not None
        else float("inf")
    )
    quality_score = recommendation.company_quality_score or 0.0
    return _RANKING_ENTRY_DELIMITER.join(
        [
            str(distance_pct),
            str(-quality_score),
            recommendation.stock_code,
            recommendation.recommendation_id,
        ]
    )


def _decode_near_buy_ranking_entry(entry: str) -> tuple[tuple[float, ...], str, str]:
    distance_pct, neg_quality_score, stock_code, recommendation_id = entry.split(
        _RANKING_ENTRY_DELIMITER
    )
    sort_key = (float(distance_pct), float(neg_quality_score))
    return sort_key, stock_code, recommendation_id


def _encode_sector_entry(sector: BuyIndustrySector, market_value: Decimal, stock_code: str) -> str:
    return _SECTOR_ENTRY_DELIMITER.join([sector.value, str(market_value), stock_code])


def _decode_sector_entry(entry: str) -> tuple[BuyIndustrySector, Decimal, str] | None:
    """不正なエントリはNoneを返す(呼び出し側でログ・無視すること)。"""
    parts = entry.split(_SECTOR_ENTRY_DELIMITER)
    if len(parts) != 3:
        return None
    sector_raw, value_raw, stock_code = parts
    if not _STOCK_CODE_PATTERN.match(stock_code):
        return None
    try:
        sector = BuyIndustrySector(sector_raw)
        market_value = Decimal(value_raw)
    except (ValueError, ArithmeticError):
        return None
    return sector, market_value, stock_code


def _holding_data_consistency(
    holding_quantity: int | None, average_acquisition_price: Decimal | None, trading_unit: int
) -> tuple[bool, bool]:
    """保有データの整合性を判定する(戻り値: (holding_data_inconsistent, is_odd_lot))。

    ハードブロック対象(holding_data_inconsistent=True)は株数・平均取得単価が
    存在しない・0以下の場合のみ。単元未満株(端株)はそれだけでは不整合扱いに
    しない(統合BUY候補パイプライン2026-07: 単元未満株・株式分割等で正当に
    発生しうるため。ブロックするかはconfig.add_on.block_add_on_on_odd_lotで
    別途制御する)。
    """
    if holding_quantity is None or holding_quantity <= 0:
        return True, False
    if average_acquisition_price is None or average_acquisition_price <= 0:
        return True, False
    is_odd_lot = trading_unit > 0 and holding_quantity % trading_unit != 0
    return False, is_odd_lot


def _build_unified_targets(
    config: AppConfig,
    now: dt.datetime,
    execution_context: ExecutionContext = _DEFAULT_EXECUTION_CONTEXT,
) -> list[BuyEvaluationTarget]:
    """気になる銘柄と保有銘柄を銘柄コード単位で統合する(要求仕様§2)。

    事前ガード(要求仕様§8): 保有銘柄(ユニーク銘柄コード数)がMAX_SECTOR_ENTRIES
    を超える場合、sector_entriesの書き込み上限に達する恐れがあるため保有銘柄側は
    評価対象へ含めない(監査へ記録したうえで、気になる銘柄側の評価は継続する)。

    M3.1(複数owner対応): 同一stock_codeを複数ownerが保有していても、BUY候補
    評価はowner別に分割せず銘柄コード単位で1回だけ行う(要求仕様§2)。
    holding_quantity/average_acquisition_priceは全owner分を集約した値とする
    (単純なowner間平均ではなく、購入金額合計÷株数合計の加重平均)。
    """
    watchlist_names: dict[str, str | None] = {}
    if config.notification.include_watchlist:
        for item in WatchlistService().list_items():
            watchlist_names[item.stock_code] = item.stock_name

    holdings_by_code: dict[str, list[Holding]] = {}
    if config.notification.include_holdings:
        holdings = PortfolioService().list_holdings()
        unique_stock_codes = {h.stock_code for h in holdings}
        if len(unique_stock_codes) > MAX_SECTOR_ENTRIES:
            logger.error(
                "buy_candidates_handler: holding_count=%d exceeds MAX_SECTOR_ENTRIES=%d; "
                "skipping holding-side evaluation for this run",
                len(unique_stock_codes),
                MAX_SECTOR_ENTRIES,
            )
            AuditService(execution_context=execution_context).record(
                decision_type="unified_buy_candidate_batch_aborted",
                stock_code=None,
                input_values={"holding_count": len(unique_stock_codes)},
                calculation_formulas={},
                output_values={"reason": "SECTOR_ENTRIES_LIMIT_EXCEEDED"},
                data_sources=[],
                rule_version=RULE_VERSION_PLACEHOLDER,
                timestamp=now,
            )
        else:
            for holding in holdings:
                holdings_by_code.setdefault(holding.stock_code, []).append(holding)

    codes = sorted(set(watchlist_names) | set(holdings_by_code))
    targets: list[BuyEvaluationTarget] = []
    for code in codes:
        in_watchlist = code in watchlist_names
        in_holding = code in holdings_by_code
        if in_watchlist and in_holding:
            source = CandidateSource.BOTH
        elif in_holding:
            source = CandidateSource.HOLDING
        else:
            source = CandidateSource.WATCHLIST
        holdings_for_code = holdings_by_code.get(code, [])
        total_shares = sum(h.shares for h in holdings_for_code)
        total_acquisition_amount = sum(
            (h.total_purchase_amount for h in holdings_for_code), Decimal("0")
        )
        average_acquisition_price = (
            total_acquisition_amount / total_shares if total_shares > 0 else None
        )
        stock_name = watchlist_names.get(code) or (
            holdings_for_code[0].stock_name if holdings_for_code else None
        )
        targets.append(
            BuyEvaluationTarget(
                stock_code=code,
                stock_name=stock_name,
                source=source,
                holding_quantity=total_shares if holdings_for_code else None,
                average_acquisition_price=average_acquisition_price,
            )
        )
    return targets


def _record_evaluation_audit(
    audit_service: AuditService,
    rule_version: str,
    now: dt.datetime,
    stock_code: str,
    source: CandidateSource,
    holding_quantity: int | None,
    average_acquisition_price: Decimal | None,
    current_market_value: Decimal | None,
    unrealized_profit_loss: Decimal | None,
    unrealized_profit_loss_pct: Decimal | None,
    base_buy_action: BuyAction,
    final_buy_action: BuyAction,
    conflicting_holding_action: RecommendationType | None,
    holding_data_inconsistent: bool,
    holding_owner_count: int | None = None,
    holding_ids: tuple[str, ...] | None = None,
    exclusion_reasons: list[str] | None = None,
) -> None:
    """全評価対象銘柄(BUY系以外も含む)について記録する監査(要求仕様§4・§14)。

    M3.1: holding_quantity/average_acquisition_priceは(保有銘柄の場合)常に
    owner横断の集約値であり、単一ownerの値ではない。holding_owner_count/
    holding_idsを渡すことで、何名分・どのholding_id分の集約かを監査から
    追跡できるようにする(owner単位の別Auditは作らない)。
    """
    audit_service.record(
        decision_type="unified_buy_candidate_evaluation",
        stock_code=stock_code,
        input_values={
            "candidate_source": source.value,
            "holding_quantity": holding_quantity,
            "average_acquisition_price": (
                str(average_acquisition_price) if average_acquisition_price is not None else None
            ),
            "holding_owner_count": holding_owner_count,
            "holding_ids": list(holding_ids) if holding_ids is not None else None,
            "exclusion_reasons": exclusion_reasons,
        },
        calculation_formulas={},
        output_values={
            "current_market_value": (
                str(current_market_value) if current_market_value is not None else None
            ),
            "unrealized_profit_loss": (
                str(unrealized_profit_loss) if unrealized_profit_loss is not None else None
            ),
            "unrealized_profit_loss_pct": (
                str(unrealized_profit_loss_pct) if unrealized_profit_loss_pct is not None else None
            ),
            "base_buy_action": base_buy_action.value,
            "final_buy_action": final_buy_action.value,
            "conflicting_holding_action": (
                conflicting_holding_action.value if conflicting_holding_action is not None else None
            ),
            "holding_data_inconsistent": holding_data_inconsistent,
        },
        data_sources=[],
        rule_version=rule_version,
        timestamp=now,
    )


def _process_single_candidate(
    stock_code: str,
    source: CandidateSource,
    holding_quantity: int | None,
    average_acquisition_price: Decimal | None,
    batch_id: str | None,
    now: dt.datetime,
    providers: ProviderBundle,
    config: AppConfig,
    calendar: BusinessCalendar,
    recommendation_repo: RecommendationRepository,
    notification_service: LineNotificationService,
    execution_context: ExecutionContext = _DEFAULT_EXECUTION_CONTEXT,
    evaluation_record_repo: BuyCandidateEvaluationRecordRepository | None = None,
    latest_batch_pointer_repo: LatestBuyCandidateBatchPointerRepository | None = None,
) -> dict[str, Any]:
    service = BuySignalService(
        providers=providers,
        config=config,
        business_calendar=calendar,
        execution_context=execution_context,
    )
    audit_service = AuditService(execution_context=execution_context)
    rule_version = RuleVersionService().get_active_version_or(RULE_VERSION_PLACEHOLDER)
    category = "failed"
    ranking_entry: str | None = None
    near_buy_ranking_entry: str | None = None
    watch_end_ranking_entry: str | None = None
    sector_entry: str | None = None
    validation_recommendation_id: str | None = None
    # 買い候補サマリー表示改修(2026-08): 将来のLINE詳細理由照会機能に向けて、
    # try/exceptのどの経路を通っても最終的な購入判定分類・recommendation_idを
    # 記録できるよう、既定値(処理失敗扱い)をtry開始前に用意しておく。
    record_purchase_category = PurchaseCategory.FAILED
    record_final_buy_action: BuyAction | None = None
    record_raw_buy_action: BuyAction | None = None
    record_recommendation_id: str | None = None
    record_exclusion_reasons: list[str] | None = None
    try:
        # --- 統合BUY候補パイプライン(2026-07)。購入判定と、保有銘柄の場合の
        # 売却・利確判定(後段)とで同一のスナップショット(現在値・財務データ)を
        # 使うことで、同一銘柄について矛盾した判定が同時に成立しないようにする ---
        snapshot, snapshot_error = build_stock_snapshot(providers, stock_code, now, config)
        outcome = service.analyze(stock_code, now, snapshot=snapshot)
        if outcome.data_error:
            # --- BUYパイプライン第3次修正(2026-07)。個別のデータ取得エラーは
            # 購入候補通知パイプラインからLINE個別送信しない(既定)。CloudWatch
            # 警告ログとバッチ完了サマリーのdata_insufficient件数への集計のみと
            # する。監査ログへの記録はBuySignalService.analyze()側
            # (snapshot is Noneの分岐)で既に完了している。運用上どうしても
            # 個別のLINE通知が必要な場合のみ、config.notification.buy_candidates.
            # notify_data_errorsをtrueにすることで有効化できる ---
            item = WatchlistService().get_item(stock_code)
            if config.notification.buy_candidates.notify_data_errors:
                notification_service.notify_data_error(
                    stock_code,
                    outcome.data_error,
                    now,
                    stock_name=item.stock_name if item else None,
                )
            else:
                name_part = f" {item.stock_name}" if item and item.stock_name else ""
                logger.warning(
                    "buy_candidate_data_error stock_code=%s%s message=%s",
                    stock_code,
                    name_part,
                    outcome.data_error,
                )
            category = "data_insufficient"
            result = {"stock_code": stock_code, "recommended": False, "notified": False}
            record_purchase_category = PurchaseCategory.DATA_INSUFFICIENT
            record_final_buy_action = BuyAction.DATA_INSUFFICIENT
            record_raw_buy_action = BuyAction.DATA_INSUFFICIENT
            _record_evaluation_audit(
                audit_service,
                rule_version,
                now,
                stock_code,
                source,
                holding_quantity,
                average_acquisition_price,
                current_market_value=None,
                unrealized_profit_loss=None,
                unrealized_profit_loss_pct=None,
                base_buy_action=BuyAction.DATA_INSUFFICIENT,
                final_buy_action=BuyAction.DATA_INSUFFICIENT,
                conflicting_holding_action=None,
                holding_data_inconsistent=False,
            )
        elif outcome.buy_action == BuyAction.EXCLUDED or outcome.recommendation is None:
            # 投資対象スクリーニングで除外(第1段階)。screening_passed=Falseの場合、
            # recommendationは常にNoneとなる想定(buy_signal_service.py参照)。
            # スナップショット自体は取得済みのため、保有銘柄の場合は時価総額のみ
            # sector_entriesへ報告できる(業種分類はスクリーニング除外時点では
            # 未計算のため報告できない。ポートフォリオ集計の既知の制約)。
            category = "hold"
            result = {"stock_code": stock_code, "recommended": False, "notified": False}
            record_purchase_category = PurchaseCategory.EXCLUDED
            record_final_buy_action = BuyAction.EXCLUDED
            record_raw_buy_action = BuyAction.EXCLUDED
            record_exclusion_reasons = outcome.exclusion_reasons or None
            _record_evaluation_audit(
                audit_service,
                rule_version,
                now,
                stock_code,
                source,
                holding_quantity,
                average_acquisition_price,
                current_market_value=None,
                unrealized_profit_loss=None,
                unrealized_profit_loss_pct=None,
                base_buy_action=BuyAction.EXCLUDED,
                final_buy_action=BuyAction.EXCLUDED,
                conflicting_holding_action=None,
                holding_data_inconsistent=False,
                exclusion_reasons=record_exclusion_reasons,
            )
        else:
            recommendation = outcome.recommendation
            assert outcome.buy_action is not None  # noqa: S101 - recommendationがあれば必ず設定される
            base_buy_action = outcome.buy_action
            final_recommendation = recommendation
            conflicting_holding_action: RecommendationType | None = None
            holding_data_inconsistent = False
            current_market_value: Decimal | None = None
            unrealized_profit_loss: Decimal | None = None
            unrealized_profit_loss_pct: Decimal | None = None
            holding_owner_count: int | None = None
            holding_ids: tuple[str, ...] | None = None

            if source in (CandidateSource.HOLDING, CandidateSource.BOTH):
                current_price = recommendation.price_at_recommendation
                trading_unit = config.profit_taking.trading_unit.default_trading_unit
                holding_data_inconsistent, holding_is_odd_lot = _holding_data_consistency(
                    holding_quantity, average_acquisition_price, trading_unit
                )
                # holding_quantity/average_acquisition_priceは_build_unified_targets()側で
                # 既に全owner集約済みの値(M3.1)。current_market_valueもowner合算の
                # 総株数に対して算出されるため、自動的に全owner合算値になる。
                if holding_quantity is not None and holding_quantity > 0:
                    current_market_value = current_price * holding_quantity
                    if average_acquisition_price is not None and average_acquisition_price > 0:
                        total_acquisition = average_acquisition_price * holding_quantity
                        unrealized_profit_loss = current_market_value - total_acquisition
                        unrealized_profit_loss_pct = (
                            unrealized_profit_loss / total_acquisition * 100
                        )

                # M3.1(複数owner対応): 同一stock_codeを保有する全owner分のHoldingを
                # 集約して扱う(監査記録用にholding_owner_count/holding_idsも保持する)。
                holdings_for_stock = PortfolioService().list_holdings_by_stock(stock_code)
                holding_owner_count = len(holdings_for_stock)
                holding_ids = tuple(h.holding_id for h in holdings_for_stock)

                # --- 買い増し固有リスク: 売却・利確判定との競合(要求仕様§6)。
                # base_buy_actionがBUY系、かつ保有データに致命的な不整合が無い
                # 場合のみ確認する(不整合な保有データでSell/ProfitTakingを
                # 実行しても無意味なため)。共通購入判断と同一snapshotを渡すことで
                # 現在値・財務データの矛盾を防ぐ。
                #
                # M3.1(複数owner対応): 全owner分のHoldingを独立にSell/ProfitTaking
                # 評価する(例: 本人#8306→SELL、子供#8306→HOLD)。1owner分でも
                # 売却/利確系の競合Recommendationがあれば競合ありとして扱い、
                # BUY/買い増しを抑止する。複数ownerから異なる種別の競合が同時に
                # 出た場合は、Cross Pipeline Priorityと同じ優先度表
                # (notification_priority_for_recommendation、CRITICAL_RISK>
                # PROMOTED_TO_BUY>SELL/PARTIAL_SELL>BUY>ATTENTION>その他)に従って
                # 最も強いものをconflicting_holding_actionとして記録する
                # (新たな優先順位は新設しない) ---
                if base_buy_action in BUY_FAMILY_ACTIONS and not holding_data_inconsistent:
                    sell_service = SellSignalService(
                        providers=providers,
                        config=config,
                        business_calendar=calendar,
                        execution_context=execution_context,
                    )
                    profit_service = ProfitTakingService(
                        providers=providers,
                        config=config,
                        business_calendar=calendar,
                        execution_context=execution_context,
                    )
                    best_conflict: Recommendation | None = None
                    best_conflict_priority = -1
                    for owner_holding in holdings_for_stock:
                        sell_outcome = sell_service.analyze(owner_holding, now, snapshot=snapshot)
                        holding_conflict = sell_outcome.recommendation
                        if holding_conflict is None:
                            profit_outcome = profit_service.analyze(
                                owner_holding, now, snapshot=snapshot
                            )
                            holding_conflict = profit_outcome.recommendation
                        if holding_conflict is None:
                            continue
                        candidate_priority = notification_priority_for_recommendation(
                            holding_conflict
                        )
                        if candidate_priority > best_conflict_priority:
                            best_conflict_priority = candidate_priority
                            best_conflict = holding_conflict
                    if best_conflict is not None:
                        conflicting_holding_action = best_conflict.recommendation_type

                final_recommendation = recommendation.model_copy(
                    update={
                        "candidate_source": source,
                        "holding_quantity": holding_quantity,
                        "average_acquisition_price": average_acquisition_price,
                        "current_market_value": current_market_value,
                        "unrealized_profit_loss": unrealized_profit_loss,
                        "unrealized_profit_loss_pct": unrealized_profit_loss_pct,
                        "base_buy_action": base_buy_action,
                        "conflicting_holding_action": conflicting_holding_action,
                    }
                )

                # 業種集中度用のsector_entry報告(全保有銘柄が対象。BUY_FAMILY以外も
                # 含む。既知の制約: buy_industry_sectorはスクリーニング通過後にしか
                # 計算されないため、この分岐に到達した=screening_passed=True の
                # 保有銘柄のみが対象になる)。
                buy_industry_sector = recommendation.buy_industry_sector
                if current_market_value is not None and buy_industry_sector is not None:
                    candidate_entry = _encode_sector_entry(
                        buy_industry_sector, current_market_value, stock_code
                    )
                    if len(candidate_entry.encode("utf-8")) <= MAX_SECTOR_ENTRY_BYTES:
                        sector_entry = candidate_entry
                    else:
                        logger.error(
                            "buy_candidates_handler: sector_entry exceeds "
                            "MAX_SECTOR_ENTRY_BYTES=%d stock_code=%s",
                            MAX_SECTOR_ENTRY_BYTES,
                            stock_code,
                        )
            else:
                final_recommendation = recommendation.model_copy(
                    update={
                        "candidate_source": source,
                        "base_buy_action": base_buy_action,
                        "add_on_eligibility": AddOnEligibility.NOT_APPLICABLE,
                    }
                )

            recommendation_repo.save(final_recommendation)
            if execution_context.is_validation:
                # 通知検証モード機能(2026-08追加): _finalize_batchが正常完了後に
                # 検証用テーブルから削除するため、このバッチで保存した
                # recommendation_idをrecord_result経由で報告する(4.2節参照)。
                validation_recommendation_id = final_recommendation.recommendation_id
            # 判定精度向上機能Phase A: DecisionSnapshotを記録する(スコア項目は
            # Phase Bまで全てNone)。失敗しても既存の通知・戻り値には一切影響しない。
            # 通知検証モード機能(2026-08追加): VALIDATIONでは通常運用の判定履歴を
            # 汚さないため保存自体をスキップする。
            if not execution_context.is_validation:
                save_decision_snapshot_safely(
                    DecisionSnapshotRepository(), final_recommendation, DecisionType.BUY, logger
                )

            # --- WATCH終了通知(コードレビュー対応2026-08、§3)。
            # PROMOTED_TO_BUY(BUY到達)はここでは対象にしない(§2の「BUY到達」
            # 通知へ統合済み、format_notification_text側で「到達」ラベル・
            # 「N日監視後」を表示する)。TRADE_EVENTによる終了は
            # WatchStateService.end_for_trade_events()経由のためwatch_
            # transition_typeが設定されず、ここには現れない。
            # 防御的対策(コードレビュー対応2026-08、指摘1): WatchStateService側で
            # PROMOTED_TO_BUYをstale判定より優先する修正を行ったが、万一
            # watch_transition_type=ENDEDのままbuy_actionがBUY家族になっている
            # 状態が発生しても、BUY到達通知と監視終了通知の二重送信を防ぐため
            # ここでも同じ条件を明示的に確認する。 ---
            if (
                final_recommendation.watch_transition_type == WatchTransitionType.ENDED.value
                and final_recommendation.watch_end_reason in WATCH_END_NOTIFIABLE_REASONS
                and final_recommendation.buy_action not in BUY_FAMILY_ACTIONS
                and config.notification.watch_end_notification.enabled
                and (final_recommendation.watch_previous_consecutive_business_days or 0)
                >= config.notification.watch_end_notification.min_consecutive_business_days
            ):
                watch_end_ranking_entry = final_recommendation.recommendation_id

            record_final_buy_action = final_recommendation.buy_action
            record_raw_buy_action = base_buy_action
            record_recommendation_id = final_recommendation.recommendation_id

            if final_recommendation.buy_action == BuyAction.MANUAL_REVIEW:
                category = "review"
                record_purchase_category = PurchaseCategory.MANUAL_REVIEW
                result = {"stock_code": stock_code, "recommended": True, "notified": False}
            elif outcome.ranking_group == "buy_candidate":
                # 実際の送信可否判定は行わず、ランキング候補として登録するだけに
                # 留める(全銘柄処理完了後、購入候補ランキング順に評価・送信する)。
                category = "candidate_not_ranked"
                record_purchase_category = PurchaseCategory.BUY_CANDIDATE
                ranking_entry = _encode_buy_ranking_entry(final_recommendation)
                result = {"stock_code": stock_code, "recommended": True, "notified": False}
            elif outcome.ranking_group == "watch_price":
                # 買い候補サマリー表示改修(2026-08): 表示上「買い間近」(NEAR_BUY)と
                # 「買い待ち」(それ以外のWATCH_FOR_PRICE・WATCH_BEFORE_EARNINGS)を
                # 分離する。NEAR BUY/WATCH_BEFORE_EARNINGS向けの日次ランキング
                # エントリ生成ロジック自体は変更しない(既存どおり)。
                is_near_buy = (
                    final_recommendation.buy_action == BuyAction.WATCH_FOR_PRICE
                    and final_recommendation.watch_type is not None
                )
                category = "near_buy" if is_near_buy else "watch_wait"
                record_purchase_category = resolve_purchase_category(
                    final_recommendation.buy_action, final_recommendation.watch_type
                ) or PurchaseCategory.WATCH_FOR_PRICE
                if (
                    final_recommendation.watch_type is not None
                    or final_recommendation.buy_action == BuyAction.WATCH_BEFORE_EARNINGS
                ):
                    near_buy_ranking_entry = _encode_near_buy_ranking_entry(final_recommendation)
                result = {"stock_code": stock_code, "recommended": True, "notified": False}
            else:
                category = "hold"
                record_purchase_category = PurchaseCategory.NOT_ATTRACTIVE
                result = {"stock_code": stock_code, "recommended": True, "notified": False}

            _record_evaluation_audit(
                audit_service,
                rule_version,
                now,
                stock_code,
                source,
                holding_quantity,
                average_acquisition_price,
                current_market_value,
                unrealized_profit_loss,
                unrealized_profit_loss_pct,
                base_buy_action=base_buy_action,
                final_buy_action=final_recommendation.buy_action or base_buy_action,
                conflicting_holding_action=conflicting_holding_action,
                holding_data_inconsistent=holding_data_inconsistent,
                holding_owner_count=holding_owner_count,
                holding_ids=holding_ids,
            )
    except Exception:  # noqa: BLE001 - 1銘柄の想定外エラーで再帰呼び出し全体を落とさない
        logger.exception("buy candidate analysis failed unexpectedly stock_code=%s", stock_code)
        result = {"stock_code": stock_code, "recommended": False, "notified": False, "failed": True}

    if batch_id is not None:
        evaluation_record_saved = _save_evaluation_record_safely(
            evaluation_record_repo,
            batch_id,
            stock_code,
            now,
            rule_version,
            source,
            record_purchase_category,
            record_final_buy_action,
            record_raw_buy_action,
            record_recommendation_id,
            record_exclusion_reasons,
        )
        needs_code = category in ("data_insufficient", "failed")
        stock_code_for_category = stock_code if needs_code else None
        progress = record_result(
            batch_id,
            category,
            stock_code=stock_code_for_category,
            ranking_entry=ranking_entry,
            sector_entry=sector_entry,
            validation_recommendation_id=validation_recommendation_id,
            near_buy_ranking_entry=near_buy_ranking_entry,
            watch_end_ranking_entry=watch_end_ranking_entry,
            evaluation_record_saved_stock_code=(
                stock_code if evaluation_record_saved else None
            ),
        )
        if progress is not None and progress.is_complete:
            _finalize_batch(
                progress,
                batch_id,
                config,
                now,
                recommendation_repo,
                notification_service,
                execution_context,
                evaluation_record_repo,
                latest_batch_pointer_repo,
            )
    return result


def _save_evaluation_record_safely(
    evaluation_record_repo: BuyCandidateEvaluationRecordRepository | None,
    batch_id: str,
    stock_code: str,
    now: dt.datetime,
    rule_version: str,
    source: CandidateSource,
    purchase_category: PurchaseCategory,
    final_buy_action: BuyAction | None,
    raw_buy_action: BuyAction | None,
    recommendation_id: str | None,
    exclusion_reasons: list[str] | None = None,
) -> bool:
    """BuyCandidateEvaluationRecordの構築・保存失敗が既存の判定・通知フローを
    絶対にブロックしないためのラッパー(save_decision_snapshot_safely()と同じ
    設計方針)。将来のLINE詳細理由照会機能に向けた参照用の副次的な記録であり、
    失敗してもWARNINGログのみに留め、呼び出し元へ伝播させない。

    戻り値は保存に成功したかどうか(LINE UI第二弾「対象確認」機能2026-08で
    追加)。呼び出し元はこの結果をrecord_result()のevaluation_record_saved_
    stock_code引数へ渡し、latest batch pointerの更新条件(全対象銘柄分の
    保存成功)の判定に使う。
    """
    if evaluation_record_repo is None:
        return False
    try:
        evaluation_record_repo.upsert(
            BuyCandidateEvaluationRecord(
                evaluation_id=build_evaluation_id(batch_id, stock_code),
                batch_id=batch_id,
                stock_code=stock_code,
                evaluated_at=now,
                rule_version=rule_version,
                candidate_source=source,
                purchase_category=purchase_category,
                final_buy_action=final_buy_action,
                raw_buy_action=raw_buy_action,
                recommendation_id=recommendation_id,
                exclusion_reasons=(
                    tuple(exclusion_reasons) if exclusion_reasons is not None else None
                ),
            )
        )
        return True
    except Exception:  # noqa: BLE001 - 参照用の副次記録の失敗で本処理を止めない
        logger.warning(
            "buy_candidates_handler: failed to save BuyCandidateEvaluationRecord "
            "stock_code=%s batch_id=%s",
            stock_code,
            batch_id,
            exc_info=True,
        )
        return False


def _aggregate_sector_entries(
    sector_entries: list[str], holding_count: int
) -> tuple[dict[BuyIndustrySector, Decimal], Decimal | None, PortfolioValuationBasis, float]:
    """sector_entriesを銘柄コード単位で集計し、業種別・全体の時価総額を導出する
    (要求仕様§5後半・§7・§8)。

    保有銘柄全員分のエントリが揃い、かつ内容競合(同一銘柄で異なる値)が無い
    場合のみPortfolioValuationBasis.MARKET_VALUEとする。1件でも欠落・競合が
    あればUNAVAILABLEとし、時価ベースの比率を信頼できないものとして扱う
    (時価と取得金額を混在させない)。DynamoDB String Setは順序保証が無いため、
    「最後に見つかった値を採用」はしない。
    """
    if holding_count == 0:
        return {}, None, PortfolioValuationBasis.UNAVAILABLE, 0.0

    if len(sector_entries) > MAX_SECTOR_ENTRIES:
        logger.error(
            "buy_candidates_handler: sector_entries count=%d exceeds MAX_SECTOR_ENTRIES=%d",
            len(sector_entries),
            MAX_SECTOR_ENTRIES,
        )
        return {}, None, PortfolioValuationBasis.UNAVAILABLE, 0.0

    by_stock: dict[str, tuple[BuyIndustrySector, Decimal]] = {}
    conflicting_codes: set[str] = set()
    for raw_entry in sector_entries:
        parsed = _decode_sector_entry(raw_entry)
        if parsed is None:
            logger.warning("buy_candidates_handler: invalid sector_entry=%r ignored", raw_entry)
            continue
        sector, market_value, stock_code = parsed
        existing = by_stock.get(stock_code)
        if existing is not None and existing != (sector, market_value):
            conflicting_codes.add(stock_code)
            continue
        by_stock[stock_code] = (sector, market_value)

    coverage_ratio = len(by_stock) / holding_count if holding_count > 0 else 0.0

    if conflicting_codes:
        logger.error(
            "buy_candidates_handler: conflicting sector_entry values for stock_codes=%s",
            sorted(conflicting_codes),
        )
        return {}, None, PortfolioValuationBasis.UNAVAILABLE, coverage_ratio

    if len(by_stock) < holding_count:
        logger.warning(
            "buy_candidates_handler: sector_entries coverage incomplete (%d/%d holdings)",
            len(by_stock),
            holding_count,
        )
        return {}, None, PortfolioValuationBasis.UNAVAILABLE, coverage_ratio

    sector_totals: dict[BuyIndustrySector, Decimal] = {}
    portfolio_total = Decimal("0")
    for sector, market_value in by_stock.values():
        sector_totals[sector] = sector_totals.get(sector, Decimal("0")) + market_value
        portfolio_total += market_value

    return sector_totals, portfolio_total, PortfolioValuationBasis.MARKET_VALUE, coverage_ratio


def _record_notification_outcome_audit(
    audit_service: AuditService,
    rule_version: str,
    now: dt.datetime,
    recommendation: Recommendation,
    unified_rank: int | None,
    notification_rank: int | None,
    notification_status: str,
    eligibility: NotificationEligibility,
    basis: PortfolioValuationBasis,
    portfolio_total_market_value: Decimal | None,
    coverage_ratio: float,
) -> None:
    """ランキングに登録された候補(BUY系)について記録する監査(要求仕様§4・§10・§14)。

    unified_rank=Noneは、順位付けを行わない一過性の通知(WATCH終了通知等、
    コードレビュー対応2026-08)を記録する場合に使う。
    """
    reliable = basis == PortfolioValuationBasis.MARKET_VALUE
    block_category = eligibility.block_category.value if eligibility.block_category else None
    portfolio_total_str = (
        str(portfolio_total_market_value) if portfolio_total_market_value is not None else None
    )
    audit_service.record(
        decision_type="unified_buy_candidate_notification_outcome",
        stock_code=recommendation.stock_code,
        input_values={"recommendation_id": recommendation.recommendation_id},
        calculation_formulas={},
        output_values={
            "unified_rank": unified_rank,
            "notification_rank": notification_rank,
            "notification_status": notification_status,
            "block_category": block_category,
            "block_reason": eligibility.block_reason,
            "portfolio_valuation_basis": basis.value,
            "portfolio_total_market_value": portfolio_total_str,
            "portfolio_value_coverage_ratio": coverage_ratio,
            "position_ratio_reliability": reliable,
            "sector_ratio_reliability": reliable,
        },
        data_sources=[],
        rule_version=rule_version,
        timestamp=now,
    )


def _update_evaluation_record_outcome_safely(
    evaluation_record_repo: BuyCandidateEvaluationRecordRepository | None,
    batch_id: str,
    stock_code: str,
    unified_rank: int | None,
    notification_rank: int | None,
    eligible: bool,
    block_category: str | None,
    block_reason: str | None,
    add_on_block_reasons: tuple[str, ...],
    send_outcome: str | None,
) -> None:
    """買い候補ランキングループの各finalize判定結果を、判定時点(worker側)で
    既に作成済みのBuyCandidateEvaluationRecordへ追記する(save_decision_
    snapshot_safely()と同じ「失敗しても本処理を止めない」設計方針)。
    """
    if evaluation_record_repo is None:
        return
    try:
        evaluation_id = build_evaluation_id(batch_id, stock_code)
        existing = evaluation_record_repo.get(evaluation_id)
        if existing is None:
            logger.warning(
                "buy_candidates_handler: BuyCandidateEvaluationRecord not found at finalize "
                "stock_code=%s batch_id=%s",
                stock_code,
                batch_id,
            )
            return
        evaluation_record_repo.upsert(
            existing.model_copy(
                update={
                    "unified_rank": unified_rank,
                    "notification_rank": notification_rank,
                    "notification_eligible": eligible,
                    "notification_block_category": block_category,
                    "notification_block_reason": block_reason,
                    "add_on_block_reasons": add_on_block_reasons,
                    "send_outcome": send_outcome,
                }
            )
        )
    except Exception:  # noqa: BLE001 - 参照用の副次記録の失敗で本処理を止めない
        logger.warning(
            "buy_candidates_handler: failed to update BuyCandidateEvaluationRecord "
            "stock_code=%s batch_id=%s",
            stock_code,
            batch_id,
            exc_info=True,
        )


def _finalize_batch(
    progress: BatchProgress,
    batch_id: str,
    config: AppConfig,
    now: dt.datetime,
    recommendation_repo: RecommendationRepository,
    notification_service: LineNotificationService,
    execution_context: ExecutionContext = _DEFAULT_EXECUTION_CONTEXT,
    evaluation_record_repo: BuyCandidateEvaluationRecordRepository | None = None,
    latest_batch_pointer_repo: LatestBuyCandidateBatchPointerRepository | None = None,
) -> None:
    """全銘柄の処理完了を検知したワーカーが1回だけ呼ぶ。購入候補ランキング順に
    以下の固定順序でゲートを評価する(breakしない。全件をループし尽くす):
      1. データ品質  2. 保有銘柄固有ゲート(売却競合→保有データ整合性→
      ポートフォリオデータ信頼性→銘柄集中→業種集中)  3. 再送防止  4. 最大5件判定
    最大5件に到達していても他のゲートを先に評価し、真に全ゲートを通過した
    6位以下だけをOUTSIDE_TOP_5として扱う(統合BUY候補パイプライン2026-07)。
    """
    max_notifications = config.notification.buy_candidate_max_notifications_per_run
    audit_service = AuditService(execution_context=execution_context)
    rule_version = RuleVersionService().get_active_version_or(RULE_VERSION_PLACEHOLDER)

    buy_entries: list[tuple[tuple[float, ...], str, str]] = [
        _decode_buy_ranking_entry(entry) for entry in progress.ranking_entries
    ]
    # 降順ソート。同点時は銘柄コード昇順で決定性を確保する(要求仕様15節)。
    buy_entries.sort(key=lambda item: (tuple(-v for v in item[0]), item[1]))

    sector_totals, portfolio_total, basis, coverage_ratio = _aggregate_sector_entries(
        progress.sector_entries, progress.holding_count
    )

    data_quality_blocked_count = 0
    trade_cooldown_blocked_count = 0
    cross_pipeline_blocked_count = 0
    addon_blocked_count = 0
    resend_suppressed_count = 0
    outside_top5_count = 0
    record_not_found_count = 0
    eligible_winners: list[tuple[int, Recommendation]] = []

    for unified_rank, (_sort_key, stock_code, recommendation_id) in enumerate(buy_entries, start=1):
        recommendation = recommendation_repo.get(recommendation_id)
        if recommendation is None:
            logger.warning(
                "buy_candidates_handler: recommendation not found for ranking winner "
                "stock_code=%s recommendation_id=%s",
                stock_code,
                recommendation_id,
            )
            record_not_found_count += 1
            _update_evaluation_record_outcome_safely(
                evaluation_record_repo, batch_id, stock_code, unified_rank, None,
                False, "RECORD_NOT_FOUND", "RECORD_NOT_FOUND", (), None,
            )
            continue

        dq = notification_service.check_data_quality_eligibility(
            recommendation, now, context=NotificationContext.BUY_CANDIDATE_BATCH
        )
        if not dq.eligible:
            data_quality_blocked_count += 1
            _record_notification_outcome_audit(
                audit_service, rule_version, now, recommendation, unified_rank, None,
                "NOT_REQUIRED", dq, basis, portfolio_total, coverage_ratio,
            )
            _update_evaluation_record_outcome_safely(
                evaluation_record_repo, batch_id, stock_code, unified_rank, None,
                False,
                dq.block_category.value
                if dq.block_category
                else EligibilityBlockCategory.DATA_QUALITY.value,
                dq.block_reason, (), None,
            )
            continue

        # コードレビュー対応(2026-08): このBUYランキングループには売買
        # クールダウン判定が欠落していた(NEAR BUYループのみcheck_trade_
        # cooldown_eligibility()を呼んでいた)。買ったばかりの銘柄へ再度
        # BUY通知が送られてしまう不備のため、あわせて修正する。
        buy_cooldown = notification_service.check_trade_cooldown_eligibility(recommendation, now)
        if not buy_cooldown.eligible:
            trade_cooldown_blocked_count += 1
            _record_notification_outcome_audit(
                audit_service, rule_version, now, recommendation, unified_rank, None,
                buy_cooldown.block_reason or "NOT_REQUIRED", buy_cooldown,
                basis, portfolio_total, coverage_ratio,
            )
            _update_evaluation_record_outcome_safely(
                evaluation_record_repo, batch_id, stock_code, unified_rank, None,
                False,
                buy_cooldown.block_category.value
                if buy_cooldown.block_category
                else EligibilityBlockCategory.TRADE_COOLDOWN.value,
                buy_cooldown.block_reason, (), None,
            )
            continue

        # cross-pipeline重複抑止(コードレビュー対応2026-08、指摘5)。
        buy_priority = notification_service.check_cross_pipeline_priority_eligibility(
            recommendation, now
        )
        if not buy_priority.eligible:
            cross_pipeline_blocked_count += 1
            _record_notification_outcome_audit(
                audit_service, rule_version, now, recommendation, unified_rank, None,
                buy_priority.block_reason or "NOT_REQUIRED", buy_priority,
                basis, portfolio_total, coverage_ratio,
            )
            _update_evaluation_record_outcome_safely(
                evaluation_record_repo, batch_id, stock_code, unified_rank, None,
                False,
                buy_priority.block_category.value
                if buy_priority.block_category
                else EligibilityBlockCategory.LOW_PRIORITY.value,
                buy_priority.block_reason, (), None,
            )
            continue

        if recommendation.candidate_source in (CandidateSource.HOLDING, CandidateSource.BOTH):
            sector_total = (
                sector_totals.get(recommendation.buy_industry_sector, Decimal("0"))
                if recommendation.buy_industry_sector is not None
                else Decimal("0")
            )
            trading_unit = config.profit_taking.trading_unit.default_trading_unit
            holding_data_inconsistent, holding_is_odd_lot = _holding_data_consistency(
                recommendation.holding_quantity,
                recommendation.average_acquisition_price,
                trading_unit,
            )
            assessment, addon_eligibility = evaluate_add_on_eligibility(
                current_market_value=recommendation.current_market_value or Decimal("0"),
                current_price=recommendation.price_at_recommendation,
                trading_unit=trading_unit,
                portfolio_total_market_value=portfolio_total,
                sector_total_market_value=sector_total,
                portfolio_valuation_basis=basis,
                conflicting_holding_action=recommendation.conflicting_holding_action,
                holding_data_inconsistent=holding_data_inconsistent,
                holding_is_odd_lot=holding_is_odd_lot,
                config=config.add_on,
            )
            # --- Recommendationは不変スナップショットのため、ワーカーが既に保存
            # 済みのレコードを上書き保存することはできない
            # (RecommendationRepository.saveはrecommendation_id重複時に例外を送出
            # する設計)。finalize時に確定するadd-on評価結果はここでは永続化せず、
            # 通知本文の生成・監査ログ記録のためのin-memoryコピーとしてのみ使う
            # (最終的な処分はunified_buy_candidate_notification_outcome監査に
            # 記録される)。---
            recommendation = recommendation.model_copy(
                update={
                    "projection_basis": assessment.projection_basis,
                    "projected_add_on_quantity": assessment.projected_add_on_quantity,
                    "projected_add_on_price": assessment.projected_add_on_price,
                    "projected_add_on_amount": assessment.projected_add_on_amount,
                    "projected_investment_amount": (
                        (recommendation.current_market_value or Decimal("0"))
                        + assessment.projected_add_on_amount
                    ),
                    "current_position_ratio": assessment.current_position_ratio,
                    "projected_position_ratio": assessment.projected_position_ratio,
                    "current_sector_ratio": assessment.current_sector_ratio,
                    "projected_sector_ratio": assessment.projected_sector_ratio,
                    "add_on_eligibility": (
                        AddOnEligibility.ELIGIBLE
                        if addon_eligibility.eligible
                        else AddOnEligibility.BLOCKED
                    ),
                    "add_on_block_reasons": assessment.reasons,
                    "buy_action": (
                        recommendation.buy_action
                        if addon_eligibility.eligible
                        else BuyAction.MANUAL_REVIEW
                    ),
                }
            )
            if not addon_eligibility.eligible:
                addon_blocked_count += 1
                _record_notification_outcome_audit(
                    audit_service, rule_version, now, recommendation, unified_rank, None,
                    "NOT_REQUIRED", addon_eligibility, basis, portfolio_total, coverage_ratio,
                )
                _update_evaluation_record_outcome_safely(
                    evaluation_record_repo, batch_id, stock_code, unified_rank, None,
                    False,
                    addon_eligibility.block_category.value
                    if addon_eligibility.block_category
                    else None,
                    addon_eligibility.block_reason, assessment.reasons, None,
                )
                continue

        resend = notification_service.check_resend_eligibility(recommendation, now)
        if not resend.eligible:
            resend_suppressed_count += 1
            _record_notification_outcome_audit(
                audit_service, rule_version, now, recommendation, unified_rank, None,
                resend.block_reason or "SUPPRESSED", resend, basis, portfolio_total, coverage_ratio,
            )
            _update_evaluation_record_outcome_safely(
                evaluation_record_repo, batch_id, stock_code, unified_rank, None,
                False,
                resend.block_category.value
                if resend.block_category
                else EligibilityBlockCategory.RECENTLY_NOTIFIED.value,
                resend.block_reason, (), None,
            )
            continue

        if len(eligible_winners) >= max_notifications:
            outside_top5_count += 1
            _record_notification_outcome_audit(
                audit_service, rule_version, now, recommendation, unified_rank, None,
                "NOT_REQUIRED",
                NotificationEligibility(
                    eligible=False,
                    block_category=EligibilityBlockCategory.OUTSIDE_TOP_5,
                    block_reason="OUTSIDE_TOP_5",
                ),
                basis, portfolio_total, coverage_ratio,
            )
            _update_evaluation_record_outcome_safely(
                evaluation_record_repo, batch_id, stock_code, unified_rank, None,
                False, EligibilityBlockCategory.OUTSIDE_TOP_5.value, "OUTSIDE_TOP_5", (), None,
            )
            continue

        eligible_winners.append((unified_rank, recommendation))

    send_result = notification_service.notify_buy_candidates_digest(
        [rec for _, rec in eligible_winners], now
    )

    notification_rank = 0
    sent_count = 0
    send_failed_count = 0
    # 通知ドライラン機能(2026-08追加): DRY_RUN時は全通知条件・ランキング・
    # 上限判定を通過していても外部LINE送信のみ行われない(WOULD_SEND_DRY_RUN)。
    # 「通知済み」にも「送信失敗」にも計上しない、独立したカウンタとする。
    dry_run_would_send_count = 0
    for unified_rank, rec in eligible_winners:
        outcome = send_result.get(rec.stock_code, "SEND_FAILED")
        # 通知検証モード機能(2026-08追加): SENT_VALIDATIONもLINE送信に成功した
        # 銘柄数として扱う(NotificationLog未保存を理由に0件扱いにしない)。
        # コードレビュー対応(2026-08、買い候補サマリー表示改修): SENT_LOG_FAILED
        # はLINE送信自体には成功しているため、表示上・件数上は「通知済み」として
        # 扱う(「送信失敗」とはしない)。内部のsend_outcomeでのみSENT_LOG_FAILEDを
        # 区別し、既存どおりLambda例外による運用検知(下記log_failed)は維持する。
        if outcome == "WOULD_SEND_DRY_RUN":
            dry_run_would_send_count += 1
            # コードレビュー対応(2026-08、通知ドライラン機能): 通知条件は通過した
            # (eligible=True)が実LINE送信はしていないため、notification_status
            # (実際の送信結果)へ"SENT"を記録しない。「eligible=True・
            # send_outcome=WOULD_SEND_DRY_RUN」の組み合わせで、「条件は満たしたが
            # 実送信はしていない」ことを監査上も明確に区別する(将来のLINEからの
            # 理由照会機能で「送信済み」と誤認されないようにするため)。
            _record_notification_outcome_audit(
                audit_service, rule_version, now, rec, unified_rank, None,
                outcome, NotificationEligibility(eligible=True),
                basis, portfolio_total, coverage_ratio,
            )
            _update_evaluation_record_outcome_safely(
                evaluation_record_repo, batch_id, rec.stock_code, unified_rank, None,
                True, None, None, (), outcome,
            )
        elif outcome in ("SENT_AND_RECORDED", "SENT_VALIDATION", "SENT_LOG_FAILED"):
            notification_rank += 1
            sent_count += 1
            _record_notification_outcome_audit(
                audit_service, rule_version, now, rec, unified_rank, notification_rank,
                "SENT", NotificationEligibility(eligible=True),
                basis, portfolio_total, coverage_ratio,
            )
            _update_evaluation_record_outcome_safely(
                evaluation_record_repo, batch_id, rec.stock_code, unified_rank, notification_rank,
                True, None, None, (), outcome,
            )
        else:
            send_failed_count += 1
            _record_notification_outcome_audit(
                audit_service, rule_version, now, rec, unified_rank, None,
                outcome, NotificationEligibility(eligible=False, block_reason=outcome),
                basis, portfolio_total, coverage_ratio,
            )
            _update_evaluation_record_outcome_safely(
                evaluation_record_repo, batch_id, rec.stock_code, unified_rank, None,
                False, None, outcome, (), outcome,
            )

    log_failed = [code for code, outcome in send_result.items() if outcome == "SENT_LOG_FAILED"]
    if log_failed:
        logger.error(
            "buy_candidates_handler: NotificationLog save failed after successful LINE send "
            "stock_codes=%s (manual verification required)",
            sorted(log_failed),
        )

    # --- NEAR BUY/WATCH_BEFORE_EARNINGS専用ランキング→finalizeループ
    # (BUY候補裾野拡大機能2026-08、要求仕様§1-B)。BUYループとは独立した
    # 集計・日次上限を持つ。data quality → trade cooldown → resend →
    # (NEAR BUYのみ)日次上限、の順でゲートを評価する。 ---
    near_buy_entries: list[tuple[tuple[float, ...], str, str]] = [
        _decode_near_buy_ranking_entry(entry) for entry in progress.near_buy_ranking_entries
    ]
    near_buy_entries.sort(key=lambda item: item[0])  # distance_pct昇順、同点は-quality_score昇順
    near_buy_max = config.buy_decision.near_buy.daily_max_notifications
    near_buy_daily_count = 0
    near_buy_sent_count = 0

    for near_unified_rank, (_nb_sort_key, nb_stock_code, nb_recommendation_id) in enumerate(
        near_buy_entries, start=1
    ):
        nb_recommendation = recommendation_repo.get(nb_recommendation_id)
        if nb_recommendation is None:
            logger.warning(
                "buy_candidates_handler: recommendation not found for near-buy winner "
                "stock_code=%s recommendation_id=%s",
                nb_stock_code,
                nb_recommendation_id,
            )
            continue

        nb_dq = notification_service.check_data_quality_eligibility(
            nb_recommendation, now, context=NotificationContext.BUY_CANDIDATE_BATCH
        )
        if not nb_dq.eligible:
            _record_notification_outcome_audit(
                audit_service, rule_version, now, nb_recommendation, near_unified_rank, None,
                "NOT_REQUIRED", nb_dq, basis, portfolio_total, coverage_ratio,
            )
            continue

        nb_cooldown = notification_service.check_trade_cooldown_eligibility(nb_recommendation, now)
        if not nb_cooldown.eligible:
            _record_notification_outcome_audit(
                audit_service, rule_version, now, nb_recommendation, near_unified_rank, None,
                nb_cooldown.block_reason or "NOT_REQUIRED", nb_cooldown,
                basis, portfolio_total, coverage_ratio,
            )
            continue

        # cross-pipeline重複抑止(コードレビュー対応2026-08、指摘5)。
        nb_priority = notification_service.check_cross_pipeline_priority_eligibility(
            nb_recommendation, now
        )
        if not nb_priority.eligible:
            _record_notification_outcome_audit(
                audit_service, rule_version, now, nb_recommendation, near_unified_rank, None,
                nb_priority.block_reason or "NOT_REQUIRED", nb_priority,
                basis, portfolio_total, coverage_ratio,
            )
            continue

        nb_resend = notification_service.check_resend_eligibility(nb_recommendation, now)
        if not nb_resend.eligible:
            _record_notification_outcome_audit(
                audit_service, rule_version, now, nb_recommendation, near_unified_rank, None,
                nb_resend.block_reason or "SUPPRESSED", nb_resend,
                basis, portfolio_total, coverage_ratio,
            )
            continue

        # 日次上限はNEAR_BUYカテゴリのみに適用する(WATCH_BEFORE_EARNINGSは
        # 元々対象銘柄数が少ないため上限を設けない)。
        is_near_buy = nb_recommendation.watch_type == WatchType.NEAR_BUY
        if is_near_buy and near_buy_daily_count >= near_buy_max:
            _record_notification_outcome_audit(
                audit_service, rule_version, now, nb_recommendation, near_unified_rank, None,
                "NOT_REQUIRED",
                NotificationEligibility(
                    eligible=False,
                    block_category=EligibilityBlockCategory.DAILY_LIMIT_NEAR_BUY,
                    block_reason="DAILY_LIMIT_NEAR_BUY",
                ),
                basis, portfolio_total, coverage_ratio,
            )
            continue

        # コードレビュー対応(2026-08、LINE通知アクション限定化): NEAR BUY/
        # WATCH_BEFORE_EARNINGSは「今すぐ売買アクションを取れない」監視系
        # 判定のため、ここまでの全ゲート通過後もLINEへは送信しない
        # (WatchStateService側の内部監視・昇格判定自体はこのゲートより前段
        # で完了済みのため一切影響を受けない)。送らなかったこと自体は
        # NON_ACTIONABLEとしてAuditへ必ず記録する(黙って握りつぶさない)。
        near_buy_sent_count += 1
        if is_near_buy:
            near_buy_daily_count += 1
        _record_notification_outcome_audit(
            audit_service, rule_version, now, nb_recommendation, near_unified_rank,
            near_buy_sent_count, "NOT_REQUIRED",
            NotificationEligibility(
                eligible=False,
                block_category=EligibilityBlockCategory.NON_ACTIONABLE,
                block_reason="NON_ACTIONABLE",
            ),
            basis, portfolio_total, coverage_ratio,
        )

    # --- WATCH終了通知の実送信経路(コードレビュー対応2026-08、§3)。
    # ランキングは不要(1銘柄1回のみ発生する一過性イベント)なため、日次上限
    # ループとは異なり単純な順次処理とする。 ---
    for we_recommendation_id in progress.watch_end_ranking_entries:
        we_recommendation = recommendation_repo.get(we_recommendation_id)
        if we_recommendation is None:
            logger.warning(
                "buy_candidates_handler: recommendation not found for watch-end notification "
                "recommendation_id=%s",
                we_recommendation_id,
            )
            continue

        we_dq = notification_service.check_data_quality_eligibility(
            we_recommendation, now, context=NotificationContext.BUY_CANDIDATE_BATCH
        )
        if not we_dq.eligible:
            _record_notification_outcome_audit(
                audit_service, rule_version, now, we_recommendation, None, None,
                "NOT_REQUIRED", we_dq, basis, portfolio_total, coverage_ratio,
            )
            continue

        we_cooldown = notification_service.check_trade_cooldown_eligibility(we_recommendation, now)
        if not we_cooldown.eligible:
            _record_notification_outcome_audit(
                audit_service, rule_version, now, we_recommendation, None, None,
                we_cooldown.block_reason or "NOT_REQUIRED", we_cooldown,
                basis, portfolio_total, coverage_ratio,
            )
            continue

        # コードレビュー対応(2026-08、LINE通知アクション限定化): WATCH終了通知は
        # 「監視をやめた」ことの報告であり、ユーザーに売買アクションを促す通知
        # ではないため、NEAR BUY/WATCH_BEFORE_EARNINGSと同様にLINE送信をやめ、
        # NON_ACTIONABLEとしてAuditへ記録するのみとする。
        _record_notification_outcome_audit(
            audit_service, rule_version, now, we_recommendation, None, None,
            "NOT_REQUIRED",
            NotificationEligibility(
                eligible=False,
                block_category=EligibilityBlockCategory.NON_ACTIONABLE,
                block_reason="NON_ACTIONABLE",
            ),
            basis, portfolio_total, coverage_ratio,
        )

    # 買い候補サマリー表示改修(2026-08): 「購入判定」(判定状態)と「買い候補の
    # 通知結果」(通知処理状態)を明確に分離した2種類の内訳を組み立てる。
    # 「買い候補」総数(candidate_not_ranked)は判定時点の値をそのまま使い、
    # finalizeで0へ上書きしない(以前はここで購入判定の総数自体が消えていた)。
    purchase_judgment_counts = {
        "buy_candidate": progress.category_counts.get("candidate_not_ranked", 0),
        "near_buy": progress.category_counts.get("near_buy", 0),
        "watch_wait": progress.category_counts.get("watch_wait", 0),
        "not_attractive": progress.category_counts.get("hold", 0),
        "manual_review": progress.category_counts.get("review", 0),
        "data_insufficient": progress.category_counts.get("data_insufficient", 0),
        "failed": progress.category_counts.get("failed", 0),
    }
    other_suppressed_count = (
        data_quality_blocked_count + trade_cooldown_blocked_count + cross_pipeline_blocked_count
    )
    notification_result_counts = {
        "sent": sent_count,
        "notification_limit": outside_top5_count,
        "resend_suppressed": resend_suppressed_count,
        "addon_blocked": addon_blocked_count,
        "other_suppressed": other_suppressed_count,
        "send_failed": send_failed_count,
        "other_error": record_not_found_count,
        # 通知ドライラン機能(2026-08追加): 「通知済み」にも「送信失敗」にも
        # 計上しない独立区分。SEND/NORMALでは構造的に常に0のまま
        # (WOULD_SEND_DRY_RUNはnotification_mode=DRY_RUN時にしか発生しない)。
        "dry_run_would_send": dry_run_would_send_count,
    }

    notification_service.notify_batch_summary(
        _PROCESS_NAME,
        progress.total,
        progress.category_counts,
        now,
        data_insufficient_stock_codes=progress.data_insufficient_stock_codes,
        failed_stock_codes=progress.failed_stock_codes,
        buy_candidates_sent_count=sent_count,
        near_buy_sent_count=near_buy_sent_count,
        send_empty_summary=config.notification.send_empty_summary,
        purchase_judgment_counts=purchase_judgment_counts,
        notification_result_counts=notification_result_counts,
    )

    # LINE UI第二弾「対象確認」機能(2026-08)向け、latest completed batch
    # pointerの更新。以下2条件を両方満たす場合のみ更新する:
    #   1. execution_context.mode == NORMAL(VALIDATION/DRY_RUNでは絶対に
    #      更新しない。mode自体を直接比較し、is_validation/is_dry_runという
    #      派生プロパティの意味論に依存しない最も明示的な条件とする)
    #   2. このbatchの全対象銘柄についてBuyCandidateEvaluationRecordの保存が
    #      実際に成功している(evaluation_record_saved_stock_codesの件数が
    #      total と一致。EvaluationRecord保存はbest-effortのため、GSI反映
    #      遅延とは独立に、保存そのものの欠損を見逃さないための判定)。
    # 条件を満たさない場合はポインタを更新せず、直前の正常完了batchの値を
    # 維持する(新しいNotificationLog保存失敗(下記log_failed)によってここまで
    # 到達していれば、通知系のブックキーピング障害であり分析結果自体は既に
    # 完成しているため、ポインタ更新には影響させない)。
    if latest_batch_pointer_repo is not None and execution_context.mode == ExecutionMode.NORMAL:
        saved_count = len(progress.evaluation_record_saved_stock_codes)
        if saved_count == progress.total:
            latest_batch_pointer_repo.update_latest_completed(
                LatestBuyCandidateBatchPointer(
                    latest_completed_batch_id=batch_id,
                    completed_at=now,
                    total_candidates=progress.total,
                )
            )
        else:
            logger.error(
                "buy_candidates_handler: evaluation record save incomplete, "
                "latest batch pointer NOT updated (前回正常batchのまま維持) "
                "batch_id=%s saved=%d total=%d",
                batch_id,
                saved_count,
                progress.total,
            )

    if execution_context.is_validation:
        # 通知検証モード機能(2026-08追加): 通知送信が正常終了した後、このバッチで
        # 保存した全Recommendationを検証用テーブルから削除する(使い捨て
        # テーブルのため。異常終了時はTTL(2時間)が安全網となる)。個別の削除
        # 失敗はバッチの成功可否・LINE送信結果に影響させないベストエフォート処理。
        for recommendation_id in progress.validation_recommendation_ids:
            try:
                recommendation_repo.delete(recommendation_id)
            except Exception:  # noqa: BLE001 - TTLで最終的に解消されるため処理は継続する
                logger.warning(
                    "buy_candidates_handler: failed to delete validation recommendation "
                    "recommendation_id=%s (TTLで自動削除されます)",
                    recommendation_id,
                    exc_info=True,
                )

    if log_failed:
        # LINE送信自体は成功済みだがNotificationLog保存に失敗した銘柄がある。
        # 二重送信を避けるためこのバッチ内では再送しないが、記録漏れを見逃さない
        # よう、Lambda呼び出し自体を失敗させて運用検知(CloudWatch Alarm等)に
        # 委ねる(統合BUY候補パイプライン2026-07)。
        raise RuntimeError(
            f"notify_buy_candidates_digest: NotificationLog保存に失敗した銘柄があります "
            f"(LINE送信自体は成功済み): {sorted(log_failed)}"
        )


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    # 通知検証モード機能(2026-08追加)。不正なexecution_modeは他の一切の処理より
    # 前にここで例外を送出し、Lambda呼び出し自体を失敗させる(NORMALへフォール
    # バックしない)。
    execution_context = resolve_execution_context(event)
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    calendar = BusinessCalendar.from_config(config.holiday_calendar)
    providers = build_real_provider_bundle(now, config)
    recommendation_repo = RecommendationRepository.for_execution_context(execution_context)
    evaluation_record_repo = BuyCandidateEvaluationRecordRepository()
    latest_batch_pointer_repo = LatestBuyCandidateBatchPointerRepository()
    # BUY候補裾野拡大機能(2026-08、§5-1): 子Lambda(task=buy_candidate)は
    # 親Lambdaがdetect_and_apply()の結果をイベントペイロード経由で伝播した
    # trade_detection_confirmedをそのまま使う(親自身は通知を送らないため
    # このフラグ自体は不要、既定Trueのままでよい)。
    trade_detection_confirmed = event.get("trade_detection_confirmed", True)
    notification_service = LineNotificationService(
        line_client=build_line_client_from_env(),
        notification_log_repository=NotificationLogRepository(),
        recommendation_repository=recommendation_repo,
        config=config,
        execution_context=execution_context,
        trade_detection_confirmed=trade_detection_confirmed,
    )

    if event.get("task") == "buy_candidate":
        # 子Lambda: batch_idはevent由来なのでこの時点で既に確定している。
        if execution_context.is_validation:
            logger.info(
                "VALIDATION MODE task=buy_candidate execution_mode=VALIDATION "
                "notification_mode=%s event_notification_mode=%r is_dry_run=%s "
                "validation_run_id=%s stock_code=%s",
                execution_context.notification_mode.value,
                event.get("notification_mode"),
                execution_context.is_dry_run,
                event.get("batch_id"),
                event["stock_code"],
            )
        average_acquisition_price = (
            Decimal(event["average_acquisition_price"])
            if event.get("average_acquisition_price") is not None
            else None
        )
        result = _process_single_candidate(
            event["stock_code"],
            CandidateSource(event["source"]),
            event.get("holding_quantity"),
            average_acquisition_price,
            event.get("batch_id"),
            now,
            providers,
            config,
            calendar,
            recommendation_repo,
            notification_service,
            execution_context,
            evaluation_record_repo,
            latest_batch_pointer_repo,
        )
        logger.info("buy_candidates_handler single candidate done: %s", result)
        return result

    # 通常のスケジュール起動(ディスパッチのみ行い、銘柄ごとの実処理は非同期の
    # 自己再帰呼び出しに委ねる。気になる銘柄と保有銘柄を銘柄コード単位で統合し、
    # 両方に登録されている銘柄は1回だけ評価する)
    # 株主優待レジストリの読み込み件数チェックは、銘柄ごとのワーカー呼び出しでは
    # なくバッチ開始時(ここ)で1回だけ行う(2026-07仕様レビュー対応)。
    check_registry_health(
        config.notification.operations.shareholder_benefit_registry_min_expected_entries
    )

    # --- BUY候補裾野拡大機能(2026-08、§5-1・§5-2): 売買イベント検知を
    # BUY候補Lambda・保有銘柄Lambdaの起動順序に依存させない。両ハンドラの
    # 入口でTradeCooldownService.detect_and_apply()を呼ぶ(冪等・PROCESSING/
    # COMPLETEDロックにより当日1回だけ実際の検知処理が走る)。検知した
    # TradeEventをWatchStateService.end_for_trade_events()へ明示的に渡し、
    # 該当銘柄のWatchStateを終了する(TradeCooldownServiceとWatchStateServiceは
    # 直接依存しない、責務分離)。
    trade_cooldown_service = TradeCooldownService(
        business_calendar=calendar,
        config=config.notification.trade_cooldown,
        execution_context=execution_context,
    )
    current_holdings_by_id = {h.holding_id: h for h in PortfolioService().list_holdings()}
    detection_outcome = trade_cooldown_service.detect_and_apply(current_holdings_by_id, now)
    if detection_outcome.confirmed:
        watch_state_service = WatchStateService(
            business_calendar=calendar, execution_context=execution_context
        )
        # 再コードレビュー対応(2026-08、JST暦日境界修正・指摘4): 保有銘柄Lambda側
        # (holdings_watchlist_handler.py)と同じ理由でJST暦日(evaluation_date_jst)
        # を使う(TradeDetectionの基準日・WatchStateの経過判定基準日を統一する)。
        watch_state_service.end_for_trade_events(detection_outcome.events, evaluation_date_jst(now))
    else:
        logger.warning(
            "buy_candidates_handler: trade detection not confirmed this run "
            "(TRADE_DETECTION_IN_PROGRESSとして通常通知をfail-closedする)"
        )

    function_name = resolve_function_name(context, os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""))
    targets = _build_unified_targets(config, now, execution_context)
    holding_count = sum(
        1 for t in targets if t.source in (CandidateSource.HOLDING, CandidateSource.BOTH)
    )
    batch_id = f"buy-candidates-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    start_batch(batch_id, len(targets), now, holding_count=holding_count)
    if execution_context.is_validation:
        # 通知検証モード機能(2026-08追加): batch_idはここで初めて確定するため、
        # イベント解析直後ではなくこの時点でVALIDATION開始ログを出す。
        logger.info(
            "VALIDATION MODE START execution_mode=VALIDATION notification_mode=%s "
            "validation_run_id=%s target_count=%d",
            execution_context.notification_mode.value,
            batch_id,
            len(targets),
        )

    for target in targets:
        child_payload: dict[str, Any] = {
            "task": "buy_candidate",
            "stock_code": target.stock_code,
            "source": target.source.value,
            "batch_id": batch_id,
            "holding_quantity": target.holding_quantity,
            "average_acquisition_price": (
                str(target.average_acquisition_price)
                if target.average_acquisition_price is not None
                else None
            ),
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
        dispatch_async(function_name, child_payload)

    logger.info(
        "buy_candidates_handler dispatched: scanned=%d (holdings=%d) batch_id=%s",
        len(targets),
        holding_count,
        batch_id,
    )
    return {"dispatched": len(targets)}
