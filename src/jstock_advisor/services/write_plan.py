"""LINEボタン起点会話型UIのTransactWriteItems原子コミット(実装プランv2 3節・
追加条件1「楽観ロック必須化」)向けに、PortfolioService等が「一切の永続化を
行わず、書き込み計画のみを返す」ために使う共有データ構造。

`expected_data`は計画構築時点でDynamoDBから読み取った`data`属性の生JSON
文字列そのもの(モデルを`model_dump_json()`で再シリアライズした値ではない。
再シリアライズ結果はフィールド順序等の理由でバイト単位の一致が保証されない
ため、実際に保存されているバイト列と完全一致する値のみを条件に使う)。
新規追加アイテムは`expected_data=None`とし、呼び出し側(conversation_commit.py)
がTransactWriteItems構築時に`attribute_not_exists(PK)`条件へ変換する。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from jstock_advisor.domain.entities.holding import Holding


@dataclass(frozen=True)
class ConditionalPut:
    """新規追加または更新のPut計画。

    expected_data=None: 新規追加(TransactWriteItems側でattribute_not_exists(PK)を使う)。
    expected_data!=None: 既存アイテムの楽観ロック更新(#data = :expected_data)。
    """

    model: BaseModel
    id_field: str
    expected_data: str | None


@dataclass(frozen=True)
class ConditionalDelete:
    """既存アイテムの削除計画(#data = :expected_data を必須の楽観ロック条件とする)。"""

    id_value: str
    id_field: str
    expected_data: str


@dataclass(frozen=True)
class PurchaseWritePlan:
    """PortfolioService.build_purchase_write_plan()の戻り値。"""

    lot_put: ConditionalPut
    holding_put: ConditionalPut
    resulting_holding: Holding


@dataclass(frozen=True)
class SaleWritePlan:
    """PortfolioService.build_sale_write_plan()の戻り値。

    全部売却時はholding_put=None・holding_delete=(既存Holdingがあれば設定)、
    一部売却時はholding_put=(再計算後Holding)・holding_delete=Noneとなる。
    """

    lot_deletes: list[ConditionalDelete]
    lot_puts: list[ConditionalPut]
    holding_put: ConditionalPut | None
    holding_delete: ConditionalDelete | None
    resulting_holding: Holding | None


@dataclass(frozen=True)
class HoldingReplacementPlan:
    """保有(Holding)とその全ロット(PurchaseLot)を**原子的に置き換える/削除する**
    計画(Issue #61 Phase B2)。

    overwrite取込と保有削除の双方で使う。
      overwrite : lot_deletes=既存全ロット / holding_delete=既存Holding /
                  lot_put=新ロット / holding_put=新Holding
      削除のみ  : lot_deletes=既存全ロット / holding_delete=既存Holding /
                  lot_put=None / holding_put=None

    `SaleWritePlan`と異なり、**部分的な残存ロットを持たない**(全削除→全置換)。
    途中状態(Holdingだけ旧値・ロット一部欠落・Holding無し/ロット有り・
    Holding有り/ロット無し)をコミット後に残さないことが本計画の契約である。
    """

    lot_deletes: list[ConditionalDelete]
    holding_delete: ConditionalDelete | None
    lot_put: ConditionalPut | None
    holding_put: ConditionalPut | None
    resulting_holding: Holding | None

    @property
    def write_item_count(self) -> int:
        """この計画が必要とする書き込み項目数(DynamoDB TransactWriteItems換算)。"""
        return (
            len(self.lot_deletes)
            + (1 if self.holding_delete is not None else 0)
            + (1 if self.lot_put is not None else 0)
            + (1 if self.holding_put is not None else 0)
        )
