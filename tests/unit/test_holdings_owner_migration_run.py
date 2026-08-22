"""保有銘柄オーナー機能移行(M2)本体のテスト
(dry-run・冪等性・pause強制確認・sequence継続・pointer移行後update成功)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import (
    AccountType,
    BaselineOrigin,
    BaselineStatus,
    ExecutionMode,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holding import Holding, PurchaseLot
from jstock_advisor.domain.entities.holding_decision import (
    BaselineValueSnapshot,
    CompanyQualityScore,
    ComponentCoverage,
    ExecutionPlanReason,
    HoldingDecisionCategory,
    HoldingDecisionConfidenceLevel,
    HoldingDecisionHardGate,
    HoldingDecisionResult,
    InvestmentThesis,
    InvestmentThesisBaseline,
    InvestmentThesisScore,
    RiskDeductionScore,
)
from jstock_advisor.domain.entities.owner import InvalidOwnerError
from jstock_advisor.infrastructure.aws import (
    baseline_pointer,
    baseline_sequence,
    trading_pause_config,
)
from jstock_advisor.infrastructure.collection_store import build_collection_store
from jstock_advisor.migrations.baseline_migration import get_pointer_v2, get_sequence_v2
from jstock_advisor.migrations.holdings_owner_migration import (
    MigrationAbortedError,
    run_migration,
)
from jstock_advisor.migrations.target import MigrationTarget
from jstock_advisor.migrations.v2_entities import HoldingV2, PurchaseLotV2
from jstock_advisor.services.investment_thesis_service import InvestmentThesisService

_NOW = dt.datetime(2026, 8, 22, 0, 0, tzinfo=dt.UTC)
_STOCK = "8306"
_VALIDATION_CONTEXT = ExecutionContext(mode=ExecutionMode.VALIDATION)


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


def _seed_holding_and_lot(store_dir: Path, stock_code: str = _STOCK, shares: int = 100) -> None:
    build_collection_store(PurchaseLot, "purchase_lots.json", "lot_id", store_dir).upsert(
        PurchaseLot(
            lot_id="lot-1",
            stock_code=stock_code,
            purchase_date=dt.date(2026, 1, 1),
            shares=shares,
            purchase_price=Decimal("1500"),
            account_type=AccountType.GENERAL,
        )
    )
    build_collection_store(Holding, "holdings.json", "stock_code", store_dir).upsert(
        Holding(
            stock_code=stock_code,
            stock_name="三菱UFJ",
            shares=shares,
            average_purchase_price=Decimal("1500"),
            total_purchase_amount=Decimal("1500") * shares,
            first_purchase_date=dt.date(2026, 1, 1),
            last_purchase_date=dt.date(2026, 1, 1),
            account_type=AccountType.GENERAL,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )


def _holding_decision_result(
    holding_decision_result_id: str, holding_id: str
) -> HoldingDecisionResult:
    return HoldingDecisionResult(
        holding_decision_result_id=holding_decision_result_id,
        holding_id=holding_id,
        stock_code=_STOCK,
        evaluated_at=_NOW,
        company_quality=CompanyQualityScore(score=30.0, coverage_ratio=1.0),
        investment_thesis=InvestmentThesisScore(score=25.0, coverage_ratio=1.0),
        risk_deduction=RiskDeductionScore(score=10.0, coverage_ratio=1.0),
        base_score=45.0,
        hard_gate=HoldingDecisionHardGate(triggered=False),
        final_score=45.0,
        display_value=45,
        category=HoldingDecisionCategory.SELL_CONSIDERATION,
        coverage=ComponentCoverage(
            overall=1.0, company_quality=1.0, investment_thesis=1.0, risk_deduction=1.0
        ),
        confidence=HoldingDecisionConfidenceLevel.HIGH,
        should_notify=True,
        scoring_model_version=1,
        runtime_config_version=1,
        execution_plan_reason=ExecutionPlanReason.NORMAL_ACTIVE,
    )


# --- pause強制確認 -----------------------------------------------------------


def test_migration_refuses_when_pause_unset(store_dir: Path) -> None:
    with pytest.raises(MigrationAbortedError, match="未初期化"):
        run_migration(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


def test_migration_refuses_when_pause_false(store_dir: Path) -> None:
    _set_pause(store_dir, False)
    with pytest.raises(MigrationAbortedError, match="pause_buy_sell=false"):
        run_migration(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


def test_migration_allowed_when_pause_true(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_holding_and_lot(store_dir)
    result = run_migration(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)
    assert result.preflight.passed is True


def test_migration_refuses_when_pause_get_raises(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(store_dir: Path | None = None) -> None:
        raise RuntimeError("simulated DynamoDB failure")

    monkeypatch.setattr(trading_pause_config, "get", _boom)
    with pytest.raises(MigrationAbortedError, match="取得に失敗"):
        run_migration(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


def test_migration_refuses_when_preflight_fails(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    # 孤立したPurchaseLot(対応するHoldingが無い)によりpreflightを失敗させる。
    build_collection_store(PurchaseLot, "purchase_lots.json", "lot_id", store_dir).upsert(
        PurchaseLot(
            lot_id="lot-orphan",
            stock_code="9999",
            purchase_date=dt.date(2026, 1, 1),
            shares=10,
            purchase_price=Decimal("1000"),
            account_type=AccountType.GENERAL,
        )
    )
    with pytest.raises(MigrationAbortedError, match="preflight"):
        run_migration(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)


# --- dry-run ----------------------------------------------------------------


def test_dry_run_writes_nothing(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_holding_and_lot(store_dir)

    result = run_migration(MigrationTarget.LOCAL, dry_run=True, store_dir=store_dir)

    assert result.dry_run is True
    assert result.counts_written["holdings"] == 1
    v2_store = build_collection_store(HoldingV2, "holdings_v2.json", "holding_id", store_dir)
    assert v2_store.list_all() == []
    assert not (store_dir / "holdings_v2.json").exists()


# --- 実行・冪等性 -------------------------------------------------------------


def test_run_migrates_holding_and_purchase_lot(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_holding_and_lot(store_dir)

    result = run_migration(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    assert result.counts_written["holdings"] == 1
    assert result.counts_written["purchase_lots"] == 1

    holding_v2 = build_collection_store(
        HoldingV2, "holdings_v2.json", "holding_id", store_dir
    ).get("本人#8306")
    assert holding_v2 is not None
    assert holding_v2.owner == "本人"
    assert holding_v2.shares == 100

    lot_v2 = build_collection_store(PurchaseLotV2, "purchase_lots.json", "lot_id", store_dir).get(
        "lot-1"
    )
    assert lot_v2 is not None
    assert lot_v2.owner == "本人"
    assert lot_v2.holding_id == "本人#8306"


def test_run_migration_is_idempotent_on_rerun(store_dir: Path) -> None:
    _set_pause(store_dir, True)
    _seed_holding_and_lot(store_dir)

    run_migration(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)
    result_second = run_migration(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    assert result_second.counts_written["holdings"] == 1
    holdings_v2 = build_collection_store(
        HoldingV2, "holdings_v2.json", "holding_id", store_dir
    ).list_all()
    assert len(holdings_v2) == 1  # 重複していない

    lots_v2 = build_collection_store(
        PurchaseLotV2, "purchase_lots.json", "lot_id", store_dir
    ).list_all()
    assert len(lots_v2) == 1  # 重複していない


# --- InvestmentThesisBaselineSequence継続(必須回帰テスト) ---------------------


def test_baseline_sequence_current_version_continues_after_migration(store_dir: Path) -> None:
    """旧: holding_id="8306"・current_version=4 → migration →
    新: holding_id="本人#8306"・current_version=4 →
    allocate_next_baseline_version() → 結果: 5。"""
    _set_pause(store_dir, True)
    _seed_holding_and_lot(store_dir)
    for _ in range(4):
        baseline_sequence.allocate_next_baseline_version("8306", store_dir=store_dir)

    run_migration(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    migrated = get_sequence_v2("本人#8306", MigrationTarget.LOCAL, store_dir)
    assert migrated is not None
    assert migrated.current_version == 4  # リセットされていない

    # V2テーブル用の新しい採番モジュールを使い、続きから採番されることを確認する
    # (production側のallocate_next_baseline_version()はV1固定のため、ここでは
    # V2テーブルへ直接同じADD操作を適用して確認する)。
    from jstock_advisor.infrastructure.local_repository.json_store import JsonCollectionStore
    from jstock_advisor.migrations.legacy_shapes import LegacyBaselineSequenceCounterV1

    store = JsonCollectionStore(
        LegacyBaselineSequenceCounterV1,
        "investment_thesis_baseline_sequences_v2.json",
        "holding_id",
        store_dir,
    )
    current = store.get("本人#8306")
    assert current is not None
    next_version = current.current_version + 1
    store.upsert(current.model_copy(update={"current_version": next_version}))
    assert next_version == 5


# --- InvestmentThesisBaselinePointer移行後のupdate成功 ------------------------


def test_baseline_pointer_migration_then_update_succeeds(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """migration → get_pointer() → update_pointer() → get_pointer() が
    production側の実関数(baseline_pointer.py)で成立することを確認する
    (本番検証で修正済みのdataブロブCAS形式が、V2テーブルでも維持されている
    ことの回帰テスト)。"""
    _set_pause(store_dir, True)
    _seed_holding_and_lot(store_dir)
    thesis_service = InvestmentThesisService(store_dir=store_dir)
    thesis_service.activate_baseline(
        "8306",
        "8306",
        BaselineOrigin.SYSTEM_INITIALIZED,
        BaselineValueSnapshot(total_yield_pct=4.0, equity_ratio_pct=45.0),
    )

    run_migration(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    migrated_pointer = get_pointer_v2("本人#8306", store_dir)
    assert migrated_pointer is not None
    assert migrated_pointer.pointer_version == 1
    assert migrated_pointer.active_baseline_version == 1

    # production側のget_pointer/update_pointerをV2ファイルへ向けて実行し、
    # 実際に使われる関数で往復できることを確認する。
    monkeypatch.setattr(
        baseline_pointer, "_TABLE_FILE_NAME", "investment_thesis_baseline_pointers_v2.json"
    )

    fetched = baseline_pointer.get_pointer("本人#8306", store_dir)
    assert fetched is not None
    assert fetched.pointer_version == 1

    updated = baseline_pointer.update_pointer(
        holding_id="本人#8306",
        new_baseline_id="本人#8306:v2",
        new_baseline_version=2,
        expected_pointer_version=1,
        updated_by="tester",
        store_dir=store_dir,
    )
    assert updated.pointer_version == 2

    refetched = baseline_pointer.get_pointer("本人#8306", store_dir)
    assert refetched is not None
    assert refetched.pointer_version == 2
    assert refetched.active_baseline_version == 2


# --- holding_id "field-only"移行の冪等性・fail-closed(必須2) -----------------


def test_migration_aborts_when_holding_id_field_has_mismatched_owner(store_dir: Path) -> None:
    """HoldingDecisionResult等のholding_idが既に別ownerで移行済みだった場合、
    誤って上書き/二重prefix化せずfail-closedで中止すること。"""
    _set_pause(store_dir, True)
    _seed_holding_and_lot(store_dir)
    store = build_collection_store(
        HoldingDecisionResult,
        "holding_decision_results.json",
        "holding_decision_result_id",
        store_dir,
    )
    store.upsert(_holding_decision_result("hdr-1", holding_id="子供#8306"))

    with pytest.raises(InvalidOwnerError):
        run_migration(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)


def test_migration_retry_after_partial_failure_does_not_double_prefix_holding_id(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """holding_decision_results(前半)の移行が成功した直後にinvestment_theses
    (後半)側で技術的障害が発生してmigrationが中止された場合でも、再実行時に
    holding_decision_resultsが"本人#本人#8306"のような二重prefixにならず、
    investment_theses/investment_thesis_baselinesも正しく1回だけ移行される
    こと(必須テスト: 前半成功→後半で例外→再実行)。"""
    _set_pause(store_dir, True)
    _seed_holding_and_lot(store_dir)

    hdr_store = build_collection_store(
        HoldingDecisionResult,
        "holding_decision_results.json",
        "holding_decision_result_id",
        store_dir,
    )
    hdr_store.upsert(_holding_decision_result("hdr-1", holding_id=_STOCK))

    thesis_store = build_collection_store(
        InvestmentThesis, "investment_theses.json", "investment_thesis_id", store_dir
    )
    thesis_store.upsert(
        InvestmentThesis(
            investment_thesis_id="thesis-1", holding_id=_STOCK, stock_code=_STOCK, updated_at=_NOW
        )
    )

    baseline_store = build_collection_store(
        InvestmentThesisBaseline, "investment_thesis_baselines.json", "baseline_id", store_dir
    )
    baseline_store.upsert(
        InvestmentThesisBaseline(
            baseline_id=f"{_STOCK}:v1",
            holding_id=_STOCK,
            stock_code=_STOCK,
            version=1,
            origin=BaselineOrigin.SYSTEM_INITIALIZED,
            status=BaselineStatus.APPROVED,
            created_at=_NOW,
            baseline_values=BaselineValueSnapshot(total_yield_pct=4.0, equity_ratio_pct=45.0),
        )
    )

    original_build_collection_store = build_collection_store
    call_state = {"raised": False}

    def _flaky_build_collection_store(
        model_type: Any,
        file_name: str,
        id_field: str,
        store_dir_arg: Path | None = None,
        ttl_seconds: int | None = None,
    ) -> Any:
        if file_name == "investment_theses.json" and not call_state["raised"]:
            call_state["raised"] = True
            raise RuntimeError("simulated transient failure while migrating investment_theses")
        return original_build_collection_store(
            model_type, file_name, id_field, store_dir_arg, ttl_seconds=ttl_seconds
        )

    monkeypatch.setattr(
        "jstock_advisor.migrations.holdings_owner_migration.build_collection_store",
        _flaky_build_collection_store,
    )

    with pytest.raises(RuntimeError, match="simulated transient failure"):
        run_migration(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)

    # 前半(holding_decision_results)は既に正しく移行済みであること。
    migrated_hdr = hdr_store.get("hdr-1")
    assert migrated_hdr is not None
    assert migrated_hdr.holding_id == "本人#8306"

    # 後半(investment_theses)はまだ未移行のまま(旧形式)であること。
    unmigrated_thesis = thesis_store.get("thesis-1")
    assert unmigrated_thesis is not None
    assert unmigrated_thesis.holding_id == _STOCK

    monkeypatch.undo()

    result = run_migration(MigrationTarget.LOCAL, dry_run=False, store_dir=store_dir)
    assert result.preflight.passed is True

    migrated_hdr_again = hdr_store.get("hdr-1")
    migrated_thesis = thesis_store.get("thesis-1")
    migrated_baseline = baseline_store.get(f"{_STOCK}:v1")
    assert migrated_hdr_again is not None
    assert migrated_thesis is not None
    assert migrated_baseline is not None
    # 再実行後、いずれも二重prefixにならず正しく"本人#8306"であること。
    assert migrated_hdr_again.holding_id == "本人#8306"
    assert migrated_thesis.holding_id == "本人#8306"
    assert migrated_baseline.holding_id == "本人#8306"
