"""Issue #591(#128 A4): BUY登録時にAvailable Cash不足を検知し、資金超過の
購入登録を拒否する。`AvailableCashService.build_trade_update_plan()`が
送出する専用例外`InsufficientAvailableCashError`のsemanticsを固定する。

#590の設計(plan構築をI/O前に完結させる規約)により、拒否時にHolding/
Transaction/Cashのいずれも書き込まれないことは、追加のtransaction制御
なしで自動的に満たされる(本ファイルではその前提の再確認のみ行う)。

LINE(A5a)/CLI(#619)向けの文言・呼び出し側の翻訳層はscope外
(conversation_service.pyの回帰はtest_conversation_service.py側で検証する)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.infrastructure.local_repository.available_cash_repository import (
    AvailableCashRepository,
)
from jstock_advisor.services.available_cash_service import (
    AvailableCashService,
    InsufficientAvailableCashError,
)

_NOW = dt.datetime(2026, 9, 26, 9, 0, tzinfo=dt.UTC)
_LATER = dt.datetime(2026, 9, 26, 10, 0, tzinfo=dt.UTC)


def _seed(service: AvailableCashService, owner: str, amount: str) -> None:
    service.reconcile(owner, Decimal(amount), _NOW)


# --- 必須Acceptance Criteria(#591本文どおり) --------------------------------


def test_purchase_amount_exceeding_available_cash_is_rejected(tmp_path) -> None:
    """available_cash=100000 / BUY amount=120000 -> reject"""
    service = AvailableCashService(store_dir=tmp_path)
    _seed(service, "owner-a", "100000")

    with pytest.raises(InsufficientAvailableCashError):
        service.build_trade_update_plan("owner-a", Decimal("-120000"), _LATER)


def test_purchase_amount_equal_to_available_cash_is_allowed(tmp_path) -> None:
    """available_cash=100000 / BUY amount=100000 -> allow / cash=0(境界値)"""
    service = AvailableCashService(store_dir=tmp_path)
    _seed(service, "owner-a", "100000")

    plan = service.build_trade_update_plan("owner-a", Decimal("-100000"), _LATER)

    assert plan.model.available_cash == Decimal("0")


def test_purchase_amount_one_yen_over_available_cash_is_rejected(tmp_path) -> None:
    """境界値: available_cash+1円のBUYは拒否される。"""
    service = AvailableCashService(store_dir=tmp_path)
    _seed(service, "owner-a", "100000")

    with pytest.raises(InsufficientAvailableCashError):
        service.build_trade_update_plan("owner-a", Decimal("-100001"), _LATER)


def test_sell_is_always_allowed_regardless_of_available_cash_amount(tmp_path) -> None:
    """SELL: available_cashの大小に関係なくallow(#591 Scope)。

    delta>=0(SELL)はavailable_cashを増やす方向にしか作用しないため、
    available_cashが少額・0円であっても拒否されない。
    """
    service = AvailableCashService(store_dir=tmp_path)
    _seed(service, "owner-a", "0")

    plan = service.build_trade_update_plan("owner-a", Decimal("999999999"), _LATER)

    assert plan.model.available_cash == Decimal("999999999")


# --- 拒否時にHolding/Transaction/Cashのいずれも変化しない(#590の性質の再確認) --


def test_rejection_happens_before_any_persistence(tmp_path) -> None:
    service = AvailableCashService(store_dir=tmp_path)
    _seed(service, "owner-a", "100000")
    repo = AvailableCashRepository(store_dir=tmp_path)

    with pytest.raises(InsufficientAvailableCashError):
        service.build_trade_update_plan("owner-a", Decimal("-120000"), _LATER)

    assert repo.get("owner-a").available_cash == Decimal("100000")  # 変化なし


# --- 例外の属性契約(呼び出し側がowner/available_cash/purchase_amountを ---
# --- 使えることを固定する) --------------------------------------------------


def test_exception_carries_available_cash_and_purchase_amount(tmp_path) -> None:
    service = AvailableCashService(store_dir=tmp_path)
    _seed(service, "owner-a", "100000")

    with pytest.raises(InsufficientAvailableCashError) as exc_info:
        service.build_trade_update_plan("owner-a", Decimal("-120000"), _LATER)

    assert exc_info.value.available_cash == Decimal("100000")
    assert exc_info.value.purchase_amount == Decimal("120000")  # 符号反転済み(正値)
    assert exc_info.value.owner == "owner-a"


def test_exception_message_does_not_leak_raw_owner_value(tmp_path) -> None:
    """Issue #135と同じ方針: 例外messageに生owner値を含めない。"""
    service = AvailableCashService(store_dir=tmp_path)
    _seed(service, "owner-secret-name", "100000")

    with pytest.raises(InsufficientAvailableCashError) as exc_info:
        service.build_trade_update_plan("owner-secret-name", Decimal("-120000"), _LATER)

    assert "owner-secret-name" not in str(exc_info.value)


def test_insufficient_available_cash_error_is_distinguishable_from_not_registered(
    tmp_path,
) -> None:
    """未登録owner(AvailableCashNotRegisteredError)と余力不足
    (InsufficientAvailableCashError)は別の型であり、呼び出し側が
    型で区別できる(いずれもValueErrorのサブクラスだが、互いのサブクラスでは
    ない)。"""
    from jstock_advisor.services.available_cash_service import (
        AvailableCashNotRegisteredError,
    )

    assert not issubclass(InsufficientAvailableCashError, AvailableCashNotRegisteredError)
    assert not issubclass(AvailableCashNotRegisteredError, InsufficientAvailableCashError)
