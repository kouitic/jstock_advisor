import datetime as dt

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import RecordDateUnknownReason
from jstock_advisor.interfaces.types import BenefitDetail, DividendInfo, ShareholderBenefit
from jstock_advisor.services.profit_taking_service import (
    _benefit_record_date_recurring_label,
    _dividend_record_date_recurring_label,
)

_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)
_SOURCE = DataSourceReference(provider="test", fetched_at=_NOW)


def _dividend(**overrides: object) -> DividendInfo:
    defaults: dict[str, object] = {
        "stock_code": "7042",
        "fiscal_year": "2027",
        "dividend_record_date": None,
        "dividend_record_date_unknown_reason": RecordDateUnknownReason.DATA_PROVIDER_MISSING,
        "source": _SOURCE,
    }
    defaults.update(overrides)
    return DividendInfo(**defaults)  # type: ignore[arg-type]


def test_dividend_record_date_recurring_label_derived_from_fiscal_year_end() -> None:
    # 決算期末が3月末の場合、期末配当(3月末)+中間配当(9月末)の年2回パターンを推定する。
    dividend = _dividend()
    label = _dividend_record_date_recurring_label(dividend, dt.date(2027, 3, 31))
    assert label is not None
    assert "3月末" in label
    assert "9月末" in label


def test_dividend_record_date_recurring_label_none_when_date_is_known() -> None:
    dividend = _dividend(
        dividend_record_date=dt.date(2027, 3, 31),
        dividend_record_date_unknown_reason=None,
    )
    assert _dividend_record_date_recurring_label(dividend, dt.date(2027, 3, 31)) is None


def test_dividend_record_date_recurring_label_none_when_fiscal_period_end_unknown() -> None:
    dividend = _dividend()
    assert _dividend_record_date_recurring_label(dividend, None) is None


def test_dividend_record_date_recurring_label_none_when_reason_is_not_provider_missing() -> None:
    # データ提供元の恒久的制約以外の理由(未登録等)では推定ラベルを出さない。
    dividend = _dividend(
        dividend_record_date_unknown_reason=RecordDateUnknownReason.SOURCE_NOT_FOUND
    )
    assert _dividend_record_date_recurring_label(dividend, dt.date(2027, 3, 31)) is None


def _benefit(**overrides: object) -> ShareholderBenefit:
    defaults: dict[str, object] = {
        "stock_code": "7042",
        "min_shares_required": 100,
        "benefits": [
            BenefitDetail(
                category="CASH_EQUIVALENT",
                description="QUOカード",
                min_shares_for_tier=100,
            )
        ],
        "frequency_per_year": 1,
        "benefit_record_date_unknown_reason": RecordDateUnknownReason.DATA_PROVIDER_MISSING,
        "source": _SOURCE,
    }
    defaults.update(overrides)
    return ShareholderBenefit(**defaults)  # type: ignore[arg-type]


def test_benefit_record_date_recurring_label_single_frequency() -> None:
    benefit = _benefit(frequency_per_year=1)
    label = _benefit_record_date_recurring_label(benefit, dt.date(2027, 3, 31))
    assert label is not None
    assert "3月末" in label
    assert "9月末" not in label


def test_benefit_record_date_recurring_label_semiannual_frequency() -> None:
    benefit = _benefit(frequency_per_year=2)
    label = _benefit_record_date_recurring_label(benefit, dt.date(2027, 3, 31))
    assert label is not None
    assert "3月末" in label
    assert "9月末" in label


def test_benefit_record_date_recurring_label_none_when_no_benefit() -> None:
    assert _benefit_record_date_recurring_label(None, dt.date(2027, 3, 31)) is None
