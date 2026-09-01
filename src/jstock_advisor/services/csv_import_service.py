"""保有銘柄CSV一括登録サービス(要求仕様3節 csv_import_service、5節)。

MVPではローカルCLIからの取り込みを実装する(S3経由の取り込みは後続フェーズで追加)。
全件成功/全件失敗ではなく、行単位で結果を返す。

Issue #61 Phase B1(2026-09-02)で次の3点を変更した。

1. **ownerを必須列にした。** 従来は`owner`列の欠落・空欄を無警告で
   `DEFAULT_OWNER`へ補完していたが、実際に別所有者の保有が1件へ誤って
   統合される事故が起きたため(functional_spec 変更履歴 2026-08-23)、
   CSV取込では暗黙の補完を廃止しERRORとする。
   **CLI等、CSV以外の経路の既定owner仕様は変更していない。**
2. **CSV内の重複行を登録しない。** 従来は警告を積むだけで登録は実行していた。
3. **同一CSVの再取込・途中失敗後の再実行を冪等にした。**
   行単位の適用済み台帳(`csv_import_ledger`)を参照し、適用済みの行はskipする。

`on_duplicate="additional_purchase"`(既定)の意味論は変更していない。
**別のCSVによる同一銘柄への追加購入は引き続き正当な操作**であり、
「保有が既に存在すること」を重複取込として扱わない。
重複とみなすのは**同一内容のCSVの同一行**だけである。
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.owner import (
    InvalidOwnerError,
    build_holding_id,
    normalize_and_validate_owner,
)
from jstock_advisor.infrastructure.external_value_parser import ExternalValueParser
from jstock_advisor.services.csv_import_ledger import CsvImportLedger, compute_import_id
from jstock_advisor.services.portfolio_service import PortfolioService

# Issue #61 Phase B1: ownerを必須列へ移した(暗黙のDEFAULT_OWNER補完を廃止)。
# 列自体が無いCSVは、行ごとに同じERRORを大量生成せず、既存のヘッダー検証
# (ValueError)で「必須列がありません」として1回だけ返す。
REQUIRED_COLUMNS = {"stock_code", "shares", "purchase_price", "owner"}
OPTIONAL_COLUMNS = {
    "stock_name",
    "purchase_date",
    "account_type",
    "investment_purpose",
    "profit_target_rate",
    "memo",
}
ALL_COLUMNS = REQUIRED_COLUMNS | OPTIONAL_COLUMNS

DuplicatePolicy = Literal["additional_purchase", "overwrite"]


class CsvRowStatus(StrEnum):
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    # Issue #61 Phase B1: 既に取り込み済みの行を「登録せずにskipした」ことを
    # 利用者へ明示する(無音のskipにしない)。取込のやり直しは正常な操作であり
    # ERRORではないため、専用のstatusを設ける。
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    ERROR = "ERROR"


class CsvImportRowResult(BaseModel):
    row_number: int
    status: CsvRowStatus
    stock_code: str | None
    message: str


class CsvImportSummary(BaseModel):
    total_rows: int = 0
    success_count: int = 0
    warning_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
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


class HoldingsCsvImportService:
    def __init__(
        self,
        portfolio_service: PortfolioService | None = None,
        ledger: CsvImportLedger | None = None,
    ) -> None:
        self._portfolio = portfolio_service or PortfolioService()
        self._ledger = ledger or CsvImportLedger()

    def import_file(
        self,
        path: Path,
        on_duplicate: DuplicatePolicy = "additional_purchase",
    ) -> CsvImportSummary:
        summary = CsvImportSummary()
        # Issue #61 Phase B1: **実際に読み込んだバイト列**からimport idを算出する
        # (ファイル名は使わない。別名でも内容が同一なら同一importとして扱う)。
        # 同じバイト列をそのままデコードして解析し、hash対象と解析対象を一致させる。
        content = path.read_bytes()
        import_id = compute_import_id(content)
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if reader.fieldnames is None:
            raise ValueError("CSVにヘッダー行がありません")
        missing = REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            raise ValueError(f"CSVに必須列がありません: {sorted(missing)}")

        now = dt.datetime.now(dt.UTC)
        seen_rows: set[tuple[str, str, str, str, str]] = set()
        overwritten_holding_ids: set[str] = set()
        for row_number, row in enumerate(reader, start=2):  # 1行目はヘッダー
            result = self._process_row(
                row_number,
                row,
                seen_rows,
                on_duplicate,
                overwritten_holding_ids,
                import_id=import_id,
                now=now,
            )
            summary.add(result)
        return summary

    def _process_row(
        self,
        row_number: int,
        row: dict[str, str | None],
        seen_rows: set[tuple[str, str, str, str, str]],
        on_duplicate: DuplicatePolicy,
        overwritten_holding_ids: set[str],
        *,
        import_id: str,
        now: dt.datetime,
    ) -> CsvImportRowResult:
        # Issue #61 Phase B1: ownerを暗黙にDEFAULT_OWNERへ補完しない。
        # 空欄は「未指定」であって「既定の所有者」ではないため、登録せずERRORにする。
        owner_raw = (row.get("owner") or "").strip()
        if not owner_raw:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=ExternalValueParser.stock_code(row.get("stock_code")),
                message="所有者(owner)が未指定です。所有者を明示してください",
            )
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
            raw_stock_code = (row.get("stock_code") or "").strip()
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=raw_stock_code or None,
                message="銘柄コードが不正です(4桁の英数字が必要です)",
            )

        shares = ExternalValueParser.integer(row.get("shares"))
        if shares is None or shares <= 0:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message="株数は正の整数である必要があります",
            )

        purchase_price = ExternalValueParser.decimal(row.get("purchase_price"))
        if purchase_price is None or purchase_price <= 0:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message="購入単価は正の数値である必要があります",
            )

        purchase_date_raw = (row.get("purchase_date") or "").strip()
        if purchase_date_raw:
            purchase_date = ExternalValueParser.date(purchase_date_raw)
            if purchase_date is None:
                return CsvImportRowResult(
                    row_number=row_number,
                    status=CsvRowStatus.ERROR,
                    stock_code=stock_code,
                    message="購入日はYYYY-MM-DD形式で指定してください",
                )
        else:
            purchase_date = dt.date.today()

        messages: list[str] = []

        account_type_raw = (row.get("account_type") or "").strip().upper()
        if account_type_raw:
            try:
                account_type = AccountType(account_type_raw)
            except ValueError:
                return CsvImportRowResult(
                    row_number=row_number,
                    status=CsvRowStatus.ERROR,
                    stock_code=stock_code,
                    message=f"口座区分が不正です: {account_type_raw}",
                )
        else:
            account_type = AccountType.GENERAL
            messages.append("口座区分が未指定のためGENERAL(一般口座)として登録しました")

        profit_target_rate_raw = (row.get("profit_target_rate") or "").strip()
        profit_target_rate: float | None = None
        if profit_target_rate_raw:
            try:
                profit_target_rate = float(profit_target_rate_raw)
            except ValueError:
                return CsvImportRowResult(
                    row_number=row_number,
                    status=CsvRowStatus.ERROR,
                    stock_code=stock_code,
                    message="利確目標率は数値で指定してください",
                )

        # Issue #61 Phase B1: CSV内の完全重複行は**登録しない**。
        # 従来は警告を積むだけで登録は実行しており、CSVの作成ミスがそのまま
        # 保有株数の二重計上になっていた。CSVを直すべき入力の誤りなのでERRORとする
        # (取込のやり直しによるskip=SKIPPED_DUPLICATEとは区別する)。
        dedup_key = (owner, stock_code, str(purchase_date), str(purchase_price), str(shares))
        if dedup_key in seen_rows:
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.ERROR,
                stock_code=stock_code,
                message="CSV内に同一内容の行が重複しています。2件目以降は登録していません",
            )
        seen_rows.add(dedup_key)

        stock_name = (row.get("stock_name") or "").strip() or None
        investment_purpose = (row.get("investment_purpose") or "").strip() or None
        memo = (row.get("memo") or "").strip() or None

        # Issue #61 Phase B1: 同一CSVの再取込・途中失敗後の再実行を冪等にする。
        # **保有が既に存在すること**を重複取込とみなしてはならない(別CSVによる
        # 同一銘柄への追加購入は正当な操作)。重複とみなすのは
        # 「同一内容のCSVの同一行」= import_id × row_number だけである。
        if self._ledger.is_applied(import_id, row_number):
            return CsvImportRowResult(
                row_number=row_number,
                status=CsvRowStatus.SKIPPED_DUPLICATE,
                stock_code=stock_code,
                message="このCSVの同じ行は取り込み済みのため、登録せずにスキップしました",
            )

        holding_id = build_holding_id(owner, stock_code)
        existing = self._portfolio.get_holding(owner, stock_code)
        if (
            existing is not None
            and on_duplicate == "overwrite"
            and holding_id not in overwritten_holding_ids
        ):
            self._portfolio.delete_holding(owner, stock_code)
            overwritten_holding_ids.add(holding_id)
            messages.append("既存の保有銘柄を上書きしました")
        elif existing is not None and on_duplicate == "additional_purchase":
            messages.append("既存の保有銘柄への追加購入として登録しました")

        self._portfolio.register_purchase(
            owner=owner,
            stock_code=stock_code,
            stock_name=stock_name,
            shares=shares,
            purchase_price=purchase_price,
            purchase_date=purchase_date,
            account_type=account_type,
            investment_purpose=investment_purpose,
            profit_target_rate=profit_target_rate,
            memo=memo,
        )
        # **適用が成功した後に**台帳へ記録する。適用前に記録すると、適用が例外で
        # 失敗した行が「適用済み」として永久にskipされ、再実行しても登録されない
        # (データ欠落)。この順序により、途中失敗しても成功済みの行だけがskipされ、
        # 失敗した行は再実行で1回だけ適用される。
        self._ledger.mark_applied(
            import_id,
            row_number,
            owner=owner,
            stock_code=stock_code,
            shares=shares,
            now=now,
        )

        status = CsvRowStatus.WARNING if messages else CsvRowStatus.SUCCESS
        return CsvImportRowResult(
            row_number=row_number,
            status=status,
            stock_code=stock_code,
            message="; ".join(messages) if messages else "登録成功",
        )
