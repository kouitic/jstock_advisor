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
from jstock_advisor.domain.signals.watchlist_screening import categorize_exclusion_reasons
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
from jstock_advisor.services.watchlist_screening_audit import record_candidate_audit
from jstock_advisor.services.watchlist_screening_service import WatchlistScreeningService

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 7節: Lambda Timeout(180秒、3節)+60秒安全余裕。
_WORKER_LEASE_SECONDS = 240


@dataclass(frozen=True)
class _EvaluationOutcome:
    terminal_status: WatchlistProgressStatus
    evaluation_result: str
    ranking_entry_json: str | None
    is_rate_limit_suspected: bool


def _evaluate_candidate(
    stock_code: str, batch_id: str, now: dt.datetime, providers: ProviderBundle, config: AppConfig
) -> _EvaluationOutcome:
    screening_data_provider = StockSnapshotScreeningDataProvider(providers, config)
    screening_data = screening_data_provider.get_screening_input(stock_code, now)

    if screening_data.status != ScreeningDataStatus.OK or screening_data.input is None:
        logger.info(
            "watchlist screening data unavailable stock_code=%s status=%s error=%s",
            stock_code,
            screening_data.status,
            screening_data.error_message,
        )
        record_candidate_audit(stock_code, None, "DATA_INSUFFICIENT", now, batch_id=batch_id)
        return _EvaluationOutcome(
            WatchlistProgressStatus.COMPLETED,
            "DATA_INSUFFICIENT",
            None,
            screening_data.is_rate_limit_suspected,
        )

    screening_service = WatchlistScreeningService(config)
    result = screening_service.evaluate(
        stock_code, screening_data.input.stock_name, screening_data.input, now
    )
    category, evaluation_result = categorize_exclusion_reasons(result.exclusion_reasons)

    ranking_entry_json = None
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

    record_candidate_audit(stock_code, result, evaluation_result, now, batch_id=batch_id)
    return _EvaluationOutcome(
        WatchlistProgressStatus.COMPLETED,
        evaluation_result,
        ranking_entry_json,
        screening_data.is_rate_limit_suspected,
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
    providers = build_real_provider_bundle(dt.datetime.now(dt.UTC), config)
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
                WatchlistProgressStatus.FAILED, "UNEXPECTED_ERROR", None, False
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
            is_rate_limit_suspected=outcome.is_rate_limit_suspected,
            processing_duration_ms=duration_ms,
            now=completion_time,
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
