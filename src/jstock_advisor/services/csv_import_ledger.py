"""CSV取込の適用済み台帳(Issue #61 Phase B1)。

保有CSVの取込は「宣言的な状態の適用」ではなく「イベントの追記」として実装されて
おり、行の同一性(idempotency key)が定義されていなかった。そのため

- 同一CSVを再取込すると保有株数が二重計上される
- 途中で失敗したCSVを「やり直すつもり」で再実行すると、
  **成功済みの行だけが二重計上される**(Phase Aの実測)

という資産記録の破壊が起きていた。本モジュールは、**行単位で「適用済み」を
永続化**することでこれを防ぐ。

## identityの決め方

```
import_id = SHA-256(実際に読み込んだCSVのバイト列)   … 64桁hex
row_id    = f"{import_id}:{row_number}"
```

- **ファイル名はidentityに使わない。** 別名でも内容が同一なら同一importとして扱い、
  同名でも内容が違えば別importとして扱う(Issue #61 Phase B1の要件)。
- hash前に行の並べ替え・数値の再format・owner値の書き換えといった**意味的な正規化は
  行わない**。「実際に取込対象として読み込んだ内容」に対する決定的hashとする。
- SHA-256のfull 64桁を使う(`notification_claim.compute_claim_id()`と同じ方針。
  切り詰めによる衝突リスクを新規に持ち込まない)。

## 保存先

既存の`jstock-audit_log`へ`decision_type`を分けて記録する
(新規テーブル・migration・backfillはいずれも不要)。

**読み取りは決定的な`audit_id`によるGetItem相当のみ**で、
`list_all()`やfull Scanは使わない(audit_logは3万件規模であり、
Issue #113で除去したのと同じ全件materialize構造を持ち込まないため)。

`AuditService`ではなく`AuditLogRepository`を直接使う。`AuditService`は
VALIDATION実行時に永続化を抑止するため、**冪等性の判定に使う台帳としては
使えない**(書かれなかった台帳を「未適用」と誤読する)。

## 保証範囲(Phase B1の境界)

行の適用(`register_purchase`等)と台帳への記録は**別の書き込み**であり、
その間でプロセスが強制終了すると、行は適用済みだが台帳に残らない。
この場合の再実行では**その1行だけ**が二重計上される。

現状(再実行で**全行**が二重計上される)と比べれば窓は大幅に狭いが、
完全に閉じるには適用と台帳記録の原子化が必要であり、
これは**Issue #61 Phase B2(overwriteの原子性)の範囲**とする。
"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any

from jstock_advisor.domain.entities.audit import AuditLogEntry
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.services.buy_signal_service import RULE_VERSION_PLACEHOLDER

DECISION_TYPE_HOLDING_IMPORT_ROW = "csv_holding_import_row"


def compute_import_id(content: bytes) -> str:
    """取込対象CSVの内容から決定的なimport idを算出する。

    引数は**実際に読み込んだバイト列そのもの**を渡すこと(BOM・改行コードを含む)。
    デコード後の文字列や、正規化後の内容を渡してはならない。
    """
    return hashlib.sha256(content).hexdigest()


def build_row_audit_id(import_id: str, row_number: int) -> str:
    """行単位の台帳キー。既存の`<用途>:<識別子>`形式に揃える。"""
    return f"{DECISION_TYPE_HOLDING_IMPORT_ROW}:{import_id}:{row_number}"


class CsvImportLedger:
    """CSV取込の「この行は適用済み」を記録・照会する。"""

    def __init__(self, repository: AuditLogRepository | None = None) -> None:
        self._repository = repository or AuditLogRepository()

    def is_applied(self, import_id: str, row_number: int) -> bool:
        """決定的キーによる単一取得のみ(list_all()/Scanは使わない)。"""
        return self._repository.get(build_row_audit_id(import_id, row_number)) is not None

    def mark_applied(
        self,
        import_id: str,
        row_number: int,
        *,
        owner: str,
        stock_code: str,
        shares: int,
        now: dt.datetime,
    ) -> None:
        """行の適用が成功した**後**に呼ぶ。

        適用前に記録すると、適用が例外で失敗した行が「適用済み」として
        永久にskipされ、再実行しても登録されなくなる(データ欠落)。
        したがって順序は **適用 → 記録** とする。
        """
        input_values: dict[str, Any] = {
            "import_id": import_id,
            "row_number": row_number,
            "owner": owner,
            "shares": shares,
        }
        entry = AuditLogEntry(
            audit_id=build_row_audit_id(import_id, row_number),
            timestamp=now,
            stock_code=stock_code,
            decision_type=DECISION_TYPE_HOLDING_IMPORT_ROW,
            input_values=input_values,
            calculation_formulas={},
            output_values={"applied": True},
            data_sources=[],
            rule_version=RULE_VERSION_PLACEHOLDER,
        )
        self._repository.save_if_absent(entry)
