"""shareholder_benefit_provider インターフェース。

MVPでは自動取得を行わず、ユーザーが手動/CSVで登録したデータを返す実装のみを提供する
(未確定事項#5の決定に基づく)。将来的に許諾を得たデータ提供元と接続する場合も、
このインターフェースを実装するだけで済むようにする。
"""

from __future__ import annotations

from typing import Protocol

from jstock_advisor.interfaces.types import ShareholderBenefit


class ShareholderBenefitProvider(Protocol):
    def get_shareholder_benefit(self, stock_code: str) -> ShareholderBenefit | None:
        """株主優待情報を取得する。登録が無ければNone。"""
        ...
