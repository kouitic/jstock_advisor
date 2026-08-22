"""sell_signal/profit_takingが同一銘柄のstock_snapshotを共有できることの回帰テスト
(Lambdaタイムアウト対策: 銘柄あたりの実データ取得を1回に抑える要求仕様18節)。
"""

import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import AccountType
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.providers.corporate_action.mock_impl import MockCorporateActionProvider
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.dividend_data.mock_impl import MockDividendDataProvider
from jstock_advisor.providers.financial_data.mock_impl import MockFinancialDataProvider
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.shareholder_benefit.mock_impl import MockShareholderBenefitProvider
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.sell_signal_service import SellSignalService
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

_NOW = dt.datetime(2026, 7, 27, 7, 0, tzinfo=dt.UTC)
_CONFIG = load_config()


def _providers() -> ProviderBundle:
    return ProviderBundle(
        market_data=MockMarketDataProvider(now=_NOW),
        financial_data=MockFinancialDataProvider(now=_NOW),
        dividend_data=MockDividendDataProvider(now=_NOW),
        shareholder_benefit=MockShareholderBenefitProvider(now=_NOW),
        disclosure=MockDisclosureProvider(now=_NOW),
        corporate_action=MockCorporateActionProvider(),
    )


def _holding() -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, "2914"),
        stock_code="2914",
        stock_name="日本たばこ産業",
        shares=100,
        average_purchase_price=Decimal("4000"),
        total_purchase_amount=Decimal("400000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_profit_taking_service_skips_refetch_when_snapshot_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers = _providers()
    snapshot, error = build_stock_snapshot(providers, "2914", _NOW, _CONFIG)
    assert snapshot is not None
    assert error is None

    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("snapshotを渡した場合はbuild_stock_snapshotを呼ばないはず")

    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.build_stock_snapshot", _fail_if_called
    )

    service = ProfitTakingService(providers=providers, config=_CONFIG)
    outcome = service.analyze(_holding(), _NOW, snapshot=snapshot)

    assert outcome.data_error is None


def test_sell_signal_service_skips_refetch_when_snapshot_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers = _providers()
    snapshot, error = build_stock_snapshot(providers, "2914", _NOW, _CONFIG)
    assert snapshot is not None
    assert error is None

    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("snapshotを渡した場合はbuild_stock_snapshotを呼ばないはず")

    monkeypatch.setattr(
        "jstock_advisor.services.sell_signal_service.build_stock_snapshot", _fail_if_called
    )

    service = SellSignalService(providers=providers, config=_CONFIG)
    outcome = service.analyze(_holding(), _NOW, snapshot=snapshot)

    assert outcome.data_error is None


def test_profit_taking_service_still_fetches_when_snapshot_omitted() -> None:
    providers = _providers()
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding(), _NOW)

    assert outcome.data_error is None
