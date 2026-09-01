"""services/csv_import_ledger.py のテスト(Issue #61 Phase B1)。

CSV取込の「適用済み」台帳の identity 設計を固定する。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.services.csv_import_ledger import (
    DECISION_TYPE_HOLDING_IMPORT_ROW,
    CsvImportLedger,
    build_row_audit_id,
    compute_import_id,
)

_NOW = dt.datetime(2026, 9, 2, 0, 0, tzinfo=dt.UTC)
_CONTENT = "owner,stock_code,shares,purchase_price\n所有者A,2914,100,4200\n".encode()


@pytest.fixture
def ledger(tmp_path: Path) -> CsvImportLedger:
    return CsvImportLedger(repository=AuditLogRepository(store_dir=tmp_path))


# --- import id ---------------------------------------------------------------


def test_import_id_is_sha256_of_content() -> None:
    import_id = compute_import_id(_CONTENT)
    assert len(import_id) == 64, "SHA-256のfull 64桁hexを使う(切り詰めない)"
    assert import_id == compute_import_id(_CONTENT), "同一内容なら決定的"


def test_import_id_differs_when_content_differs() -> None:
    assert compute_import_id(_CONTENT) != compute_import_id(_CONTENT + b"x")


def test_import_id_does_not_depend_on_filename() -> None:
    """ファイル名はidentityに含まれない(引数がバイト列のみであることの明示)。"""
    assert compute_import_id(_CONTENT) == compute_import_id(bytes(_CONTENT))


def test_row_audit_id_includes_import_id_and_row_number() -> None:
    import_id = compute_import_id(_CONTENT)
    assert build_row_audit_id(import_id, 2) == f"{DECISION_TYPE_HOLDING_IMPORT_ROW}:{import_id}:2"
    assert build_row_audit_id(import_id, 2) != build_row_audit_id(import_id, 3)


# --- ledger ------------------------------------------------------------------


def test_unapplied_row_is_not_applied(ledger: CsvImportLedger) -> None:
    assert ledger.is_applied(compute_import_id(_CONTENT), 2) is False


def test_marked_row_is_applied(ledger: CsvImportLedger) -> None:
    import_id = compute_import_id(_CONTENT)
    ledger.mark_applied(import_id, 2, owner="所有者A", stock_code="2914", shares=100, now=_NOW)
    assert ledger.is_applied(import_id, 2) is True
    # 行番号が違えば別扱い。
    assert ledger.is_applied(import_id, 3) is False
    # 内容が違うCSVは別扱い。
    assert ledger.is_applied(compute_import_id(_CONTENT + b"x"), 2) is False


def test_mark_applied_is_idempotent(ledger: CsvImportLedger, tmp_path: Path) -> None:
    import_id = compute_import_id(_CONTENT)
    for _ in range(2):
        ledger.mark_applied(
            import_id, 2, owner="所有者A", stock_code="2914", shares=100, now=_NOW
        )
    entries = AuditLogRepository(store_dir=tmp_path).list_by_decision_type(
        DECISION_TYPE_HOLDING_IMPORT_ROW
    )
    assert len(entries) == 1


def test_marker_records_traceable_values(ledger: CsvImportLedger, tmp_path: Path) -> None:
    import_id = compute_import_id(_CONTENT)
    ledger.mark_applied(import_id, 2, owner="所有者A", stock_code="2914", shares=100, now=_NOW)
    entry = AuditLogRepository(store_dir=tmp_path).get(build_row_audit_id(import_id, 2))
    assert entry is not None
    assert entry.stock_code == "2914"
    assert entry.input_values["import_id"] == import_id
    assert entry.input_values["row_number"] == 2
    assert entry.input_values["owner"] == "所有者A"
    assert entry.output_values["applied"] is True


def test_lookup_does_not_scan_all_entries(
    ledger: CsvImportLedger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """決定的キーによる単一取得のみを使う(full Scan構造を持ち込まない)。

    audit_logは3万件規模であり、`list_all()`での探索は
    Issue #113で除去したのと同じ全件materializeになるため禁止する。
    """
    repo = AuditLogRepository(store_dir=tmp_path)

    def _forbidden() -> list:
        raise AssertionError("list_all() を使ってはならない")

    monkeypatch.setattr(repo, "list_all", _forbidden)
    scoped = CsvImportLedger(repository=repo)

    import_id = compute_import_id(_CONTENT)
    assert scoped.is_applied(import_id, 2) is False
    scoped.mark_applied(import_id, 2, owner="所有者A", stock_code="2914", shares=100, now=_NOW)
    assert scoped.is_applied(import_id, 2) is True
