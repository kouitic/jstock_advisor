"""株主還元方針レジストリによるDividendInfoのenrichment(Issue #30 Phase 1)。

外部データ取得(yfinance/EDINET)と「人間確認済み方針データ」の責務を分離する:
内側のProvider(通常はCrossValidatingDividendDataProvider)がDividendInfoを
返した後に、手動レジストリ(config/shareholder_return_policies.yaml)の方針
情報をdecorateする。

- レジストリに登録あり(CONFIRMED): is_progressive_or_doe_policyを
  policy_typeから導出(PROGRESSIVE/DOE/BOTH -> True、NONE -> False)し、
  監査用スナップショット(type/source/checked_at)を付与する
- レジストリに登録なし: 内側Providerの値(None=UNKNOWN)のまま返す

このモジュール以外がis_progressive_or_doe_policyをTrueへ設定してはならない
(過去実績・キーワードヒット・LLM出力からの推測は禁止。レジストリが唯一の正本)。
"""

from __future__ import annotations

from jstock_advisor.config.models import ShareholderReturnPoliciesConfig
from jstock_advisor.domain.entities.enums import ShareholderReturnPolicyType
from jstock_advisor.interfaces.dividend_data import DividendDataProvider
from jstock_advisor.interfaces.types import DividendInfo


class ShareholderReturnPolicyEnrichingDividendDataProvider:
    def __init__(
        self, inner: DividendDataProvider, policies: ShareholderReturnPoliciesConfig
    ) -> None:
        self._inner = inner
        self._policies = policies

    def get_dividend_info(
        self, stock_code: str, fiscal_year_end_month: int | None = None
    ) -> DividendInfo | None:
        info = self._inner.get_dividend_info(stock_code, fiscal_year_end_month)
        if info is None:
            return None
        entry = self._policies.entry_for(stock_code)
        if entry is None:
            return info
        return info.model_copy(
            update={
                "is_progressive_or_doe_policy": (
                    entry.policy_type != ShareholderReturnPolicyType.NONE
                ),
                "shareholder_return_policy_type": entry.policy_type,
                "shareholder_return_policy_source_type": entry.source_type,
                "shareholder_return_policy_source_reference": entry.source_reference,
                "shareholder_return_policy_checked_at": entry.checked_at,
            }
        )
