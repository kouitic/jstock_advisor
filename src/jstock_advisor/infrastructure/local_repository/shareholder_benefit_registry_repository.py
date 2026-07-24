"""株主優待の手動登録データのローカルリポジトリ(要求仕様7節、未確定事項#5)。

株主優待は自動取得できる公式データ源が無いため、ユーザーが手動/CSVで登録した
内容をそのまま保存する。銘柄コードを主キーとし、1銘柄1レコード(優待内容は
ShareholderBenefit.benefits配下に複数段階を保持できる)。
"""

from __future__ import annotations

from pathlib import Path

from jstock_advisor.infrastructure.collection_store import CollectionStore, build_collection_store
from jstock_advisor.interfaces.types import ShareholderBenefit


class ShareholderBenefitRegistryRepository:
    def __init__(self, store_dir: Path | None = None) -> None:
        self._store: CollectionStore[ShareholderBenefit] = build_collection_store(
            ShareholderBenefit, "shareholder_benefits.json", "stock_code", store_dir
        )

    def list_all(self) -> list[ShareholderBenefit]:
        return self._store.list_all()

    def get(self, stock_code: str) -> ShareholderBenefit | None:
        return self._store.get(stock_code)

    def save(self, benefit: ShareholderBenefit) -> None:
        self._store.upsert(benefit)

    def delete(self, stock_code: str) -> bool:
        return self._store.delete(stock_code)
