"""購入判断における業種別分類(2026-07 BUYパイプライン再設計。要求仕様12節)。

医薬品・自動車部品・銀行等は一般事業会社向けのPER/PBR/配当利回りモデルを
そのまま適用すべきではない(新薬パイプラインの承認確率、自動車生産台数への
依存、CET1比率等はいずれも通常のPER/PBRだけでは反映できない)。ただし、
これらを反映する専用の多変量モデルを安定運用できるデータソースが現時点で
存在しないため、区分の識別と信頼度HIGH禁止ゲート・安全余裕率加算のみを行い、
専用モデル自体は実装しない(推測で補完しない方針)。industry_model_appliedは
常にFalseとなる。

利確判定用の`profit_taking_industry.py::classify_profit_taking_industry_sector()`
とはメンバー構成・用途が異なるため独立させている(統合すると利確側の
既存キーワード判定・ゲートを壊すリスクがあるため)。
"""

from __future__ import annotations

from jstock_advisor.domain.entities.enums import BuyIndustrySector

# 判定順序が重要(先に一致したカテゴリを採用する)。より特定的な業種
# (医薬品・自動車部品等)を、より一般的な区分(GENERAL_MANUFACTURING)より先に判定する。
_KEYWORDS: dict[BuyIndustrySector, tuple[str, ...]] = {
    BuyIndustrySector.BANK: ("銀行", "Bank", "Banks"),
    BuyIndustrySector.LEASE_FINANCE: (
        "リース",
        "Lease",
        "貸金",
        "信販",
        "Credit Services",
    ),
    BuyIndustrySector.PHARMACEUTICAL: (
        "医薬品",
        "製薬",
        "Pharmaceutical",
        "Drug Manufacturers",
        "Biotechnology",
    ),
    BuyIndustrySector.AUTOMOTIVE_PARTS: (
        "自動車部品",
        "輸送用機器",
        "Auto Parts",
        "Auto Components",
        "Automobiles",
    ),
    BuyIndustrySector.UTILITY: (
        "電気",
        "ガス",
        "Utilities",
        "公益",
        "Gas",
        "Electric Utilities",
    ),
    BuyIndustrySector.FOOD: (
        "食品",
        "Food",
        "飲料",
        "Beverages",
        "水産",
        "農林",
        "鶏卵",
        "食鳥",
        "Agricultural",
    ),
    BuyIndustrySector.CYCLICAL_MATERIALS: (
        "化学",
        "Chemical",
        "鉄鋼",
        "Steel",
        "非鉄金属",
        "金属製品",
        "Metal",
        "紙・パルプ",
        "ガラス・土石製品",
        "石油・石炭製品",
        "Basic Materials",
    ),
    BuyIndustrySector.GENERAL_MANUFACTURING: (
        "機械",
        "Machinery",
        "精密機器",
        "電気機器",
        "Electrical Equipment",
        "Industrial",
    ),
}

_MISSING_REASON: dict[BuyIndustrySector, str] = {
    BuyIndustrySector.BANK: "BANK_MODEL_NOT_AVAILABLE",
    BuyIndustrySector.LEASE_FINANCE: "LEASE_FINANCE_MODEL_NOT_AVAILABLE",
    BuyIndustrySector.PHARMACEUTICAL: "PHARMACEUTICAL_MODEL_NOT_AVAILABLE",
    BuyIndustrySector.AUTOMOTIVE_PARTS: "AUTOMOTIVE_PARTS_MODEL_NOT_AVAILABLE",
    BuyIndustrySector.CYCLICAL_MATERIALS: "CYCLICAL_MATERIALS_MODEL_NOT_AVAILABLE",
    BuyIndustrySector.UTILITY: "UTILITY_MODEL_NOT_AVAILABLE",
    BuyIndustrySector.FOOD: "FOOD_MODEL_NOT_AVAILABLE",
    BuyIndustrySector.GENERAL_MANUFACTURING: "GENERAL_MANUFACTURING_MODEL_NOT_AVAILABLE",
    BuyIndustrySector.SMALL_GROWTH: "SMALL_GROWTH_MODEL_NOT_AVAILABLE",
    BuyIndustrySector.GENERAL: "GENERAL_MODEL_NOT_DIFFERENTIATED",
    BuyIndustrySector.UNKNOWN: "INDUSTRY_UNKNOWN",
}

# 市況(商品価格・為替・景気循環)の影響を強く受け、単年度予想EPSだけでの
# PER方式評価が適さない業種(平準化EPSの適用対象、要求仕様13節)。
CYCLICAL_SECTORS = frozenset(
    {
        BuyIndustrySector.AUTOMOTIVE_PARTS,
        BuyIndustrySector.CYCLICAL_MATERIALS,
        BuyIndustrySector.FOOD,
    }
)


def classify_buy_industry_sector(
    industry: str | None, sector: str | None, is_growth_stock: bool
) -> BuyIndustrySector:
    """industry/sector文字列(yfinance由来、日本語企業でも英語表記の場合がある)から
    キーワード一致で区分を判定する。いずれも取得できない場合はUNKNOWN、
    一致しない場合は成長株分類(is_growth_stock)ならSMALL_GROWTH、
    それ以外はGENERALとする。
    """
    text = f"{industry or ''} {sector or ''}"
    if not text.strip():
        return BuyIndustrySector.UNKNOWN
    for category, keywords in _KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return category
    if is_growth_stock:
        return BuyIndustrySector.SMALL_GROWTH
    return BuyIndustrySector.GENERAL


def buy_industry_model_missing_reason(sector: BuyIndustrySector) -> str:
    return _MISSING_REASON[sector]
