import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import CorporateActionType
from jstock_advisor.interfaces.types import CorporateActionEvent
from jstock_advisor.providers.corporate_action.merged_impl import MergedCorporateActionProvider

_NOW = dt.datetime(2026, 7, 27, tzinfo=dt.UTC)
_SOURCE = DataSourceReference(provider="test", fetched_at=_NOW)


class _FixedProvider:
    def __init__(self, events: list[CorporateActionEvent]) -> None:
        self._events = events

    def get_corporate_actions(self, stock_code: str, since: dt.date) -> list[CorporateActionEvent]:
        return [e for e in self._events if e.stock_code == stock_code]


def _event(event_type: CorporateActionType, effective_date: dt.date, ratio: str | None) -> (
    CorporateActionEvent
):
    return CorporateActionEvent(
        stock_code="5401",
        event_type=event_type,
        announced_date=effective_date,
        effective_date=effective_date,
        ratio=Decimal(ratio) if ratio else None,
        source=_SOURCE,
    )


def test_merges_auto_and_manual_events() -> None:
    auto = _FixedProvider([_event(CorporateActionType.SPLIT, dt.date(2025, 10, 1), "5")])
    manual = _FixedProvider([_event(CorporateActionType.SPINOFF, dt.date(2026, 1, 1), None)])
    provider = MergedCorporateActionProvider(auto_provider=auto, manual_provider=manual)
    events = provider.get_corporate_actions("5401", dt.date(2025, 1, 1))
    assert {e.event_type for e in events} == {
        CorporateActionType.SPLIT,
        CorporateActionType.SPINOFF,
    }


def test_manual_registration_takes_precedence_on_duplicate() -> None:
    auto_event = _event(CorporateActionType.SPLIT, dt.date(2025, 10, 1), "5")
    manual_event = _event(CorporateActionType.SPLIT, dt.date(2025, 10, 1), "5").model_copy(
        update={"detail": "運用者確認済み"}
    )
    provider = MergedCorporateActionProvider(
        auto_provider=_FixedProvider([auto_event]),
        manual_provider=_FixedProvider([manual_event]),
    )
    events = provider.get_corporate_actions("5401", dt.date(2025, 1, 1))
    assert len(events) == 1
    assert events[0].detail == "運用者確認済み"
