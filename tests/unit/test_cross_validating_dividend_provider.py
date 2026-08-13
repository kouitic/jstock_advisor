import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import (
    CorporateActionType,
    DividendPeriodEndBasis,
    DividendValidationStatus,
)
from jstock_advisor.interfaces.types import AnnualDividendActual, CorporateActionEvent, DividendInfo
from jstock_advisor.providers.dividend_data.cross_validating_impl import (
    CrossValidatingDividendDataProvider,
)
from jstock_advisor.services.corporate_action_service import CorporateActionService

_NOW = dt.datetime(2026, 7, 24, 7, 0, tzinfo=dt.UTC)
_BASIS_DATE = dt.date(2026, 7, 24)
_SOURCE_A = DataSourceReference(provider="yfinance", fetched_at=_NOW)
_SOURCE_B = DataSourceReference(provider="edinet", fetched_at=_NOW)
_CONFIG = load_config().data_validation


class _FixedDividendProvider:
    def __init__(self, info: DividendInfo | None) -> None:
        self._info = info

    def get_dividend_info(
        self, stock_code: str, fiscal_year_end_month: int | None = None
    ) -> DividendInfo | None:
        del fiscal_year_end_month
        return self._info


class _FixedCorporateActionProvider:
    """sinceによる事後フィルタを実際に行う(動的lookback窓のテストに必要)。"""

    def __init__(self, events: list[CorporateActionEvent]) -> None:
        self._events = events

    def get_corporate_actions(self, stock_code: str, since: dt.date) -> list[CorporateActionEvent]:
        return [e for e in self._events if e.effective_date is None or e.effective_date >= since]


def _service(events: list[CorporateActionEvent] | None = None) -> CorporateActionService:
    return CorporateActionService(_FixedCorporateActionProvider(events or []), now=_NOW)


def _split(effective_date: dt.date, ratio: str) -> CorporateActionEvent:
    return CorporateActionEvent(
        stock_code="8136",
        event_type=CorporateActionType.SPLIT,
        announced_date=effective_date,
        effective_date=effective_date,
        ratio=Decimal(ratio),
        source=_SOURCE_A,
    )


def _actual(
    period_end: dt.date,
    raw: str,
    *,
    normalized: str | None = None,
    basis_date: dt.date | None = None,
    basis: DividendPeriodEndBasis = DividendPeriodEndBasis.DERIVED_FROM_FISCAL_YEAR_END,
    period_start: dt.date | None = None,
) -> AnnualDividendActual:
    is_estimated = normalized is None
    return AnnualDividendActual(
        period_end=period_end,
        period_end_basis=basis,
        period_start=period_start or dt.date(period_end.year - 1, period_end.month, 1),
        period_start_is_estimated=is_estimated,
        raw_dividend_per_share=Decimal(raw),
        normalized_dividend_per_share=Decimal(normalized) if normalized is not None else None,
        normalization_basis_date=basis_date,
    )


def _primary_info(
    actuals: list[AnnualDividendActual], *, calendar_year_fallback_used: bool = False
) -> DividendInfo:
    latest = actuals[-1] if actuals else None
    return DividendInfo(
        stock_code="8136",
        fiscal_year="2026",
        actual_annual_dividend_per_share=latest.normalized_dividend_per_share if latest else None,
        source=_SOURCE_A,
        annual_dividend_actuals=actuals,
        calendar_year_fallback_used=calendar_year_fallback_used,
    )


def _secondary_info(actuals: list[AnnualDividendActual]) -> DividendInfo:
    latest = actuals[-1] if actuals else None
    return DividendInfo(
        stock_code="8136",
        fiscal_year="2026",
        actual_annual_dividend_per_share=latest.raw_dividend_per_share if latest else None,
        source=_SOURCE_B,
        annual_dividend_actuals=actuals,
    )


def _provider(
    primary: DividendInfo | None,
    secondary: DividendInfo | None,
    events: list[CorporateActionEvent] | None = None,
) -> CrossValidatingDividendDataProvider:
    return CrossValidatingDividendDataProvider(
        primary=_FixedDividendProvider(primary),
        secondary=_FixedDividendProvider(secondary),
        corporate_action_service=_service(events),
        config=_CONFIG,
        now=_NOW,
    )


def test_returns_none_when_primary_missing() -> None:
    provider = _provider(None, _secondary_info([_actual(dt.date(2025, 3, 31), "37")]))
    assert provider.get_dividend_info("8136") is None


