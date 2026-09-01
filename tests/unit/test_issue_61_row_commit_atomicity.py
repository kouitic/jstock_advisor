"""Issue #61 Phase B1 レビュー指摘R1: 行コミットの原子性の回帰テスト。

当初実装は「適用 → 台帳へ記録」の順で別々に書いていたため、その間で失敗すると
**適用済みなのに台帳が無い**状態が残り、再実行で二重計上しうる窓があった。

本モジュールは、各永続write境界へ例外を注入して

- 部分状態が残らないこと
- 再実行が必ず1回だけ適用されること(二重計上も欠落も起きないこと)

を固定する。OSレベルのprocess killは再現せず、例外注入で境界を検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.services.csv_import_ledger import (
    DECISION_TYPE_HOLDING_IMPORT_ROW,
    CsvImportLedger,
    build_row_audit_id,
    build_row_lot_id,
    compute_import_id,
)
from jstock_advisor.services.csv_import_service import CsvRowStatus, HoldingsCsvImportService
from jstock_advisor.services.portfolio_service import PortfolioService

_OWNER = "所有者A"
_NOW = dt.datetime(2026, 9, 2, tzinfo=dt.UTC)
_CSV = (
    "owner,stock_code,shares,purchase_price,purchase_date,account_type\n"
    f"{_OWNER},2914,100,4200,2025-05-10,NISA\n"
)


def _write(tmp_path: Path, content: str = _CSV) -> Path:
    path = tmp_path / "holdings.csv"
    path.write_text(content, encoding="utf-8-sig")
    return path


def _state(portfolio: PortfolioService) -> tuple[int | None, int]:
    holding = portfolio.get_holding(_OWNER, "2914")
    lots = portfolio.list_lots(_OWNER, "2914")
    return (holding.shares if holding else None, len(lots))


def _ledger_entries(store_dir: Path) -> list[Any]:
    return AuditLogRepository(store_dir=store_dir).list_by_decision_type(
        DECISION_TYPE_HOLDING_IMPORT_ROW
    )


# --- 適用済みの判定材料 -------------------------------------------------------


def test_applied_state_is_determined_by_persisted_data_not_by_ledger(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
    store_dir: Path,
) -> None:
    """**台帳が消えても再適用で二重計上しない**(R1の中核)。

    「この行は適用済みか」は別置きの台帳ではなく、決定的lot_idのロットの存在で
    判定する。したがって台帳と実データがずれても二重計上は起きない。
    """
    path = _write(tmp_path)
    csv_import_service.import_file(path)
    assert _state(portfolio_service) == (100, 1)

    # 台帳だけが失われた状態を作る(適用済み・台帳なし)。
    import_id = compute_import_id(path.read_bytes())
    AuditLogRepository(store_dir=store_dir).delete(build_row_audit_id(import_id, 2))
    assert _ledger_entries(store_dir) == []

    summary = csv_import_service.import_file(path)

    assert _state(portfolio_service) == (100, 1), "台帳欠落で再適用され二重計上している"
    assert summary.results[0].status == CsvRowStatus.SKIPPED_DUPLICATE


def test_lot_id_is_deterministic_for_the_same_row(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    path = _write(tmp_path)
    csv_import_service.import_file(path)
    import_id = compute_import_id(path.read_bytes())
    assert portfolio_service.lot_exists(build_row_lot_id(import_id, 2)) is True


# --- 各永続write境界への例外注入 ----------------------------------------------


def test_ledger_claim_failure_applies_nothing(
    tmp_path: Path,
    portfolio_service: PortfolioService,
    csv_import_ledger: CsvImportLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """台帳のclaim自体が失敗した場合、保有・ロットは一切変化しない。

    claimをデータ適用より**先**に行うことで、
    「holding applied / ledger missing」という禁止状態を作らない。
    """

    def _boom(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("simulated ledger claim failure")

    monkeypatch.setattr(csv_import_ledger, "claim", _boom)
    service = HoldingsCsvImportService(
        portfolio_service=portfolio_service, ledger=csv_import_ledger
    )

    with pytest.raises(RuntimeError):
        service.import_file(_write(tmp_path))

    assert _state(portfolio_service) == (None, 0)


def test_lot_write_failure_leaves_no_partial_state_and_retry_applies_once(
    tmp_path: Path,
    portfolio_service: PortfolioService,
    csv_import_ledger: CsvImportLedger,
    store_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ロット書き込みで失敗 → 保有もロットも台帳も残らず、再実行で1回だけ適用。"""
    service = HoldingsCsvImportService(
        portfolio_service=portfolio_service, ledger=csv_import_ledger
    )
    path = _write(tmp_path)

    original = portfolio_service.register_purchase

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated lot/holding write failure")

    monkeypatch.setattr(portfolio_service, "register_purchase", _boom)
    with pytest.raises(RuntimeError):
        service.import_file(path)

    assert _state(portfolio_service) == (None, 0)
    assert _ledger_entries(store_dir) == [], "失敗したclaimが解放されていない"

    monkeypatch.setattr(portfolio_service, "register_purchase", original)
    summary = service.import_file(path)

    assert _state(portfolio_service) == (100, 1)
    assert summary.results[0].status != CsvRowStatus.SKIPPED_DUPLICATE
    assert len(_ledger_entries(store_dir)) == 1


