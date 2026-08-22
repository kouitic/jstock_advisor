import datetime as dt
from decimal import Decimal

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.common import DataSourceReference
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    CorporateActionType,
    RecommendationType,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.interfaces.types import CorporateActionEvent
from jstock_advisor.services.data_quality_service import (
    DataQualityIssueSeverity,
    check_split_consistency,
    detect_anomalies,
)

_NOW = dt.datetime(2026, 7, 27, tzinfo=dt.UTC)
_SOURCE = DataSourceReference(provider="test", fetched_at=_NOW)
_CONFIG = load_config().data_validation


def _holding(
    average_purchase_price: Decimal, basis_date: dt.date | None = None
) -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, "5401"),
        stock_code="5401",
        stock_name="日本製鉄",
        shares=100,
        average_purchase_price=average_purchase_price,
        total_purchase_amount=average_purchase_price * 100,
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type="GENERAL",  # type: ignore[arg-type]
        created_at=_NOW,
        updated_at=_NOW,
        shares_and_price_adjustment_basis_date=basis_date,
    )


def test_fair_value_divergence_resembling_split_ratio_flagged_without_known_split() -> None:
    issues = check_split_consistency(
        stock_code="5401",
        current_price=Decimal("636.9"),
        bars_close_by_date=[],
        fair_value=Decimal("127.4"),  # 636.9/127.4 ≈ 5.0倍
        actual_annual_dividend_per_share=None,
        previous_fiscal_year_dividend_per_share=None,
        corporate_action_events=[],  # 分割イベントなし
        holding=None,
        now=_NOW,
        config=_CONFIG.split_consistency,
    )
    names = [i.check_name for i in issues]
    assert "fair_value_divergence_resembles_split_ratio" in names
    assert all(i.severity == DataQualityIssueSeverity.BLOCKING for i in issues)


def test_fair_value_divergence_not_flagged_when_split_is_known() -> None:
    events = [
        CorporateActionEvent(
            stock_code="5401",
            event_type=CorporateActionType.SPLIT,
            announced_date=dt.date(2025, 10, 1),
            effective_date=dt.date(2025, 10, 1),
            ratio=Decimal("5"),
            source=_SOURCE,
        )
    ]
    issues = check_split_consistency(
        stock_code="5401",
        current_price=Decimal("636.9"),
        bars_close_by_date=[],
        fair_value=Decimal("127.4"),
        actual_annual_dividend_per_share=None,
        previous_fiscal_year_dividend_per_share=None,
        corporate_action_events=events,
        holding=None,
        now=_NOW,
        config=_CONFIG.split_consistency,
    )
    assert issues == []


def test_purchase_price_basis_mismatch_flagged() -> None:
    events = [
        CorporateActionEvent(
            stock_code="5401",
            event_type=CorporateActionType.SPLIT,
            announced_date=dt.date(2025, 10, 1),
            effective_date=dt.date(2025, 10, 1),
            ratio=Decimal("5"),
            source=_SOURCE,
        )
    ]
    # 平均取得単価が現在株価の約5倍(分割前基準のまま未調整)、
    # かつ調整基準日が分割より前(=未調整)
    issues = check_split_consistency(
        stock_code="5401",
        current_price=Decimal("700"),
        bars_close_by_date=[],
        fair_value=None,
        actual_annual_dividend_per_share=None,
        previous_fiscal_year_dividend_per_share=None,
        corporate_action_events=events,
        holding=_holding(Decimal("3500"), basis_date=dt.date(2024, 1, 1)),
        now=_NOW,
        config=_CONFIG.split_consistency,
    )
    names = [i.check_name for i in issues]
    assert "purchase_price_basis_mismatch" in names


