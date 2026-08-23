"""profit_taking_service.pyの決算発表確認待ち抑制(REVIEW_AFTER_EARNINGS)の
結合テスト(コードレビュー対応: 明治ホールディングス(2269)事例)。

決算予定日を経過したが無償データで発表実績を確認できない間、通常の
PARTIAL/FULL_PROFIT_TAKE提案を保留してREVIEW_AFTER_EARNINGSへ切り替えることと、
過去の決算予定日がbusiness_days_between()へ渡されて負の営業日数になり
永久に決算前抑制へ入り込むバグが再発しないことを、実際のモックProvider経由の
build_stock_snapshot()パイプラインで確認する。

MockFinancialDataProvider.get_financial_summary()はfiscal_period_endを常に
評価時刻の日付で返すため、財務データ未更新の状況を再現するには別途ラップして
fiscal_period_endを固定する必要がある。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import (
    DataSourceReference,
    PriceWithRationale,
    SellPriceLevels,
)
from jstock_advisor.domain.entities.enums import (
    AccountType,
    CorporateActionType,
    RecentPeriodsSource,
    RecommendationType,
    SellIntensity,
    TimingAction,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.signals.profit_taking import ProfitTakingResult, UnrealizedPnl
from jstock_advisor.interfaces.types import (
    CorporateActionEvent,
    Disclosure,
    FinancialSummary,
    QuarterlyFinancials,
)
from jstock_advisor.providers.corporate_action.mock_impl import MockCorporateActionProvider
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.dividend_data.mock_impl import MockDividendDataProvider
from jstock_advisor.providers.financial_data.mock_impl import MockFinancialDataProvider
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.shareholder_benefit.mock_impl import MockShareholderBenefitProvider
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_bundle import ProviderBundle

_CONFIG = load_config()
_NOW = dt.datetime(2026, 8, 6, 7, 0, tzinfo=dt.UTC)  # 明治HD事例: 決算予定日(8/5)の翌日
_STALE_EARNINGS_DATE = dt.date(2026, 8, 5)


class _FixedEarningsDateDisclosureProvider:
    """次回決算予定日を固定値で返すフェイク(get_disclosuresは委譲元へ委譲)。"""

    def __init__(self, delegate: object, next_earnings_date: dt.date | None) -> None:
        self._delegate = delegate
        self._next_earnings_date = next_earnings_date

    def get_disclosures(self, stock_code: str, since: dt.date) -> list[Disclosure]:
        return self._delegate.get_disclosures(stock_code, since)  # type: ignore[attr-defined]

    def get_next_earnings_date(self, stock_code: str) -> dt.date | None:
        return self._next_earnings_date


class _FixedFinancialPeriodFinancialDataProvider:
    """fiscal_period_end・recent_quarters・fetched_atを固定値で上書きするフェイク
    (他は委譲元へ委譲、デプロイ前対応)。

    MockFinancialDataProvider.get_financial_summary()は既定でrecent_quartersへ
    評価時刻に近い(=常に新しい)期末日を入れるため、fiscal_period_endだけを
    上書きしてもresolve_latest_financial_period_end()はrecent_quarters側を
    優先してしまう。既定でrecent_quarters=[]へ上書きすることで、従来通り
    fiscal_period_end(年次フォールバック)単独でデータ鮮度を制御できるように
    する。recent_quartersを明示的に渡した場合はそちらが使われる。
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
            # 由来精緻化対応: recent_quartersを明示的に渡した場合は四半期実績
            # 由来として扱う(テストの意図に合わせる)。既定(空)はUNAVAILABLE。
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


_TEST_FINANCIAL_SOURCE = DataSourceReference(provider="test-fixture", fetched_at=_NOW)


def _quarter(quarter_end: dt.date, stock_code: str = "2914") -> QuarterlyFinancials:
    return QuarterlyFinancials(
        stock_code=stock_code, quarter_end=quarter_end, source=_TEST_FINANCIAL_SOURCE
    )


def _providers(
    next_earnings_date: dt.date | None,
    fiscal_period_end: dt.date | None,
    now: dt.datetime = _NOW,
    recent_quarters: Sequence[QuarterlyFinancials] = (),
    fetched_at: dt.datetime | None = None,
) -> ProviderBundle:
    base = ProviderBundle(
        market_data=MockMarketDataProvider(now=now),
        financial_data=MockFinancialDataProvider(now=now),
        dividend_data=MockDividendDataProvider(now=now),
        shareholder_benefit=MockShareholderBenefitProvider(now=now),
        disclosure=MockDisclosureProvider(now=now),
        corporate_action=MockCorporateActionProvider(),
    )
    return dataclasses.replace(
        base,
        disclosure=_FixedEarningsDateDisclosureProvider(base.disclosure, next_earnings_date),
        financial_data=_FixedFinancialPeriodFinancialDataProvider(
            base.financial_data, fiscal_period_end, recent_quarters, fetched_at
        ),
    )


