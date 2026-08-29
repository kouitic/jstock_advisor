"""disclosure_provider の未実装プレースホルダー。

適時開示(TDnet)・決算発表予定日の実データ提供元は本フェーズでは未実装。
モック実装のような架空データは返さず、常に「データ無し」を返す
(実データと架空データが混在しないようにするため)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.interfaces.disclosure import (
    DisclosureQueryResult,
    DisclosureUnavailableReason,
)


class UnavailableDisclosureProvider:
    def get_disclosures(self, stock_code: str, since: dt.date) -> DisclosureQueryResult:
        """実データ源が未接続のためUNAVAILABLEを返す(Issue #53 Phase B2)。

        「providerが使えない」と「調査したが開示0件」は別の事実であり、
        AVAILABLE + 空リストで代用しない。テスト等で「開示なし」を表現したい
        場合はMockDisclosureProviderを使うこと。"""
        return DisclosureQueryResult.unavailable(DisclosureUnavailableReason.NOT_CONFIGURED)

    def get_next_earnings_date(self, stock_code: str) -> dt.date | None:
        return None
