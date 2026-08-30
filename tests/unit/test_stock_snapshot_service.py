"""build_stock_snapshot()の決算日検証ロジックのテスト(コードレビュー対応:
明治ホールディングス(2269)事例)。

データ提供元(yfinance等)の更新遅延により、評価日より過去の日付が「次回決算
予定日」として返ってくることがある。過去日をそのまま次回決算日として使わず、
buy/sell/profit_takingの3消費者すべてが一元的に検証された値のみを使うことを
build_stock_snapshot()の出力で直接確認する。
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    EarningsDateStatus,
    EarningsDecisionRelevance,
    EarningsReleaseConfirmationState,
    EarningsSurpriseEvaluationState,
    EarningsTrendEvaluationState,
    HistoricalValuationEvaluationState,
    ValuationBasis,
)
from jstock_advisor.interfaces.provider_errors import (
    ProviderDataError,
    ProviderFailureCategory,
)
from jstock_advisor.interfaces.types import Disclosure
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.stock_snapshot_service import build_stock_snapshot

_CFG = load_config()
_NOW = dt.datetime(2026, 8, 6, tzinfo=dt.UTC)
_STOCK_CODE = "2914"


class _FixedEarningsDateDisclosureProvider:
    """次回決算予定日を固定値(または欠損)で返すフェイク。get_disclosuresは
    委譲元のモックProviderへそのまま委譲する(決算日検証以外は変更しない)。
    """

    def __init__(self, delegate: object, next_earnings_date: dt.date | None) -> None:
        self._delegate = delegate
        self._next_earnings_date = next_earnings_date

    def get_disclosures(self, stock_code: str, since: dt.date) -> list[Disclosure]:
        return self._delegate.get_disclosures(stock_code, since)  # type: ignore[attr-defined]

    def get_next_earnings_date(self, stock_code: str) -> dt.date | None:
        return self._next_earnings_date


def _providers_with_fixed_earnings_date(next_earnings_date: dt.date | None):
    base = build_mock_provider_bundle(_NOW)
    fake_disclosure = _FixedEarningsDateDisclosureProvider(base.disclosure, next_earnings_date)
    return dataclasses.replace(base, disclosure=fake_disclosure)


def test_past_earnings_date_is_rejected_as_stale() -> None:
    """明治HD事例の回帰: 過去の決算予定日をそのままnext_earnings_dateとして
    使わない。"""
    providers = _providers_with_fixed_earnings_date(dt.date(2026, 8, 5))
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.earnings_date_status == EarningsDateStatus.STALE_PAST_DATE
    assert snapshot.earnings_date_raw == dt.date(2026, 8, 5)
    assert snapshot.next_earnings_date is None


def test_today_earnings_date_is_confirmed() -> None:
    """予定日当日はCONFIRMEDとして扱う(過去日として除外しない)。"""
    providers = _providers_with_fixed_earnings_date(_NOW.date())
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.earnings_date_status == EarningsDateStatus.CONFIRMED
    assert snapshot.next_earnings_date == _NOW.date()


def test_future_earnings_date_is_confirmed() -> None:
    future = _NOW.date() + dt.timedelta(days=90)
    providers = _providers_with_fixed_earnings_date(future)
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.earnings_date_status == EarningsDateStatus.CONFIRMED
    assert snapshot.earnings_date_raw == future
    assert snapshot.next_earnings_date == future


def test_missing_earnings_date_is_unavailable() -> None:
    providers = _providers_with_fixed_earnings_date(None)
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.earnings_date_status == EarningsDateStatus.UNAVAILABLE
    assert snapshot.earnings_date_raw is None
    assert snapshot.next_earnings_date is None


# ===== JST基準の境界テスト(デプロイ前対応) =====


def test_jst_boundary_rejects_date_that_is_today_in_utc_but_past_in_jst() -> None:
    """UTC 2026-08-05T23:00 = JST 2026-08-06T08:00。素の.date()(UTC基準)なら
    8/5になり「当日」と誤判定するが、JST基準では8/6が評価日のため、8/5は
    過去日として正しくSTALE_PAST_DATEになる。
    """
    now_utc_23 = dt.datetime(2026, 8, 5, 23, 0, tzinfo=dt.UTC)
    providers = _providers_with_fixed_earnings_date(dt.date(2026, 8, 5))
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, now_utc_23, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.earnings_date_status == EarningsDateStatus.STALE_PAST_DATE
    assert snapshot.next_earnings_date is None


def test_jst_boundary_treats_date_as_today_when_jst_date_matches() -> None:
    """UTC 2026-08-04T23:00 = JST 2026-08-05T08:00。決算予定日が8/5の場合、
    JST基準では「当日」のためCONFIRMEDのままとなり、営業日数は0になる。
    """
    now_utc_23 = dt.datetime(2026, 8, 4, 23, 0, tzinfo=dt.UTC)
    providers = _providers_with_fixed_earnings_date(dt.date(2026, 8, 5))
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, now_utc_23, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.earnings_date_status == EarningsDateStatus.CONFIRMED
    assert snapshot.next_earnings_date == dt.date(2026, 8, 5)
    assert snapshot.business_days_to_earnings == 0


def test_naive_now_is_rejected() -> None:
    providers = _providers_with_fixed_earnings_date(None)
    naive_now = dt.datetime(2026, 8, 6)
    with pytest.raises(ValueError, match="timezone-aware"):
        build_stock_snapshot(providers, _STOCK_CODE, naive_now, _CFG)


def test_business_days_to_earnings_is_computed_once_on_snapshot() -> None:
    """next_earnings_dateが未来日の場合、business_days_to_earningsがJST暦日
    基準で1回だけ計算されsnapshotへ格納される(buy/sell/profit_takingが
    個別に再計算しないための一元化)。"""
    future = _NOW.date() + dt.timedelta(days=7)
    providers = _providers_with_fixed_earnings_date(future)
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.business_days_to_earnings is not None
    assert snapshot.business_days_to_earnings > 0


def test_business_days_to_earnings_is_none_when_earnings_date_stale() -> None:
    providers = _providers_with_fixed_earnings_date(dt.date(2026, 8, 5))
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.business_days_to_earnings is None


# ===== 判定精度向上機能Phase B: Historical Valuation Score配線確認 =====


def test_historical_valuation_score_is_computed_when_data_available() -> None:
    """モックプロバイダの過去バリュエーションデータ・trailing_eps/forecast_bpsが
    揃っていれば、-100〜+100の範囲でhistorical_valuation.scoreが計算されること
    (配線確認。スコアの計算ロジック自体の詳細はtest_historical_valuation_score.py
    で検証)。"""
    providers = build_mock_provider_bundle(_NOW)
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.historical_valuation.state == HistoricalValuationEvaluationState.EVALUATED
    assert snapshot.historical_valuation.score is not None
    assert -100.0 <= snapshot.historical_valuation.score <= 100.0


def test_historical_valuation_current_per_uses_trailing_basis() -> None:
    """current PERはforecast_eps(forward)ではなくtrailing_epsから算出され、
    TRAILING basisとして記録される(コードレビュー対応: basis整合性)。"""
    providers = build_mock_provider_bundle(_NOW)
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.historical_valuation.current_per_basis == ValuationBasis.TRAILING
    assert snapshot.historical_valuation.current_per == (
        snapshot.current_price / snapshot.financial.trailing_eps
    )


# ===== 判定精度向上機能Phase B第二弾: Timing Score配線確認 =====


def test_timing_score_is_computed_from_momentum_snapshot() -> None:
    """StockSnapshot.timingがmomentum・current_priceを基に計算されること
    (配線確認。算出式自体の詳細はtest_timing_score.pyで検証)。"""
    providers = build_mock_provider_bundle(_NOW)
    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)
    assert error is None
    assert snapshot is not None
    assert snapshot.timing.trend_quality_component is not None
    assert snapshot.timing.model_version == _CFG.timing_score.model_version


# ===== 判定精度向上機能Phase C: build_stock_snapshot()統合配線確認
# (コードレビュー対応 第3回)。EarningsDecisionRelevance判定・Earnings
# Surprise履歴取得のI/O抑止(phase_c_earnings_blocked)の本番配線を検証する。
# 単体のdecision_relevance挙動自体はtest_earnings_surprise.py/
# test_earnings_trend.pyで検証済みのため、ここではbuild_stock_snapshot()
# ↓resolve_earnings_release_confirmation()↓resolve_earnings_decision_
# relevance()↓evaluate_earnings_surprise()/evaluate_earnings_trend()という
# 実際の配線経路のみを対象とする。 =====


class _SpyFinancialDataProvider:
    """financial_data providerのフェイクラッパー。get_financial_summary()の
    fetched_atを固定値へ差し替えて決算反映確認(DATA_UPDATED)の誤発火を防ぎ、
    get_earnings_surprise_history()の呼び出し回数を数える(Medium対応の
    回帰テスト用)。他のメソッドは委譲元へそのまま委譲する。"""

    def __init__(self, delegate: object, fetched_at_override: dt.datetime) -> None:
        self._delegate = delegate
        self._fetched_at_override = fetched_at_override
        self.earnings_surprise_history_call_count = 0

    def get_financial_summary(self, stock_code: str):
        summary = self._delegate.get_financial_summary(stock_code)  # type: ignore[attr-defined]
        if summary is None:
            return None
        source = summary.source.model_copy(update={"fetched_at": self._fetched_at_override})
        return summary.model_copy(update={"source": source})

    def get_historical_valuation(self, stock_code: str, years: int):
        return self._delegate.get_historical_valuation(stock_code, years)  # type: ignore[attr-defined]

    def get_cashflow_decomposition(self, stock_code: str):
        return self._delegate.get_cashflow_decomposition(stock_code)  # type: ignore[attr-defined]

    def get_earnings_surprise_history(self, stock_code: str):
        self.earnings_surprise_history_call_count += 1
        return self._delegate.get_earnings_surprise_history(stock_code)  # type: ignore[attr-defined]


def _providers_for_phase_c_earnings_window(
    earnings_date_raw: dt.date, fetched_at_override: dt.datetime
) -> tuple[object, _SpyFinancialDataProvider]:
    base = build_mock_provider_bundle(_NOW)
    fake_disclosure = _FixedEarningsDateDisclosureProvider(base.disclosure, earnings_date_raw)
    spy_financial = _SpyFinancialDataProvider(base.financial_data, fetched_at_override)
    providers = dataclasses.replace(base, disclosure=fake_disclosure, financial_data=spy_financial)
    return providers, spy_financial


class _SpyDividendDataProvider:
    """dividend_data providerのフェイクラッパー。get_dividend_info()に渡された
    fiscal_year_end_monthを記録する(配当クロスバリデーション根本修正:
    financial.fiscal_year_end_monthの配線確認用)。"""

    def __init__(self, delegate: object) -> None:
        self._delegate = delegate
        self.fiscal_year_end_month_calls: list[int | None] = []

    def get_dividend_info(self, stock_code: str, fiscal_year_end_month: int | None = None):
        self.fiscal_year_end_month_calls.append(fiscal_year_end_month)
        return self._delegate.get_dividend_info(  # type: ignore[attr-defined]
            stock_code, fiscal_year_end_month=fiscal_year_end_month
        )


def test_dividend_info_is_fetched_with_financial_summary_fiscal_year_end_month() -> None:
    """build_stock_snapshot()がdividend_data.get_dividend_info()を呼ぶ際、
    financial_data.get_financial_summary()で取得したfiscal_year_end_monthを
    正しく引き渡すこと(配当クロスバリデーション根本修正の配線確認)。"""
    base = build_mock_provider_bundle(_NOW)
    financial = base.financial_data.get_financial_summary(_STOCK_CODE)
    assert financial is not None
    spy_dividend = _SpyDividendDataProvider(base.dividend_data)
    providers = dataclasses.replace(base, dividend_data=spy_dividend)

    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)

    assert error is None
    assert snapshot is not None
    assert spy_dividend.fiscal_year_end_month_calls == [financial.fiscal_year_end_month]


def test_unknown_relevance_does_not_block_shadow_evaluation_indefinitely() -> None:
    """4.1: 古すぎる決算予定日(stale_earnings_relevance_days=10日を超える)
    かつ財務データの更新が確認できない場合、release_confirmation_stateは
    AWAITING_CONFIRMATION/DELAYED、decision_relevanceはUNKNOWNとなり、
    決算待ちだけを理由にEarnings Surprise/TrendがNOT_APPLICABLEへ無期限
    停止しないことを確認する(データ不足によるNOT_EVALUATEDは許容)。"""
    earnings_date_raw = _NOW.date() - dt.timedelta(days=30)
    fetched_at_override = _NOW - dt.timedelta(days=40)
    providers, _spy = _providers_for_phase_c_earnings_window(earnings_date_raw, fetched_at_override)

    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)

    assert error is None
    assert snapshot is not None
    assert snapshot.earnings_date_status == EarningsDateStatus.STALE_PAST_DATE
    assert snapshot.earnings_surprise.release_confirmation_state in (
        EarningsReleaseConfirmationState.AWAITING_CONFIRMATION,
        EarningsReleaseConfirmationState.DELAYED,
    )
    assert (
        snapshot.earnings_surprise.earnings_decision_relevance == EarningsDecisionRelevance.UNKNOWN
    )
    assert snapshot.earnings_trend.earnings_decision_relevance == EarningsDecisionRelevance.UNKNOWN
    assert snapshot.earnings_trend.release_confirmation_state == (
        snapshot.earnings_surprise.release_confirmation_state
    )
    assert snapshot.earnings_surprise.state != EarningsSurpriseEvaluationState.NOT_APPLICABLE
    assert snapshot.earnings_trend.state != EarningsTrendEvaluationState.NOT_APPLICABLE


def test_relevant_stale_earnings_makes_phase_c_not_applicable() -> None:
    """4.2: 直近(stale_earnings_relevance_days以内)の過去決算予定日で
    財務データの更新が確認できない場合、decision_relevance=RELEVANTとなり
    Earnings Surprise/TrendがNOT_APPLICABLEになる。あわせてMedium対応
    (外部I/O抑止)の証明として、get_earnings_surprise_history()が
    一度も呼ばれないことを確認する(今回最重要の回帰テスト)。"""
    earnings_date_raw = _NOW.date() - dt.timedelta(days=1)
    fetched_at_override = _NOW - dt.timedelta(days=10)
    providers, spy = _providers_for_phase_c_earnings_window(earnings_date_raw, fetched_at_override)

    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)

    assert error is None
    assert snapshot is not None
    assert snapshot.earnings_date_status == EarningsDateStatus.STALE_PAST_DATE
    assert snapshot.earnings_surprise.release_confirmation_state in (
        EarningsReleaseConfirmationState.AWAITING_CONFIRMATION,
        EarningsReleaseConfirmationState.DELAYED,
    )
    assert (
        snapshot.earnings_surprise.earnings_decision_relevance == EarningsDecisionRelevance.RELEVANT
    )
    assert snapshot.earnings_surprise.state == EarningsSurpriseEvaluationState.NOT_APPLICABLE
    assert snapshot.earnings_trend.state == EarningsTrendEvaluationState.NOT_APPLICABLE
    # Medium対応: 必ずNOT_APPLICABLEになると分かっている場合、不要な
    # Yahoo Finance問い合わせ(get_earnings_surprise_history())を行わない。
    assert spy.earnings_surprise_history_call_count == 0


def test_normal_case_still_fetches_earnings_surprise_history() -> None:
    """4.3: phase_c_earnings_blocked=Falseの通常ケース(決算待ちで
    ブロックされていない)では、従来どおりget_earnings_surprise_history()
    が呼ばれる(最適化により一切取得されなくなる回帰を防ぐ)。"""
    base = build_mock_provider_bundle(_NOW)
    spy_financial = _SpyFinancialDataProvider(base.financial_data, _NOW)
    providers = dataclasses.replace(base, financial_data=spy_financial)

    snapshot, error = build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)

    assert error is None
    assert snapshot is not None
    assert spy_financial.earnings_surprise_history_call_count == 1


# --- Issue #59 Phase B4(2026-08-30): provider障害を欠測へロンダリングしない ------


class _FailingEarningsDateDisclosureProvider:
    """次回決算日の取得だけが provider 障害で失敗する disclosure provider。"""

    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    def get_disclosures(self, stock_code: str, since: dt.date) -> object:
        return self._delegate.get_disclosures(stock_code, since)  # type: ignore[attr-defined]

    def get_next_earnings_date(self, stock_code: str) -> dt.date | None:
        raise ProviderDataError(
            provider_name="yfinance",
            operation="get_next_earnings_date",
            retryable=True,
            failure_category=ProviderFailureCategory.RETRYABLE_PROVIDER_FAILURE,
            error_type="RuntimeError",
            error_summary="429 Too Many Requests",
        )


def test_provider_failure_is_not_converted_to_unavailable_status() -> None:
    """T10: provider障害を EarningsDateStatus.UNAVAILABLE へ縮退させない。

    以前は provider 例外が provider 内で None へ潰され、consumer 側で
    「決算予定なし(UNAVAILABLE)」と同義になり、決算直前のBUY抑制ゲートが
    無音ですり抜けていた(business_days_to_earnings=None のため条件不成立)。

    Phase B4 以降は ProviderDataError がそのまま伝播し、呼び出し元の既存
    retry 境界(call_with_rate_limit_retry)が観測する。snapshot 側で
    UNAVAILABLE へ変換しない。
    """
    base = build_mock_provider_bundle(_NOW)
    providers = dataclasses.replace(
        base, disclosure=_FailingEarningsDateDisclosureProvider(base.disclosure)
    )

    with pytest.raises(ProviderDataError) as excinfo:
        build_stock_snapshot(providers, _STOCK_CODE, _NOW, _CFG)

    assert excinfo.value.operation == "get_next_earnings_date"
    assert excinfo.value.retryable is True
