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
from jstock_advisor.domain.entities.buy_evaluation_target import BuyEvaluationTarget
from jstock_advisor.domain.entities.enums import (
    BUY_FAMILY_ACTIONS,
    AddOnEligibility,
    BuyAction,
    BuyIndustrySector,
    CandidateSource,
    EligibilityBlockCategory,
    NotificationContext,
    PortfolioValuationBasis,
    RecommendationType,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.notification_eligibility import NotificationEligibility
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.add_on_risk import evaluate_add_on_eligibility
from jstock_advisor.infrastructure.aws.batch_tracker import (
    MAX_SECTOR_ENTRIES,
    MAX_SECTOR_ENTRY_BYTES,
    BatchProgress,
    record_result,
    start_batch,
)
from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers._fanout import dispatch_async, resolve_function_name
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.buy_signal_service import RULE_VERSION_PLACEHOLDER, BuySignalService
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.provider_factory import build_real_provider_bundle
from jstock_advisor.services.rule_version_service import RuleVersionService
from jstock_advisor.services.sell_signal_service import SellSignalService
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot
from jstock_advisor.services.watchlist_service import WatchlistService

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_PROCESS_NAME = "買い候補分析"
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


def _build_unified_targets(config: AppConfig, now: dt.datetime) -> list[BuyEvaluationTarget]:
    """気になる銘柄と保有銘柄を銘柄コード単位で統合する(要求仕様§2)。

    事前ガード(要求仕様§8): 保有銘柄数がMAX_SECTOR_ENTRIESを超える場合、
    sector_entriesの書き込み上限に達する恐れがあるため保有銘柄側は評価対象へ
    含めない(監査へ記録したうえで、気になる銘柄側の評価は継続する)。
    """
    watchlist_names: dict[str, str | None] = {}
    if config.notification.include_watchlist:
        for item in WatchlistService().list_items():
            watchlist_names[item.stock_code] = item.stock_name

    holdings_by_code: dict[str, Holding] = {}
    if config.notification.include_holdings:
        holdings = PortfolioService().list_holdings()
        if len(holdings) > MAX_SECTOR_ENTRIES:
            logger.error(
                "buy_candidates_handler: holding_count=%d exceeds MAX_SECTOR_ENTRIES=%d; "
                "skipping holding-side evaluation for this run",
                len(holdings),
                MAX_SECTOR_ENTRIES,
            )
            AuditService().record(
                decision_type="unified_buy_candidate_batch_aborted",
                stock_code=None,
                input_values={"holding_count": len(holdings)},
                calculation_formulas={},
                output_values={"reason": "SECTOR_ENTRIES_LIMIT_EXCEEDED"},
                data_sources=[],
                rule_version=RULE_VERSION_PLACEHOLDER,
                timestamp=now,
            )
        else:
            holdings_by_code = {h.stock_code: h for h in holdings}

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
        holding = holdings_by_code.get(code)
        stock_name = watchlist_names.get(code) or (holding.stock_name if holding else None)
        targets.append(
            BuyEvaluationTarget(
                stock_code=code,
                stock_name=stock_name,
                source=source,
                holding_quantity=holding.shares if holding else None,
                average_acquisition_price=holding.average_purchase_price if holding else None,
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
) -> None:
    """全評価対象銘柄(BUY系以外も含む)について記録する監査(要求仕様§4・§14)。"""
    audit_service.record(
        decision_type="unified_buy_candidate_evaluation",
        stock_code=stock_code,
        input_values={
            "candidate_source": source.value,
            "holding_quantity": holding_quantity,
            "average_acquisition_price": (
                str(average_acquisition_price) if average_acquisition_price is not None else None
            ),
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
) -> dict[str, Any]:
    service = BuySignalService(providers=providers, config=config, business_calendar=calendar)
    audit_service = AuditService()
    rule_version = RuleVersionService().get_active_version_or(RULE_VERSION_PLACEHOLDER)
    category = "failed"
    ranking_entry: str | None = None
    sector_entry: str | None = None
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

            if source in (CandidateSource.HOLDING, CandidateSource.BOTH):
                current_price = recommendation.price_at_recommendation
                trading_unit = config.profit_taking.trading_unit.default_trading_unit
                holding_data_inconsistent, holding_is_odd_lot = _holding_data_consistency(
                    holding_quantity, average_acquisition_price, trading_unit
                )
                if holding_quantity is not None and holding_quantity > 0:
                    current_market_value = current_price * holding_quantity
                    if average_acquisition_price is not None and average_acquisition_price > 0:
                        total_acquisition = average_acquisition_price * holding_quantity
                        unrealized_profit_loss = current_market_value - total_acquisition
                        unrealized_profit_loss_pct = (
                            unrealized_profit_loss / total_acquisition * 100
                        )

                # --- 買い増し固有リスク: 売却・利確判定との競合(要求仕様§6)。
                # base_buy_actionがBUY系、かつ保有データに致命的な不整合が無い
                # 場合のみ確認する(不整合な保有データでSell/ProfitTakingを
                # 実行しても無意味なため)。共通購入判断と同一snapshotを渡すことで
                # 現在値・財務データの矛盾を防ぐ ---
                if base_buy_action in BUY_FAMILY_ACTIONS and not holding_data_inconsistent:
                    holding = PortfolioService().get_holding(stock_code)
                    if holding is not None:
                        sell_service = SellSignalService(
                            providers=providers, config=config, business_calendar=calendar
                        )
                        sell_outcome = sell_service.analyze(holding, now, snapshot=snapshot)
                        if sell_outcome.recommendation is not None:
                            conflicting_holding_action = (
                                sell_outcome.recommendation.recommendation_type
                            )
                        if conflicting_holding_action is None:
                            profit_service = ProfitTakingService(
                                providers=providers, config=config, business_calendar=calendar
                            )
                            profit_outcome = profit_service.analyze(holding, now, snapshot=snapshot)
                            if profit_outcome.recommendation is not None:
                                conflicting_holding_action = (
                                    profit_outcome.recommendation.recommendation_type
                                )

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

            if final_recommendation.buy_action == BuyAction.MANUAL_REVIEW:
                category = "review"
                result = {"stock_code": stock_code, "recommended": True, "notified": False}
            elif outcome.ranking_group == "buy_candidate":
                # 実際の送信可否判定は行わず、ランキング候補として登録するだけに
                # 留める(全銘柄処理完了後、購入候補ランキング順に評価・送信する)。
                category = "candidate_not_ranked"
                ranking_entry = _encode_buy_ranking_entry(final_recommendation)
                result = {"stock_code": stock_code, "recommended": True, "notified": False}
            elif outcome.ranking_group == "watch_price":
                category = "watch_not_ranked"
                result = {"stock_code": stock_code, "recommended": True, "notified": False}
            else:
                category = "hold"
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
            )
    except Exception:  # noqa: BLE001 - 1銘柄の想定外エラーで再帰呼び出し全体を落とさない
        logger.exception("buy candidate analysis failed unexpectedly stock_code=%s", stock_code)
        result = {"stock_code": stock_code, "recommended": False, "notified": False, "failed": True}

    if batch_id is not None:
        needs_code = category in ("data_insufficient", "failed")
        stock_code_for_category = stock_code if needs_code else None
        progress = record_result(
            batch_id,
            category,
            stock_code=stock_code_for_category,
            ranking_entry=ranking_entry,
            sector_entry=sector_entry,
        )
        if progress is not None and progress.is_complete:
            _finalize_batch(progress, config, now, recommendation_repo, notification_service)
    return result


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
    unified_rank: int,
    notification_rank: int | None,
    notification_status: str,
    eligibility: NotificationEligibility,
    basis: PortfolioValuationBasis,
    portfolio_total_market_value: Decimal | None,
    coverage_ratio: float,
) -> None:
    """ランキングに登録された候補(BUY系)について記録する監査(要求仕様§4・§10・§14)。"""
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


def _finalize_batch(
    progress: BatchProgress,
    config: AppConfig,
    now: dt.datetime,
    recommendation_repo: RecommendationRepository,
    notification_service: LineNotificationService,
) -> None:
    """全銘柄の処理完了を検知したワーカーが1回だけ呼ぶ。購入候補ランキング順に
    以下の固定順序でゲートを評価する(breakしない。全件をループし尽くす):
      1. データ品質  2. 保有銘柄固有ゲート(売却競合→保有データ整合性→
      ポートフォリオデータ信頼性→銘柄集中→業種集中)  3. 再送防止  4. 最大5件判定
    最大5件に到達していても他のゲートを先に評価し、真に全ゲートを通過した
    6位以下だけをOUTSIDE_TOP_5として扱う(統合BUY候補パイプライン2026-07)。
    """
    max_notifications = config.notification.buy_candidate_max_notifications_per_run
    audit_service = AuditService()
    rule_version = RuleVersionService().get_active_version_or(RULE_VERSION_PLACEHOLDER)

    buy_entries: list[tuple[tuple[float, ...], str, str]] = [
        _decode_buy_ranking_entry(entry) for entry in progress.ranking_entries
    ]
    # 降順ソート。同点時は銘柄コード昇順で決定性を確保する(要求仕様15節)。
    buy_entries.sort(key=lambda item: (tuple(-v for v in item[0]), item[1]))

    sector_totals, portfolio_total, basis, coverage_ratio = _aggregate_sector_entries(
        progress.sector_entries, progress.holding_count
    )

    quality_blocked_count = 0
    addon_blocked_count = 0
    suppressed_count = 0
    outside_top5_count = 0
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
            continue

        dq = notification_service.check_data_quality_eligibility(
            recommendation, now, context=NotificationContext.BUY_CANDIDATE_BATCH
        )
        if not dq.eligible:
            quality_blocked_count += 1
            _record_notification_outcome_audit(
                audit_service, rule_version, now, recommendation, unified_rank, None,
                "NOT_REQUIRED", dq, basis, portfolio_total, coverage_ratio,
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
                continue

        resend = notification_service.check_resend_eligibility(recommendation, now)
        if not resend.eligible:
            suppressed_count += 1
            _record_notification_outcome_audit(
                audit_service, rule_version, now, recommendation, unified_rank, None,
                resend.block_reason or "SUPPRESSED", resend, basis, portfolio_total, coverage_ratio,
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
            continue

        eligible_winners.append((unified_rank, recommendation))

    send_result = notification_service.notify_buy_candidates_digest(
        [rec for _, rec in eligible_winners], now
    )

    notification_rank = 0
    sent_count = 0
    for unified_rank, rec in eligible_winners:
        outcome = send_result.get(rec.stock_code, "SEND_FAILED")
        if outcome == "SENT_AND_RECORDED":
            notification_rank += 1
            sent_count += 1
            _record_notification_outcome_audit(
                audit_service, rule_version, now, rec, unified_rank, notification_rank,
                "SENT", NotificationEligibility(eligible=True),
                basis, portfolio_total, coverage_ratio,
            )
        else:
            _record_notification_outcome_audit(
                audit_service, rule_version, now, rec, unified_rank, None,
                outcome, NotificationEligibility(eligible=False, block_reason=outcome),
                basis, portfolio_total, coverage_ratio,
            )

    log_failed = [code for code, outcome in send_result.items() if outcome == "SENT_LOG_FAILED"]
    if log_failed:
        logger.error(
            "buy_candidates_handler: NotificationLog save failed after successful LINE send "
            "stock_codes=%s (manual verification required)",
            sorted(log_failed),
        )

    total_buy_candidates = progress.category_counts.get("candidate_not_ranked", 0)
    evaluated_count = len(buy_entries)
    adjusted_counts = dict(progress.category_counts)
    adjusted_counts["sent"] = progress.category_counts.get("sent", 0) + sent_count
    adjusted_counts["review"] = (
        progress.category_counts.get("review", 0) + quality_blocked_count + addon_blocked_count
    )
    adjusted_counts["suppressed"] = (
        progress.category_counts.get("suppressed", 0) + suppressed_count + outside_top5_count
    )
    adjusted_counts["candidate_not_ranked"] = total_buy_candidates - evaluated_count

    notification_service.notify_batch_summary(
        _PROCESS_NAME,
        progress.total,
        adjusted_counts,
        now,
        data_insufficient_stock_codes=progress.data_insufficient_stock_codes,
        failed_stock_codes=progress.failed_stock_codes,
        buy_candidates_sent_count=sent_count,
        send_empty_summary=config.notification.send_empty_summary,
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
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    calendar = BusinessCalendar.from_config(config.holiday_calendar)
    providers = build_real_provider_bundle(now, config)
    recommendation_repo = RecommendationRepository()
    notification_service = LineNotificationService(
        line_client=build_line_client_from_env(),
        notification_log_repository=NotificationLogRepository(),
        recommendation_repository=recommendation_repo,
        config=config,
    )

    if event.get("task") == "buy_candidate":
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
        )
        logger.info("buy_candidates_handler single candidate done: %s", result)
        return result

    # 通常のスケジュール起動(ディスパッチのみ行い、銘柄ごとの実処理は非同期の
    # 自己再帰呼び出しに委ねる。気になる銘柄と保有銘柄を銘柄コード単位で統合し、
    # 両方に登録されている銘柄は1回だけ評価する)
    function_name = resolve_function_name(context, os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""))
    targets = _build_unified_targets(config, now)
    holding_count = sum(
        1 for t in targets if t.source in (CandidateSource.HOLDING, CandidateSource.BOTH)
    )
    batch_id = f"buy-candidates-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    start_batch(batch_id, len(targets), now, holding_count=holding_count)

    for target in targets:
        dispatch_async(
            function_name,
            {
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
            },
        )

    logger.info(
        "buy_candidates_handler dispatched: scanned=%d (holdings=%d) batch_id=%s",
        len(targets),
        holding_count,
        batch_id,
    )
    return {"dispatched": len(targets)}