def test_release_failure_still_retries_without_double_count(
    tmp_path: Path,
    portfolio_service: PortfolioService,
    csv_import_ledger: CsvImportLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claimの解放にも失敗した最悪ケースでも、二重計上も欠落も起きない。

    「適用済みか」の判定がclaimではなく永続データ(lot)であるため、
    claimが残っていても再実行で正しく適用される。
    """
    service = HoldingsCsvImportService(
        portfolio_service=portfolio_service, ledger=csv_import_ledger
    )
    path = _write(tmp_path)

    def _boom_apply(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated apply failure")

    def _boom_release(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated release failure")

    monkeypatch.setattr(portfolio_service, "register_purchase", _boom_apply)
    monkeypatch.setattr(csv_import_ledger, "release", _boom_release)
    with pytest.raises(RuntimeError):
        service.import_file(path)
    assert _state(portfolio_service) == (None, 0)

    # claimが残ったまま再実行しても、実データ基準で未適用と判定され適用される。
    monkeypatch.undo()
    summary = service.import_file(path)
    assert _state(portfolio_service) == (100, 1), "claim残留で欠落している"
    assert summary.results[0].status != CsvRowStatus.SKIPPED_DUPLICATE

    # さらにもう一度実行しても二重計上しない。
    service.import_file(path)
    assert _state(portfolio_service) == (100, 1)


# --- 同時実行 -----------------------------------------------------------------


def test_concurrent_same_row_commit_applies_once(
    tmp_path: Path,
    portfolio_service: PortfolioService,
    csv_import_ledger: CsvImportLedger,
) -> None:
    """同一row_idを2つのサービス実体が処理しても、適用は1回だけ。

    2つのサービスは同じ永続層を共有し、片方が先にclaimを獲得する。
    """
    first = HoldingsCsvImportService(
        portfolio_service=portfolio_service, ledger=csv_import_ledger
    )
    second = HoldingsCsvImportService(
        portfolio_service=portfolio_service, ledger=csv_import_ledger
    )
    path = _write(tmp_path)

    first.import_file(path)
    summary = second.import_file(path)

    assert _state(portfolio_service) == (100, 1)
    assert summary.results[0].status == CsvRowStatus.SKIPPED_DUPLICATE


def test_claim_without_applied_data_is_recovered_not_skipped(
    tmp_path: Path,
    portfolio_service: PortfolioService,
    csv_import_ledger: CsvImportLedger,
) -> None:
    """claimだけが残り実データが未適用の場合は、skipせず**適用して回復**する。

    ここでclaimを権威にしてskipすると、その行は再実行しても永久に登録されない
    (データ欠落)。権威は永続データ(決定的lot_idのロット)側に置く。
    """
    service = HoldingsCsvImportService(
        portfolio_service=portfolio_service, ledger=csv_import_ledger
    )
    path = _write(tmp_path)
    import_id = compute_import_id(path.read_bytes())

    # 取り残されたclaim(適用前に落ちて解放にも失敗した状態)を作る。
    assert (
        csv_import_ledger.claim(
            import_id, 2, owner=_OWNER, stock_code="2914", shares=100, now=_NOW
        )
        is True
    )

    summary = service.import_file(path)

    assert summary.results[0].status != CsvRowStatus.SKIPPED_DUPLICATE
    assert _state(portfolio_service) == (100, 1)


def test_applying_the_same_row_twice_converges(
    tmp_path: Path,
    portfolio_service: PortfolioService,
    csv_import_ledger: CsvImportLedger,
) -> None:
    """同一行を2回適用しても最終状態が変わらない(収束する)。

    同時実行で2プロセスが同じ行を適用してしまった場合でも、決定的lot_idと
    「Holdingはロット集合からの再計算」により、**適用の効果は1回分**になる。
    """
    service = HoldingsCsvImportService(
        portfolio_service=portfolio_service, ledger=csv_import_ledger
    )
    path = _write(tmp_path)
    import_id = compute_import_id(path.read_bytes())
    lot_id = build_row_lot_id(import_id, 2)

    service.import_file(path)
    assert _state(portfolio_service) == (100, 1)

    # claimも実データも消さずに、適用処理だけをもう一度直接走らせる
    # (同時実行で2プロセスが同じ行を適用してしまった状況の再現)。
    portfolio_service.register_purchase(
        owner=_OWNER,
        stock_code="2914",
        stock_name=None,
        shares=100,
        purchase_price=Decimal("4200"),
        purchase_date=dt.date(2025, 5, 10),
        account_type=AccountType.NISA,
        lot_id=lot_id,
    )

    assert _state(portfolio_service) == (100, 1), "同一lot_idの再適用で二重計上している"
