"""services/csv_import_ledger.py のテスト(Issue #61 Phase B1)。

CSV取込の行コミット境界の identity 設計と排他claimを固定する。
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
    build_row_lot_id,
    compute_import_id,
)

_NOW = dt.datetime(2026, 9, 2, 0, 0, tzinfo=dt.UTC)
_CONTENT = "owner,stock_code,shares,purchase_price\n所有者A,2914,100,4200\n".encode()


@pytest.fixture
def ledger(tmp_path: Path) -> CsvImportLedger:
    return CsvImportLedger(repository=AuditLogRepository(store_dir=tmp_path))


def _claim(ledger: CsvImportLedger, import_id: str, row_number: int) -> bool:
    return ledger.claim(
        import_id, row_number, owner="所有者A", stock_code="2914", shares=100, now=_NOW
    )


# --- import id / row identity ------------------------------------------------


def test_import_id_is_sha256_of_content() -> None:
    import_id = compute_import_id(_CONTENT)
    assert len(import_id) == 64, "SHA-256のfull 64桁hexを使う(切り詰めない)"
    assert import_id == compute_import_id(_CONTENT), "同一内容なら決定的"


def test_import_id_differs_when_content_differs() -> None:
    assert compute_import_id(_CONTENT) != compute_import_id(_CONTENT + b"x")


def test_import_id_does_not_depend_on_filename() -> None:
    """ファイル名はidentityに含まれない(引数がバイト列のみであることの明示)。"""
    assert compute_import_id(_CONTENT) == compute_import_id(bytes(_CONTENT))


def test_row_lot_id_is_deterministic_and_row_scoped() -> None:
    """**「適用済みか」の唯一の判定材料**が決定的であること。"""
    import_id = compute_import_id(_CONTENT)
    assert build_row_lot_id(import_id, 2) == build_row_lot_id(import_id, 2)
    assert build_row_lot_id(import_id, 2) != build_row_lot_id(import_id, 3)
    assert build_row_lot_id(import_id, 2) != build_row_lot_id(
        compute_import_id(_CONTENT + b"x"), 2
    )
    assert build_row_lot_id(import_id, 2).startswith("csv:"), "uuid4由来と見分けがつくこと"


def test_row_audit_id_includes_import_id_and_row_number() -> None:
    import_id = compute_import_id(_CONTENT)
    assert build_row_audit_id(import_id, 2) == f"{DECISION_TYPE_HOLDING_IMPORT_ROW}:{import_id}:2"
    assert build_row_audit_id(import_id, 2) != build_row_audit_id(import_id, 3)


# --- claim / release ---------------------------------------------------------


def test_claim_succeeds_once(ledger: CsvImportLedger) -> None:
    import_id = compute_import_id(_CONTENT)
    assert _claim(ledger, import_id, 2) is True
    # 行番号が違えば別のclaim。
    assert _claim(ledger, import_id, 3) is True
    # 内容が違うCSVも別のclaim。
    assert _claim(ledger, compute_import_id(_CONTENT + b"x"), 2) is True


def test_concurrent_claim_of_same_row_succeeds_only_once(ledger: CsvImportLedger) -> None:
    """同一row_idを2回commitしようとしても一度しか成立しない。

    事前の存在チェックではTOCTOU raceが残るため、`insert_if_absent`
    (DynamoDB実装では条件付き書き込み)による原子的な獲得で塞ぐ。
    """
    import_id = compute_import_id(_CONTENT)
    first = _claim(ledger, import_id, 2)
    second = _claim(ledger, import_id, 2)
    assert (first, second) == (True, False)


def test_release_allows_reclaim(ledger: CsvImportLedger) -> None:
    """適用に失敗したclaimを解放すると、再実行で必ず適用できる。"""
    import_id = compute_import_id(_CONTENT)
    assert _claim(ledger, import_id, 2) is True
    ledger.release(import_id, 2)
    assert _claim(ledger, import_id, 2) is True


def test_release_of_unclaimed_row_is_harmless(ledger: CsvImportLedger) -> None:
    ledger.release(compute_import_id(_CONTENT), 2)  # 例外にならない


def test_claim_records_traceable_values(ledger: CsvImportLedger, tmp_path: Path) -> None:
    import_id = compute_import_id(_CONTENT)
    _claim(ledger, import_id, 2)
    entry = AuditLogRepository(store_dir=tmp_path).get(build_row_audit_id(import_id, 2))
    assert entry is not None
    assert entry.stock_code == "2914"
    assert entry.input_values["import_id"] == import_id
    assert entry.input_values["row_number"] == 2
    assert entry.input_values["owner"] == "所有者A"
    assert entry.input_values["lot_id"] == build_row_lot_id(import_id, 2)


def test_lookup_does_not_scan_all_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    assert _claim(scoped, import_id, 2) is True
    assert _claim(scoped, import_id, 2) is False
    scoped.release(import_id, 2)
