"""ランタイムConfig(mode/kill switch)の取得・更新サービス(実装プラン1節)。

config/*.yaml(Lambda Layer同梱、再デプロイが必要)とは別に、mode/
notification_enabled/financial_policy_overrideという頻繁に切り替えたい
運用パラメータを専用DynamoDBテーブルへ保存する。

取得はモジュールレベルのキャッシュ(TTL)を経由し、取得失敗時は直近の
キャッシュ値、それも無ければ安全側の既定値(mode=legacy、notification_enabled=
false、financial_policy_override=FORCE_DEFER_ALL)へフォールバックする。
フォールバック使用時はHoldingDecisionResult.runtime_config_versionへ-1を
保存できるよう、RuntimeConfigLookup.is_fallbackで判別可能にする。
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from jstock_advisor.domain.entities.enums import FinancialPolicyOverride, RuntimeConfigMode
from jstock_advisor.domain.entities.holding_decision import HoldingDecisionRuntimeConfig
from jstock_advisor.infrastructure.local_repository import (
    holding_decision_runtime_config_repository as _repo,
)
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.incident_envelope_publisher import publish_incident_envelope

logger = logging.getLogger(__name__)

_FALLBACK_MODE = RuntimeConfigMode.LEGACY
_FALLBACK_NOTIFICATION_ENABLED = False
_FALLBACK_FINANCIAL_POLICY_OVERRIDE = FinancialPolicyOverride.FORCE_DEFER_ALL

# フォールバック使用時にHoldingDecisionResult.runtime_config_versionへ保存する予約値。
FALLBACK_RUNTIME_CONFIG_VERSION = -1

# Issue #669(HF-4): RuntimeConfigの取得に失敗してfallbackで動き続けたことを、HF-0契約(#665)で
# USERへ通知する。fail-safe(fallbackで処理を継続し、kill switchは通知しない側へ倒す)は維持する。
# 通知はSNS -> IncidentNotifier -> LINEの独立した経路で、kill switch(notification_enabled)とは
# 無関係に届く(HF4-AC2)。本serviceの本番の呼び出し元は保有監視のLambdaだけのため、job名は固定する
# (incident_message.py::_INTERNAL_NAME_TO_JOBの"holdings-watchlist" = 「保有株チェック」)。
_INCIDENT_SOURCE = "holding_decision_runtime_config"
_INCIDENT_JOB_NAME = "holdings-watchlist"
_INCIDENT_FAILURE_TYPE = "FETCH_FAILED"
_INCIDENT_TOPIC_ENV = "INCIDENT_NOTIFICATION_TOPIC_ARN"
_FAILURE_STAGE_RUNTIME_CONFIG_FETCH = "RUNTIME_CONFIG_FETCH"
_FAILURE_STAGE_KILL_SWITCH_FETCH = "KILL_SWITCH_FETCH"
# 利用者向けの「内容」文はincident_message.py::IncidentContent(PROVISIONAL)に登録する。
_REASON_CODE_RUNTIME_CONFIG_STALE_CACHE_USED = "HOLDINGS_WATCHLIST_RUNTIME_CONFIG_STALE_CACHE_USED"
_REASON_CODE_RUNTIME_CONFIG_FALLBACK_USED = "HOLDINGS_WATCHLIST_RUNTIME_CONFIG_FALLBACK_USED"
_REASON_CODE_KILL_SWITCH_FETCH_FAILED = "HOLDINGS_WATCHLIST_KILL_SWITCH_FETCH_FAILED"


def _notify_handled_failure(failure_stage: str, reason_code: str, now: dt.datetime) -> None:
    """取得失敗によるfallbackをHF-0契約(#665)でUSER通知する。

    envelopeはallowlistのキーのみ(銘柄コード・所有者・stack trace・生のexception messageは
    含めない。`publish_incident_envelope()`が最後の防御として再確認する)。
    `failure_class = HANDLED_FAILURE`のため、GitHub Issueは自動起票されない(HF-0)。
    """
    publish_incident_envelope(
        {
            "source": _INCIDENT_SOURCE,
            "job_name": _INCIDENT_JOB_NAME,
            "failure_stage": failure_stage,
            "failure_type": _INCIDENT_FAILURE_TYPE,
            "reason_code": reason_code,
            "occurred_at": now.isoformat(),
            "failure_class": "HANDLED_FAILURE",
        }
    )


def _notify_handled_failure_safely(failure_stage: str, reason_code: str, now: dt.datetime) -> None:
    """`_notify_handled_failure()`の失敗(SNS権限不足・一時的な障害等)が、fallbackでの処理継続を
    ブロックしないためのラッパー(fail-soft。RuntimeConfigの取得失敗の上に、通知の失敗で
    保有判断全体を止めない)。"""
    try:
        _notify_handled_failure(failure_stage, reason_code, now)
    except Exception:  # noqa: BLE001 - HANDLED_FAILURE通知自体の失敗でfallbackでの処理継続を止めない
        logger.warning(
            "HoldingDecisionRuntimeConfig: failed to publish HANDLED_FAILURE envelope "
            "failure_stage=%s",
            failure_stage,
        )


@dataclass(frozen=True)
class RuntimeConfigLookup:
    config: HoldingDecisionRuntimeConfig
    is_fallback: bool

    @property
    def effective_runtime_config_version(self) -> int:
        return FALLBACK_RUNTIME_CONFIG_VERSION if self.is_fallback else self.config.config_version


class RuntimeConfigAlreadyInitializedError(Exception):
    pass


# --- モジュールレベルキャッシュ(ウォームコンテナ間で共有) ---------------------
_cached_config: HoldingDecisionRuntimeConfig | None = None
_cached_at: dt.datetime | None = None


def _build_fallback_config(now: dt.datetime) -> HoldingDecisionRuntimeConfig:
    return HoldingDecisionRuntimeConfig(
        config_version=FALLBACK_RUNTIME_CONFIG_VERSION,
        mode=_FALLBACK_MODE,
        notification_enabled=_FALLBACK_NOTIFICATION_ENABLED,
        financial_policy_override=_FALLBACK_FINANCIAL_POLICY_OVERRIDE,
        updated_at=now,
        updated_by="__fallback__",
        change_reason="RuntimeConfig取得失敗によるフォールバック",
    )


class HoldingDecisionRuntimeConfigService:
    def __init__(
        self,
        cache_ttl_seconds: int = 60,
        audit_service: AuditService | None = None,
        store_dir: Path | None = None,
        notify_fetch_failures: bool = True,
    ) -> None:
        """`notify_fetch_failures`(Issue #669): `get_config()`/`get_notification_enabled()`は、
        RuntimeConfigの取得に失敗してfallbackを使ったとき、HF-0契約でUSERへ通知する
        (SNS publish。**read名のメソッドが外部状態〔SNS〕へ副作用を持つ**ことの明示)。
        `INCIDENT_NOTIFICATION_TOPIC_ARN`が設定されていない環境(運用者のCLI・ローカル実行)
        では送らない。送りたくない呼び出し元は`False`を渡す。通知の有無でfallbackの
        挙動・戻り値は変わらない。1つのinstanceにつき、reason_codeごとに最大1回だけ送る。
        """
        self._cache_ttl_seconds = cache_ttl_seconds
        self._audit_service = audit_service or AuditService()
        self._store_dir = store_dir
        self._notify_fetch_failures = notify_fetch_failures
        self._notified_reason_codes: set[str] = set()

    def _should_notify(self, reason_code: str) -> bool:
        """このinstanceでこのreason_codeを通知してよいか(同一instanceでは1回だけ)。"""
        if not self._notify_fetch_failures or reason_code in self._notified_reason_codes:
            return False
        if not os.environ.get(_INCIDENT_TOPIC_ENV):
            return False
        self._notified_reason_codes.add(reason_code)
        return True

    def get_config(self, now: dt.datetime | None = None) -> RuntimeConfigLookup:
        global _cached_config, _cached_at
        current_time = now or dt.datetime.now(dt.UTC)

        if (
            _cached_config is not None
            and _cached_at is not None
            and (current_time - _cached_at).total_seconds() < self._cache_ttl_seconds
        ):
            return RuntimeConfigLookup(config=_cached_config, is_fallback=False)

        try:
            fetched = _repo.get(self._store_dir)
        except Exception:
            logger.exception("RuntimeConfigの取得に失敗しました")
            fetched = None

        if fetched is not None:
            _cached_config = fetched
            _cached_at = current_time
            return RuntimeConfigLookup(config=fetched, is_fallback=False)

        # 取得失敗(レコード未作成含む): 直近の正常取得値があればそれを使う
        # (スタレ値の許容。TTLを過ぎていても取得失敗時は優先してこちらを使う)。
        if _cached_config is not None:
            logger.warning("RuntimeConfig取得失敗、直近のキャッシュ値を使用します")
            if self._should_notify(_REASON_CODE_RUNTIME_CONFIG_STALE_CACHE_USED):
                _notify_handled_failure_safely(
                    _FAILURE_STAGE_RUNTIME_CONFIG_FETCH,
                    _REASON_CODE_RUNTIME_CONFIG_STALE_CACHE_USED,
                    current_time,
                )
            return RuntimeConfigLookup(config=_cached_config, is_fallback=False)

        logger.warning(
            "RuntimeConfig取得失敗、正常取得履歴も無いため安全側の既定値へフォールバックします"
        )
        if self._should_notify(_REASON_CODE_RUNTIME_CONFIG_FALLBACK_USED):
            _notify_handled_failure_safely(
                _FAILURE_STAGE_RUNTIME_CONFIG_FETCH,
                _REASON_CODE_RUNTIME_CONFIG_FALLBACK_USED,
                current_time,
            )
        return RuntimeConfigLookup(config=_build_fallback_config(current_time), is_fallback=True)

    def get_notification_enabled(self, now: dt.datetime | None = None) -> bool:
        """kill switch(notification_enabled)を、TTLキャッシュを経由せず毎回取得する。

        kill switchは緊急停止用途(実装プラン修正2)のため、mode等と同じ
        `_cache_ttl_seconds`(既定60秒)のキャッシュに乗せてしまうと、運用者が
        `holding-decision kill-switch on`で停止させても、既に稼働中のバッチが
        最大でキャッシュ有効期間ぶん停止操作に気づかない恐れがある。この
        メソッドはget_config()のモジュールレベルキャッシュを一切参照せず、
        呼び出しのたびにリポジトリへ直接問い合わせる。取得に失敗した場合は
        安全側(通知しない)へフォールバックする(get_config()のフォールバック
        方針と同じ考え方)。

        取得に失敗した場合(例外、またはレコード未作成)は、安全側へ倒すとともに、
        USERへHANDLED_FAILUREを通知する(Issue #669。`notify_fetch_failures`の説明を参照)。
        この通知は投資判断の通知の経路(kill switchで止まる側)とは独立しており、kill switchが
        「通知しない」へ倒れた状況でも届く。
        """
        try:
            fetched = _repo.get(self._store_dir)
        except Exception:
            logger.exception(
                "kill switch(notification_enabled)の取得に失敗しました。"
                "安全側(通知しない)へフォールバックします"
            )
            if self._should_notify(_REASON_CODE_KILL_SWITCH_FETCH_FAILED):
                _notify_handled_failure_safely(
                    _FAILURE_STAGE_KILL_SWITCH_FETCH,
                    _REASON_CODE_KILL_SWITCH_FETCH_FAILED,
                    now or dt.datetime.now(dt.UTC),
                )
            return _FALLBACK_NOTIFICATION_ENABLED
        if fetched is None:
            logger.warning(
                "RuntimeConfig未初期化のため、kill switchは安全側(通知しない)として扱います"
            )
            if self._should_notify(_REASON_CODE_KILL_SWITCH_FETCH_FAILED):
                _notify_handled_failure_safely(
                    _FAILURE_STAGE_KILL_SWITCH_FETCH,
                    _REASON_CODE_KILL_SWITCH_FETCH_FAILED,
                    now or dt.datetime.now(dt.UTC),
                )
            return _FALLBACK_NOTIFICATION_ENABLED
        return fetched.notification_enabled

    def init_config(
        self,
        updated_by: str,
        change_reason: str = "初期化",
        mode: RuntimeConfigMode = RuntimeConfigMode.LEGACY,
        notification_enabled: bool = False,
        financial_policy_override: FinancialPolicyOverride = FinancialPolicyOverride.DEFAULT,
        now: dt.datetime | None = None,
    ) -> HoldingDecisionRuntimeConfig:
        created = _repo.init(
            mode=mode,
            notification_enabled=notification_enabled,
            financial_policy_override=financial_policy_override,
            updated_by=updated_by,
            change_reason=change_reason,
            now=now,
            store_dir=self._store_dir,
        )
        if created is None:
            raise RuntimeConfigAlreadyInitializedError(
                "HoldingDecisionRuntimeConfigは既に初期化済みです。"
                "変更するにはset-mode等の通常コマンドを使用してください。"
            )
        self._invalidate_cache()
        self._record_audit(before=None, after=created)
        return created

    def update_config(
        self,
        expected_config_version: int,
        mode: RuntimeConfigMode,
        notification_enabled: bool,
        financial_policy_override: FinancialPolicyOverride,
        updated_by: str,
        change_reason: str,
        now: dt.datetime | None = None,
    ) -> HoldingDecisionRuntimeConfig:
        before = _repo.get(self._store_dir)
        updated = _repo.update(
            expected_config_version=expected_config_version,
            mode=mode,
            notification_enabled=notification_enabled,
            financial_policy_override=financial_policy_override,
            updated_by=updated_by,
            change_reason=change_reason,
            now=now,
            store_dir=self._store_dir,
        )
        self._invalidate_cache()
        self._record_audit(before=before, after=updated)
        return updated

    def _invalidate_cache(self) -> None:
        global _cached_config, _cached_at
        _cached_config = None
        _cached_at = None

    def _record_audit(
        self,
        before: HoldingDecisionRuntimeConfig | None,
        after: HoldingDecisionRuntimeConfig,
    ) -> None:
        # 監査ログ記録は更新成功後にのみ行う。記録自体が失敗しても、既に成功した
        # 設定変更をロールバックしない(操作は成功扱いのまま警告ログのみ残す)。
        try:
            self._audit_service.record(
                decision_type="holding_decision_runtime_config",
                stock_code=None,
                input_values={
                    "before_mode": before.mode.value if before else None,
                    "before_notification_enabled": before.notification_enabled if before else None,
                    "before_financial_policy_override": (
                        before.financial_policy_override.value if before else None
                    ),
                    "expected_config_version": before.config_version if before else None,
                },
                calculation_formulas={},
                output_values={
                    "after_mode": after.mode.value,
                    "after_notification_enabled": after.notification_enabled,
                    "after_financial_policy_override": after.financial_policy_override.value,
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
                "RuntimeConfig変更の監査ログ記録に失敗しました(設定変更自体は正常に反映済みです)"
            )
