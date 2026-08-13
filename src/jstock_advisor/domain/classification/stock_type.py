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
from jstock_advisor.domain.signals.simple_roe import compute_simple_forecast_roe
from jstock_advisor.interfaces.types import Disclosure, DividendInfo, FinancialSummary


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
    dividend: DividendInfo | None = None,
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

    # --- GROWTH: BUY候補裾野拡大機能(2026-08)で配当条件を撤廃。営業利益
    # トレンドのみを主条件とする(高ROEは補助情報。それ単独では「成長」を
    # 意味しないため分類条件には使わない) ---
    growth_trend = is_earnings_trend_non_decreasing(quarterly_operating_incomes)
    roe_result = compute_simple_forecast_roe(financial.forecast_eps, financial.forecast_bps)
    if growth_trend:
        types.append(StockType.GROWTH)
        growth_basis = "営業利益が非減少トレンド"
        if roe_result.value is not None:
            growth_basis += f"(参考: 簡易予想ROE{roe_result.value * 100:.1f}%)"
        basis.append(growth_basis)

    current_pbr: Decimal | None = None
    if financial.forecast_bps is not None and financial.forecast_bps > 0:
        current_pbr = current_price / financial.forecast_bps
    # BuySignalService側と同じ式(current_price / forecast_eps)を
    # ここでも自己完結的に算出する(StockSnapshotに保持されているフィールド
    # ではないため。既存のcurrent_pbrと同じパターンを踏襲)。
    current_per: Decimal | None = None
    if financial.forecast_eps is not None and financial.forecast_eps > 0:
        current_per = current_price / financial.forecast_eps

    # --- VALUE: BUY候補裾野拡大機能(2026-08)で配当条件を撤廃。PBR/PERの
    # 現在水準のみで独立判定する(Historical Valuationは Shadow計測専用の
    # ためIssue #2境界を守り使用しない) ---
    value_reasons: list[str] = []
    if current_pbr is not None and current_pbr < Decimal(str(config.value.max_pbr)):
        value_reasons.append(f"PBR{current_pbr:.2f}倍が{config.value.max_pbr}倍未満")
    if current_per is not None and current_per < Decimal(str(config.value.max_per)):
        value_reasons.append(f"PER{current_per:.1f}倍が{config.value.max_per}倍未満")
    if value_reasons:
        types.append(StockType.VALUE)
        basis.append("、".join(value_reasons))

    # --- DIVIDEND_GROWTH(連続増配株、BUY候補裾野拡大機能2026-08で新設)。
    # 主条件は連続増配年数。「今期予想が前期実績比で減配」の判定は
    # forecast_annual_dividend_per_share(今期会社予想)と
    # previous_fiscal_year_dividend_per_share(前期実績)の比較という
    # 唯一の意味で固定する(どちらか片方が無ければこの否定条件は評価
    # せずスキップする。データ不足を理由に非該当にはしない) ---
    if dividend is not None:
        years = dividend.consecutive_dividend_increase_years
        min_years = config.dividend_growth.min_consecutive_dividend_increase_years
        if years is not None and years >= min_years:
            forecast_cut = False
            growth_pct_note = ""
            forecast_dps = dividend.forecast_annual_dividend_per_share
            previous_dps = dividend.previous_fiscal_year_dividend_per_share
            if forecast_dps is not None and previous_dps is not None and previous_dps > 0:
                growth_pct = float((forecast_dps - previous_dps) / previous_dps * 100)
                if forecast_dps < previous_dps:
                    forecast_cut = True
                growth_pct_note = f"(今期予想配当は前期比{growth_pct:+.1f}%)"
            if not forecast_cut and not dividend.is_dividend_cut_announced:
                types.append(StockType.DIVIDEND_GROWTH)
                basis.append(f"連続増配{years}年が下限{min_years}年以上{growth_pct_note}")

    # --- QUALITY(優良株、BUY候補裾野拡大機能2026-08で新設) ---
    quality_reasons: list[str] = []
    equity_ok = (
        financial.equity_ratio_pct is not None
        and financial.equity_ratio_pct >= config.quality.min_equity_ratio_pct
    )
    if equity_ok:
        quality_reasons.append(f"自己資本比率{financial.equity_ratio_pct:.1f}%")
    simple_roe_pct = roe_result.value * 100 if roe_result.value is not None else None
    roe_ok = simple_roe_pct is not None and simple_roe_pct >= config.quality.min_roe_pct
    if roe_ok and simple_roe_pct is not None:
        quality_reasons.append(f"簡易予想ROE{simple_roe_pct:.1f}%")
    earnings_ok = (
        not config.quality.require_earnings_trend_non_decreasing or growth_trend
    )
    cashflow_ok = financial.operating_cashflow is not None and financial.operating_cashflow > 0
    if cashflow_ok:
        quality_reasons.append("営業キャッシュフローが正")
    if equity_ok and roe_ok and earnings_ok and cashflow_ok:
        types.append(StockType.QUALITY)
        basis.append("、".join(quality_reasons) + "がいずれも優良株基準を満たす")

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
