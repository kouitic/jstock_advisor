"""Issue #61 F-A4: 取引履歴CSVでも所有者(owner)を必須にする。

## 背景

取引履歴CSVは、`owner` 列が無い場合・空欄の場合に、**警告なしで**
`DEFAULT_OWNER`(本人)へ縮退させていた。

```
owner_raw = (row.get("owner") or "").strip() or DEFAULT_OWNER
```

「列が無い」「空欄」「空白のみ」「明示指定」が区別されず、
利用者は**別人の取引として登録されたことに気づけなかった**。

保有銘柄CSVは Phase B1 で既に owner 必須へ移行済みであり、同じ「所有者」という
概念でCSVごとに規則が違う状態になっていた。本 Phase でこれを揃える。

## 確定した業務ルール(ユーザー決定 2026-09-04)

```
OWNER_POLICY = OWNER_REQUIRED_FAIL_CLOSED

owner 列が無い        -> ファイル単位のエラー。取込を開始しない(1件も登録しない)
owner が空欄・空白    -> その行のみエラー。登録しない
DEFAULT_OWNER 補完    -> 廃止(取引履歴CSV経路)
明示された owner      -> 従来どおり
```

## 本モジュールが変更しないもの

```
保有銘柄CSVの挙動        既に同じ規則。変更しない
CLI 等の既定 owner 仕様  変更しない(CSV 経路のみ)
既存の登録済み Transaction  変更しない。migration も行わない
既存のエラー優先順位      owner 検査は従来どおり行の先頭
```
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.local_repository.transaction_repository import (
    SkippedRecommendationRepository,
    TransactionRepository,
)
from jstock_advisor.services.transaction_csv_import_service import (
    CsvRowStatus,
    TransactionCsvImportService,
)
from jstock_advisor.services.transaction_history_service import TransactionHistoryService

_NOW = dt.datetime(2026, 7, 24, 8, 0, tzinfo=dt.UTC)

_HEADER = "owner,stock_code,transaction_type,execution_date,shares,execution_price"
_HEADER_WITHOUT_OWNER = "stock_code,transaction_type,execution_date,shares,execution_price"

_OWNER_A = "所有者A"


@pytest.fixture
def transaction_repo(tmp_path: Path) -> TransactionRepository:
    return TransactionRepository(store_dir=tmp_path)


@pytest.fixture
def import_service(
    tmp_path: Path, transaction_repo: TransactionRepository
) -> TransactionCsvImportService:
    recommendation_repo = RecommendationRepository(store_dir=tmp_path)
    recommendation_repo.save(
        Recommendation(
            recommendation_id="rec-buy",
            stock_code="2914",
            stock_name="日本たばこ産業",
            recommended_at=_NOW,
            recommendation_type=RecommendationType.BUY,
            buy_prices=BuyPriceLevels(
                standard=PriceWithRationale(price=Decimal("3359"), rationale="x"),
            ),
            price_at_recommendation=Decimal("4200"),
            confidence=ConfidenceLevel.HIGH,
            rule_version="v1-mvp",
        )
    )
    history = TransactionHistoryService(
        transaction_repository=transaction_repo,
        skipped_repository=SkippedRecommendationRepository(store_dir=tmp_path),
        recommendation_repository=recommendation_repo,
    )
    return TransactionCsvImportService(transaction_history_service=history)


def _write_csv(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "import.csv"
    path.write_text(content, encoding="utf-8")
    return path


def _saved_owners(repo: TransactionRepository) -> list[str]:
    return [t.owner for t in repo.list_all()]


# --- T1: owner 列そのものが無い -> ファイル単位のエラー ---------------------------


def test_t1_missing_owner_column_aborts_import(
    import_service: TransactionCsvImportService,
    transaction_repo: TransactionRepository,
    tmp_path: Path,
) -> None:
    """owner 列が無いCSVは取込を開始しない。**1件も登録されない。**

    行単位のエラーではなく、必須列不足としてファイル全体を中止する。
    """
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER_WITHOUT_OWNER}\n2914,BUY,2026-07-20,100,3400\n",
    )

    with pytest.raises(ValueError, match="必須列"):
        import_service.import_file(csv_path)

    assert _saved_owners(transaction_repo) == []


def test_t1_missing_owner_column_message_names_owner(
    import_service: TransactionCsvImportService, tmp_path: Path
) -> None:
    """どの列が足りないかが利用者に分かること。"""
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER_WITHOUT_OWNER}\n2914,BUY,2026-07-20,100,3400\n",
    )

    with pytest.raises(ValueError) as exc:
        import_service.import_file(csv_path)

    assert "owner" in str(exc.value)


# --- T2 / T3: owner が空欄・空白のみ -> 行エラー ----------------------------------


@pytest.mark.parametrize(
    ("owner_cell", "label"),
    [("", "empty"), ("   ", "spaces"), ("\t", "tab"), ("  \t ", "mixed_whitespace")],
    ids=["empty", "spaces", "tab", "mixed_whitespace"],
)
def test_t2_t3_blank_owner_row_is_error_and_not_saved(
    import_service: TransactionCsvImportService,
    transaction_repo: TransactionRepository,
    tmp_path: Path,
    owner_cell: str,
    label: str,
) -> None:
    """owner が空欄・空白のみの行は登録しない(その行だけエラー)。"""
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER}\n{owner_cell},2914,BUY,2026-07-20,100,3400\n",
    )

    summary = import_service.import_file(csv_path)

    assert len(summary.results) == 1
    result = summary.results[0]
    assert result.status is CsvRowStatus.ERROR
    assert "所有者" in (result.message or "")
    assert _saved_owners(transaction_repo) == []


def test_t2_blank_owner_error_reports_stock_code(
    import_service: TransactionCsvImportService, tmp_path: Path
) -> None:
    """どの行かを特定できるよう、空欄エラーでも銘柄コードを返す。

    保有銘柄CSVと同じ扱い(所有者の値自体が不正な場合は None のまま)。
    """
    csv_path = _write_csv(tmp_path, f"{_HEADER}\n,2914,BUY,2026-07-20,100,3400\n")

    summary = import_service.import_file(csv_path)

    assert summary.results[0].stock_code == "2914"


# --- T4: 明示された owner は保持される -------------------------------------------


def test_t4_explicit_owner_is_preserved(
    import_service: TransactionCsvImportService,
    transaction_repo: TransactionRepository,
    tmp_path: Path,
) -> None:
    """明示された所有者がそのまま登録される(既定値へ置き換えない)。"""
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER}\n{_OWNER_A},2914,BUY,2026-07-20,100,3400\n",
    )

    summary = import_service.import_file(csv_path)

    assert summary.results[0].status is CsvRowStatus.SUCCESS
    assert _saved_owners(transaction_repo) == [_OWNER_A]


# --- T5: partial import(正常行は残る)-------------------------------------------


def test_t5_only_blank_owner_row_is_dropped(
    import_service: TransactionCsvImportService,
    transaction_repo: TransactionRepository,
    tmp_path: Path,
) -> None:
    """1行だけ owner 空欄でも、他の正常行は取り込まれる。

    owner **列** の欠落はファイル全体を止めるが、owner **値** の欠落は行単位である。
    """
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER}\n"
        f"{_OWNER_A},2914,BUY,2026-07-20,100,3400\n"
        f",8136,BUY,2026-07-21,200,3000\n",
    )

    summary = import_service.import_file(csv_path)

    statuses = [r.status for r in summary.results]
    assert statuses == [CsvRowStatus.SUCCESS, CsvRowStatus.ERROR]
    assert _saved_owners(transaction_repo) == [_OWNER_A]


# --- T6: DEFAULT_OWNER へ fallback しない(本 Phase の中心的契約)------------------


def test_t6_blank_owner_never_falls_back_to_default_owner(
    import_service: TransactionCsvImportService,
    transaction_repo: TransactionRepository,
    tmp_path: Path,
) -> None:
    """空欄 owner が既定の所有者として登録されないこと。

    修正前はここが `DEFAULT_OWNER` として無警告で登録されていた。
    `or DEFAULT_OWNER` を復活させると本テストが失敗する。
    """
    csv_path = _write_csv(tmp_path, f"{_HEADER}\n,2914,BUY,2026-07-20,100,3400\n")

    import_service.import_file(csv_path)

    saved = _saved_owners(transaction_repo)
    assert saved == []
    assert DEFAULT_OWNER not in saved


def test_t6_source_has_no_default_owner_fallback() -> None:
    """取込サービスが `DEFAULT_OWNER` を参照していないこと(再混入の防止)。

    実装をコメントごと戻した場合も検知できるよう、AST の Name 参照で判定する。
    """
    import ast

    path = (
        Path(__file__).resolve().parents[2]
        / "src/jstock_advisor/services/transaction_csv_import_service.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    referenced = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    assert "DEFAULT_OWNER" not in referenced, (
        "取引履歴CSVの取込で DEFAULT_OWNER を参照しています。"
        "owner 未指定は既定の所有者へ補完せずエラーにしてください(Issue #61 F-A4)。"
    )


# --- T9: エラー優先順位を変えていない --------------------------------------------


def test_t9_owner_error_takes_precedence_over_other_invalid_fields(
    import_service: TransactionCsvImportService,
    transaction_repo: TransactionRepository,
    tmp_path: Path,
) -> None:
    """owner 未指定と他項目の不正が同時にある行では、owner のエラーを返す。

    owner の検査は従来から行の先頭にあり、本 Phase で位置を変えていない。
    """
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER}\n,9999999,INVALID,2026-13-40,-1,-1\n",
    )

    summary = import_service.import_file(csv_path)

    result = summary.results[0]
    assert result.status is CsvRowStatus.ERROR
    assert "所有者" in (result.message or "")
    assert _saved_owners(transaction_repo) == []


def test_t9_invalid_owner_value_still_reported_as_invalid_not_missing(
    import_service: TransactionCsvImportService, tmp_path: Path
) -> None:
    """空欄(未指定)と不正値は別のエラーとして区別され続けること。"""
    csv_path = _write_csv(
        tmp_path,
        f"{_HEADER}\n{'x' * 200},2914,BUY,2026-07-20,100,3400\n",
    )

    summary = import_service.import_file(csv_path)

    result = summary.results[0]
    assert result.status is CsvRowStatus.ERROR
    assert "不正" in (result.message or "")
