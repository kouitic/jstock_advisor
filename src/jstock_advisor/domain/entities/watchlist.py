"""ウォッチリスト(要求仕様6節)。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from pydantic import Field

from jstock_advisor.domain.entities.base import Entity
from jstock_advisor.domain.entities.enums import Priority, WatchlistRegistrationSource


class WatchlistItem(Entity):
    stock_code: str
    stock_name: str | None = None
    reason: str | None = None
    desired_total_yield_pct: float | None = None
    desired_buy_price: Decimal | None = None
    benefit_interest: bool = False
    priority: Priority = Priority.MEDIUM
    notify_enabled: bool = True
    memo: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime

    # --- ウォッチリスト自動追加機能で追加。既存レコードには存在しないため、
    # 後方互換のためデフォルト値(手動登録)を持たせる ---
    registration_source: WatchlistRegistrationSource = WatchlistRegistrationSource.MANUAL
    registration_policy: str | None = None
    # --- 運用ハードニング第3弾2節で追加。AUTO_SCREENING追加時の発行元batch_id。
    # add_if_new()成功後・finalize側のrepository_results永続化前に障害が起きた
    # 場合、再試行時にadd_if_new()がFalseを返しても、この値が今回のbatch_idと
    # 一致すれば「このバッチ自身が過去の試行で既に追加していた」と判定でき、
    # 誤ってSKIPPED_EXISTING(＝通知対象漏れ)にしないための復元キー。既存レコード
    # には存在しないためNoneとして読み込める(手動登録・旧バージョンのレコード) ---
    registration_batch_id: str | None = None

    # --- ウォッチリスト自動運用の改善(自動メンテナンス、2026-08)で追加。
    # AUTO_SCREENING銘柄のみ意味を持つ。既存レコードには存在しないため、
    # 全てOptional/デフォルト値付きで後方互換を保つ ---
    last_screened_at: dt.datetime | None = None
    last_qualified_at: dt.datetime | None = None
    consecutive_not_qualified_count: int = 0
    last_monitoring_score: float | None = None
    last_matched_target_types: list[str] = Field(default_factory=list)
    last_screening_result: str | None = None
    last_screening_policy: str | None = None
    # 直近で非該当と判定され始めた時刻(初回非該当時にNone→now、連続非該当中は
    # 変更しない、再度合格した時点でNoneへ戻す)。3回連続非該当による削除判定の
    # AND条件(minimum_not_qualified_span_days)にそのまま使う実判定用フィールド
    # であり、単なる表示用途ではない(services/watchlist_maintenance_service.py参照)。
    removal_candidate_since: dt.datetime | None = None


class RotationState(Entity):
    """ウォッチリスト新規候補選定の永続ラウンドロビン方式(2026-08)における、
    唯一の永続カーソル(単一行、rotation_id="default"固定)。

    pointer_versionによる楽観ロックで更新する(baseline_pointer.pyの
    InvestmentThesisBaselinePointerと同じ技法)。last_market_segment/
    last_stock_codeが「次回の選択をどこから始めるか」を表すcursorの実体であり、
    それ以外のフィールドは監査・CLI表示専用(判定ロジックには使わない)。
    """

    rotation_id: str
    pointer_version: int
    last_market_segment: str | None = None
    last_stock_code: str | None = None
    cycle_number: int = 1
    cycle_progress_selected_count: int = 0
    universe_signature: str | None = None
    last_started_at: dt.datetime | None = None
    last_completed_at: dt.datetime | None = None


class WatchlistRemovalHistory(Entity):
    """AUTO_SCREENING銘柄の自動削除における、再追加クールダウン判定専用の
    軽量ルックアップ(単一行=1銘柄、最新の削除のみ保持、計画Part C-4)。

    完全な削除履歴(複数回分)は監査ログ(watchlist_screening_audit.py の
    DECISION_TYPE_REMOVAL)が正本であり、このテーブルは
    `cooldown_until`が未来かどうかの高速判定専用。DynamoDB環境では
    `readd_cooldown_days`相当のTTLを付与し、クールダウン終了後は自動的に
    消える(WatchlistRemovalHistoryRepository参照)。
    """

    stock_code: str
    removed_at: dt.datetime
    removal_reason: str
    removal_category: str  # "IMMEDIATE" | "CONSECUTIVE_NOT_QUALIFIED"
    cooldown_until: dt.datetime
