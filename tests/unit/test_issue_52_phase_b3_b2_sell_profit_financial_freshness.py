"""Issue #52 Phase B3-B2: SELL / 利確の既存confidence経路へ財務鮮度を接続する。

B3-B1(BUY)は共通confidence scoreが存在しないため警告のみだった。SELLと利確には
`ConfidenceFactors` -> `compute_confidence()` -> `penalty_*` という共通経路が実在
するため、こちらには減点を接続する。

## 確定仕様(人間確定。ここで再判断しない)

```
penalty_financial_data_stale = 15.0

FRESH     penalty なし / HIGH禁止 なし / warning なし
STALE     penalty 15.0 / HIGH禁止 あり / warning あり
UNKNOWN   penalty なし / HIGH禁止 なし / warning なし(観測のみ)
```

15点とするのは、現行の`stale_data` / `financial_period_incomparable` /
`missing_data`がいずれも15点であり、同程度の信頼性低下として扱うためである。
base=100・HIGH閾値=85のため15点減点だけでは85点でHIGHが残る。「最新決算が未反映
である可能性が確認された状態」を最上位の信頼度にしないため、HIGH禁止条件にも加える。

## 本moduleが固定しないこと

判定そのものの境界値(domain契約)と、財務鮮度の接続先一覧(どのファイルから
呼ばれてよいか)。それは`tests/unit/test_issue_52_phase_b3_a_financial_freshness.py`
にある(接続先の正本を2箇所へ書くと、Phaseごとに両方直す必要が生じるため)。
BUY側の挙動は`tests/unit/test_buy_signal_service.py`の`test_b3_b1_*`にある。
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jstock_advisor.config.loader import load_config
from jstock_advisor.config.models import ConfidenceScoringWeights
from jstock_advisor.domain.entities.common import (
    DataSourceReference,
    PriceWithRationale,
    SellPriceLevels,
)
from jstock_advisor.domain.entities.enums import (
    AccountType,
    ConfidenceLevel,
    RecentPeriodsSource,
    RecommendationType,
    SellIntensity,
    TimingAction,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.signals.confidence_scoring import (
    ConfidenceFactors,
    compute_confidence,
)
from jstock_advisor.domain.signals.profit_taking import ProfitTakingResult, UnrealizedPnl
from jstock_advisor.interfaces.types import Disclosure, FinancialSummary, QuarterlyFinancials
from jstock_advisor.providers.corporate_action.mock_impl import MockCorporateActionProvider
from jstock_advisor.providers.disclosure.mock_impl import MockDisclosureProvider
from jstock_advisor.providers.dividend_data.mock_impl import MockDividendDataProvider
from jstock_advisor.providers.financial_data.mock_impl import MockFinancialDataProvider
from jstock_advisor.providers.market_data.mock_impl import MockMarketDataProvider
from jstock_advisor.providers.shareholder_benefit.mock_impl import MockShareholderBenefitProvider
from jstock_advisor.services import profit_taking_service as pt_module
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.financial_freshness_integration import (
    FINANCIAL_STALE_USER_WARNING,
    assess_financial_freshness,
)
from jstock_advisor.services.profit_taking_service import ProfitTakingService
from jstock_advisor.services.provider_bundle import ProviderBundle
from jstock_advisor.services.sell_signal_service import SellSignalService

_CONFIG = load_config()
_CONF = _CONFIG.confidence
_SRC = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor"

# 期末2026-03-31 -> 期待される次の期末2026-06-30 -> 報告期限 2026-06-30 + 50日
# = 2026-08-19。期限当日はSTALE側に含める(domain契約)。
_LATEST_PERIOD_END = dt.date(2026, 3, 31)
_FRESH_NOW = dt.datetime(2026, 8, 18, 7, 0, tzinfo=dt.UTC)
_STALE_NOW = dt.datetime(2026, 8, 19, 7, 0, tzinfo=dt.UTC)
_STOCK_CODE = "2914"


# ---------------------------------------------------------------------------
# A. confidence scoring 本体(純粋関数)
# ---------------------------------------------------------------------------


def _factors(**overrides: Any) -> ConfidenceFactors:
    """減点が何も発火しない状態を基準にし、検証したい factor だけを変える。"""
    base: dict[str, Any] = {
        "data_freshness_days": 0,
        "primary_source_fetch_rate": 1.0,
        "corporate_action_adjustment_consistent": True,
        "financial_period_comparable": True,
        "fair_value_method_spread_ratio": 1.1,
        "days_to_next_earnings_business_days": 30,
        "latest_quarter_fetched": True,
        "split_adjustment_confirmed": True,
        "record_date_known": True,
        "key_metric_missing": False,
        "primary_secondary_conflict": False,
        "one_time_factors_identified": True,
        "cross_rule_agreement": True,
    }
    base.update(overrides)
    return ConfidenceFactors(**base)


def test_a1_financial_fresh_does_not_deduct_or_disallow() -> None:
    """factorがFalseなら従来どおり(HIGHのまま)。"""
    result = compute_confidence(_factors(financial_data_freshness_stale=False), _CONF)
    assert result.score == _CONF.scoring.base_score
    assert result.level == ConfidenceLevel.HIGH
    assert FINANCIAL_STALE_USER_WARNING not in result.reasons_not_high


def test_a2_financial_stale_deducts_exactly_the_configured_penalty() -> None:
    """減点は設定値ちょうど1回分。"""
    baseline = compute_confidence(_factors(), _CONF)
    stale = compute_confidence(_factors(financial_data_freshness_stale=True), _CONF)
    assert baseline.score - stale.score == _CONF.scoring.penalty_financial_data_stale


def test_a3_financial_stale_reason_appears_exactly_once() -> None:
    """減点理由とHIGH禁止理由で同じ文言を使うが、利用者へは1回しか見せない。"""
    result = compute_confidence(_factors(financial_data_freshness_stale=True), _CONF)
    assert result.reasons_not_high.count(FINANCIAL_STALE_USER_WARNING) == 1


def test_a4_financial_stale_alone_is_medium_not_high() -> None:
    """base100 - 15 = 85 はHIGH閾値ちょうどだが、HIGH禁止によりMEDIUMになる。

    この境界が本Phaseの中核であり、HIGH禁止を外すとHIGHのまま残る。
    """
    result = compute_confidence(_factors(financial_data_freshness_stale=True), _CONF)
    assert result.score == 85.0
    assert result.score >= _CONF.scoring.high_threshold
    assert result.level == ConfidenceLevel.MEDIUM


def test_a5_generic_stale_and_financial_stale_are_counted_once_each() -> None:
    """取得時刻の鮮度と財務期間の鮮度は別事象。それぞれ1回ずつ減点する。"""
    generic_only = compute_confidence(
        _factors(data_freshness_days=_CONF.max_data_freshness_days + 1), _CONF
    )
    both = compute_confidence(
        _factors(
            data_freshness_days=_CONF.max_data_freshness_days + 1,
            financial_data_freshness_stale=True,
        ),
        _CONF,
    )
    w = _CONF.scoring
    assert generic_only.score == w.base_score - w.penalty_stale_data
    assert both.score == w.base_score - w.penalty_stale_data - w.penalty_financial_data_stale
    assert both.reasons_not_high.count(FINANCIAL_STALE_USER_WARNING) == 1


def test_a6_generic_stale_alone_does_not_apply_the_financial_penalty() -> None:
    """取得が古いだけでは財務鮮度の減点は入らない(2つの概念を混同しない)。"""
    result = compute_confidence(
        _factors(data_freshness_days=_CONF.max_data_freshness_days + 1), _CONF
    )
    w = _CONF.scoring
    assert result.score == w.base_score - w.penalty_stale_data
    assert FINANCIAL_STALE_USER_WARNING not in result.reasons_not_high


# ---------------------------------------------------------------------------
# B. config
# ---------------------------------------------------------------------------


def _weights(**overrides: Any) -> dict[str, Any]:
    base = _CONF.scoring.model_dump()
    base.update(overrides)
    return base


def test_b1_human_decided_penalty_is_fifteen() -> None:
    assert _CONF.scoring.penalty_financial_data_stale == 15.0


def test_b2_negative_penalty_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ConfidenceScoringWeights(**_weights(penalty_financial_data_stale=-1.0))


def test_b3_missing_penalty_fails_fast() -> None:
    """キーが無い設定は起動時に落とす(暗黙のPython既定値で動かさない)。"""
    values = _weights()
    del values["penalty_financial_data_stale"]
    with pytest.raises(ValidationError):
        ConfidenceScoringWeights(**values)


def test_b4_financial_penalty_is_a_separate_key_from_fetch_based_staleness() -> None:
    """取得時刻ベースの減点とは別キーであること(片方の変更が他方へ波及しない)。

    現時点では両方とも15.0だが、値が同じであることは同じ設定であることを意味
    しない。片方だけを変えられることをテストで固定する。
    """
    fields = ConfidenceScoringWeights.model_fields
    assert "penalty_stale_data" in fields
    assert "penalty_financial_data_stale" in fields

    changed = ConfidenceScoringWeights(**_weights(penalty_financial_data_stale=1.0))
    assert changed.penalty_financial_data_stale == 1.0
    assert changed.penalty_stale_data == _CONF.scoring.penalty_stale_data


# ---------------------------------------------------------------------------
# C. SELL / 利確が共有する接続helper
# ---------------------------------------------------------------------------

_SOURCE = DataSourceReference(provider="test-fixture", fetched_at=_STALE_NOW)


def _financial(
    *,
    quarterly: bool,
    fiscal_year_end_month: int | None = 3,
    fetched_at: dt.datetime = _STALE_NOW,
) -> FinancialSummary:
    quarters = (
        [
            QuarterlyFinancials(stock_code=_STOCK_CODE, quarter_end=q, source=_SOURCE.model_copy())
            for q in (dt.date(2025, 12, 31), _LATEST_PERIOD_END)
        ]
        if quarterly
        else []
    )
    return FinancialSummary(
        stock_code=_STOCK_CODE,
        stock_name=None,
        fiscal_period_end=_LATEST_PERIOD_END,
        fiscal_year_end_month=fiscal_year_end_month,
        recent_quarters=quarters,
        recent_periods_source=(
            RecentPeriodsSource.QUARTERLY if quarterly else RecentPeriodsSource.UNAVAILABLE
        ),
        source=DataSourceReference(provider="test-fixture", fetched_at=fetched_at),
    )


def test_c1_verdicts_fresh_stale_unknown() -> None:
    fresh = assess_financial_freshness(_financial(quarterly=True), _FRESH_NOW, _CONFIG)
    stale = assess_financial_freshness(_financial(quarterly=True), _STALE_NOW, _CONFIG)
    unknown = assess_financial_freshness(
        _financial(quarterly=False, fiscal_year_end_month=None), _STALE_NOW, _CONFIG
    )
    assert fresh.result.verdict.value == "FRESH"
    assert stale.result.verdict.value == "STALE"
    assert unknown.result.verdict.value == "UNKNOWN"
    assert (fresh.is_stale, stale.is_stale, unknown.is_stale) == (False, True, False)


def test_c2_deadline_day_itself_is_stale() -> None:
    """50暦日の境界。期限前日はFRESH、期限当日はSTALE。"""
    fresh = assess_financial_freshness(_financial(quarterly=True), _FRESH_NOW, _CONFIG)
    stale = assess_financial_freshness(_financial(quarterly=True), _STALE_NOW, _CONFIG)
    assert fresh.result.expected_report_deadline == dt.date(2026, 8, 19)
    assert stale.result.expected_report_deadline == dt.date(2026, 8, 19)
    assert fresh.is_stale is False
    assert stale.is_stale is True


def test_c3_fetched_at_today_but_financial_period_is_stale() -> None:
    """Issue #52の中核: 取得は当日でも、財務期間が古ければSTALEとして検知する。"""
    financial = _financial(quarterly=True, fetched_at=_STALE_NOW)
    assessment = assess_financial_freshness(financial, _STALE_NOW, _CONFIG)
    assert financial.source.fetched_at == _STALE_NOW
    assert assessment.is_stale is True


