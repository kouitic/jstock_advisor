"""利確判定における業種別分類(2026-07仕様レビュー対応・要求仕様§7)。

銀行・リース金融等は一般事業会社向けのPER/PBR/配当利回りモデルをそのまま
適用すべきではない。ただし、指定された評価要素(CET1比率・DOE・信用コスト等)を
安定して取得できるデータソースが現時点で存在しないため、区分の識別と
HIGH信頼度禁止ゲートのみを行い、専用の多変量モデル自体は実装しない
(推測で補完しない方針)。industry_model_appliedは常にFalseとなる。
"""

from __future__ import annotations

from jstock_advisor.domain.entities.enums import ProfitTakingIndustrySector

_KEYWORDS: dict[ProfitTakingIndustrySector, tuple[str, ...]] = {
    ProfitTakingIndustrySector.BANKING: ("銀行", "Bank"),
    ProfitTakingIndustrySector.LEASING_FINANCE: (
        "リース",
        "Lease",
        "貸金",
        "信販",
        "Credit Services",
    ),
    ProfitTakingIndustrySector.FOOD: ("食品", "Food", "飲料", "Beverages"),
    ProfitTakingIndustrySector.CHEMICAL: ("化学", "Chemical"),
    ProfitTakingIndustrySector.GAS_UTILITY: ("ガス", "Gas", "電気", "Utilities", "公益"),
}

_MISSING_REASON: dict[ProfitTakingIndustrySector, str] = {
    ProfitTakingIndustrySector.BANKING: "BANKING_MODEL_NOT_AVAILABLE",
    ProfitTakingIndustrySector.LEASING_FINANCE: "LEASING_FINANCE_MODEL_NOT_AVAILABLE",
    ProfitTakingIndustrySector.FOOD: "FOOD_MODEL_NOT_AVAILABLE",
    ProfitTakingIndustrySector.CHEMICAL: "CHEMICAL_MODEL_NOT_AVAILABLE",
    ProfitTakingIndustrySector.GAS_UTILITY: "GAS_UTILITY_MODEL_NOT_AVAILABLE",
    ProfitTakingIndustrySector.SMALL_GROWTH: "SMALL_GROWTH_MODEL_NOT_AVAILABLE",
    ProfitTakingIndustrySector.GENERAL: "GENERAL_MODEL_NOT_DIFFERENTIATED",
    ProfitTakingIndustrySector.UNKNOWN: "INDUSTRY_UNKNOWN",
}


def classify_profit_taking_industry_sector(
    industry: str | None, sector: str | None, is_growth_stock: bool
) -> ProfitTakingIndustrySector:
    """industry/sector文字列(yfinance由来、日本語企業でも英語表記)からキーワード一致で
    区分を判定する。いずれも取得できない場合はUNKNOWN、一致しない場合は
    成長株分類(is_growth_stock)ならSMALL_GROWTH、それ以外はGENERALとする。
    """
    text = f"{industry or ''} {sector or ''}"
    if not text.strip():
        return ProfitTakingIndustrySector.UNKNOWN
    for category, keywords in _KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return category
    if is_growth_stock:
        return ProfitTakingIndustrySector.SMALL_GROWTH
    return ProfitTakingIndustrySector.GENERAL


def industry_model_missing_reason(sector: ProfitTakingIndustrySector) -> str:
    return _MISSING_REASON[sector]
