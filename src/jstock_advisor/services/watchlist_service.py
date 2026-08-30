"""ウォッチリスト管理サービス(要求仕様3節 watchlist_service、6節)。"""

from __future__ import annotations

import datetime as dt
from typing import Any

from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.infrastructure.local_repository.watchlist_repository import WatchlistRepository

# --- Issue #58 Phase B1: field ownership -------------------------------------
# ユーザーが登録・編集してよいfield(user-owned)。会話型UI・CSV取込・CLIの
# いずれの経路からも、ここに無いfieldをuser patchで変更してはならない。
USER_OWNED_FIELDS: frozenset[str] = frozenset(
    {
        "stock_name",
        "reason",
        "memo",
        "priority",
        "notify_enabled",
        "benefit_interest",
        "desired_total_yield_pct",
        "desired_buy_price",
    }
)

# 作成時に確定し、以後変更してはならないfield。
IMMUTABLE_FIELDS: frozenset[str] = frozenset({"stock_code", "created_at"})

# システムが管理するfield。自動追加(AUTO_SCREENING)・自動メンテナンスだけが
# 設定してよく、ユーザー操作(再登録・CSV再取込・CLI編集)では一切変更しない。
# `updated_at` は本サービスがwrite時に管理するため、callerからの指定も受け付けない。
SYSTEM_OWNED_FIELDS: frozenset[str] = frozenset(
    {
        "registration_source",
        "registration_policy",
        "registration_batch_id",
        "last_screened_at",
        "last_qualified_at",
        "consecutive_not_qualified_count",
        "last_monitoring_score",
        "last_matched_target_types",
        "last_screening_result",
        "last_screening_policy",
        "removal_candidate_since",
        "updated_at",
    }
)


class WatchlistFieldOwnershipError(ValueError):
    """user経路から変更が許されないfieldを指定した場合に送出する(Issue #58)。"""


def _reject_non_user_fields(patch: dict[str, Any]) -> None:
    """user patchのallowlist検証。

    unknown field だけでなく、**既知だがownership違反のfield**も拒否する。
    `WatchlistItem` は `extra="forbid"` のため unknown は後段でも検出できるが、
    `registration_source` 等の既知fieldは検出できないため、ここで明示的に弾く。
    """
    violations = sorted(set(patch) - USER_OWNED_FIELDS)
    if not violations:
        return
    detail = []
    for name in violations:
        if name in IMMUTABLE_FIELDS:
            detail.append(f"{name}(作成後は変更できません)")
        elif name in SYSTEM_OWNED_FIELDS:
            detail.append(f"{name}(システム管理項目のため利用者操作では変更できません)")
        else:
            detail.append(f"{name}(不明な項目です)")
    raise WatchlistFieldOwnershipError(
        "ウォッチリストの利用者操作では変更できない項目が指定されました: " + " / ".join(detail)
    )


class WatchlistService:
    def __init__(self, repository: WatchlistRepository | None = None) -> None:
        self._repository = repository or WatchlistRepository()

    def list_items(self) -> list[WatchlistItem]:
        return self._repository.list_all()

    def get_item(self, stock_code: str) -> WatchlistItem | None:
        return self._repository.get(stock_code)

    def add_item(
        self,
        stock_code: str,
        patch: dict[str, Any] | None = None,
    ) -> WatchlistItem:
        """ウォッチリストへ登録する(新規はcreate、既存はmerge)。

        `build_add_item_plan()`の結果をその場で永続化する薄いラッパー。

        **Issue #58: 既存itemに対する実質的な変更が無い場合は書き込まない。**
        `updated_at`は「レコード全体の最終更新日時」であり、
        何も変わっていない再登録を更新として記録する必要がないため。
        """
        now = dt.datetime.now(dt.UTC)
        patch = dict(patch or {})
        _reject_non_user_fields(patch)
        existing = self._repository.get(stock_code)
        if existing is None:
            item = WatchlistItem(
                stock_code=stock_code,
                created_at=now,
                updated_at=now,
                **patch,
            )
            self._repository.upsert(item)
            return item

        effective = {k: v for k, v in patch.items() if getattr(existing, k) != v}
        if not effective:
            # no-op(実質的な変更なし)。writeもupdated_atの前進も行わない。
            return existing
        item = existing.model_copy(update={**effective, "updated_at": now})
        self._repository.upsert(item)
        return item

    def build_add_item_plan(
        self,
        stock_code: str,
        patch: dict[str, Any] | None = None,
    ) -> WatchlistItem:
        """登録(新規)または再登録(既存)後のWatchlistItemを、永続化せずに返す。

        LINEボタン起点会話型UI(実装プランv2 3節)がTransactWriteItemsで
        Put(無条件)するための「書き込み予定の完成形」を組み立てる。

        **Issue #58: 既存itemがある場合は全置換せずmergeする。**
        従来は`created_at`だけを引き継ぎ、他はすべて引数(未指定ならdefault)から
        再構築して`upsert`していたため、再登録するだけで
        `registration_source`(AUTO_SCREENING→MANUAL)・自動メンテナンス状態・
        ユーザーが設定したmemo/priority/notify_enabled等が既定値へ戻っていた。

        `patch`には**利用者が明示的に指定したuser-owned fieldだけ**を入れること。
        「未指定」を関数defaultへ変換してから渡してはならない
        (未指定と明示指定が区別できなくなり、同じ破壊が再発する)。
        """
        now = dt.datetime.now(dt.UTC)
        patch = dict(patch or {})
        _reject_non_user_fields(patch)
        existing = self._repository.get(stock_code)
        if existing is None:
            return WatchlistItem(
                stock_code=stock_code,
                created_at=now,
                updated_at=now,
                **patch,
            )
        effective = {k: v for k, v in patch.items() if getattr(existing, k) != v}
        if not effective:
            # no-op(実質的な変更なし)。呼び出し側がこれをPutしても内容は変わらず、
            # `updated_at`も進まない(単なる再登録を更新として記録しない)。
            return existing
        return existing.model_copy(update={**effective, "updated_at": now})

    def update_item(self, stock_code: str, **fields: Any) -> WatchlistItem:
        """既存itemのuser-owned fieldを明示的に更新する(CLI editの正本)。

        Issue #58: allowlistを強制する。`registration_source`等の
        システム管理項目・`created_at`等の不変項目は、既知fieldであっても拒否する
        (従来は`**fields: Any`をそのままmergeしており上書きできた)。
        """
        _reject_non_user_fields(fields)
        existing = self._repository.get(stock_code)
        if existing is None:
            raise ValueError(f"銘柄コード{stock_code}はウォッチリストに登録されていません")
        effective = {k: v for k, v in fields.items() if getattr(existing, k) != v}
        if not effective:
            return existing
        updated = existing.model_copy(
            update={**effective, "updated_at": dt.datetime.now(dt.UTC)}
        )
        self._repository.upsert(updated)
        return updated

    def delete_item(self, stock_code: str) -> bool:
        return self._repository.delete(stock_code)
