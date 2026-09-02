"""Issue #61 Phase B3 — 取引CSV再取込の冪等性(F-C3 / AC2)の回帰テスト。

## 何が問題だったか

`TransactionHistoryService.record_execution()` が `transaction_id` を
`uuid4()` で毎回採番し、`TransactionRepository.save()` が無条件 upsert だったため、
**同じCSVを取り込み直すたびに Transaction が増え続けた**。部分失敗後にやり直すと、
成功済みの行だけが二重に記録された。

なお取引CSV取込は Transaction のみを書き、Holding / PurchaseLot を更新しない。
そのため保有株数・平均取得単価の二重反映は元から発生しない(この契約自体を
本ファイルで固定する)。

## このテストが固定する契約

**同一バイト列のCSVファイルを同じparserで再取込した場合、各行は最大1回だけ
Transactionとして保存される。** ファイル名はidentityに含めない。

次はいずれもバイト列が変わるため**別の取り込み**として扱う。

  行順の変更 / 改行コードの変更 / BOMの追加・削除 / 空白等の差異

## このテストが固定「しない」こと

一般の「同一業務取引」に対する exactly-once。CSVに証券会社の約定IDに相当する
列が無く、同日・同銘柄・同数量・同単価の正当な複数約定(分割約定)を区別
できないためである。属性の一致だけで重複と判定すると正当な2件目を欠落させる。
"""

from __future__ import annotations

import datetime as dt
import shutil
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import AccountType, TransactionType
from jstock_advisor.infrastructure.local_repository.transaction_repository import (
    TransactionRepository,
)
from jstock_advisor.services.transaction_csv_import_service import (
    CsvRowStatus,
    TransactionCsvImportService,
    build_row_transaction_id,
    compute_import_id,
)
from jstock_advisor.services.transaction_history_service import TransactionHistoryService

_OWNER = "所有者A"
_CODE = "2914"
_HEADER = "owner,stock_code,transaction_type,execution_date,shares,execution_price\n"
_ROW = f"{_OWNER},{_CODE},BUY,2026-03-01,100,4200\n"


@pytest.fixture
def repository(tmp_path: Path) -> TransactionRepository:
    return TransactionRepository(store_dir=tmp_path)


@pytest.fixture
def history(repository: TransactionRepository) -> TransactionHistoryService:
    return TransactionHistoryService(transaction_repository=repository)


@pytest.fixture
def importer(history: TransactionHistoryService) -> TransactionCsvImportService:
    return TransactionCsvImportService(transaction_history_service=history)


def _write(tmp_path: Path, body: str = _ROW, name: str = "tx.csv") -> Path:
    path = tmp_path / name
    path.write_text(_HEADER + body, encoding="utf-8")
    return path


def _count(history: TransactionHistoryService) -> int:
    return len(history.list_transactions())


def _ids(history: TransactionHistoryService) -> list[str]:
    return sorted(t.transaction_id for t in history.list_transactions())


# --- T1〜T4 同一バイト列の再取込は冪等 -----------------------------------------


def test_reimporting_the_same_file_does_not_add_transactions(
    tmp_path: Path, history: TransactionHistoryService, importer: TransactionCsvImportService
) -> None:
    """T1: 同一CSVを2回取り込んでも件数が変わらない。"""
    path = _write(tmp_path)

    importer.import_file(path)
    first_count = _count(history)
    first_ids = _ids(history)
    summary = importer.import_file(path)

    assert first_count == 1
    assert _count(history) == first_count, "再取込で二重登録された"
    assert _ids(history) == first_ids, "transaction_idが変化した"
    assert summary.results[0].status is CsvRowStatus.SKIPPED_DUPLICATE
    assert summary.skipped_count == 1
    assert summary.error_count == 0, "再取込全体は正常終了すること"


def test_reimporting_a_single_row_file_is_idempotent(
    tmp_path: Path, history: TransactionHistoryService, importer: TransactionCsvImportService
) -> None:
    """T2: 同一transaction行だけのCSVを再取込しても増えない。"""
    path = _write(tmp_path)
    for _ in range(4):
        importer.import_file(path)

    assert _count(history) == 1


