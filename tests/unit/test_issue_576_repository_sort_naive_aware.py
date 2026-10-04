"""Issue #576: 5 repositoryのsort/max処理が、naive/aware混在のデータでも
TypeErrorにならず、正しいinstant順で結果を返すことを固定するテスト。

各repositoryとも、片方はnaive(tzinfo無し)・もう片方はaware(UTC以外の
タイムゾーン表現を含む)という最小構成で、より新しい方が正しく選ばれる/
並ぶことを検証する(#66 F-L6-bの本来の反証: 修正前はTypeErrorになっていた)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

from jstock_advisor.domain.entities.audit import AuditLogEntry
from jstock_advisor.domain.entities.buy_candidate_evaluation_record import (
    BuyCandidateEvaluationRecord,
)
from jstock_advisor.domain.entities.enums import (
    ConfidenceLevel,
    ExecutionPlanReason,
    HoldingDecisionCategory,
    HoldingDecisionConfidenceLevel,
    PurchaseCategory,
    RecommendationType,
)
from jstock_advisor.domain.entities.holding_decision import (
    CompanyQualityScore,
    ComponentCoverage,
    HoldingDecisionHardGate,
    HoldingDecisionResult,
    InvestmentThesisScore,
    RiskDeductionScore,
)
from jstock_advisor.domain.entities.holding_evaluation_record import HoldingEvaluationRecord
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.infrastructure.local_repository.audit_log_repository import (
    AuditLogRepository,
)
from jstock_advisor.infrastructure.local_repository.buy_candidate_evaluation_record_repository import (  # noqa: E501
    BuyCandidateEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.holding_decision_result_repository import (
    HoldingDecisionResultRepository,
)
from jstock_advisor.infrastructure.local_repository.holding_evaluation_record_repository import (
    HoldingEvaluationRecordRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from tests.factories import build_recommendation

_JST = dt.timezone(dt.timedelta(hours=9))
_STOCK_CODE = "8306"
# aware(JST表現)。UTC換算で2026-08-01T01:00Z。naiveより新しい瞬間。
_NEWER_AWARE_JST = dt.datetime(2026, 8, 1, 10, 0, tzinfo=_JST)
# naive(tzinfo無し)。UTCとみなされる。上記より古い瞬間。
_OLDER_NAIVE = dt.datetime(2026, 7, 31, 23, 0)  # noqa: DTZ001 - naive混在の検証のため意図的


def test_audit_log_repository_sorts_mixed_naive_and_aware_by_instant(tmp_path: Path) -> None:
    repo = AuditLogRepository(store_dir=tmp_path)
    repo.save(
        AuditLogEntry(
            audit_id="a-older-naive",
            timestamp=_OLDER_NAIVE,
            stock_code=_STOCK_CODE,
            decision_type="buy_signal",
            input_values={},
            calculation_formulas={},
            output_values={},
            data_sources=[],
            rule_version="v1",
        )
    )
    repo.save(
        AuditLogEntry(
            audit_id="a-newer-aware",
            timestamp=_NEWER_AWARE_JST,
            stock_code=_STOCK_CODE,
            decision_type="buy_signal",
            input_values={},
            calculation_formulas={},
            output_values={},
            data_sources=[],
            rule_version="v1",
        )
    )

    by_stock = repo.list_by_stock(_STOCK_CODE)
    by_type = repo.list_by_decision_type("buy_signal")

    assert [e.audit_id for e in by_stock] == ["a-older-naive", "a-newer-aware"]
    assert [e.audit_id for e in by_type] == ["a-older-naive", "a-newer-aware"]


def test_buy_candidate_evaluation_record_repository_sorts_mixed_naive_and_aware(
    tmp_path: Path,
) -> None:
    repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)
    repo.upsert(
        BuyCandidateEvaluationRecord(
            evaluation_id="batch-1:older",
            batch_id="batch-1",
            stock_code=_STOCK_CODE,
            evaluated_at=_OLDER_NAIVE,
            rule_version="v1",
            purchase_category=PurchaseCategory.BUY_CANDIDATE,
        )
    )
    repo.upsert(
        BuyCandidateEvaluationRecord(
            evaluation_id="batch-2:newer",
            batch_id="batch-2",
            stock_code=_STOCK_CODE,
            evaluated_at=_NEWER_AWARE_JST,
            rule_version="v1",
            purchase_category=PurchaseCategory.BUY_CANDIDATE,
        )
    )

    result = repo.list_by_stock(_STOCK_CODE)

    assert [r.evaluation_id for r in result] == ["batch-1:older", "batch-2:newer"]


def _holding_decision_result(
    result_id: str, evaluated_at: dt.datetime, holding_id: str
) -> HoldingDecisionResult:
    return HoldingDecisionResult(
        holding_decision_result_id=result_id,
        holding_id=holding_id,
        stock_code=_STOCK_CODE,
        evaluated_at=evaluated_at,
        company_quality=CompanyQualityScore(score=30.0, coverage_ratio=1.0),
        investment_thesis=InvestmentThesisScore(score=25.0, coverage_ratio=1.0),
        risk_deduction=RiskDeductionScore(score=10.0, coverage_ratio=1.0),
        base_score=45.0,
        hard_gate=HoldingDecisionHardGate(triggered=False),
        final_score=45.0,
        display_value=45,
        category=HoldingDecisionCategory.SELL_CONSIDERATION,
        coverage=ComponentCoverage(
            overall=1.0, company_quality=1.0, investment_thesis=1.0, risk_deduction=1.0
        ),
        confidence=HoldingDecisionConfidenceLevel.HIGH,
        should_notify=True,
        scoring_model_version=1,
        runtime_config_version=1,
        execution_plan_reason=ExecutionPlanReason.NORMAL_ACTIVE,
    )


def test_holding_decision_result_repository_sorts_mixed_naive_and_aware(tmp_path: Path) -> None:
    holding_id = build_holding_id(DEFAULT_OWNER, _STOCK_CODE)
    repo = HoldingDecisionResultRepository(store_dir=tmp_path)
    repo.save(_holding_decision_result("r-older-naive", _OLDER_NAIVE, holding_id))
    repo.save(_holding_decision_result("r-newer-aware", _NEWER_AWARE_JST, holding_id))

    by_holding = repo.list_by_holding(holding_id)
    by_stock = repo.list_by_stock(_STOCK_CODE)
    latest = repo.latest_by_holding(holding_id)
    between = repo.list_between(
        dt.datetime(2026, 7, 31, tzinfo=dt.UTC), dt.datetime(2026, 8, 2, tzinfo=dt.UTC)
    )

    assert [r.holding_decision_result_id for r in by_holding] == [
        "r-older-naive",
        "r-newer-aware",
    ]
    assert [r.holding_decision_result_id for r in by_stock] == [
        "r-older-naive",
        "r-newer-aware",
    ]
    assert latest is not None and latest.holding_decision_result_id == "r-newer-aware"
    assert [r.holding_decision_result_id for r in between] == [
        "r-older-naive",
        "r-newer-aware",
    ]


def test_holding_evaluation_record_repository_max_with_mixed_naive_and_aware(
    tmp_path: Path,
) -> None:
    holding_id = build_holding_id(DEFAULT_OWNER, _STOCK_CODE)
    repo = HoldingEvaluationRecordRepository(store_dir=tmp_path)
    repo.save(
        HoldingEvaluationRecord(
            holding_evaluation_id="h-older-naive",
            holding_id=holding_id,
            owner=DEFAULT_OWNER,
            stock_code=_STOCK_CODE,
            evaluated_at=_OLDER_NAIVE,
            rule_version="v1",
            authoritative_outcome_category="hold",
        )
    )
    repo.save(
        HoldingEvaluationRecord(
            holding_evaluation_id="h-newer-aware",
            holding_id=holding_id,
            owner=DEFAULT_OWNER,
            stock_code=_STOCK_CODE,
            evaluated_at=_NEWER_AWARE_JST,
            rule_version="v1",
            authoritative_outcome_category="hold",
        )
    )

    latest = repo.get_latest_by_holding_id(holding_id)

    assert latest is not None
    assert latest.holding_evaluation_id == "h-newer-aware"


def test_recommendation_repository_sort_and_max_with_mixed_naive_and_aware(
    tmp_path: Path,
) -> None:
    repo = RecommendationRepository(store_dir=tmp_path)
    repo.save(
        build_recommendation(
            recommendation_id="rec-older-naive",
            recommended_at=_OLDER_NAIVE,
            stock_code=_STOCK_CODE,
            stock_name="x",
            recommendation_type=RecommendationType.WATCH,
            price_at_recommendation=Decimal("1000"),
            confidence=ConfidenceLevel.MEDIUM,
            rule_version="v1",
        )
    )
    repo.save(
        build_recommendation(
            recommendation_id="rec-newer-aware",
            recommended_at=_NEWER_AWARE_JST,
            stock_code=_STOCK_CODE,
            stock_name="x",
            recommendation_type=RecommendationType.WATCH,
            price_at_recommendation=Decimal("1000"),
            confidence=ConfidenceLevel.MEDIUM,
            rule_version="v1",
        )
    )

    by_stock = repo.list_by_stock(_STOCK_CODE)
    latest = repo.latest_by_stock(_STOCK_CODE)
    latest_by_type = repo.get_latest_by_type(RecommendationType.WATCH)

    assert [r.recommendation_id for r in by_stock] == ["rec-older-naive", "rec-newer-aware"]
    assert latest is not None and latest.recommendation_id == "rec-newer-aware"
    assert latest_by_type is not None and latest_by_type.recommendation_id == "rec-newer-aware"
