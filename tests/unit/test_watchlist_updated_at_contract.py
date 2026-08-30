"""Issue #58 Phase B2(F-O5): `updated_at` の契約を write 経路横断で固定する。

## 契約

**`WatchlistItem.updated_at` は「そのレコードが最後に実質的に更新された時刻」。**
利用者の最終編集日時ではない。したがって、レコードを実質的に更新して永続化する
経路では、それが利用者操作でもシステム保守でも `updated_at` が進む。

従来 `watchlist_maintenance_service` は `model_copy()` で system-owned state を
更新しながら `updated_at` を設定しておらず、自動メンテナンスで
`last_screened_at` 等が変わっても最終更新日時が古いままだった(F-O5)。

## このファイルの役割

`test_watchlist_maintenance_service.py` は maintenance の **state 遷移**を
担保している(24件)。本ファイルはそれを重複させず、
**`updated_at` の契約**と **maintenance が user-owned field を壊さないこと**に
絞って検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import Priority, WatchlistRegistrationSource
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.infrastructure.local_repository.watchlist_repository import (
    WatchlistRepository,
)
from jstock_advisor.services.watchlist_maintenance_service import (
    MaintenanceOutcome,
    MaintenanceScreeningSummary,
    evaluate_maintenance_decision,
)
from jstock_advisor.services.watchlist_service import WatchlistService

from .test_watchlist_maintenance_service import _CONFIG, _summary

_NOW = dt.datetime(2026, 8, 30, 7, 0, tzinfo=dt.UTC)
_OLD = _NOW - dt.timedelta(days=7)

# maintenance の結果が **永続化される** outcome。
# 削除系(IMMEDIATE_REMOVAL / CONSECUTIVE_NOT_QUALIFIED_REMOVAL)は
# finalizer が `watchlist_repo.delete()` 分岐へ入るため updated_item を保存しない。
PERSISTED_OUTCOMES = frozenset({MaintenanceOutcome.KEEP, MaintenanceOutcome.DATA_UNAVAILABLE})

# 利用者が所有し、maintenance が変更してはならない項目(Phase B1 の定義)
_USER_OWNED = (
    "stock_name",
    "reason",
    "memo",
    "priority",
    "notify_enabled",
    "benefit_interest",
    "desired_total_yield_pct",
    "desired_buy_price",
)


def _item_with_user_data() -> WatchlistItem:
    """利用者が編集済みの AUTO_SCREENING 銘柄。"""
    return WatchlistItem(
        stock_code="1111",
        stock_name="テスト銘柄",
        reason="高配当",
        memo="決算後に再確認",
        priority=Priority.HIGH,
        notify_enabled=False,
        benefit_interest=True,
        desired_total_yield_pct=4.5,
        desired_buy_price=Decimal("1200"),
        registration_source=WatchlistRegistrationSource.AUTO_SCREENING,
        registration_policy="multi_style_monitoring",
        registration_batch_id="batch-orig",
        created_at=_NOW - dt.timedelta(days=120),
        updated_at=_OLD,
    )


# 永続化される outcome を実際に発生させる入力。
# **新しい永続化対象 outcome が追加されたら、ここへ追記しないと
# `test_all_persisted_outcomes_are_covered` が失敗する。**
_PersistedCase = tuple[str, MaintenanceOutcome, "MaintenanceScreeningSummary | None"]
_PERSISTED_CASES: tuple[_PersistedCase, ...] = (
    ("data_unavailable", MaintenanceOutcome.DATA_UNAVAILABLE, None),
    (
        "keep_passed",
        MaintenanceOutcome.KEEP,
        _summary(passed=True, matched_target_types=["INCOME"]),
    ),
    ("keep_first_failure", MaintenanceOutcome.KEEP, _summary(passed=False)),
)


def _decide(summary: MaintenanceScreeningSummary | None, item: WatchlistItem | None = None):
    return evaluate_maintenance_decision(item or _item_with_user_data(), summary, _CONFIG, _NOW)


# ============================================================================
# 必須1: maintenance outcome 横断
# ============================================================================


@pytest.mark.parametrize(
    ("label", "expected_outcome", "summary"), _PERSISTED_CASES, ids=lambda v: getattr(v, "value", v)
)
def test_persisted_maintenance_outcome_advances_updated_at(
    label: str,
    expected_outcome: MaintenanceOutcome,
    summary: MaintenanceScreeningSummary | None,
) -> None:
    """永続化される全 outcome で `updated_at` が now まで進む(F-O5)。"""
    item = _item_with_user_data()

    decision = _decide(summary, item)

    assert decision.outcome is expected_outcome, label
    assert decision.updated_item.updated_at == _NOW
    assert decision.updated_item.updated_at != item.updated_at


@pytest.mark.parametrize(
    ("label", "expected_outcome", "summary"), _PERSISTED_CASES, ids=lambda v: getattr(v, "value", v)
)
def test_persisted_maintenance_outcome_updates_system_state(
    label: str,
    expected_outcome: MaintenanceOutcome,
    summary: MaintenanceScreeningSummary | None,
) -> None:
    """`updated_at` 以外にも実際に system state が更新されている。

    「`updated_at` だけが進む」実装になっていないこと(=更新の実体があること)を
    確認する。個々の遷移内容は test_watchlist_maintenance_service.py が担保する。
    """
    item = _item_with_user_data()

    updated = _decide(summary, item).updated_item

    assert updated.last_screened_at == _NOW
    changed = {
        f for f in WatchlistItem.model_fields if getattr(item, f) != getattr(updated, f)
    }
    assert changed - {"updated_at"}, f"{label}: updated_at 以外の変更が無い"


def test_all_persisted_outcomes_are_covered() -> None:
    """永続化対象 outcome を取りこぼしなく検証していること。

    新しい `MaintenanceOutcome` を追加して永続化対象にした場合、
    `PERSISTED_OUTCOMES` と `_PERSISTED_CASES` の両方を更新しないと失敗する。
    これにより `updated_at` の設定漏れを構造的に検出する。
    """
    covered = {outcome for _label, outcome, _summary in _PERSISTED_CASES}

    assert covered == PERSISTED_OUTCOMES, (
        "永続化対象の MaintenanceOutcome が増減している。"
        " _PERSISTED_CASES へ追加し、その outcome でも updated_at が進むことを確認すること"
    )
    assert set(MaintenanceOutcome) > PERSISTED_OUTCOMES, (
        "PERSISTED_OUTCOMES は MaintenanceOutcome の真部分集合であるべき"
        "(削除系は永続化されない)"
    )


# ============================================================================
# 必須2: field preservation(maintenance は利用者の項目を壊さない)
# ============================================================================


@pytest.mark.parametrize(
    ("label", "expected_outcome", "summary"), _PERSISTED_CASES, ids=lambda v: getattr(v, "value", v)
)
def test_maintenance_preserves_user_owned_and_immutable_fields(
    label: str,
    expected_outcome: MaintenanceOutcome,
    summary: MaintenanceScreeningSummary | None,
) -> None:
    """user-owned / registration_source / created_at / stock_code は不変。"""
    item = _item_with_user_data()

    updated = _decide(summary, item).updated_item

    for field in _USER_OWNED:
        assert getattr(updated, field) == getattr(item, field), f"{label}: {field} が変更された"
    assert updated.registration_source is item.registration_source
    assert updated.registration_policy == item.registration_policy
    assert updated.registration_batch_id == item.registration_batch_id
    assert updated.created_at == item.created_at
    assert updated.stock_code == item.stock_code


# ============================================================================
# 必須3: write 経路横断の contract
# ============================================================================


def _service(tmp_path: Path) -> tuple[WatchlistService, WatchlistItem]:
    repository = WatchlistRepository(store_dir=tmp_path)
    item = _item_with_user_data()
    repository.upsert(item)
    return WatchlistService(repository), item


def test_user_patch_advances_updated_at_when_value_changes(tmp_path: Path) -> None:
    """利用者操作:実質的な更新なら `updated_at` が進む(B1で実装)。"""
    service, before = _service(tmp_path)

    after = service.add_item(stock_code="1111", patch={"memo": "新しいメモ"})

    assert after.memo == "新しいメモ"
    assert after.updated_at > before.updated_at


def test_cli_update_advances_updated_at_when_value_changes(tmp_path: Path) -> None:
    service, before = _service(tmp_path)

    after = service.update_item("1111", priority=Priority.LOW)

    assert after.priority is Priority.LOW
    assert after.updated_at > before.updated_at


def test_maintenance_advances_updated_at_when_value_changes() -> None:
    """システム保守でも、実質的な更新なら同じ契約が成り立つ(F-O5の是正)。"""
    item = _item_with_user_data()

    updated = _decide(_summary(passed=True)).updated_item

    assert updated.last_screened_at == _NOW
    assert updated.updated_at > item.updated_at


def test_user_patch_does_not_advance_updated_at_without_change(tmp_path: Path) -> None:
    """実質的な変更が無ければ進めない(契約の裏側)。

    maintenance には対応する状況が存在しない(`last_screened_at` が必ず変わるため
    真の no-op は発生しない)。そのため maintenance 側の no-op 判定は設けていない。
    """
    service, before = _service(tmp_path)

    after = service.add_item(stock_code="1111", patch={})

    assert after.updated_at == before.updated_at