def _holding(stock_code: str) -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        stock_name="テスト銘柄",
        # 売買単位(既定100株)ちょうどだとPARTIAL成立時にpartial_sale_executable
        # =Falseとなり不変条件違反でrecommendation=Noneへ落ちてしまうため、
        # 単位を超える株数にする(コードレビュー対応2026-08、PARTIAL数量欠落
        # 不具合の再発防止で追加した不変条件のためのfixture調整)。
        shares=300,
        average_purchase_price=Decimal("4000"),
        total_purchase_amount=Decimal("400000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _canned_result(
    recommendation_type: RecommendationType,
    sell_intensity: SellIntensity | None = SellIntensity.STANDARD,
) -> ProfitTakingResult:
    """evaluate_profit_taking()の結果をモックのファンダメンタルズに依存せず
    固定するためのフェイク結果(REVIEW_AFTER_EARNINGS分岐だけを検証したいため)。

    sell_intensityは既定でSTANDARD(PARTIAL_PROFIT_TAKEが実際に一部売却数量を
    確定できる、現実的な状態を表す)。sell_intensity自体を検証したいテストは
    明示的にNone等を渡すこと(コードレビュー対応2026-08: PARTIAL_PROFIT_TAKEなのに
    sell_intensity=Noneのままだと、ProfitTakingService側の不変条件チェックに
    より数量なしRecommendationは生成されなくなったため)。
    """
    return ProfitTakingResult(
        recommendation_type=recommendation_type,
        fundamental_action=recommendation_type,
        timing_action=TimingAction.NEUTRAL,
        final_action=recommendation_type,
        triggered_reasons=["含み益率が一部利確基準を超過"],
        mitigating_factors_applied=[],
        hold_reasons=[],
        sell_prices=SellPriceLevels(
            recommended_limit_price=PriceWithRationale(price=Decimal("5000"), rationale="test")
        ),
        pnl=UnrealizedPnl(
            unrealized_pnl=Decimal("100000"),
            unrealized_pnl_pct=25.0,
            total_return_including_income=Decimal("105000"),
            total_return_pct=26.25,
        ),
        independent_condition_count=1,
        fair_value_used_as_sole_strong_basis=False,
        current_price_vs_neutral_fair_value_pct=10.0,
        current_price_vs_bull_fair_value_pct=5.0,
        fair_value_action_usable=False,
        origin="OTHER_CONDITIONS",
        ceiling_price=None,
        upside_pct=None,
        profit_protection_signal="NONE",
        profit_protection_basis_date=None,
        profit_protection_peak_price=None,
        profit_protection_peak_date=None,
        profit_protection_peak_gain_pct=None,
        profit_protection_current_gain_pct=None,
        profit_protection_drawdown_from_peak_pct=None,
        profit_protection_gain_giveback_ratio_pct=None,
        profit_protection_insufficient_reason=None,
        sell_intensity=sell_intensity,
    )


@pytest.mark.parametrize("stock_code", ["2914", "9861", "8136", "8306"])
def test_stale_earnings_date_with_unreflected_financials_becomes_review_after_earnings(
    monkeypatch: pytest.MonkeyPatch, stock_code: str
) -> None:
    """明治HD回帰(財務データ未更新ケース): 決算予定日を経過し、fiscal_period_end
    が想定報告ラグより前(=財務データ未更新)のとき、PARTIAL_PROFIT_TAKEは
    REVIEW_AFTER_EARNINGSへ切り替わり、sell_pricesは空になる。銘柄コードを
    変えても同じ結果になることを確認し、コード固有の分岐が無いことを示す。
    """
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE),
    )
    providers = _providers(_STALE_EARNINGS_DATE, dt.date(2026, 3, 31))
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding(stock_code), _NOW)

    assert outcome.data_error is None
    assert outcome.recommendation is not None
    rec = outcome.recommendation
    assert rec.recommendation_type == RecommendationType.REVIEW_AFTER_EARNINGS
    assert rec.sell_prices == SellPriceLevels()
    assert rec.next_earnings_date is None
    # 過去日をbusiness_days_between()へ渡さないため、負の営業日数にならない
    # (次回決算日がNoneのため決算前抑制の分岐自体に入らない)
    assert rec.business_days_to_earnings is None
    assert any("決算発表予定日を経過" in c for c in rec.next_review_conditions)


def test_stale_earnings_date_with_reflected_financials_keeps_original_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """明治HD回帰(財務データ更新済みケース): fiscal_period_endが想定報告ラグ以内
    まで進んでいれば、財務データが最新決算を反映したとみなし、通常の
    PARTIAL_PROFIT_TAKE判定をそのまま使う(REVIEW_AFTER_EARNINGSへ切り替えない)。
    """
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE),
    )
    # 決算予定日8/5から60日以内(想定報告ラグ既定値)のfiscal_period_end
    providers = _providers(_STALE_EARNINGS_DATE, dt.date(2026, 6, 30))
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.data_error is None
    assert outcome.recommendation is not None
    rec = outcome.recommendation
    assert rec.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE
    assert rec.sell_prices.recommended_limit_price is not None


