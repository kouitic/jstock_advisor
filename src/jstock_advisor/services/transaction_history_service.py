"""実際の売買記録サービス(要求仕様3節 transaction_history_service、27〜28節)。

システムの推奨とは独立に、ユーザーが実際に行った売買を記録する。保有銘柄の
管理(portfolio_service/jstock holdings)とは別の関心事であり、ここでは
「推奨の妥当性を事後検証できるようにする」ための履歴記録に徹する
(推奨に従ったかどうか、価格差はいくらだったか等)。
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from jstock_advisor.domain.entities.enums import AccountType, SkipReason, TransactionType
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.entities.transaction import SkippedRecommendation, Transaction
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.local_repository.transaction_repository import (
    SkippedRecommendationRepository,
    TransactionRepository,
)

_BUY_TYPES = (TransactionType.BUY, TransactionType.ADDITIONAL_BUY)
_SELL_TYPES = (TransactionType.PARTIAL_SELL, TransactionType.FULL_SELL)


def _reference_price(
    recommendation: Recommendation | None, transaction_type: TransactionType
) -> Decimal | None:
    if recommendation is None:
        return None
    if transaction_type in _BUY_TYPES:
        if recommendation.buy_prices is not None and recommendation.buy_prices.standard is not None:
            return recommendation.buy_prices.standard.price
        return None
    if transaction_type in _SELL_TYPES:
        sp = recommendation.sell_prices
        if sp is None:
            return None
        if transaction_type == TransactionType.PARTIAL_SELL:
            candidate = sp.partial_profit_start_price or sp.recommended_limit_price
        else:
            candidate = (
                sp.full_profit_consideration_price
                or sp.stop_review_price
                or sp.recommended_limit_price
            )
        return candidate.price if candidate is not None else None
    return None


class TransactionHistoryService:
    def __init__(
        self,
        transaction_repository: TransactionRepository | None = None,
        skipped_repository: SkippedRecommendationRepository | None = None,
        recommendation_repository: RecommendationRepository | None = None,
    ) -> None:
        self._transactions = transaction_repository or TransactionRepository()
        self._skipped = skipped_repository or SkippedRecommendationRepository()
        self._recommendations = recommendation_repository or RecommendationRepository()

    def record_execution(
        self,
        stock_code: str,
        transaction_type: TransactionType,
        shares: int,
        execution_price: Decimal,
        execution_date: dt.date,
        recommendation_id: str | None = None,
        fee: Decimal = Decimal("0"),
        tax: Decimal = Decimal("0"),
        account_type: AccountType | None = None,
        reason: str | None = None,
        memo: str | None = None,
        now: dt.datetime | None = None,
    ) -> Transaction:
        if shares <= 0:
            raise ValueError("shares must be positive")
        if execution_price <= 0:
            raise ValueError("execution_price must be positive")

        recommendation = self._recommendations.get(recommendation_id) if recommendation_id else None
        if recommendation_id and recommendation is None:
            raise ValueError(f"recommendation_id={recommendation_id} が見つかりません")

        reference_price = _reference_price(recommendation, transaction_type)
        price_diff = execution_price - reference_price if reference_price is not None else None

        transaction = Transaction(
            transaction_id=str(uuid.uuid4()),
            recommendation_id=recommendation_id,
            stock_code=stock_code,
            transaction_type=transaction_type,
            execution_date=execution_date,
            shares=shares,
            execution_price=execution_price,
            fee=fee,
            tax=tax,
            account_type=account_type,
            followed_recommendation=recommendation_id is not None,
            price_diff_from_recommendation=price_diff,
            reason=reason,
            memo=memo,
            created_at=now or dt.datetime.now(dt.UTC),
        )
        self._transactions.save(transaction)
        return transaction

    def record_skip(
        self,
        recommendation_id: str,
        skip_reason: SkipReason,
        reason_detail: str | None = None,
        now: dt.datetime | None = None,
    ) -> SkippedRecommendation:
        if self._recommendations.get(recommendation_id) is None:
            raise ValueError(f"recommendation_id={recommendation_id} が見つかりません")

        skipped = SkippedRecommendation(
            recommendation_id=recommendation_id,
            skip_reason=skip_reason,
            reason_detail=reason_detail,
            created_at=now or dt.datetime.now(dt.UTC),
        )
        self._skipped.save(skipped)
        return skipped

    def list_transactions(self, stock_code: str | None = None) -> list[Transaction]:
        if stock_code:
            return self._transactions.list_by_stock(stock_code)
        return self._transactions.list_all()
