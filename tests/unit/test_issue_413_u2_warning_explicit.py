"""Issue #494(#413 U-2): cross_validating_impl の logger level を WARNING で明示する(意図した静音)。

`cross_validating_impl` の INFO は「候補ごとの条件付き」で高頻度になりうるため、当初設計
(#413 issuecomment-5738498397 §2)は INFO を有効化せず、**`setLevel(logging.WARNING)` と理由で
明示する**とした。MANAGER が 2026-09-20 に、INFO の有効化(私の提案)を当初設計へ訂正した。
ここでは次を確認する。

    1 宣言が実際に効く: Lambda の root logger の既定(WARNING)のもとで、INFO は無効・WARNING は有効。
    2 意図した静音: INFO の 5 か所(全経路)を実際に通しても、INFO は出力されない。
      root logger を INFO にしても出ない(module の宣言が効いている)。
    3 静音にした情報が失われない: 各 INFO が伝えていた状態は、返り値の `validation_status` に残る。
    4 既存の WARNING が変わっていない(共通決算期で正規化後も乖離した場合。文面・level・値)。

宣言があること自体は tests/unit/test_issue_413_logger_level_declared.py(#413 の guard)が見る。
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import (
    CorporateActionType,
    DividendPeriodEndBasis,
    DividendValidationStatus,
)
from jstock_advisor.interfaces.types import AnnualDividendActual, CorporateActionEvent, DividendInfo
from jstock_advisor.providers.dividend_data import cross_validating_impl
from jstock_advisor.providers.dividend_data.cross_validating_impl import (
    CrossValidatingDividendDataProvider,
)
from jstock_advisor.services.corporate_action_service import CorporateActionService

_MODULE = cross_validating_impl.__name__
_NOW = dt.datetime(2026, 7, 24, 7, 0, tzinfo=dt.UTC)
_BASIS_DATE = dt.date(2026, 7, 24)
_SOURCE_A = DataSourceReference(provider="yfinance", fetched_at=_NOW)
_SOURCE_B = DataSourceReference(provider="edinet", fetched_at=_NOW)
_CONFIG = load_config().data_validation
_CODE = "0000"  # 架空の銘柄コード
_REPORTED = DividendPeriodEndBasis.REPORTED
_NYV = DividendValidationStatus.NOT_YET_VALIDATABLE


class _FixedDividendProvider:
    def __init__(self, info: DividendInfo | None) -> None:
        self._info = info

    def get_dividend_info(
        self, stock_code: str, fiscal_year_end_month: int | None = None
    ) -> DividendInfo | None:
        del stock_code, fiscal_year_end_month
        return self._info


class _FixedCorporateActionProvider:
    def __init__(self, events: list[CorporateActionEvent]) -> None:
        self._events = events

    def get_corporate_actions(self, stock_code: str, since: dt.date) -> list[CorporateActionEvent]:
        return [e for e in self._events if e.effective_date is None or e.effective_date >= since]


def _split(effective_date: dt.date, ratio: str) -> CorporateActionEvent:
    return CorporateActionEvent(
        stock_code=_CODE,
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
    basis: DividendPeriodEndBasis = DividendPeriodEndBasis.DERIVED_FROM_FISCAL_YEAR_END,
    period_start: dt.date | None = None,
) -> AnnualDividendActual:
    return AnnualDividendActual(
        period_end=period_end,
        period_end_basis=basis,
        period_start=period_start or dt.date(period_end.year - 1, period_end.month, 1),
        period_start_is_estimated=normalized is None,
        raw_dividend_per_share=Decimal(raw),
        normalized_dividend_per_share=Decimal(normalized) if normalized is not None else None,
        normalization_basis_date=_BASIS_DATE if normalized is not None else None,
    )


def _primary(actuals: list[AnnualDividendActual], *, fallback: bool = False) -> DividendInfo:
    latest = actuals[-1]
    return DividendInfo(
        stock_code=_CODE,
        fiscal_year="2026",
        actual_annual_dividend_per_share=latest.normalized_dividend_per_share,
        source=_SOURCE_A,
        annual_dividend_actuals=actuals,
        calendar_year_fallback_used=fallback,
    )


def _secondary(actuals: list[AnnualDividendActual]) -> DividendInfo:
    return DividendInfo(
        stock_code=_CODE,
        fiscal_year="2026",
        actual_annual_dividend_per_share=actuals[-1].raw_dividend_per_share,
        source=_SOURCE_B,
        annual_dividend_actuals=actuals,
    )


def _provider(
    primary: DividendInfo, secondary: DividendInfo, events: list[CorporateActionEvent] | None = None
) -> CrossValidatingDividendDataProvider:
    return CrossValidatingDividendDataProvider(
        primary=_FixedDividendProvider(primary),
        secondary=_FixedDividendProvider(secondary),
        corporate_action_service=CorporateActionService(
            _FixedCorporateActionProvider(events or []), now=_NOW
        ),
        config=_CONFIG,
        now=_NOW,
    )


# INFO の 5 か所を、それぞれ実際に通すシナリオ(id, provider, 期待する validation_status)
def _scenarios() -> list[tuple[str, CrossValidatingDividendDataProvider, DividendValidationStatus]]:
    p31 = dt.date(2025, 3, 31)
    return [
        (
            "calendar_year_fallback",  # 1 fiscal_year_end_month 不明
            _provider(
                _primary([_actual(p31, "50", normalized="50")], fallback=True),
                _secondary([_actual(p31, "50", basis=_REPORTED)]),
            ),
            _NYV,
        ),
        (
            "no_common_period",  # 2 共通決算期なし
            _provider(
                _primary([_actual(dt.date(2026, 3, 31), "38", normalized="38")]),
                _secondary([_actual(dt.date(2020, 3, 31), "10", basis=_REPORTED)]),
            ),
            _NYV,
        ),
        (
            "split_within_period",  # 3 決算期内の分割
            _provider(
                _primary(
                    [
                        _actual(
                            dt.date(2026, 3, 31),
                            "40",
                            normalized="40",
                            period_start=dt.date(2025, 4, 1),
                        )
                    ]
                ),
                _secondary([_actual(dt.date(2026, 3, 31), "120", basis=_REPORTED)]),
                [_split(dt.date(2025, 10, 1), "5")],
            ),
            _NYV,
        ),
        (
            "validated",  # 4 検証成功
            _provider(
                _primary([_actual(p31, "100", normalized="100")]),
                _secondary([_actual(p31, "103", basis=_REPORTED)]),
            ),
            DividendValidationStatus.VALIDATED,
        ),
        (
            "discrepancy_on_estimated_period",  # 5 推定期間での乖離
            _provider(
                _primary([_actual(dt.date(2023, 3, 31), "50", normalized="50")]),
                _secondary(
                    [
                        _actual(
                            dt.date(2023, 3, 31),
                            "90",
                            basis=DividendPeriodEndBasis.DERIVED_FROM_RELATIVE_PERIOD,
                        ),
                        _actual(dt.date(2026, 3, 31), "95", basis=_REPORTED),
                    ]
                ),
            ),
            _NYV,
        ),
    ]


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _MODULE]


def test_declared_level_takes_effect_under_the_lambda_root_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lambda の root(WARNING)のもとで、INFO は無効・WARNING は有効(宣言が効いている)。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)

    logger = logging.getLogger(_MODULE)
    assert logger.level == logging.WARNING  # 明示されている(NOTSET ではない)
    assert not logger.isEnabledFor(logging.INFO)
    assert logger.isEnabledFor(logging.WARNING)


@pytest.mark.parametrize("scenario_id", [s[0] for s in _scenarios()])
def test_info_paths_are_silent_even_when_the_root_logger_is_at_info(
    scenario_id: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """意図した静音: root を INFO にしても、この module の INFO は出ない。状態は返り値に残る。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.INFO)
    _, provider, expected_status = next(s for s in _scenarios() if s[0] == scenario_id)

    with caplog.at_level(logging.INFO):  # root と handler を INFO にする(module は変えない)
        result = provider.get_dividend_info(_CODE)

    assert result is not None
    assert result.validation_status == expected_status  # 静音にした情報はデータに残る
    assert _records(caplog) == []  # INFO は出ない(WARNING も、この経路では出ない)


def test_the_existing_warning_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """共通決算期(REPORTED)で、期間内分割が無く、正規化後も乖離する場合は WARNING(従来どおり)。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    p31 = dt.date(2025, 3, 31)
    provider = _provider(
        _primary([_actual(p31, "100", normalized="100")]),
        _secondary([_actual(p31, "150", basis=_REPORTED)]),
    )

    result = provider.get_dividend_info(_CODE)

    assert result is None  # 真の乖離 = 取得不可(挙動は不変)
    warnings = [r for r in _records(caplog) if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "dividend values disagree for same reported fiscal period" in message
    assert f"stock_code={_CODE}" in message
    assert "discrepancy_pct=" in message


def test_every_info_call_site_is_reached_by_a_scenario() -> None:
    """検査の網羅: この module の INFO は 5 か所で、シナリオも 5 つ(INFO が増えたら赤くなる)。"""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(cross_validating_impl.__file__).read_text(encoding="utf-8"))
    infos = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "info"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "logger"
    ]
    assert len(infos) == len(_scenarios()) == 5