def test_recent_quarter_update_and_fetched_after_earnings_becomes_data_updated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """デプロイ前対応(4ケース必須テーブル・正常更新): 年次fiscal_period_endは
    古いままでも、recent_quartersに決算予定日からの想定報告ラグ以内の四半期実績
    (2026-06-30)があり、かつfetched_atが決算予定日以後であれば、DATA_UPDATED
    として通常のPARTIAL_PROFIT_TAKE判定を維持する(四半期決算の反映を年次
    fiscal_period_endだけでは検知できなかったバグの回帰)。
    """
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE),
    )
    providers = _providers(
        _STALE_EARNINGS_DATE,
        dt.date(2026, 3, 31),
        recent_quarters=[_quarter(dt.date(2026, 3, 31)), _quarter(dt.date(2026, 6, 30))],
        fetched_at=dt.datetime(2026, 8, 6, 7, 0, tzinfo=dt.UTC),
    )
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.data_error is None
    assert outcome.recommendation is not None
    assert outcome.recommendation.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE


def test_fetched_at_alone_being_recent_does_not_become_data_updated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """デプロイ前対応(4ケース必須テーブル・fetched_atのみ新しい): 財務データの
    取得時刻が決算予定日以後でも、最新財務期間末(recent_quarters/年次とも
    2026-03-31のまま)が古ければDATA_UPDATEDにしない。
    """
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE),
    )
    providers = _providers(
        _STALE_EARNINGS_DATE,
        dt.date(2026, 3, 31),
        recent_quarters=[_quarter(dt.date(2026, 3, 31))],
        fetched_at=dt.datetime(2026, 8, 6, 7, 0, tzinfo=dt.UTC),
    )
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.data_error is None
    assert outcome.recommendation is not None
    assert outcome.recommendation.recommendation_type == RecommendationType.REVIEW_AFTER_EARNINGS


def test_period_alone_being_recent_does_not_become_data_updated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """デプロイ前対応(4ケース必須テーブル・periodのみ新しい): 最新財務期間末
    (recent_quarters内に2026-06-30)が十分新しくても、fetched_atが決算予定日
    (2026-08-05)より前(2026-08-04)であれば、決算発表前から保持していた
    データの可能性があるためDATA_UPDATEDにしない。
    """
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE),
    )
    providers = _providers(
        _STALE_EARNINGS_DATE,
        dt.date(2026, 3, 31),
        recent_quarters=[_quarter(dt.date(2026, 3, 31)), _quarter(dt.date(2026, 6, 30))],
        fetched_at=dt.datetime(2026, 8, 4, 7, 0, tzinfo=dt.UTC),
    )
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.data_error is None
    assert outcome.recommendation is not None
    assert outcome.recommendation.recommendation_type == RecommendationType.REVIEW_AFTER_EARNINGS


def test_both_period_and_fetched_at_unconfirmable_does_not_become_data_updated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """デプロイ前対応(4ケース必須テーブル・両方確認不能): recent_quartersが空、
    年次fiscal_period_endも取得できない(None)場合、最新財務期間末が解決できず
    DATA_UPDATEDにしない(取得不能時に取得日で代替しない)。
    """
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE),
    )
    providers = _providers(
        _STALE_EARNINGS_DATE,
        None,
        recent_quarters=[],
        fetched_at=dt.datetime(2026, 8, 6, 7, 0, tzinfo=dt.UTC),
    )
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.data_error is None
    assert outcome.recommendation is not None
    assert outcome.recommendation.recommendation_type == RecommendationType.REVIEW_AFTER_EARNINGS


def test_future_quarter_end_is_ignored_for_data_reflection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """デプロイ前対応: recent_quartersに評価日より未来の期末日(2026-09-30)が
    混入しても、それを決算反映済みの証拠として採用しない。有効な最大値
    (2026-06-30)がDATA_UPDATED判定に使われる。
    """
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE),
    )
    providers = _providers(
        _STALE_EARNINGS_DATE,
        dt.date(2026, 3, 31),
        recent_quarters=[
            _quarter(dt.date(2026, 3, 31)),
            _quarter(dt.date(2026, 6, 30)),
            _quarter(dt.date(2026, 9, 30)),  # 評価日(2026-08-06)より未来
        ],
        fetched_at=dt.datetime(2026, 8, 6, 7, 0, tzinfo=dt.UTC),
    )
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.data_error is None
    assert outcome.recommendation is not None
    assert outcome.recommendation.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE


