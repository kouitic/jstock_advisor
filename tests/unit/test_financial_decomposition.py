import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.financial_decomposition import (
    has_guidance_revision_disclosure,
    is_fundamentally_driven,
)
from jstock_advisor.interfaces.types import CashflowDecomposition, Disclosure

_NOW = dt.datetime(2026, 7, 27, tzinfo=dt.UTC)
_SOURCE = DataSourceReference(provider="test", fetched_at=_NOW)


def _decomposition(**overrides: Decimal | None) -> CashflowDecomposition:
    base = {
        "stock_code": "5401",
        "period_end": dt.date(2026, 3, 31),
        "pretax_income": Decimal("10000"),
        "depreciation_amortization": Decimal("500"),
        "receivables_change": Decimal("0"),
        "inventory_change": Decimal("0"),
        "payables_change": Decimal("0"),
        "tax_paid": Decimal("-2000"),
        "one_time_items": Decimal("0"),
        "ma_related_items": Decimal("0"),
        "other_working_capital": Decimal("0"),
        "source": _SOURCE,
    }
    base.update(overrides)
    return CashflowDecomposition(**base)  # type: ignore[arg-type]


def test_none_decomposition_is_inconclusive() -> None:
    assert is_fundamentally_driven(None) is None


def test_missing_component_is_inconclusive() -> None:
    decomposition = _decomposition(receivables_change=None)
    assert is_fundamentally_driven(decomposition) is None


def test_small_working_capital_swing_is_fundamentally_driven() -> None:
    decomposition = _decomposition(
        receivables_change=Decimal("500"), inventory_change=Decimal("-200")
    )
    assert is_fundamentally_driven(decomposition) is True


def test_large_working_capital_swing_is_not_fundamentally_driven() -> None:
    # 運転資本要因(売上債権+棚卸資産+一過性)の合計が税引前利益を上回るケース
    decomposition = _decomposition(
        pretax_income=Decimal("1000"),
        receivables_change=Decimal("-3000"),
        one_time_items=Decimal("2000"),
    )
    assert is_fundamentally_driven(decomposition) is False


def test_ma_related_large_payment_is_not_fundamentally_driven() -> None:
    decomposition = _decomposition(
        pretax_income=Decimal("5000"), ma_related_items=Decimal("-8000")
    )
    assert is_fundamentally_driven(decomposition) is False


def test_guidance_revision_detected_by_category() -> None:
    disclosures = [
        Disclosure(
            stock_code="5401",
            published_at=_NOW,
            title="お知らせ",
            category="業績予想の修正",
            source=_SOURCE,
        )
    ]
    assert has_guidance_revision_disclosure(disclosures) is True


def test_guidance_revision_detected_by_title() -> None:
    disclosures = [
        Disclosure(
            stock_code="5401",
            published_at=_NOW,
            title="2026年3月期通期業績予想の下方修正に関するお知らせ",
            category=None,
            source=_SOURCE,
        )
    ]
    assert has_guidance_revision_disclosure(disclosures) is True


def test_no_guidance_revision_when_unrelated() -> None:
    disclosures = [
        Disclosure(
            stock_code="5401",
            published_at=_NOW,
            title="自己株式取得に関するお知らせ",
            category="その他",
            source=_SOURCE,
        )
    ]
    assert has_guidance_revision_disclosure(disclosures) is False


def test_empty_disclosures_returns_false() -> None:
    assert has_guidance_revision_disclosure([]) is False