def test_c4_audit_values_per_verdict() -> None:
    stale = assess_financial_freshness(
        _financial(quarterly=True), _STALE_NOW, _CONFIG
    ).audit_values(_CONFIG)
    fresh = assess_financial_freshness(
        _financial(quarterly=True), _FRESH_NOW, _CONFIG
    ).audit_values(_CONFIG)
    unknown = assess_financial_freshness(
        _financial(quarterly=False, fiscal_year_end_month=None), _STALE_NOW, _CONFIG
    ).audit_values(_CONFIG)

    assert stale["financial_freshness_verdict"] == "STALE"
    assert stale["financial_freshness_warning"] is True
    assert stale["financial_stale_confidence_penalty_applied"] is True
    assert stale["financial_stale_high_confidence_disallowed"] is True
    assert stale["latest_financial_period_end"] == "2026-03-31"
    assert stale["expected_next_financial_period_end"] == "2026-06-30"
    assert stale["expected_financial_report_deadline"] == "2026-08-19"
    assert stale["financial_reporting_lag_calendar_days"] == 50

    for values in (fresh, unknown):
        assert values["financial_freshness_warning"] is False
        assert values["financial_stale_confidence_penalty_applied"] is False
        assert values["financial_stale_high_confidence_disallowed"] is False
    # UNKNOWNでも観測項目は残す(空文字で潰さない)。
    assert unknown["financial_freshness_verdict"] == "UNKNOWN"
    assert unknown["financial_freshness_reason"]