def test_far_past_earnings_date_does_not_trigger_before_earnings_suppression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """business_days_between()の負数バグの回帰: 決算予定日がかなり過去でも
    (STALE_PAST_DATEによりnext_earnings_date=Noneとなるため)、決算直前の
    WATCH_BEFORE_EARNINGS抑制には入らない。
    """
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: _canned_result(RecommendationType.WATCH),
    )
    far_past = _NOW.date() - dt.timedelta(days=30)
    providers = _providers(far_past, dt.date(2026, 3, 31))
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.data_error is None
    assert outcome.recommendation is not None
    rec = outcome.recommendation
    assert rec.recommendation_type == RecommendationType.WATCH
    assert rec.business_days_to_earnings is None


def test_far_past_earnings_date_resumes_normal_profit_take_when_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """デプロイ前対応の回帰: Providerが何か月も前の過去日を返し続け、財務データも
    更新されないままの場合、decision_relevance=UNKNOWNとなり、REVIEW_AFTER_EARNINGS
    へは切り替えず通常のPARTIAL_PROFIT_TAKE判定を維持する(無期限抑制の防止)。
    """
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE),
    )
    # stale_earnings_relevance_days(既定10日)を大幅に超過する過去日。
    # fiscal_period_endはその過去日からの想定報告ラグ(既定60日)より前の
    # ままとし、財務データが一切更新されていない状況を表す。
    far_past = _NOW.date() - dt.timedelta(days=180)
    providers = _providers(far_past, dt.date(2025, 9, 30))
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.data_error is None
    assert outcome.recommendation is not None
    rec = outcome.recommendation
    assert rec.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE
    assert rec.earnings_decision_relevance is not None
    assert rec.earnings_decision_relevance.value == "UNKNOWN"


def test_is_confirmed_critical_bypasses_awaiting_confirmation_suppression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上場廃止決定・会計不正等の一次情報確認済みcritical(is_confirmed_critical)が
    検出されている場合は、AWAITING_CONFIRMATION中でも通常判定を保留しない
    (§8: 一次情報に基づく確定的シグナルは決算タイミングで抑制しない)。
    """
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE),
    )
    import jstock_advisor.services.profit_taking_service as module

    original_condition_inputs = module.ProfitTakingConditionInputs

    def _forced_critical_condition_inputs(*args: object, **kwargs: object) -> object:
        kwargs["accounting_or_scandal_or_delisting_risk"] = True
        return original_condition_inputs(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "ProfitTakingConditionInputs", _forced_critical_condition_inputs)

    providers = _providers(_STALE_EARNINGS_DATE, dt.date(2026, 3, 31))
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.data_error is None
    assert outcome.recommendation is not None
    assert outcome.recommendation.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE


def test_future_earnings_date_within_suppression_window_still_suppresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既存の決算直前抑制(REVIEW_BEFORE_EARNINGS)は今回の変更で退行していない
    ことの確認(未来の確定日はCONFIRMEDのままbusiness_days_betweenへ渡る)。
    """
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE),
    )
    near_future = _NOW.date() + dt.timedelta(days=1)
    providers = _providers(near_future, dt.date(2026, 3, 31))
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.data_error is None
    assert outcome.recommendation is not None
    rec = outcome.recommendation
    assert rec.recommendation_type == RecommendationType.REVIEW_BEFORE_EARNINGS
    assert rec.business_days_to_earnings is not None
    assert rec.business_days_to_earnings >= 0


# --- 利益保全(Profit Protection)判定の配線テスト(2026-08追加) ---


class _StubCorporateActionProvider:
    """指定したイベント一覧をそのまま返す企業行動Providerのスタブ。"""

    def __init__(self, events: list[CorporateActionEvent]) -> None:
        self._events = events

    def get_corporate_actions(
        self, stock_code: str, since: dt.date
    ) -> list[CorporateActionEvent]:
        return self._events


def _split_event(effective_date: dt.date, stock_code: str = "2914") -> CorporateActionEvent:
    return CorporateActionEvent(
        stock_code=stock_code,
        event_type=CorporateActionType.SPLIT,
        announced_date=effective_date - dt.timedelta(days=30),
        effective_date=effective_date,
        ratio=Decimal("2"),
        source=_TEST_FINANCIAL_SOURCE,
    )


def test_ratio_adjustment_event_since_entry_marks_profit_protection_data_insufficient() -> None:
    """保有開始日以降に株式分割があった場合、Profit Protection判定はデータ不足
    としてスキップする(要求仕様§9)。"""
    from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

    holding = _holding("2914")
    providers = _providers(None, dt.date(2026, 6, 30))
    providers = dataclasses.replace(
        providers,
        corporate_action=_StubCorporateActionProvider(
            [_split_event(dt.date(2025, 6, 1))]  # first_purchase_date(2024-1-1)以降
        ),
    )
    service = ProfitTakingService(providers=providers, config=_CONFIG)
    snapshot, error = build_stock_snapshot(providers, "2914", _NOW, _CONFIG)
    assert error is None
    assert snapshot is not None

    metrics = service._compute_profit_protection_metrics(holding, snapshot, _NOW)

    assert metrics.insufficient_data_reason is not None
    assert metrics.candidate_signal is False
    assert metrics.strong_signal is False


