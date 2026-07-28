"""金融業の業種分類(2026-07仕様§2、レビュー対応で三値化)。

yfinanceの`sector`/`industry`は英語のGICS準拠の値を返す(日本のTSE33業種名では
ない。実測で確認済み: 例えば三菱UFJフィナンシャル・グループは
sector="Financial Services", industry="Banks - Diversified")。
将来的に日本語の業種名(銀行業・保険業・証券、商品先物取引業等)を渡す
データソースが追加された場合にも対応できるよう、日本語キーワードも判定する。

分類結果は三値(GENERAL_CORPORATE/FINANCIAL/UNKNOWN)。sectorが欠損・空文字の
場合や、既知のいずれのキーワードにも一致しない場合はUNKNOWNとし、
「非金融業であることが確認済み」とは区別する。UNKNOWNを一般事業会社として
扱ってはならない(一般事業会社向けの財務健全性ルールを適用しない)。
"""

from __future__ import annotations

from dataclasses import dataclass

from jstock_advisor.domain.entities.enums import FinancialIndustryCategory, IndustryClassification

_FINANCIAL_SECTOR_VALUES_EN = {"financial services", "financial", "financials"}
_FINANCIAL_SECTOR_KEYWORDS_JA = ("金融",)

_INDUSTRY_KEYWORDS: tuple[tuple[str, FinancialIndustryCategory], ...] = (
    ("bank", FinancialIndustryCategory.BANKING),
    ("銀行", FinancialIndustryCategory.BANKING),
    ("insurance", FinancialIndustryCategory.INSURANCE),
    ("保険", FinancialIndustryCategory.INSURANCE),
    ("capital markets", FinancialIndustryCategory.SECURITIES),
    ("securities", FinancialIndustryCategory.SECURITIES),
    ("asset management", FinancialIndustryCategory.SECURITIES),
    ("証券", FinancialIndustryCategory.SECURITIES),
    ("商品先物", FinancialIndustryCategory.SECURITIES),
)

# yfinanceが返す既知のGICS準拠sector値(Financial Services以外)。この集合に
# 一致する場合のみ「非金融業であることが確認済み」= GENERAL_CORPORATEとする。
# 未知の値(誤記・新規sector追加・データソース変更等)はUNKNOWNとして安全側に倒す。
_KNOWN_NON_FINANCIAL_SECTORS_EN = {
    "technology",
    "healthcare",
    "consumer cyclical",
    "consumer defensive",
    "industrials",
    "communication services",
    "energy",
    "basic materials",
    "real estate",
    "utilities",
}


@dataclass(frozen=True)
class IndustryClassificationResult:
    classification: IndustryClassification
    financial_category: FinancialIndustryCategory | None = None


def classify_industry(sector: str | None, industry: str | None) -> IndustryClassificationResult:
    sector_norm = (sector or "").strip()
    if not sector_norm:
        return IndustryClassificationResult(IndustryClassification.UNKNOWN)

    sector_lower = sector_norm.lower()
    is_financial_sector = sector_lower in _FINANCIAL_SECTOR_VALUES_EN or any(
        kw in sector_norm for kw in _FINANCIAL_SECTOR_KEYWORDS_JA
    )
    if is_financial_sector:
        industry_text = f"{industry or ''}".lower() + (industry or "")
        for keyword, category in _INDUSTRY_KEYWORDS:
            if keyword.lower() in industry_text.lower():
                return IndustryClassificationResult(IndustryClassification.FINANCIAL, category)
        return IndustryClassificationResult(
            IndustryClassification.FINANCIAL, FinancialIndustryCategory.OTHER_FINANCIAL
        )

    if sector_lower in _KNOWN_NON_FINANCIAL_SECTORS_EN:
        return IndustryClassificationResult(IndustryClassification.GENERAL_CORPORATE)

    # 既知のいずれの値にも一致しない(誤記・未知のsector表記等) -> 安全側でUNKNOWN
    return IndustryClassificationResult(IndustryClassification.UNKNOWN)


def classify_financial_industry(
    sector: str | None, industry: str | None
) -> FinancialIndustryCategory | None:
    """後方互換用の簡易API。金融業の細分類のみが必要な場合に使う。"""
    return classify_industry(sector, industry).financial_category


def is_financial_industry(sector: str | None, industry: str | None) -> bool:
    return classify_industry(sector, industry).classification == IndustryClassification.FINANCIAL