def test_renaming_the_file_does_not_change_identity(
    tmp_path: Path, history: TransactionHistoryService, importer: TransactionCsvImportService
) -> None:
    """T3: ファイル名だけ変更してもバイト列が同じなら同一の取り込み。"""
    path = _write(tmp_path)
    importer.import_file(path)
    before = _ids(history)

    renamed = tmp_path / "renamed.csv"
    shutil.copy(path, renamed)
    summary = importer.import_file(renamed)

    assert _ids(history) == before
    assert summary.results[0].status is CsvRowStatus.SKIPPED_DUPLICATE


def test_copying_to_another_directory_does_not_change_identity(
    tmp_path: Path, history: TransactionHistoryService, importer: TransactionCsvImportService
) -> None:
    """T4: 別ディレクトリへコピーしてもバイト列が同じなら同一の取り込み。"""
    path = _write(tmp_path)
    importer.import_file(path)
    before = _ids(history)

    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    copied = other_dir / "tx.csv"
    shutil.copy(path, copied)
    summary = importer.import_file(copied)

    assert _ids(history) == before
    assert summary.results[0].status is CsvRowStatus.SKIPPED_DUPLICATE


# --- T5 / T6 正当な同一内容行を潰さない ----------------------------------------


def test_identical_rows_in_one_file_are_both_saved(
    tmp_path: Path, history: TransactionHistoryService, importer: TransactionCsvImportService
) -> None:
    """T5: 同一内容の2行は**両方**保存される(分割約定を欠落させない)。

    同日・同銘柄・同数量・同単価の約定が正当に複数存在しうるため、属性の一致
    だけで重複と判定してはならない。
    """
    path = _write(tmp_path, _ROW + _ROW)

    summary = importer.import_file(path)

    assert _count(history) == 2, "正当な同一属性の2件目が欠落した"
    assert [r.status for r in summary.results] == [CsvRowStatus.SUCCESS, CsvRowStatus.SUCCESS]
    assert len(set(_ids(history))) == 2, "2件が同じtransaction_idになっている"


def test_reimporting_a_file_with_identical_rows_adds_nothing(
    tmp_path: Path, history: TransactionHistoryService, importer: TransactionCsvImportService
) -> None:
    """T6: T5のCSVを再取込しても2件のまま増えない。"""
    path = _write(tmp_path, _ROW + _ROW)
    importer.import_file(path)
    before = _ids(history)

    summary = importer.import_file(path)

    assert _count(history) == 2
    assert _ids(history) == before
    assert all(r.status is CsvRowStatus.SKIPPED_DUPLICATE for r in summary.results)


# --- T7 / T8 取引種別ごとの再取込 ----------------------------------------------


@pytest.mark.parametrize(
    "transaction_type",
    [
        TransactionType.BUY,
        TransactionType.ADDITIONAL_BUY,
        TransactionType.PARTIAL_SELL,
        TransactionType.FULL_SELL,
    ],
)
def test_reimport_is_idempotent_for_every_transaction_type(
    tmp_path: Path,
    history: TransactionHistoryService,
    importer: TransactionCsvImportService,
    transaction_type: TransactionType,
) -> None:
    """T7 / T8: BUY / ADDITIONAL_BUY / PARTIAL_SELL / FULL_SELL のいずれも冪等。"""
    path = _write(tmp_path, f"{_OWNER},{_CODE},{transaction_type.value},2026-03-05,50,4500\n")

    importer.import_file(path)
    first = _count(history)
    importer.import_file(path)

    assert first == 1, f"{transaction_type.value} の初回登録に失敗した"
    assert _count(history) == 1, f"{transaction_type.value} が二重登録された"


# --- T9 owner --------------------------------------------------------------


def test_different_owners_are_separate_transactions(
    tmp_path: Path, history: TransactionHistoryService, importer: TransactionCsvImportService
) -> None:
    """T9: owner が違えば別の取引として登録される。"""
    path = _write(tmp_path, _ROW + f"所有者B,{_CODE},BUY,2026-03-01,100,4200\n")

    importer.import_file(path)

    assert _count(history) == 2
    assert sorted(t.owner or "" for t in history.list_transactions()) == ["所有者A", "所有者B"]