def test_ratio_adjustment_event_before_entry_does_not_block() -> None:
    """保有開始日より前の株式分割は、Profit Protection判定を妨げない
    (影響範囲は保有期間中のイベントのみ)。"""
    from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

    holding = _holding("2914")
    providers = _providers(None, dt.date(2026, 6, 30))
    providers = dataclasses.replace(
        providers,
        corporate_action=_StubCorporateActionProvider(
            [_split_event(dt.date(2023, 6, 1))]  # first_purchase_date(2024-1-1)より前
        ),
    )
    service = ProfitTakingService(providers=providers, config=_CONFIG)
    snapshot, error = build_stock_snapshot(providers, "2914", _NOW, _CONFIG)
    assert error is None
    assert snapshot is not None

    metrics = service._compute_profit_protection_metrics(holding, snapshot, _NOW)

    assert metrics.insufficient_data_reason is None


def _holding_with_buy_more(stock_code: str) -> Holding:
    """買い増しがあり、first_purchase_date != last_purchase_dateとなる保有
    (コードレビュー対応2026-08、指摘1: basis_date=last_purchase_dateの回帰確認用)。
    """
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        stock_name="テスト銘柄",
        shares=100,
        average_purchase_price=Decimal("4000"),
        total_purchase_amount=Decimal("400000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2025, 6, 1),  # 買い増し日
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_corporate_action_between_first_and_last_purchase_does_not_block() -> None:
    """買い増し前(first_purchase_dateとlast_purchase_dateの間)の株式分割は、
    basis_date(last_purchase_date)より前であるためProfit Protection判定を
    妨げない(コードレビュー対応2026-08、指摘1のCase E相当)。"""
    from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

    holding = _holding_with_buy_more("2914")
    providers = _providers(None, dt.date(2026, 6, 30))
    providers = dataclasses.replace(
        providers,
        corporate_action=_StubCorporateActionProvider(
            [_split_event(dt.date(2024, 6, 1))]  # first(2024-1-1)〜last(2025-6-1)の間
        ),
    )
    service = ProfitTakingService(providers=providers, config=_CONFIG)
    snapshot, error = build_stock_snapshot(providers, "2914", _NOW, _CONFIG)
    assert error is None
    assert snapshot is not None

    metrics = service._compute_profit_protection_metrics(holding, snapshot, _NOW)

    assert metrics.insufficient_data_reason is None


def test_corporate_action_on_or_after_last_purchase_date_blocks() -> None:
    """買い増し日(last_purchase_date)以降の株式分割は、basis_date以降の
    イベントであるためProfit Protection判定をデータ不足とする
    (コードレビュー対応2026-08、指摘1のCase E相当)。"""
    from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

    holding = _holding_with_buy_more("2914")
    providers = _providers(None, dt.date(2026, 6, 30))
    providers = dataclasses.replace(
        providers,
        corporate_action=_StubCorporateActionProvider(
            [_split_event(dt.date(2025, 6, 1))]  # last_purchase_dateと同日(境界)
        ),
    )
    service = ProfitTakingService(providers=providers, config=_CONFIG)
    snapshot, error = build_stock_snapshot(providers, "2914", _NOW, _CONFIG)
    assert error is None
    assert snapshot is not None

    metrics = service._compute_profit_protection_metrics(holding, snapshot, _NOW)

    assert metrics.insufficient_data_reason is not None


def _holding_with_last_sale_date(
    stock_code: str, last_purchase_date: dt.date, last_sale_date: dt.date | None
) -> Holding:
    """Profit Protection由来の一部売却後を模擬する保有(last_sale_date設定済み、
    コードレビュー対応2026-08、指摘A-2)。"""
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        stock_name="テスト銘柄",
        shares=100,
        average_purchase_price=Decimal("4000"),
        total_purchase_amount=Decimal("400000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=last_purchase_date,
        last_sale_date=last_sale_date,
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_last_sale_date_advances_basis_date_beyond_last_purchase_date() -> None:
    """last_sale_date(直近売却日)がlast_purchase_dateより後の場合、実効
    basis_dateはlast_sale_dateとなる(コードレビュー対応2026-08、指摘A-2)。
    last_purchase_date〜last_sale_dateの間の株式分割は、新しい実効basis_date
    より前であるため判定を妨げない(妨げれば旧basis_date=last_purchase_dateが
    誤って使われていることになる)。"""
    from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

    holding = _holding_with_last_sale_date(
        "2914", last_purchase_date=dt.date(2024, 1, 1), last_sale_date=dt.date(2025, 6, 1)
    )
    providers = _providers(None, dt.date(2026, 6, 30))
    providers = dataclasses.replace(
        providers,
        corporate_action=_StubCorporateActionProvider(
            [_split_event(dt.date(2024, 6, 1))]  # last_purchase_date〜last_sale_dateの間
        ),
    )
    service = ProfitTakingService(providers=providers, config=_CONFIG)
    snapshot, error = build_stock_snapshot(providers, "2914", _NOW, _CONFIG)
    assert error is None
    assert snapshot is not None

    metrics = service._compute_profit_protection_metrics(holding, snapshot, _NOW)

    assert metrics.insufficient_data_reason is None


def test_corporate_action_on_or_after_last_sale_date_blocks() -> None:
    """last_sale_date以降の株式分割は、新しい実効basis_date(=last_sale_date)
    以降のイベントであるためデータ不足とする(コードレビュー対応2026-08、
    指摘A-2)。"""
    from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

    holding = _holding_with_last_sale_date(
        "2914", last_purchase_date=dt.date(2024, 1, 1), last_sale_date=dt.date(2025, 6, 1)
    )
    providers = _providers(None, dt.date(2026, 6, 30))
    providers = dataclasses.replace(
        providers,
        corporate_action=_StubCorporateActionProvider(
            [_split_event(dt.date(2025, 6, 1))]  # last_sale_dateと同日(境界)
        ),
    )
    service = ProfitTakingService(providers=providers, config=_CONFIG)
    snapshot, error = build_stock_snapshot(providers, "2914", _NOW, _CONFIG)
    assert error is None
    assert snapshot is not None

    metrics = service._compute_profit_protection_metrics(holding, snapshot, _NOW)

    assert metrics.insufficient_data_reason is not None


def test_last_purchase_date_wins_when_more_recent_than_last_sale_date() -> None:
    """last_sale_dateよりlast_purchase_dateの方が新しい場合(売却後に買い増し
    した場合)、実効basis_dateはlast_purchase_date(より新しい方)となる
    (コードレビュー対応2026-08、指摘A-2。FIFO/last_purchase_dateの意味を
    壊さないことの確認)。last_sale_date〜last_purchase_dateの間の株式分割は
    実効basis_dateより前であるため判定を妨げない。"""
    from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

    holding = _holding_with_last_sale_date(
        "2914", last_purchase_date=dt.date(2025, 6, 1), last_sale_date=dt.date(2024, 1, 1)
    )
    providers = _providers(None, dt.date(2026, 6, 30))
    providers = dataclasses.replace(
        providers,
        corporate_action=_StubCorporateActionProvider(
            [_split_event(dt.date(2024, 6, 1))]  # last_sale_date〜last_purchase_dateの間
        ),
    )
    service = ProfitTakingService(providers=providers, config=_CONFIG)
    snapshot, error = build_stock_snapshot(providers, "2914", _NOW, _CONFIG)
    assert error is None
    assert snapshot is not None

    metrics = service._compute_profit_protection_metrics(holding, snapshot, _NOW)

    assert metrics.insufficient_data_reason is None


def test_no_last_sale_date_uses_last_purchase_date_as_before() -> None:
    """last_sale_date=None(未売却)の場合、従来どおりlast_purchase_dateのみを
    基準日として使う(後方互換確認)。"""
    from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

    holding = _holding_with_last_sale_date(
        "2914", last_purchase_date=dt.date(2024, 1, 1), last_sale_date=None
    )
    providers = _providers(None, dt.date(2026, 6, 30))
    service = ProfitTakingService(providers=providers, config=_CONFIG)
    snapshot, error = build_stock_snapshot(providers, "2914", _NOW, _CONFIG)
    assert error is None
    assert snapshot is not None

    metrics = service._compute_profit_protection_metrics(holding, snapshot, _NOW)

    assert metrics.insufficient_data_reason is None


def test_insufficient_reason_persisted_on_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DATA_INSUFFICIENT時の具体的理由がRecommendationへ永続化される
    (コードレビュー対応2026-08、指摘2)。"""
    canned = dataclasses.replace(
        _canned_result(RecommendationType.WATCH),
        profit_protection_signal="DATA_INSUFFICIENT",
        profit_protection_insufficient_reason="保有期間中に株式分割・併合等があり判定不能",
    )
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: canned,
    )
    providers = _providers(None, dt.date(2026, 6, 30))
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.recommendation is not None
    rec = outcome.recommendation
    assert rec.profit_protection_signal == "DATA_INSUFFICIENT"
    assert rec.profit_protection_insufficient_reason == (
        "保有期間中に株式分割・併合等があり判定不能"
    )


def test_recommendation_without_new_field_loads_with_none_default() -> None:
    """既存(本フィールド追加前)のRecommendationデータでも、新規フィールドが
    無いままロードできる(コードレビュー対応2026-08、指摘2の後方互換確認)。"""
    from jstock_advisor.domain.entities.recommendation import Recommendation

    rec_dict = {
        "recommendation_id": "test-id",
        "stock_code": "2914",
        "stock_name": "テスト銘柄",
        "recommended_at": _NOW,
        "recommendation_type": RecommendationType.WATCH,
        "price_at_recommendation": Decimal("4200"),
        "confidence": "MEDIUM",
        "rule_version": "test",
    }
    assert "profit_protection_insufficient_reason" not in rec_dict
    assert "sell_intensity" not in rec_dict
    assert "suggested_sell_ratio" not in rec_dict
    rec = Recommendation.model_validate(rec_dict)
    assert rec.profit_protection_insufficient_reason is None
    assert rec.profit_protection_signal is None
    # コードレビュー対応2026-08、Part B: sell_intensity/suggested_sell_ratioも
    # Optional/default Noneで後方互換(AK)。
    assert rec.sell_intensity is None
    assert rec.suggested_sell_ratio is None


def test_no_corporate_action_events_computes_metrics_normally() -> None:
    """企業行動イベントが無い通常ケースでは、価格履歴からProfit Protection指標を
    正常に算出できる(データ不足として扱わない)。"""
    from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

    holding = _holding("2914")
    providers = _providers(None, dt.date(2026, 6, 30))
    service = ProfitTakingService(providers=providers, config=_CONFIG)
    snapshot, error = build_stock_snapshot(providers, "2914", _NOW, _CONFIG)
    assert error is None
    assert snapshot is not None

    metrics = service._compute_profit_protection_metrics(holding, snapshot, _NOW)

    assert metrics.insufficient_data_reason is None
    assert metrics.peak_price_since_entry is not None


def test_recommendation_exposes_profit_protection_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recommendationへevaluate_profit_taking()の結果由来のProfit Protection
    フィールドが伝播することを確認する(要求仕様§8: 判定理由の追跡可能性)。"""
    canned = dataclasses.replace(
        _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE),
        origin="PROFIT_PROTECTION_STRONG",
        profit_protection_signal="STRONG",
        profit_protection_peak_price=Decimal("1454.5"),
        profit_protection_peak_gain_pct=58.1,
        profit_protection_current_gain_pct=33.4,
        profit_protection_drawdown_from_peak_pct=15.6,
        profit_protection_gain_giveback_ratio_pct=42.5,
    )
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: canned,
    )
    providers = _providers(None, dt.date(2026, 6, 30))
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.recommendation is not None
    rec = outcome.recommendation
    assert rec.profit_protection_signal == "STRONG"
    assert rec.profit_protection_peak_price == Decimal("1454.5")
    assert rec.profit_protection_peak_gain_pct == 58.1
    assert rec.profit_protection_current_gain_pct == 33.4
    assert rec.profit_protection_drawdown_from_peak_pct == 15.6
    assert rec.profit_protection_gain_giveback_ratio_pct == 42.5
    assert "profit_protection" in rec.config_values_used


