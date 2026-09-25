"""LINEボタン起点会話型UIのTransactWriteItems原子コミット(実装プランv2 3節・
追加条件1「楽観ロック必須化」)向けに、PortfolioService等が「一切の永続化を
行わず、書き込み計画のみを返す」ために使う共有データ構造。

`expected_data`は計画構築時点でDynamoDBから読み取った`data`属性の生JSON
文字列そのもの(モデルを`model_dump_json()`で再シリアライズした値ではない。
再シリアライズ結果はフィールド順序等の理由でバイト単位の一致が保証されない
ため、実際に保存されているバイト列と完全一致する値のみを条件に使う)。
新規追加アイテムは`expected_data=None`とし、呼び出し側(conversation_commit.py)
がTransactWriteItems構築時に`attribute_not_exists(PK)`条件へ変換する。

Issue #530: `apply_conditional_put()`/`apply_conditional_delete()`は、
TransactWriteItemsを使わない単一repositoryへの直接適用経路
(`PortfolioService.register_purchase()`/`sell_shares()`等、CLIやバッチ処理からの
直接呼び出し)向けに、同じ`ConditionalPut`/`ConditionalDelete`計画を
`CollectionStore`の`insert_if_absent()`/`replace_if_raw_matches()`/
`delete_if_raw_matches()`(既存のCAS primitive。Issue #17)で適用する。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

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

      overwrite : lot_deletes=既存ロット / lot_put=新ロット /
                  holding_put=新Holding(既存があれば楽観ロック付き置換)
                  **holding_delete は None**
      削除のみ  : lot_deletes=既存全ロット / holding_delete=既存Holding /
                  lot_put=None / holding_put=None

    **同一アイテムに対して複数のアクションを持ってはならない。**
    DynamoDBのTransactWriteItemsは、1トランザクション内で同一アイテムを対象と
    する複数アクションを許可しない(ValidationException)。そのためoverwriteでは
    Holdingを「Delete → Put」にせず、既存の生JSONを`expected_data`とする
    **1回のConditionalPutで置換**する(楽観ロックは維持される)。
    ロットについても、新しいロットIDが既存ロットに含まれる場合は削除対象から
    除外し、Putだけを行う。

    `SaleWritePlan`と異なり、部分的な残存ロットを持たない(全削除→全置換)。
    途中状態(Holdingだけ旧値・ロット一部欠落・Holding無し/ロット有り・
    Holding有り/ロット無し)をコミット後に残さないことが本計画の契約である。
    """

    lot_deletes: list[ConditionalDelete]
    holding_delete: ConditionalDelete | None
    lot_put: ConditionalPut | None
    holding_put: ConditionalPut | None
    resulting_holding: Holding | None

    def __post_init__(self) -> None:
        if self.holding_delete is not None and self.holding_put is not None:
            raise ValueError(
                "HoldingへのDeleteとPutを同時に持つ計画は作れません"
                "(DynamoDBは同一アイテムへの複数アクションを許可しません)。"
                "置換はexpected_data付きのConditionalPut 1件で表現してください。"
            )
        delete_lot_ids = {d.id_value for d in self.lot_deletes}
        if len(delete_lot_ids) != len(self.lot_deletes):
            raise ValueError("同一ロットに対する削除が重複しています")
        if self.lot_put is not None:
            put_lot_id = str(getattr(self.lot_put.model, self.lot_put.id_field))
            if put_lot_id in delete_lot_ids:
                raise ValueError(
                    f"ロット{put_lot_id}に対するDeleteとPutを同時に持つ計画は作れません"
                )

    @property
    def write_item_count(self) -> int:
        """この計画が必要とする書き込み項目数(DynamoDB TransactWriteItems換算)。

        overwrite(既存あり) : ロット削除N + 新ロットPut1 + HoldingPut1 = N + 2
        削除のみ            : ロット削除N + Holding削除1               = N + 1
        """
        return (
            len(self.lot_deletes)
            + (1 if self.holding_delete is not None else 0)
            + (1 if self.lot_put is not None else 0)
            + (1 if self.holding_put is not None else 0)
        )


class ConcurrentUpdateError(ValueError):
    """楽観ロック(CAS)条件が不成立のまま書き込みが拒否された(Issue #530)。

    `HoldingLotLimitExceededError`/`WatchlistFieldOwnershipError`と同じく
    `ValueError`を継承する(本コードベースの既存規約: 業務ルール違反は
    `ValueError`系。`cli/holdings.py`の`update-meta`/`delete-lot`コマンドは
    既に`except ValueError`で捕捉し利用者へ日本語メッセージを表示するため、
    `RuntimeError`のままだと生のtracebackが表示されてしまう)。

    別の処理が計画構築後にこのアイテムを変更・削除していたことを示す。
    呼び出し元(CLI等)は最新状態を読み直してから操作をやり直すかどうかを
    判断すること。本モジュールは自動リトライを行わない(#530 N7: bounded
    retryか明示失敗のいずれかとし、silent overwriteは禁止。無限retryを
    勝手に導入しない)。
    """

    def __init__(self, id_value: str) -> None:
        super().__init__(
            f"id={id_value}は別の処理によって更新または削除されています。"
            "最新の状態を確認のうえ、操作をやり直してください。"
        )
        self.id_value = id_value


class _CasCapableRepository(Protocol):
    """`apply_conditional_put`/`apply_conditional_delete`が要求する最小限の
    repository interface(`CollectionStore`のCAS primitiveをそのまま転送する
    repositoryであれば、DynamoDB実装・ローカルJSON実装のどちらでもよい)。
    """

    def insert_if_absent(self, item: Any) -> bool: ...
    def replace_if_raw_matches(self, item_id: str, expected_raw_data: str, item: Any) -> bool: ...
    def delete_if_raw_matches(self, item_id: str, expected_raw_data: str) -> bool: ...


def apply_conditional_put(repository: _CasCapableRepository, put: ConditionalPut) -> None:
    """`ConditionalPut`を、TransactWriteItemsを使わない単一repositoryのCASで
    適用する(`PortfolioService.register_purchase()`等、LINEボタン起点会話型UI
    以外の直接適用経路向け)。

    `expected_data=None`は新規追加(`insert_if_absent`)、それ以外は既存アイテムの
    楽観ロック更新(`replace_if_raw_matches`)として扱う
    (`conditional_put_transact_item()`と同じ判定基準)。条件不成立は
    `ConcurrentUpdateError`として明示的に失敗させ、自動リトライしない。
    """
    id_value = str(getattr(put.model, put.id_field))
    if put.expected_data is None:
        if not repository.insert_if_absent(put.model):
            raise ConcurrentUpdateError(id_value)
        return
    if not repository.replace_if_raw_matches(id_value, put.expected_data, put.model):
        raise ConcurrentUpdateError(id_value)


def apply_conditional_delete(repository: _CasCapableRepository, delete: ConditionalDelete) -> None:
    """`ConditionalDelete`を、単一repositoryの`delete_if_raw_matches`で適用する。"""
    if not repository.delete_if_raw_matches(delete.id_value, delete.expected_data):
        raise ConcurrentUpdateError(delete.id_value)