def test_c5_reporting_lag_comes_from_config_not_from_the_helper() -> None:
    """猶予日数の供給は設定の責務(helperへ50を埋め込まない)。"""
    source = (_SRC / "services" / "financial_freshness_integration.py").read_text(encoding="utf-8")
    assert "financial_reporting_lag_calendar_days" in source
    assert "50" not in source


# ---------------------------------------------------------------------------
# D. SELL / 利確への実接続(モックProvider経由の結合)
# ---------------------------------------------------------------------------


class _CapturingAuditService(AuditService):
    """判定が監査へ何を残したかを検証するためのスパイ。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict[str, Any]] = []

    def record(self, *args: Any, **kwargs: Any) -> Any:
        self.records.append(dict(kwargs["output_values"]))
        return super().record(*args, **kwargs)

    def last(self) -> dict[str, Any]:
        assert self.records, "監査レコードが記録されていない"
        return self.records[-1]


class _FixedFinancialProvider:
    """財務期間・由来・決算期末月・取得時刻を固定するフェイク(他は委譲)。"""

    def __init__(self, delegate: Any, financial: FinancialSummary) -> None:
        self._delegate = delegate
        self._financial = financial

    def get_financial_summary(self, stock_code: str) -> FinancialSummary | None:
        summary = self._delegate.get_financial_summary(stock_code)
        if summary is None:
            return None
        return summary.model_copy(
            update={
                "fiscal_period_end": self._financial.fiscal_period_end,
                "fiscal_year_end_month": self._financial.fiscal_year_end_month,
                "recent_quarters": list(self._financial.recent_quarters),
                "recent_periods_source": self._financial.recent_periods_source,
            }
        )

    def get_historical_valuation(self, stock_code: str, years: int) -> list[Any]:
        return self._delegate.get_historical_valuation(stock_code, years)

    def get_cashflow_decomposition(self, stock_code: str) -> Any:
        return self._delegate.get_cashflow_decomposition(stock_code)

    def get_earnings_surprise_history(self, stock_code: str) -> list[Any]:
        return self._delegate.get_earnings_surprise_history(stock_code)


class _FixedEarningsDateDisclosureProvider:
    def __init__(self, delegate: Any, next_earnings_date: dt.date | None) -> None:
        self._delegate = delegate
        self._next_earnings_date = next_earnings_date

    def get_disclosures(self, stock_code: str, since: dt.date) -> list[Disclosure]:
        return self._delegate.get_disclosures(stock_code, since)

    def get_next_earnings_date(self, stock_code: str) -> dt.date | None:
        return self._next_earnings_date


def _providers(financial: FinancialSummary, now: dt.datetime) -> ProviderBundle:
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
        financial_data=_FixedFinancialProvider(base.financial_data, financial),
        # 決算が近いことによるHIGH禁止・決算待ち抑制が混ざらないよう十分先へ置く。
        disclosure=_FixedEarningsDateDisclosureProvider(base.disclosure, dt.date(2026, 11, 13)),
    )


def _holding() -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, _STOCK_CODE),
        stock_code=_STOCK_CODE,
        stock_name="テスト銘柄",
        shares=300,
        average_purchase_price=Decimal("4000"),
        total_purchase_amount=Decimal("1200000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_STALE_NOW,
        updated_at=_STALE_NOW,
    )


def _canned_profit_taking_result() -> ProfitTakingResult:
    return ProfitTakingResult(
        recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE,
        fundamental_action=RecommendationType.PARTIAL_PROFIT_TAKE,
        timing_action=TimingAction.NEUTRAL,
        final_action=RecommendationType.PARTIAL_PROFIT_TAKE,
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
        sell_intensity=SellIntensity.STANDARD,
    )


_CASES = [
    ("STALE", True, None, _STALE_NOW, True),
    ("FRESH", True, 3, _FRESH_NOW, False),
    ("UNKNOWN", False, None, _STALE_NOW, False),
]


def _run_sell(quarterly: bool, fy_month: int | None, now: dt.datetime) -> dict[str, Any]:
    financial = _financial(quarterly=quarterly, fiscal_year_end_month=fy_month)
    audit = _CapturingAuditService()
    service = SellSignalService(
        providers=_providers(financial, now), config=_CONFIG, audit_service=audit
    )
    service.analyze(_holding(), now)
    return audit.last()


@pytest.mark.parametrize(("label", "quarterly", "fy_month", "now", "stale"), _CASES)
def test_d1_sell_records_financial_freshness(
    label: str, quarterly: bool, fy_month: int | None, now: dt.datetime, stale: bool
) -> None:
    """SELLの監査へ、判定に実際に使った財務鮮度が残ること。"""
    values = _run_sell(quarterly, fy_month, now)
    assert values["financial_freshness_verdict"] == label
    assert values["financial_freshness_warning"] is stale
    assert values["financial_stale_confidence_penalty_applied"] is stale
    assert values["financial_stale_high_confidence_disallowed"] is stale
    assert values["financial_reporting_lag_calendar_days"] == 50


def test_d2_sell_stale_lowers_confidence_and_blocks_high() -> None:
    """STALEのときだけ信頼度が下がり、HIGHにならないこと。"""
    stale = _run_sell(True, None, _STALE_NOW)
    fresh = _run_sell(True, 3, _FRESH_NOW)
    unknown = _run_sell(False, None, _STALE_NOW)

    assert stale["confidence_score"] == fresh["confidence_score"] - 15.0
    assert stale["confidence"] != ConfidenceLevel.HIGH.value
    assert FINANCIAL_STALE_USER_WARNING in stale["confidence_reasons"]
    assert stale["confidence_reasons"].count(FINANCIAL_STALE_USER_WARNING) == 1

    # UNKNOWNは減点しない(観測のみ)。FRESHと同じ信頼度になる。
    assert unknown["confidence_score"] == fresh["confidence_score"]
    assert FINANCIAL_STALE_USER_WARNING not in fresh["confidence_reasons"]
    assert FINANCIAL_STALE_USER_WARNING not in unknown["confidence_reasons"]


def test_d3_sell_business_decision_is_not_overridden_by_financial_freshness() -> None:
    """財務鮮度だけで売買判定そのものを変えない。"""
    stale = _run_sell(True, None, _STALE_NOW)
    fresh = _run_sell(True, 3, _FRESH_NOW)
    assert stale["recommendation_type"] == fresh["recommendation_type"]
    assert stale["triggered_rules"] == fresh["triggered_rules"]
    assert stale["reasons"] == fresh["reasons"]


def _run_profit_taking(
    monkeypatch: pytest.MonkeyPatch, quarterly: bool, fy_month: int | None, now: dt.datetime
) -> tuple[dict[str, Any], Any]:
    financial = _financial(quarterly=quarterly, fiscal_year_end_month=fy_month)
    monkeypatch.setattr(
        pt_module, "evaluate_profit_taking", lambda *a, **kw: _canned_profit_taking_result()
    )
    audit = _CapturingAuditService()
    service = ProfitTakingService(
        providers=_providers(financial, now), config=_CONFIG, audit_service=audit
    )
    outcome = service.analyze(_holding(), now)
    return audit.last(), outcome


@pytest.mark.parametrize(("label", "quarterly", "fy_month", "now", "stale"), _CASES)
def test_d4_profit_taking_records_financial_freshness(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    quarterly: bool,
    fy_month: int | None,
    now: dt.datetime,
    stale: bool,
) -> None:
    values, _ = _run_profit_taking(monkeypatch, quarterly, fy_month, now)
    assert values["financial_freshness_verdict"] == label
    assert values["financial_freshness_warning"] is stale
    assert values["financial_stale_confidence_penalty_applied"] is stale
    assert values["financial_stale_high_confidence_disallowed"] is stale


def test_d5_profit_taking_stale_lowers_confidence_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STALEのときだけ減点され、利用者向けの留意事項が付くこと。"""
    stale_values, stale_outcome = _run_profit_taking(monkeypatch, True, None, _STALE_NOW)
    fresh_values, fresh_outcome = _run_profit_taking(monkeypatch, True, 3, _FRESH_NOW)

    assert stale_values["confidence_score"] == fresh_values["confidence_score"] - 15.0
    assert stale_values["confidence"] != ConfidenceLevel.HIGH.value
    assert stale_values["confidence_reasons"].count(FINANCIAL_STALE_USER_WARNING) == 1

    assert stale_outcome.recommendation is not None
    assert fresh_outcome.recommendation is not None
    assert FINANCIAL_STALE_USER_WARNING in stale_outcome.recommendation.key_risks
    assert FINANCIAL_STALE_USER_WARNING not in fresh_outcome.recommendation.key_risks