def test_recommendation_exposes_sell_intensity_and_suggested_quantity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recommendationへ、evaluate_profit_taking()が決定したsell_intensityに
    応じた売却株数・比率が伝播することを確認する(コードレビュー対応2026-08、
    指摘Part B)。サンリオ8136相当(500株、Strong)で300株程度を期待する。"""
    from jstock_advisor.domain.entities.enums import SellIntensity

    canned = dataclasses.replace(
        _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE),
        origin="PROFIT_PROTECTION_STRONG",
        sell_intensity=SellIntensity.STRONG,
    )
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: canned,
    )
    holding = _holding("2914").model_copy(
        update={"shares": 500, "average_purchase_price": Decimal("920")}
    )
    providers = _providers(None, dt.date(2026, 6, 30))
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(holding, _NOW)

    assert outcome.recommendation is not None
    rec = outcome.recommendation
    assert rec.sell_intensity == "STRONG"
    assert rec.partial_sale_executable is True
    assert rec.suggested_sell_shares == 300  # strong比率0.60 -> floor(500*0.6/100)=3単元
    assert rec.suggested_sell_ratio is not None and abs(rec.suggested_sell_ratio - 0.6) < 1e-9


def test_earnings_suppression_clears_sell_intensity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """決算直前抑制でPARTIAL_PROFIT_TAKEがREVIEW_BEFORE_EARNINGSへ格下げされた
    場合、sell_intensity/suggested_sell_sharesも算出しない(コードレビュー
    対応2026-08、指摘Part B: 「売るべきか」の格下げに数量ロジックが追従する
    ことの確認)。"""
    from jstock_advisor.domain.entities.enums import SellIntensity

    canned = dataclasses.replace(
        _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE),
        origin="PROFIT_PROTECTION_STRONG",
        sell_intensity=SellIntensity.STRONG,
    )
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: canned,
    )
    holding = _holding("2914").model_copy(
        update={"shares": 500, "average_purchase_price": Decimal("920")}
    )
    near_future = _NOW.date() + dt.timedelta(days=1)
    providers = _providers(near_future, dt.date(2026, 6, 30))
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(holding, _NOW)

    assert outcome.recommendation is not None
    rec = outcome.recommendation
    assert rec.recommendation_type == RecommendationType.REVIEW_BEFORE_EARNINGS
    assert rec.sell_intensity is None
    assert rec.suggested_sell_shares is None
    assert rec.suggested_sell_ratio is None


# --- domain/service間の不変条件(コードレビュー対応2026-08、PARTIAL数量欠落
# 不具合の再発防止): 「PARTIAL_PROFIT_TAKEがRecommendationとして成立するなら
# suggested_sell_sharesが必ず存在する」ことをService層自身が最終確認し、
# 満たさない場合は数量なしRecommendationを生成せずrecommendation=None・
# data_errorを設定してfail-closedする(1銘柄の異常でLambda全体を落とさない
# 既存パターン、429行目の snapshot取得失敗時と同じ形)。 -----------------------


def test_normal_partial_has_shares_and_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    # A. 通常のPARTIAL(株数が売買単位を十分超える保有)では
    # suggested_sell_shares/suggested_sell_ratioが必ず確定する。
    canned = _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE)  # sell_intensity=STANDARD既定
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: canned,
    )
    providers = _providers(None, dt.date(2026, 6, 30))
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding("2914"), _NOW)  # shares=300(単位100超)

    assert outcome.data_error is None
    assert outcome.recommendation is not None
    assert outcome.recommendation.recommendation_type == RecommendationType.PARTIAL_PROFIT_TAKE
    assert outcome.recommendation.suggested_sell_shares is not None
    assert outcome.recommendation.suggested_sell_shares > 0
    assert outcome.recommendation.suggested_sell_ratio is not None
    assert 0 < outcome.recommendation.suggested_sell_ratio < 1


def test_partial_blocked_when_shares_at_or_below_trading_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # B. domain層をモックしてPARTIAL成立を強制しても、保有株数が売買単位以下
    # (partial_sale_executable=False相当)の場合は数量なしRecommendationを
    # 生成せず、data_errorを設定してfail-closedする。
    canned = _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE)  # sell_intensity=STANDARD既定
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: canned,
    )
    providers = _providers(None, dt.date(2026, 6, 30))
    service = ProfitTakingService(providers=providers, config=_CONFIG)
    holding = _holding("2914").model_copy(update={"shares": 100})  # 売買単位ちょうど

    outcome = service.analyze(holding, _NOW)

    assert outcome.recommendation is None
    assert outcome.data_error is not None
    assert "PARTIAL_PROFIT_TAKE" in outcome.data_error


def test_partial_blocked_when_sell_intensity_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # C. 保有株数は十分でも(partial_sale_executable=True相当)、
    # sell_intensity=Noneのままではやはり数量なしRecommendationを生成しない。
    canned = _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE, sell_intensity=None)
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: canned,
    )
    providers = _providers(None, dt.date(2026, 6, 30))
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding("2914"), _NOW)  # shares=300

    assert outcome.recommendation is None
    assert outcome.data_error is not None


def test_partial_blocked_when_compute_suggested_sell_shares_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # D. partial_sale_executable=True・sell_intensityも確定しているにもかかわらず、
    # compute_suggested_sell_shares()自身がNoneを返す異常ケース(売買単位の
    # 1〜1.99単位分、例: 150株/単位100株では unit_count=1<2 のため一部売却後に
    # 最低1単位を残せず算出不能)でも、数量なしRecommendationを生成しない。
    canned = _canned_result(
        RecommendationType.PARTIAL_PROFIT_TAKE, sell_intensity=SellIntensity.LIGHT
    )
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: canned,
    )
    providers = _providers(None, dt.date(2026, 6, 30))
    service = ProfitTakingService(providers=providers, config=_CONFIG)
    # 150株: partial_sale_executable=True(150>100)だが
    # compute_suggested_sell_shares()はunit_count=1<2でNoneを返す。
    holding = _holding("2914").model_copy(update={"shares": 150})

    outcome = service.analyze(holding, _NOW)

    assert outcome.recommendation is None
    assert outcome.data_error is not None


def test_normal_partial_notification_text_includes_share_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # E. 正常なPARTIALをRecommendation -> adapter -> formatterまで通し、
    # 通知本文に株数が実際に存在することを確認する。
    from jstock_advisor.domain.entities.enums import NotificationCategory
    from jstock_advisor.domain.notification.message_formatter import format_notification_text
    from jstock_advisor.domain.notification.recommendation_adapter import (
        build_notification_text_input,
    )

    canned = _canned_result(RecommendationType.PARTIAL_PROFIT_TAKE)  # sell_intensity=STANDARD既定
    monkeypatch.setattr(
        "jstock_advisor.services.profit_taking_service.evaluate_profit_taking",
        lambda **kwargs: canned,
    )
    providers = _providers(None, dt.date(2026, 6, 30))
    service = ProfitTakingService(providers=providers, config=_CONFIG)

    outcome = service.analyze(_holding("2914"), _NOW)

    assert outcome.recommendation is not None
    text_input = build_notification_text_input(
        outcome.recommendation, NotificationCategory.PARTIAL_SELL
    )
    assert text_input.suggested_sell_shares is not None
    text = format_notification_text(text_input)
    assert "株" in text
