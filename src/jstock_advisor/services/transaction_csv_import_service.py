"""実売買記録CSV一括登録サービス(要求仕様3節 transaction_history_service、28節)。

全件成功/全件失敗ではなく、行単位で結果を返す(csv_import_serviceと同じ方針)。

## 再取込の冪等性(Issue #61 Phase B3)

以前は`transaction_id`を`uuid4()`で毎回採番していたため、**同じCSVを取り込み直す
たびにTransactionが増え続けた**。部分失敗後にやり直すと、成功済みの行だけが
二重に記録された。

現在は取り込み行から決定的な`transaction_id`を作り、**未登録のときだけ保存する**
(`TransactionRepository.save_if_absent()`)。

```
import_id      = SHA-256(実際に読み込んだCSVのバイト列)   … 64桁hex
transaction_id = f"csv:{import_id}:{row_number}"
```

「取込済みか」の判定材料は**永続データそのもの**(当該transaction_idの
Transactionが存在するか)であり、別置きの台帳を持たない。書き込みは
`insert_if_absent`(DynamoDB実装では`attribute_not_exists(transaction_id)`の
条件付き書き込み)で原子的に行うため、事前の存在チェックでは塞げない
TOCTOU raceも塞がれる。

### 保証する契約と、保証しない範囲

**同一バイト列のCSVファイルを同じparserで再取込した場合、各行は最大1回だけ
Transactionとして保存される。** ファイル名はidentityに含めないため、名前を
変えても別ディレクトリへコピーしても同じ取り込みとして扱う。

一方、次はいずれも**バイト列が変わるため別の取り込み**として扱う。

  行順の変更 / 改行コードの変更 / BOMの追加・削除 / 空白等の差異

**一般の「同一業務取引」に対するexactly-onceは保証しない。** CSVに証券会社の
約定IDに相当する列が無く、同日・同銘柄・同数量・同単価の正当な複数約定
(分割約定)を区別できないためである。属性の一致だけで重複と判定すると、
正当な2件目を欠落させる(資産記録の欠損)。
"""

from __future__ import annotations

import csv
import hashlib
import io
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from jstock_advisor.domain.entities.enums import AccountType, TransactionType
from jstock_advisor.domain.entities.owner import (
    DEFAULT_OWNER,
    InvalidOwnerError,
    normalize_and_validate_owner,
)
from jstock_advisor.infrastructure.external_value_parser import ExternalValueParser
from jstock_advisor.services.transaction_history_service import TransactionHistoryService

REQUIRED_COLUMNS = {"stock_code", "transaction_type", "execution_date", "shares", "execution_price"}

# 決定的transaction_idの接頭辞。uuid4由来の既存transaction_idと見分けがつくようにする
# (過去データはuuid4のまま共存する。書き換えない)。
_TRANSACTION_ID_PREFIX = "csv"


def compute_import_id(content: bytes) -> str:
    """取込対象CSVの内容から決定的なimport idを算出する。

    引数は**実際に読み込んだバイト列そのもの**を渡すこと(BOM・改行コードを含む)。
    デコード後の文字列や、正規化後の内容を渡してはならない。
    ファイル名・パス・取込時刻はidentityに含めない。
    """
    return hashlib.sha256(content).hexdigest()


def build_row_transaction_id(import_id: str, row_number: int) -> str:
    """取り込み行に対応するTransactionの決定的PK。

    **これが「この行は取込済みか」の唯一の判定材料**である。別置きの台帳ではなく
    永続データ自身が識別子を持つことで、「取込済みだが記録が無い」というずれが
    構造的に発生しない。

    `row_number`は**ヘッダー後のCSVレコード順に対応する安定した行ordinal**
    (ヘッダーを1として2始まり)であり、**同一バイト列に対して必ず同じ値になる**。
    無効行を除外した連番や成功行だけの連番にはしない(前方に無効行があっても
    後続行のIDが変わらないようにするため)。

    引用符で囲まれた複数行フィールドがある場合、この値はファイル上の物理行番号とは
    一致しない(`csv.DictReader`が返すレコードの通し番号である)。冪等性に必要なのは
    「同じバイト列 → 同じordinal」であり物理行番号そのものではないため、
    この差は識別子の安定性に影響しない。
    """
    return f"{_TRANSACTION_ID_PREFIX}:{import_id}:{row_number}"


class CsvRowStatus(StrEnum):
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    ERROR = "ERROR"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    """既に取込済みの行(Issue #61 Phase B3)。エラーではない。

    同一バイト列のCSVを再取込した場合、その行は登録せずにスキップする。
    再取込全体は正常終了する。
    """


class CsvImportRowResult(BaseModel):
    row_number: int
    status: CsvRowStatus
    stock_code: str | None
    message: str


class CsvImportSummary(BaseModel):
    total_rows: int = 0
    success_count: int = 0
    warning_count: int = 0
    error_count: int = 0
    skipped_count: int = 0
    results: list[CsvImportRowResult] = []

    def add(self, result: CsvImportRowResult) -> None:
        self.results.append(result)
        self.total_rows += 1
        if result.status == CsvRowStatus.SUCCESS:
            self.success_count += 1
        elif result.status == CsvRowStatus.WARNING:
            self.warning_count += 1
        elif result.status == CsvRowStatus.SKIPPED_DUPLICATE:
            self.skipped_count += 1
        else:
            self.error_count += 1