# --- T10 / T13 部分失敗後の retry と行番号の安定性 -----------------------------


def test_retry_after_partial_failure_adds_only_the_failed_row(
    tmp_path: Path, history: TransactionHistoryService, importer: TransactionCsvImportService
) -> None:
    """T10: 部分失敗後のretryで、成功済みの行が二重計上されない。

    修正前は成功済みの行にも新しいuuid4が振られ、retryのたびに増えていた。
    """
    good = _ROW
    bad = f"{_OWNER},7203,BUY,2026-03-02,-5,2500\n"
    path = _write(tmp_path, good + bad)

    first = importer.import_file(path)
    assert first.success_count == 1
    assert first.error_count == 1
    assert _count(history) == 1

    fixed = f"{_OWNER},7203,BUY,2026-03-02,5,2500\n"
    path.write_text(_HEADER + good + fixed, encoding="utf-8")
    second = importer.import_file(path)

    assert _count(history) == 3, (
        "行を修正するとバイト列が変わるため、この再取込は別importとして扱われる"
    )
    assert second.success_count == 2


def test_row_identity_is_stable_when_an_invalid_row_precedes_valid_rows(
    tmp_path: Path, history: TransactionHistoryService, importer: TransactionCsvImportService
) -> None:
    """T13: 前方に無効行があっても、同一CSVの再取込で後続行のIDが変わらない。

    無効行を除外した連番や成功行だけの連番にしていると、retry時にIDがずれて
    二重登録になる。
    """
    bad = f"{_OWNER},7203,BUY,2026-03-02,-5,2500\n"
    path = _write(tmp_path, bad + _ROW)

    first = importer.import_file(path)
    assert first.error_count == 1
    assert first.success_count == 1
    first_ids = _ids(history)

    second = importer.import_file(path)

    assert _ids(history) == first_ids, "無効行の存在で後続行のIDがずれた"
    assert _count(history) == 1
    assert second.skipped_count == 1


def test_row_number_is_the_physical_csv_row(
    tmp_path: Path, history: TransactionHistoryService, importer: TransactionCsvImportService
) -> None:
    """行番号はヘッダーを1行目とする物理行番号であること。"""
    bad = f"{_OWNER},7203,BUY,2026-03-02,-5,2500\n"
    path = _write(tmp_path, bad + _ROW)
    content = path.read_bytes()

    importer.import_file(path)

    expected = build_row_transaction_id(compute_import_id(content), 3)
    assert _ids(history) == [expected], "無効行を除いた連番になっている"


# --- T12 バイト列が変われば別import -------------------------------------------


def test_reordered_rows_are_treated_as_a_different_import(
    tmp_path: Path, history: TransactionHistoryService, importer: TransactionCsvImportService
) -> None:
    """T12: 行順の変更はバイト列が変わるため別importとして扱う(契約の明示)。

    これは「同一業務取引の exactly-once」を保証しないという契約の帰結であり、
    仕様として固定する。
    """
    row_a = _ROW
    row_b = f"{_OWNER},7203,BUY,2026-03-02,50,2500\n"
    first_path = _write(tmp_path, row_a + row_b, name="a.csv")
    importer.import_file(first_path)
    assert _count(history) == 2

    second_path = _write(tmp_path, row_b + row_a, name="b.csv")
    importer.import_file(second_path)

    assert _count(history) == 4, "行順が変わればバイト列が変わるため別importになる"


def test_import_id_depends_only_on_bytes(tmp_path: Path) -> None:
    """import_idはバイト列だけで決まり、ファイル名・パスに依存しない。"""
    content = (_HEADER + _ROW).encode("utf-8")

    assert compute_import_id(content) == compute_import_id(bytes(content))
    assert len(compute_import_id(content)) == 64, "SHA-256のfull 64桁を使う"
    assert compute_import_id(content) != compute_import_id(content + b" ")


