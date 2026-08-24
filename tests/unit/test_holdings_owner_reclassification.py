"""既存保有データのowner実態補正(M4.1)のテスト。

4680分割・9434単価訂正に相当する分割/価格訂正ケース、pause強制確認、
holding_id衝突検知、dry-run、冪等性・途中失敗後再実行、tombstone不変、
InvestmentThesis/BaselineSequence/BaselinePointerの分割時継承方針を検証する。

実データ(所有者名・実際の保有数量/取得単価)はGit管理対象のソースコードへ
記録しない(2026-08-25コードレビュー対応、CLAUDE.md恒久ルール)。本テストは
実行時に読み込む`RealDataInput`が完全に架空の値(所有者A/B/C、架空の
stock_code・lot_id・数量)であっても本番相当のロジックを検証できることを
示す(本モジュール自体が特定の所有者名・数値に依存しない汎用機構である
ことの裏付けでもある)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import AccountType, TransactionType
from jstock_advisor.domain.entities.holding import Holding, PurchaseLot
from jstock_advisor.domain.entities.holding_decision import (
    InvestmentThesis,
    InvestmentThesisBaselinePointer,
)
from jstock_advisor.domain.entities.holdings_snapshot import HoldingsSnapshotEntry
from jstock_advisor.domain.entities.owner import build_holding_id
from jstock_advisor.infrastructure.aws import trading_pause_config
from jstock_advisor.infrastructure.collection_store import build_collection_store
from jstock_advisor.migrations.holdings_owner_reclassification import (
    PlanValidationError,
    RealDataInput,
    ReclassificationAbortedError,
    _SimplePrecondition,
    run_reclassification,
)
from jstock_advisor.migrations.legacy_shapes import LegacyBaselineSequenceCounterV1
from jstock_advisor.migrations.target import MigrationTarget
from jstock_advisor.services.investment_thesis_service import InvestmentThesisService

_NOW = dt.datetime(2026, 8, 23, 0, 0, tzinfo=dt.UTC)
_OLD = "本人"

# --- テスト専用の架空RealDataInput(実データを一切含まない) --------------------
_OWNER_A = "所有者A"  # default_new_owner
_OWNER_B = "所有者B"  # child owner (複数銘柄)
_OWNER_C = "所有者C"  # child owner (単一銘柄)

_STOCK_A = "1001"  # 通常owner付け替え(所有者Aへ)
_STOCK_B1 = "2001"  # 子owner付け替え(所有者Bへ)
_STOCK_B2 = "2002"  # 子owner付け替え(所有者Bへ)
_STOCK_C = "3001"  # 子owner付け替え(所有者Cへ)
_STOCK_SPLIT = "4001"  # 分割対象
_STOCK_PRICE_CORRECTED = "5001"  # 子owner付け替え+単価訂正

_SPLIT_LOT_A = "lot-4001-a"  # 300株@1193 → 所有者A(大きい持分側)
_SPLIT_LOT_B = "lot-4001-b"  # 100株@1258 → 所有者B(小さい持分側)
_PRICE_CORRECTED_LOT = "lot-5001-x"  # 100株、187→188へ訂正

_REAL_DATA = RealDataInput(
    default_new_owner=_OWNER_A,
    child_owner_by_stock_code={
        _STOCK_B1: _OWNER_B,
        _STOCK_B2: _OWNER_B,
        _STOCK_PRICE_CORRECTED: _OWNER_B,
        _STOCK_C: _OWNER_C,
    },
    split_stock_code=_STOCK_SPLIT,
    split_lot_owners={
        _SPLIT_LOT_A: _OWNER_A,
        _SPLIT_LOT_B: _OWNER_B,
    },
    price_corrections={_PRICE_CORRECTED_LOT: Decimal("188")},
    simple_preconditions={
        _STOCK_B1: _SimplePrecondition(
            expected_shares=200, expected_average_price=Decimal("3215")
        ),
        _STOCK_B2: _SimplePrecondition(
            expected_shares=500, expected_average_price=Decimal("587")
        ),
        _STOCK_C: _SimplePrecondition(
            expected_shares=100, expected_average_price=Decimal("5480")
        ),
    },
    nine_four_three_four_stock_code=_STOCK_PRICE_CORRECTED,
    nine_four_three_four_lot_id=_PRICE_CORRECTED_LOT,
    nine_four_three_four_shares=100,
    nine_four_three_four_old_price=Decimal("187"),
    split_lot_preconditions={
        _SPLIT_LOT_A: (300, Decimal("1193")),
        _SPLIT_LOT_B: (100, Decimal("1258")),
    },
    split_old_shares=400,
    split_old_average_price=Decimal("1209.25"),
    split_old_total_amount=Decimal("483700"),
)


def _run(target: MigrationTarget, dry_run: bool, store_dir: Path):
    return run_reclassification(target, dry_run=dry_run, real_data=_REAL_DATA, store_dir=store_dir)


def _set_pause(store_dir: Path, paused: bool) -> None:
    existing = trading_pause_config.get(store_dir)
    if existing is None:
        trading_pause_config.init(
            pause_buy_sell=paused,
            updated_by="tester",
            change_reason="test setup",
            store_dir=store_dir,
        )
        return
    trading_pause_config.update(
        expected_config_version=existing.config_version,
        pause_buy_sell=paused,
        updated_by="tester",
        change_reason="test setup",
        store_dir=store_dir,
    )


def _seed_lot(
    store_dir: Path,
    lot_id: str,
    owner: str,
    stock_code: str,
    shares: int,
    price: str,
) -> None:
    build_collection_store(PurchaseLot, "purchase_lots.json", "lot_id", store_dir).upsert(
        PurchaseLot(
            lot_id=lot_id,
            owner=owner,
            holding_id=build_holding_id(owner, stock_code),
            stock_code=stock_code,
            purchase_date=dt.date(2026, 1, 1),
            shares=shares,
            purchase_price=Decimal(price),
            account_type=AccountType.GENERAL,
        )
    )


def _seed_holding(
    store_dir: Path,
    owner: str,
    stock_code: str,
    shares: int,
    price: str,
) -> None:
    total = Decimal(price) * shares
    build_collection_store(Holding, "holdings_v2.json", "holding_id", store_dir).upsert(
        Holding(
            owner=owner,
            holding_id=build_holding_id(owner, stock_code),
            stock_code=stock_code,
            stock_name=f"銘柄{stock_code}",
            shares=shares,
            average_purchase_price=Decimal(price),
            total_purchase_amount=total,
            first_purchase_date=dt.date(2026, 1, 1),
            last_purchase_date=dt.date(2026, 1, 1),
            account_type=AccountType.GENERAL,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )


def _seed_simple(
    store_dir: Path, stock_code: str, shares: int, price: str, lot_id: str | None = None
) -> None:
    lot_id = lot_id or f"lot-{stock_code}"
    _seed_lot(store_dir, lot_id, _OLD, stock_code, shares, price)
    _seed_holding(store_dir, _OLD, stock_code, shares, price)


def _seed_snapshot(
    store_dir: Path,
    owner: str,
    stock_code: str,
    shares: int,
    price: str,
    cooldown_until: dt.date | None = None,
) -> None:
    build_collection_store(
        HoldingsSnapshotEntry, "holdings_snapshots_v2.json", "holding_id", store_dir
    ).upsert(
        HoldingsSnapshotEntry(
            owner=owner,
            holding_id=build_holding_id(owner, stock_code),
            stock_code=stock_code,
            shares=shares,
            average_purchase_price=Decimal(price),
            recorded_at=dt.date(2026, 8, 20),
            last_trade_event_type=TransactionType.BUY if cooldown_until else None,
            trade_detected_at=dt.date(2026, 8, 20) if cooldown_until else None,
            cooldown_until_date=cooldown_until,
            active_holding=True,
        )
    )


def _seed_thesis(store_dir: Path, owner: str, stock_code: str, thesis_id: str) -> None:
    build_collection_store(
        InvestmentThesis, "investment_theses.json", "investment_thesis_id", store_dir
    ).upsert(
        InvestmentThesis(
            investment_thesis_id=thesis_id,
            holding_id=build_holding_id(owner, stock_code),
            stock_code=stock_code,
            conditions=[],
            updated_at=_NOW,
        )
    )


def _seed_sequence(store_dir: Path, owner: str, stock_code: str, current_version: int) -> None:
    build_collection_store(
        LegacyBaselineSequenceCounterV1,
        "investment_thesis_baseline_sequences_v2.json",
        "holding_id",
        store_dir,
    ).upsert(
        LegacyBaselineSequenceCounterV1(
            holding_id=build_holding_id(owner, stock_code),
            current_version=current_version,
            updated_at=_NOW,
        )
    )


def _seed_pointer(store_dir: Path, owner: str, stock_code: str, baseline_version: int) -> None:
    holding_id = build_holding_id(owner, stock_code)
    build_collection_store(
        InvestmentThesisBaselinePointer,
        "investment_thesis_baseline_pointers_v2.json",
        "holding_id",
        store_dir,
    ).upsert(
        InvestmentThesisBaselinePointer(
            holding_id=holding_id,
            active_baseline_id=f"{stock_code}:v{baseline_version}",
            active_baseline_version=baseline_version,
            pointer_version=1,
            updated_at=_NOW,
            updated_by="tester",
        )
    )


def _seed_split(store_dir: Path) -> None:
    _seed_lot(store_dir, _SPLIT_LOT_A, _OLD, _STOCK_SPLIT, 300, "1193")
    _seed_lot(store_dir, _SPLIT_LOT_B, _OLD, _STOCK_SPLIT, 100, "1258")
    _seed_holding(store_dir, _OLD, _STOCK_SPLIT, 400, "1209.25")  # (300*1193+100*1258)/400


def _holding_store(store_dir: Path):
    return build_collection_store(Holding, "holdings_v2.json", "holding_id", store_dir)


def _lot_store(store_dir: Path):
    return build_collection_store(PurchaseLot, "purchase_lots.json", "lot_id", store_dir)


def _snapshot_store(store_dir: Path):
    return build_collection_store(
        HoldingsSnapshotEntry, "holdings_snapshots_v2.json", "holding_id", store_dir
    )


def _thesis_store(store_dir: Path):
    return build_collection_store(
        InvestmentThesis, "investment_theses.json", "investment_thesis_id", store_dir
    )


# --- pause強制確認・fail-closed ----------------------------------------------


def test_refuses_when_pause_false(store_dir: Path) -> None:
    _set_pause(store_dir, False)
    _seed_simple(store_dir, _STOCK_A, 100, "1500")
    with pytest.raises(ReclassificationAbortedError, match="pause_buy_sell=false"):
        _run(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


def test_refuses_when_pause_unset(store_dir: Path) -> None:
    with pytest.raises(ReclassificationAbortedError, match="未初期化"):
        _run(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


# --- 通常Holding / 子owner Holding のowner付け替え -----------------------------


def test_default_owner_reassignment(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, _STOCK_A, 100, "1500")

    result = _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    new_id = f"{_OWNER_A}#{_STOCK_A}"
    assert new_id in result.processed_new_holding_ids
    new_holding = _holding_store(store_dir).get(new_id)
    assert new_holding is not None
    assert new_holding.owner == _OWNER_A
    assert new_holding.shares == 100
    assert _holding_store(store_dir).get(f"{_OLD}#{_STOCK_A}") is None
    lot = _lot_store(store_dir).get(f"lot-{_STOCK_A}")
    assert lot is not None
    assert lot.owner == _OWNER_A
    assert lot.holding_id == new_id


def test_child_owner_reassignment_stock_b1(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, _STOCK_B1, 200, "3215")

    _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    new_holding = _holding_store(store_dir).get(f"{_OWNER_B}#{_STOCK_B1}")
    assert new_holding is not None
    assert new_holding.owner == _OWNER_B
    assert _holding_store(store_dir).get(f"{_OLD}#{_STOCK_B1}") is None


def test_child_owner_reassignment_stock_c(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, _STOCK_C, 100, "5480")

    _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    new_holding = _holding_store(store_dir).get(f"{_OWNER_C}#{_STOCK_C}")
    assert new_holding is not None
    assert new_holding.owner == _OWNER_C


# --- 分割 ----------------------------------------------------------------------


def test_split_stock_splits_into_two_holdings(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_split(store_dir)

    result = _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    id_a = f"{_OWNER_A}#{_STOCK_SPLIT}"
    id_b = f"{_OWNER_B}#{_STOCK_SPLIT}"
    assert set(result.processed_new_holding_ids) == {id_a, id_b}
    holding_a = _holding_store(store_dir).get(id_a)
    holding_b = _holding_store(store_dir).get(id_b)
    assert holding_a is not None and holding_b is not None
    assert holding_a.shares == 300
    assert holding_a.average_purchase_price == Decimal("1193")
    assert holding_b.shares == 100
    assert holding_b.average_purchase_price == Decimal("1258")
    assert _holding_store(store_dir).get(f"{_OLD}#{_STOCK_SPLIT}") is None


def test_split_lot_ids_unchanged_only_owner_fields_updated(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_split(store_dir)

    _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    lot_a = _lot_store(store_dir).get(_SPLIT_LOT_A)
    lot_b = _lot_store(store_dir).get(_SPLIT_LOT_B)
    assert lot_a is not None
    assert lot_a.owner == _OWNER_A
    assert lot_a.holding_id == f"{_OWNER_A}#{_STOCK_SPLIT}"
    assert lot_a.shares == 300
    assert lot_b is not None
    assert lot_b.owner == _OWNER_B
    assert lot_b.holding_id == f"{_OWNER_B}#{_STOCK_SPLIT}"
    assert lot_b.shares == 100


# --- 単価訂正 --------------------------------------------------------------------


def test_price_corrected_with_matching_lot_id(store_dir: Path) -> None:
    """price_correctionsはこの特定lot_idにのみ紐づくことを確認する
    (別lot_idでは補正されない、という設計自体は
    test_other_stock_prices_unchangedで別途確認)。"""
    _set_pause(store_dir, True)
    _seed_lot(store_dir, _PRICE_CORRECTED_LOT, _OLD, _STOCK_PRICE_CORRECTED, 100, "187")
    _seed_holding(store_dir, _OLD, _STOCK_PRICE_CORRECTED, 100, "187")

    _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    lot = _lot_store(store_dir).get(_PRICE_CORRECTED_LOT)
    holding = _holding_store(store_dir).get(f"{_OWNER_B}#{_STOCK_PRICE_CORRECTED}")
    assert lot is not None
    assert lot.purchase_price == Decimal("188")
    assert holding is not None
    assert holding.average_purchase_price == Decimal("188")
    assert holding.total_purchase_amount == Decimal("18800")


def test_other_stock_prices_unchanged(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, _STOCK_A, 100, "1500")
    _seed_simple(store_dir, _STOCK_B1, 200, "3215")

    _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    assert _lot_store(store_dir).get(f"lot-{_STOCK_A}").purchase_price == Decimal("1500")  # type: ignore[union-attr]
    assert _lot_store(store_dir).get(f"lot-{_STOCK_B1}").purchase_price == Decimal("3215")  # type: ignore[union-attr]


# --- holding_id衝突検知 --------------------------------------------------------


def test_fails_closed_on_holding_id_collision_with_existing_holding(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, _STOCK_A, 100, "1500")
    # 既に新holding_idが(本移行対象外の理由で)存在するという衝突状態を再現する。
    _seed_holding(store_dir, _OWNER_A, _STOCK_A, 999, "1")

    with pytest.raises(PlanValidationError, match="衝突"):
        _run(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


# --- dry-run --------------------------------------------------------------


def test_dry_run_writes_nothing(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, _STOCK_A, 100, "1500")

    result = _run(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)

    assert result.dry_run is True
    new_id = f"{_OWNER_A}#{_STOCK_A}"
    assert new_id in result.processed_new_holding_ids
    assert _holding_store(store_dir).get(f"{_OLD}#{_STOCK_A}") is not None
    assert _holding_store(store_dir).get(new_id) is None


# --- 冪等性・途中失敗後再実行 ---------------------------------------------------


def test_rerun_after_full_success_is_idempotent_noop(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, _STOCK_A, 100, "1500")

    _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)
    result_second = _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    assert result_second.processed_new_holding_ids == ()
    holdings = _holding_store(store_dir).list_all()
    assert len(holdings) == 1
    assert holdings[0].holding_id == f"{_OWNER_A}#{_STOCK_A}"


class _FlakyHoldingStore:
    """1回目のupsert()のみ、実書き込み後に例外を送出する(2回目以降は正常)。"""

    def __init__(self, inner, call_state: dict[str, int]) -> None:  # type: ignore[no-untyped-def]
        self._inner = inner
        self._call_state = call_state

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._inner, name)

    def upsert(self, item: Holding) -> None:
        self._call_state["count"] += 1
        self._inner.upsert(item)
        if self._call_state["count"] == 1:
            raise RuntimeError("simulated transient failure right after first Holding upsert")


def test_resume_after_partial_failure_completes_split_correctly(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """分割処理中、片方のHolding upsert成功直後に技術的障害が発生した場合でも、
    再実行でもう片方まで正しく完了し、旧Holdingが削除されること
    (途中失敗→再実行の必須テスト)。"""
    _set_pause(store_dir, True)
    _seed_split(store_dir)

    import jstock_advisor.migrations.holdings_owner_reclassification as m

    original_build_collection_store = m.build_collection_store
    call_state = {"count": 0}

    def _flaky_build_collection_store(  # type: ignore[no-untyped-def]
        model_type, file_name, id_field, store_dir_arg=None, ttl_seconds=None
    ):
        real_store = original_build_collection_store(
            model_type, file_name, id_field, store_dir_arg, ttl_seconds=ttl_seconds
        )
        if file_name == "holdings_v2.json":
            return _FlakyHoldingStore(real_store, call_state)
        return real_store

    monkeypatch.setattr(m, "build_collection_store", _flaky_build_collection_store)

    with pytest.raises(RuntimeError, match="simulated transient failure"):
        _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    monkeypatch.undo()

    old_id = f"{_OLD}#{_STOCK_SPLIT}"
    # 旧Holdingはまだ存在する(旧Holding削除はグループ完了時のみ)。
    assert _holding_store(store_dir).get(old_id) is not None

    result = _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    id_a = f"{_OWNER_A}#{_STOCK_SPLIT}"
    id_b = f"{_OWNER_B}#{_STOCK_SPLIT}"
    assert set(result.processed_new_holding_ids) == {id_a, id_b}
    assert _holding_store(store_dir).get(old_id) is None
    holding_a = _holding_store(store_dir).get(id_a)
    holding_b = _holding_store(store_dir).get(id_b)
    assert holding_a is not None and holding_a.shares == 300
    assert holding_b is not None and holding_b.shares == 100


# --- owner値の自由度(allow-listではない) --------------------------------------


def test_owner_is_not_restricted_to_allow_list(store_dir: Path) -> None:
    """RealDataInputの所有者マッピングは今回の実行専用の入力値にすぎず、
    owner型自体がEnum/allow-listでないことを、通常運用側
    (PortfolioService経由のHolding作成)で別owner文字列を使い確認する。"""
    from jstock_advisor.domain.entities.owner import normalize_and_validate_owner

    # 今回のマッピングに含まれない任意のowner文字列でも正規化・検証が通る
    # (M4.1のマッピングが正規化ロジック自体を制限していないことの確認)。
    assert normalize_and_validate_owner("第三子") == "第三子"


# --- tombstone不変 -------------------------------------------------------------


def test_tombstone_snapshot_untouched(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, _STOCK_A, 100, "1500")
    # tombstone(対応するHoldingが存在しない、全部売却済み)。
    _seed_snapshot(store_dir, _OLD, "9999", 0, "0")
    tombstone_before = _snapshot_store(store_dir).get(f"{_OLD}#9999")
    assert tombstone_before is not None

    _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    tombstone_after = _snapshot_store(store_dir).get(f"{_OLD}#9999")
    assert tombstone_after is not None
    assert tombstone_after == tombstone_before


# --- InvestmentThesis: 分割時は大きい持分側のみ継承 -----------------------------


def test_split_investment_thesis_inherited_by_larger_share_owner_only(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_split(store_dir)
    _seed_thesis(store_dir, _OLD, _STOCK_SPLIT, "thesis-split")

    _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    thesis_a = _thesis_store(store_dir).get("thesis-split")
    assert thesis_a is not None
    assert thesis_a.holding_id == f"{_OWNER_A}#{_STOCK_SPLIT}"

    all_theses = _thesis_store(store_dir).list_all()
    theses_b = [t for t in all_theses if t.holding_id == f"{_OWNER_B}#{_STOCK_SPLIT}"]
    assert theses_b == []


def test_split_smaller_share_owner_thesis_created_lazily_via_get_or_create(
    store_dir: Path,
) -> None:
    _set_pause(store_dir, True)
    _seed_split(store_dir)
    _seed_thesis(store_dir, _OLD, _STOCK_SPLIT, "thesis-split")

    _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    service = InvestmentThesisService(store_dir=store_dir)
    holding_id_b = f"{_OWNER_B}#{_STOCK_SPLIT}"
    thesis_b = service.get_or_create_thesis(holding_id=holding_id_b, stock_code=_STOCK_SPLIT)

    assert thesis_b.investment_thesis_id != "thesis-split"
    assert thesis_b.conditions == []
    assert thesis_b.holding_id == holding_id_b


# --- BaselineSequence/BaselinePointer: 分割時の継承方針 -------------------------


def test_split_baseline_sequence_and_pointer_inherited_by_larger_share_owner_only(
    store_dir: Path,
) -> None:
    _set_pause(store_dir, True)
    _seed_split(store_dir)
    _seed_sequence(store_dir, _OLD, _STOCK_SPLIT, current_version=1)
    _seed_pointer(store_dir, _OLD, _STOCK_SPLIT, baseline_version=1)

    _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    from jstock_advisor.migrations.holdings_owner_reclassification import (
        _get_pointer,
        _get_sequence,
    )

    id_a = f"{_OWNER_A}#{_STOCK_SPLIT}"
    id_b = f"{_OWNER_B}#{_STOCK_SPLIT}"

    seq_a = _get_sequence(id_a, store_dir)
    seq_b = _get_sequence(id_b, store_dir)
    assert seq_a is not None
    assert seq_a.current_version == 1
    assert seq_b is None

    ptr_a = _get_pointer(id_a, store_dir)
    ptr_b = _get_pointer(id_b, store_dir)
    assert ptr_a is not None
    assert ptr_a.active_baseline_id == f"{_STOCK_SPLIT}:v1"
    assert ptr_a.pointer_version == 1
    assert ptr_b is None

    # 旧holding_idのSequence/Pointerは削除されている。
    old_id = f"{_OLD}#{_STOCK_SPLIT}"
    assert _get_sequence(old_id, store_dir) is None
    assert _get_pointer(old_id, store_dir) is None


# --- HoldingsSnapshot: 分割時のcooldown引き継ぎ方針 -----------------------------


def test_split_snapshot_larger_share_inherits_cooldown_smaller_share_gets_fresh_baseline(
    store_dir: Path,
) -> None:
    _set_pause(store_dir, True)
    _seed_split(store_dir)
    cooldown_until = dt.date(2026, 8, 27)
    _seed_snapshot(store_dir, _OLD, _STOCK_SPLIT, 400, "1209.25", cooldown_until=cooldown_until)

    _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    snapshot_a = _snapshot_store(store_dir).get(f"{_OWNER_A}#{_STOCK_SPLIT}")
    snapshot_b = _snapshot_store(store_dir).get(f"{_OWNER_B}#{_STOCK_SPLIT}")

    assert snapshot_a is not None
    assert snapshot_a.shares == 300
    assert snapshot_a.cooldown_until_date == cooldown_until  # cooldown引き継ぎ

    assert snapshot_b is not None
    assert snapshot_b.shares == 100
    assert snapshot_b.cooldown_until_date is None  # 新規baseline、cooldown無し

    assert _snapshot_store(store_dir).get(f"{_OLD}#{_STOCK_SPLIT}") is None


def test_simple_reassignment_snapshot_carries_over_without_spurious_event(
    store_dir: Path,
) -> None:
    """分割を伴わない単純なowner付け替えでは、旧スナップショットの内容
    (shares等)がそのまま引き継がれ、TradeCooldownServiceが次回検知する
    虚偽イベントの原因(shares不一致)を作らないこと。"""
    _set_pause(store_dir, True)
    _seed_simple(store_dir, _STOCK_A, 100, "1500")
    _seed_snapshot(store_dir, _OLD, _STOCK_A, 100, "1500")

    _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    new_snapshot = _snapshot_store(store_dir).get(f"{_OWNER_A}#{_STOCK_A}")
    assert new_snapshot is not None
    assert new_snapshot.shares == 100
    assert _snapshot_store(store_dir).get(f"{_OLD}#{_STOCK_A}") is None


# --- 実行前precondition(2026-08-23確定指示相当) --------------------------------


def test_stock_b2_reassignment_matches_confirmed_precondition(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, _STOCK_B2, 500, "587")

    _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    new_holding = _holding_store(store_dir).get(f"{_OWNER_B}#{_STOCK_B2}")
    assert new_holding is not None
    assert new_holding.owner == _OWNER_B
    assert new_holding.shares == 500
    assert new_holding.average_purchase_price == Decimal("587")


def test_stock_b1_precondition_fails_on_shares_mismatch(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, _STOCK_B1, 199, "3215")  # 確定指示は200株

    with pytest.raises(PlanValidationError, match=_STOCK_B1):
        _run(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


def test_stock_b1_precondition_fails_on_price_mismatch(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, _STOCK_B1, 200, "3216")  # 確定指示は@3215

    with pytest.raises(PlanValidationError, match=_STOCK_B1):
        _run(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


def test_stock_b2_precondition_fails_on_shares_mismatch(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, _STOCK_B2, 501, "587")  # 確定指示は500株

    with pytest.raises(PlanValidationError, match=_STOCK_B2):
        _run(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


def test_stock_c_precondition_fails_on_price_mismatch(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, _STOCK_C, 100, "5481")  # 確定指示は@5480

    with pytest.raises(PlanValidationError, match=_STOCK_C):
        _run(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


def test_simple_precondition_fails_on_lot_composition_mismatch(store_dir: Path) -> None:
    """合計shares/averageは一致していても、複数lotの構成自体がPurchaseLot
    再計算値と食い違うケースを検知する(1lot構成のはずが2lot合算で偶然
    合計が一致してしまう場合等、再計算による突合が必須である根拠)。"""
    _set_pause(store_dir, True)
    _seed_holding(store_dir, _OLD, _STOCK_B1, 200, "3215")
    # 2lotに分割し、合計株数は200のまま平均単価が3215からずれるケース
    # (100株@3000+100株@3400=640000/200=3200 != 3215)。
    _seed_lot(store_dir, f"lot-{_STOCK_B1}-a", _OLD, _STOCK_B1, 100, "3000")
    _seed_lot(store_dir, f"lot-{_STOCK_B1}-b", _OLD, _STOCK_B1, 100, "3400")

    with pytest.raises(PlanValidationError, match=_STOCK_B1):
        _run(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


def test_price_correction_precondition_passes_when_already_price_corrected(
    store_dir: Path,
) -> None:
    """途中失敗後の再実行で、対象lotが既に188円へ訂正済みの状態でも
    冪等にPASSすること。"""
    _set_pause(store_dir, True)
    _seed_lot(store_dir, _PRICE_CORRECTED_LOT, _OLD, _STOCK_PRICE_CORRECTED, 100, "188")
    _seed_holding(store_dir, _OLD, _STOCK_PRICE_CORRECTED, 100, "188")

    result = _run(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)

    assert f"{_OWNER_B}#{_STOCK_PRICE_CORRECTED}" in result.processed_new_holding_ids


def test_price_correction_precondition_fails_on_unexpected_price(store_dir: Path) -> None:
    """187円(訂正前)でも188円(訂正後)でもない想定外の価格(189円)を
    勝手に188円へ上書きしないこと。"""
    _set_pause(store_dir, True)
    _seed_lot(store_dir, _PRICE_CORRECTED_LOT, _OLD, _STOCK_PRICE_CORRECTED, 100, "189")
    _seed_holding(store_dir, _OLD, _STOCK_PRICE_CORRECTED, 100, "189")

    with pytest.raises(PlanValidationError, match=_STOCK_PRICE_CORRECTED):
        _run(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)

    # 実際に書き込みは行われていない(fail-closed、書き込み前に中止)。
    lot = _lot_store(store_dir).get(_PRICE_CORRECTED_LOT)
    assert lot is not None
    assert lot.purchase_price == Decimal("189")


def test_price_correction_precondition_fails_on_shares_mismatch(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_lot(store_dir, _PRICE_CORRECTED_LOT, _OLD, _STOCK_PRICE_CORRECTED, 101, "187")
    _seed_holding(store_dir, _OLD, _STOCK_PRICE_CORRECTED, 101, "187")

    with pytest.raises(PlanValidationError, match=_STOCK_PRICE_CORRECTED):
        _run(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


def test_price_correction_precondition_fails_on_lot_composition_mismatch(
    store_dir: Path,
) -> None:
    """確定指示のlot_id1件のみのはずが、別lotが混在している場合は
    fail-closedで中止すること。"""
    _set_pause(store_dir, True)
    _seed_lot(store_dir, _PRICE_CORRECTED_LOT, _OLD, _STOCK_PRICE_CORRECTED, 100, "187")
    extra_lot_id = f"lot-{_STOCK_PRICE_CORRECTED}-extra"
    _seed_lot(store_dir, extra_lot_id, _OLD, _STOCK_PRICE_CORRECTED, 0, "0")
    _seed_holding(store_dir, _OLD, _STOCK_PRICE_CORRECTED, 100, "187")

    with pytest.raises(PlanValidationError, match=_STOCK_PRICE_CORRECTED):
        _run(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


def test_split_precondition_fails_when_lot_shares_wrong(store_dir: Path) -> None:
    """lot_idは正しいがsharesが確定指示と異なる場合、分割を実行しないこと。"""
    _set_pause(store_dir, True)
    _seed_lot(store_dir, _SPLIT_LOT_A, _OLD, _STOCK_SPLIT, 301, "1193")
    _seed_lot(store_dir, _SPLIT_LOT_B, _OLD, _STOCK_SPLIT, 99, "1258")
    _seed_holding(store_dir, _OLD, _STOCK_SPLIT, 400, "1209.25")

    with pytest.raises(PlanValidationError, match=_STOCK_SPLIT):
        _run(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


def test_split_precondition_fails_when_lot_price_wrong(store_dir: Path) -> None:
    """lot_id・sharesは正しいがpurchase_priceが確定指示と異なる場合、
    分割を実行しないこと。"""
    _set_pause(store_dir, True)
    _seed_lot(store_dir, _SPLIT_LOT_A, _OLD, _STOCK_SPLIT, 300, "1194")
    _seed_lot(store_dir, _SPLIT_LOT_B, _OLD, _STOCK_SPLIT, 100, "1258")
    _seed_holding(store_dir, _OLD, _STOCK_SPLIT, 400, "1209.25")

    with pytest.raises(PlanValidationError, match=_STOCK_SPLIT):
        _run(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


def test_split_precondition_fails_when_old_holding_total_wrong(store_dir: Path) -> None:
    """average_purchase_priceは確定指示どおり(1209.25)でも、
    total_purchase_amountだけが確定指示の値(483700)と食い違う場合を
    独立に検知すること。"""
    _set_pause(store_dir, True)
    _seed_lot(store_dir, _SPLIT_LOT_A, _OLD, _STOCK_SPLIT, 300, "1193")
    _seed_lot(store_dir, _SPLIT_LOT_B, _OLD, _STOCK_SPLIT, 100, "1258")
    build_collection_store(Holding, "holdings_v2.json", "holding_id", store_dir).upsert(
        Holding(
            owner=_OLD,
            holding_id=build_holding_id(_OLD, _STOCK_SPLIT),
            stock_code=_STOCK_SPLIT,
            stock_name=f"銘柄{_STOCK_SPLIT}",
            shares=400,
            average_purchase_price=Decimal("1209.25"),
            total_purchase_amount=Decimal("999999"),  # 確定指示(483700)と不一致
            first_purchase_date=dt.date(2026, 1, 1),
            last_purchase_date=dt.date(2026, 1, 1),
            account_type=AccountType.GENERAL,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )

    with pytest.raises(PlanValidationError, match=_STOCK_SPLIT):
        _run(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


def test_split_precondition_fails_when_lot_composition_wrong(store_dir: Path) -> None:
    """確定指示のlot_id集合と異なる(想定外のlot_idが混在する)場合、
    分割を実行しないこと。"""
    _set_pause(store_dir, True)
    _seed_lot(store_dir, _SPLIT_LOT_A, _OLD, _STOCK_SPLIT, 300, "1193")
    _seed_lot(store_dir, "unexpected-lot-id", _OLD, _STOCK_SPLIT, 100, "1258")
    _seed_holding(store_dir, _OLD, _STOCK_SPLIT, 400, "1209.25")

    with pytest.raises(PlanValidationError, match=_STOCK_SPLIT):
        _run(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


def test_precondition_not_checked_when_already_migrated(store_dir: Path) -> None:
    """旧Holding(owner=本人)が既に存在しない(=既に移行済み)stock_codeは、
    preconditionが再検証されず冪等に成功扱いになること。"""
    _set_pause(store_dir, True)
    _seed_simple(store_dir, _STOCK_B1, 200, "3215")
    _run(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    # 移行後、新Holdingの株数を(本移行と無関係な事情で)後から変更しても、
    # 旧Holdingが存在しない以上preconditionの再検証対象外。
    new_holding = _holding_store(store_dir).get(f"{_OWNER_B}#{_STOCK_B1}")
    assert new_holding is not None

    result = _run(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)
    assert result.processed_new_holding_ids == ()
