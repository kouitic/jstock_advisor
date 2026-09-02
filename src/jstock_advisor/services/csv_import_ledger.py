"""CSV取込の行コミット境界(Issue #61 Phase B1)。

保有CSVの取込は「宣言的な状態の適用」ではなく「イベントの追記」として実装されて
おり、行の同一性(idempotency key)が定義されていなかった。そのため

- 同一CSVを再取込すると保有株数が二重計上される
- 途中で失敗したCSVを「やり直すつもり」で再実行すると、
  **成功済みの行だけが二重計上される**(Phase Aの実測)

という資産記録の破壊が起きていた。

## identityの決め方

```
import_id = SHA-256(実際に読み込んだCSVのバイト列)   … 64桁hex
row_id    = f"{import_id}:{row_number}"
lot_id    = f"csv:{row_id}"      ← **永続データそのものが行の識別子を持つ**
```

- **ファイル名はidentityに使わない。** 別名でも内容が同一なら同一importとして扱い、
  同名でも内容が違えば別importとして扱う。
- hash前に行の並べ替え・数値の再format・owner値の書き換えといった**意味的な正規化は
  行わない**。「実際に取込対象として読み込んだ内容」に対する決定的hashとする。
- SHA-256のfull 64桁を使う(`notification_claim.compute_claim_id()`と同じ方針。
  切り詰めによる衝突リスクを新規に持ち込まない)。

## 「適用済み」の判定を台帳に依存させない(レビュー指摘R1)

当初は「適用 → 台帳へ記録」の順で別々に書いていたため、その間で失敗すると
**適用済みなのに台帳が無い**状態が残り、再実行で二重計上しうる窓があった。

現在は次のようにして、**別置きの台帳と実データがずれても二重計上が起きない**
構造にしている。

```
「この行は適用済みか」= **決定的lot_idのロットが存在するか**
```

行の識別子が永続データ(PurchaseLot)そのもののPKであるため、
「適用済みの記録」と「適用結果」が**同一の書き込み**になり、両者がずれない。
`Holding`は`PurchaseLot`集合からの再計算(`PortfolioService._compute_holding()`)
であり、同じlot_idを再適用しても集合が変わらないため、
**行の適用は何回実行しても同じ最終状態へ収束する**。

## 台帳(監査記録)の役割と順序

台帳(`jstock-audit_log` / `decision_type=csv_holding_import_row`)は
**行コミットの排他claimと監査証跡**として使う。

```
claim(insert_if_absent)  ← 原子的。同時実行でも1プロセスだけが獲得する
   ↓
データ適用(lot / holding)
   ↓ 失敗したら
release(claim削除)       ← 補償。次回の再実行で必ず適用される
```

**claimをデータ適用より先に行う。** 台帳の書き込み自体が失敗した場合に
データが適用済みになっていると、「holding applied / ledger missing」という
禁止状態が残るため。claimが先なら、台帳の失敗時は**何も適用されない**。

claimは`insert_if_absent`(DynamoDB実装では条件付き書き込み)で行うため、
同一row_idを2プロセスが同時にcommitしようとしても**一度しか成立しない**
(単なる事前存在チェックではTOCTOU raceが残るため、これを使う)。

## 保存先

既存の`jstock-audit_log`へ`decision_type`を分けて記録する
(新規テーブル・schema変更・migration・backfillはいずれも不要)。

**読み取りは決定的な`audit_id`によるGetItem相当のみ**で、
`list_all()`やfull Scanは使わない(audit_logは3万件規模であり、
Issue #113で除去したのと同じ全件materialize構造を持ち込まないため)。

`AuditService`ではなく`AuditLogRepository`を直接使う。`AuditService`は
VALIDATION実行時に永続化を抑止するため、**行コミットのclaimには使えない**
(書かれなかったclaimを「未claim」と誤読する)。
"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any

from jstock_advisor.domain.entities.audit import AuditLogEntry
from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.services.buy_signal_service import RULE_VERSION_PLACEHOLDER

DECISION_TYPE_HOLDING_IMPORT_ROW = "csv_holding_import_row"

# 決定的lot_idの接頭辞。uuid4由来の既存lot_idと見分けがつくようにする。
_LOT_ID_PREFIX = "csv"


def compute_import_id(content: bytes) -> str:
    """取込対象CSVの内容から決定的なimport idを算出する。

    引数は**実際に読み込んだバイト列そのもの**を渡すこと(BOM・改行コードを含む)。
    デコード後の文字列や、正規化後の内容を渡してはならない。
    """
    return hashlib.sha256(content).hexdigest()


def build_row_lot_id(import_id: str, row_number: int) -> str:
    """行に対応するPurchaseLotの決定的PK。

    **これが「この行は適用済みか」の唯一の判定材料**である。
    別置きの台帳ではなく永続データ自身が識別子を持つことで、
    「適用済みだが記録が無い」というずれが構造的に発生しない。
    """
    return f"{_LOT_ID_PREFIX}:{import_id}:{row_number}"


def build_row_audit_id(import_id: str, row_number: int) -> str:
    """行コミットのclaim兼監査記録のキー。既存の`<用途>:<識別子>`形式に揃える。"""
    return f"{DECISION_TYPE_HOLDING_IMPORT_ROW}:{import_id}:{row_number}"


class CsvImportLedger:
    """CSV取込の行コミットを排他的にclaimし、監査証跡を残す。"""

    def __init__(self, repository: AuditLogRepository | None = None) -> None:
        self._repository = repository or AuditLogRepository()

    def claim(
        self,
        import_id: str,
        row_number: int,
        *,
        owner: str,
        stock_code: str,
        shares: int,
        now: dt.datetime,
    ) -> bool:
        """行コミットを排他的に獲得する。獲得できたらTrue、既に他が獲得済みならFalse。

        `insert_if_absent`(DynamoDB実装では条件付き書き込み)による**原子的な**
        獲得であり、事前の存在チェックでは塞げないTOCTOU raceを塞ぐ。
        """
        input_values: dict[str, Any] = {
            "import_id": import_id,
            "row_number": row_number,
            "owner": owner,
            "shares": shares,
            "lot_id": build_row_lot_id(import_id, row_number),
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
        return self._repository.save_if_absent(entry)

    def release(self, import_id: str, row_number: int) -> None:
        """データ適用に失敗したclaimを解放する(補償)。

        解放しておかないと、実データが未適用のまま「claim済み」が残り、
        再実行しても適用されない(データ欠落)。

        なお解放自体に失敗しても二重計上は起きない。「適用済みか」の判定は
        claimではなく**決定的lot_idのロットの存在**で行うため、
        次回の再実行では未適用と判定され、正しく適用される。
        """
        self._repository.delete(build_row_audit_id(import_id, row_number))