def test_purchase_price_basis_mismatch_not_flagged_when_already_adjusted() -> None:
    events = [
        CorporateActionEvent(
            stock_code="5401",
            event_type=CorporateActionType.SPLIT,
            announced_date=dt.date(2025, 10, 1),
            effective_date=dt.date(2025, 10, 1),
            ratio=Decimal("5"),
            source=_SOURCE,
        )
    ]
    issues = check_split_consistency(
        stock_code="5401",
        current_price=Decimal("700"),
        bars_close_by_date=[],
        fair_value=None,
        actual_annual_dividend_per_share=None,
        previous_fiscal_year_dividend_per_share=None,
        corporate_action_events=events,
        holding=_holding(Decimal("700"), basis_date=dt.date(2026, 7, 27)),
        now=_NOW,
        config=_CONFIG.split_consistency,
    )
    assert issues == []


def test_no_issues_when_nothing_anomalous() -> None:
    issues = check_split_consistency(
        stock_code="2914",
        current_price=Decimal("6531"),
        bars_close_by_date=[],
        fair_value=Decimal("4840"),
        actual_annual_dividend_per_share=Decimal("242"),
        previous_fiscal_year_dividend_per_share=Decimal("230"),
        corporate_action_events=[],
        holding=None,
        now=_NOW,
        config=_CONFIG.split_consistency,
    )
    assert issues == []


def _recommendation(
    fair_value: Decimal | None,
    price: Decimal,
    recommendation_type: RecommendationType = RecommendationType.WATCH,
    average_purchase_price: Decimal | None = None,
    dividend_yield_pct: float | None = None,
    dividend_record_date: dt.date | None = None,
) -> Recommendation:
    return Recommendation(
        recommendation_id="rec-1",
        stock_code="2914",
        stock_name="JT",
        recommended_at=_NOW,
        recommendation_type=recommendation_type,
        price_at_recommendation=price,
        average_purchase_price_at_recommendation=average_purchase_price,
        fair_value_at_recommendation=fair_value,
        dividend_yield_pct_at_recommendation=dividend_yield_pct,
        dividend_record_date=dividend_record_date,
        confidence=ConfidenceLevel.MEDIUM,
        rule_version="v1-mvp",
    )


def test_fair_value_out_of_plausible_range_flagged() -> None:
    current = _recommendation(fair_value=Decimal("100"), price=Decimal("1000"))  # 0.1倍
    issues = detect_anomalies("2914", current, None, _CONFIG.anomaly_detection)
    names = [i.check_name for i in issues]
    assert "fair_value_out_of_plausible_range" in names


def test_fair_value_changed_sharply_flagged() -> None:
    previous = _recommendation(fair_value=Decimal("4000"), price=Decimal("6000"))
    current = _recommendation(fair_value=Decimal("6000"), price=Decimal("6000"))  # +50%
    issues = detect_anomalies("2914", current, previous, _CONFIG.anomaly_detection)
    names = [i.check_name for i in issues]
    assert "fair_value_changed_sharply" in names


def test_full_profit_take_with_unrealized_loss_flagged() -> None:
    current = _recommendation(
        fair_value=Decimal("5000"),
        price=Decimal("900"),
        recommendation_type=RecommendationType.FULL_PROFIT_TAKE,
        average_purchase_price=Decimal("1000"),  # 現在値900 < 取得単価1000 = 含み損
    )
    issues = detect_anomalies("2914", current, None, _CONFIG.anomaly_detection)
    names = [i.check_name for i in issues]
    assert "full_profit_take_with_unrealized_loss" in names
    assert any(i.severity == DataQualityIssueSeverity.BLOCKING for i in issues)


def test_record_date_regression_flagged() -> None:
    previous = _recommendation(
        fair_value=Decimal("5000"), price=Decimal("6000"), dividend_record_date=dt.date(2026, 3, 31)
    )
    current = _recommendation(fair_value=Decimal("5000"), price=Decimal("6000"))
    issues = detect_anomalies("2914", current, previous, _CONFIG.anomaly_detection)
    names = [i.check_name for i in issues]
    assert "record_date_regressed_to_unknown" in names


def test_no_anomalies_for_stable_recommendation() -> None:
    previous = _recommendation(fair_value=Decimal("5000"), price=Decimal("6000"))
    current = _recommendation(fair_value=Decimal("5100"), price=Decimal("6050"))
    issues = detect_anomalies("2914", current, previous, _CONFIG.anomaly_detection)
    assert issues == []