def test_transaction_id_is_deterministic_and_row_scoped() -> None:
    content = (_HEADER + _ROW).encode("utf-8")
    import_id = compute_import_id(content)

    assert build_row_transaction_id(import_id, 2) == build_row_transaction_id(import_id, 2)
    assert build_row_transaction_id(import_id, 2) != build_row_transaction_id(import_id, 3)
    assert build_row_transaction_id(import_id, 2).startswith("csv:"), (
        "uuid4由来のIDと見分けがつくこと"
    )


# --- T11 同時実行 --------------------------------------------------------------


def test_concurrent_insert_of_the_same_row_succeeds_only_once(
    history: TransactionHistoryService,
) -> None:
    """T11: 同一transaction_idへのinsert_if_absentは1件だけ成功する。

    事前の存在チェック(check-then-act)ではTOCTOU raceが残るため、
    原子的な条件付き書き込みで塞ぐ。
    """
    transaction_id = build_row_transaction_id("a" * 64, 2)
    kwargs: dict[str, Any] = {
        "owner": _OWNER,
        "stock_code": _CODE,
        "transaction_type": TransactionType.BUY,
        "shares": 100,
        "execution_price": Decimal("4200"),
        "execution_date": dt.date(2026, 3, 1),
    }

    first = history.record_execution_if_absent(transaction_id=transaction_id, **kwargs)
    second = history.record_execution_if_absent(transaction_id=transaction_id, **kwargs)

    assert (first, second) == (True, False)
    assert _count(history) == 1


def test_existing_transaction_is_not_overwritten(
    history: TransactionHistoryService, repository: TransactionRepository
) -> None:
    """既に存在する行は内容も上書きしない。"""
    transaction_id = build_row_transaction_id("b" * 64, 2)
    history.record_execution_if_absent(
        transaction_id=transaction_id,
        owner=_OWNER,
        stock_code=_CODE,
        transaction_type=TransactionType.BUY,
        shares=100,
        execution_price=Decimal("4200"),
        execution_date=dt.date(2026, 3, 1),
    )
    before = repository.get(transaction_id)
    assert before is not None

    history.record_execution_if_absent(
        transaction_id=transaction_id,
        owner=_OWNER,
        stock_code=_CODE,
        transaction_type=TransactionType.BUY,
        shares=999,
        execution_price=Decimal("9999"),
        execution_date=dt.date(2026, 3, 1),
    )

    after = repository.get(transaction_id)
    assert after is not None
    assert after.shares == before.shares == 100, "既存Transactionが上書きされた"
    assert after.execution_price == before.execution_price


# --- T14 既存 uuid4 データとの後方互換 -----------------------------------------


def test_existing_uuid4_transactions_remain_readable_and_untouched(
    tmp_path: Path,
    history: TransactionHistoryService,
    repository: TransactionRepository,
    importer: TransactionCsvImportService,
) -> None:
    """T14: uuid4で採番された過去データを読めること・書き換えないこと。"""
    legacy_id = str(uuid.uuid4())
    history.record_execution_if_absent(
        transaction_id=legacy_id,
        owner=_OWNER,
        stock_code=_CODE,
        transaction_type=TransactionType.BUY,
        shares=77,
        execution_price=Decimal("1234"),
        execution_date=dt.date(2026, 1, 1),
    )

    importer.import_file(_write(tmp_path))

    legacy = repository.get(legacy_id)
    assert legacy is not None, "過去データが読めなくなった"
    assert legacy.shares == 77, "過去データが書き換えられた"
    assert _count(history) == 2, "過去データと新規取込が共存すること"


# --- 取引CSVは保有を更新しない(既存契約の維持) --------------------------------


