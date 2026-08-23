"""既存保有データのowner実態補正(M4.1)のテスト。

4680(ラウンドワン)分割、9434(ソフトバンク)単価訂正、pause強制確認、
holding_id衝突検知、dry-run、冪等性・途中失敗後再実行、tombstone不変、
InvestmentThesis/BaselineSequence/BaselinePointerの分割時継承方針を検証する。
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
    ReclassificationAbortedError,
    run_reclassification,
)
from jstock_advisor.migrations.legacy_shapes import LegacyBaselineSequenceCounterV1
from jstock_advisor.migrations.target import MigrationTarget
from jstock_advisor.services.investment_thesis_service import InvestmentThesisService

_NOW = dt.datetime(2026, 8, 23, 0, 0, tzinfo=dt.UTC)
_OLD = "本人"


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


def _seed_4680_split(store_dir: Path) -> None:
    _seed_lot(store_dir, "295d6620-bea5-464b-8f37-e887df26bc3d", _OLD, "4680", 300, "1193")
    _seed_lot(store_dir, "f86f9ed3-3a78-4784-943e-2925d591b4e4", _OLD, "4680", 100, "1258")
    _seed_holding(store_dir, _OLD, "4680", 400, "1225.75")  # (300*1193+100*1258)/400


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
    _seed_simple(store_dir, "8306", 100, "1500")
    with pytest.raises(ReclassificationAbortedError, match="pause_buy_sell=false"):
        run_reclassification(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


def test_refuses_when_pause_unset(store_dir: Path) -> None:
    with pytest.raises(ReclassificationAbortedError, match="未初期化"):
        run_reclassification(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


# --- 通常Holding / 子供Holding のowner付け替え --------------------------------


def test_default_owner_reassignment_8306_to_koichi(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, "8306", 100, "1500")

    result = run_reclassification(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    assert "所有者A#8306" in result.processed_new_holding_ids
    new_holding = _holding_store(store_dir).get("所有者A#8306")
    assert new_holding is not None
    assert new_holding.owner == "所有者A"
    assert new_holding.shares == 100
    assert _holding_store(store_dir).get("本人#8306") is None
    lot = _lot_store(store_dir).get("lot-8306")
    assert lot is not None
    assert lot.owner == "所有者A"
    assert lot.holding_id == "所有者A#8306"


def test_child_owner_reassignment_2269_to_kazuho(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, "2269", 200, "3215")

    run_reclassification(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    new_holding = _holding_store(store_dir).get("所有者B#2269")
    assert new_holding is not None
    assert new_holding.owner == "所有者B"
    assert _holding_store(store_dir).get("本人#2269") is None


def test_child_owner_reassignment_8566_to_ryosuke(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, "8566", 100, "5480")

    run_reclassification(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    new_holding = _holding_store(store_dir).get("所有者C#8566")
    assert new_holding is not None
    assert new_holding.owner == "所有者C"


# --- 4680分割 ----------------------------------------------------------------


def test_4680_splits_into_two_holdings(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_4680_split(store_dir)

    result = run_reclassification(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    assert set(result.processed_new_holding_ids) == {"所有者A#4680", "所有者B#4680"}
    koichi = _holding_store(store_dir).get("所有者A#4680")
    kazuho = _holding_store(store_dir).get("所有者B#4680")
    assert koichi is not None and kazuho is not None
    assert koichi.shares == 300
    assert koichi.average_purchase_price == Decimal("1193")
    assert kazuho.shares == 100
    assert kazuho.average_purchase_price == Decimal("1258")
    assert _holding_store(store_dir).get("本人#4680") is None


def test_4680_lot_ids_unchanged_only_owner_fields_updated(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_4680_split(store_dir)

    run_reclassification(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    koichi_lot = _lot_store(store_dir).get("295d6620-bea5-464b-8f37-e887df26bc3d")
    kazuho_lot = _lot_store(store_dir).get("f86f9ed3-3a78-4784-943e-2925d591b4e4")
    assert koichi_lot is not None
    assert koichi_lot.owner == "所有者A"
    assert koichi_lot.holding_id == "所有者A#4680"
    assert koichi_lot.shares == 300
    assert kazuho_lot is not None
    assert kazuho_lot.owner == "所有者B"
    assert kazuho_lot.holding_id == "所有者B#4680"
    assert kazuho_lot.shares == 100


# --- 9434価格訂正 --------------------------------------------------------------


def test_9434_price_corrected_with_matching_lot_id(store_dir: Path) -> None:
    """9434の実lot_id(e5865e06-...)は本番データ由来の固定値であり、
    PRICE_CORRECTIONSがこの特定lot_idにのみ紐づくことを確認する
    (別lot_idでは補正されない、という設計自体は
    test_other_stock_prices_unchangedで別途確認)。"""
    _set_pause(store_dir, True)
    _seed_lot(store_dir, "e5865e06-c43b-47ae-baa9-fc8a133482aa", _OLD, "9434", 100, "187")
    _seed_holding(store_dir, _OLD, "9434", 100, "187")

    run_reclassification(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    lot = _lot_store(store_dir).get("e5865e06-c43b-47ae-baa9-fc8a133482aa")
    holding = _holding_store(store_dir).get("所有者B#9434")
    assert lot is not None
    assert lot.purchase_price == Decimal("188")
    assert holding is not None
    assert holding.average_purchase_price == Decimal("188")
    assert holding.total_purchase_amount == Decimal("18800")


def test_other_stock_prices_unchanged(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, "8306", 100, "1500")
    _seed_simple(store_dir, "2269", 200, "3215")

    run_reclassification(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    assert _lot_store(store_dir).get("lot-8306").purchase_price == Decimal("1500")  # type: ignore[union-attr]
    assert _lot_store(store_dir).get("lot-2269").purchase_price == Decimal("3215")  # type: ignore[union-attr]


# --- holding_id衝突検知 --------------------------------------------------------


def test_fails_closed_on_holding_id_collision_with_existing_holding(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, "8306", 100, "1500")
    # 既に所有者A#8306が(本移行対象外の理由で)存在するという衝突状態を再現する。
    _seed_holding(store_dir, "所有者A", "8306", 999, "1")

    with pytest.raises(PlanValidationError, match="衝突"):
        run_reclassification(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


# --- dry-run --------------------------------------------------------------


def test_dry_run_writes_nothing(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, "8306", 100, "1500")

    result = run_reclassification(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)

    assert result.dry_run is True
    assert "所有者A#8306" in result.processed_new_holding_ids
    assert _holding_store(store_dir).get("本人#8306") is not None
    assert _holding_store(store_dir).get("所有者A#8306") is None


# --- 冪等性・途中失敗後再実行 ---------------------------------------------------


def test_rerun_after_full_success_is_idempotent_noop(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, "8306", 100, "1500")

    run_reclassification(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)
    result_second = run_reclassification(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    assert result_second.processed_new_holding_ids == ()
    holdings = _holding_store(store_dir).list_all()
    assert len(holdings) == 1
    assert holdings[0].holding_id == "所有者A#8306"


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
    """4680の分割処理中、片方のHolding upsert成功直後に技術的障害が発生した
    場合でも、再実行でもう片方まで正しく完了し、旧本人#4680が削除されること
    (途中失敗→再実行の必須テスト)。"""
    _set_pause(store_dir, True)
    _seed_4680_split(store_dir)

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
        run_reclassification(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    monkeypatch.undo()

    # 本人#4680はまだ存在する(旧Holding削除はグループ完了時のみ)。
    assert _holding_store(store_dir).get("本人#4680") is not None

    result = run_reclassification(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    assert set(result.processed_new_holding_ids) == {"所有者A#4680", "所有者B#4680"}
    assert _holding_store(store_dir).get("本人#4680") is None
    koichi = _holding_store(store_dir).get("所有者A#4680")
    kazuho = _holding_store(store_dir).get("所有者B#4680")
    assert koichi is not None and koichi.shares == 300
    assert kazuho is not None and kazuho.shares == 100


# --- owner値の自由度(allow-listではない) --------------------------------------


def test_owner_is_not_restricted_to_allow_list(store_dir: Path) -> None:
    """本モジュールのマッピング定数(所有者A/所有者B/所有者C)は今回の実データ入力に
    すぎず、owner型自体がEnum/allow-listでないことを、通常運用側
    (PortfolioService経由のHolding作成)で別owner文字列を使い確認する。"""
    from jstock_advisor.domain.entities.owner import normalize_and_validate_owner

    # 今回のマッピングに含まれない任意のowner文字列でも正規化・検証が通る
    # (M4.1のマッピングが正規化ロジック自体を制限していないことの確認)。
    assert normalize_and_validate_owner("第三子") == "第三子"


# --- tombstone不変 -------------------------------------------------------------


def test_tombstone_4631_snapshot_untouched(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_simple(store_dir, "8306", 100, "1500")
    # 4631は対応するHoldingが存在しないtombstone(全部売却済み)。
    _seed_snapshot(store_dir, _OLD, "4631", 0, "0")
    tombstone_before = _snapshot_store(store_dir).get("本人#4631")
    assert tombstone_before is not None

    run_reclassification(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    tombstone_after = _snapshot_store(store_dir).get("本人#4631")
    assert tombstone_after is not None
    assert tombstone_after == tombstone_before


# --- InvestmentThesis: 4680分割時は所有者A側のみ継承 -------------------------------


def test_4680_investment_thesis_inherited_by_koichi_only(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_4680_split(store_dir)
    _seed_thesis(store_dir, _OLD, "4680", "thesis-4680")

    run_reclassification(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    koichi_thesis = _thesis_store(store_dir).get("thesis-4680")
    assert koichi_thesis is not None
    assert koichi_thesis.holding_id == "所有者A#4680"

    all_theses = _thesis_store(store_dir).list_all()
    kazuho_theses = [t for t in all_theses if t.holding_id == "所有者B#4680"]
    assert kazuho_theses == []


def test_4680_kazuho_thesis_created_lazily_via_get_or_create(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_4680_split(store_dir)
    _seed_thesis(store_dir, _OLD, "4680", "thesis-4680")

    run_reclassification(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    service = InvestmentThesisService(store_dir=store_dir)
    kazuho_thesis = service.get_or_create_thesis(holding_id="所有者B#4680", stock_code="4680")

    assert kazuho_thesis.investment_thesis_id != "thesis-4680"
    assert kazuho_thesis.conditions == []
    assert kazuho_thesis.holding_id == "所有者B#4680"


# --- BaselineSequence/BaselinePointer: 4680分割時の継承方針 ---------------------


def test_4680_baseline_sequence_and_pointer_inherited_by_koichi_only(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_4680_split(store_dir)
    _seed_sequence(store_dir, _OLD, "4680", current_version=1)
    _seed_pointer(store_dir, _OLD, "4680", baseline_version=1)

    run_reclassification(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    from jstock_advisor.migrations.holdings_owner_reclassification import (
        _get_pointer,
        _get_sequence,
    )

    koichi_seq = _get_sequence("所有者A#4680", store_dir)
    kazuho_seq = _get_sequence("所有者B#4680", store_dir)
    assert koichi_seq is not None
    assert koichi_seq.current_version == 1
    assert kazuho_seq is None

    koichi_ptr = _get_pointer("所有者A#4680", store_dir)
    kazuho_ptr = _get_pointer("所有者B#4680", store_dir)
    assert koichi_ptr is not None
    assert koichi_ptr.active_baseline_id == "4680:v1"
    assert koichi_ptr.pointer_version == 1
    assert kazuho_ptr is None

    # 旧holding_idのSequence/Pointerは削除されている。
    assert _get_sequence("本人#4680", store_dir) is None
    assert _get_pointer("本人#4680", store_dir) is None


# --- HoldingsSnapshot: 4680分割時のcooldown引き継ぎ方針 -------------------------


def test_4680_snapshot_koichi_inherits_cooldown_kazuho_gets_fresh_baseline(
    store_dir: Path,
) -> None:
    _set_pause(store_dir, True)
    _seed_4680_split(store_dir)
    cooldown_until = dt.date(2026, 8, 27)
    _seed_snapshot(store_dir, _OLD, "4680", 400, "1225.75", cooldown_until=cooldown_until)

    run_reclassification(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    koichi_snapshot = _snapshot_store(store_dir).get("所有者A#4680")
    kazuho_snapshot = _snapshot_store(store_dir).get("所有者B#4680")

    assert koichi_snapshot is not None
    assert koichi_snapshot.shares == 300
    assert koichi_snapshot.cooldown_until_date == cooldown_until  # cooldown引き継ぎ

    assert kazuho_snapshot is not None
    assert kazuho_snapshot.shares == 100
    assert kazuho_snapshot.cooldown_until_date is None  # 新規baseline、cooldown無し

    assert _snapshot_store(store_dir).get("本人#4680") is None


def test_simple_reassignment_snapshot_carries_over_without_spurious_event(
    store_dir: Path,
) -> None:
    """分割を伴わない単純なowner付け替えでは、旧スナップショットの内容
    (shares等)がそのまま引き継がれ、TradeCooldownServiceが次回検知する
    虚偽イベントの原因(shares不一致)を作らないこと。"""
    _set_pause(store_dir, True)
    _seed_simple(store_dir, "8306", 100, "1500")
    _seed_snapshot(store_dir, _OLD, "8306", 100, "1500")

    run_reclassification(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    new_snapshot = _snapshot_store(store_dir).get("所有者A#8306")
    assert new_snapshot is not None
    assert new_snapshot.shares == 100
    assert _snapshot_store(store_dir).get("本人#8306") is None
