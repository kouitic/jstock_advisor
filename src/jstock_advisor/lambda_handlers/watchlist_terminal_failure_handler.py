"""ウォッチリスト自動追加(候補ユニバース本格対応)のTerminal Failure Handler(4節)。

メインキュー(WatchlistScreeningQueue)でmaxReceiveCount(3回)を使い果たした
メッセージは、SQSのRedrivePolicyによりTerminalFailureQueueへ移動する。この
Lambdaはそのトリガーとして起動し、メッセージを消費・削除しながら該当銘柄を
FAILED確定する(4節: TerminalFailureQueueは"作業用キュー"、自動消費してFAILED
確定する。監視対象はこのHandlerの実行回数(Invocations)自体)。

Handler自体が失敗した場合(例外送出によりメッセージが削除されない)は、
TerminalFailureQueue自身のRedrivePolicy(maxReceiveCount 3回)により、真正の
DLQ(WatchlistTerminalFailureDLQ、何も自動消費しない)へ移動する。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any, NoReturn

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import AppConfig
from jstock_advisor.infrastructure.aws.batch_tracker import (
    UnknownWatchlistJobTypeError,
    WatchlistJobType,
    record_terminal_failure,
    resolve_watchlist_job_type,
)
from jstock_advisor.infrastructure.line.client import (
    LineClient,
    LineCredentialsMissingError,
    QuickReplyButton,
    build_live_line_client_from_env,
)
from jstock_advisor.infrastructure.local_repository.notification_claim_repository import (
    NotificationClaimRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers._watchlist_execution_mode import reject_execution_mode
from jstock_advisor.lambda_handlers._watchlist_notification_prescan import (
    sqs_records_require_notification_service,
)
from jstock_advisor.services.line_notification_service import LineNotificationService
from jstock_advisor.services.provider_factory import build_real_provider_bundle
from jstock_advisor.services.watchlist_batch_finalizer import (
    maybe_finalize,
    maybe_finalize_maintenance,
)
from jstock_advisor.services.watchlist_data_cache import build_cached_provider_bundle

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class _CredentialDeferredLineClient:
    """LINE認証情報が無いときに渡す、送信の瞬間に必ず失敗するclient(Issue #430。
    #429〔reconciler〕と同一ロジック。USER決定OD1=Bによりreconciler側との共通化は
    行わず、このファイル内へ独立に複製する)。

    terminal failure handlerは終端記録(状態変更)の後にfinalizeのPhase 3(通知)を
    行う。認証情報の欠落を通知サービスの「構築失敗」として扱うと、終端記録より
    前に失敗し、再配信では「既に終端」となりfinalizeが呼ばれない中途状態になる。

    そこで欠落は「実際の送信時の失敗」として扱う。送信メソッド(push_message等)は
    **必ず**`LineCredentialsMissingError`を送出し、決して成功を返さない。
    finalizerのPhase 3は例外を捕捉してNOTIFICATION_FAILEDとして記録し
    (終端記録は保持される)、通知だけが既存のretry_notification()の再試行
    機構に載る。

    ★ Phase 3の例外捕捉により、このままではLambda呼び出しが成功扱いになり欠落が
      不可視になる。そのため「欠落のまま送信が試みられた」事実を保持し、
      `raise_if_send_attempted()`をhandlerの全処理完了後に呼んで送出する。
    """

    def __init__(self, missing: LineCredentialsMissingError) -> None:
        self._missing = missing
        self.send_attempted = False

    def _fail(self) -> NoReturn:
        self.send_attempted = True
        raise LineCredentialsMissingError(str(self._missing))

    def push_message(self, text: str) -> None:
        self._fail()

    def reply_message(
        self, reply_token: str, text: str, quick_reply: list[QuickReplyButton] | None = None
    ) -> None:
        self._fail()

    def reply_messages(
        self,
        reply_token: str,
        texts: list[str],
        quick_reply: list[QuickReplyButton] | None = None,
    ) -> None:
        self._fail()

    def raise_if_send_attempted(self) -> None:
        if self.send_attempted:
            raise LineCredentialsMissingError(str(self._missing))


def _build_terminal_failure_line_client() -> LineClient:
    """認証情報があればLiveLineClient、無ければ送信時に必ず失敗するclientを返す。

    構築の失敗(`LineCredentialsMissingError`)だけを送信時の失敗へ変える。認証情報の
    欠落以外の例外は握りつぶさず、従来どおり伝播する。
    """
    try:
        return build_live_line_client_from_env()
    except LineCredentialsMissingError as exc:
        return _CredentialDeferredLineClient(exc)


def _build_notification_service(
    config: AppConfig, line_client: LineClient | None = None
) -> LineNotificationService:
    return LineNotificationService(
        line_client=(
            line_client if line_client is not None else _build_terminal_failure_line_client()
        ),
        notification_log_repository=NotificationLogRepository(),
        # LINE通知dedupの原子化(Issue #17): NORMAL実行の送信決定を原子的に
        # 一意化するclaimリポジトリ(VALIDATION/DRY_RUNでは使用されない)。
        notification_claim_repository=NotificationClaimRepository(),
        recommendation_repository=RecommendationRepository(),
        config=config,
    )


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    # Issue #286 (#70 F-B4): watchlist系は execution_mode を**受け付けない**。
    # SQS経由が通常だが、手動invokeでキーを渡された場合も黙殺しない。
    reject_execution_mode(event, handler_name="watchlist terminal failure")
    now = dt.datetime.now(dt.UTC)
    config = load_config()
    providers = build_cached_provider_bundle(build_real_provider_bundle(now, config), config, now)
    # Issue #117: 通知サービスを使うのはNEW_CANDIDATE_SCREENINGのfinalizeだけ。
    # job_type欠損時の既定は本処理(下のresolve_watchlist_job_type)と同じNEW_CANDIDATE_SCREENING。
    # Issue #430: 認証情報欠落は構築の失敗にせず、送信時の失敗として扱う
    # (_CredentialDeferredLineClient。終端記録[状態変更]より前に失敗させない。
    # 終端記録の後に失敗すると、再配信では「既に終端」となりfinalizeが呼ばれない
    # 中途状態になるため)。
    line_client: LineClient | None = None
    notification_service: LineNotificationService | None = None
    if sqs_records_require_notification_service(
        event, missing_job_type_default=WatchlistJobType.NEW_CANDIDATE_SCREENING
    ):
        line_client = _build_terminal_failure_line_client()
        notification_service = _build_notification_service(config, line_client)

    processed: list[dict[str, str]] = []
    for record in event.get("Records", []):
        body = json.loads(record["body"])
        batch_id = body["batch_id"]
        stock_code = body["stock_code"]

        if record_terminal_failure(batch_id, stock_code, now):
            # Issue #56: この銘柄が最後の未処理分だった場合、ここがfinalizeの
            # 起点になる。job_typeを見ずに常にADD用finalizerを呼ぶと、
            # WATCHLIST_MAINTENANCEバッチがメンテナンス業務(自動削除・
            # 連続非該当カウント更新・監視スコア更新)を一切実行しないまま
            # COMPLETED(終端)になり、二度と実行されない。
            # job_typeはSQSメッセージ本文に含まれている
            # (watchlist_dispatcher_handler.py)。
            try:
                job_type = resolve_watchlist_job_type(
                    body.get("job_type"),
                    default=WatchlistJobType.NEW_CANDIDATE_SCREENING,
                )
            except UnknownWatchlistJobTypeError:
                # 未知値は暗黙にどちらかへ倒さずfail-closeする。
                # finalizeしないだけでterminal failure自体は記録済みであり、
                # Reconcilerのtimeout経路が後続を担う。
                logger.error(
                    "watchlist terminal failure handler: unknown job_type=%r "
                    "batch_id=%s stock_code=%s (finalize skipped)",
                    body.get("job_type"),
                    batch_id,
                    stock_code,
                )
                processed.append({"batch_id": batch_id, "stock_code": stock_code})
                continue
            if job_type is WatchlistJobType.WATCHLIST_MAINTENANCE:
                maybe_finalize_maintenance(batch_id, now, config)
            else:
                if notification_service is None:
                    # prescanがNEW_CANDIDATE_SCREENINGを検出した場合は必ず構築済み。乖離したら
                    # 通知が黙って欠落するのではなく、明示的に失敗させる。
                    raise RuntimeError(
                        "notification service was not built for a NEW_CANDIDATE message"
                    )
                maybe_finalize(batch_id, now, providers, config, notification_service)
        else:
            # 既に他の主体(Worker/Reconciler)が終端状態へ確定済み(冪等スキップ)。
            logger.info(
                "watchlist terminal failure handler: already terminal batch_id=%s stock_code=%s",
                batch_id,
                stock_code,
            )
        processed.append({"batch_id": batch_id, "stock_code": stock_code})

    logger.info("watchlist terminal failure handler processed %d messages", len(processed))
    # Issue #430: 終端記録・NOTIFICATION_FAILEDの記録を全て終えた後に、認証情報の
    # 欠落を顕在化させる(Lambda呼び出しをErrorsとして失敗させる)。finalizerの
    # Phase 3が例外を捕捉するため、これが無いと欠落が不可視になる(#429と同じ
    # 設計方針)。
    if isinstance(line_client, _CredentialDeferredLineClient):
        line_client.raise_if_send_attempted()
    return {"processed": processed}