def test_transaction_csv_import_does_not_touch_holdings(
    tmp_path: Path, importer: TransactionCsvImportService
) -> None:
    """取引CSV取込はTransactionのみを書き、Holding / PurchaseLotを更新しない。

    この契約により、再取込による保有株数・平均取得単価の二重反映は元から
    発生しない。Phase B3でもこの契約を変えない。
    """
    from jstock_advisor.infrastructure.local_repository.holding_repository import (
        HoldingRepository,
        PurchaseLotRepository,
    )
    from jstock_advisor.services.portfolio_service import PortfolioService

    portfolio = PortfolioService(
        holding_repository=HoldingRepository(store_dir=tmp_path),
        lot_repository=PurchaseLotRepository(store_dir=tmp_path),
    )
    path = _write(tmp_path)

    importer.import_file(path)
    importer.import_file(path)

    assert portfolio.get_holding(_OWNER, _CODE) is None
    assert portfolio.list_lots(_OWNER, _CODE) == []


# --- T15 LINE会話型UI経路の回帰 ------------------------------------------------


def test_conversation_operation_id_path_is_unchanged(
    history: TransactionHistoryService, repository: TransactionRepository
) -> None:
    """T15: operation_idをtransaction_idとして使う会話型UI経路の契約が不変。

    `build_execution_plan()` は永続化を行わず計画のみを返す。
    """
    operation_id = str(uuid.uuid4())
    plan = history.build_execution_plan(
        transaction_id=operation_id,
        owner=_OWNER,
        stock_code=_CODE,
        transaction_type=TransactionType.BUY,
        shares=100,
        execution_price=Decimal("4200"),
        execution_date=dt.date(2026, 3, 1),
        account_type=AccountType.SPECIFIC,
    )

    assert plan.transaction_id == operation_id
    assert repository.get(operation_id) is None, "build_execution_plan が永続化している"
    assert _count(history) == 0


def test_record_execution_still_generates_a_unique_id(
    history: TransactionHistoryService,
) -> None:
    """既存の record_execution()(単発CLI経路)の挙動は変えていない。"""
    kwargs: dict[str, Any] = {
        "owner": _OWNER,
        "stock_code": _CODE,
        "transaction_type": TransactionType.BUY,
        "shares": 100,
        "execution_price": Decimal("4200"),
        "execution_date": dt.date(2026, 3, 1),
    }

    first = history.record_execution(**kwargs)
    second = history.record_execution(**kwargs)

    assert first.transaction_id != second.transaction_id
    assert _count(history) == 2


# --- T16 / T17 duplicate 判定を可変状態へ依存させない --------------------------
#
# `build_execution_plan()` は recommendation_id の実在を Recommendation
# リポジトリへ問い合わせる。取込済みかどうかの判定をその後ろに置くと、
# 「取込時点では存在したがその後に削除された推奨」を参照する再取込がエラーになり、
# 「同一バイト列のCSVの再取込は正常な no-op」という契約に違反する。

_HEADER_WITH_REC = (
    "owner,stock_code,transaction_type,execution_date,shares,execution_price,"
    "recommendation_id\n"
)
_ROW_WITH_REC = f"{_OWNER},{_CODE},BUY,2026-03-01,100,4200,R1\n"


def _recommendation(recommendation_id: str) -> Any:
    from jstock_advisor.domain.entities.enums import ConfidenceLevel, RecommendationType
    from jstock_advisor.domain.entities.recommendation import Recommendation

    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code=_CODE,
        stock_name="J社",
        recommended_at=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
        recommendation_type=RecommendationType.BUY,
        price_at_recommendation=Decimal("4200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1",
    )


def _with_recommendation(
    tmp_path: Path, repository: TransactionRepository
) -> tuple[Any, TransactionHistoryService, TransactionCsvImportService, Path]:
    from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
        RecommendationRepository,
    )

    recommendations = RecommendationRepository(store_dir=tmp_path)
    recommendations.save(_recommendation("R1"))
    history = TransactionHistoryService(
        transaction_repository=repository, recommendation_repository=recommendations
    )
    importer = TransactionCsvImportService(transaction_history_service=history)
    path = tmp_path / "with_rec.csv"
    path.write_text(_HEADER_WITH_REC + _ROW_WITH_REC, encoding="utf-8")
    return recommendations, history, importer, path


