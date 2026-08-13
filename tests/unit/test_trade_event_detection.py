import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.enums import AccountType, TransactionType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holdings_snapshot import HoldingsSnapshotEntry
from jstock_advisor.domain.signals.trade_event_detection import detect_trade_events

_TODAY = dt.date(2026, 8, 17)


_NOW = dt.datetime(2026, 8, 17, tzinfo=dt.UTC)


def _holding(stock_code: str, shares: int, avg_price: str = "1000") -> Holding:
    return Holding(
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        shares=shares,
        average_purchase_price=Decimal(avg_price),
        total_purchase_amount=Decimal(avg_price) * shares,
        first_purchase_date=_TODAY,
        last_purchase_date=_TODAY,
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _snapshot(stock_code: str, shares: int, active: bool = True) -> HoldingsSnapshotEntry:
    return HoldingsSnapshotEntry(
        stock_code=stock_code,
        shares=shares,
        average_purchase_price=Decimal("1000") if shares > 0 else None,
        recorded_at=_TODAY - dt.timedelta(days=1),
        active_holding=active,
    )


def test_new_purchase_detected_as_buy() -> None:
    events = detect_trade_events({}, {"1000": _holding("1000", 100)}, _TODAY)
    assert len(events) == 1
    assert events[0].event_type == TransactionType.BUY
    assert events[0].stock_code == "1000"
    assert events[0].shares == 100


def test_increase_detected_as_additional_buy() -> None:
    previous = {"1000": _snapshot("1000", 100)}
    events = detect_trade_events(previous, {"1000": _holding("1000", 200)}, _TODAY)
    assert len(events) == 1
    assert events[0].event_type == TransactionType.ADDITIONAL_BUY


def test_decrease_detected_as_partial_sell() -> None:
    previous = {"1000": _snapshot("1000", 100)}
    events = detect_trade_events(previous, {"1000": _holding("1000", 40)}, _TODAY)
    assert len(events) == 1
    assert events[0].event_type == TransactionType.PARTIAL_SELL


def test_disappearance_detected_as_full_sell() -> None:
    previous = {"1000": _snapshot("1000", 100)}
    events = detect_trade_events(previous, {}, _TODAY)
    assert len(events) == 1
    assert events[0].event_type == TransactionType.FULL_SELL
    assert events[0].shares == 0


def test_no_change_produces_no_event() -> None:
    previous = {"1000": _snapshot("1000", 100)}
    events = detect_trade_events(previous, {"1000": _holding("1000", 100)}, _TODAY)
    assert events == []


def test_repurchase_after_tombstone_detected_as_buy() -> None:
    """全部売却(tombstone: shares=0, active_holding=False)後の再購入は
    新規BUYとして正しく検知される(§5-3のtombstone保持の目的)。"""
    previous = {"1000": _snapshot("1000", 0, active=False)}
    events = detect_trade_events(previous, {"1000": _holding("1000", 50)}, _TODAY)
    assert len(events) == 1
    assert events[0].event_type == TransactionType.BUY