def test_secondary_unavailable_uses_primary() -> None:
    primary_info = _primary_info(
        [_actual(dt.date(2025, 3, 31), "16", normalized="16", basis_date=_BASIS_DATE)]
    )
    provider = _provider(primary_info, None)
    result = provider.get_dividend_info("8136")
    assert result is not None
    assert result.actual_annual_dividend_per_share == Decimal("16")
    assert result.validation_status == DividendValidationStatus.SECONDARY_UNAVAILABLE


def test_filing_lag_validates_older_period_but_keeps_latest_value() -> None:
    """【有報提出タイムラグ】primaryにFY2026、secondaryにFY2025までしか無い場合、
    FY2025で検証されVALIDATEDとなり、返されるactual_annual_dividend_per_shareは
    FY2026(最新yfinance値)のままであること(最新値を捨てない)。"""
    primary_info = _primary_info(
        [
            _actual(dt.date(2025, 3, 31), "37", normalized="37", basis_date=_BASIS_DATE),
            _actual(dt.date(2026, 3, 31), "38", normalized="38", basis_date=_BASIS_DATE),
        ]
    )
    secondary_info = _secondary_info(
        [_actual(dt.date(2025, 3, 31), "37", basis=DividendPeriodEndBasis.REPORTED)]
    )
    provider = _provider(primary_info, secondary_info)

    result = provider.get_dividend_info("8136")

    assert result is not None
    assert result.validation_status == DividendValidationStatus.VALIDATED
    assert result.validated_period_end == dt.date(2025, 3, 31)
    assert result.actual_annual_dividend_per_share == Decimal("38")  # FY2026のまま


def test_no_common_period_is_not_yet_validatable_not_none() -> None:
    """【共通期間なし】primary・secondaryの期間が一切重ならない場合、
    NOT_YET_VALIDATABLEとなりNoneにはならず主データ源の値がそのまま利用可能。"""
    primary_info = _primary_info(
        [_actual(dt.date(2026, 3, 31), "38", normalized="38", basis_date=_BASIS_DATE)]
    )
    secondary_info = _secondary_info(
        [_actual(dt.date(2020, 3, 31), "10", basis=DividendPeriodEndBasis.REPORTED)]
    )
    provider = _provider(primary_info, secondary_info)

    result = provider.get_dividend_info("8136")

    assert result is not None
    assert result.validation_status == DividendValidationStatus.NOT_YET_VALIDATABLE
    assert result.validated_period_end is None
    assert result.actual_annual_dividend_per_share == Decimal("38")


def test_matched_reported_period_within_threshold_is_validated() -> None:
    """共通期間(REPORTED)が一致し閾値内なら従来通りVALIDATED(回帰確認)。"""
    primary_info = _primary_info(
        [_actual(dt.date(2025, 3, 31), "100", normalized="100", basis_date=_BASIS_DATE)]
    )
    secondary_info = _secondary_info(
        [_actual(dt.date(2025, 3, 31), "103", basis=DividendPeriodEndBasis.REPORTED)]
    )
    provider = _provider(primary_info, secondary_info)

    result = provider.get_dividend_info("8136")

    assert result is not None
    assert result.validation_status == DividendValidationStatus.VALIDATED
    assert result.validated_period_end == dt.date(2025, 3, 31)


def test_split_after_period_end_is_formally_normalized_and_validated() -> None:
    """【決算期終了後に分割】共通決算期の終了日より後にのみ分割がある場合、
    CorporateActionServiceで正式にbasis_dateへ正規化した結果が一致しVALIDATED。

    yfinance(primary)=16(分割調整済み)、EDINET(secondary)=80(額面ベース)は、
    naiveに直接比較すれば大きく乖離しているように見えるが、決算期終了後の
    5分割で正式に正規化すると80/5=16で一致する(基準が異なる値を直接
    _within_threshold()へ渡していないことの確認を兼ねる)。
    """
    period_end = dt.date(2024, 3, 31)
    primary_info = _primary_info(
        [
            _actual(
                period_end, "16", normalized="16", basis_date=_BASIS_DATE,
                period_start=dt.date(2023, 4, 1),
            )
        ]
    )
    secondary_info = _secondary_info(
        [_actual(period_end, "80", basis=DividendPeriodEndBasis.REPORTED)]
    )
    split_event = _split(dt.date(2025, 1, 1), "5")  # period_end後、basis_date前
    provider = _provider(primary_info, secondary_info, [split_event])

    result = provider.get_dividend_info("8136")

    assert result is not None
    assert result.validation_status == DividendValidationStatus.VALIDATED
    assert result.validated_period_end == period_end


