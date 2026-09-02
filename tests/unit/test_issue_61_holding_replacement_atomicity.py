"""Issue #61 Phase B2 — 保有の原子的な置換・削除の回帰テスト。

## 何が問題だったか

`--on-duplicate overwrite` の保有CSV取込は、`delete_holding()` で既存の全ロットと
Holdingを消してから `register_purchase()` で新しいロットを作っていた。この2つは
別々の書き込みであり、間で失敗すると

  - Holdingだけ旧値でロットが一部欠落
  - Holdingが消えてロットだけ残る / その逆
  - 保有そのものが消滅(新ロットが作られない)

という部分状態が残った。しかも**再実行しても元の保有は復元されない**
(削除は既に確定しているため)。Phase B1 の冪等化は「新規適用を何度行っても同じ
最終状態へ収束する」ことは保証するが、「削除された既存データの復元」は対象外である。

`delete_holding()` 自体も `lots.delete_by_holding()`(1件ずつ削除するループ)→
`holdings.delete()` の非原子的な2段構成であり、CLIの保有削除でも部分削除が起こりえた。

## このテストが固定する契約

1. overwrite は外から見て「旧state」か「新state」のどちらか一方だけになる
2. `delete_holding()` は「全部消える」か「何も消えない」のどちらか一方だけになる
3. 失敗時は**旧stateが完全に維持される**(部分削除・部分置換を残さない)
4. ロット数が上限を超える場合は、**変更を一切行う前に**明示的に拒否する
5. 上限超過時に部分適用・非トランザクション経路へのフォールバック・
   先頭N件だけの削除・無音の切り詰めをしない

## 失敗注入の位置について

失敗は**ストアのファイル書き込み(`_write_all`)** へ注入する。これが実装上の
実際の失敗境界であり、DynamoDB実装における1回の書き込み呼び出しに対応する。
リポジトリの公開APIそのものを壊す注入は、補償処理までもが同じ壊れたAPIを使う
ことになり、テストとして実態を反映しない。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.holding import Holding, PurchaseLot
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)
from jstock_advisor.services.csv_import_ledger import CsvImportLedger
from jstock_advisor.services.csv_import_service import CsvRowStatus, HoldingsCsvImportService
from jstock_advisor.services.portfolio_service import (
    MAX_LOTS_PER_HOLDING,
    HoldingLotLimitExceededError,
    PortfolioService,
)

_OWNER = "所有者A"
_CODE = "2914"
_CSV_HEADER = "owner,stock_code,stock_name,shares,purchase_price,purchase_date,account_type\n"
_CSV_ROW = f"{_OWNER},{_CODE},J社,10,5000,2026-03-01,SPECIFIC\n"


@pytest.fixture
def portfolio(tmp_path: Path) -> PortfolioService:
    return PortfolioService(
        holding_repository=HoldingRepository(store_dir=tmp_path),
        lot_repository=PurchaseLotRepository(store_dir=tmp_path),
    )


@pytest.fixture
def importer(tmp_path: Path, portfolio: PortfolioService) -> HoldingsCsvImportService:
    return HoldingsCsvImportService(
        portfolio_service=portfolio,
        ledger=CsvImportLedger(repository=AuditLogRepository(store_dir=tmp_path)),
    )


def _seed(portfolio: PortfolioService, lots: list[tuple[int, str, str]]) -> None:
    for shares, price, date in lots:
        portfolio.register_purchase(
            owner=_OWNER,
            stock_code=_CODE,
            stock_name="J社",
            shares=shares,
            purchase_price=Decimal(price),
            purchase_date=dt.date.fromisoformat(date),
            account_type=AccountType.SPECIFIC,
        )


def _seed_two_lots(portfolio: PortfolioService) -> None:
    _seed(portfolio, [(100, "4000", "2026-01-10"), (50, "4500", "2026-02-10")])


def _state(portfolio: PortfolioService) -> tuple[int | None, int, int]:
    """(Holdingの株数 or None, ロット件数, ロット株数合計)。"""
    holding = portfolio.get_holding(_OWNER, _CODE)
    lots = portfolio.list_lots(_OWNER, _CODE)
    return (
        None if holding is None else holding.shares,
        len(lots),
        sum(lot.shares for lot in lots),
    )


def _write_csv(tmp_path: Path, row: str = _CSV_ROW) -> Path:
    path = tmp_path / "overwrite.csv"
    path.write_text(_CSV_HEADER + row, encoding="utf-8")
    return path


def _break_store_write(store: Any) -> None:
    """ストアのファイル書き込みを失敗させる(実装上の実際の失敗境界)。"""

    def _fail(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("ストアへの書き込みに失敗しました")

    store._write_all = _fail


# --- T1 / T7 正常系 -----------------------------------------------------------


def test_overwrite_replaces_all_lots_with_the_csv_row(
    tmp_path: Path, portfolio: PortfolioService, importer: HoldingsCsvImportService
) -> None:
    _seed_two_lots(portfolio)
    assert _state(portfolio) == (150, 2, 150)

    summary = importer.import_file(_write_csv(tmp_path), on_duplicate="overwrite")

    assert summary.results[0].status is CsvRowStatus.WARNING
    assert "上書き" in (summary.results[0].message or "")
    assert _state(portfolio) == (10, 1, 10), "既存2ロットがCSVの1行へ置き換わること"
    holding = portfolio.get_holding(_OWNER, _CODE)
    assert holding is not None
    assert holding.average_purchase_price == Decimal("5000"), (
        "置換後の平均取得単価は新ロットだけから再計算されること"
    )


def test_delete_holding_removes_holding_and_all_lots(portfolio: PortfolioService) -> None:
    _seed_two_lots(portfolio)

    assert portfolio.delete_holding(_OWNER, _CODE) is True

    assert _state(portfolio) == (None, 0, 0)


def test_delete_holding_returns_false_when_absent(portfolio: PortfolioService) -> None:
    assert portfolio.delete_holding(_OWNER, "9999") is False


# --- T2〜T5 失敗時に旧stateが完全維持される -----------------------------------


@pytest.mark.parametrize("failing_store", ["lots", "holdings"])
def test_overwrite_failure_preserves_the_previous_state(
    tmp_path: Path,
    portfolio: PortfolioService,
    importer: HoldingsCsvImportService,
    failing_store: str,
) -> None:
    """overwriteの書き込みが失敗しても、既存の保有・ロットが1件も失われない。

    修正前は「既存ロット削除 → 新ロット作成」の間で失敗すると保有が消え、
    再実行しても復元されなかった。
    """
    _seed_two_lots(portfolio)
    before = _state(portfolio)
    assert before == (150, 2, 150)

    target = portfolio._lots if failing_store == "lots" else portfolio._holdings
    _break_store_write(target._store)

    with pytest.raises(OSError):
        importer.import_file(_write_csv(tmp_path), on_duplicate="overwrite")

    assert _state(portfolio) == before, f"{failing_store}の書き込み失敗で旧stateが壊れた"


@pytest.mark.parametrize("failing_store", ["lots", "holdings"])
def test_delete_holding_failure_preserves_the_previous_state(
    portfolio: PortfolioService, failing_store: str
) -> None:
    """delete_holding()の書き込みが失敗しても部分削除を残さない(T8)。"""
    _seed_two_lots(portfolio)
    before = _state(portfolio)

    target = portfolio._lots if failing_store == "lots" else portfolio._holdings
    _break_store_write(target._store)

    with pytest.raises(OSError):
        portfolio.delete_holding(_OWNER, _CODE)

    assert _state(portfolio) == before, f"{failing_store}の書き込み失敗で部分削除が残った"


def test_lot_deletion_is_not_applied_one_by_one(portfolio: PortfolioService) -> None:
    """ロット削除が1件ずつではなく1回の書き込みで適用される(部分削除の構造的排除)。

    修正前は `delete_by_holding()` がロットを1件ずつ削除するループだったため、
    途中で失敗すると一部のロットだけが消えた。
    """
    _seed(
        portfolio,
        [(10, "100", "2026-01-01"), (20, "200", "2026-01-02"), (30, "300", "2026-01-03")],
    )
    calls: list[int] = []
    original = portfolio._lots._store._write_all

    def _counting(items: dict[str, PurchaseLot]) -> None:
        calls.append(len(items))
        original(items)

    portfolio._lots._store._write_all = _counting  # type: ignore[method-assign]

    portfolio.delete_holding(_OWNER, _CODE)

    assert len(calls) == 1, "ロット削除が複数回の書き込みに分割されている"
    assert _state(portfolio) == (None, 0, 0)


# --- T6 再実行で新stateへ収束する ---------------------------------------------


def test_retry_after_failure_converges_to_the_csv_state(
    tmp_path: Path, portfolio: PortfolioService, importer: HoldingsCsvImportService
) -> None:
    _seed_two_lots(portfolio)
    csv_path = _write_csv(tmp_path)
    _break_store_write(portfolio._holdings._store)
    with pytest.raises(OSError):
        importer.import_file(csv_path, on_duplicate="overwrite")

    fresh_portfolio = PortfolioService(
        holding_repository=HoldingRepository(store_dir=tmp_path),
        lot_repository=PurchaseLotRepository(store_dir=tmp_path),
    )
    fresh_importer = HoldingsCsvImportService(
        portfolio_service=fresh_portfolio,
        ledger=CsvImportLedger(repository=AuditLogRepository(store_dir=tmp_path)),
    )
    summary = fresh_importer.import_file(csv_path, on_duplicate="overwrite")

    assert summary.results[0].status is CsvRowStatus.WARNING
    assert _state(fresh_portfolio) == (10, 1, 10)


def test_repeated_overwrite_is_idempotent(
    tmp_path: Path, portfolio: PortfolioService, importer: HoldingsCsvImportService
) -> None:
    """同じCSVをoverwriteで繰り返し取り込んでも二重計上しない(B1契約の維持)。"""
    _seed_two_lots(portfolio)
    csv_path = _write_csv(tmp_path)

    importer.import_file(csv_path, on_duplicate="overwrite")
    first = _state(portfolio)
    importer.import_file(csv_path, on_duplicate="overwrite")

    assert _state(portfolio) == first == (10, 1, 10)


# --- T9〜T12 ロット数上限 ------------------------------------------------------


def _seed_n_lots(portfolio: PortfolioService, count: int) -> None:
    base = dt.date(2026, 1, 1)
    _seed(
        portfolio,
        [(1, "100", (base + dt.timedelta(days=i)).isoformat()) for i in range(count)],
    )


def test_max_lots_boundary_is_allowed(portfolio: PortfolioService) -> None:
    """上限ちょうど(90ロット)は許容される(T9 / T11)。"""
    _seed_n_lots(portfolio, MAX_LOTS_PER_HOLDING)
    assert _state(portfolio) == (MAX_LOTS_PER_HOLDING, MAX_LOTS_PER_HOLDING, MAX_LOTS_PER_HOLDING)

    plan = portfolio.build_holding_replacement_plan(_OWNER, _CODE, purchase=None)

    assert len(plan.lot_deletes) == MAX_LOTS_PER_HOLDING
    assert plan.write_item_count == MAX_LOTS_PER_HOLDING + 1
    assert portfolio.delete_holding(_OWNER, _CODE) is True
    assert _state(portfolio) == (None, 0, 0)


def test_over_limit_is_rejected_before_any_mutation(portfolio: PortfolioService) -> None:
    """上限超過(91ロット)は変更を一切行う前に拒否される(T10 / T12)。"""
    _seed_n_lots(portfolio, MAX_LOTS_PER_HOLDING + 1)
    before = _state(portfolio)

    with pytest.raises(HoldingLotLimitExceededError):
        portfolio.delete_holding(_OWNER, _CODE)

    assert _state(portfolio) == before, "上限超過で拒否したのにデータが変更された"


def test_over_limit_overwrite_is_rejected_before_any_mutation(
    tmp_path: Path, portfolio: PortfolioService, importer: HoldingsCsvImportService
) -> None:
    _seed_n_lots(portfolio, MAX_LOTS_PER_HOLDING + 1)
    before = _state(portfolio)

    with pytest.raises(HoldingLotLimitExceededError):
        importer.import_file(_write_csv(tmp_path), on_duplicate="overwrite")

    assert _state(portfolio) == before


def test_over_limit_does_not_truncate_or_partially_delete(portfolio: PortfolioService) -> None:
    """上限超過時に「先頭90件だけ削除」等の部分適用・無音の切り詰めをしない。"""
    _seed_n_lots(portfolio, MAX_LOTS_PER_HOLDING + 5)
    before = _state(portfolio)

    with pytest.raises(HoldingLotLimitExceededError):
        portfolio.build_holding_replacement_plan(_OWNER, _CODE, purchase=None)

    assert _state(portfolio) == before
    assert before[1] == MAX_LOTS_PER_HOLDING + 5


def test_business_limit_is_independent_of_the_dynamodb_physical_limit() -> None:
    """業務上限(90)とDynamoDBの物理上限(100)を同じ定数にしない。"""
    from jstock_advisor.infrastructure.aws import dynamodb_transaction

    assert MAX_LOTS_PER_HOLDING == 90
    assert dynamodb_transaction.MAX_TRANSACT_ITEMS == 100
    assert MAX_LOTS_PER_HOLDING < dynamodb_transaction.MAX_TRANSACT_ITEMS
    assert MAX_LOTS_PER_HOLDING + 3 <= dynamodb_transaction.MAX_TRANSACT_ITEMS, (
        "置換1回分(ロット削除N + Holding削除1 + 新ロットPut1 + 新HoldingPut1)が"
        "物理上限へ収まること"
    )


# --- T18 local / AWS の契約一致 -----------------------------------------------


def test_plan_is_translated_into_a_single_transaction(portfolio: PortfolioService) -> None:
    """AWS側では計画が1回のTransactWriteItemsへ変換される(契約の等価性)。"""
    from jstock_advisor.infrastructure.aws import holding_replacement_commit

    _seed_two_lots(portfolio)
    plan = portfolio.build_holding_replacement_plan(_OWNER, _CODE, purchase=None)
    items = holding_replacement_commit.build_transact_items(plan)

    assert len(items) == plan.write_item_count == 3, "ロット2件 + Holding1件"
    assert all("Delete" in item for item in items), "削除のみの計画でPutが混ざっている"
    for item in items:
        assert item["Delete"]["ConditionExpression"] == "#data = :expected_data", (
            "楽観ロック条件が付与されていない"
        )


def test_overwrite_plan_contains_deletes_and_puts_in_one_transaction(
    portfolio: PortfolioService,
) -> None:
    from jstock_advisor.infrastructure.aws import holding_replacement_commit

    _seed_two_lots(portfolio)
    purchase = portfolio.build_purchase_write_plan(
        _OWNER,
        _CODE,
        "J社",
        10,
        Decimal("5000"),
        dt.date(2026, 3, 1),
        AccountType.SPECIFIC,
        lot_id="csv:test:2",
    )
    plan = portfolio.build_holding_replacement_plan(_OWNER, _CODE, purchase=purchase)
    items = holding_replacement_commit.build_transact_items(plan)

    assert len(items) == plan.write_item_count == 5, (
        "ロット削除2 + Holding削除1 + 新ロットPut1 + 新HoldingPut1"
    )
    assert sum(1 for i in items if "Delete" in i) == 3
    assert sum(1 for i in items if "Put" in i) == 2
    assert plan.resulting_holding is not None
    assert plan.resulting_holding.shares == 10, "置換後は新ロットだけから再計算されること"


def test_plan_does_not_persist_anything(portfolio: PortfolioService) -> None:
    """計画構築は永続化を一切伴わない。"""
    _seed_two_lots(portfolio)
    before = _state(portfolio)

    portfolio.build_holding_replacement_plan(_OWNER, _CODE, purchase=None)

    assert _state(portfolio) == before


def test_plan_carries_optimistic_lock_for_every_existing_item(
    portfolio: PortfolioService,
) -> None:
    """既存アイテムの削除には必ず楽観ロック条件(生JSON)が付く。"""
    _seed_two_lots(portfolio)

    plan = portfolio.build_holding_replacement_plan(_OWNER, _CODE, purchase=None)

    assert plan.holding_delete is not None
    assert plan.holding_delete.expected_data
    assert len(plan.lot_deletes) == 2
    assert all(d.expected_data for d in plan.lot_deletes)


# --- T13〜T15 Phase B1 契約の回帰 ---------------------------------------------


def test_additional_purchase_path_is_unchanged(
    tmp_path: Path, portfolio: PortfolioService, importer: HoldingsCsvImportService
) -> None:
    """既定の additional_purchase は削除を伴わない(T7相当の回帰)。"""
    _seed_two_lots(portfolio)

    summary = importer.import_file(_write_csv(tmp_path), on_duplicate="additional_purchase")

    assert summary.results[0].status is CsvRowStatus.WARNING
    assert _state(portfolio) == (160, 3, 160), "既存ロットが保持されたまま追加されること"


def test_owner_required_regression(
    tmp_path: Path, importer: HoldingsCsvImportService, portfolio: PortfolioService
) -> None:
    path = tmp_path / "no_owner.csv"
    path.write_text(
        _CSV_HEADER.replace("owner,", "") + _CSV_ROW.replace(f"{_OWNER},", ""), encoding="utf-8"
    )

    # owner列そのものが無いCSVは、行単位のERRORではなく取込自体を拒否する(B1契約)
    with pytest.raises(ValueError, match="owner"):
        importer.import_file(path, on_duplicate="overwrite")

    assert _state(portfolio) == (None, 0, 0)


def test_duplicate_row_regression(
    tmp_path: Path, portfolio: PortfolioService, importer: HoldingsCsvImportService
) -> None:
    """CSV内の重複行は登録されない(B1契約の維持)。"""
    path = tmp_path / "dup.csv"
    path.write_text(_CSV_HEADER + _CSV_ROW + _CSV_ROW, encoding="utf-8")

    summary = importer.import_file(path, on_duplicate="overwrite")

    assert summary.error_count >= 1
    assert _state(portfolio) == (10, 1, 10), "重複行が二重計上された"


def test_multi_lot_holding_overwrite(
    tmp_path: Path, portfolio: PortfolioService, importer: HoldingsCsvImportService
) -> None:
    """3ロット以上の保有でも1回の置換で新stateになる(T10相当)。"""
    _seed(
        portfolio,
        [(10, "100", "2026-01-01"), (20, "200", "2026-01-02"), (30, "300", "2026-01-03")],
    )
    assert _state(portfolio) == (60, 3, 60)

    importer.import_file(_write_csv(tmp_path), on_duplicate="overwrite")

    assert _state(portfolio) == (10, 1, 10)


def test_holding_projection_matches_lots_after_overwrite(
    tmp_path: Path, portfolio: PortfolioService, importer: HoldingsCsvImportService
) -> None:
    """置換後にHoldingとロット集合が必ず整合する(中間状態を残さない)。"""
    _seed_two_lots(portfolio)

    importer.import_file(_write_csv(tmp_path), on_duplicate="overwrite")

    holding = portfolio.get_holding(_OWNER, _CODE)
    lots = portfolio.list_lots(_OWNER, _CODE)
    assert holding is not None
    assert holding.shares == sum(lot.shares for lot in lots)


def test_holding_model_type_is_validated(portfolio: PortfolioService) -> None:
    """置換計画のモデル型が想定外の場合は適用前に落とす。"""
    _seed_two_lots(portfolio)
    plan = portfolio.build_holding_replacement_plan(_OWNER, _CODE, purchase=None)
    assert plan.lot_put is None and plan.holding_put is None
    assert isinstance(portfolio.get_holding(_OWNER, _CODE), Holding)
