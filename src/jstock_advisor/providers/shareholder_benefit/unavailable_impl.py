"""shareholder_benefit_provider の未実装プレースホルダー。

株主優待は自動取得できる公式データ源が無いため、MVPでは手動/CSV登録を前提とする
(未確定事項#5の決定)。ユーザー登録データに基づくローカルレジストリ実装が
提供されるまでの間、実データ用のProviderBundleではこのプレースホルダーを使う。
モック実装(mock_impl.py)のような架空データは返さず、常に「データ無し」を返す
(実データと架空データが混在しないようにするため)。
"""

from __future__ import annotations

from jstock_advisor.interfaces.types import ShareholderBenefit


class UnavailableShareholderBenefitProvider:
    def get_shareholder_benefit(self, stock_code: str) -> ShareholderBenefit | None:
        return None
