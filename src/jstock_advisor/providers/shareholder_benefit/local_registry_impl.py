"""shareholder_benefit_provider のローカルレジストリ実装(未確定事項#5)。

株主優待は自動取得できる公式データ源が無いため、ユーザーが
`jstock shareholder-benefit` CLI(またはCSV一括登録)で登録した内容を
そのまま返す。未登録銘柄はNone(優待無しではなく「データ無し」)。

`next_benefit_record_date`だけは、保存済みの派生値をそのまま返さず、現行の
計算契約(`with_refreshed_next_record_date`)に従って読み取り時に再導出する
(Issue #120)。サービス層の読み取りAPIと同じ値になるようにするためであり、
**この再導出は純粋な計算で、永続化は一切行わない**。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.valuation.shareholder_benefit_matching import (
    with_refreshed_next_record_date,
)
from jstock_advisor.infrastructure.local_repository.shareholder_benefit_registry_repository import (
    ShareholderBenefitRegistryRepository,
)
from jstock_advisor.interfaces.types import ShareholderBenefit


class LocalRegistryShareholderBenefitProvider:
    def __init__(
        self,
        repository: ShareholderBenefitRegistryRepository | None = None,
        now: dt.datetime | None = None,
    ) -> None:
        self._repo = repository or ShareholderBenefitRegistryRepository()
        self._now = now

    def get_shareholder_benefit(self, stock_code: str) -> ShareholderBenefit | None:
        benefit = self._repo.get(stock_code)
        if benefit is None:
            return None
        reference = (self._now or dt.datetime.now(dt.UTC)).date()
        return with_refreshed_next_record_date(benefit, reference)
