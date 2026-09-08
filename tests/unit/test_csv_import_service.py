import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER
from jstock_advisor.services.csv_import_service import CsvRowStatus, HoldingsCsvImportService
from jstock_advisor.services.portfolio_service import PortfolioService

_OWNER = DEFAULT_OWNER
_OTHER_OWNER = "所有者A"


def _write_csv(tmp_path: Path, content: str, name: str = "holdings.csv") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8-sig")
    return path


def test_valid_rows_are_imported(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    csv_path = _write_csv(
        tmp_path,
        "owner,stock_code,stock_name,shares,purchase_price,purchase_date,account_type\n"
        f"{_OWNER},2914,日本たばこ産業,100,4200,2025-05-10,NISA\n",
    )
    summary = csv_import_service.import_file(csv_path)
    assert summary.total_rows == 1
    assert summary.success_count == 1
    assert summary.error_count == 0
    holding = portfolio_service.get_holding(_OWNER, "2914")
    assert holding is not None
    assert holding.shares == 100


def test_missing_required_column_raises(
    tmp_path: Path, csv_import_service: HoldingsCsvImportService
) -> None:
    csv_path = _write_csv(tmp_path, "stock_code,shares\n2914,100\n")
    with pytest.raises(ValueError) as excinfo:
        csv_import_service.import_file(csv_path)
    assert "purchase_price" in str(excinfo.value)
    # Issue #61 Phase B1: ownerも必須列になった。
    assert "owner" in str(excinfo.value)


def test_invalid_rows_are_reported_without_blocking_others(
    tmp_path: Path, csv_import_service: HoldingsCsvImportService
) -> None:
    csv_path = _write_csv(
        tmp_path,
        "owner,stock_code,shares,purchase_price,account_type\n"
        f"{_OWNER},2914,100,4200,NISA\n"  # valid
        f"{_OWNER},7203,-5,3000,NISA\n"  # invalid shares
        f"{_OWNER},6501,100,abc,NISA\n"  # invalid price
        f"{_OWNER},12345,100,3000,NISA\n",  # invalid stock code length
    )
    summary = csv_import_service.import_file(csv_path)
    assert summary.total_rows == 4
    assert summary.success_count == 1
    assert summary.error_count == 3
    statuses = {r.row_number: r.status for r in summary.results}
    assert statuses[2] == CsvRowStatus.SUCCESS
    assert statuses[3] == CsvRowStatus.ERROR
    assert statuses[4] == CsvRowStatus.ERROR
    assert statuses[5] == CsvRowStatus.ERROR


def test_missing_account_type_defaults_to_general_with_warning(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    csv_path = _write_csv(
        tmp_path, f"owner,stock_code,shares,purchase_price\n{_OWNER},2914,100,4200\n"
    )
    summary = csv_import_service.import_file(csv_path)
    assert summary.results[0].status == CsvRowStatus.WARNING
    holding = portfolio_service.get_holding(_OWNER, "2914")
    assert holding is not None
    assert holding.account_type.value == "GENERAL"


def test_existing_holding_additional_purchase_accumulates(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    portfolio_service.register_purchase(
        owner=_OWNER,
        stock_code="2914",
        stock_name="日本たばこ産業",
        shares=100,
        purchase_price=Decimal("4000"),
        purchase_date=dt.date(2025, 1, 1),
        account_type=AccountType.NISA,
    )
    csv_path = _write_csv(
        tmp_path,
        f"owner,stock_code,shares,purchase_price,account_type\n{_OWNER},2914,100,4400,NISA\n",
    )
    summary = csv_import_service.import_file(csv_path, on_duplicate="additional_purchase")
    assert summary.results[0].status == CsvRowStatus.WARNING
    holding = portfolio_service.get_holding(_OWNER, "2914")
    assert holding is not None
    assert holding.shares == 200


def test_existing_holding_overwrite_replaces_lots(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    portfolio_service.register_purchase(
        owner=_OWNER,
        stock_code="2914",
        stock_name="日本たばこ産業",
        shares=100,
        purchase_price=Decimal("4000"),
        purchase_date=dt.date(2025, 1, 1),
        account_type=AccountType.NISA,
    )
    csv_path = _write_csv(
        tmp_path,
        f"owner,stock_code,shares,purchase_price,account_type\n{_OWNER},2914,50,4400,NISA\n",
    )
    summary = csv_import_service.import_file(csv_path, on_duplicate="overwrite")
    assert summary.results[0].status == CsvRowStatus.WARNING
    holding = portfolio_service.get_holding(_OWNER, "2914")
    assert holding is not None
    assert holding.shares == 50
    assert holding.average_purchase_price == Decimal("4400")


# --- Issue #61 Phase B1: owner必須化 ---------------------------------------


def test_owner_column_missing_is_rejected(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    """Case A: owner列そのものが無いCSVは取り込まない。

    行ごとに同じERRORを大量生成せず、既存のヘッダー検証で1回だけ返す。
    """
    csv_path = _write_csv(tmp_path, "stock_code,shares,purchase_price\n2914,100,4200\n")
    with pytest.raises(ValueError) as excinfo:
        csv_import_service.import_file(csv_path)
    assert "owner" in str(excinfo.value)
    assert portfolio_service.list_holdings() == [], "書き込みが発生している"


def test_owner_blank_is_rejected_without_write(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    """Case B: owner列はあるが空欄の行はERROR。DEFAULT_OWNERへfallbackしない。"""
    csv_path = _write_csv(
        tmp_path, "owner,stock_code,shares,purchase_price\n,2914,100,4200\n"
    )
    summary = csv_import_service.import_file(csv_path)
    assert summary.results[0].status == CsvRowStatus.ERROR
    assert "所有者" in summary.results[0].message
    assert summary.error_count == 1
    assert portfolio_service.list_holdings() == [], "書き込みが発生している"


def test_owner_explicit_is_used(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    """Case C: 明示されたownerがそのまま使われる。"""
    csv_path = _write_csv(
        tmp_path,
        f"owner,stock_code,shares,purchase_price\n{_OTHER_OWNER},2914,100,4200\n",
    )
    summary = csv_import_service.import_file(csv_path)
    assert summary.error_count == 0
    assert [h.owner for h in portfolio_service.list_holdings()] == [_OTHER_OWNER]
    assert portfolio_service.get_holding(_OWNER, "2914") is None


def test_owner_missing_column_rejected_even_with_existing_holding(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    """Case D: 既存保有があっても owner列なしCSVは追加購入しない。"""
    portfolio_service.register_purchase(
        owner=_OWNER,
        stock_code="2914",
        stock_name="日本たばこ産業",
        shares=100,
        purchase_price=Decimal("4000"),
        purchase_date=dt.date(2025, 1, 1),
        account_type=AccountType.NISA,
    )
    csv_path = _write_csv(tmp_path, "stock_code,shares,purchase_price\n2914,100,4400\n")
    with pytest.raises(ValueError):
        csv_import_service.import_file(csv_path, on_duplicate="additional_purchase")
    holding = portfolio_service.get_holding(_OWNER, "2914")
    assert holding is not None
    assert holding.shares == 100, "additional purchaseが実行されている"


# --- Issue #61 Phase B1: CSV内duplicate ------------------------------------


def test_duplicate_row_in_same_csv_is_not_registered(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    """CSV内の完全重複行は**登録しない**(従来は警告のみで登録していた)。"""
    csv_path = _write_csv(
        tmp_path,
        "owner,stock_code,shares,purchase_price,purchase_date,account_type\n"
        f"{_OWNER},2914,100,4200,2025-05-10,NISA\n"
        f"{_OWNER},2914,100,4200,2025-05-10,NISA\n",
    )
    summary = csv_import_service.import_file(csv_path)
    assert summary.results[1].status == CsvRowStatus.ERROR
    assert "重複" in summary.results[1].message

    holding = portfolio_service.get_holding(_OWNER, "2914")
    assert holding is not None
    assert holding.shares == 100, "2件目が登録され二重計上になっている"
    assert len(portfolio_service.list_lots(_OWNER, "2914")) == 1


# --- Issue #61 Phase B1: 再取込の冪等性 ------------------------------------

_IDEMPOTENT_CSV = (
    "owner,stock_code,shares,purchase_price,purchase_date,account_type\n"
    f"{_OWNER},2914,100,4200,2025-05-10,NISA\n"
)


def test_same_csv_reimport_is_idempotent(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    csv_path = _write_csv(tmp_path, _IDEMPOTENT_CSV)
    csv_import_service.import_file(csv_path)
    first = portfolio_service.get_holding(_OWNER, "2914")
    assert first is not None
    assert (first.shares, len(portfolio_service.list_lots(_OWNER, "2914"))) == (100, 1)

    summary = csv_import_service.import_file(csv_path)

    second = portfolio_service.get_holding(_OWNER, "2914")
    assert second is not None
    assert (second.shares, len(portfolio_service.list_lots(_OWNER, "2914"))) == (100, 1)
    # 無音のskipにしない。
    assert summary.results[0].status == CsvRowStatus.SKIPPED_DUPLICATE
    assert summary.skipped_count == 1
    assert "取り込み済み" in summary.results[0].message


def test_reimport_identity_is_content_based_not_filename(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    """別ファイル名でも内容が同一なら同一importとして扱う。"""
    csv_import_service.import_file(_write_csv(tmp_path, _IDEMPOTENT_CSV, name="a.csv"))
    summary = csv_import_service.import_file(
        _write_csv(tmp_path, _IDEMPOTENT_CSV, name="b.csv")
    )
    assert summary.results[0].status == CsvRowStatus.SKIPPED_DUPLICATE
    holding = portfolio_service.get_holding(_OWNER, "2914")
    assert holding is not None
    assert holding.shares == 100


def test_same_filename_different_content_is_a_new_import(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    """同じファイル名でも内容が違えば別importとして扱う。"""
    csv_import_service.import_file(_write_csv(tmp_path, _IDEMPOTENT_CSV))
    summary = csv_import_service.import_file(
        _write_csv(
            tmp_path,
            "owner,stock_code,shares,purchase_price,purchase_date,account_type\n"
            f"{_OWNER},2914,50,4300,2025-06-10,NISA\n",
        )
    )
    assert summary.results[0].status != CsvRowStatus.SKIPPED_DUPLICATE
    holding = portfolio_service.get_holding(_OWNER, "2914")
    assert holding is not None
    assert holding.shares == 150


def test_legitimate_additional_purchase_from_another_csv_still_works(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    """保有が既にあることを重複取込とみなさない(additional_purchaseの意味論維持)。"""
    csv_import_service.import_file(_write_csv(tmp_path, _IDEMPOTENT_CSV, name="first.csv"))
    summary = csv_import_service.import_file(
        _write_csv(
            tmp_path,
            "owner,stock_code,shares,purchase_price,purchase_date,account_type\n"
            f"{_OWNER},2914,200,4500,2025-07-01,NISA\n",
            name="second.csv",
        ),
        on_duplicate="additional_purchase",
    )
    assert summary.error_count == 0
    assert summary.skipped_count == 0
    holding = portfolio_service.get_holding(_OWNER, "2914")
    assert holding is not None
    assert holding.shares == 300
    assert len(portfolio_service.list_lots(_OWNER, "2914")) == 2


def test_partial_failure_retry_does_not_double_count(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase Aで実測した**最も危険な経路**を固定する。

    3行CSVの3行目で失敗 → 同一CSVを再実行したとき、
    成功済みの1・2行目が二重計上されず、3行目だけが1回適用されること。
    """
    csv_path = _write_csv(
        tmp_path,
        "owner,stock_code,shares,purchase_price,purchase_date,account_type\n"
        f"{_OWNER},7203,100,2000,2026-08-01,NISA\n"
        f"{_OWNER},6758,200,1500,2026-08-02,NISA\n"
        f"{_OWNER},9432,300,1000,2026-08-03,NISA\n",
    )

    original = portfolio_service.register_purchase
    calls = {"n": 0}

    def _boom_on_third(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated crash on 3rd row")
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(portfolio_service, "register_purchase", _boom_on_third)
    with pytest.raises(RuntimeError):
        csv_import_service.import_file(csv_path)
    monkeypatch.setattr(portfolio_service, "register_purchase", original)

    assert portfolio_service.get_holding(_OWNER, "9432") is None

    # 「やり直すつもり」で同一CSVを再実行する。
    summary = csv_import_service.import_file(csv_path)

    expected = {"7203": 100, "6758": 200, "9432": 300}
    for code, shares in expected.items():
        holding = portfolio_service.get_holding(_OWNER, code)
        assert holding is not None, code
        assert holding.shares == shares, f"{code} が二重計上されている"
        assert len(portfolio_service.list_lots(_OWNER, code)) == 1, code

    statuses = {r.stock_code: r.status for r in summary.results}
    assert statuses["7203"] == CsvRowStatus.SKIPPED_DUPLICATE
    assert statuses["6758"] == CsvRowStatus.SKIPPED_DUPLICATE
    assert statuses["9432"] != CsvRowStatus.SKIPPED_DUPLICATE


def test_overwrite_reimport_is_idempotent(
    tmp_path: Path,
    csv_import_service: HoldingsCsvImportService,
    portfolio_service: PortfolioService,
) -> None:
    """overwriteの正常経路を壊していないこと(原子性の改修はPhase B2)。"""
    csv_path = _write_csv(tmp_path, _IDEMPOTENT_CSV)
    csv_import_service.import_file(csv_path, on_duplicate="overwrite")
    summary = csv_import_service.import_file(csv_path, on_duplicate="overwrite")
    assert summary.results[0].status == CsvRowStatus.SKIPPED_DUPLICATE
    holding = portfolio_service.get_holding(_OWNER, "2914")
    assert holding is not None
    assert holding.shares == 100
    assert len(portfolio_service.list_lots(_OWNER, "2914")) == 1
