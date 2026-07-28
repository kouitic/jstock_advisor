"""金融業の業種分類(2026-07仕様§2)。

yfinanceの`sector`/`industry`は英語のGICS準拠の値を返す(日本のTSE33業種名では
ない。実測で確認済み: 例えば三菱UFJフィナンシャル・グループは
sector="Financial Services", industry="Banks - Diversified")。
このモジュールはそれらの英語表記から金融業の細分類を機械的に判定する。

分類できない場合はNoneを返す(推測で補完しない)。呼び出し側は、Noneの場合を
「一般事業会社」として扱うのではなく、「業種不明」として財務健全性ルールの
適用要否を保守的に判断すること。
"""

from __future__ import annotations

from jstock_advisor.domain.entities.enums import FinancialIndustryCategory

_FINANCIAL_SECTOR_VALUES = {"financial services", "financial"}

_INDUSTRY_KEYWORDS: tuple[tuple[str, FinancialIndustryCategory], ...] = (
    ("bank", FinancialIndustryCategory.BANKING),
    ("insurance", FinancialIndustryCategory.INSURANCE),
    ("capital markets", FinancialIndustryCategory.SECURITIES),
    ("securities", FinancialIndustryCategory.SECURITIES),
    ("asset management", FinancialIndustryCategory.SECURITIES),
)


def classify_financial_industry(
    sector: str | None, industry: str | None
) -> FinancialIndustryCategory | None:
    """sector/industryの英語表記から金融業の細分類を判定する。

    sectorが金融関連でない場合は明示的にNoneを返す(=一般事業会社)。
    sectorが金融関連だが、industryが既知のキーワードに一致しない場合は
    OTHER_FINANCIALとする(証券商品先物取引業・その他金融業を包含する保守的な扱い)。
    sector自体が取得できない場合は判定不能としてNoneを返す。
    """
    if sector is None:
        return None
    sector_lower = sector.strip().lower()
    if sector_lower not in _FINANCIAL_SECTOR_VALUES:
        return None

    industry_lower = (industry or "").strip().lower()
    for keyword, category in _INDUSTRY_KEYWORDS:
        if keyword in industry_lower:
            return category
    return FinancialIndustryCategory.OTHER_FINANCIAL


def is_financial_industry(sector: str | None, industry: str | None) -> bool:
    return classify_financial_industry(sector, industry) is not None
