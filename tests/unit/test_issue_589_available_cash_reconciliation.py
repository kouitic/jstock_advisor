"""Issue #589(#128 A2): owner単位のavailable_cash棚卸し更新(reconciliation)。

trade連携によるcash増減(TRADE_UPDATE)・LINE/CLI UI・insufficient cash guard
はいずれもscope外(#589本文どおり)。`AvailableCashService.reconcile()`/`get()`
のsemanticsのみを固定する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from jstock_advisor.domain.entities.available_cash import AvailableCash
from jstock_advisor.domain.entities.enums import AvailableCashUpdateType
from jstock_advisor.infrastructure.local_repository.available_cash_repository import (
    AvailableCashRepository,
)
from jstock_advisor.services.available_cash_service import (
    AvailableCashReconciliationExhaustedError,
    AvailableCashService,
)

_NOW = dt.datetime(2026, 9, 26, 9, 0, tzinfo=dt.UTC)
_LATER = dt.datetime(2026, 9, 26, 10, 0, tzinfo=dt.UTC)


# --- T1: owner正規化(AC1) ------------------------------------------------------------


def test_t1_full_width_and_half_width_owner_reconcile_to_the_same_record(tmp_path) -> None:
    """全角/半角混じりの入力揺れが、既存normalize_and_validate_owner()により
    同一ownerへ正規化されることを固定する(新しい正規化ロジックを作らない)。"""
    service = AvailableCashService(store_dir=tmp_path)

    service.reconcile("owner-ａ", Decimal("1000"), _NOW)  # 全角の"a"を含む
    result = service.reconcile("owner-a", Decimal("1500"), _LATER)  # 半角

    assert result.owner == "owner-a"
    assert service.get("owner-a").available_cash == Decimal("1500")
    # 2レコードに分裂していないことを確認する。
    repo = AvailableCashRepository(store_dir=tmp_path)
    assert len(repo.list_all()) == 1


def test_t1b_invalid_owner_is_rejected_via_existing_validation(tmp_path) -> None:
    from jstock_advisor.domain.entities.owner import InvalidOwnerError

    service = AvailableCashService(store_dir=tmp_path)
    with pytest.raises(InvalidOwnerError):
        service.reconcile("owner#a", Decimal("1000"), _NOW)  # holding_id区切り文字を含む


# --- T2: 負値拒否(AC2。entity側の既存validatorへ委譲) -------------------------------


def test_t2_negative_amount_is_rejected(tmp_path) -> None:
    service = AvailableCashService(store_dir=tmp_path)
    with pytest.raises(ValidationError, match="0以上"):
        service.reconcile("owner-a", Decimal("-1"), _NOW)


# --- T3〜T6: USER_RECONCILIATION契約(AC2) --------------------------------------------


def test_t3_reconcile_sets_user_reconciliation_contract_fields(tmp_path) -> None:
    service = AvailableCashService(store_dir=tmp_path)

    result = service.reconcile("owner-a", Decimal("812400"), _NOW)

    assert result.available_cash == Decimal("812400")
    assert result.last_update_type == AvailableCashUpdateType.USER_RECONCILIATION
    assert result.updated_at == _NOW
    assert result.last_reconciled_at == _NOW


def test_t4_reconcile_overwrites_absolute_value_not_a_delta(tmp_path) -> None:
    """入力値を正とする(過去理論値との差額イベントは作らない)。"""
    service = AvailableCashService(store_dir=tmp_path)
    service.reconcile("owner-a", Decimal("300000"), _NOW)

    result = service.reconcile("owner-a", Decimal("812400"), _LATER)

    assert result.available_cash == Decimal("812400")  # 差額(512400)ではない


def test_t5_reconcile_advances_updated_at_and_last_reconciled_at(tmp_path) -> None:
    service = AvailableCashService(store_dir=tmp_path)
    service.reconcile("owner-a", Decimal("1000"), _NOW)

    result = service.reconcile("owner-a", Decimal("2000"), _LATER)

    assert result.updated_at == _LATER
    assert result.last_reconciled_at == _LATER


def test_t6_zero_is_a_legal_reconciliation_value(tmp_path) -> None:
    service = AvailableCashService(store_dir=tmp_path)
    result = service.reconcile("owner-a", Decimal("0"), _NOW)
    assert result.available_cash == Decimal("0")


# --- T7/T8: 未登録owner initialize / 既存owner CAS更新 -------------------------------


def test_t7_first_reconciliation_initializes_an_unregistered_owner(tmp_path) -> None:
    service = AvailableCashService(store_dir=tmp_path)
    assert service.get("owner-a") is None

    service.reconcile("owner-a", Decimal("1000"), _NOW)

    assert service.get("owner-a").available_cash == Decimal("1000")


def test_t8_second_reconciliation_updates_the_existing_record_via_cas(tmp_path) -> None:
    service = AvailableCashService(store_dir=tmp_path)
    service.reconcile("owner-a", Decimal("1000"), _NOW)

    service.reconcile("owner-a", Decimal("1500"), _LATER)

    repo = AvailableCashRepository(store_dir=tmp_path)
    assert len(repo.list_all()) == 1  # 新規レコードが増えていない(同一ownerを更新)
    assert repo.get("owner-a").available_cash == Decimal("1500")


def test_t8b_reconciling_owner_a_does_not_affect_owner_b(tmp_path) -> None:
    service = AvailableCashService(store_dir=tmp_path)
    service.reconcile("owner-a", Decimal("1000"), _NOW)
    service.reconcile("owner-b", Decimal("2000"), _NOW)

    service.reconcile("owner-a", Decimal("1500"), _LATER)

    assert service.get("owner-a").available_cash == Decimal("1500")
    assert service.get("owner-b").available_cash == Decimal("2000")


# --- T9〜T11: stale/concurrent update対策(bounded retry) ----------------------------


class _ConflictingRepository:
    """最初のN回のreplace_if_raw_matches()を強制的に失敗させ、他プロセスによる
    competing writeを模倣する(実際のCAS機構〔replace_if_raw_matches〕自体は
    本物のAvailableCashRepositoryへ委譲する。新しいCAS方式は作らない)。"""

    def __init__(self, inner: AvailableCashRepository, fail_first_n: int) -> None:
        self._inner = inner
        self._remaining_failures = fail_first_n

    def get(self, owner: str) -> AvailableCash | None:
        return self._inner.get(owner)

    def get_raw(self, owner: str) -> str | None:
        return self._inner.get_raw(owner)

    def initialize(self, record: AvailableCash) -> bool:
        return self._inner.initialize(record)

    def replace_if_raw_matches(
        self, owner: str, expected_raw_data: str, record: AvailableCash
    ) -> bool:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            return False
        return self._inner.replace_if_raw_matches(owner, expected_raw_data, record)


def test_t9_reconcile_retries_and_succeeds_after_a_transient_conflict(tmp_path) -> None:
    inner = AvailableCashRepository(store_dir=tmp_path)
    inner.initialize(
        AvailableCash(
            owner="owner-a",
            available_cash=Decimal("1000"),
            updated_at=_NOW,
            last_update_type=AvailableCashUpdateType.USER_RECONCILIATION,
            last_reconciled_at=_NOW,
        )
    )
    flaky = _ConflictingRepository(inner, fail_first_n=2)
    service = AvailableCashService(repository=flaky, default_max_retries=3)

    result = service.reconcile("owner-a", Decimal("1500"), _LATER)

    assert result.available_cash == Decimal("1500")
    assert inner.get("owner-a").available_cash == Decimal("1500")


def test_t10_reconcile_raises_after_exhausting_retries(tmp_path) -> None:
    inner = AvailableCashRepository(store_dir=tmp_path)
    inner.initialize(
        AvailableCash(
            owner="owner-a",
            available_cash=Decimal("1000"),
            updated_at=_NOW,
            last_update_type=AvailableCashUpdateType.USER_RECONCILIATION,
            last_reconciled_at=_NOW,
        )
    )
    always_conflicting = _ConflictingRepository(inner, fail_first_n=10)
    service = AvailableCashService(repository=always_conflicting, default_max_retries=3)

    with pytest.raises(AvailableCashReconciliationExhaustedError):
        service.reconcile("owner-a", Decimal("1500"), _LATER)

    # 失敗した試行はいずれも既存値へ影響しない。
    assert inner.get("owner-a").available_cash == Decimal("1000")


def test_t11_max_retries_parameter_overrides_the_default(tmp_path) -> None:
    """呼び出し側が明示的に渡したmax_retriesが、コンストラクタのdefault_max_retries
    より優先されることを固定する。fail_first_n=1(1回失敗して2回目で成功する)
    に対し、default_max_retries=5なら成功するはずの状況で、max_retries=1
    (1回しか試さない)を明示すると1回目の失敗で例外になることを確認する。"""
    inner = AvailableCashRepository(store_dir=tmp_path)
    inner.initialize(
        AvailableCash(
            owner="owner-a",
            available_cash=Decimal("1000"),
            updated_at=_NOW,
            last_update_type=AvailableCashUpdateType.USER_RECONCILIATION,
            last_reconciled_at=_NOW,
        )
    )
    flaky = _ConflictingRepository(inner, fail_first_n=1)
    service = AvailableCashService(repository=flaky, default_max_retries=5)

    with pytest.raises(AvailableCashReconciliationExhaustedError):
        service.reconcile("owner-a", Decimal("1500"), _LATER, max_retries=1)

    # 失敗した試行は既存値へ影響しない。
    assert inner.get("owner-a").available_cash == Decimal("1000")


# --- T12: 0円と未登録の区別を維持(#584 T4と同じ契約) ---------------------------------


def test_t12_get_distinguishes_unregistered_from_zero(tmp_path) -> None:
    service = AvailableCashService(store_dir=tmp_path)
    service.reconcile("owner-a", Decimal("0"), _NOW)

    assert service.get("owner-a") is not None
    assert service.get("owner-a").available_cash == Decimal("0")
    assert service.get("owner-unregistered") is None