def test_d6_profit_taking_unknown_is_observability_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unknown_values, unknown_outcome = _run_profit_taking(monkeypatch, False, None, _STALE_NOW)
    fresh_values, _ = _run_profit_taking(monkeypatch, True, 3, _FRESH_NOW)
    assert unknown_values["confidence_score"] == fresh_values["confidence_score"]
    assert unknown_outcome.recommendation is not None
    assert FINANCIAL_STALE_USER_WARNING not in unknown_outcome.recommendation.key_risks
    assert unknown_values["financial_freshness_verdict"] == "UNKNOWN"


def test_d7_profit_taking_business_rules_are_not_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """利確の判定・売却価格・数量は財務鮮度で変わらない。"""
    _, stale_outcome = _run_profit_taking(monkeypatch, True, None, _STALE_NOW)
    _, fresh_outcome = _run_profit_taking(monkeypatch, True, 3, _FRESH_NOW)
    stale_rec = stale_outcome.recommendation
    fresh_rec = fresh_outcome.recommendation
    assert stale_rec is not None
    assert fresh_rec is not None
    assert stale_rec.recommendation_type == fresh_rec.recommendation_type
    assert stale_rec.reasons == fresh_rec.reasons


# ---------------------------------------------------------------------------
# E. at-most-once と BUY の非回帰(構造で固定する)
# ---------------------------------------------------------------------------


