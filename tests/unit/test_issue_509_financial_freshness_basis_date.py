"""Issue #509(P1・design-defect): 財務鮮度A/Bの基準日を統一する。

## 背景

`domain/financial_freshness.py`の鮮度判定(以下A。`assess_financial_freshness()`
経由)は、最新財務期間末の解決に`resolve_latest_financial_period_end()`
(`domain/signals/earnings_window.py`)を使い、四半期実績(recent_quarters)が
あればそれを優先する。

一方、`ProfitTakingService._fair_value_reflects_latest_earnings()`(以下B。
適正価格の算出根拠が最新決算を反映しているかの代理指標。400日以内かを見る)は、
`snapshot.financial.fiscal_period_end`(年次決算期末)を**直接参照**していた。
年次決算後に四半期決算が新たに発表されていても、Bはそれを検知できず
(直接参照は年次決算期末のまま古い値を返す)、AとBが食い違う経路があった
(Issue #509 Phase A調査で確認)。

## 修正内容

BをAと同じ`resolve_latest_financial_period_end()`経由へ統一した。400日
しきい値・age_daysの算出方法(`data_fetched_at`基準)自体は変更していない
(基準日の解決方法のみを是正する)。

## 本moduleが固定しないこと

- 400日しきい値の妥当性(#509では変更しない。USER決定)。
- `evaluate_financial_freshness()`(A本体)の判定契約自体
  (`tests/unit/test_issue_52_phase_b3_a_financial_freshness.py`が正本)。
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import AccountType, RecentPeriodsSource
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.interfaces.types import FinancialSummary, QuarterlyFinancials
from jstock_advisor.providers.corporate_action.mock_impl import MockCorporateActionProvider
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.dividend_data.mock_impl import MockDividendDataProvider
from jstock_advisor.providers.financial_data.mock_impl import MockFinancialDataProvider
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.shareholder_benefit.mock_impl import MockShareholderBenefitProvider
from jstock_advisor.services import profit_taking_service as pt_module
from jstock_advisor.services.profit_taking_service import ProfitTakingConditionInputs
from jstock_advisor.services.provider_bundle import ProviderBundle

_CONFIG = load_config()
_STOCK_CODE = "2914"
# 十分未来にして「決算予定日を経過した」系の抑制分岐へ入らないようにする
# (本テストの関心事はfair_value_reflects_latest_earnthingsの値そのもの)。
_NOW = dt.datetime(2026, 9, 25, 7, 0, tzinfo=dt.UTC)
_EVALUATION_DATE = dt.date(2026, 9, 25)
_TEST_FINANCIAL_SOURCE = DataSourceReference(provider="test-fixture", fetched_at=_NOW)


class _FixedFinancialPeriodFinancialDataProvider:
    """fiscal_period_end・recent_quarters・fetched_atを固定値で上書きするフェイク。

    test_profit_taking_service.pyの同名helperと同じ設計(重複実装だが、
    Issueスコープのfileを自己完結させるため、このfile内でのみ定義する)。
    """

    def __init__(
        self,
        delegate: object,
        fiscal_period_end: dt.date | None,
        recent_quarters: Sequence[QuarterlyFinancials] = (),
        fetched_at: dt.datetime | None = None,
    ) -> None:
        self._delegate = delegate
        self._fiscal_period_end = fiscal_period_end
        self._recent_quarters = list(recent_quarters)
        self._fetched_at = fetched_at

    def get_financial_summary(self, stock_code: str) -> FinancialSummary | None:
        summary = self._delegate.get_financial_summary(stock_code)  # type: ignore[attr-defined]
        if summary is None:
            return None
        update: dict[str, object] = {
            "fiscal_period_end": self._fiscal_period_end,
            "recent_quarters": self._recent_quarters,
            "recent_periods_source": (
                RecentPeriodsSource.QUARTERLY
                if self._recent_quarters
                else RecentPeriodsSource.UNAVAILABLE
            ),
        }
        if self._fetched_at is not None:
            update["source"] = summary.source.model_copy(update={"fetched_at": self._fetched_at})
        return summary.model_copy(update=update)

    def get_historical_valuation(self, stock_code: str, years: int) -> list[object]:
        return self._delegate.get_historical_valuation(stock_code, years)  # type: ignore[attr-defined]

    def get_cashflow_decomposition(self, stock_code: str) -> object | None:
        return self._delegate.get_cashflow_decomposition(stock_code)  # type: ignore[attr-defined]

    def get_earnings_surprise_history(self, stock_code: str) -> list[object]:
        return self._delegate.get_earnings_surprise_history(stock_code)  # type: ignore[attr-defined]


def _quarter(quarter_end: dt.date, stock_code: str = _STOCK_CODE) -> QuarterlyFinancials:
    return QuarterlyFinancials(
        stock_code=stock_code, quarter_end=quarter_end, source=_TEST_FINANCIAL_SOURCE
    )


def _providers(
    fiscal_period_end: dt.date | None,
    recent_quarters: Sequence[QuarterlyFinancials] = (),
    fetched_at: dt.datetime | None = None,
    now: dt.datetime = _NOW,
    providers_now: dt.datetime | None = None,
) -> ProviderBundle:
    """`providers_now`は他のMock provider(market/dividend/benefit/disclosure)の
    構築に使う`now`を、`analyze()`へ渡す`now`(evaluation_date解決に使う)から
    独立させたい場合のみ指定する(既定はnowと同じ)。`StockSnapshot.data_fetched_at`
    は`min(全sourceのfetched_at)`のため、これらのMock providerのfetched_atが
    financial providerのfetched_at(`fetched_at`引数)より前だと、期待しない
    値がdata_fetched_atへ紛れ込む(T7のUTC/JST境界テストでの実測で判明)。
    """
    build_now = providers_now if providers_now is not None else now
    base = ProviderBundle(
        market_data=MockMarketDataProvider(now=build_now),
        financial_data=MockFinancialDataProvider(now=build_now),
        dividend_data=MockDividendDataProvider(now=build_now),
        shareholder_benefit=MockShareholderBenefitProvider(now=build_now),
        disclosure=MockDisclosureProvider(now=build_now),
        corporate_action=MockCorporateActionProvider(),
    )
    return dataclasses.replace(
        base,
        financial_data=_FixedFinancialPeriodFinancialDataProvider(
            base.financial_data, fiscal_period_end, recent_quarters, fetched_at
        ),
    )


def _holding(stock_code: str = _STOCK_CODE) -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        stock_name="テスト銘柄",
        shares=300,
        average_purchase_price=Decimal("4000"),
        total_purchase_amount=Decimal("400000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _captured_fair_value_reflects_latest_earnings(
    monkeypatch: pytest.MonkeyPatch,
    fiscal_period_end: dt.date | None,
    recent_quarters: Sequence[QuarterlyFinancials] = (),
    fetched_at: dt.datetime | None = None,
    now: dt.datetime = _NOW,
    providers_now: dt.datetime | None = None,
) -> bool | None:
    """`ProfitTakingService.analyze()`を実際に実行し、`evaluate_profit_taking()`へ
    渡される直前の`condition_inputs.fair_value_reflects_latest_earnings`を
    捕捉して返す(`evaluate_profit_taking`自体はスタブで即座にNotImplementedErrorを
    送出させ、analyze()の以降の処理には依存しない)。
    """
    captured: dict[str, bool | None] = {}

    def _capture(**kwargs: object) -> None:
        condition_inputs = kwargs["condition_inputs"]
        assert isinstance(condition_inputs, ProfitTakingConditionInputs)
        captured["value"] = condition_inputs.fair_value_reflects_latest_earnings
        raise _StopAfterCaptureError

    monkeypatch.setattr(pt_module, "evaluate_profit_taking", _capture)
    providers = _providers(
        fiscal_period_end, recent_quarters, fetched_at, now=now, providers_now=providers_now
    )
    service = pt_module.ProfitTakingService(providers=providers, config=_CONFIG)
    with contextlib.suppress(_StopAfterCaptureError):
        service.analyze(_holding(), now)
    assert "value" in captured, "evaluate_profit_takingが呼ばれず、値を捕捉できなかった"
    return captured["value"]


class _StopAfterCaptureError(Exception):
    """condition_inputsを捕捉した直後にanalyze()を打ち切るための専用例外。"""


# --- T1/T2/T3: 基準日解決がresolve_latest_financial_period_end()経由になっている ---


def test_t1_recent_quarters_available_uses_the_latest_quarter_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1: recent_quartersがある場合、Bも(Aと同じく)直近四半期末を基準日とする。

    年次fiscal_period_end(2025-03-31)はdata_fetched_at(2026-09-25)から543日
    (400日超で古い)。直近四半期末(2026-06-30)は87日(400日以内で新しい)。
    四半期優先ならTrue、年次直接参照ならFalseになり、この2値の違いが
    「基準日の解決方法」を実際に区別する(下のT8がこの区別自体を固定する)。
    """
    value = _captured_fair_value_reflects_latest_earnings(
        monkeypatch,
        fiscal_period_end=dt.date(2025, 3, 31),
        recent_quarters=[_quarter(dt.date(2025, 3, 31)), _quarter(dt.date(2026, 6, 30))],
    )
    assert value is True


