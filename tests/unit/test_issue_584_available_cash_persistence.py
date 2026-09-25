"""Issue #584(#128 A1): owner単位のavailable_cash永続モデル。

trade連携atomicity・cash増減ロジック・UI等は一切scope外(Issue本文どおり)。
schema/repository capability(保存・取得・楽観ロック更新)のみを固定する。
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

_NOW = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.UTC)
_LATER = dt.datetime(2026, 9, 25, 13, 0, tzinfo=dt.UTC)


def _record(
    owner: str = "owner-a",
    available_cash: Decimal = Decimal("1000"),
    updated_at: dt.datetime = _NOW,
    last_update_type: AvailableCashUpdateType = AvailableCashUpdateType.USER_RECONCILIATION,
    last_reconciled_at: dt.datetime | None = _NOW,
) -> AvailableCash:
    return AvailableCash(
        owner=owner,
        available_cash=available_cash,
        updated_at=updated_at,
        last_update_type=last_update_type,
        last_reconciled_at=last_reconciled_at,
    )


# --- T1: owner別に独立したcashを保持 -------------------------------------------------


def test_t1_owner_a_and_owner_b_are_independent(tmp_path) -> None:
    repo = AvailableCashRepository(store_dir=tmp_path)
    repo.initialize(_record(owner="owner-a", available_cash=Decimal("1000")))
    repo.initialize(_record(owner="owner-b", available_cash=Decimal("2000")))

    assert repo.get("owner-a").available_cash == Decimal("1000")
    assert repo.get("owner-b").available_cash == Decimal("2000")


# --- T2: available_cash負値拒否 -------------------------------------------------------


@pytest.mark.parametrize("negative_value", [Decimal("-1"), Decimal("-0.01")])
def test_t2_negative_available_cash_is_rejected(negative_value: Decimal) -> None:
    """整数境界(-1)と小数境界(-0.01)の両方で拒否されることを固定する。

    実装(_check_non_negative)を実際に一時的に無効化して赤くなることを確認
    する手作業のmutation testは、PR本文のNegative verification節に記録した
    (production codeを変更する必要があるため、恒久的なテストとしては
    ここへ含めていない。サブちゃんレビューF2対応)。
    """
    with pytest.raises(ValidationError, match="0以上"):
        _record(available_cash=negative_value)


# --- T3: 0円は合法 --------------------------------------------------------------------


def test_t3_zero_is_legal(tmp_path) -> None:
    repo = AvailableCashRepository(store_dir=tmp_path)
    repo.initialize(_record(available_cash=Decimal("0")))
    assert repo.get("owner-a").available_cash == Decimal("0")


# --- T4: 未登録と0円を区別 -------------------------------------------------------------


def test_t4_unregistered_owner_is_none_not_zero(tmp_path) -> None:
    repo = AvailableCashRepository(store_dir=tmp_path)
    repo.initialize(_record(owner="owner-a", available_cash=Decimal("0")))

    assert repo.get("owner-a") is not None
    assert repo.get("owner-a").available_cash == Decimal("0")
    assert repo.get("owner-unregistered") is None


# --- T5: updated_at保存 ---------------------------------------------------------------


def test_t5_updated_at_is_persisted(tmp_path) -> None:
    repo = AvailableCashRepository(store_dir=tmp_path)
    repo.initialize(_record(updated_at=_NOW))
    assert repo.get("owner-a").updated_at == _NOW


# --- T6: TRADE_UPDATE保存 / T7: USER_RECONCILIATION保存 -------------------------------


def test_t6_trade_update_is_persisted(tmp_path) -> None:
    repo = AvailableCashRepository(store_dir=tmp_path)
    repo.initialize(
        _record(last_update_type=AvailableCashUpdateType.TRADE_UPDATE, last_reconciled_at=None)
    )
    assert repo.get("owner-a").last_update_type == AvailableCashUpdateType.TRADE_UPDATE


def test_t7_user_reconciliation_is_persisted(tmp_path) -> None:
    repo = AvailableCashRepository(store_dir=tmp_path)
    repo.initialize(_record(last_update_type=AvailableCashUpdateType.USER_RECONCILIATION))
    assert repo.get("owner-a").last_update_type == AvailableCashUpdateType.USER_RECONCILIATION


# --- T8: TRADE_UPDATEでlast_reconciled_atを勝手に更新しない ---------------------------
# --- T9: reconciliation時のみlast_reconciled_at更新可能 -------------------------------


def test_t8_last_reconciled_at_is_preserved_when_caller_carries_it_forward(tmp_path) -> None:
    """呼び出し側が前回のlast_reconciled_atを明示的に引き継いだ場合、その値が
    保存されることを固定する(repository/entity層はどちらの値を引き継ぐかを
    自動では判断しない。判断・強制の責務はA1のscope外であり、将来のA2/A3
    〔trade+available cash atomicity〕側で担う。サブちゃんレビューF1対応:
    本テストは「repositoryが勝手に更新しないこと」の強い主張ではなく、
    「呼び出し側が引き継いだ値がそのまま保存されること」のみを確認する)。"""
    repo = AvailableCashRepository(store_dir=tmp_path)
    original = _record(
        available_cash=Decimal("1000"),
        updated_at=_NOW,
        last_update_type=AvailableCashUpdateType.USER_RECONCILIATION,
        last_reconciled_at=_NOW,
    )
    repo.initialize(original)
    raw = repo.get_raw("owner-a")

    trade_updated = original.model_copy(
        update={
            "available_cash": Decimal("900"),
            "updated_at": _LATER,
            "last_update_type": AvailableCashUpdateType.TRADE_UPDATE,
            # last_reconciled_atは明示的に引き継ぐ(TRADE_UPDATEでは進めない)。
            "last_reconciled_at": original.last_reconciled_at,
        }
    )
    assert repo.replace_if_raw_matches("owner-a", raw, trade_updated)

    updated = repo.get("owner-a")
    assert updated.available_cash == Decimal("900")
    assert updated.updated_at == _LATER
    assert updated.last_reconciled_at == _NOW  # TRADE_UPDATEでも変わっていない


def test_t9_user_reconciliation_can_advance_last_reconciled_at(tmp_path) -> None:
    repo = AvailableCashRepository(store_dir=tmp_path)
    original = _record(
        available_cash=Decimal("900"),
        updated_at=_NOW,
        last_update_type=AvailableCashUpdateType.TRADE_UPDATE,
        last_reconciled_at=None,
    )
    repo.initialize(original)
    raw = repo.get_raw("owner-a")

    reconciled = original.model_copy(
        update={
            "available_cash": Decimal("950"),
            "updated_at": _LATER,
            "last_update_type": AvailableCashUpdateType.USER_RECONCILIATION,
            "last_reconciled_at": _LATER,
        }
    )
    assert repo.replace_if_raw_matches("owner-a", raw, reconciled)

    updated = repo.get("owner-a")
    assert updated.last_reconciled_at == _LATER


# --- T10: owner A更新でowner B不変 -----------------------------------------------------


def test_t10_updating_owner_a_does_not_affect_owner_b(tmp_path) -> None:
    repo = AvailableCashRepository(store_dir=tmp_path)
    repo.initialize(_record(owner="owner-a", available_cash=Decimal("1000")))
    repo.initialize(_record(owner="owner-b", available_cash=Decimal("2000")))

    raw_a = repo.get_raw("owner-a")
    updated_a = repo.get("owner-a").model_copy(update={"available_cash": Decimal("1500")})
    repo.replace_if_raw_matches("owner-a", raw_a, updated_a)

    assert repo.get("owner-a").available_cash == Decimal("1500")
    assert repo.get("owner-b").available_cash == Decimal("2000")


# --- T11: concurrent stale updateを既存CAS方式で拒否 -----------------------------------


def test_t11_stale_update_is_rejected_by_cas(tmp_path) -> None:
    repo = AvailableCashRepository(store_dir=tmp_path)
    original = _record(available_cash=Decimal("1000"))
    repo.initialize(original)
    stale_raw = repo.get_raw("owner-a")

    # 別の更新が先に成功する。
    first_update = original.model_copy(
        update={"available_cash": Decimal("800"), "updated_at": _LATER}
    )
    assert repo.replace_if_raw_matches("owner-a", stale_raw, first_update)

    # 古いraw値のまま2回目を試みると失敗する(競合)。
    second_update = original.model_copy(
        update={"available_cash": Decimal("700"), "updated_at": _LATER}
    )
    assert not repo.replace_if_raw_matches("owner-a", stale_raw, second_update)

    # 実際の値は最初の更新のまま(2回目は反映されていない)。
    assert repo.get("owner-a").available_cash == Decimal("800")


def test_caller_misuse_of_always_fresh_raw_data_defeats_cas(tmp_path) -> None:
    """これはCAS機構自体の反証テストではない(サブちゃんレビューF2対応:
    誤って「negative_verification」と名付けていたが、実装を変異させておらず
    本物の反証ではなかった)。CASの安全性は呼び出し側が「更新前に読んだ
    raw値」をexpected_raw_dataへ渡すことに懸かっており、呼び出し側が誤って
    都度`get_raw()`し直した値を渡すと、古い前提に基づく更新でも常に成功して
    しまう、という**呼び出し側の誤用パターン**を記録するデモンストレーション
    である。CAS実装自体の反証(replace_if_raw_matches()を無条件upsert()へ
    変異させ、T11本体が正しく赤くなることの確認)はPR本文のNegative
    verification節に記録した(production codeを変更する必要があるため、
    恒久的なテストとしてはここへ含めていない)。"""
    repo = AvailableCashRepository(store_dir=tmp_path)
    original = _record(available_cash=Decimal("1000"))
    repo.initialize(original)

    first_update = original.model_copy(update={"available_cash": Decimal("800")})
    repo.replace_if_raw_matches("owner-a", repo.get_raw("owner-a"), first_update)

    always_fresh_raw = repo.get_raw("owner-a")
    second_update = first_update.model_copy(update={"available_cash": Decimal("700")})
    assert repo.replace_if_raw_matches("owner-a", always_fresh_raw, second_update)
    assert repo.get("owner-a").available_cash == Decimal("700")


# --- T12: serialization/deserialization round-trip ------------------------------------


def test_t12_serialization_round_trip(tmp_path) -> None:
    repo = AvailableCashRepository(store_dir=tmp_path)
    original = _record(
        owner="owner-a",
        available_cash=Decimal("1234.56"),
        updated_at=_NOW,
        last_update_type=AvailableCashUpdateType.USER_RECONCILIATION,
        last_reconciled_at=_LATER,
    )
    repo.initialize(original)

    reloaded = AvailableCashRepository(store_dir=tmp_path).get("owner-a")
    assert reloaded == original


# --- 未初期化record作成時の初回作成契約(insert_if_absentの原子性) ---------------------


def test_initialize_returns_false_when_already_exists(tmp_path) -> None:
    repo = AvailableCashRepository(store_dir=tmp_path)
    assert repo.initialize(_record(available_cash=Decimal("1000")))
    assert not repo.initialize(_record(available_cash=Decimal("9999")))
    # 既存値は変更されない。
    assert repo.get("owner-a").available_cash == Decimal("1000")


# --- naive datetime拒否(timestamp semantics契約) --------------------------------------


def test_naive_updated_at_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AvailableCash(
            owner="owner-a",
            available_cash=Decimal("1000"),
            updated_at=dt.datetime(2026, 9, 25, 12, 0),  # naive
            last_update_type=AvailableCashUpdateType.USER_RECONCILIATION,
        )


def test_naive_last_reconciled_at_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AvailableCash(
            owner="owner-a",
            available_cash=Decimal("1000"),
            updated_at=_NOW,
            last_update_type=AvailableCashUpdateType.USER_RECONCILIATION,
            last_reconciled_at=dt.datetime(2026, 9, 25, 12, 0),  # naive
        )
