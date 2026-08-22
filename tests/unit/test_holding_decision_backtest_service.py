"""保有判断スコアのバックテスト/リプレイのテスト(実装プラン修正5、コードレビュー対応)。

live比較モード(--start-date未指定)とreplayモード(--start-date指定)の
両方について、指定銘柄・全銘柄・CSV出力・空データ時の挙動を検証する。
history replayの対応付け(旧方式マッチング・新方式FK検証・通知実績分離・
JST基準の同一日判定)を重点的に検証する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import (
    AccountType,
    ConfidenceLevel,
    ExecutionPlanReason,
    NotificationType,
    RecommendationType,
)
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.holding_decision import (
    CompanyQualityScore,
    ComponentCoverage,
    HoldingDecisionHardGate,
    HoldingDecisionResult,
    InvestmentThesisScore,
    RiskDeductionScore,
)
from jstock_advisor.domain.entities.notification import NotificationLog
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.domain.entities.recommendation import Recommendation
from jstock_advisor.domain.signals.holding_decision_score import combine_holding_decision
from jstock_advisor.infrastructure.local_repository.holding_decision_result_repository import (
    HoldingDecisionResultRepository,
)
from jstock_advisor.infrastructure.local_repository.holding_repository import (
    HoldingRepository,
)
from jstock_advisor.infrastructure.local_repository.notification_log_repository import (
    NotificationLogRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.services.holding_decision_backtest_service import (
    BacktestNotificationStatus,
    LegacyRecommendationMatchMethod,
    NewRecommendationMatchMethod,
    RecommendationMatchConfidence,
    _as_aware_utc,
    _jst_date_range_to_utc,
    _match_legacy_recommendation,
    business_date,
    resolve_target_stock_codes,
    run_history_replay,
    run_live_comparison,
    write_backtest_csv,
)
from jstock_advisor.services.portfolio_service import PortfolioService
from jstock_advisor.services.provider_factory import build_mock_provider_bundle
from jstock_advisor.services.sell_signal_service import SellSignalOutcome, SellSignalService

_CFG = load_config()
_RULES = _CFG.holding_decision
_NOW = dt.datetime(2026, 8, 5, tzinfo=dt.UTC)
_PROVIDERS = build_mock_provider_bundle(_NOW)


def _holding(stock_code: str) -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        stock_name="x",
        shares=100,
        average_purchase_price=Decimal("1000"),
        total_purchase_amount=Decimal("100000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _recommendation(
    stock_code: str,
    recommended_at: dt.datetime,
    recommendation_type: RecommendationType = RecommendationType.SELL,
    recommendation_id: str | None = None,
) -> Recommendation:
    return Recommendation(
        recommendation_id=recommendation_id or f"rec-{stock_code}-{recommended_at.isoformat()}",
        stock_code=stock_code,
        stock_name="test",
        recommended_at=recommended_at,
        recommendation_type=recommendation_type,
        price_at_recommendation=Decimal("1000"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1",
    )


def _hd_result(
    stock_code: str,
    evaluated_at: dt.datetime,
    *,
    result_id: str,
    should_notify: bool,
    recommendation_id: str | None = None,
    execution_plan_reason: ExecutionPlanReason = ExecutionPlanReason.NORMAL_SHADOW,
) -> HoldingDecisionResult:
    if should_notify:
        q, i, r = (
            CompanyQualityScore(score=10, coverage_ratio=1.0),
            InvestmentThesisScore(score=10, coverage_ratio=1.0),
            RiskDeductionScore(score=70, coverage_ratio=1.0),
        )
    else:
        q, i, r = (
            CompanyQualityScore(score=50, coverage_ratio=1.0),
            InvestmentThesisScore(score=50, coverage_ratio=1.0),
            RiskDeductionScore(score=0, coverage_ratio=1.0),
        )
    gate = HoldingDecisionHardGate(triggered=False)
    outcome = combine_holding_decision(q, i, r, gate, _RULES)
    assert outcome.should_notify is should_notify
    return HoldingDecisionResult(
        holding_decision_result_id=result_id,
        holding_id=stock_code,
        stock_code=stock_code,
        evaluated_at=evaluated_at,
        company_quality=q,
        investment_thesis=i,
        risk_deduction=r,
        base_score=outcome.base_score,
        hard_gate=outcome.hard_gate,
        final_score=outcome.final_score,
        display_value=outcome.display_value,
        category=outcome.category,
        coverage=ComponentCoverage(
            overall=1.0, company_quality=1.0, investment_thesis=1.0, risk_deduction=1.0
        ),
        confidence=outcome.confidence,
        should_notify=outcome.should_notify,
        recommendation_id=recommendation_id,
        scoring_model_version=_RULES.scoring_model_version,
        runtime_config_version=1,
        execution_plan_reason=execution_plan_reason,
    )


def _sent_log(recommendation_id: str, sent_at: dt.datetime) -> NotificationLog:
    return NotificationLog(
        notification_id=f"log-{recommendation_id}-{sent_at.isoformat()}",
        notification_type=NotificationType.SELL_SIGNAL,
        stock_code="2914",
        content_hash="hash",
        sent_at=sent_at,
        related_recommendation_id=recommendation_id,
    )


# ===== resolve_target_stock_codes =====


def test_resolve_target_stock_codes_uses_explicit_list_when_given(store_dir: Path):
    portfolio = PortfolioService(holding_repository=HoldingRepository(store_dir=store_dir))
    result = resolve_target_stock_codes(["2914", "9861"], portfolio_service=portfolio)
    assert result == ["2914", "9861"]


def test_resolve_target_stock_codes_deduplicates_while_preserving_order(store_dir: Path):
    portfolio = PortfolioService(holding_repository=HoldingRepository(store_dir=store_dir))
    result = resolve_target_stock_codes(["2914", "9861", "2914"], portfolio_service=portfolio)
    assert result == ["2914", "9861"]


def test_resolve_target_stock_codes_falls_back_to_all_holdings(store_dir: Path):
    holding_repo = HoldingRepository(store_dir=store_dir)
    portfolio = PortfolioService(holding_repository=holding_repo)
    holding_repo.upsert(_holding("2914"))
    holding_repo.upsert(_holding("9861"))
    result = resolve_target_stock_codes([], portfolio_service=portfolio)
    assert set(result) == {"2914", "9861"}


def test_resolve_target_stock_codes_empty_when_no_holdings_and_no_explicit_codes(store_dir: Path):
    portfolio = PortfolioService(holding_repository=HoldingRepository(store_dir=store_dir))
    assert resolve_target_stock_codes([], portfolio_service=portfolio) == []


# ===== run_live_comparison =====


def test_live_comparison_returns_one_row_per_stock_code():
    rows = run_live_comparison(["2914", "9861"], _PROVIDERS, _CFG, _NOW)
    assert [r.stock_code for r in rows] == ["2914", "9861"]
    assert all(r.source == "live" for r in rows)


def test_live_comparison_row_has_legacy_and_new_engine_fields(store_dir: Path):
    holding_repo = HoldingRepository(store_dir=store_dir)
    holding_repo.upsert(_holding("2914"))
    portfolio = PortfolioService(holding_repository=holding_repo)
    rows = run_live_comparison(["2914"], _PROVIDERS, _CFG, _NOW, portfolio_service=portfolio)
    row = rows[0]
    assert row.legacy_recommendation_type is not None
    assert row.new_score is not None
    assert row.new_category is not None
    assert isinstance(row.new_should_notify, bool)
    assert isinstance(row.legacy_should_notify, bool)


def test_live_comparison_never_created_or_sent_since_nothing_is_persisted():
    """liveモードは何も永続化・送信しないため、recommendation_created/
    notification_sentは常にFalse(should_notifyとは独立)。"""
    rows = run_live_comparison(["2914"], _PROVIDERS, _CFG, _NOW)
    row = rows[0]
    assert row.legacy_recommendation_created is False
    assert row.new_recommendation_created is False
    assert row.legacy_notification_sent is False
    assert row.new_notification_sent is False
    assert row.legacy_notification_status == BacktestNotificationStatus.NOT_EXECUTED_LIVE_MODE.value
    assert row.new_notification_status == BacktestNotificationStatus.NOT_EXECUTED_LIVE_MODE.value


def test_live_comparison_non_holding_stock_skips_legacy_engine(store_dir: Path, monkeypatch):
    """非保有銘柄は旧方式(SellSignalService)を評価しない(コードレビュー対応:
    架空の取得単価・保有期間による誤評価を防ぐ)。新方式のみ評価される。"""
    portfolio = PortfolioService(holding_repository=HoldingRepository(store_dir=store_dir))
    called = {"count": 0}

    def _spy_analyze(self, holding, now, snapshot=None):
        called["count"] += 1
        return SellSignalOutcome(holding.stock_code, None, None)

    monkeypatch.setattr(SellSignalService, "analyze", _spy_analyze)
    rows = run_live_comparison(["2914"], _PROVIDERS, _CFG, _NOW, portfolio_service=portfolio)
    assert called["count"] == 0
    assert rows[0].legacy_recommendation_type == "NOT_EVALUATED_NON_HOLDING"
    assert rows[0].legacy_should_notify is None
    assert rows[0].legacy_recommendation_created is False
    # 新方式は非保有でも評価される(取得単価・株数を入力に使わないため)。
    assert rows[0].new_score is not None


def test_live_comparison_holding_overrides_used_for_non_holding_stock(store_dir: Path):
    """--purchase-price等で明示指定した場合のみ、非保有銘柄でも旧方式を評価する。"""
    portfolio = PortfolioService(holding_repository=HoldingRepository(store_dir=store_dir))
    override = Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, "2914"),
        stock_code="2914",
        stock_name="override",
        shares=200,
        average_purchase_price=Decimal("500"),
        total_purchase_amount=Decimal("100000"),
        first_purchase_date=dt.date(2026, 1, 1),
        last_purchase_date=dt.date(2026, 1, 1),
        account_type=AccountType.GENERAL,
        created_at=_NOW,
        updated_at=_NOW,
    )
    rows = run_live_comparison(
        ["2914"],
        _PROVIDERS,
        _CFG,
        _NOW,
        portfolio_service=portfolio,
        holding_overrides={"2914": override},
    )
    assert rows[0].legacy_recommendation_type != "NOT_EVALUATED_NON_HOLDING"


def test_live_comparison_holding_overrides_conflicts_with_actual_holding_raises(store_dir: Path):
    holding_repo = HoldingRepository(store_dir=store_dir)
    holding_repo.upsert(_holding("2914"))
    portfolio = PortfolioService(holding_repository=holding_repo)
    override = _holding("2914")
    with pytest.raises(ValueError, match="既に保有銘柄として登録されている"):
        run_live_comparison(
            ["2914"],
            _PROVIDERS,
            _CFG,
            _NOW,
            portfolio_service=portfolio,
            holding_overrides={"2914": override},
        )


def test_live_comparison_holding_overrides_not_persisted(store_dir: Path):
    """CLI入力から生成した一時Holdingはどのrepositoryへも保存されない。"""
    holding_repo = HoldingRepository(store_dir=store_dir)
    portfolio = PortfolioService(holding_repository=holding_repo)
    override = _holding("2914")
    run_live_comparison(
        ["2914"],
        _PROVIDERS,
        _CFG,
        _NOW,
        portfolio_service=portfolio,
        holding_overrides={"2914": override},
    )
    assert holding_repo.get("2914") is None


# ===== _match_legacy_recommendation =====


def test_match_nearest_timestamp_when_single_candidate_within_window():
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    candidate = _recommendation("2914", dt.datetime(2026, 6, 15, 8, 2, tzinfo=dt.UTC))
    rec, method, confidence, warning = _match_legacy_recommendation(
        evaluated_at, [candidate], False
    )
    assert rec is candidate
    assert method == LegacyRecommendationMatchMethod.NEAREST_TIMESTAMP
    assert confidence == RecommendationMatchConfidence.HIGH
    assert warning is None


def test_match_ambiguous_when_multiple_candidates_within_window():
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    candidates = [
        _recommendation(
            "2914", dt.datetime(2026, 6, 15, 8, 1, tzinfo=dt.UTC), recommendation_id="a"
        ),
        _recommendation(
            "2914", dt.datetime(2026, 6, 15, 8, 3, tzinfo=dt.UTC), recommendation_id="b"
        ),
    ]
    rec, method, confidence, warning = _match_legacy_recommendation(evaluated_at, candidates, False)
    assert rec is None
    assert method == LegacyRecommendationMatchMethod.AMBIGUOUS_MATCH
    assert confidence == RecommendationMatchConfidence.LOW
    assert warning is not None


def test_match_ambiguous_even_when_time_deltas_are_equal():
    """最小時間差が一意でも、近接時間内に複数候補があれば最も近い1件を自動採用せずAMBIGUOUS。"""
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    candidates = [
        _recommendation(
            "2914", dt.datetime(2026, 6, 15, 7, 58, tzinfo=dt.UTC), recommendation_id="a"
        ),
        _recommendation(
            "2914", dt.datetime(2026, 6, 15, 8, 2, tzinfo=dt.UTC), recommendation_id="b"
        ),
    ]
    rec, method, confidence, warning = _match_legacy_recommendation(evaluated_at, candidates, False)
    assert rec is None
    assert method == LegacyRecommendationMatchMethod.AMBIGUOUS_MATCH


def test_match_no_match_by_default_when_only_same_day_candidate_exists():
    # 両者ともJST 2026-06-15(evaluated_at=17:00 JST、candidate=19:00 JST)。
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    candidate = _recommendation("2914", dt.datetime(2026, 6, 15, 10, 0, tzinfo=dt.UTC))
    rec, method, confidence, warning = _match_legacy_recommendation(
        evaluated_at, [candidate], allow_same_day_fallback=False
    )
    assert rec is None
    assert method == LegacyRecommendationMatchMethod.NO_MATCH
    assert confidence == RecommendationMatchConfidence.NONE
    assert warning is not None and "same-day fallback" in warning


def test_match_same_day_fallback_when_explicitly_enabled():
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    candidate = _recommendation("2914", dt.datetime(2026, 6, 15, 10, 0, tzinfo=dt.UTC))
    rec, method, confidence, warning = _match_legacy_recommendation(
        evaluated_at, [candidate], allow_same_day_fallback=True
    )
    assert rec is candidate
    assert method == LegacyRecommendationMatchMethod.SAME_DAY_FALLBACK
    assert confidence == RecommendationMatchConfidence.MEDIUM
    assert warning is not None


def test_match_ambiguous_when_multiple_same_day_candidates_and_fallback_enabled():
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    candidates = [
        _recommendation(
            "2914", dt.datetime(2026, 6, 15, 9, 0, tzinfo=dt.UTC), recommendation_id="a"
        ),
        _recommendation(
            "2914", dt.datetime(2026, 6, 15, 10, 0, tzinfo=dt.UTC), recommendation_id="b"
        ),
    ]
    rec, method, confidence, warning = _match_legacy_recommendation(
        evaluated_at, candidates, allow_same_day_fallback=True
    )
    assert rec is None
    assert method == LegacyRecommendationMatchMethod.AMBIGUOUS_MATCH


def test_match_no_match_when_no_candidates_at_all():
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    rec, method, confidence, warning = _match_legacy_recommendation(evaluated_at, [], True)
    assert rec is None
    assert method == LegacyRecommendationMatchMethod.NO_MATCH
    assert warning is None


def test_match_jst_same_day_when_utc_dates_differ():
    """UTC 15:30 2026-06-15とUTC 00:30 2026-06-16(JSTでは同日8/16の09:30)は
    JST基準では同一日になる。"""
    evaluated_at = dt.datetime(2026, 6, 15, 15, 30, tzinfo=dt.UTC)  # JST 2026-06-16 00:30
    # JST同日09:30
    candidate = _recommendation("2914", dt.datetime(2026, 6, 16, 0, 30, tzinfo=dt.UTC))
    rec, method, _, _ = _match_legacy_recommendation(
        evaluated_at, [candidate], allow_same_day_fallback=True
    )
    assert rec is candidate
    assert method == LegacyRecommendationMatchMethod.SAME_DAY_FALLBACK


def test_match_jst_different_day_when_utc_dates_are_same():
    """UTC上は同日でも、JST基準では日付が異なる場合はSAME_DAY_FALLBACKの対象外。"""
    evaluated_at = dt.datetime(2026, 6, 15, 23, 0, tzinfo=dt.UTC)  # JST 2026-06-16 08:00
    # JST前日10:00
    candidate = _recommendation("2914", dt.datetime(2026, 6, 15, 1, 0, tzinfo=dt.UTC))
    rec, method, _, _ = _match_legacy_recommendation(
        evaluated_at, [candidate], allow_same_day_fallback=True
    )
    assert rec is None
    assert method == LegacyRecommendationMatchMethod.NO_MATCH


def test_match_naive_datetime_is_treated_as_utc():
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0)  # naive
    candidate = _recommendation("2914", dt.datetime(2026, 6, 15, 8, 2, tzinfo=dt.UTC))
    rec, method, _, _ = _match_legacy_recommendation(evaluated_at, [candidate], False)
    assert rec is candidate
    assert method == LegacyRecommendationMatchMethod.NEAREST_TIMESTAMP


# ===== _as_aware_utc / business_date / _jst_date_range_to_utc =====


def test_as_aware_utc_converts_non_utc_aware_datetime_to_utc():
    from jstock_advisor.domain.jst import JST

    jst_value = dt.datetime(2026, 6, 16, 9, 30, tzinfo=JST)
    result = _as_aware_utc(jst_value)
    assert result.tzinfo == dt.UTC
    assert result == dt.datetime(2026, 6, 16, 0, 30, tzinfo=dt.UTC)


def test_business_date_at_jst_midnight_boundary():
    # JST 2026-06-16 00:00:00 == UTC 2026-06-15 15:00:00
    assert business_date(dt.datetime(2026, 6, 15, 15, 0, tzinfo=dt.UTC)) == dt.date(2026, 6, 16)


def test_business_date_just_before_jst_midnight():
    # JST 2026-06-15 23:59:59.999999 == UTC 2026-06-15 14:59:59.999999
    value = dt.datetime(2026, 6, 15, 14, 59, 59, 999999, tzinfo=dt.UTC)
    assert business_date(value) == dt.date(2026, 6, 15)


def test_jst_date_range_to_utc_excludes_next_day():
    start_utc, end_exclusive_utc = _jst_date_range_to_utc(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
    # 6/30のJST 23:59:59.999999は範囲内、7/1のJST 00:00:00は範囲外(排他的上限)。
    just_inside = dt.datetime(2026, 6, 30, 14, 59, 59, 999999, tzinfo=dt.UTC)
    just_outside = dt.datetime(2026, 6, 30, 15, 0, 0, tzinfo=dt.UTC)  # JST 2026-07-01 00:00
    assert start_utc <= just_inside < end_exclusive_utc
    assert not (start_utc <= just_outside < end_exclusive_utc)


# ===== run_history_replay =====


def test_history_replay_returns_empty_when_no_data(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    rows = run_history_replay(
        ["2914"],
        dt.date(2020, 1, 1),
        dt.date(2020, 12, 31),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert rows == []


def test_history_replay_matches_legacy_recommendation_by_nearest_timestamp(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    hd_repo.save(
        _hd_result(
            "2914",
            evaluated_at,
            result_id="r1",
            should_notify=False,
            execution_plan_reason=ExecutionPlanReason.NORMAL_SHADOW,
        )
    )
    rec_repo.save(_recommendation("2914", evaluated_at + dt.timedelta(minutes=1)))

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.legacy_should_notify is True
    assert row.legacy_recommendation_created is True
    assert row.legacy_recommendation_type == "SELL"
    assert row.legacy_match_method == LegacyRecommendationMatchMethod.NEAREST_TIMESTAMP.value


def test_history_replay_excludes_data_outside_range(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    rec_repo.save(_recommendation("2914", dt.datetime(2025, 1, 1, tzinfo=dt.UTC)))

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 1, 1),
        dt.date(2026, 12, 31),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert rows == []


def test_history_replay_filters_by_stock_code(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    rec_repo.save(_recommendation("2914", dt.datetime(2026, 6, 15, tzinfo=dt.UTC)))
    rec_repo.save(_recommendation("9861", dt.datetime(2026, 6, 15, tzinfo=dt.UTC)))

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert [r.stock_code for r in rows] == ["2914"]


def test_history_replay_no_stock_filter_includes_all(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    rec_repo.save(_recommendation("2914", dt.datetime(2026, 6, 15, tzinfo=dt.UTC)))
    rec_repo.save(_recommendation("9861", dt.datetime(2026, 6, 16, tzinfo=dt.UTC)))

    rows = run_history_replay(
        [],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert {r.stock_code for r in rows} == {"2914", "9861"}


def test_history_replay_excludes_non_legacy_sell_recommendation_types(store_dir: Path):
    """集中リスク・利確・新方式自身のRecommendationは旧方式として対応付けない。"""
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    hd_repo.save(
        _hd_result(
            "2914",
            evaluated_at,
            result_id="r1",
            should_notify=False,
            execution_plan_reason=ExecutionPlanReason.NORMAL_ACTIVE,
        )
    )
    rec_repo.save(
        _recommendation(
            "2914",
            evaluated_at,
            recommendation_type=RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW,
        )
    )

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert len(rows) == 1
    assert rows[0].legacy_recommendation_type is None
    assert rows[0].legacy_match_method != LegacyRecommendationMatchMethod.NEAREST_TIMESTAMP.value


def test_history_replay_excludes_profit_taking_recommendation_type(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    hd_repo.save(
        _hd_result(
            "2914",
            evaluated_at,
            result_id="r1",
            should_notify=False,
            execution_plan_reason=ExecutionPlanReason.NORMAL_ACTIVE,
        )
    )
    rec_repo.save(
        _recommendation(
            "2914", evaluated_at, recommendation_type=RecommendationType.PARTIAL_PROFIT_TAKE
        )
    )

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert rows[0].legacy_recommendation_type is None


def test_history_replay_no_match_normal_active_means_legacy_not_run(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    hd_repo.save(
        _hd_result(
            "2914",
            evaluated_at,
            result_id="r1",
            should_notify=False,
            execution_plan_reason=ExecutionPlanReason.NORMAL_ACTIVE,
        )
    )

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert rows[0].legacy_should_notify is None
    assert rows[0].legacy_recommendation_created is False
    assert rows[0].legacy_match_method == LegacyRecommendationMatchMethod.NO_MATCH.value


def test_history_replay_no_match_non_active_mode_is_unknown_not_hold(store_dir: Path):
    """旧方式が実行予定だった(NORMAL_SHADOW等)が候補が見つからない場合、
    過去データから実行完了を証明できないためHOLDと断定せずUNKNOWN_NO_MATCHとする。"""
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    hd_repo.save(
        _hd_result(
            "2914",
            evaluated_at,
            result_id="r1",
            should_notify=False,
            execution_plan_reason=ExecutionPlanReason.NORMAL_SHADOW,
        )
    )

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert rows[0].legacy_recommendation_type == "UNKNOWN_NO_MATCH"
    assert rows[0].legacy_should_notify is None
    assert rows[0].legacy_recommendation_created is None
    assert rows[0].legacy_match_method == LegacyRecommendationMatchMethod.UNKNOWN_NO_MATCH.value


def test_history_replay_ambiguous_match_does_not_assert_created_or_notify(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    hd_repo.save(_hd_result("2914", evaluated_at, result_id="r1", should_notify=False))
    rec_repo.save(
        _recommendation(
            "2914", evaluated_at + dt.timedelta(minutes=1), recommendation_id="dup-a"
        )
    )
    rec_repo.save(
        _recommendation(
            "2914", evaluated_at + dt.timedelta(minutes=2), recommendation_id="dup-b"
        )
    )

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert rows[0].legacy_match_method == LegacyRecommendationMatchMethod.AMBIGUOUS_MATCH.value
    assert rows[0].legacy_should_notify is None
    assert rows[0].legacy_recommendation_created is None
    assert rows[0].legacy_match_warning is not None


def test_history_replay_does_not_double_assign_same_recommendation(store_dir: Path):
    """同じ旧方式Recommendationを2つの評価サイクルへ割り当てない。"""
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    ts1 = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    ts2 = dt.datetime(2026, 6, 15, 8, 10, tzinfo=dt.UTC)
    hd_repo.save(_hd_result("2914", ts1, result_id="r1", should_notify=False))
    hd_repo.save(_hd_result("2914", ts2, result_id="r2", should_notify=False))
    rec_repo.save(_recommendation("2914", ts1 + dt.timedelta(minutes=1)))

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    matched = [r for r in rows if r.legacy_recommendation_created is True]
    assert len(matched) == 1


# ===== 新方式recommendation_idの実在確認 =====


def test_history_replay_new_recommendation_id_verified(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    notif_repo = NotificationLogRepository(store_dir)
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    rec_repo.save(
        _recommendation(
            "2914",
            evaluated_at,
            recommendation_type=RecommendationType.SELL_CONSIDERATION,
            recommendation_id="new-rec-1",
        )
    )
    hd_repo.save(
        _hd_result(
            "2914", evaluated_at, result_id="r1", should_notify=True, recommendation_id="new-rec-1"
        )
    )

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
        notification_log_repo=notif_repo,
    )
    assert rows[0].new_recommendation_created is True
    assert rows[0].new_match_method == NewRecommendationMatchMethod.RECOMMENDATION_ID.value


def test_history_replay_new_recommendation_id_missing_record(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    hd_repo.save(
        _hd_result(
            "2914",
            evaluated_at,
            result_id="r1",
            should_notify=True,
            recommendation_id="missing-rec",
        )
    )

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert rows[0].new_recommendation_created is None
    assert rows[0].new_match_method == NewRecommendationMatchMethod.RECOMMENDATION_ID_MISSING.value
    assert rows[0].new_match_warning is not None


def test_history_replay_new_recommendation_id_different_stock_code(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    rec_repo.save(
        _recommendation(
            "9861",
            evaluated_at,
            recommendation_type=RecommendationType.SELL_CONSIDERATION,
            recommendation_id="cross-stock-rec",
        )
    )
    hd_repo.save(
        _hd_result(
            "2914",
            evaluated_at,
            result_id="r1",
            should_notify=True,
            recommendation_id="cross-stock-rec",
        )
    )

    rows = run_history_replay(
        ["2914", "9861"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    target = next(r for r in rows if r.stock_code == "2914")
    assert target.new_recommendation_created is None
    assert target.new_match_method == NewRecommendationMatchMethod.RECOMMENDATION_ID_MISSING.value


def test_history_replay_new_recommendation_id_type_mismatch_legacy_type(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    rec_repo.save(
        _recommendation(
            "2914",
            evaluated_at,
            recommendation_type=RecommendationType.SELL,  # 旧方式のType
            recommendation_id="wrong-type-rec",
        )
    )
    hd_repo.save(
        _hd_result(
            "2914",
            evaluated_at,
            result_id="r1",
            should_notify=True,
            recommendation_id="wrong-type-rec",
        )
    )

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert rows[0].new_recommendation_created is None
    expected_method = NewRecommendationMatchMethod.RECOMMENDATION_ID_TYPE_MISMATCH.value
    assert rows[0].new_match_method == expected_method


def test_history_replay_new_recommendation_id_type_mismatch_excluded_type(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    rec_repo.save(
        _recommendation(
            "2914",
            evaluated_at,
            recommendation_type=RecommendationType.PORTFOLIO_CONCENTRATION_REVIEW,
            recommendation_id="excluded-type-rec",
        )
    )
    hd_repo.save(
        _hd_result(
            "2914",
            evaluated_at,
            result_id="r1",
            should_notify=True,
            recommendation_id="excluded-type-rec",
        )
    )

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    expected_method = NewRecommendationMatchMethod.RECOMMENDATION_ID_TYPE_MISMATCH.value
    assert rows[0].new_match_method == expected_method


def test_history_replay_new_recommendation_no_recommendation_when_id_is_none(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    hd_repo.save(_hd_result("2914", evaluated_at, result_id="r1", should_notify=False))

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert rows[0].new_recommendation_created is False
    assert rows[0].new_match_method == NewRecommendationMatchMethod.NO_RECOMMENDATION.value
    assert rows[0].new_notification_sent is False


# ===== 通知実績(NotificationLog) =====


def test_history_replay_notification_sent_when_log_exists(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    notif_repo = NotificationLogRepository(store_dir)
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    rec_repo.save(
        _recommendation(
            "2914",
            evaluated_at,
            recommendation_type=RecommendationType.SELL_CONSIDERATION,
            recommendation_id="sent-rec",
        )
    )
    hd_repo.save(
        _hd_result(
            "2914", evaluated_at, result_id="r1", should_notify=True, recommendation_id="sent-rec"
        )
    )
    notif_repo.save(_sent_log("sent-rec", evaluated_at))

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
        notification_log_repo=notif_repo,
    )
    assert rows[0].new_notification_status == BacktestNotificationStatus.SENT.value
    assert rows[0].new_notification_sent is True


def test_history_replay_notification_unknown_when_no_log(store_dir: Path):
    """Recommendationは作成されたが送信ログが無い場合、Falseと断定せずUNKNOWNとする
    (抑止か記録漏れか過去データから確認できないため)。"""
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    rec_repo.save(
        _recommendation(
            "2914",
            evaluated_at,
            recommendation_type=RecommendationType.SELL_CONSIDERATION,
            recommendation_id="unsent-rec",
        )
    )
    hd_repo.save(
        _hd_result(
            "2914", evaluated_at, result_id="r1", should_notify=True, recommendation_id="unsent-rec"
        )
    )

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert rows[0].new_notification_status == BacktestNotificationStatus.UNKNOWN.value
    assert rows[0].new_notification_sent is None


def test_history_replay_notification_duplicate_logs_produce_warning(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    notif_repo = NotificationLogRepository(store_dir)
    evaluated_at = dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)
    rec_repo.save(
        _recommendation(
            "2914",
            evaluated_at,
            recommendation_type=RecommendationType.SELL_CONSIDERATION,
            recommendation_id="dup-log-rec",
        )
    )
    hd_repo.save(
        _hd_result(
            "2914",
            evaluated_at,
            result_id="r1",
            should_notify=True,
            recommendation_id="dup-log-rec",
        )
    )
    notif_repo.save(_sent_log("dup-log-rec", evaluated_at))
    notif_repo.save(_sent_log("dup-log-rec", evaluated_at + dt.timedelta(minutes=1)))

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
        notification_log_repo=notif_repo,
    )
    assert rows[0].new_notification_sent is True
    assert rows[0].new_notification_warning is not None


# ===== 未消費Recommendationのstandalone行 =====


def test_history_replay_standalone_legacy_only_row(store_dir: Path):
    hd_repo = HoldingDecisionResultRepository(store_dir)
    rec_repo = RecommendationRepository(store_dir)
    rec_repo.save(_recommendation("2914", dt.datetime(2026, 6, 15, 8, 0, tzinfo=dt.UTC)))

    rows = run_history_replay(
        ["2914"],
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        holding_decision_result_repo=hd_repo,
        recommendation_repo=rec_repo,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.legacy_should_notify is True
    assert row.legacy_recommendation_created is True
    assert row.new_score is None
    assert row.new_category is None
    assert row.new_recommendation_created is False
    assert row.new_match_method == NewRecommendationMatchMethod.NO_RECOMMENDATION.value


# ===== CSV出力 =====


def test_write_backtest_csv_round_trips_live_rows(tmp_path: Path):
    rows = run_live_comparison(["2914"], _PROVIDERS, _CFG, _NOW)
    csv_path = tmp_path / "backtest.csv"
    write_backtest_csv(rows, csv_path)

    content = csv_path.read_text(encoding="utf-8-sig")
    lines = content.strip().splitlines()
    assert lines[0].startswith("date,stock_code,source,legacy_recommendation_type,")
    assert "match_confidence" in lines[0]
    assert len(lines) == 2
    assert "2914" in lines[1]


def test_write_backtest_csv_handles_empty_rows(tmp_path: Path):
    csv_path = tmp_path / "empty.csv"
    write_backtest_csv([], csv_path)
    content = csv_path.read_text(encoding="utf-8-sig")
    lines = content.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("date,stock_code,source")