class TransactionCsvImportService:
    def __init__(
        self, transaction_history_service: TransactionHistoryService | None = None
    ) -> None:
        self._service = transaction_history_service or TransactionHistoryService()

    def import_file(self, path: Path) -> CsvImportSummary:
        """CSVを取り込む。同一バイト列の再取込では各行を最大1回だけ登録する。

        取り込みIDは**実際に読み込んだバイト列**から算出するため、ファイル名や
        置き場所には依存しない。
        """
        summary = CsvImportSummary()
        content = path.read_bytes()
        import_id = compute_import_id(content)
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if reader.fieldnames is None:
            raise ValueError("CSVにヘッダー行がありません")
        missing = REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            raise ValueError(f"CSVに必須列がありません: {sorted(missing)}")

        # ヘッダー後のレコード順に対応する安定した行ordinal(ヘッダーを1として2始まり)。
        # 無効行があっても後続行の番号は変わらない。
        for row_number, row in enumerate(reader, start=2):
            summary.add(self._process_row(row_number, row, import_id=import_id))
        return summary

    def _process_row(
        self, row_number: int, row: dict[str, str | None], *, import_id: str
    ) -> CsvImportRowResult:
        owner_raw = (row.get("owner") or "").strip() or DEFAULT_OWNER
        try:
            owner = normalize_and_validate_owner(owner_raw)
        except InvalidOwnerError:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=None,
                message=f"所有者の指定が不正です: {owner_raw!r}",
            )

        stock_code = ExternalValueParser.stock_code(row.get("stock_code"))
        if stock_code is None:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=None,
                message="銘柄コードが未指定です",
            )

        type_raw = (row.get("transaction_type") or "").strip().upper()
        try:
            transaction_type = TransactionType(type_raw)
        except ValueError:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message=f"transaction_typeが不正です: {type_raw}",
            )

        execution_date = ExternalValueParser.date(row.get("execution_date"))
        if execution_date is None:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message="execution_dateはYYYY-MM-DD形式で指定してください",
            )

        shares = ExternalValueParser.integer(row.get("shares"))
        if shares is None or shares <= 0:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message="sharesは正の整数である必要があります",
            )

        execution_price = ExternalValueParser.decimal(row.get("execution_price"))
        if execution_price is None or execution_price <= 0:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message="execution_priceは正の数値である必要があります",
            )

        messages: list[str] = []

        fee_raw = (row.get("fee") or "").strip()
        fee = ExternalValueParser.decimal(fee_raw) if fee_raw else Decimal("0")
        if fee is None:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message="feeは数値で指定してください",
            )

        tax_raw = (row.get("tax") or "").strip()
        tax = ExternalValueParser.decimal(tax_raw) if tax_raw else Decimal("0")
        if tax is None:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message="taxは数値で指定してください",
            )

        account_type_raw = (row.get("account_type") or "").strip().upper()
        account_type: AccountType | None = None
        if account_type_raw:
            try:
                account_type = AccountType(account_type_raw)
            except ValueError:
                return CsvImportRowResult(
                    row_number=row_number,
                    status=CsvRowStatus.ERROR,
                    stock_code=stock_code,
                    message=f"account_typeが不正です: {account_type_raw}",
                )

        recommendation_id = (row.get("recommendation_id") or "").strip() or None
        reason = (row.get("execution_reason") or "").strip() or None
        memo = (row.get("memo") or "").strip() or None

        # 「取込済みか」は永続データそのもの(決定的transaction_idのTransactionの
        # 存在)で判定する。事前にexists()を見てからsave()するcheck-then-actは
        # TOCTOU raceが残るため行わない。insert_if_absentが原子的に保証する。
        transaction_id = build_row_transaction_id(import_id, row_number)
        try:
            saved = self._service.record_execution_if_absent(
                transaction_id=transaction_id,
                owner=owner,
                stock_code=stock_code,
                transaction_type=transaction_type,
                shares=shares,
                execution_price=execution_price,
                execution_date=execution_date,
                recommendation_id=recommendation_id,
                fee=fee,
                tax=tax,
                account_type=account_type,
                reason=reason,
                memo=memo,
            )
        except ValueError as e:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message=str(e),
            )

        if not saved:
            # 同一バイト列のCSVを再取込した行。エラーではなく「取込済み」として
            # 可視化し、登録は行わない(既存Transactionの内容も上書きしない)。
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.SKIPPED_DUPLICATE,
                stock_code=stock_code,
                message="このCSVの同じ行は取り込み済みのため、登録せずにスキップしました",
            )

        status = CsvRowStatus.WARNING if messages else CsvRowStatus.SUCCESS
        return CsvImportRowResult(
            row_number=row_number,
            status=status,
            stock_code=stock_code,
            message="; ".join(messages) or "登録成功",
        )
