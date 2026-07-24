import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.interfaces.types import CorporateActionEvent, DividendInfo
from jstock_advisor.providers.dividend_data.cross_validating_impl import (
    CrossValidatingDividendDataProvider,
)

_NOW = dt.datetime(2026, 7, 24, 7, 0, tzinfo=dt.UTC)
_SOURCE_A = DataSourceReference(provider="yfinance", fetched_at=_NOW)
_SOURCE_B = DataSourceReference(provider="edinet", fetched_at=_NOW)
_CONFIG = load_config().data_validation


class _FixedDividendProvider:
    def __init__(self, info: DividendInfo | None) -> None:
        self._info = info

    def get_dividend_info(self, stock_code: str) -> DividendInfo | None:
        return self._info


class _FixedCorporateActionProvider:
    def __init__(self, events: list[CorporateActionEvent]) -> None:
        self._events = events

    def get_corporate_actions(self, stock_code: str, since: dt.date) -> list[CorporateActionEvent]:
        return self._events


def _dividend_info(actual: str, source: DataSourceReference = _SOURCE_A) -> DividendInfo:
    return DividendInfo(
        stock_code="8136",
        fiscal_year="2026",
        actual_annual_dividend_per_share=Decimal(actual),
        source=source,
    )


def test_returns_none_when_primary_missing() -> None:
    provider = CrossValidatingDividendDataProvider(
        primary=_FixedDividendProvider(None),
        secondary=_FixedDividendProvider(_dividend_info("100")),
        corporate_action_provider=_FixedCorporateActionProvider([]),
        config=_CONFIG,
        now=_NOW,
    )
    assert provider.get_dividend_info("8136") is None


def test_returns_primary_when_secondary_missing() -> None:
    primary_info = _dividend_info("16")
    provider = CrossValidatingDividendDataProvider(
        primary=_FixedDividendProvider(primary_info),
        secondary=_FixedDividendProvider(None),
        corporate_action_provider=_FixedCorporateActionProvider([]),
        config=_CONFIG,
        now=_NOW,
    )
    result = provider.get_dividend_info("8136")
    assert result is primary_info


def test_returns_primary_when_within_threshold() -> None:
    primary_info = _dividend_info("100")
    provider = CrossValidatingDividendDataProvider(
        primary=_FixedDividendProvider(primary_info),
        secondary=_FixedDividendProvider(_dividend_info("103", _SOURCE_B)),
        corporate_action_provider=_FixedCorporateActionProvider([]),
        config=_CONFIG,
        now=_NOW,
    )
    result = provider.get_dividend_info("8136")
    assert result is primary_info


def test_reconciles_discrepancy_via_stock_split() -> None:
    # yfinance(分割調整済み)=16, EDINET(額面ベース)=69 のサンリオの実例を模した回帰テスト
    primary_info = _dividend_info("16")
    split_event = CorporateActionEvent(
        stock_code="8136",
        event_type="SPLIT",
        announced_date=dt.date(2026, 3, 30),
        ratio=Decimal("5"),
        source=_SOURCE_A,
    )
    provider = CrossValidatingDividendDataProvider(
        primary=_FixedDividendProvider(primary_info),
        secondary=_FixedDividendProvider(_dividend_info("69", _SOURCE_B)),
        corporate_action_provider=_FixedCorporateActionProvider([split_event]),
        config=_CONFIG,
        now=_NOW,
    )
    result = provider.get_dividend_info("8136")
    assert result is primary_info


def test_excludes_when_discrepancy_unresolvable() -> None:
    primary_info = _dividend_info("16")
    provider = CrossValidatingDividendDataProvider(
        primary=_FixedDividendProvider(primary_info),
        secondary=_FixedDividendProvider(_dividend_info("50", _SOURCE_B)),
        corporate_action_provider=_FixedCorporateActionProvider([]),  # 説明可能な分割なし
        config=_CONFIG,
        now=_NOW,
    )
    result = provider.get_dividend_info("8136")
    assert result is None


def test_unrelated_split_does_not_falsely_reconcile() -> None:
    # 分割はあるが、その比率では説明できない乖離は除外されるべき
    primary_info = _dividend_info("16")
    split_event = CorporateActionEvent(
        stock_code="8136",
        event_type="SPLIT",
        announced_date=dt.date(2024, 1, 1),
        ratio=Decimal("2"),
        source=_SOURCE_A,
    )
    provider = CrossValidatingDividendDataProvider(
        primary=_FixedDividendProvider(primary_info),
        secondary=_FixedDividendProvider(_dividend_info("69", _SOURCE_B)),
        corporate_action_provider=_FixedCorporateActionProvider([split_event]),
        config=_CONFIG,
        now=_NOW,
    )
    result = provider.get_dividend_info("8136")
    assert result is None
