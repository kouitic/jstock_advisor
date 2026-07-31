import datetime as dt

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import RecordDateUnknownReason, SourceType
from jstock_advisor.domain.signals.record_date_resolution import (
    resolve_benefit_record_date_recurring_label,
    resolve_benefit_record_date_source_type,
    resolve_dividend_record_date_recurring_label,
    resolve_dividend_record_date_source_type,
)
from jstock_advisor.interfaces.types import BenefitDetail, DividendInfo, ShareholderBenefit

_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)
_PROVIDER_SOURCE = DataSourceReference(
    provider="test", fetched_at=_NOW, source_type=SourceType.CONTRACTED_PROVIDER
)
_MANUAL_SOURCE = DataSourceReference(
    provider="manual_registry", fetched_at=_NOW, source_type=SourceType.MANUAL_REGISTRY
)


def _dividend(**overrides: object) -> DividendInfo:
    defaults: dict[str, object] = {
        "stock_code": "7042",
        "fiscal_year": "2027",
        "dividend_record_date": None,
        "dividend_record_date_unknown_reason": RecordDateUnknownReason.DATA_PROVIDER_MISSING,
        "source": _PROVIDER_SOURCE,
    }
    defaults.update(overrides)
    return DividendInfo(**defaults)  # type: ignore[arg-type]


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
        "source": _MANUAL_SOURCE,
    }
    defaults.update(overrides)
    return ShareholderBenefit(**defaults)  # type: ignore[arg-type]


# --- 配当基準日の推定ラベル(既存動作、移設のみ) ---


def test_dividend_record_date_recurring_label_derived_from_fiscal_year_end() -> None:
    # 決算期末が3月末の場合、期末配当(3月末)+中間配当(9月末)の年2回パターンを推定する。
    dividend = _dividend()
    label = resolve_dividend_record_date_recurring_label(dividend, 3)
    assert label is not None
    assert "3月末" in label
    assert "9月末" in label


def test_dividend_record_date_recurring_label_none_when_date_is_known() -> None:
    dividend = _dividend(
        dividend_record_date=dt.date(2027, 3, 31),
        dividend_record_date_unknown_reason=None,
    )
    assert resolve_dividend_record_date_recurring_label(dividend, 3) is None


def test_dividend_record_date_recurring_label_none_when_fiscal_period_end_unknown() -> None:
    dividend = _dividend()
    assert resolve_dividend_record_date_recurring_label(dividend, None) is None


def test_dividend_record_date_recurring_label_none_when_reason_is_not_provider_missing() -> None:
    # データ提供元の恒久的制約以外の理由(未登録等)では推定ラベルを出さない。
    dividend = _dividend(
        dividend_record_date_unknown_reason=RecordDateUnknownReason.SOURCE_NOT_FOUND
    )
    assert resolve_dividend_record_date_recurring_label(dividend, 3) is None


# --- 優待基準日の推定・登録済み周期ラベル ---


def test_benefit_record_date_recurring_label_single_frequency() -> None:
    benefit = _benefit(frequency_per_year=1, benefit_record_date_recurrence_months=[])
    label = resolve_benefit_record_date_recurring_label(benefit, 3)
    assert label is not None
    assert "3月末" in label
    assert "9月末" not in label


def test_benefit_record_date_recurring_label_semiannual_frequency() -> None:
    benefit = _benefit(frequency_per_year=2, benefit_record_date_recurrence_months=[])
    label = resolve_benefit_record_date_recurring_label(benefit, 3)
    assert label is not None
    assert "3月末" in label
    assert "9月末" in label


def test_benefit_record_date_recurring_label_none_when_no_benefit() -> None:
    assert resolve_benefit_record_date_recurring_label(None, 3) is None


def test_benefit_record_date_recurring_label_none_when_literal_date_exists() -> None:
    benefit = _benefit(
        benefit_record_dates=[dt.date(2027, 3, 31)],
        benefit_record_date_unknown_reason=None,
    )
    assert resolve_benefit_record_date_recurring_label(benefit, 3) is None


