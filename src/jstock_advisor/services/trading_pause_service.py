"""BUY/SELL一時停止フラグの取得・更新サービス(保有銘柄オーナー機能移行用)。

保有銘柄オーナー機能の移行(V2テーブルへの切替)中、LINE会話型UIからの新規
BUY/SELL登録を一時的に止めるためのフラグ。WATCH(ウォッチリスト登録)は
Holdings/PurchaseLotsを一切更新しないため対象外(commit_watch()はConversation
State削除+Watchlist登録のみ)。

kill switch(HoldingDecisionRuntimeConfigService.get_notification_enabled())と
同じ理由で、TTLキャッシュを一切経由せず毎回リポジトリへ直接問い合わせる
(移行作業中にpause状態を切り替えた場合、LINE会話型UIが古いキャッシュ値で
新規取引を受け付けてしまうことを避けるため)。取得に失敗した場合は安全側
(pause_buy_sell=True、つまり一時停止扱い)へフォールバックする。未初期化
(レコードが存在しない、=この機能をまだ使っていない通常運用)の場合は
pause_buy_sell=False(通常どおり売買を許可)として扱う。
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

from jstock_advisor.domain.entities.trading_pause import TradingPauseConfig
from jstock_advisor.infrastructure.aws import trading_pause_config as _repo
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.incident_envelope_publisher import publish_incident_envelope

logger = logging.getLogger(__name__)

# 取得失敗時の安全側既定値: 一時停止扱い(誤って新規取引を受け付けない)。
_FALLBACK_PAUSE_BUY_SELL = True

# Issue #670(HF-5): 設定の取得に失敗して安全側(一時停止)へ倒れたことを、HF-0契約(#665)で
# USERへ通知する。fail-safe(一時停止)は維持し、戻り値は通知の有無・成否に依らない。
# 本serviceの本番の呼び出し元はLINE webhook(会話型UIの売買登録)だけのため、job名は固定する
# (incident_message.py::_INTERNAL_NAME_TO_JOBの"line-webhook" = 「LINE の応答」)。
_INCIDENT_SOURCE = "trading_pause_config"
_INCIDENT_JOB_NAME = "line-webhook"
_INCIDENT_FAILURE_STAGE = "TRADING_PAUSE_CONFIG_FETCH"
_INCIDENT_FAILURE_TYPE = "FETCH_FAILED"
_INCIDENT_TOPIC_ENV = "INCIDENT_NOTIFICATION_TOPIC_ARN"
# reason_codeはenvelope内にliteralで書く(#745のAST完全性ガードが読む)。利用者向けの「内容」文は
# incident_message.py::IncidentContent(PROVISIONAL)に登録する。
# LINE webhookの同期の応答の経路。通知の発行が応答を待たせる上限(接続・読取のtimeout。再試行なし)。
_PUBLISH_TIMEOUT_SECONDS = 1.5


class TradingPauseAlreadyInitializedError(Exception):
    pass


class TradingPauseService:
    def __init__(
        self,
        audit_service: AuditService | None = None,
        store_dir: Path | None = None,
        notify_fetch_failures: bool = True,
    ) -> None:
        """`notify_fetch_failures`(Issue #670): `is_buy_sell_paused()`は、設定の取得に失敗して
        安全側へ倒れたとき、HF-0契約でUSERへ通知する(SNS publish。**read名のメソッドが
        外部状態〔SNS〕へ副作用を持つ**ことの明示)。`INCIDENT_NOTIFICATION_TOPIC_ARN`が設定されていない環境
        (運用者のCLI・ローカル実行・template反映前)では送らない。送りたくない呼び出し元は
        `False`を渡す。通知の有無・成否でfail-safeの戻り値は変わらない。1つのinstanceにつき、
        最大1回だけ試みる(失敗・timeoutでも再試行しない = 同じ応答の中で何度呼ばれても待ちは1回)。
        """
        self._audit_service = audit_service or AuditService()
        self._store_dir = store_dir
        self._notify_fetch_failures = notify_fetch_failures
        self._fetch_failure_notified = False

    def _notify_fetch_failure_safely(self, now: dt.datetime) -> None:
        """取得失敗によるfallbackをHF-0契約(#665)でUSERへ通知する(fail-soft)。

        envelopeはallowlistのキーのみ(銘柄コード・所有者・stack trace・生のexception messageは
        含めない。`publish_incident_envelope()`が最後の防御として再確認する)。
        `failure_class = HANDLED_FAILURE`のため、GitHub Issueは自動起票されない(HF-0)。
        通知の失敗(SNS権限不足・障害・timeout)は握り、fail-safeの戻り値・LINEの応答を変えない。
        """
        if not self._notify_fetch_failures or self._fetch_failure_notified:
            return
        if not os.environ.get(_INCIDENT_TOPIC_ENV):
            return
        # ★ 送る前に印を付ける ★: 失敗・timeoutでも、同じinstanceでは再試行しない
        self._fetch_failure_notified = True
        try:
            publish_incident_envelope(
                {
                    "source": _INCIDENT_SOURCE,
                    "job_name": _INCIDENT_JOB_NAME,
                    "failure_stage": _INCIDENT_FAILURE_STAGE,
                    "failure_type": _INCIDENT_FAILURE_TYPE,
                    "reason_code": "LINE_WEBHOOK_TRADING_PAUSE_CONFIG_FETCH_FAILED",
                    "occurred_at": now.isoformat(),
                    "failure_class": "HANDLED_FAILURE",
                },
                timeout_seconds=_PUBLISH_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 - 通知の失敗でfail-safe・LINEの応答を止めない
            logger.warning(
                "TradingPauseConfig: failed to publish HANDLED_FAILURE envelope failure_stage=%s",
                _INCIDENT_FAILURE_STAGE,
            )

    def is_buy_sell_paused(self, now: dt.datetime | None = None) -> bool:
        try:
            fetched = _repo.get(self._store_dir)
        except Exception:
            logger.exception(
                "TradingPauseConfigの取得に失敗しました。安全側(一時停止)として扱います"
            )
            self._notify_fetch_failure_safely(now or dt.datetime.now(dt.UTC))
            return _FALLBACK_PAUSE_BUY_SELL
        if fetched is None:
            return False
        return fetched.pause_buy_sell

    def get_config(self) -> TradingPauseConfig | None:
        return _repo.get(self._store_dir)

    def init_config(
        self,
        pause_buy_sell: bool,
        updated_by: str,
        change_reason: str,
        now: dt.datetime | None = None,
    ) -> TradingPauseConfig:
        created = _repo.init(
            pause_buy_sell=pause_buy_sell,
            updated_by=updated_by,
            change_reason=change_reason,
            now=now,
            store_dir=self._store_dir,
        )
        if created is None:
            raise TradingPauseAlreadyInitializedError(
                "TradingPauseConfigは既に初期化済みです。変更するにはsetコマンドを使用してください。"
            )
        self._record_audit(before=None, after=created)
        return created

    def update_config(
        self,
        expected_config_version: int,
        pause_buy_sell: bool,
        updated_by: str,
        change_reason: str,
        now: dt.datetime | None = None,
    ) -> TradingPauseConfig:
        before = _repo.get(self._store_dir)
        updated = _repo.update(
            expected_config_version=expected_config_version,
            pause_buy_sell=pause_buy_sell,
            updated_by=updated_by,
            change_reason=change_reason,
            now=now,
            store_dir=self._store_dir,
        )
        self._record_audit(before=before, after=updated)
        return updated

    def _record_audit(
        self, before: TradingPauseConfig | None, after: TradingPauseConfig
    ) -> None:
        # 監査ログ記録は更新成功後にのみ行う。記録自体が失敗しても、既に成功した
        # 設定変更をロールバックしない(操作は成功扱いのまま警告ログのみ残す)。
        try:
            self._audit_service.record(
                decision_type="trading_pause_config",
                stock_code=None,
                input_values={
                    "before_pause_buy_sell": before.pause_buy_sell if before else None,
                    "expected_config_version": before.config_version if before else None,
                },
                calculation_formulas={},
                output_values={
                    "after_pause_buy_sell": after.pause_buy_sell,
                    "config_version": after.config_version,
                    "change_reason": after.change_reason,
                    "updated_by": after.updated_by,
                },
                data_sources=[],
                rule_version=str(after.config_version),
                timestamp=after.updated_at,
            )
        except Exception:
            logger.exception(
                "TradingPauseConfig変更の監査ログ記録に失敗しました"
                "(設定変更自体は正常に反映済みです)"
            )
