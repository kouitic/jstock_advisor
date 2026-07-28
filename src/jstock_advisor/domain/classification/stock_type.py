"""銘柄タイプ分類(要求仕様7節)。

LLMや自由文推測は使わず、config/stock_classification_rules.yamlの閾値駆動の
決定的ルールのみで分類する(既存プロジェクト方針を踏襲)。複合タイプを許容する
(例: 5401日本製鉄はCYCLICAL+INCOME、8136サンリオはGROWTH、JTはINCOME+DEFENSIVE)。

VALUE/ASSET_PLAYはPBRのみに依存し、過去PER中央値等(適正価格の複数手法化が
整うまで利用不可)を使わないため、これらが該当する場合は分類全体の信頼度を
LOWとする(データ制約を正直に反映する)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.config.models import StockClassificationRulesConfig
from jstock_advisor.domain.entities.classification import StockTypeClassification
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import ConfidenceLevel, StockType
from jstock_advisor.domain.signals.buy_signal import is_earnings_trend_non_decreasing
from jstock_advisor.interfaces.types import Disclosure, FinancialSummary


def _is_improving(values: list[Decimal], consecutive_periods: int) -> bool:
    if consecutive_periods < 1 or len(values) < consecutive_periods + 1:
        return False
    recent = values[-(consecutive_periods + 1) :]
    return all(recent[i] > recent[i - 1] for i in range(1, len(recent)))


def classify_stock_type(
    financial: FinancialSummary,
    dividend_yield_pct: float | None,
    current_price: Decimal,
    quarterly_operating_incomes: list[Decimal],
    disclosures: list[Disclosure],
    now: dt.datetime,
    config: StockClassificationRulesConfig,
    data_sources: list[DataSourceReference],
) -> StockTypeClassification:
    types: list[StockType] = []
    basis: list[str] = []

    if (
        dividend_yield_pct is not None
        and dividend_yield_pct >= config.income.min_dividend_yield_pct
        and (
            financial.payout_ratio_pct is None
            or financial.payout_ratio_pct <= config.income.max_payout_ratio_pct
        )
    ):
        types.append(StockType.INCOME)
        basis.append(
            f"予想配当利回り{dividend_yield_pct:.2f}%が下限{config.income.min_dividend_yield_pct}%以上"
        )

    growth_trend = is_earnings_trend_non_decreasing(quarterly_operating_incomes)
    if growth_trend and (
        dividend_yield_pct is None or dividend_yield_pct < config.growth.max_dividend_yield_pct
    ):
        types.append(StockType.GROWTH)
        basis.append("営業利益が非減少トレンド、かつ配当利回りが低水準")

    current_pbr: Decimal | None = None
    if financial.forecast_bps is not None and financial.forecast_bps > 0:
        current_pbr = current_price / financial.forecast_bps

    if (
        current_pbr is not None
        and current_pbr < Decimal(str(config.value.max_pbr))
        and dividend_yield_pct is not None
        and dividend_yield_pct >= config.value.min_dividend_yield_pct
    ):
        types.append(StockType.VALUE)
        basis.append(f"PBR{current_pbr:.2f}倍が{config.value.max_pbr}倍未満、かつ配当利回りが一定水準以上")

    industry = financial.industry or ""
    if any(keyword in industry for keyword in config.cyclical.industry_keywords):
        types.append(StockType.CYCLICAL)
        basis.append(f"業種({industry})が景気敏感セクターのキーワードに一致")

    if any(keyword in industry for keyword in config.defensive.industry_keywords):
        types.append(StockType.DEFENSIVE)
        basis.append(f"業種({industry})がディフェンシブセクターのキーワードに一致")

    if financial.is_deficit and _is_improving(
        quarterly_operating_incomes, config.turnaround.min_consecutive_improvement_quarters
    ):
        types.append(StockType.TURNAROUND)
        basis.append("赤字だが営業利益が連続改善傾向")

    if (
        current_pbr is not None
        and current_pbr < Decimal(str(config.asset_play.max_pbr))
        and financial.equity_ratio_pct is not None
        and financial.equity_ratio_pct >= config.asset_play.min_equity_ratio_pct
    ):
        types.append(StockType.ASSET_PLAY)
        basis.append(f"PBR{current_pbr:.2f}倍かつ自己資本比率{financial.equity_ratio_pct:.1f}%")

    matched_keywords = [
        keyword
        for keyword in config.event_driven.disclosure_keywords
        if any(
            keyword in d.title or (d.category is not None and keyword in d.category)
            for d in disclosures
        )
    ]
    if matched_keywords:
        types.append(StockType.EVENT_DRIVEN)
        basis.append(f"開示にイベント関連キーワード({'、'.join(matched_keywords)})を検出")

    primary_type = types[0] if types else None
    pbr_dependent = StockType.VALUE in types or StockType.ASSET_PLAY in types
    if not types or pbr_dependent:
        confidence = ConfidenceLevel.LOW
    elif len(types) >= 2:
        confidence = ConfidenceLevel.HIGH
    else:
        confidence = ConfidenceLevel.MEDIUM

    return StockTypeClassification(
        stock_code=financial.stock_code,
        classified_at=now,
        types=types,
        primary_type=primary_type,
        confidence=confidence,
        classification_basis=basis,
        data_sources=data_sources,
    )