def test_t2_no_recent_quarters_falls_back_to_fiscal_period_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T2: recent_quartersが空の場合、年次fiscal_period_endへfallbackする
    (resolve_latest_financial_period_end()の既存契約どおり)。"""
    value = _captured_fair_value_reflects_latest_earnings(
        monkeypatch,
        fiscal_period_end=dt.date(2026, 6, 30),
        recent_quarters=[],
    )
    assert value is True  # data_fetched_at(9/25)から87日 <= 400


def test_t3_a_and_b_use_the_same_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """T3: AとBが同一のresolve_latest_financial_period_end()を使うことを、
    実装コードの呼び出しを直接確認することで固定する(import元の同一性)。"""
    import jstock_advisor.services.financial_freshness_integration as ffi_module

    assert pt_module.resolve_latest_financial_period_end is (
        ffi_module.resolve_latest_financial_period_end
    )


# --- T4/T5/T6: 400日しきい値の境界(basis dateの解決先が変わっても境界式は不変) ---


def test_t4_399_days_after_period_end_is_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """T4: 基準日から399日後のdata_fetched_atならTrue。"""
    period_end = dt.date(2025, 1, 1)
    fetched_at = dt.datetime(2026, 1, 5, 7, 0, tzinfo=dt.UTC)  # period_end + 369日
    assert (fetched_at.date() - period_end).days == 369
    fetched_at = period_end + dt.timedelta(days=399)
    fetched_at_dt = dt.datetime(
        fetched_at.year, fetched_at.month, fetched_at.day, 7, 0, tzinfo=dt.UTC
    )
    value = _captured_fair_value_reflects_latest_earnings(
        monkeypatch, fiscal_period_end=period_end, fetched_at=fetched_at_dt
    )
    assert value is True


def test_t5_400_days_after_period_end_is_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """T5: 境界(400日ちょうど)は現行契約どおりTrue(0<=age_days<=400)。"""
    period_end = dt.date(2025, 1, 1)
    fetched_at = period_end + dt.timedelta(days=400)
    fetched_at_dt = dt.datetime(
        fetched_at.year, fetched_at.month, fetched_at.day, 7, 0, tzinfo=dt.UTC
    )
    value = _captured_fair_value_reflects_latest_earnings(
        monkeypatch, fiscal_period_end=period_end, fetched_at=fetched_at_dt
    )
    assert value is True


def test_t6_401_days_after_period_end_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """T6: 401日後はFalse(境界超過)。"""
    period_end = dt.date(2025, 1, 1)
    fetched_at = period_end + dt.timedelta(days=401)
    fetched_at_dt = dt.datetime(
        fetched_at.year, fetched_at.month, fetched_at.day, 7, 0, tzinfo=dt.UTC
    )
    value = _captured_fair_value_reflects_latest_earnings(
        monkeypatch, fiscal_period_end=period_end, fetched_at=fetched_at_dt
    )
    assert value is False


# --- T7: UTC/JST境界 ---


def test_t7_evaluation_date_crossing_the_utc_jst_boundary_still_resolves_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T7: nowがUTC 15:00以降(=JSTでは既に翌日)でも、evaluation_date_jst(now)で
    計算されたJST暦日がresolve_latest_financial_period_end()へ正しく渡される
    (ProfitTakingService.analyze()が既に持つevaluation_date確定方針〔F-L4〕を
    そのまま使うだけであり、本Issueで新しい変換ロジックは作らない)。
    """
    # UTC 2026-09-24 21:00 = JST 2026-09-25 06:00
    now_near_boundary = dt.datetime(2026, 9, 24, 21, 0, tzinfo=dt.UTC)
    # 直近四半期末が「UTC暦日では未来」だがJST暦日では評価日以前、という
    # ケースを作る(2026-09-25はJST evaluation_dateなら候補に含まれる)。
    # もう一方の四半期(2025-03-31)は543日前(400日超で古い)にしておく。
    # evaluation_date_jst()がnaiveなUTC日付切り捨てへ退行すると、evaluation_date
    # が2026-09-24となり2026-09-25の四半期末が候補から除外され、古い方
    # (2025-03-31。400日超)へfallbackしてFalseになる(この2値の違いが
    # evaluation_dateの解決を実際に区別する。境界を跨がない通常ケースの回帰は
    # T1が別途固定する)。age_days側(data_fetched_at)は本Issueのscope外
    # (UTC暦日での.date()切り捨てという別の既知の性質を持つ)のため、
    # fetched_at/providers_nowを明示してage_days側を境界の影響から切り離す。
    quarter_end = dt.date(2026, 9, 25)
    value = _captured_fair_value_reflects_latest_earnings(
        monkeypatch,
        fiscal_period_end=dt.date(2025, 3, 31),
        recent_quarters=[_quarter(dt.date(2025, 3, 31)), _quarter(quarter_end)],
        now=now_near_boundary,
        fetched_at=dt.datetime(2026, 9, 25, 7, 0, tzinfo=dt.UTC),
        providers_now=dt.datetime(2026, 9, 25, 7, 0, tzinfo=dt.UTC),
    )
    assert value is True


