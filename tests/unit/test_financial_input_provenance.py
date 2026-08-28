"""financial input provenance(Issue #20 Phase B2-A、fp1)のテスト。

- builderがsnapshot構築時点の事実のみから決定的にprovenanceを組み立てること
- 値種別semantics(予想EPS/配当=PROVIDER_FORECAST_UNSPECIFIED、
  予想BPS=UNKNOWN、実績配当=ACTUAL。根拠なくCOMPANY_FORECASTとしない)
- NOT_AVAILABLE(available=False)とNOT_CAPTURED(provenance=None)の分離
- BUY/SELL両パイプラインでのcapture(実mock providerパイプライン)
- serialization往復・旧レコード互換
を固定する。判定・スコア・valuationへの影響ゼロ(観測専用)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.business_calendar import BusinessCalendar
from jstock_advisor.domain.entities.enums import (
    AccountType,
    ConfidenceLevel,
    RecentPeriodsSource,
    RecommendationType,
)
from jstock_advisor.domain.entities.financial_input_provenance import (
    FINANCIAL_INPUT_PROVENANCE_SCHEMA_VERSION,
    FinancialInputProvenance,
    FinancialValueSourceType,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.stock_snapshot_service import (
    build_financial_input_provenance,
    build_stock_snapshot,
)

_NOW = dt.datetime(2026, 8, 9, tzinfo=dt.UTC)
_CONFIG = load_config()


def _snapshot(stock_code: str = "2914"):
    snapshot, error = build_stock_snapshot(
        build_mock_provider_bundle(_NOW), stock_code, _NOW, _CONFIG
    )
    assert error is None and snapshot is not None
    return snapshot


# --- builder(純関数) ------------------------------------------------------


def test_builder_captures_fiscal_period_and_observation_facts() -> None:
    snapshot = _snapshot()
    provenance = build_financial_input_provenance(snapshot.financial, snapshot.dividend)

    assert provenance.provenance_schema_version == FINANCIAL_INPUT_PROVENANCE_SCHEMA_VERSION
    assert provenance.fiscal_period_end == snapshot.financial.fiscal_period_end
    assert provenance.fiscal_year_end_month == snapshot.financial.fiscal_year_end_month
    assert provenance.recent_periods_source == snapshot.financial.recent_periods_source
    # latest_quarter_endはrecent_quarters中の最大期末日の生値(再判定しない)
    quarter_ends = [q.quarter_end for q in snapshot.financial.recent_quarters]
    assert provenance.latest_quarter_end == (max(quarter_ends) if quarter_ends else None)
    # observed_at=「システムが取得した時刻」(公開時刻ではない)
    assert provenance.financial_provider == snapshot.financial.source.provider
    assert provenance.financial_observed_at == snapshot.financial.source.fetched_at


def test_builder_value_source_semantics() -> None:
    """予想EPS/予想配当は出所不明の予想としてPROVIDER_FORECAST_UNSPECIFIED、
    予想BPSは種別を断定できないためUNKNOWN、実績配当はACTUAL。
    根拠なくCOMPANY_FORECASTを付けない。"""
    snapshot = _snapshot()
    provenance = build_financial_input_provenance(snapshot.financial, snapshot.dividend)

    assert provenance.forecast_eps_source is not None
    assert (
        provenance.forecast_eps_source.source_type
        is FinancialValueSourceType.PROVIDER_FORECAST_UNSPECIFIED
    )
    assert provenance.forecast_bps_source is not None
    assert provenance.forecast_bps_source.source_type is FinancialValueSourceType.UNKNOWN
    assert provenance.forecast_dividend_source is not None
    assert (
        provenance.forecast_dividend_source.source_type
        is FinancialValueSourceType.PROVIDER_FORECAST_UNSPECIFIED
    )
    assert provenance.actual_dividend_source is not None
    assert provenance.actual_dividend_source.source_type is FinancialValueSourceType.ACTUAL
    # COMPANY_FORECASTはfp1のbuilderからは生成されない
    for source in (
        provenance.forecast_eps_source,
        provenance.forecast_bps_source,
        provenance.forecast_dividend_source,
        provenance.actual_dividend_source,
    ):
        assert source.source_type is not FinancialValueSourceType.COMPANY_FORECAST


def test_builder_not_available_when_provider_omits_value() -> None:
    """providerが値を提供しなかった場合はavailable=False(NOT_AVAILABLE)。
    取得を試みた先(provider/observed_at)の事実は保持する。現在値からの
    補完はしない。"""
    snapshot = _snapshot()
    financial = snapshot.financial.model_copy(update={"forecast_eps": None})
    dividend = snapshot.dividend.model_copy(
        update={"forecast_annual_dividend_per_share": None}
    )
    provenance = build_financial_input_provenance(financial, dividend)

    assert provenance.forecast_eps_source is not None
    assert provenance.forecast_eps_source.available is False
    assert provenance.forecast_eps_source.provider == financial.source.provider
    assert provenance.forecast_dividend_source is not None
    assert provenance.forecast_dividend_source.available is False
    # 提供された値はavailable=Trueのまま
    assert provenance.forecast_bps_source is not None
    assert provenance.forecast_bps_source.available is (financial.forecast_bps is not None)


def test_builder_handles_missing_quarters_without_guessing() -> None:
    financial = _snapshot().financial.model_copy(
        update={
            "recent_quarters": [],
            "recent_periods_source": RecentPeriodsSource.UNAVAILABLE,
            "fiscal_period_end": None,
        }
    )
    provenance = build_financial_input_provenance(financial, _snapshot().dividend)
    assert provenance.latest_quarter_end is None
    assert provenance.fiscal_period_end is None  # 補完・推測しない
    assert provenance.recent_periods_source is RecentPeriodsSource.UNAVAILABLE


def test_fp1_has_no_publication_timestamp_field() -> None:
    """fp1はpublication(公開)時刻をcapture/保証しない。取得できない値のための
    固定UNKNOWNフィールドを置かないことをschemaレベルで固定する。"""
    field_names = set(FinancialInputProvenance.model_fields)
    assert not any("publication" in name or "announce" in name for name in field_names)


# --- pipeline capture ------------------------------------------------------


def test_buy_pipeline_captures_provenance_without_behavior_change() -> None:
    from jstock_advisor.services.buy_signal_service import BuySignalService

    service = BuySignalService(
        providers=build_mock_provider_bundle(_NOW),
        config=_CONFIG,
        business_calendar=BusinessCalendar.from_config(_CONFIG.holiday_calendar),
    )
    outcome = service.analyze("2914", _NOW, RecommendationType.BUY)
    assert outcome.recommendation is not None
    rec = outcome.recommendation

    provenance = rec.financial_input_provenance
    assert provenance is not None
    assert provenance.provenance_schema_version == "fp1"
    # 値とprovenanceの対応: forecast_eps_sourceはRecommendation.forecast_epsを説明する
    assert provenance.forecast_eps_source is not None
    assert provenance.forecast_eps_source.available is (rec.forecast_eps is not None)


def test_sell_pipeline_captures_provenance() -> None:
    from jstock_advisor.services.profit_taking_service import ProfitTakingService

    holding = Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, "2914"),
        stock_code="2914",
        stock_name="テスト銘柄",
        shares=300,
        average_purchase_price=Decimal("1000"),
        total_purchase_amount=Decimal("300000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )
    service = ProfitTakingService(providers=build_mock_provider_bundle(_NOW), config=_CONFIG)
    outcome = service.analyze(holding, _NOW)
    assert outcome.recommendation is not None
    provenance = outcome.recommendation.financial_input_provenance
    assert provenance is not None
    assert provenance.provenance_schema_version == "fp1"
    assert provenance.financial_observed_at is not None


# --- serialization / 互換 --------------------------------------------------


def test_recommendation_serialization_round_trip_with_provenance() -> None:
    snapshot = _snapshot()
    provenance = build_financial_input_provenance(snapshot.financial, snapshot.dividend)
    rec = Recommendation(
        recommendation_id="fp1-rec-1",
        stock_code="2914",
        stock_name="テスト銘柄",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        price_at_recommendation=Decimal("1200"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-test",
        financial_input_provenance=provenance,
    )
    restored = Recommendation.model_validate_json(rec.model_dump_json())
    assert restored.financial_input_provenance == provenance


def test_recommendation_without_provenance_means_not_captured() -> None:
    """financial_input_provenance未指定(旧レコード相当)はNone=NOT_CAPTURED。"""
    rec = Recommendation(
        recommendation_id="fp1-rec-0",
        stock_code="2914",
        stock_name="テスト銘柄",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        price_at_recommendation=Decimal("1200"),
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-test",
    )
    assert rec.financial_input_provenance is None
