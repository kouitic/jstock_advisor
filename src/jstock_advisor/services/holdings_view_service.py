"""保有銘柄一覧表示(LINE UI第二弾、読み取り専用、2026-08)。

Holding/PurchaseLot等の正データを一切書き換えない読み取り専用サービス。
owner一覧は既存Holdingから動的に導出し、コードへハードコードしない
(将来の所有者増減へコード変更なしで追従するため)。
"""

from __future__ import annotations

from jstock_advisor.infrastructure.local_repository.holding_repository import HoldingRepository
from jstock_advisor.services.watchlist_display_name import StockDisplayNameResolver


class HoldingsViewService:
    def __init__(
        self,
        holding_repository: HoldingRepository | None = None,
        display_name_resolver: StockDisplayNameResolver | None = None,
    ) -> None:
        self._holdings = holding_repository or HoldingRepository()
        self._display_name_resolver = display_name_resolver

    def list_owners(self) -> list[str]:
        """現在保有銘柄に登録されている所有者一覧(重複除去済み、安定順)。"""
        return self._holdings.list_distinct_owners()

    def build_owner_holdings_lines(self, owner: str) -> list[str]:
        """指定ownerの保有銘柄を1行1銘柄、stock_code昇順で整形する。

        表示項目: 社名（銘柄コード）｜保有株数｜平均取得単価。
        """
        holdings = self._holdings.list_by_owner(owner)
        lines: list[str] = []
        for holding in holdings:
            display_name = (
                self._display_name_resolver.resolve(
                    holding.stock_code, fallback_name=holding.stock_name
                )
                if self._display_name_resolver is not None
                else holding.stock_name or holding.stock_code
            )
            lines.append(
                f"{display_name}（{holding.stock_code}）｜"
                f"{holding.shares:,}株｜平均{holding.average_purchase_price:,.0f}円"
            )
        return lines
