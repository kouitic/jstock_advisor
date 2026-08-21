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
from pathlib import Path

from jstock_advisor.domain.entities.trading_pause import TradingPauseConfig
from jstock_advisor.infrastructure.aws import trading_pause_config as _repo
from jstock_advisor.services.audit_service import AuditService

logger = logging.getLogger(__name__)

# 取得失敗時の安全側既定値: 一時停止扱い(誤って新規取引を受け付けない)。
_FALLBACK_PAUSE_BUY_SELL = True


class TradingPauseAlreadyInitializedError(Exception):
    pass


class TradingPauseService:
    def __init__(
        self,
        audit_service: AuditService | None = None,
        store_dir: Path | None = None,
    ) -> None:
        self._audit_service = audit_service or AuditService()
        self._store_dir = store_dir

    def is_buy_sell_paused(self) -> bool:
        try:
            fetched = _repo.get(self._store_dir)
        except Exception:
            logger.exception(
                "TradingPauseConfigの取得に失敗しました。安全側(一時停止)として扱います"
            )
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
