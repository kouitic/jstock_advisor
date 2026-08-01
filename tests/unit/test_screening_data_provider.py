import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest

from jstock_advisor.services import screening_data_provider as sdp_module
from jstock_advisor.services.screening_data_provider import (
    ScreeningDataStatus,
    StockSnapshotScreeningDataProvider,
)

_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)


def _fake_snapshot(**overrides: object) -> SimpleNamespace:
    financial = SimpleNamespace(
        stock_name="テスト株式会社",
        security_type="STOCK",
        sector="Consumer",
        industry="Retail",
        shares_outstanding=Decimal("1000000"),
        forecast_eps=Decimal("100"),
        forecast_bps=Decimal("2000"),
        equity_ratio_pct=55.0,
        operating_cashflow=Decimal("500000000"),
        payout_ratio_pct=35.0,
        is_debt_excess=False,
        is_deficit=False,
        is_going_concern_doubt=False,
    )
    dividend = SimpleNamespace(
        consecutive_dividend_increase_years=3,
        is_dividend_cut_announced=False,
        is_dividend_omission_announced=False,
    )
    defaults: dict[str, object] = {
        "stock_code": "1234",
        "current_price": Decimal("3000"),
        "financial": financial,
        "dividend": dividend,
        "benefit": None,
        "dividend_yield_pct": 4.0,
        "benefit_yield_pct": None,
        "next_earnings_date": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeProviders:
    pass


def _make_provider() -> StockSnapshotScreeningDataProvider:
    return StockSnapshotScreeningDataProvider(_FakeProviders(), object())  # type: ignore[arg-type]


def test_ok_status_builds_input_with_computed_market_cap_and_valuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sdp_module,
        "build_stock_snapshot",
        lambda *a, **kw: (_fake_snapshot(benefit_yield_pct=1.0), None),
    )
    result = _make_provider().get_screening_input("1234", _NOW)

    assert result.status == ScreeningDataStatus.OK
    assert result.input is not None
    assert result.input.stock_code == "1234"
    assert result.input.market_cap == Decimal("1000000") * Decimal("3000")
    assert result.input.current_per == Decimal("3000") / Decimal("100")
    assert result.input.current_pbr == Decimal("3000") / Decimal("2000")
    assert result.missing_fields == []
    assert result.error_message is None


def test_not_found_status_when_snapshot_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sdp_module,
        "build_stock_snapshot",
        lambda *a, **kw: (None, "株価データを取得できません"),
    )
    result = _make_provider().get_screening_input("1234", _NOW)

    assert result.status == ScreeningDataStatus.NOT_FOUND
    assert result.input is None
    assert result.error_message == "株価データを取得できません"


def test_data_error_status_when_provider_raises_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("network error")

    monkeypatch.setattr(sdp_module, "build_stock_snapshot", _raise)
    result = _make_provider().get_screening_input("1234", _NOW)

    assert result.status == ScreeningDataStatus.DATA_ERROR
    assert result.input is None
    assert result.error_message is not None
    assert "network error" in result.error_message


def test_missing_shares_outstanding_marks_required_field_missing_and_no_market_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _fake_snapshot()
    snap.financial.shares_outstanding = None
    monkeypatch.setattr(sdp_module, "build_stock_snapshot", lambda *a, **kw: (snap, None))

    result = _make_provider().get_screening_input("1234", _NOW)

    assert result.status == ScreeningDataStatus.OK
    assert result.input is not None
    assert result.input.market_cap is None
    assert "shares_outstanding" in result.input.missing_required_fields
    assert "shares_outstanding" in result.missing_fields


def test_missing_operating_cashflow_marks_required_field_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _fake_snapshot()
    snap.financial.operating_cashflow = None
    monkeypatch.setattr(sdp_module, "build_stock_snapshot", lambda *a, **kw: (snap, None))

    result = _make_provider().get_screening_input("1234", _NOW)

    assert result.input is not None
    assert result.input.missing_required_fields == ["operating_cashflow"]


def test_missing_scoring_fields_are_reported_separately_from_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # benefit=None(デフォルト、優待制度自体が無い)のため、
    # shareholder_benefit_yield_pct=Noneは欠損として数えない(運用ハードニング
    # 第2弾4節、下のtest_shareholder_benefit_yield_missing_*で区別を確認する)。
    snap = _fake_snapshot(dividend_yield_pct=None, benefit_yield_pct=None)
    snap.financial.equity_ratio_pct = None
    monkeypatch.setattr(sdp_module, "build_stock_snapshot", lambda *a, **kw: (snap, None))

    result = _make_provider().get_screening_input("1234", _NOW)

    assert result.input is not None
    assert result.input.missing_required_fields == []
    assert set(result.input.missing_scoring_fields) == {
        "dividend_yield_pct",
        "equity_ratio_pct",
    }
    assert set(result.missing_fields) == set(result.input.missing_scoring_fields)


def test_shareholder_benefit_yield_missing_without_benefit_program_is_not_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """運用ハードニング第2弾4節: 優待制度自体が無い銘柄(benefit=None)は
    shareholder_benefit_yield_pct=Noneでも欠損として数えないこと
    (max_missing_fieldsによる誤ったDATA_INSUFFICIENT除外を防ぐための修正)。
    """
    snap = _fake_snapshot(benefit=None, benefit_yield_pct=None)
    monkeypatch.setattr(sdp_module, "build_stock_snapshot", lambda *a, **kw: (snap, None))

    result = _make_provider().get_screening_input("1234", _NOW)

    assert result.input is not None
    assert "shareholder_benefit_yield_pct" not in result.input.missing_scoring_fields


def test_shareholder_benefit_yield_missing_with_benefit_program_is_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """優待制度はあるが利回りが算出できない場合は、引き続き欠損として扱うこと。"""
    snap = _fake_snapshot(benefit=SimpleNamespace(), benefit_yield_pct=None)
    monkeypatch.setattr(sdp_module, "build_stock_snapshot", lambda *a, **kw: (snap, None))

    result = _make_provider().get_screening_input("1234", _NOW)

    assert result.input is not None
    assert "shareholder_benefit_yield_pct" in result.input.missing_scoring_fields


def test_shareholder_benefit_exists_flag_reflects_benefit_presence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _fake_snapshot(benefit=SimpleNamespace())
    monkeypatch.setattr(sdp_module, "build_stock_snapshot", lambda *a, **kw: (snap, None))

    result = _make_provider().get_screening_input("1234", _NOW)

    assert result.input is not None
    assert result.input.shareholder_benefit_exists is True
