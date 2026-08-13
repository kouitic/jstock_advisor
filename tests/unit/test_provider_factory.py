"""provider_factory.pyの配線確認テスト(配当クロスバリデーション コードレビュー
修正2: YFinanceDividendDataProviderのcorporate_action_service必須化に伴う回帰確認)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.config.loader import load_config
from jstock_advisor.providers.dividend_data.cross_validating_impl import (
    CrossValidatingDividendDataProvider,
)
from jstock_advisor.providers.dividend_data.yfinance_impl import YFinanceDividendDataProvider
from jstock_advisor.services.provider_factory import build_real_provider_bundle

_NOW = dt.datetime(2026, 8, 13, tzinfo=dt.UTC)


def test_real_bundle_wires_corporate_action_service_into_primary_dividend_provider() -> None:
    """build_real_provider_bundle()が構築するYFinanceDividendDataProvider(主データ源)へ、
    CorporateActionServiceが必ず注入されていることを確認する。未注入の場合、
    YFinanceDividendDataProviderのコンストラクタが必須引数不足でTypeErrorになるため、
    この確認自体はbuild_real_provider_bundle呼び出しの成功のみで既に担保されるが、
    正しいprimary providerへ配線されていることも明示的に確認する。"""
    config = load_config()

    bundle = build_real_provider_bundle(_NOW, config)

    assert isinstance(bundle.dividend_data, CrossValidatingDividendDataProvider)
    primary = bundle.dividend_data._primary  # noqa: SLF001
    assert isinstance(primary, YFinanceDividendDataProvider)
    assert primary._corporate_action is not None  # noqa: SLF001
