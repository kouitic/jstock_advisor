"""Issue #58 Phase B1: 既存watchlist itemの再登録で既存fieldを破壊しない。

従来は `build_add_item_plan()` が既存レコードから `created_at` だけを引き継ぎ、
他はすべて引数(未指定なら関数default)から `WatchlistItem` を再構築して
`upsert` していた。そのため既存itemを再登録するだけで **22 field 中 20 field**
(`stock_code` と `created_at` 以外すべて)が既定値へ戻っていた。

とくに危険だったのは:

- `registration_source` が AUTO_SCREENING → MANUAL へ暗黙昇格する
  (自動メンテナンスの保護対象が変わり、登録経緯の監査情報も失われる)
- `consecutive_not_qualified_count` / `removal_candidate_since` が
  リセットされ、自動削除の判定がやり直しになる
- ユーザーが設定した memo / priority / notify_enabled が既定値へ戻る

本ファイルは「どの操作が何を変更してよいか」(field ownership)を固定する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import (
    Priority,
    WatchlistRegistrationSource,
)
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
)
from jstock_advisor.services.watchlist_service import (
    WatchlistFieldOwnershipError,
    WatchlistService,
)

_NOW = dt.datetime(2026, 8, 30, tzinfo=dt.UTC)

# システムが管理し、利用者操作では決して変わってはならない項目
_SYSTEM_FIELDS = (
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
)


def _auto_screening_item() -> WatchlistItem:
    """自動追加され、自動メンテナンスが進行し、利用者も編集した状態のitem。

    `consecutive_not_qualified_count=2` / `removal_candidate_since=40日前` は
    「あと1回非該当なら自動削除の候補になる」状態を表す(#58 のACに対応)。
    """
    return WatchlistItem(
        stock_code="9999",
        stock_name="テスト株式会社",
        reason="高配当",
        memo="決算後に再確認",
        priority=Priority.HIGH,
        notify_enabled=False,
        benefit_interest=True,
        desired_buy_price=Decimal("1200"),
        desired_total_yield_pct=4.5,
        registration_source=WatchlistRegistrationSource.AUTO_SCREENING,
        registration_policy="multi_style_monitoring",
        registration_batch_id="batch-orig",
        last_screened_at=_NOW - dt.timedelta(days=7),
        last_qualified_at=_NOW - dt.timedelta(days=60),
        consecutive_not_qualified_count=2,
        removal_candidate_since=_NOW - dt.timedelta(days=40),
        last_monitoring_score=61.5,
        last_matched_target_types=["INCOME"],
        last_screening_result="FAILED",
        last_screening_policy="multi_style_monitoring",
        created_at=_NOW - dt.timedelta(days=120),
        updated_at=_NOW - dt.timedelta(days=7),
    )


@pytest.fixture
def service(tmp_path: Path) -> WatchlistService:
    return WatchlistService(WatchlistRepository(store_dir=tmp_path))


@pytest.fixture
def seeded(service: WatchlistService) -> WatchlistItem:
    item = _auto_screening_item()
    service._repository.upsert(item)  # noqa: SLF001 - 前提データの投入
    return item


def _assert_system_state_preserved(before: WatchlistItem, after: WatchlistItem) -> None:
    for field in _SYSTEM_FIELDS:
        assert getattr(after, field) == getattr(before, field), (
            f"system管理項目 {field} が利用者操作で変更された"
        )
    assert after.stock_code == before.stock_code
    assert after.created_at == before.created_at


# ============================================================================
# T1 / T2 / T3: conversation 再登録
# ============================================================================


def test_conversation_re_registration_preserves_all_system_fields(
    service: WatchlistService, seeded: WatchlistItem
) -> None:
    """T1: conversationはstock_codeしか渡さない。system stateを一切壊さない。"""
    plan = service.build_add_item_plan(stock_code="9999")

    _assert_system_state_preserved(seeded, plan)
    assert plan.registration_source is WatchlistRegistrationSource.AUTO_SCREENING
    assert plan.consecutive_not_qualified_count == 2
    assert plan.removal_candidate_since == seeded.removal_candidate_since


def test_conversation_re_registration_preserves_user_fields(
    service: WatchlistService, seeded: WatchlistItem
) -> None:
    """T2: 利用者が設定した項目も既定値へ戻さない。"""
    plan = service.build_add_item_plan(stock_code="9999")

    assert plan.memo == "決算後に再確認"
    assert plan.priority is Priority.HIGH
    assert plan.notify_enabled is False
    assert plan.benefit_interest is True
    assert plan.desired_buy_price == Decimal("1200")
    assert plan.desired_total_yield_pct == 4.5
    assert plan.stock_name == "テスト株式会社"
    assert plan.reason == "高配当"


def test_conversation_no_op_does_not_advance_updated_at(
    service: WatchlistService, seeded: WatchlistItem
) -> None:
    """T3: 実質的な変更が無い再登録では updated_at を進めない。

    `updated_at` は「レコード全体の最終更新日時」であり、
    何も変わっていない再登録を更新として記録しない。
    """
    plan = service.build_add_item_plan(stock_code="9999")

    assert plan.updated_at == seeded.updated_at
    assert plan == seeded


def test_add_item_no_op_does_not_write(
    service: WatchlistService, seeded: WatchlistItem
) -> None:
    """no-op の add_item は repository へ書き込まない。"""
    result = service.add_item(stock_code="9999", patch={})

    assert result == seeded
    assert service.get_item("9999") == seeded


# ============================================================================
# T4 / T5 / T6 / T7 / T8: CSV 再取込
# ============================================================================


def test_csv_missing_column_preserves_existing(
    service: WatchlistService, seeded: WatchlistItem
) -> None:
    """T4: 列そのものが無い場合は既存値を保持する。"""
    result = service.add_item(stock_code="9999", patch={})

    assert result.priority is Priority.HIGH
    assert result.notify_enabled is False
    assert result.memo == "決算後に再確認"


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
def test_csv_empty_or_whitespace_cell_preserves_existing(
    service: WatchlistService, seeded: WatchlistItem, raw: str
) -> None:
    """T5 / T6: 空セル・空白のみのセルは「未指定」であり、クリア要求ではない。

    CSVから値をクリアする機能は提供しない(#58 の確定契約)。
    parserは非空の明示値だけをpatchへ積むため、ここではpatchが空になる。
    """
    patch = {"memo": raw.strip()} if raw.strip() else {}

    result = service.add_item(stock_code="9999", patch=patch)

    assert result.memo == "決算後に再確認"


def test_csv_explicit_value_updates_only_that_field(
    service: WatchlistService, seeded: WatchlistItem
) -> None:
    """T7: 非空の明示値は更新する。ただし指定した項目だけ。"""
    result = service.add_item(stock_code="9999", patch={"memo": "新しいメモ"})

    assert result.memo == "新しいメモ"
    assert result.priority is Priority.HIGH
    assert result.notify_enabled is False
    assert result.updated_at != seeded.updated_at


def test_csv_update_preserves_system_fields(
    service: WatchlistService, seeded: WatchlistItem
) -> None:
    """T8 / T9 / T10: user項目を更新してもsystem stateとregistration_sourceは不変。"""
    result = service.add_item(
        stock_code="9999", patch={"memo": "新しいメモ", "priority": Priority.LOW}
    )

    _assert_system_state_preserved(seeded, result)
    assert result.registration_source is WatchlistRegistrationSource.AUTO_SCREENING
    assert result.consecutive_not_qualified_count == 2
    assert result.removal_candidate_since == seeded.removal_candidate_since


# ============================================================================
# T11 / T12: ownership enforcement
# ============================================================================


@pytest.mark.parametrize("field", _SYSTEM_FIELDS)
def test_user_patch_cannot_change_system_fields(
    service: WatchlistService, seeded: WatchlistItem, field: str
) -> None:
    """T11: 既知fieldであってもsystem管理項目はuser経路から変更できない。"""
    with pytest.raises(WatchlistFieldOwnershipError, match=field):
        service.add_item(stock_code="9999", patch={field: None})

    with pytest.raises(WatchlistFieldOwnershipError, match=field):
        service.update_item("9999", **{field: None})


@pytest.mark.parametrize("field", ["stock_code", "created_at"])
def test_user_patch_cannot_change_immutable_fields(
    service: WatchlistService, seeded: WatchlistItem, field: str
) -> None:
    """T12: 作成時に確定する項目は変更できない。

    patch経路(会話・CSV・CLI add が使う)で検証する。`update_item()` は
    `stock_code` を位置引数に持つため、そちらでは呼び出し自体が成立しない。
    """
    with pytest.raises(WatchlistFieldOwnershipError, match=field):
        service.add_item(stock_code="9999", patch={field: "0000"})


def test_update_item_cannot_change_created_at(
    service: WatchlistService, seeded: WatchlistItem
) -> None:
    with pytest.raises(WatchlistFieldOwnershipError, match="created_at"):
        service.update_item("9999", created_at=_NOW)


def test_user_patch_rejects_unknown_field(
    service: WatchlistService, seeded: WatchlistItem
) -> None:
    with pytest.raises(WatchlistFieldOwnershipError, match="unknown_field"):
        service.add_item(stock_code="9999", patch={"unknown_field": 1})


def test_updated_at_cannot_be_set_by_caller(
    service: WatchlistService, seeded: WatchlistItem
) -> None:
    """updated_at はserviceがwrite時に管理する(caller指定不可)。"""
    with pytest.raises(WatchlistFieldOwnershipError, match="updated_at"):
        service.update_item("9999", updated_at=_NOW)


# ============================================================================
# T16: 新規作成は従来どおり
# ============================================================================


def test_new_item_is_created_normally(service: WatchlistService) -> None:
    """T16: 未登録銘柄は従来どおり作成できる(MANUAL既定)。"""
    item = service.add_item(
        stock_code="7203", patch={"stock_name": "トヨタ自動車", "priority": Priority.HIGH}
    )

    assert item.stock_code == "7203"
    assert item.stock_name == "トヨタ自動車"
    assert item.priority is Priority.HIGH
    assert item.registration_source is WatchlistRegistrationSource.MANUAL
    assert item.created_at == item.updated_at


def test_new_item_plan_is_created_normally(service: WatchlistService) -> None:
    plan = service.build_add_item_plan(stock_code="7203")

    assert plan.stock_code == "7203"
    assert plan.registration_source is WatchlistRegistrationSource.MANUAL
