"""コーポレートアクションproviderのfailure契約(Issue #59 Phase B2 / E-4)。

「イベントが無かった([])」と「確認できなかった(取得失敗)」を区別する。
従来はどちらも空リストで、取得失敗時に配当のクロスバリデーション補正が
無言でスキップされ得た。

E-4の判断: 失敗は例外契約(ProviderDataError)で表現できるため、
`CorporateActionAvailability` のような専用Result型は**導入しない**
(consumerがavailabilityを正常値として保持する必要が無いため。最小設計を優先)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from jstock_advisor.domain.entities.enums import CorporateActionType
from jstock_advisor.interfaces.provider_errors import ProviderDataError
from jstock_advisor.providers.corporate_action.yfinance_impl import (
    YFinanceCorporateActionProvider,
)

_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)
_SINCE = dt.date(2026, 1, 1)


class _SplitsTicker:
    def __init__(self, splits: pd.Series) -> None:
        self.splits = splits


class _RaisingSplitsTicker:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    @property
    def splits(self) -> pd.Series:
        raise self._exc


def _patch_ticker(monkeypatch: pytest.MonkeyPatch, ticker: object) -> None:
    import jstock_advisor.providers.corporate_action.yfinance_impl as module

    monkeypatch.setattr(module.yf, "Ticker", lambda symbol: ticker)


def test_success_with_no_event_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """SUCCESS + イベント無しは従来どおり空リスト(分割・併合なし)。"""
    _patch_ticker(monkeypatch, _SplitsTicker(pd.Series(dtype=float)))

    assert YFinanceCorporateActionProvider(now=_NOW).get_corporate_actions("7203", _SINCE) == []


def test_success_with_events_returns_event_list(monkeypatch: pytest.MonkeyPatch) -> None:
    splits = pd.Series([2.0], index=pd.to_datetime(["2026-04-01"]))
    _patch_ticker(monkeypatch, _SplitsTicker(splits))

    events = YFinanceCorporateActionProvider(now=_NOW).get_corporate_actions("7203", _SINCE)

    assert len(events) == 1
    assert events[0].event_type is CorporateActionType.SPLIT
    assert events[0].ratio == Decimal("2")


def test_failure_raises_instead_of_returning_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E-4: 取得失敗を「イベントなし」へ潰さない。"""
    original = RuntimeError("429 Too Many Requests")
    _patch_ticker(monkeypatch, _RaisingSplitsTicker(original))

    with pytest.raises(ProviderDataError) as excinfo:
        YFinanceCorporateActionProvider(now=_NOW).get_corporate_actions("7203", _SINCE)

    assert excinfo.value.provider_name == "yfinance"
    assert excinfo.value.operation == "splits"
    assert excinfo.value.retryable is True
    assert excinfo.value.__cause__ is original


def test_non_retryable_failure_is_also_propagated(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ticker(monkeypatch, _RaisingSplitsTicker(AttributeError("splits")))

    with pytest.raises(ProviderDataError) as excinfo:
        YFinanceCorporateActionProvider(now=_NOW).get_corporate_actions("7203", _SINCE)

    assert excinfo.value.retryable is False
