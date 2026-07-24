"""shareholder_benefit_provider のローカルレジストリ実装(未確定事項#5)。

株主優待は自動取得できる公式データ源が無いため、ユーザーが
`jstock shareholder-benefit` CLI(またはCSV一括登録)で登録した内容を
そのまま返す。未登録銘柄はNone(優待無しではなく「データ無し」)。
"""

from __future__ import annotations

from jstock_advisor.infrastructure.local_repository.shareholder_benefit_registry_repository import (
    ShareholderBenefitRegistryRepository,
)
from jstock_advisor.interfaces.types import ShareholderBenefit


class LocalRegistryShareholderBenefitProvider:
    def __init__(self, repository: ShareholderBenefitRegistryRepository | None = None) -> None:
        self._repo = repository or ShareholderBenefitRegistryRepository()

    def get_shareholder_benefit(self, stock_code: str) -> ShareholderBenefit | None:
        return self._repo.get(stock_code)
