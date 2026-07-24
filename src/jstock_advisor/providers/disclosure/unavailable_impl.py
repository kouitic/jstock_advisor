"""disclosure_provider の未実装プレースホルダー。

適時開示(TDnet)・決算発表予定日の実データ提供元は本フェーズでは未実装。
モック実装のような架空データは返さず、常に「データ無し」を返す
(実データと架空データが混在しないようにするため)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.interfaces.types import Disclosure


class UnavailableDisclosureProvider:
    def get_disclosures(self, stock_code: str, since: dt.date) -> list[Disclosure]:
        return []

    def get_next_earnings_date(self, stock_code: str) -> dt.date | None:
        return None
