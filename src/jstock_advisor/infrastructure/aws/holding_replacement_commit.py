"""保有(Holding)と全購入ロット(PurchaseLot)の原子的な置換・削除を、
DynamoDB TransactWriteItemsによる単一コミットとして実行する(Issue #61 Phase B2)。

overwrite取込(`--on-duplicate overwrite`)と保有削除(CLI)は、いずれも
「既存の全ロット + Holding を消して、置換の場合は新しいロット + Holding を作る」
という同一の操作である。これを個別のwriteへ分解すると、途中で失敗したときに
部分状態(Holdingだけ旧値でロットが一部欠落 / Holdingが消えてロットだけ残る等)が
残り、**再実行しても元の保有は復元されない**。

1回の`transact_write_items`呼び出しに次をすべて含めることで、
DynamoDBが「全部成功 or 全部不成功」を保証する。

```
既存ロットのDelete × N   (楽観ロック: #data = :expected_data)
既存HoldingのDelete × 1  (同上。Holdingが存在する場合のみ)
新ロットのPut × 1        (attribute_not_exists(PK)。置換の場合のみ)
新HoldingのPut × 1       (同上)
```

TransactWriteItemsの構築・条件式・リトライ方針は
`dynamodb_transaction.py`(会話型UIの確定操作と共有するプリミティブ)に従う。
ロット数の業務上限(`portfolio_service.MAX_LOTS_PER_HOLDING`)は
**計画構築の時点で**検証済みであり、本モジュールへ到達する計画は上限内である。

`conversation_commit.py`と異なり、ConversationStateの消費・Transactionの記録・
取引停止フラグのConditionCheckは含まない(本経路はCSV取込とCLI削除であり、
会話型UIの確定操作ではないため)。
"""

from __future__ import annotations

from typing import Any

from jstock_advisor.infrastructure.aws import dynamodb_transaction
from jstock_advisor.infrastructure.collection_store import resolve_table_name
from jstock_advisor.services.write_plan import HoldingReplacementPlan

_HOLDINGS_TABLE_FILE = "holdings_v2.json"
_PURCHASE_LOTS_TABLE_FILE = "purchase_lots.json"


class HoldingReplacementConflictError(RuntimeError):
    """置換対象のHolding/ロットが、計画構築後に別経路で変更されていた。

    楽観ロック条件(`#data = :expected_data`)が成立せずトランザクション全体が
    失敗したことを表す。**この時点でHolding・PurchaseLotはいずれも変更されて
    いない**(DynamoDBが全部不成功を保証する)。古い計画のまま再試行してはならず、
    呼び出し側は状態を読み直すこと。
    """


def build_transact_items(plan: HoldingReplacementPlan) -> list[dict[str, Any]]:
    """計画をTransactWriteItemsの項目列へ変換する(送信は行わない)。

    テストが「1回のトランザクションへ何が入るか」を直接検証できるよう、
    構築と送信を分けている。
    """
    lots_table = resolve_table_name(_PURCHASE_LOTS_TABLE_FILE)
    holdings_table = resolve_table_name(_HOLDINGS_TABLE_FILE)

    items: list[dict[str, Any]] = []
    for lot_delete in plan.lot_deletes:
        items.append(dynamodb_transaction.conditional_delete_transact_item(lots_table, lot_delete))
    if plan.holding_delete is not None:
        items.append(
            dynamodb_transaction.conditional_delete_transact_item(
                holdings_table, plan.holding_delete
            )
        )
    if plan.lot_put is not None:
        items.append(dynamodb_transaction.conditional_put_transact_item(lots_table, plan.lot_put))
    if plan.holding_put is not None:
        items.append(
            dynamodb_transaction.conditional_put_transact_item(holdings_table, plan.holding_put)
        )
    return items


def commit_holding_replacement(plan: HoldingReplacementPlan) -> None:
    """計画を単一のTransactWriteItemsで適用する。

    成功時は何も返さない。楽観ロック競合(計画構築後に対象が変更された)の場合は
    `HoldingReplacementConflictError`を送出する。スロットリング・内部障害・
    権限不足等の非業務エラーはそのまま伝播する。

    **いずれの失敗でもHolding・PurchaseLotは変更されない**(全部不成功)。
    """
    items = build_transact_items(plan)
    if not items:
        return
    if len(items) > dynamodb_transaction.MAX_TRANSACT_ITEMS:
        # 業務上限(MAX_LOTS_PER_HOLDING)の検証を通過していれば到達しない。
        # 物理上限に対する最後の防波堤であり、部分適用へフォールバックしない。
        raise RuntimeError(
            f"書き込み項目が{len(items)}件あり、DynamoDBの上限"
            f"({dynamodb_transaction.MAX_TRANSACT_ITEMS}件)を超えています。"
            "データを変更せず中止しました。"
        )
    if not dynamodb_transaction.commit(items):
        raise HoldingReplacementConflictError(
            "保有または購入ロットが処理中に変更されたため、置換を中止しました。"
            "データは変更されていません。最新の状態を確認してからやり直してください。"
        )
