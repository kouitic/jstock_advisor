import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.common import (
    BuyPriceLevels,
    PriceWithRationale,
    SellPriceLevels,
)
from jstock_advisor.domain.entities.enums import (
    AccountType,
    ConfidenceLevel,
    RecommendationType,
    SkipReason,
    TransactionType,
)
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.infrastructure.local_repository.transaction_repository import (
    SkippedRecommendationRepository,
    TransactionRepository,
)
from jstock_advisor.services.transaction_history_service import TransactionHistoryService

_NOW = dt.datetime(2026, 7, 24, 8, 0, tzinfo=dt.UTC)


def _buy_recommendation(recommendation_id: str = "rec-buy") -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code="2914",
        stock_name="日本たばこ産業",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            tentative=PriceWithRationale(price=Decimal("3500"), rationale="x"),
            standard=PriceWithRationale(price=Decimal("3359"), rationale="x"),
            aggressive=PriceWithRationale(price=Decimal("3172"), rationale="x"),
        ),
        price_at_recommendation=Decimal("4200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


def _profit_take_recommendation(recommendation_id: str = "rec-sell") -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id,
        stock_code="2914",
        stock_name="日本たばこ産業",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
        sell_prices=SellPriceLevels(
            partial_profit_start_price=PriceWithRationale(price=Decimal("4500"), rationale="x"),
            full_profit_consideration_price=PriceWithRationale(
                price=Decimal("5000"), rationale="x"
            ),
        ),
        price_at_recommendation=Decimal("4600"),
        average_purchase_price_at_recommendation=Decimal("3400"),
        shares_at_recommendation=100,
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
    )


@pytest.fixture
def service(tmp_path: Path) -> TransactionHistoryService:
    recommendation_repo = RecommendationRepository(store_dir=tmp_path)
    recommendation_repo.save(_buy_recommendation())
    recommendation_repo.save(_profit_take_recommendation())
    return TransactionHistoryService(
        transaction_repository=TransactionRepository(store_dir=tmp_path),
        skipped_repository=SkippedRecommendationRepository(store_dir=tmp_path),
        recommendation_repository=recommendation_repo,
    )


def test_record_buy_without_recommendation(service: TransactionHistoryService) -> None:
    tx = service.record_execution(
        owner=DEFAULT_OWNER,
        stock_code="2914",
        transaction_type=TransactionType.BUY,
        shares=100,
        execution_price=Decimal("3400"),
        execution_date=dt.date(2026, 7, 20),
        now=_NOW,
    )
    assert tx.followed_recommendation is False
    assert tx.price_diff_from_recommendation is None
    assert tx.recommendation_id is None


def test_record_buy_with_recommendation_computes_price_diff(
    service: TransactionHistoryService,
) -> None:
    tx = service.record_execution(
        owner=DEFAULT_OWNER,
        stock_code="2914",
        transaction_type=TransactionType.BUY,
        shares=100,
        execution_price=Decimal("3400"),
        execution_date=dt.date(2026, 7, 20),
        recommendation_id="rec-buy",
        account_type=AccountType.NISA,
        now=_NOW,
    )
    assert tx.followed_recommendation is True
    # 標準買い価格3359円との差
    assert tx.price_diff_from_recommendation == Decimal("41")


def test_record_partial_sell_uses_partial_profit_start_price(
    service: TransactionHistoryService,
) -> None:
    tx = service.record_execution(
        owner=DEFAULT_OWNER,
        stock_code="2914",
        transaction_type=TransactionType.PARTIAL_SELL,
        shares=50,
        execution_price=Decimal("4600"),
        execution_date=dt.date(2026, 7, 24),
        recommendation_id="rec-sell",
        now=_NOW,
    )
    # 一部利確開始価格4500円との差
    assert tx.price_diff_from_recommendation == Decimal("100")


def test_record_full_sell_uses_full_profit_consideration_price(
    service: TransactionHistoryService,
) -> None:
    tx = service.record_execution(
        owner=DEFAULT_OWNER,
        stock_code="2914",
        transaction_type=TransactionType.FULL_SELL,
        shares=100,
        execution_price=Decimal("4900"),
        execution_date=dt.date(2026, 7, 24),
        recommendation_id="rec-sell",
        now=_NOW,
    )
    # 全株利確検討価格5000円との差
    assert tx.price_diff_from_recommendation == Decimal("-100")


def test_record_sell_against_buy_recommendation_has_no_reference_price(
    service: TransactionHistoryService,
) -> None:
    # BUY型の推奨にはsell_pricesが無いため、売却の推奨価格差は算出できない
    tx = service.record_execution(
        owner=DEFAULT_OWNER,
        stock_code="2914",
        transaction_type=TransactionType.PARTIAL_SELL,
        shares=50,
        execution_price=Decimal("4600"),
        execution_date=dt.date(2026, 7, 24),
        recommendation_id="rec-buy",
        now=_NOW,
    )
    assert tx.price_diff_from_recommendation is None
    assert tx.followed_recommendation is True  # recommendation_idはあるので追従扱い


def test_record_execution_rejects_unknown_recommendation(
    service: TransactionHistoryService,
) -> None:
    with pytest.raises(ValueError, match="見つかりません"):
        service.record_execution(
            owner=DEFAULT_OWNER,
            stock_code="2914",
            transaction_type=TransactionType.BUY,
            shares=100,
            execution_price=Decimal("3400"),
            execution_date=dt.date(2026, 7, 20),
            recommendation_id="does-not-exist",
            now=_NOW,
        )


def test_record_execution_rejects_non_positive_shares(service: TransactionHistoryService) -> None:
    with pytest.raises(ValueError):
        service.record_execution(
            owner=DEFAULT_OWNER,
            stock_code="2914",
            transaction_type=TransactionType.BUY,
            shares=0,
            execution_price=Decimal("3400"),
            execution_date=dt.date(2026, 7, 20),
            now=_NOW,
        )


def test_record_skip(service: TransactionHistoryService) -> None:
    skipped = service.record_skip(
        "rec-buy", SkipReason.WAITED_FOR_EARNINGS, "決算前のため見送り", now=_NOW
    )
    assert skipped.skip_reason == SkipReason.WAITED_FOR_EARNINGS
    assert skipped.reason_detail == "決算前のため見送り"


def test_record_skip_rejects_unknown_recommendation(service: TransactionHistoryService) -> None:
    with pytest.raises(ValueError, match="見つかりません"):
        service.record_skip("does-not-exist", SkipReason.OTHER, now=_NOW)


def test_list_transactions_filters_by_stock(service: TransactionHistoryService) -> None:
    service.record_execution(
        owner=DEFAULT_OWNER,
        stock_code="2914",
        transaction_type=TransactionType.BUY,
        shares=100,
        execution_price=Decimal("3400"),
        execution_date=dt.date(2026, 7, 20),
        now=_NOW,
    )
    service.record_execution(
        owner=DEFAULT_OWNER,
        stock_code="8136",
        transaction_type=TransactionType.BUY,
        shares=100,
        execution_price=Decimal("3000"),
        execution_date=dt.date(2026, 7, 20),
        now=_NOW,
    )
    assert len(service.list_transactions("2914")) == 1
    assert len(service.list_transactions()) == 2
