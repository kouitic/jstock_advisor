from decimal import Decimal

from jstock_advisor.domain.entities.enums import BuyPriceReliability, EarningsDateStatus
from jstock_advisor.domain.valuation.buy_price_reliability import determine_buy_price_reliability
from jstock_advisor.domain.valuation.margin_of_safety import MarginOfSafetyResult

_OK_MARGIN = MarginOfSafetyResult(
    entry_margin=Decimal("0.10"),
    standard_margin=Decimal("0.15"),
    strong_margin=Decimal("0.20"),
    entry_margin_before_cap=Decimal("0.10"),
)


def test_ok_when_no_concerns() -> None:
    result = determine_buy_price_reliability(
        margin_result=_OK_MARGIN,
        maximum_entry_margin=0.30,
        valuation_dispersion_ratio=1.1,
        dispersion_medium_max=1.60,
        methods_used_count=4,
        data_quality_warning=False,
        earnings_date_status=EarningsDateStatus.CONFIRMED,
        excluded_outlier_count=0,
    )
    assert result.reliability == BuyPriceReliability.OK
    assert result.concerns == []


def test_low_when_entry_margin_before_cap_exceeds_maximum_alone() -> None:
    margin = MarginOfSafetyResult(
        entry_margin=Decimal("0.30"),
        standard_margin=Decimal("0.38"),
        strong_margin=Decimal("0.45"),
        entry_margin_before_cap=Decimal("0.48"),
    )
    result = determine_buy_price_reliability(
        margin_result=margin,
        maximum_entry_margin=0.30,
        valuation_dispersion_ratio=1.1,
        dispersion_medium_max=1.60,
        methods_used_count=4,
        data_quality_warning=False,
        earnings_date_status=EarningsDateStatus.CONFIRMED,
        excluded_outlier_count=0,
    )
    assert result.reliability == BuyPriceReliability.LOW
    assert "ENTRY_MARGIN_EXCEEDS_CAP" in result.concerns


def test_ok_when_only_one_secondary_concern() -> None:
    # 業種別モデル未適用相当のような恒常的リスクを含め、懸念が1件だけなら
    # OKのままとする(毎回LOWになることを防ぐ)。
    result = determine_buy_price_reliability(
        margin_result=_OK_MARGIN,
        maximum_entry_margin=0.30,
        valuation_dispersion_ratio=1.8,  # HIGH_VALUATION_DISPERSIONのみ該当
        dispersion_medium_max=1.60,
        methods_used_count=4,
        data_quality_warning=False,
        earnings_date_status=EarningsDateStatus.CONFIRMED,
        excluded_outlier_count=0,
    )
    assert result.reliability == BuyPriceReliability.OK
    assert result.concerns == ["HIGH_VALUATION_DISPERSION"]


def test_low_when_two_or_more_secondary_concerns() -> None:
    result = determine_buy_price_reliability(
        margin_result=_OK_MARGIN,
        maximum_entry_margin=0.30,
        valuation_dispersion_ratio=1.8,  # HIGH_VALUATION_DISPERSION
        dispersion_medium_max=1.60,
        methods_used_count=4,
        data_quality_warning=True,  # DATA_QUALITY_WARNING
        earnings_date_status=EarningsDateStatus.CONFIRMED,
        excluded_outlier_count=0,
    )
    assert result.reliability == BuyPriceReliability.LOW
    assert set(result.concerns) == {"HIGH_VALUATION_DISPERSION", "DATA_QUALITY_WARNING"}


def test_tachi_s_regression_is_low() -> None:
    # 実データ回帰(タチエス7239): entry_margin_before_capは上限(0.30)ちょうどで
    # 超過はしないが、バラつき1.93倍(medium_max超)・データ品質懸念(次回決算日
    # 不明)・DCF除外の3件が該当し、2件以上のためLOWとなる。
    margin = MarginOfSafetyResult(
        entry_margin=Decimal("0.30"),
        standard_margin=Decimal("0.38"),
        strong_margin=Decimal("0.45"),
        entry_margin_before_cap=Decimal("0.30"),
    )
    result = determine_buy_price_reliability(
        margin_result=margin,
        maximum_entry_margin=0.30,
        valuation_dispersion_ratio=1.929,
        dispersion_medium_max=1.60,
        methods_used_count=4,
        data_quality_warning=True,
        earnings_date_status=EarningsDateStatus.UNAVAILABLE,
        excluded_outlier_count=1,
    )
    assert result.reliability == BuyPriceReliability.LOW
    assert "ENTRY_MARGIN_EXCEEDS_CAP" not in result.concerns


def test_low_when_too_few_methods_and_stale_earnings_date() -> None:
    result = determine_buy_price_reliability(
        margin_result=_OK_MARGIN,
        maximum_entry_margin=0.30,
        valuation_dispersion_ratio=1.1,
        dispersion_medium_max=1.60,
        methods_used_count=2,
        data_quality_warning=False,
        earnings_date_status=EarningsDateStatus.STALE_PAST_DATE,
        excluded_outlier_count=0,
    )
    assert result.reliability == BuyPriceReliability.LOW
    assert set(result.concerns) == {"TOO_FEW_VALUATION_METHODS", "STALE_EARNINGS_DATE"}
