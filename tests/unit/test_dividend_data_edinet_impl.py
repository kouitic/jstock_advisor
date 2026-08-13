import datetime as dt
from decimal import Decimal

from jstock_advisor.domain.entities.enums import DividendPeriodEndBasis
from jstock_advisor.providers.dividend_data.edinet_impl import EdinetDividendDataProvider

_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)


def _provider() -> EdinetDividendDataProvider:
    return EdinetDividendDataProvider(now=_NOW)


def test_current_period_uses_reported_basis_and_actual_period_end() -> None:
    """当期(REPORTED)のperiod_endはEDINET書類一覧APIの実測値をそのまま使い、
    period_startも正しく算出されること。"""
    provider = _provider()
    yearly_values = {"当期": Decimal("37"), "前期": Decimal("35")}

    actuals = provider._build_annual_dividend_actuals(  # noqa: SLF001
        yearly_values, "2026-03-31"
    )

    by_basis = {a.period_end_basis: a for a in actuals if a.period_end == dt.date(2026, 3, 31)}
    current = by_basis[DividendPeriodEndBasis.REPORTED]
    assert current.period_end == dt.date(2026, 3, 31)
    assert current.period_start == dt.date(2025, 4, 1)
    assert current.raw_dividend_per_share == Decimal("37")


def test_prior_periods_are_derived_from_relative_period() -> None:
    """前期以前4期分は当期から1年刻みで逆算した推定period_endであり、
    period_end_basis=DERIVED_FROM_RELATIVE_PERIODになること
    (当期のみREPORTED、それ以外は全てDERIVED_FROM_RELATIVE_PERIOD)。"""
    provider = _provider()
    yearly_values = {
        "四期前": Decimal("30"),
        "三期前": Decimal("31"),
        "前々期": Decimal("32"),
        "前期": Decimal("35"),
        "当期": Decimal("37"),
    }

    actuals = provider._build_annual_dividend_actuals(  # noqa: SLF001
        yearly_values, "2026-03-31"
    )

    by_end = {a.period_end: a for a in actuals}
    assert set(by_end) == {
        dt.date(2022, 3, 31),
        dt.date(2023, 3, 31),
        dt.date(2024, 3, 31),
        dt.date(2025, 3, 31),
        dt.date(2026, 3, 31),
    }
    assert by_end[dt.date(2026, 3, 31)].period_end_basis == DividendPeriodEndBasis.REPORTED
    for period_end in (
        dt.date(2025, 3, 31),
        dt.date(2024, 3, 31),
        dt.date(2023, 3, 31),
        dt.date(2022, 3, 31),
    ):
        assert (
            by_end[period_end].period_end_basis
            == DividendPeriodEndBasis.DERIVED_FROM_RELATIVE_PERIOD
        )
        assert by_end[period_end].period_start_is_estimated is True
    assert by_end[dt.date(2025, 3, 31)].raw_dividend_per_share == Decimal("35")
    assert by_end[dt.date(2022, 3, 31)].raw_dividend_per_share == Decimal("30")


def test_normalized_dividend_is_always_none_for_edinet() -> None:
    """EDINETは自己正規化しないため、normalized_dividend_per_share/
    normalization_basis_dateは常にNoneであること。"""
    provider = _provider()
    yearly_values = {"当期": Decimal("37")}

    actuals = provider._build_annual_dividend_actuals(  # noqa: SLF001
        yearly_values, "2026-03-31"
    )

    assert len(actuals) == 1
    assert actuals[0].normalized_dividend_per_share is None
    assert actuals[0].normalization_basis_date is None


def test_missing_latest_annual_period_end_returns_empty_list() -> None:
    provider = _provider()
    actuals = provider._build_annual_dividend_actuals(  # noqa: SLF001
        {"当期": Decimal("37")}, None
    )
    assert actuals == []