def _financial_stale_factor_assignments(path: Path) -> list[ast.keyword]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        kw
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "financial_data_freshness_stale"
    ]


@pytest.mark.parametrize("service", ["sell_signal_service", "profit_taking_service"])
def test_e1_financial_stale_is_written_to_exactly_one_factor(service: str) -> None:
    """同じ事実を複数のfactorへ書かない(AT_MOST_ONCE を構造で保証する)。"""
    path = _SRC / "services" / f"{service}.py"
    assignments = _financial_stale_factor_assignments(path)
    assert len(assignments) == 1, f"{service}: 専用factorへの代入は1箇所のみ"

    # 他の既存penalty factorへ財務鮮度を書いていないこと。
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {
        "data_freshness_days",
        "financial_period_comparable",
        "latest_quarter_fetched",
        "key_metric_missing",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg in forbidden:
                names = {n.id for n in ast.walk(kw.value) if isinstance(n, ast.Name)}
                assert "financial_freshness" not in names, f"{service}: {kw.arg}へ財務鮮度を混入"


def test_e2_confidence_scoring_deducts_the_financial_penalty_once() -> None:
    """domain側でも減算は1箇所だけ。"""
    source = (_SRC / "domain" / "signals" / "confidence_scoring.py").read_text(encoding="utf-8")
    assert source.count("score -= w.penalty_financial_data_stale") == 1


def test_e3_buy_is_not_connected_to_the_common_confidence_path() -> None:
    """BUY(B3-B1)は変更しない。共通confidence scoreを持ち込まない。"""
    buy = (_SRC / "services" / "buy_signal_service.py").read_text(encoding="utf-8")
    tree = ast.parse(buy)
    forbidden = {"compute_confidence", "ConfidenceFactors", "financial_data_freshness_stale"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert "confidence_scoring" not in (node.module or "")
            assert forbidden.isdisjoint({a.name for a in node.names})
        elif isinstance(node, ast.Name):
            assert node.id not in forbidden
        elif isinstance(node, ast.keyword):
            assert node.arg not in forbidden
