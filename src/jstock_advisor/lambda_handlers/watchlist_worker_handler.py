"""ウォッチリスト自動追加(候補ユニバース本格対応)のWorker Lambda(7節)。

SQS(WatchlistScreeningQueue、BatchSize=1)トリガー。1メッセージ=1銘柄を評価する。
リース取得(7節)→評価→complete_candidate(TransactWriteItems、通常経路)→
try_finalize_if_ready(11節、maybe_finalize経由)、という流れで動作する。

カテゴリ分類(categorize_exclusion_reasons)とAuditLog記録
(watchlist_screening_audit.record_candidate_audit)は、CLI・Terminal Failure
Handler等と共通の関数を使う。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.domain.signals.watchlist_screening import (
    WatchlistScoreDetail,
    categorize_exclusion_reasons,
)
from jstock_advisor.infrastructure.aws.batch_tracker import (
    WatchlistProgressStatus,
    claim_candidate_lease,
    complete_candidate,
)
from jstock_advisor.infrastructure.line.client import build_line_client_from_env
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.provider_factory import build_real_provider_bundle
from jstock_advisor.services.screening_data_provider import (
    ScreeningDataStatus,
    StockSnapshotScreeningDataProvider,
)
from jstock_advisor.services.watchlist_batch_finalizer import maybe_finalize
from jstock_advisor.services.watchlist_data_cache import build_cached_provider_bundle
from jstock_advisor.services.watchlist_score_detail import build_notification_detail
from jstock_advisor.services.watchlist_screening_audit import record_candidate_audit
from jstock_advisor.services.watchlist_screening_service import WatchlistScreeningService

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 7節: Lambda Timeout(180秒、3節)+60秒安全余裕。
_WORKER_LEASE_SECONDS = 240

# screening_data_provider.WatchlistScreeningInputのスコア項目総数(dividend_yield_pct/
# equity_ratio_pct/payout_ratio_pct/consecutive_dividend_increase_years/
# shareholder_benefit_yield_pctの5件)。この件数すべてが同時欠損している場合、
# 個別銘柄のデータ欠落ではなくデータ提供元側の障害を疑う(運用ハードニング3節)。
_TOTAL_SCORING_FIELD_COUNT = 5


@dataclass(frozen=True)
class _EvaluationOutcome:
    terminal_status: WatchlistProgressStatus
    evaluation_result: str
    ranking_entry_json: str | None
    is_provider_failure_suspected: bool
    missing_field_names: list[str]
    # --- LINE通知品質改善(2026-08)で追加 ---------------------------------------
    # evaluate()が実行された全銘柄でセットする(total_scoreの保存条件と
    # notification_detailの保存条件を分離する、修正①)。
    total_score: float | None = None
    notification_detail: WatchlistScoreDetail | None = None


def _evaluate_candidate(
    stock_code: str, batch_id: str, now: dt.datetime, providers: ProviderBundle, config: AppConfig
) -> _EvaluationOutcome:
    screening_data_provider = StockSnapshotScreeningDataProvider(providers, config)
    screening_data = screening_data_provider.get_screening_input(stock_code, now)

    if screening_data.status != ScreeningDataStatus.OK or screening_data.input is None:
        # 運用ハードニング第2弾3節: DATA_ERROR(取得エラー)とNOT_FOUND(データが
        # 無かった)を区別する。両方とも旧"DATA_INSUFFICIENT"に丸めていたのを分離し、
        # compute_batch_metrics()の母数計算(screening_input_created_count)から
        # 両方を除外できるようにする。
        evaluation_result = (
            "DATA_ERROR" if screening_data.status == ScreeningDataStatus.DATA_ERROR else "NOT_FOUND"
        )
        logger.info(
            "watchlist screening data unavailable stock_code=%s status=%s error=%s",
            stock_code,
            screening_data.status,
            screening_data.error_message,
        )
        record_candidate_audit(stock_code, None, evaluation_result, now, batch_id=batch_id)
        return _EvaluationOutcome(
            WatchlistProgressStatus.COMPLETED,
            evaluation_result,
            None,
            screening_data.is_provider_failure_suspected,
            screening_data.missing_fields,
        )

    screening_service = WatchlistScreeningService(config)
    result = screening_service.evaluate(
        stock_code, screening_data.input.stock_name, screening_data.input, now
    )
    # LINE通知品質改善(2026-08、修正①): total_scoreはevaluate()が実行された
    # 全銘柄(PASSED/FAILED_SCORE/FAILED_REQUIRED等)で即座にセットする。
    # notification_detailはこの後のcategory=="passed"判定の中でのみ追加でセットし、
    # 両者の保存条件を明確に分離する。
    total_score = result.total_score
    category, evaluation_result = categorize_exclusion_reasons(result.exclusion_reasons)

    ranking_entry_json = None
    notification_detail: WatchlistScoreDetail | None = None
    if category == "passed":
        entry = screening_service.to_ranking_entry(result)
        if entry is None:
            # MAX_RANKING_ENTRY_BYTESを超過し、main_metricsを空にしても収まらない場合。
            # データ取得自体は正常に完了しているため、進捗行のstatusはCOMPLETEDのまま
            # evaluation_resultで区別する。
            logger.error(
                "watchlist ranking entry exceeds size limit even after trimming stock_code=%s",
                stock_code,
            )
            evaluation_result = "PASSED_RANKING_ENTRY_TOO_LARGE"
        else:
            ranking_entry_json = entry.model_dump_json()
            notification_detail = build_notification_detail(
                stock_code,
                result.policy_results[0].score_breakdown,
                screening_data.input,
                policy_name=result.policy_results[0].policy_name,
            )

    # 運用ハードニング3節: 例外は無かった(HTTP応答自体は成立した)が、スコア項目が
    # 1件も取得できていない場合は、通常の一部欠損(この銘柄固有のデータ欠落)とは
    # 区別してデータ提供元障害の疑いに算入する。
    missing_scoring_count = len(screening_data.input.missing_scoring_fields)
    is_provider_failure_suspected = (
        screening_data.is_provider_failure_suspected
        or missing_scoring_count >= _TOTAL_SCORING_FIELD_COUNT
    )

    record_candidate_audit(stock_code, result, evaluation_result, now, batch_id=batch_id)
    return _EvaluationOutcome(
        WatchlistProgressStatus.COMPLETED,
        evaluation_result,
        ranking_entry_json,
        is_provider_failure_suspected,
        screening_data.missing_fields,
        total_score=total_score,
        notification_detail=notification_detail,
    )


def _build_notification_service(config: AppConfig) -> LineNotificationService:
    return LineNotificationService(
        line_client=build_line_client_from_env(),
        notification_log_repository=NotificationLogRepository(),
        recommendation_repository=RecommendationRepository(),
        config=config,
    )


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    config = load_config()
    now = dt.datetime.now(dt.UTC)
    providers = build_cached_provider_bundle(build_real_provider_bundle(now, config), config, now)
    notification_service = _build_notification_service(config)
    owner_id = getattr(context, "aws_request_id", None) or uuid.uuid4().hex

    processed: list[dict[str, Any]] = []
    for record in event.get("Records", []):
        body = json.loads(record["body"])
        batch_id = body["batch_id"]
        stock_code = body["stock_code"]
        claim_time = dt.datetime.now(dt.UTC)

        lease_acquired = claim_candidate_lease(
            batch_id, stock_code, owner_id, claim_time, _WORKER_LEASE_SECONDS
        )
        if not lease_acquired:
            # 既に別Workerがリースを保持中(有効期限内)、または既に終端状態
            # (Reconcilerのタイムアウト確定処理等)。SQSメッセージは削除してよい。
            logger.warning(
                "watchlist worker: lease not acquired batch_id=%s stock_code=%s",
                batch_id,
                stock_code,
            )
            processed.append({"stock_code": stock_code, "claimed": False})
            continue

        try:
            outcome = _evaluate_candidate(stock_code, batch_id, claim_time, providers, config)
        except Exception:  # noqa: BLE001 - 1銘柄の想定外エラーでバッチ全体を止めない
            logger.exception(
                "watchlist worker: unexpected evaluation error batch_id=%s stock_code=%s",
                batch_id,
                stock_code,
            )
            outcome = _EvaluationOutcome(
                WatchlistProgressStatus.FAILED, "UNEXPECTED_ERROR", None, False, []
            )

        completion_time = dt.datetime.now(dt.UTC)
        duration_ms = int((completion_time - claim_time).total_seconds() * 1000)
        completed = complete_candidate(
            batch_id,
            stock_code,
            owner_id,
            terminal_status=outcome.terminal_status,
            evaluation_result=outcome.evaluation_result,
            ranking_entry=outcome.ranking_entry_json,
            is_provider_failure_suspected=outcome.is_provider_failure_suspected,
            missing_field_names=outcome.missing_field_names,
            processing_duration_ms=duration_ms,
            now=completion_time,
            total_score=outcome.total_score,
            notification_detail=outcome.notification_detail,
        )
        if completed:
            maybe_finalize(batch_id, completion_time, providers, config, notification_service)
        else:
            # リース失効後に別Workerが再クレームしていた、またはReconcilerが先に
            # タイムアウト確定していた(17節「WorkerとReconcilerの競合」)。
            logger.warning(
                "watchlist worker: complete_candidate lost race batch_id=%s stock_code=%s",
                batch_id,
                stock_code,
            )
        processed.append({"stock_code": stock_code, "claimed": True, "completed": completed})

    return {"processed": processed}
