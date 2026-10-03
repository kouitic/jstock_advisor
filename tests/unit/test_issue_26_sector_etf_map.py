"""Issue #26: `config/momentum_rules.yaml` の `sector_etf_map` を TOPIX-17 へ mapping する。

NEXT FUNDS TOPIX-17 シリーズ(17 本)の ETF を、業種ごとの相対強度の比較対象にする。

## 何を固定するか

- mapping の中身そのもの(literal の完全一致)。キーは yfinance の industry 文字列、
  値は ETF の ticker(`<コード>.T`)
- 載せない業種(NOT_APPLICABLE のまま)が map に入っていないこと
  (「100% mapping は強制しない」条件)
- map を通した実際の経路: 登録済み業種 → ETF の ticker で benchmark の履歴を取得して
  sector 環境を評価 / 未登録業種 → 取得せず NOT_APPLICABLE
- **SHADOW_ONLY**: map の有無で判定の入力が変わらない
  (sector 環境と、それに依存する記録用の値だけが変わる)

## 実ネットワークを使わない(MANAGER 判断)

CI の test は実ネットワークへ出ない。provider の契約は fake の yfinance で、
pipeline は mock provider をラップした spy で検証する。実 provider での取得可否の確認は、
実装時に 1 回限り・repository の外で行った(PR 本文に事実のみ記載)。

## この mapping の限界(先に明記)

キーは yfinance(Yahoo)の industry 文字列で、JPX が公開する
「東証 33 業種 → TOPIX-17」の公式対応表とは別の体系である。したがって各業種 → 区分の対応は
公式の対応表との照合ではなく、通常どの区分の企業群かの事実確認に基づく。
判断に迷う業種は載せない(fail-closed)。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import (
    RecommendationType,
    SectorEnvironmentEvaluationState,
)
from jstock_advisor.providers.market_data.yfinance_impl import YFinanceMarketDataProvider
from jstock_advisor.services.buy_signal_service import BuySignalService
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CFG = load_config()
_CALENDAR = BusinessCalendar.from_config(_CFG.holiday_calendar)
_STOCK_CODE = "2914"
_NOW = dt.datetime(2026, 8, 6, tzinfo=dt.UTC)

# NEXT FUNDS TOPIX-17 シリーズ 17 本(運用会社の各銘柄ページで 2026-10-03 に確認。コード → 区分名)
_TOPIX17_ETFS: dict[str, str] = {
    "1617.T": "食品",
    "1618.T": "エネルギー資源",
    "1619.T": "建設・資材",
    "1620.T": "素材・化学",
    "1621.T": "医薬品",
    "1622.T": "自動車・輸送機",
    "1623.T": "鉄鋼・非鉄",
    "1624.T": "機械",
    "1625.T": "電機・精密",
    "1626.T": "情報通信・サービスその他",
    "1627.T": "電力・ガス",
    "1628.T": "運輸・物流",
    "1629.T": "商社・卸売",
    "1630.T": "小売",
    "1631.T": "銀行",
    "1632.T": "金融(除く銀行)",
    "1633.T": "不動産",
}

# 期待する mapping(literal)。変更するときは、この表と PR 本文の照合材料を同時に更新する
_EXPECTED_MAPPING: dict[str, str] = {
    # 1617 食品
    "Packaged Foods": "1617.T",
    "Confectioners": "1617.T",
    "Beverages-Non-Alcoholic": "1617.T",
    "Beverages-Brewers": "1617.T",
    "Beverages-Wineries & Distilleries": "1617.T",
    "Farm Products": "1617.T",
    # 1618 エネルギー資源
    "Oil & Gas Refining & Marketing": "1618.T",
    "Oil & Gas E&P": "1618.T",
    "Oil & Gas Integrated": "1618.T",
    "Oil & Gas Equipment & Services": "1618.T",
    "Thermal Coal": "1618.T",
    "Coking Coal": "1618.T",
    # 1619 建設・資材
    "Engineering & Construction": "1619.T",
    "Building Products & Equipment": "1619.T",
    "Residential Construction": "1619.T",
    "Building Materials": "1619.T",
    # 1620 素材・化学
    "Specialty Chemicals": "1620.T",
    "Chemicals": "1620.T",
    "Textile Manufacturing": "1620.T",
    "Paper & Paper Products": "1620.T",
    "Packaging & Containers": "1620.T",
    "Agricultural Inputs": "1620.T",
    # 1621 医薬品
    "Drug Manufacturers - Specialty & Generic": "1621.T",
    "Drug Manufacturers - General": "1621.T",
    "Biotechnology": "1621.T",
    # 1622 自動車・輸送機
    "Auto Parts": "1622.T",
    "Auto Manufacturers": "1622.T",
    # 1623 鉄鋼・非鉄
    "Steel": "1623.T",
    "Aluminum": "1623.T",
    "Copper": "1623.T",
    "Other Precious Metals & Mining": "1623.T",
    "Other Industrial Metals & Mining": "1623.T",
    # 1624 機械
    "Specialty Industrial Machinery": "1624.T",
    "Farm & Heavy Construction Machinery": "1624.T",
    "Tools & Accessories": "1624.T",
    "Metal Fabrication": "1624.T",
    # 1625 電機・精密
    "Electronic Components": "1625.T",
    "Semiconductor Equipment & Materials": "1625.T",
    "Semiconductors": "1625.T",
    "Scientific & Technical Instruments": "1625.T",
    "Medical Instruments & Supplies": "1625.T",
    "Medical Devices": "1625.T",
    "Consumer Electronics": "1625.T",
    "Computer Hardware": "1625.T",
    "Communication Equipment": "1625.T",
    "Electrical Equipment & Parts": "1625.T",
    # 1626 情報通信・サービスその他
    "Information Technology Services": "1626.T",
    "Software - Application": "1626.T",
    "Software - Infrastructure": "1626.T",
    "Internet Content & Information": "1626.T",
    "Electronic Gaming & Multimedia": "1626.T",
    "Telecom Services": "1626.T",
    "Health Information Services": "1626.T",
    "Consulting Services": "1626.T",
    "Specialty Business Services": "1626.T",
    "Business Equipment & Supplies": "1626.T",
    "Staffing & Employment Services": "1626.T",
    "Advertising Agencies": "1626.T",
    "Education & Training Services": "1626.T",
    "Broadcasting": "1626.T",
    "Entertainment": "1626.T",
    "Publishing": "1626.T",
    "Security & Protection Services": "1626.T",
    "Personal Services": "1626.T",
    "Leisure": "1626.T",
    # 1627 電力・ガス
    "Utilities - Renewable": "1627.T",
    "Utilities - Regulated Gas": "1627.T",
    "Utilities - Regulated Electric": "1627.T",
    "Utilities - Independent Power Producers": "1627.T",
    "Utilities - Diversified": "1627.T",
    # 1628 運輸・物流
    "Integrated Freight & Logistics": "1628.T",
    "Railroads": "1628.T",
    "Trucking": "1628.T",
    "Marine Shipping": "1628.T",
    "Airlines": "1628.T",
    "Airports & Air Services": "1628.T",
    # 1629 商社・卸売
    "Industrial Distribution": "1629.T",
    "Electronics & Computer Distribution": "1629.T",
    "Medical Distribution": "1629.T",
    "Food Distribution": "1629.T",
    # 1630 小売
    "Specialty Retail": "1630.T",
    "Department Stores": "1630.T",
    "Apparel Retail": "1630.T",
    "Grocery Stores": "1630.T",
    "Internet Retail": "1630.T",
    "Home Improvement Retail": "1630.T",
    "Discount Stores": "1630.T",
    "Pharmaceutical Retailers": "1630.T",
    # 1631 銀行
    "Banks - Regional": "1631.T",
    "Banks - Diversified": "1631.T",
    # 1632 金融(除く銀行)
    "Capital Markets": "1632.T",
    "Credit Services": "1632.T",
    "Asset Management": "1632.T",
    "Insurance - Life": "1632.T",
    "Insurance - Property & Casualty": "1632.T",
    "Insurance - Diversified": "1632.T",
    "Insurance Brokers": "1632.T",
    "Mortgage Finance": "1632.T",
    "Financial Conglomerates": "1632.T",
    # 1633 不動産
    "Real Estate Services": "1633.T",
    "Real Estate - Diversified": "1633.T",
    "Real Estate - Development": "1633.T",
}

# 載せない業種(NOT_APPLICABLE のまま)。理由は PR 本文
_NOT_APPLICABLE_INDUSTRIES: frozenset[str] = frozenset(
    {
        "Conglomerates",
        "Waste Management",
        "Pollution & Treatment Controls",
        "Rental & Leasing Services",
        "Furnishings, Fixtures & Appliances",
        "Footwear & Accessories",
        "Apparel Manufacturing",
        "Luxury Goods",
        "Gambling",
        "Resorts & Casinos",
        "Lodging",
        "Travel Services",
        "Diagnostics & Research",
        "Auto & Truck Dealerships",
        "Aerospace & Defense",
        "Financial Data & Stock Exchanges",
    }
)


# --- T-A: config の静的検査 ---------------------------------------------------------


def test_the_config_map_is_exactly_the_reviewed_mapping() -> None:
    assert dict(_CFG.momentum.sector_etf_map) == _EXPECTED_MAPPING


def test_every_value_is_one_of_the_seventeen_topix17_etf_tickers() -> None:
    assert set(_CFG.momentum.sector_etf_map.values()) <= set(_TOPIX17_ETFS)


def test_every_topix17_etf_is_used_by_at_least_one_industry() -> None:
    """参照の無い ETF が無い(書き漏らし・取り違えの検出)。"""
    assert set(_CFG.momentum.sector_etf_map.values()) == set(_TOPIX17_ETFS)


def test_values_are_yfinance_tickers_in_the_code_dot_t_form() -> None:
    for industry, ticker in _CFG.momentum.sector_etf_map.items():
        assert re.fullmatch(r"\d{4}\.T", ticker), (industry, ticker)


def test_industries_decided_not_applicable_are_not_in_the_map() -> None:
    assert not (set(_CFG.momentum.sector_etf_map) & _NOT_APPLICABLE_INDUSTRIES)


def test_the_map_is_not_forced_to_cover_everything() -> None:
    """「100% mapping を強制しない」: NOT_APPLICABLE の業種が実際に存在する(map は網羅ではない)。"""
    assert len(_NOT_APPLICABLE_INDUSTRIES) > 0
    assert len(_CFG.momentum.sector_etf_map) == len(_EXPECTED_MAPPING)


def test_the_yaml_has_no_duplicate_keys() -> None:
    """★ YAML は重複キーを黙って後勝ちにする。重複があると mapping が意図と違っても気づけない。"""

    class _NoDuplicateLoader(yaml.SafeLoader):
        pass

    def _construct(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _value_node in node.value:
            key = loader.construct_object(key_node, deep=True)
            assert key not in seen, f"重複キー: {key!r}"
            seen.add(key)
        return loader.construct_mapping(node, deep=True)

    _NoDuplicateLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct)
    text = (_REPO_ROOT / "config" / "momentum_rules.yaml").read_text(encoding="utf-8")
    loaded = yaml.load(text, Loader=_NoDuplicateLoader)  # noqa: S506 - 検査用の SafeLoader 派生
    assert loaded["sector_etf_map"] == _EXPECTED_MAPPING


# --- T-B: provider 契約(yfinance を fake へ。実ネットワークなし) ---------------------------


class _FakeTicker:
    """登録外 symbol が、変換されずそのまま ticker として渡されることを記録する fake。"""

    created: list[str] = []

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        _FakeTicker.created.append(symbol)

    def history(self, **kwargs: object) -> pd.DataFrame:
        index = pd.to_datetime(["2026-07-24", "2026-07-27"])
        return pd.DataFrame(
            {
                "Open": [1000.0, 1010.0],
                "High": [1010.0, 1020.0],
                "Low": [990.0, 1000.0],
                "Close": [1005.0, 1015.0],
                "Volume": [10000, 12000],
            },
            index=index,
        )


@pytest.mark.parametrize("ticker", sorted(_TOPIX17_ETFS))
def test_provider_passes_an_etf_ticker_through_unchanged(
    monkeypatch: pytest.MonkeyPatch, ticker: str
) -> None:
    import jstock_advisor.providers.market_data.yfinance_impl as module

    _FakeTicker.created = []
    monkeypatch.setattr(module.yf, "Ticker", _FakeTicker)
    provider = YFinanceMarketDataProvider(now=_NOW)
    history = provider.get_benchmark_price_history(
        ticker, dt.date(2026, 7, 20), dt.date(2026, 7, 28)
    )
    assert _FakeTicker.created == [ticker]
    assert history is not None
    assert history.symbol == ticker
    assert [bar.date for bar in history.bars] == [dt.date(2026, 7, 24), dt.date(2026, 7, 27)]


# --- T-C: pipeline(mock provider をラップした spy。実ネットワークなし) --------------------------


class _IndustryOverrideFinancialProvider:
    """財務データの industry だけを差し替える(それ以外は委譲元のまま)。"""

    def __init__(self, delegate: Any, industry: str) -> None:
        self._delegate = delegate
        self._industry = industry

    def get_financial_summary(self, stock_code: str) -> Any:
        summary = self._delegate.get_financial_summary(stock_code)
        return None if summary is None else summary.model_copy(update={"industry": self._industry})

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


def _etf_stand_in(topix: Any) -> Any:
    """TOPIX の足から、TOPIX とは値の異なる ETF 系列を作る(終値だけ日ごとに増やす)。

    TOPIX と同一の系列だと、sector の足が TOPIX 側(市場相対)へ漏れても値が変わらず、
    SHADOW_ONLY の検査(T-D)が漏れを検出できない。
    """
    if topix is None:
        return None
    bars = [
        bar.model_copy(update={"close": bar.close * (1 + Decimal(index) / Decimal(2000))})
        for index, bar in enumerate(topix.bars)
    ]
    return topix.model_copy(update={"bars": bars})


class _SpyMarketDataProvider:
    """benchmark の取得を記録する。

    ETF の symbol には、`etf_has_data` なら mock の TOPIX の足を返し(ETF の足の代役)、
    そうでなければ None(取得できない)を返す。mock が登録外の symbol へ既定の系列を返す
    かどうかに依存しないよう、ETF の応答は常にここで明示する。
    """

    def __init__(self, delegate: Any, etf_symbols: frozenset[str], etf_has_data: bool) -> None:
        self._delegate = delegate
        self._etf_symbols = etf_symbols
        self._etf_has_data = etf_has_data
        self.benchmark_symbols: list[str] = []

    def get_benchmark_price_history(self, symbol: str, start: dt.date, end: dt.date) -> Any:
        self.benchmark_symbols.append(symbol)
        if symbol in self._etf_symbols:
            if not self._etf_has_data:
                return None
            return _etf_stand_in(self._delegate.get_benchmark_price_history("TOPIX", start, end))
        return self._delegate.get_benchmark_price_history(symbol, start, end)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


def _providers(industry: str) -> tuple[Any, _SpyMarketDataProvider]:
    bundle = build_mock_provider_bundle(_NOW)
    spy = _SpyMarketDataProvider(bundle.market_data, frozenset(_TOPIX17_ETFS), etf_has_data=True)
    providers = dataclasses.replace(
        bundle,
        market_data=spy,
        financial_data=_IndustryOverrideFinancialProvider(bundle.financial_data, industry),
    )
    return providers, spy


def _build(industry: str, config: Any = _CFG) -> tuple[Any, _SpyMarketDataProvider]:
    providers, spy = _providers(industry)
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, config, _CALENDAR)
    assert error is None, error
    assert snapshot is not None
    return snapshot, spy


def test_a_mapped_industry_fetches_the_mapped_etf_and_is_evaluated() -> None:
    snapshot, spy = _build("Packaged Foods")
    assert "1617.T" in spy.benchmark_symbols
    assert snapshot.sector_environment.sector_etf_symbol == "1617.T"
    assert snapshot.sector_environment.state == SectorEnvironmentEvaluationState.EVALUATED


def test_an_unmapped_industry_fetches_no_sector_benchmark_and_is_not_applicable() -> None:
    snapshot, spy = _build("Conglomerates")
    assert not [s for s in spy.benchmark_symbols if s in _TOPIX17_ETFS]
    assert snapshot.sector_environment.sector_etf_symbol is None
    assert snapshot.sector_environment.state == SectorEnvironmentEvaluationState.NOT_APPLICABLE


def test_a_mapped_industry_without_data_is_not_evaluated_not_not_applicable() -> None:
    """ETF の足が取れないときは「対象外」ではなく「評価不能」(両者を区別する現行の設計どおり)。"""
    bundle = build_mock_provider_bundle(_NOW)
    spy = _SpyMarketDataProvider(bundle.market_data, frozenset(_TOPIX17_ETFS), etf_has_data=False)
    providers = dataclasses.replace(
        bundle,
        market_data=spy,
        financial_data=_IndustryOverrideFinancialProvider(bundle.financial_data, "Packaged Foods"),
    )
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG, _CALENDAR)
    assert error is None and snapshot is not None
    assert snapshot.sector_environment.state == SectorEnvironmentEvaluationState.NOT_EVALUATED


@pytest.mark.parametrize("industry", sorted(_EXPECTED_MAPPING))
def test_every_mapped_industry_requests_exactly_its_own_etf(industry: str) -> None:
    _snapshot, spy = _build(industry)
    requested = [s for s in spy.benchmark_symbols if s in _TOPIX17_ETFS]
    assert requested == [_EXPECTED_MAPPING[industry]]


@pytest.mark.parametrize("industry", sorted(_NOT_APPLICABLE_INDUSTRIES))
def test_every_not_applicable_industry_requests_no_sector_etf(industry: str) -> None:
    _snapshot, spy = _build(industry)
    assert not [s for s in spy.benchmark_symbols if s in _TOPIX17_ETFS]


# --- T-D: SHADOW_ONLY(map の有無で、判定の入力・出力が変わらない) ------------------------------


def _config_without_map() -> Any:
    momentum = _CFG.momentum.model_copy(update={"sector_etf_map": {}})
    return _CFG.model_copy(update={"momentum": momentum})


def test_the_map_changes_only_sector_recording_fields_of_the_snapshot() -> None:
    with_map, _ = _build("Packaged Foods")
    without_map, _ = _build("Packaged Foods", _config_without_map())
    differing = {
        field.name
        for field in dataclasses.fields(with_map)
        if getattr(with_map, field.name) != getattr(without_map, field.name)
    }
    # 非 vacuous: 実際に sector 環境は変わっている(EVALUATED と NOT_APPLICABLE)
    assert "sector_environment" in differing
    # 変わってよいのは、sector 環境・それを合成する環境・momentum の sector 相対強度のみ
    assert differing <= {"sector_environment", "environment", "momentum"}, differing
    if "momentum" in differing:
        a, b = with_map.momentum.model_dump(), without_map.momentum.model_dump()
        momentum_differing = {key for key in a if a[key] != b[key]}
        assert momentum_differing == {"relative_strength_vs_sector_pct"}, momentum_differing


def test_the_buy_decision_is_identical_with_and_without_the_map() -> None:
    """★ BUY の判定(実 pipeline = BuySignalService.analyze)の出力は、
    sector 記録用の値を除いて一致する。"""

    def _analyze(config: Any) -> dict[str, Any]:
        providers, _spy = _providers("Packaged Foods")
        service = BuySignalService(providers=providers, config=config, business_calendar=_CALENDAR)
        outcome = service.analyze(_STOCK_CODE, _NOW, RecommendationType.BUY)
        assert outcome.recommendation is not None
        data = outcome.recommendation.model_dump()
        data.pop("recommendation_id")  # 実行ごとに採番される(uuid4)。判定の値ではない
        data["_buy_action"] = outcome.buy_action
        data["_ranking_group"] = outcome.ranking_group
        return data

    with_map = _analyze(_CFG)
    without_map = _analyze(_config_without_map())
    differing = {k for k in with_map if with_map[k] != without_map[k]}
    assert differing, "sector 記録用の値が変わっていない(検査が空振り)"
    unexpected = {k for k in differing if not k.startswith(("sector_", "environment_"))}
    assert not unexpected, f"判定に関わる値が map で変わった: {sorted(unexpected)}"
    assert with_map["_buy_action"] == without_map["_buy_action"]
