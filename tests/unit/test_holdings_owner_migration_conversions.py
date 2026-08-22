"""保有銘柄オーナー機能移行(M2)のLegacy読み込み・owner変換・holding_id生成テスト。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.owner import (
    InvalidOwnerError,
    build_holding_id,
    normalize_and_validate_owner,
    normalize_owner,
    split_holding_id,
)
from jstock_advisor.migrations.conversions import (
    DEFAULT_MIGRATION_OWNER,
    convert_holding,
    convert_holdings_snapshot_entry,
    convert_purchase_lot,
    migrate_holding_id_field_value,
)
from jstock_advisor.migrations.legacy_shapes import (
    LegacyHoldingsSnapshotEntryV1,
    LegacyHoldingV1,
    LegacyPurchaseLotV1,
)

_NOW = dt.datetime(2026, 8, 22, 0, 0, tzinfo=dt.UTC)


# --- owner正規化・検証 -------------------------------------------------------


def test_normalize_owner_trims_and_collapses_whitespace() -> None:
    assert normalize_owner("  本人  ") == "本人"
    assert normalize_owner("本人   子供") == "本人 子供"


def test_normalize_owner_nfkc_normalizes_fullwidth_input() -> None:
    # 全角英数字("ＡＢＣ")はNFKC正規化で半角("ABC")へ統一される。
    assert normalize_owner("ＡＢＣ") == "ABC"


def test_validate_owner_rejects_empty_string() -> None:
    with pytest.raises(InvalidOwnerError):
        normalize_and_validate_owner("   ")


def test_validate_owner_rejects_delimiter_character() -> None:
    with pytest.raises(InvalidOwnerError):
        normalize_and_validate_owner("本人#子供")


def test_validate_owner_rejects_too_long_value() -> None:
    with pytest.raises(InvalidOwnerError):
        normalize_and_validate_owner("あ" * 21)


def test_build_holding_id_format() -> None:
    assert build_holding_id("本人", "8306") == "本人#8306"


# --- Legacyモデル読み込み ----------------------------------------------------


def test_legacy_holding_v1_reads_current_production_shape() -> None:
    legacy = LegacyHoldingV1(
        stock_code="8306",
        stock_name="三菱UFJ",
        shares=100,
        average_purchase_price=Decimal("1500"),
        total_purchase_amount=Decimal("150000"),
        first_purchase_date=dt.date(2026, 1, 1),
        last_purchase_date=dt.date(2026, 1, 1),
        account_type=AccountType.GENERAL,
        created_at=_NOW,
        updated_at=_NOW,
    )
    assert legacy.stock_code == "8306"
    assert legacy.shares == 100


def test_legacy_purchase_lot_v1_reads_current_production_shape() -> None:
    legacy = LegacyPurchaseLotV1(
        lot_id="lot-1",
        stock_code="8306",
        purchase_date=dt.date(2026, 1, 1),
        shares=100,
        purchase_price=Decimal("1500"),
        account_type=AccountType.GENERAL,
    )
    assert legacy.lot_id == "lot-1"


def test_legacy_holdings_snapshot_entry_v1_reads_current_production_shape() -> None:
    legacy = LegacyHoldingsSnapshotEntryV1(
        stock_code="8306", shares=100, recorded_at=dt.date(2026, 1, 1)
    )
    assert legacy.stock_code == "8306"
    assert legacy.active_holding is True


# --- owner="本人"変換・holding_id生成 -----------------------------------------


def test_convert_holding_backfills_default_owner_and_holding_id() -> None:
    legacy = LegacyHoldingV1(
        stock_code="8306",
        stock_name="三菱UFJ",
        shares=100,
        average_purchase_price=Decimal("1500"),
        total_purchase_amount=Decimal("150000"),
        first_purchase_date=dt.date(2026, 1, 1),
        last_purchase_date=dt.date(2026, 1, 1),
        account_type=AccountType.GENERAL,
        created_at=_NOW,
        updated_at=_NOW,
    )
    v2 = convert_holding(legacy)
    assert v2.owner == DEFAULT_MIGRATION_OWNER
    assert v2.holding_id == "本人#8306"
    assert v2.stock_code == "8306"
    assert v2.shares == 100
    # 他のフィールドも欠落なく引き継がれていること。
    assert v2.stock_name == "三菱UFJ"
    assert v2.average_purchase_price == Decimal("1500")


def test_convert_purchase_lot_backfills_owner_and_holding_id() -> None:
    legacy = LegacyPurchaseLotV1(
        lot_id="lot-1",
        stock_code="8306",
        purchase_date=dt.date(2026, 1, 1),
        shares=100,
        purchase_price=Decimal("1500"),
        account_type=AccountType.GENERAL,
    )
    v2 = convert_purchase_lot(legacy)
    assert v2.owner == "本人"
    assert v2.holding_id == "本人#8306"
    assert v2.lot_id == "lot-1"


def test_convert_holdings_snapshot_entry_backfills_owner_and_holding_id() -> None:
    legacy = LegacyHoldingsSnapshotEntryV1(
        stock_code="8306", shares=100, recorded_at=dt.date(2026, 1, 1)
    )
    v2 = convert_holdings_snapshot_entry(legacy)
    assert v2.owner == "本人"
    assert v2.holding_id == "本人#8306"


# --- holding_id "field-only"移行(HoldingDecisionResult/InvestmentThesis/
# InvestmentThesisBaseline共通)の冪等性・fail-closed動作 -----------------------


def test_split_holding_id_returns_none_for_bare_stock_code() -> None:
    assert split_holding_id("8306") is None


def test_split_holding_id_splits_owner_and_stock_code() -> None:
    assert split_holding_id("本人#8306") == ("本人", "8306")


def test_split_holding_id_raises_on_multiple_delimiters() -> None:
    with pytest.raises(InvalidOwnerError):
        split_holding_id("本人#本人#8306")


def test_migrate_holding_id_field_value_converts_legacy_bare_stock_code() -> None:
    assert migrate_holding_id_field_value("8306", "本人") == "本人#8306"


def test_migrate_holding_id_field_value_is_idempotent_for_already_migrated_value() -> None:
    """2回目以降の実行で"本人#本人#8306"のような二重prefixにならないこと。"""
    once = migrate_holding_id_field_value("8306", "本人")
    twice = migrate_holding_id_field_value(once, "本人")
    assert once == "本人#8306"
    assert twice == "本人#8306"


def test_migrate_holding_id_field_value_fails_closed_on_different_owner_prefix() -> None:
    with pytest.raises(InvalidOwnerError):
        migrate_holding_id_field_value("子供#8306", "本人")


def test_migrate_holding_id_field_value_fails_closed_on_malformed_multi_prefix() -> None:
    with pytest.raises(InvalidOwnerError):
        migrate_holding_id_field_value("本人#本人#8306", "本人")


def test_convert_holding_with_custom_owner() -> None:
    legacy = LegacyHoldingV1(
        stock_code="8306",
        stock_name="三菱UFJ",
        shares=50,
        average_purchase_price=Decimal("1500"),
        total_purchase_amount=Decimal("75000"),
        first_purchase_date=dt.date(2026, 1, 1),
        last_purchase_date=dt.date(2026, 1, 1),
        account_type=AccountType.GENERAL,
        created_at=_NOW,
        updated_at=_NOW,
    )
    v2 = convert_holding(legacy, owner="子供")
    assert v2.owner == "子供"
    assert v2.holding_id == "子供#8306"
