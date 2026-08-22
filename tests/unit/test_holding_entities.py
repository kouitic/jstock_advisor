import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
from jstock_advisor.domain.entities.enums import AccountType, ConfidenceLevel, RecommendationType
from jstock_advisor.domain.entities.holding import PurchaseLot, summarize_lots
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.entities.recommendation import Recommendation


def _lot(shares: int, price: str, date: dt.date, lot_id: str = "lot") -> PurchaseLot:
    return PurchaseLot(
        lot_id=lot_id,
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, "8136"),
        stock_code="8136",
        purchase_date=date,
        shares=shares,
        purchase_price=Decimal(price),
        account_type=AccountType.NISA,
    )


def test_summarize_lots_computes_weighted_average_price() -> None:
    lots = [
        _lot(100, "3775", dt.date(2025, 4, 1), "lot-1"),
        _lot(100, "4025", dt.date(2025, 9, 1), "lot-2"),
    ]
    total_shares, avg_price, total_amount, first_date, last_date = summarize_lots(lots)
    assert total_shares == 200
    assert avg_price == Decimal("3900")
    assert total_amount == Decimal("780000")
    assert first_date == dt.date(2025, 4, 1)
    assert last_date == dt.date(2025, 9, 1)


def test_summarize_lots_with_uneven_shares() -> None:
    lots = [
        _lot(100, "1000", dt.date(2025, 1, 1), "lot-1"),
        _lot(300, "1200", dt.date(2025, 2, 1), "lot-2"),
    ]
    total_shares, avg_price, _total_amount, _first, _last = summarize_lots(lots)
    assert total_shares == 400
    # (100*1000 + 300*1200) / 400 = 1150
    assert avg_price == Decimal("1150")


def test_summarize_lots_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        summarize_lots([])


def test_recommendation_is_immutable() -> None:
    rec = Recommendation(
        recommendation_id="rec-1",
        stock_code="8136",
        stock_name="サンリオ",
        recommended_at=dt.datetime.now(dt.UTC),
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(
            tentative=PriceWithRationale(price=Decimal("3900"), rationale="適正価格の95%")
        ),
        price_at_recommendation=Decimal("4100"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1",
    )
    with pytest.raises(ValidationError):
        rec.total_score = 80  # type: ignore[misc]


def test_purchase_lot_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        PurchaseLot.model_validate(
            {
                "lot_id": "lot-1",
                "owner": DEFAULT_OWNER,
                "holding_id": build_holding_id(DEFAULT_OWNER, "8136"),
                "stock_code": "8136",
                "purchase_date": "2025-04-01",
                "shares": 100,
                "purchase_price": "3775",
                "account_type": "NISA",
                "unexpected_field": "x",
            }
        )