def test_split_within_period_is_not_yet_validatable_and_not_excluded() -> None:
    """【決算期途中に分割】共通決算期の期間内(period_start〜period_end)に分割が
    ある場合、EDINET年間合計を単一倍率で正規化せずNOT_YET_VALIDATABLE・
    reason=corporate_action_within_dividend_periodとなること。Noneにはならず
    銘柄除外されない(最重要の新規テスト)。

    中間配当100円(分割前基準)+期末配当20円(分割後基準)= EDINET上の年間合計120円。
    これは単一倍率では正規化できない(100/5+20=40が正しいが、120/5=24でも
    120*5=600でもない)ため、比較自体を試みてはならない。
    """
    period_start = dt.date(2025, 4, 1)
    period_end = dt.date(2026, 3, 31)
    primary_info = _primary_info(
        [
            _actual(
                period_end, "40", normalized="40", basis_date=_BASIS_DATE,
                period_start=period_start,
            )
        ]
    )
    secondary_info = _secondary_info(
        [_actual(period_end, "120", basis=DividendPeriodEndBasis.REPORTED)]
    )
    split_event = _split(dt.date(2025, 10, 1), "5")  # period_start < effective_date <= period_end
    provider = _provider(primary_info, secondary_info, [split_event])

    result = provider.get_dividend_info("8136")

    assert result is not None
    assert result.validation_status == DividendValidationStatus.NOT_YET_VALIDATABLE
    assert result.actual_annual_dividend_per_share == Decimal("40")


def test_true_discrepancy_on_reported_period_without_split_returns_none() -> None:
    """共通期間(REPORTED)が一致し、期間内分割も無いのに正規化後もなお閾値超過の
    場合、従来通りNone(真の乖離)になること。"""
    period_end = dt.date(2025, 3, 31)
    primary_info = _primary_info(
        [_actual(period_end, "100", normalized="100", basis_date=_BASIS_DATE)]
    )
    secondary_info = _secondary_info(
        [_actual(period_end, "150", basis=DividendPeriodEndBasis.REPORTED)]
    )
    provider = _provider(primary_info, secondary_info, [])

    result = provider.get_dividend_info("8136")

    assert result is None


def test_discrepancy_on_estimated_period_is_not_yet_validatable_not_discrepancy() -> None:
    """共通期間の一致がEDINET側DERIVED_FROM_RELATIVE_PERIOD(推定期間)にのみ
    由来し、正規化後も説明不能な乖離がある場合、Noneではなく
    NOT_YET_VALIDATABLEになること。"""
    matched_end = dt.date(2023, 3, 31)
    primary_info = _primary_info(
        [_actual(matched_end, "50", normalized="50", basis_date=_BASIS_DATE)]
    )
    secondary_info = _secondary_info(
        [
            _actual(matched_end, "90", basis=DividendPeriodEndBasis.DERIVED_FROM_RELATIVE_PERIOD),
            # REPORTED(当期)はprimary側に無い期間(有報提出タイムラグ相当)
            _actual(dt.date(2026, 3, 31), "95", basis=DividendPeriodEndBasis.REPORTED),
        ]
    )
    provider = _provider(primary_info, secondary_info, [])

    result = provider.get_dividend_info("8136")

    assert result is not None
    assert result.validation_status == DividendValidationStatus.NOT_YET_VALIDATABLE


def test_calendar_year_fallback_never_validated_even_on_period_match() -> None:
    """fiscal_year_end_month不明(暦年フォールバック)の場合、EDINETのperiod_endと
    偶然一致してもクロスバリデーション自体を行わず常にNOT_YET_VALIDATABLEに
    なること。"""
    period_end = dt.date(2025, 12, 31)
    primary_info = _primary_info(
        [_actual(period_end, "50", normalized="50", basis_date=_BASIS_DATE)],
        calendar_year_fallback_used=True,
    )
    secondary_info = _secondary_info(
        [_actual(period_end, "50", basis=DividendPeriodEndBasis.REPORTED)]
    )
    provider = _provider(primary_info, secondary_info, [])

    result = provider.get_dividend_info("8136")

    assert result is not None
    assert result.validation_status == DividendValidationStatus.NOT_YET_VALIDATABLE
    assert result.validated_period_end is None


def test_split_lookback_window_extends_to_validated_period_start() -> None:
    """【株式分割検索窓の動的拡張】最新共通決算期が4年以上前、分割はその後
    3年以上前(固定1095日設定では捕捉できない古さ)に発生、以降追加分割なし
    というケースで、新方式では検出し正式に正規化してVALIDATEDにできること。
    """
    period_end = dt.date(2022, 3, 31)  # _NOW(2026-07-24)から見て4年以上前
    period_start = dt.date(2021, 4, 1)
    primary_info = _primary_info(
        [
            _actual(
                period_end, "20", normalized="20", basis_date=_BASIS_DATE,
                period_start=period_start,
            )
        ]
    )
    secondary_info = _secondary_info(
        [_actual(period_end, "100", basis=DividendPeriodEndBasis.REPORTED)]
    )
    # 2023-01-15は_NOW-1095日(約2023-07-26)より古く、固定lookbackでは捕捉できない
    split_event = _split(dt.date(2023, 1, 15), "5")
    assert split_event.effective_date is not None
    assert split_event.effective_date < _NOW.date() - dt.timedelta(days=1095)

    provider = _provider(primary_info, secondary_info, [split_event])

    result = provider.get_dividend_info("8136")

    assert result is not None
    assert result.validation_status == DividendValidationStatus.VALIDATED


