from jstock_advisor.domain.classification.buy_industry import (
    buy_industry_model_missing_reason,
    classify_buy_industry_sector,
)
from jstock_advisor.domain.entities.enums import BuyIndustrySector


def test_classify_pharmaceutical_4516_nihon_shinyaku() -> None:
    result = classify_buy_industry_sector("医薬品", "Healthcare", is_growth_stock=False)
    assert result == BuyIndustrySector.PHARMACEUTICAL


def test_classify_automotive_parts_7239_tachi_s() -> None:
    result = classify_buy_industry_sector("輸送用機器", "Auto Parts", is_growth_stock=False)
    assert result == BuyIndustrySector.AUTOMOTIVE_PARTS


def test_classify_automotive_parts_4246_daikyo_nishikawa() -> None:
    result = classify_buy_industry_sector(
        "Auto Parts", "Consumer Cyclical", is_growth_stock=False
    )
    assert result == BuyIndustrySector.AUTOMOTIVE_PARTS


def test_classify_food_1384_hokuryo() -> None:
    result = classify_buy_industry_sector("水産・農林業", "Food", is_growth_stock=False)
    assert result == BuyIndustrySector.FOOD


def test_classify_general_manufacturing_7723_aichi_tokei() -> None:
    result = classify_buy_industry_sector("機械", "Industrial", is_growth_stock=False)
    assert result == BuyIndustrySector.GENERAL_MANUFACTURING


def test_classify_bank() -> None:
    assert classify_buy_industry_sector("銀行業", None, is_growth_stock=False) == (
        BuyIndustrySector.BANK
    )


def test_classify_unknown_when_no_data() -> None:
    assert classify_buy_industry_sector(None, None, is_growth_stock=False) == (
        BuyIndustrySector.UNKNOWN
    )


def test_classify_small_growth_when_no_keyword_match_and_growth_stock() -> None:
    result = classify_buy_industry_sector("その他サービス", None, is_growth_stock=True)
    assert result == BuyIndustrySector.SMALL_GROWTH


def test_classify_general_when_no_keyword_match_and_not_growth_stock() -> None:
    result = classify_buy_industry_sector("その他サービス", None, is_growth_stock=False)
    assert result == BuyIndustrySector.GENERAL


def test_industry_model_missing_reason_covers_all_sectors() -> None:
    for sector in BuyIndustrySector:
        reason = buy_industry_model_missing_reason(sector)
        assert reason
        assert isinstance(reason, str)
