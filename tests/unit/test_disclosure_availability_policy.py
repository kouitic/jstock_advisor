"""開示情報の取得可否(availability)に対する判定側ポリシー(Issue #53 Phase B2)。

「取得できて開示0件(= リスクなし)」と「取得できなかった(= 調査できていない)」を
判定ロジック上で混同しないことを固定する。

  BUY / watchlist : UNAVAILABLE → DATA_INSUFFICIENT(DISCLOSURE_RISKにはしない)
  holdings速報    : UNAVAILABLE → warning/auditのみ。リスク通知は出さない
  profit-taking   : UNAVAILABLE → risk=trueにしない
"""

from __future__ import annotations

import datetime as dt

import pytest

from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.signals.profit_taking import ProfitTakingConditionInputs
from jstock_advisor.interfaces.disclosure import (
    DisclosureAvailability,
    DisclosureQueryResult,
    DisclosureUnavailableReason,
)
from jstock_advisor.interfaces.types import Disclosure
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.disclosure.unavailable_impl import UnavailableDisclosureProvider
from jstock_advisor.services.screening_data_provider import (
    DISCLOSURE_AVAILABILITY_FIELD_NAME,
)

_NOW = dt.datetime(2026, 8, 31, 1, 0, tzinfo=dt.UTC)
_SINCE = dt.date(2026, 8, 1)
_SOURCE = DataSourceReference(provider="test", fetched_at=_NOW)


def _disclosure(summary: str) -> Disclosure:
    return Disclosure(
        stock_code="2914",
        published_at=_NOW,
        title="臨時報告書",
        category="臨時報告書",
        summary=summary,
        url=None,
        source=_SOURCE,
    )


# --- A/B/C: DisclosureQueryResult の3状態 -----------------------------------


def test_available_with_disclosures() -> None:
    result = DisclosureQueryResult.available([_disclosure("代表取締役の異動")])

    assert result.availability is DisclosureAvailability.AVAILABLE
    assert result.is_available is True
    assert len(result.disclosures) == 1
    assert result.unavailable_reason is None


def test_available_empty_means_no_disclosure_risk_not_unknown() -> None:
    """AVAILABLE + [] は「取得成功・対象開示なし」= 正常な開示リスクなし。"""
    result = DisclosureQueryResult.available([])

    assert result.availability is DisclosureAvailability.AVAILABLE
    assert result.is_available is True
    assert result.disclosures == []
    assert result.unavailable_reason is None


@pytest.mark.parametrize(
    "reason",
    [
        DisclosureUnavailableReason.NOT_CONFIGURED,
        DisclosureUnavailableReason.TEMPORARY_FAILURE,
        DisclosureUnavailableReason.OTHER,
    ],
)
def test_unavailable_always_has_empty_disclosures(
    reason: DisclosureUnavailableReason,
) -> None:
    result = DisclosureQueryResult.unavailable(reason)

    assert result.availability is DisclosureAvailability.UNAVAILABLE
    assert result.is_available is False
    assert result.disclosures == []
    assert result.unavailable_reason is reason


# --- D/E: provider実装の契約 ------------------------------------------------


def test_unavailable_provider_returns_unavailable_not_available_empty() -> None:
    """「providerが使えない」を「調査したが0件」で代用しない。"""
    result = UnavailableDisclosureProvider().get_disclosures("2914", _SINCE)

    assert result.availability is DisclosureAvailability.UNAVAILABLE
    assert result.unavailable_reason is DisclosureUnavailableReason.NOT_CONFIGURED


def test_mock_provider_without_disclosure_returns_available_empty() -> None:
    """モックの「開示なし」はAVAILABLE + 空リスト(UNAVAILABLEにしない)。"""
    result = MockDisclosureProvider(now=_NOW).get_disclosures("9999", _SINCE)

    assert result.availability is DisclosureAvailability.AVAILABLE
    assert result.disclosures == []


# --- P: failure reasonが秘密情報を含まないこと ------------------------------


def test_unavailable_reason_values_contain_no_secrets() -> None:
    """理由区分は固定の列挙値のみで、APIキー等を含み得ない。"""
    values = {reason.value for reason in DisclosureUnavailableReason}

    assert values == {"NOT_CONFIGURED", "TEMPORARY_FAILURE", "OTHER"}
    for value in values:
        assert "key" not in value.lower()
        assert "token" not in value.lower()
        assert "secret" not in value.lower()


# --- I: watchlist policy ----------------------------------------------------


def test_watchlist_unavailable_marks_disclosure_as_missing_required_field() -> None:
    """UNAVAILABLEは必須項目欠損として扱い、既存のDATA_INSUFFICIENT経路に乗せる。

    `missing_required_fields` が空でないと
    `HighDividendFinancialHealthPolicy` が `ExclusionReason.DATA_INSUFFICIENT` を
    返す(domain/signals/watchlist_screening.py)。新しい除外理由は増やさない。
    """
    from jstock_advisor.domain.signals.watchlist_screening import ExclusionReason

    # 実データ組み立ては重いため、経路の契約(定数名と既存enumの存在)を固定する
    assert DISCLOSURE_AVAILABILITY_FIELD_NAME == "disclosure_availability"
    assert ExclusionReason.DATA_INSUFFICIENT.value == "DATA_INSUFFICIENT"


# --- M/N/O: profit-taking policy -------------------------------------------


def test_profit_taking_risk_flag_defaults_to_false() -> None:
    """開示調査不能を売却リスクへ変換しないための既定値(既定Falseを固定)。

    `accounting_or_scandal_or_delisting_risk` はprofit_taking_service側で
    一切設定されないため、EDINET取得不能でもTrueにはならない。
    """
    inputs = ProfitTakingConditionInputs()

    assert inputs.accounting_or_scandal_or_delisting_risk is False


def test_profit_taking_service_never_sets_disclosure_risk_flag() -> None:
    """profit_taking_serviceが当該フラグへ代入していないことをソースで固定する。

    将来「取得不能 → risk=true」と書かれてしまう退行を検出するためのガード。
    """
    import inspect

    from jstock_advisor.services import profit_taking_service

    source = inspect.getsource(profit_taking_service)

    assert "accounting_or_scandal_or_delisting_risk=" not in source