def test_benefit_recurring_label_prefers_registered_recurrence_over_unknown_reason() -> None:
    """2026-07修正の核心: 手動登録(CSV取込含む)でbenefit_record_datesが空でも
    recurrence_monthsが登録されていれば、unknown_reason(=SOURCE_NOT_FOUNDでも)に
    関わらず登録済み周期ラベルを優先する(優先度の低い理由コードで高い優先度の
    登録済みデータを覆い隠さない、というのが根本原因の修正)。2269相当のケース。
    """
    benefit = _benefit(
        benefit_record_date_recurrence_months=[3],
        benefit_record_date_unknown_reason=RecordDateUnknownReason.SOURCE_NOT_FOUND,
    )
    label = resolve_benefit_record_date_recurring_label(benefit, None)
    assert label is not None
    assert "3月末" in label
    assert "登録済みの権利確定周期に基づく" in label
    assert "推定" not in label  # 登録済みデータは「推定」の文言を使わない


def test_benefit_record_date_recurring_label_multiple_recurrence_months() -> None:
    """4680相当のケース: 年4回の登録済み周期がすべて表示される。"""
    benefit = _benefit(
        frequency_per_year=4,
        benefit_record_date_recurrence_months=[3, 6, 9, 12],
        benefit_record_date_unknown_reason=RecordDateUnknownReason.SOURCE_NOT_FOUND,
    )
    label = resolve_benefit_record_date_recurring_label(benefit, None)
    assert label is not None
    for month in (3, 6, 9, 12):
        assert f"{month}月末" in label


def test_benefit_record_date_recurring_label_falls_back_to_fiscal_year_end_inference() -> None:
    # 登録済み周期が無い場合のみ、従来通り決算期末からの推定にフォールバックする。
    benefit = _benefit(benefit_record_date_recurrence_months=[])
    label = resolve_benefit_record_date_recurring_label(benefit, 3)
    assert label is not None
    assert "一般的な慣行からの推定" in label


def test_benefit_record_date_recurring_label_none_when_nothing_available() -> None:
    benefit = _benefit(
        benefit_record_date_recurrence_months=[],
        benefit_record_date_unknown_reason=RecordDateUnknownReason.SOURCE_NOT_FOUND,
    )
    assert resolve_benefit_record_date_recurring_label(benefit, None) is None


# --- 情報源区分(SourceType)の解決 ---


def test_resolve_dividend_record_date_source_type_none_when_no_literal_date() -> None:
    dividend = _dividend()
    assert resolve_dividend_record_date_source_type(dividend) is None


def test_resolve_dividend_record_date_source_type_returns_source_when_literal_date() -> None:
    dividend = _dividend(dividend_record_dates=[dt.date(2027, 3, 31)])
    assert resolve_dividend_record_date_source_type(dividend) == SourceType.CONTRACTED_PROVIDER


def test_resolve_benefit_record_date_source_type_none_when_benefit_is_none() -> None:
    assert resolve_benefit_record_date_source_type(None) is None


def test_resolve_benefit_record_date_source_type_none_when_no_literal_date_or_recurrence() -> None:
    benefit = _benefit(benefit_record_date_recurrence_months=[])
    assert resolve_benefit_record_date_source_type(benefit) is None


def test_resolve_benefit_record_date_source_type_returns_source_for_literal_date() -> None:
    benefit = _benefit(benefit_record_dates=[dt.date(2027, 3, 31)])
    assert resolve_benefit_record_date_source_type(benefit) == SourceType.MANUAL_REGISTRY


def test_resolve_benefit_record_date_source_type_returns_source_for_registered_recurrence() -> (
    None
):
    """登録済み周期(実データ)がある場合もsource_typeを返す(自己推定と区別するため)。"""
    benefit = _benefit(benefit_record_date_recurrence_months=[3])
    assert resolve_benefit_record_date_source_type(benefit) == SourceType.MANUAL_REGISTRY
