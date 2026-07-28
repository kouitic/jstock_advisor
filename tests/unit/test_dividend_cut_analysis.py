import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import CorporateActionType, DividendComparisonOutcome
from jstock_advisor.domain.signals.dividend_cut_analysis import classify_dividend_change
from jstock_advisor.interfaces.types import CorporateActionEvent
from jstock_advisor.services.corporate_action_service import CorporateActionService

_NOW = dt.datetime(2026, 7, 27, tzinfo=dt.UTC)
_SOURCE = DataSourceReference(provider="test", fetched_at=_NOW)


class _FixedProvider:
    def __init__(self, events: list[CorporateActionEvent]) -> None:
        self._events = events

    def get_corporate_actions(self, stock_code: str, since: dt.date) -> list[CorporateActionEvent]:
        return [e for e in self._events if e.stock_code == stock_code]


def _split_service(ratio: str, effective_date: dt.date) -> CorporateActionService:
    events = [
        CorporateActionEvent(
            stock_code="5401",
            event_type=CorporateActionType.SPLIT,
            announced_date=effective_date,
            effective_date=effective_date,
            ratio=Decimal(ratio),
            source=_SOURCE,
        )
    ]
    return CorporateActionService(_FixedProvider(events), now=_NOW)


def test_split_adjustment_only_not_a_cut() -> None:
    # 5401日本製鉄相当: 分割前基準32円 vs 分割後基準6.4円は、実質的に維持(32/5=6.4)
    service = _split_service("5", dt.date(2025, 10, 1))
    result = classify_dividend_change(
        stock_code="5401",
        source_dps_raw=Decimal("32"),
        source_date=dt.date(2024, 3, 1),
        source_period_label="2023年度",
        target_dps_raw=Decimal("6.4"),
        target_date=dt.date(2026, 7, 27),
        target_period_label="2025年度",
        is_forecast_comparison=False,
        source_ref=_SOURCE,
        corporate_action_service=service,
    )
    assert result.outcome == DividendComparisonOutcome.SPLIT_ADJUSTMENT_ONLY
    assert result.source_dividend_per_share == Decimal("6.4")
    assert result.target_dividend_per_share == Decimal("6.4")
    assert result.pre_adjustment_source_dps == Decimal("32")


def test_actual_dividend_cut_after_split_adjustment() -> None:
    # 分割調整後もなお減少している場合は、実質的な減配として検出する
    service = _split_service("5", dt.date(2025, 10, 1))
    result = classify_dividend_change(
        stock_code="5401",
        source_dps_raw=Decimal("32"),  # 分割前基準 -> 調整後6.4円
        source_date=dt.date(2024, 3, 1),
        source_period_label="2023年度",
        target_dps_raw=Decimal("5.0"),  # 分割後基準、既に調整済み
        target_date=dt.date(2026, 7, 27),
        target_period_label="2025年度",
        is_forecast_comparison=False,
        source_ref=_SOURCE,
        corporate_action_service=service,
    )
    assert result.outcome == DividendComparisonOutcome.ACTUAL_DIVIDEND_CUT
    assert result.cut_pct is not None
    assert result.cut_pct > 0


def test_forecast_vs_actual_is_labeled_forecast_not_confirmed() -> None:
    service = CorporateActionService(_FixedProvider([]), now=_NOW)
    result = classify_dividend_change(
        stock_code="5401",
        source_dps_raw=Decimal("24"),
        source_date=dt.date(2025, 3, 1),
        source_period_label="2025年度(実績)",
        target_dps_raw=Decimal("20"),
        target_date=dt.date(2026, 7, 27),
        target_period_label="2026年度(会社予想)",
        is_forecast_comparison=True,
        source_ref=_SOURCE,
        corporate_action_service=service,
    )
    assert result.outcome == DividendComparisonOutcome.FORECAST_DIVIDEND_CUT
    assert result.outcome != DividendComparisonOutcome.ACTUAL_DIVIDEND_CUT


def test_dividend_maintained_when_equal() -> None:
    service = CorporateActionService(_FixedProvider([]), now=_NOW)
    result = classify_dividend_change(
        stock_code="2914",
        source_dps_raw=Decimal("242"),
        source_date=dt.date(2025, 3, 1),
        source_period_label="2025年度",
        target_dps_raw=Decimal("242"),
        target_date=dt.date(2026, 7, 27),
        target_period_label="2026年度予想",
        is_forecast_comparison=True,
        source_ref=_SOURCE,
        corporate_action_service=service,
    )
    assert result.outcome == DividendComparisonOutcome.DIVIDEND_MAINTAINED


def test_dividend_increase() -> None:
    service = CorporateActionService(_FixedProvider([]), now=_NOW)
    result = classify_dividend_change(
        stock_code="2914",
        source_dps_raw=Decimal("200"),
        source_date=dt.date(2025, 3, 1),
        source_period_label="2025年度",
        target_dps_raw=Decimal("242"),
        target_date=dt.date(2026, 7, 27),
        target_period_label="2026年度予想",
        is_forecast_comparison=True,
        source_ref=_SOURCE,
        corporate_action_service=service,
    )
    assert result.outcome == DividendComparisonOutcome.DIVIDEND_INCREASE


def test_comparison_not_possible_when_missing_data() -> None:
    result = classify_dividend_change(
        stock_code="9999",
        source_dps_raw=None,
        source_date=None,
        source_period_label=None,
        target_dps_raw=Decimal("100"),
        target_date=dt.date(2026, 7, 27),
        target_period_label="2026年度予想",
        is_forecast_comparison=True,
        source_ref=_SOURCE,
    )
    assert result.outcome == DividendComparisonOutcome.COMPARISON_NOT_POSSIBLE


def test_works_without_corporate_action_service_when_no_split() -> None:
    result = classify_dividend_change(
        stock_code="2914",
        source_dps_raw=Decimal("242"),
        source_date=dt.date(2025, 3, 1),
        source_period_label="2025年度",
        target_dps_raw=Decimal("200"),
        target_date=dt.date(2026, 7, 27),
        target_period_label="2026年度予想",
        is_forecast_comparison=True,
        source_ref=_SOURCE,
        corporate_action_service=None,
    )
    assert result.outcome == DividendComparisonOutcome.FORECAST_DIVIDEND_CUT
