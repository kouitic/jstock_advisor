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
    assert plan.write_item_count == MAX_LOTS_PER_HOLDING + 1, "削除は N + 1"
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
    assert MAX_LOTS_PER_HOLDING + 2 <= dynamodb_transaction.MAX_TRANSACT_ITEMS, (
        "置換1回分(ロット削除N + 新ロットPut1 + HoldingPut1 = N + 2)が物理上限へ収まること"
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

    assert len(items) == plan.write_item_count == 4, (
        "ロット削除2 + 新ロットPut1 + HoldingのConditionalPut1 = N + 2"
    )
    assert sum(1 for i in items if "Delete" in i) == 2, "削除はロットのみ"
    assert sum(1 for i in items if "Put" in i) == 2
    assert plan.holding_delete is None, "置換ではHoldingをDeleteしない"
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


# --- T19〜T25 DynamoDB transaction の構造(同一アイテムへの複数アクション禁止) ---
#
# DynamoDBのTransactWriteItemsは、1トランザクション内で同一アイテムを対象とする
# 複数のアクションを許可しない(ValidationException)。overwriteでHoldingを
# 「Delete → Put」にしていると、ローカルJSONのテストが通っていてもProductionの
# DynamoDB経路では必ず失敗する。ここではその構造をテストで固定する。


def _transact_keys(items: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(TableName, primary key value) の一覧を返す。"""
    keys: list[tuple[str, str]] = []
    for item in items:
        if "Delete" in item:
            body = item["Delete"]
            key_value = next(iter(body["Key"].values()))
        else:
            body = item["Put"]
            pk_field = body.get("Key")
            if pk_field is not None:
                key_value = next(iter(pk_field.values()))
            else:
                # Putはitem側にPKを持つ。dataではない属性がPK。
                non_data = {k: v for k, v in body["Item"].items() if k != "data"}
                key_value = next(iter(non_data.values()))
        keys.append((body["TableName"], next(iter(key_value.values()))))
    return keys


def _overwrite_plan(portfolio: PortfolioService, lot_id: str = "csv:test:2") -> Any:
    purchase = portfolio.build_purchase_write_plan(
        _OWNER,
        _CODE,
        "J社",
        10,
        Decimal("5000"),
        dt.date(2026, 3, 1),
        AccountType.SPECIFIC,
        lot_id=lot_id,
    )
    return portfolio.build_holding_replacement_plan(_OWNER, _CODE, purchase=purchase)


def test_overwrite_plan_has_exactly_one_action_for_the_holding(
    portfolio: PortfolioService,
) -> None:
    """T19: overwriteの計画でHoldingへのアクションが1個だけであること。"""
    _seed_two_lots(portfolio)
    plan = _overwrite_plan(portfolio)

    holding_actions = [a for a in (plan.holding_delete, plan.holding_put) if a is not None]
    assert len(holding_actions) == 1


def test_overwrite_existing_holding_uses_conditional_put_with_expected_data(
    portfolio: PortfolioService,
) -> None:
    """T20: 既存Holdingがある置換は、既存の生JSONを条件とするPut 1件で行う。"""
    _seed_two_lots(portfolio)
    existing_raw = portfolio._holdings.get_raw_data(
        f"{_OWNER}#{_CODE}"
    ) or portfolio._holdings.get_raw_data(_CODE)

    plan = _overwrite_plan(portfolio)

    assert plan.holding_delete is None
    assert plan.holding_put is not None
    assert plan.holding_put.expected_data is not None, "楽観ロック条件が付いていない"
    assert plan.holding_put.expected_data == existing_raw


def test_overwrite_new_holding_uses_attribute_not_exists(portfolio: PortfolioService) -> None:
    """T21: 既存Holdingが無い場合はexpected_data=None(attribute_not_exists)。"""
    plan = _overwrite_plan(portfolio)

    assert plan.holding_delete is None
    assert plan.holding_put is not None
    assert plan.holding_put.expected_data is None

    from jstock_advisor.infrastructure.aws import holding_replacement_commit

    items = holding_replacement_commit.build_transact_items(plan)
    holding_items = [i for i in items if "Put" in i and "holdings" in i["Put"]["TableName"]]
    assert len(holding_items) == 1
    assert holding_items[0]["Put"]["ConditionExpression"] == "attribute_not_exists(#pk)"


def test_delete_plan_uses_conditional_delete_only(portfolio: PortfolioService) -> None:
    """T22: 削除の計画はHoldingへのDeleteのみを持つ。"""
    _seed_two_lots(portfolio)

    plan = portfolio.build_holding_replacement_plan(_OWNER, _CODE, purchase=None)

    assert plan.holding_put is None
    assert plan.holding_delete is not None
    assert plan.lot_put is None


@pytest.mark.parametrize("with_purchase", [True, False])
def test_transact_items_never_target_the_same_item_twice(
    portfolio: PortfolioService, with_purchase: bool
) -> None:
    """T23: 同一table×同一primary keyを対象とするアクションが重複しない。

    DynamoDBのTransactWriteItemsが拒否する形になっていないことを、
    実際に構築される項目列から直接確認する。
    """
    from jstock_advisor.infrastructure.aws import holding_replacement_commit

    _seed(
        portfolio,
        [(10, "100", "2026-01-01"), (20, "200", "2026-01-02"), (30, "300", "2026-01-03")],
    )
    plan = (
        _overwrite_plan(portfolio)
        if with_purchase
        else portfolio.build_holding_replacement_plan(_OWNER, _CODE, purchase=None)
    )
    items = holding_replacement_commit.build_transact_items(plan)

    keys = _transact_keys(items)
    assert len(keys) == len(set(keys)), f"同一アイテムへの複数アクションがある: {keys}"


def test_plan_rejects_holding_delete_and_put_together(portfolio: PortfolioService) -> None:
    """T23補: 不変条件がデータ構造の側で強制されていること。"""
    from jstock_advisor.services.write_plan import (
        ConditionalDelete,
        ConditionalPut,
        HoldingReplacementPlan,
    )

    _seed_two_lots(portfolio)
    holding = portfolio.get_holding(_OWNER, _CODE)
    assert holding is not None
    with pytest.raises(ValueError, match="DeleteとPut"):
        HoldingReplacementPlan(
            lot_deletes=[],
            holding_delete=ConditionalDelete(
                id_value=holding.holding_id, id_field="holding_id", expected_data="{}"
            ),
            lot_put=None,
            holding_put=ConditionalPut(
                model=holding, id_field="holding_id", expected_data=None
            ),
            resulting_holding=holding,
        )


def test_overwrite_write_count_is_n_plus_2_at_the_limit(portfolio: PortfolioService) -> None:
    """T24: 90ロットのoverwriteは N + 2 項目。"""
    _seed_n_lots(portfolio, MAX_LOTS_PER_HOLDING)

    plan = _overwrite_plan(portfolio)

    assert plan.write_item_count == MAX_LOTS_PER_HOLDING + 2
    assert plan.write_item_count <= 100, "DynamoDBの物理上限へ収まること"


def test_delete_write_count_is_n_plus_1_at_the_limit(portfolio: PortfolioService) -> None:
    """T25: 90ロットの削除は N + 1 項目。"""
    _seed_n_lots(portfolio, MAX_LOTS_PER_HOLDING)

    plan = portfolio.build_holding_replacement_plan(_OWNER, _CODE, purchase=None)

    assert plan.write_item_count == MAX_LOTS_PER_HOLDING + 1


def test_reusing_an_existing_lot_id_does_not_delete_and_put_the_same_lot(
    portfolio: PortfolioService,
) -> None:
    """新ロットIDが既存ロットと同一の場合も、同一アイテムへの二重アクションにしない。"""
    from jstock_advisor.infrastructure.aws import holding_replacement_commit

    _seed_two_lots(portfolio)
    existing_lot_id = portfolio.list_lots(_OWNER, _CODE)[0].lot_id

    plan = _overwrite_plan(portfolio, lot_id=existing_lot_id)

    assert all(d.id_value != existing_lot_id for d in plan.lot_deletes)
    assert plan.lot_put is not None
    assert plan.lot_put.expected_data is not None, "既存ロットの置換は楽観ロック付きPut"
    keys = _transact_keys(holding_replacement_commit.build_transact_items(plan))
    assert len(keys) == len(set(keys))


# --- T26〜T29 ローカル rollback の完全性 ---------------------------------------
#
# ロールバックの対象は「削除するロット」だけではない。**Putによって上書きされる
# 既存ロットも対象**である。新しいロットIDが既存ロットのIDと同一の場合、
# そのIDは(同一アイテムへのDelete+Putを避けるため)lot_deletes から除外され
# lot_put だけになる。削除対象のスナップショットしか保持していないと、
# 上書きされた旧ロットが復元されず消失する。


def _snapshot(portfolio: PortfolioService) -> tuple[dict[str, Any], Any]:
    """ロット全件と保有を model_dump で比較可能な形にする(IDだけでなく内容も見る)。"""
    lots = {lot.lot_id: lot.model_dump() for lot in portfolio.list_lots(_OWNER, _CODE)}
    holding = portfolio.get_holding(_OWNER, _CODE)
    return lots, (holding.model_dump() if holding is not None else None)


def _break_holdings_apply_batch(portfolio: PortfolioService) -> None:
    def _fail(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("holdingsの書き込みに失敗しました")

    portfolio._holdings.apply_batch = _fail  # type: ignore[method-assign]


def _overwrite_with_lot_id(portfolio: PortfolioService, lot_id: str) -> Any:
    purchase = portfolio.build_purchase_write_plan(
        _OWNER,
        _CODE,
        "J社",
        10,
        Decimal("5000"),
        dt.date(2026, 3, 1),
        AccountType.SPECIFIC,
        lot_id=lot_id,
    )
    return portfolio.build_holding_replacement_plan(_OWNER, _CODE, purchase=purchase)


def test_rollback_removes_the_new_lot_and_restores_the_old_state(
    portfolio: PortfolioService,
) -> None:
    """T26: 新規lot_id + 保有書き込み失敗 → 新ロット消去・旧ロット完全復元。"""
    _seed_two_lots(portfolio)
    before = _snapshot(portfolio)

    plan = _overwrite_with_lot_id(portfolio, "csv:new-import:1")
    _break_holdings_apply_batch(portfolio)
    with pytest.raises(OSError):
        portfolio.apply_holding_replacement_plan(plan)

    after = _snapshot(portfolio)
    assert "csv:new-import:1" not in after[0], "新規ロットが残っている"
    assert after == before, "旧stateが完全に復元されていない"


def test_rollback_restores_the_old_content_of_a_reused_lot_id(
    portfolio: PortfolioService,
) -> None:
    """T27: 既存lot_idの再利用 + 保有書き込み失敗 → 同一IDのロットが旧内容へ復元。

    修正前は、再利用されたIDが lot_deletes から除外される一方で
    ロールバック用スナップショットにも含まれず、上書きされた旧ロットが消失した。
    """
    _seed_two_lots(portfolio)
    before = _snapshot(portfolio)
    reused_lot_id = next(iter(before[0]))
    assert before[0][reused_lot_id]["shares"] == 100

    plan = _overwrite_with_lot_id(portfolio, reused_lot_id)
    assert all(d.id_value != reused_lot_id for d in plan.lot_deletes), (
        "再利用IDは削除対象から外れている前提"
    )
    _break_holdings_apply_batch(portfolio)
    with pytest.raises(OSError):
        portfolio.apply_holding_replacement_plan(plan)

    after = _snapshot(portfolio)
    assert reused_lot_id in after[0], "再利用された既存ロットが消失した"
    assert after[0][reused_lot_id]["shares"] == 100, "旧内容へ戻っていない"
    assert after == before, "旧stateが完全に復元されていない"


def test_reused_lot_id_overwrite_succeeds_with_the_new_content(
    tmp_path: Path, portfolio: PortfolioService
) -> None:
    """T28: 既存lot_idの再利用が正常に成功した場合は新内容になる。"""
    _seed_two_lots(portfolio)
    reused_lot_id = portfolio.list_lots(_OWNER, _CODE)[0].lot_id

    plan = _overwrite_with_lot_id(portfolio, reused_lot_id)
    portfolio.apply_holding_replacement_plan(plan)

    lots = portfolio.list_lots(_OWNER, _CODE)
    assert len(lots) == 1, "置換後は新ロット1件だけになること"
    assert lots[0].lot_id == reused_lot_id
    assert lots[0].shares == 10, "新内容になっていない"
    assert _state(portfolio) == (10, 1, 10)


def test_rollback_with_reused_id_among_many_lots_restores_every_lot(
    portfolio: PortfolioService,
) -> None:
    """T29: 複数ロットのうち1件のIDを再利用 + 保有書き込み失敗 →
    全ロット集合・全内容がミューテーション前と完全一致する。
    """
    _seed(
        portfolio,
        [
            (10, "100", "2026-01-01"),
            (20, "200", "2026-01-02"),
            (30, "300", "2026-01-03"),
            (40, "400", "2026-01-04"),
        ],
    )
    before = _snapshot(portfolio)
    assert len(before[0]) == 4
    reused_lot_id = sorted(before[0])[2]

    plan = _overwrite_with_lot_id(portfolio, reused_lot_id)
    _break_holdings_apply_batch(portfolio)
    with pytest.raises(OSError):
        portfolio.apply_holding_replacement_plan(plan)

    after = _snapshot(portfolio)
    assert set(after[0]) == set(before[0]), "ロット集合が変化した"
    assert after == before, "ロットの内容または保有が旧stateと一致しない"


def test_rollback_snapshot_covers_delete_set_union_put_target(
    portfolio: PortfolioService,
) -> None:
    """ロールバックの対象が「削除対象 ∪ Put対象」であることを直接確認する。

    再利用IDは lot_deletes に含まれないため、削除対象だけを見ていると
    スナップショットから漏れる。
    """
    _seed_two_lots(portfolio)
    reused_lot_id = portfolio.list_lots(_OWNER, _CODE)[0].lot_id
    plan = _overwrite_with_lot_id(portfolio, reused_lot_id)

    delete_ids = {d.id_value for d in plan.lot_deletes}
    assert plan.lot_put is not None
    put_id = str(getattr(plan.lot_put.model, plan.lot_put.id_field))
    assert put_id == reused_lot_id
    assert put_id not in delete_ids, "同一アイテムへのDelete+Putになっている"
    assert len(delete_ids | {put_id}) == 2, "ロールバック対象は削除対象 ∪ Put対象の2件"
