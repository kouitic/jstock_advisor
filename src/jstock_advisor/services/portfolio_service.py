"""保有銘柄管理サービス(要求仕様3節 portfolio_service、4節・5節)。

PurchaseLotを正データとし、Holdingは平均購入単価等を再計算したキャッシュとして
upsertする(要求仕様5節: 平均購入単価 = 各ロットの購入金額合計 ÷ 保有株数合計)。
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.holding import Holding, PurchaseLot, summarize_lots
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
    PurchaseLotRepository,
)


class PortfolioService:
    def __init__(
        self,
        holding_repository: HoldingRepository | None = None,
        lot_repository: PurchaseLotRepository | None = None,
    ) -> None:
        self._holdings = holding_repository or HoldingRepository()
        self._lots = lot_repository or PurchaseLotRepository()

    def list_holdings(self) -> list[Holding]:
        return self._holdings.list_all()

    def get_holding(self, stock_code: str) -> Holding | None:
        return self._holdings.get(stock_code)

    def list_lots(self, stock_code: str) -> list[PurchaseLot]:
        return self._lots.list_by_stock(stock_code)

    def register_purchase(
        self,
        stock_code: str,
        stock_name: str | None,
        shares: int,
        purchase_price: Decimal,
        purchase_date: dt.date,
        account_type: AccountType,
        fee: Decimal = Decimal("0"),
        investment_purpose: str | None = None,
        sell_policy: str | None = None,
        profit_target_rate: float | None = None,
        memo: str | None = None,
    ) -> Holding:
        lot = PurchaseLot(
            lot_id=str(uuid.uuid4()),
            stock_code=stock_code,
            purchase_date=purchase_date,
            shares=shares,
            purchase_price=purchase_price,
            fee=fee,
            account_type=account_type,
        )
        self._lots.upsert(lot)
        return self._recompute_holding(
            stock_code,
            stock_name=stock_name,
            account_type=account_type,
            investment_purpose=investment_purpose,
            sell_policy=sell_policy,
            profit_target_rate=profit_target_rate,
            memo=memo,
        )

    def _recompute_holding(
        self,
        stock_code: str,
        *,
        stock_name: str | None = None,
        account_type: AccountType | None = None,
        investment_purpose: str | None = None,
        sell_policy: str | None = None,
        profit_target_rate: float | None = None,
        memo: str | None = None,
    ) -> Holding:
        lots = self._lots.list_by_stock(stock_code)
        if not lots:
            raise ValueError(f"銘柄コード{stock_code}の購入ロットがありません")

        total_shares, avg_price, total_amount, first_date, last_date = summarize_lots(lots)
        existing = self._holdings.get(stock_code)
        now = dt.datetime.now(dt.UTC)

        holding = Holding(
            stock_code=stock_code,
            stock_name=stock_name or (existing.stock_name if existing else stock_code),
            market_segment=existing.market_segment if existing else None,
            industry=existing.industry if existing else None,
            shares=total_shares,
            average_purchase_price=avg_price,
            total_purchase_amount=total_amount,
            first_purchase_date=first_date,
            last_purchase_date=last_date,
            account_type=account_type
            or (existing.account_type if existing else AccountType.GENERAL),
            investment_purpose=investment_purpose
            or (existing.investment_purpose if existing else None),
            sell_policy=sell_policy or (existing.sell_policy if existing else None),
            cumulative_dividend_received=(
                existing.cumulative_dividend_received if existing else Decimal("0")
            ),
            cumulative_benefit_value_received=(
                existing.cumulative_benefit_value_received if existing else Decimal("0")
            ),
            profit_target_price=existing.profit_target_price if existing else None,
            profit_target_rate=(
                profit_target_rate
                if profit_target_rate is not None
                else (existing.profit_target_rate if existing else None)
            ),
            memo=memo or (existing.memo if existing else None),
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        self._holdings.upsert(holding)
        return holding

    def update_holding_meta(self, stock_code: str, **fields: Any) -> Holding:
        """stock_name/market_segment/industry/investment_purpose/sell_policy/
        cumulative_dividend_received/cumulative_benefit_value_received/
        profit_target_price/profit_target_rate/memo 等、ロットから導出されない
        項目を更新する。"""
        existing = self._holdings.get(stock_code)
        if existing is None:
            raise ValueError(f"銘柄コード{stock_code}の保有銘柄が見つかりません")
        merged = {
            **existing.model_dump(mode="python"),
            **fields,
            "updated_at": dt.datetime.now(dt.UTC),
        }
        updated = Holding.model_validate(merged)
        self._holdings.upsert(updated)
        return updated

    def delete_lot(self, stock_code: str, lot_id: str) -> Holding | None:
        if not self._lots.delete(lot_id):
            raise ValueError(f"ロットID{lot_id}が見つかりません")
        remaining = self._lots.list_by_stock(stock_code)
        if not remaining:
            self._holdings.delete(stock_code)
            return None
        return self._recompute_holding(stock_code)

    def delete_holding(self, stock_code: str) -> bool:
        self._lots.delete_by_stock(stock_code)
        return self._holdings.delete(stock_code)