# --- T8: negative verification(mutation) ---


def test_t8_reverting_to_direct_fiscal_period_end_reference_changes_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T8(negative verification): 修正前ロジック
    (`snapshot.financial.fiscal_period_end`を直接参照。`resolve_latest_financial_period_end()`
    を経由しない)を、実際に`ProfitTakingService._fair_value_reflects_latest_earnings`
    へ`monkeypatch`で注入し、T1と同じ入力でT1が期待する`True`ではなく`False`に
    変わることを固定する。実装コードそのものは書き換えない(mutationはテスト内で
    完結させ、他のテストへ影響を残さない)。
    """

    def _pre_fix_fair_value_reflects_latest_earnings(
        self: pt_module.ProfitTakingService, snapshot: object, evaluation_date: dt.date
    ) -> bool | None:
        if not snapshot.fair_value_range.methods_used:  # type: ignore[attr-defined]
            return None
        fiscal_period_end = snapshot.financial.fiscal_period_end  # type: ignore[attr-defined]
        if fiscal_period_end is None:
            return None
        age_days = (snapshot.data_fetched_at.date() - fiscal_period_end).days  # type: ignore[attr-defined]
        return 0 <= age_days <= 400

    monkeypatch.setattr(
        pt_module.ProfitTakingService,
        "_fair_value_reflects_latest_earnings",
        _pre_fix_fair_value_reflects_latest_earnings,
    )

    # T1と同じ入力: 年次2025-03-31(543日前。400日超で古い) /
    # 直近四半期2026-06-30(87日前。400日以内で新しい)。
    value = _captured_fair_value_reflects_latest_earnings(
        monkeypatch,
        fiscal_period_end=dt.date(2025, 3, 31),
        recent_quarters=[_quarter(dt.date(2025, 3, 31)), _quarter(dt.date(2026, 6, 30))],
    )
    assert value is False, (
        "修正前ロジック(fiscal_period_endの直接参照)へ戻すと、T1と同じ入力で "
        "Falseになるはずが、そうならなかった(反証が本Issueの欠陥を正しく "
        "捉えられていない)"
    )