def test_discrepancy_status_is_not_part_of_public_enum() -> None:
    """真の乖離はwarningログ+Noneでのみ表現され、DividendValidationStatusには
    観測不可能なDISCREPANCY相当の値を含めない(API契約との整合性)。"""
    assert "DISCREPANCY" not in DividendValidationStatus.__members__


def test_4193_pattern_period_alignment_resolves_apparent_discrepancy() -> None:
    """4193パターン: 3月決算、暦年集計では56.0 vs 38.00に見えていた乖離が、
    決算期単位で正しく期間を揃えると解消すること。"""
    primary_info = _primary_info(
        [
            _actual(dt.date(2025, 3, 31), "37", normalized="37", basis_date=_BASIS_DATE),
            _actual(dt.date(2026, 3, 31), "38", normalized="38", basis_date=_BASIS_DATE),
        ]
    )
    secondary_info = _secondary_info(
        [_actual(dt.date(2025, 3, 31), "37", basis=DividendPeriodEndBasis.REPORTED)]
    )
    provider = _provider(primary_info, secondary_info, [])

    result = provider.get_dividend_info("8136")

    assert result is not None
    assert result.validation_status == DividendValidationStatus.VALIDATED
    assert result.validated_period_end == dt.date(2025, 3, 31)


def test_5195_pattern_period_alignment_resolves_apparent_discrepancy() -> None:
    """5195パターン: 中間配当と期末配当が誤った暦年に混在して78 vs 120に見えて
    いた乖離が、決算期単位の正しい合算・期間整合で解消すること
    (中間+期末の正しい合算そのものはyfinance_impl側のテストで確認済み)。"""
    primary_info = _primary_info(
        [_actual(dt.date(2026, 3, 31), "120", normalized="120", basis_date=_BASIS_DATE)]
    )
    secondary_info = _secondary_info(
        [_actual(dt.date(2026, 3, 31), "120", basis=DividendPeriodEndBasis.REPORTED)]
    )
    provider = _provider(primary_info, secondary_info, [])

    result = provider.get_dividend_info("8136")

    assert result is not None
    assert result.validation_status == DividendValidationStatus.VALIDATED


def test_8136_pattern_split_after_period_end_is_validated() -> None:
    """8136パターン(期間終了後分割のみ): サンリオを模した複数分割銘柄で、
    共通決算期の終了後にのみ分割がある場合はVALIDATEDになること。"""
    period_end = dt.date(2024, 3, 31)
    primary_info = _primary_info(
        [
            _actual(
                period_end, "16", normalized="16", basis_date=_BASIS_DATE,
                period_start=dt.date(2023, 4, 1),
            )
        ]
    )
    secondary_info = _secondary_info(
        [_actual(period_end, "80", basis=DividendPeriodEndBasis.REPORTED)]
    )
    split_event = _split(dt.date(2025, 6, 1), "5")  # period_end後
    provider = _provider(primary_info, secondary_info, [split_event])

    result = provider.get_dividend_info("8136")

    assert result is not None
    assert result.validation_status == DividendValidationStatus.VALIDATED


def test_8136_pattern_split_within_period_is_not_yet_validatable() -> None:
    """8136パターン(期間内分割あり): 共通決算期の期間内に分割がある場合、
    累積倍率で機械的に一致させてVALIDATEDと決め打ちせず
    NOT_YET_VALIDATABLEになること。"""
    period_start = dt.date(2023, 4, 1)
    period_end = dt.date(2024, 3, 31)
    primary_info = _primary_info(
        [
            _actual(
                period_end, "16", normalized="16", basis_date=_BASIS_DATE,
                period_start=period_start,
            )
        ]
    )
    secondary_info = _secondary_info(
        [_actual(period_end, "80", basis=DividendPeriodEndBasis.REPORTED)]
    )
    split_event = _split(dt.date(2023, 9, 1), "5")  # period_start < effective_date <= period_end
    provider = _provider(primary_info, secondary_info, [split_event])

    result = provider.get_dividend_info("8136")

    assert result is not None
    assert result.validation_status == DividendValidationStatus.NOT_YET_VALIDATABLE