def test_reimport_is_idempotent_even_after_the_recommendation_is_removed(
    tmp_path: Path, repository: TransactionRepository
) -> None:
    """T16: 取込後に recommendation が取得不能になっても、再取込は正常なno-op。

    duplicate 判定が RecommendationRepository の現在の状態へ依存していないことを
    固定する。
    """
    recommendations, history, importer, path = _with_recommendation(tmp_path, repository)

    first = importer.import_file(path)
    assert first.results[0].status is CsvRowStatus.SUCCESS
    assert _count(history) == 1
    before = _ids(history)

    # 取込後に推奨が取得不能になる
    recommendations.delete("R1")
    assert recommendations.get("R1") is None

    second = importer.import_file(path)

    assert second.results[0].status is CsvRowStatus.SKIPPED_DUPLICATE
    assert second.error_count == 0, "取込済み行の再取込がエラーになった"
    assert second.skipped_count == 1
    assert _count(history) == 1
    assert _ids(history) == before


def test_duplicate_detection_does_not_read_the_recommendation_repository(
    tmp_path: Path, repository: TransactionRepository
) -> None:
    """T16補: 取込済み行の再取込で RecommendationRepository を参照しないこと。"""
    recommendations, _history, importer, path = _with_recommendation(tmp_path, repository)
    importer.import_file(path)

    def _forbidden(_recommendation_id: str) -> Any:
        raise AssertionError("取込済み判定でRecommendationRepositoryを参照している")

    recommendations.get = _forbidden  # type: ignore[method-assign]

    summary = importer.import_file(path)

    assert summary.results[0].status is CsvRowStatus.SKIPPED_DUPLICATE
    assert summary.error_count == 0


def test_pre_read_fast_path_does_not_weaken_the_conditional_write(
    history: TransactionHistoryService, repository: TransactionRepository
) -> None:
    """T17: 事前readが双方とも不存在を返しても、insertは1件だけ成功する。

    事前readはあくまで最適化(fast-path)であり、一意性の権威は
    `save_if_absent`(DynamoDB実装では条件付き書き込み)側にある。
    事前readと書き込みの間に他プロセスが保存しても、書き込み側で検出される。
    """
    transaction_id = build_row_transaction_id("c" * 64, 2)
    kwargs: dict[str, Any] = {
        "owner": _OWNER,
        "stock_code": _CODE,
        "transaction_type": TransactionType.BUY,
        "shares": 100,
        "execution_price": Decimal("4200"),
        "execution_date": dt.date(2026, 3, 1),
    }
    assert repository.get(transaction_id) is None

    original_get = repository.get
    calls: list[str] = []

    def _stale_get(item_id: str) -> Any:
        """最初の2回は「不存在」を返す(古いスナップショットを読んだ状況を模す)。"""
        calls.append(item_id)
        if len(calls) <= 2:
            return None
        return original_get(item_id)

    repository.get = _stale_get  # type: ignore[method-assign]
    try:
        first = history.record_execution_if_absent(transaction_id=transaction_id, **kwargs)
        second = history.record_execution_if_absent(transaction_id=transaction_id, **kwargs)
    finally:
        repository.get = original_get  # type: ignore[method-assign]

    assert (first, second) == (True, False), (
        "事前readが両方とも不存在を返しても、書き込みは1件だけ成立すること"
    )
    assert _count(history) == 1
    assert len(calls) >= 2, "事前readのfast-pathが呼ばれている"


def test_final_write_is_insert_if_absent_not_unconditional_save(
    history: TransactionHistoryService, repository: TransactionRepository
) -> None:
    """最終的な書き込みが save_if_absent であり、無条件 save ではないこと。"""
    transaction_id = build_row_transaction_id("d" * 64, 2)
    kwargs: dict[str, Any] = {
        "owner": _OWNER,
        "stock_code": _CODE,
        "transaction_type": TransactionType.BUY,
        "shares": 100,
        "execution_price": Decimal("4200"),
        "execution_date": dt.date(2026, 3, 1),
    }

    def _forbidden(_transaction: Any) -> None:
        raise AssertionError("無条件のsave()が使われている(条件付き書き込みでない)")

    repository.save = _forbidden  # type: ignore[method-assign]

    assert history.record_execution_if_absent(transaction_id=transaction_id, **kwargs) is True
    assert _count(history) == 1
