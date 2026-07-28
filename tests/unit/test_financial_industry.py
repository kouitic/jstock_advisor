from jstock_advisor.domain.classification.financial_industry import classify_industry
from jstock_advisor.domain.entities.enums import FinancialIndustryCategory, IndustryClassification


def test_sector_none_is_unknown_not_general_corporate() -> None:
    result = classify_industry(None, None)
    assert result.classification == IndustryClassification.UNKNOWN


def test_sector_empty_string_is_unknown() -> None:
    result = classify_industry("", "")
    assert result.classification == IndustryClassification.UNKNOWN


def test_clearly_non_financial_sector_is_general_corporate() -> None:
    result = classify_industry("Technology", "Software - Application")
    assert result.classification == IndustryClassification.GENERAL_CORPORATE


def test_unrecognized_sector_value_is_unknown_not_general_corporate() -> None:
    # 未知のsector表記(誤記・新規sector等)は、非金融業と確認できないためUNKNOWN。
    result = classify_industry("SomeUnknownSectorValue", "x")
    assert result.classification == IndustryClassification.UNKNOWN


def test_financial_services_sector_bank_industry_classified_as_banking() -> None:
    result = classify_industry("Financial Services", "Banks - Diversified")
    assert result.classification == IndustryClassification.FINANCIAL
    assert result.financial_category == FinancialIndustryCategory.BANKING


def test_financial_services_sector_insurance_industry_classified() -> None:
    result = classify_industry("Financial Services", "Insurance - Life")
    assert result.financial_category == FinancialIndustryCategory.INSURANCE


def test_financial_services_sector_unrecognized_industry_is_other_financial() -> None:
    result = classify_industry("Financial Services", "Some Niche Category")
    assert result.classification == IndustryClassification.FINANCIAL
    assert result.financial_category == FinancialIndustryCategory.OTHER_FINANCIAL


def test_japanese_sector_name_identifies_financial_industry() -> None:
    result = classify_industry("金融", "銀行業")
    assert result.classification == IndustryClassification.FINANCIAL
    assert result.financial_category == FinancialIndustryCategory.BANKING


def test_japanese_insurance_industry_name_identifies_insurance() -> None:
    result = classify_industry("金融", "保険業")
    assert result.financial_category == FinancialIndustryCategory.INSURANCE
