import datetime as dt
from typing import Any

import pytest

from jstock_advisor.services import watchlist_screening_audit as audit_module
from jstock_advisor.services.watchlist_screening_audit import (
    REPOSITORY_RESULT_ADDED,
    REPOSITORY_RESULT_FAILED,
    REPOSITORY_RESULT_SKIPPED_EXISTING,
    REPOSITORY_RESULT_SKIPPED_OVER_LIMIT,
    record_batch_audit,
    record_candidate_audit,
    record_repository_result_audit,
)
from jstock_advisor.services.watchlist_screening_service import WatchlistScreeningResult

_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)


class _FakeAuditService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def fake_audit(monkeypatch: pytest.MonkeyPatch) -> _FakeAuditService:
    fake = _FakeAuditService()
    monkeypatch.setattr(audit_module, "AuditService", lambda: fake)
    return fake


def test_record_candidate_audit_includes_batch_id_in_input_values(
    fake_audit: _FakeAuditService,
) -> None:
    record_candidate_audit("1234", None, "DATA_INSUFFICIENT", _NOW, batch_id="batch-1")

    assert len(fake_audit.calls) == 1
    call = fake_audit.calls[0]
    assert call["decision_type"] == "watchlist_auto_addition_candidate_evaluation"
    assert call["input_values"] == {"batch_id": "batch-1", "stock_code": "1234"}
    assert call["output_values"]["evaluation_result"] == "DATA_INSUFFICIENT"


def _fake_result() -> WatchlistScreeningResult:
    return WatchlistScreeningResult(
        stock_code="1234",
        stock_name="テスト",
        passed=True,
        policy_results=[],
        total_score=87.0,
        matched_criteria=[],
        exclusion_reasons=[],
        missing_required_fields=[],
        missing_scoring_fields=[],
        evaluated_at=_NOW,
        main_metrics={},
        classification_basis=[],
    )


def test_record_repository_result_audit_added(fake_audit: _FakeAuditService) -> None:
    record_repository_result_audit(
        "batch-1",
        "1234",
        "テスト株式会社",
        1,
        87.0,
        REPOSITORY_RESULT_ADDED,
        True,
        "AUTO_SCREENING",
        "high_dividend_financial_health",
        _NOW,
    )

    call = fake_audit.calls[0]
    assert call["decision_type"] == "watchlist_auto_addition_repository_result"
    assert call["input_values"] == {"batch_id": "batch-1", "stock_code": "1234"}
    assert call["output_values"]["repository_result"] == REPOSITORY_RESULT_ADDED
    assert call["output_values"]["added_to_watchlist"] is True
    assert call["output_values"]["rank"] == 1
    assert call["output_values"]["registration_source"] == "AUTO_SCREENING"
    assert "error_summary" not in call["output_values"]


def test_record_repository_result_audit_skipped_existing(fake_audit: _FakeAuditService) -> None:
    record_repository_result_audit(
        "batch-1",
        "1234",
        None,
        2,
        60.0,
        REPOSITORY_RESULT_SKIPPED_EXISTING,
        False,
        "AUTO_SCREENING",
        "high_dividend_financial_health",
        _NOW,
    )

    assert fake_audit.calls[0]["output_values"]["repository_result"] == (
        REPOSITORY_RESULT_SKIPPED_EXISTING
    )
    assert fake_audit.calls[0]["output_values"]["added_to_watchlist"] is False


def test_record_repository_result_audit_skipped_over_limit(fake_audit: _FakeAuditService) -> None:
    record_repository_result_audit(
        "batch-1",
        "5678",
        None,
        25,
        55.0,
        REPOSITORY_RESULT_SKIPPED_OVER_LIMIT,
        False,
        "AUTO_SCREENING",
        "high_dividend_financial_health",
        _NOW,
    )

    assert fake_audit.calls[0]["output_values"]["repository_result"] == (
        REPOSITORY_RESULT_SKIPPED_OVER_LIMIT
    )
    assert fake_audit.calls[0]["output_values"]["rank"] == 25


def test_record_repository_result_audit_failed_includes_safe_error_summary(
    fake_audit: _FakeAuditService,
) -> None:
    error = ValueError("boom: something went wrong")
    record_repository_result_audit(
        "batch-1",
        "1234",
        "テスト",
        1,
        87.0,
        REPOSITORY_RESULT_FAILED,
        False,
        "AUTO_SCREENING",
        "high_dividend_financial_health",
        _NOW,
        error=error,
    )

    output = fake_audit.calls[0]["output_values"]
    assert output["repository_result"] == REPOSITORY_RESULT_FAILED
    assert output["error_summary"] == "ValueError: boom: something went wrong"


def test_record_repository_result_audit_truncates_long_error_summary(
    fake_audit: _FakeAuditService,
) -> None:
    error = ValueError("x" * 10_000)
    record_repository_result_audit(
        "batch-1",
        "1234",
        None,
        1,
        87.0,
        REPOSITORY_RESULT_FAILED,
        False,
        "AUTO_SCREENING",
        "high_dividend_financial_health",
        _NOW,
        error=error,
    )

    output = fake_audit.calls[0]["output_values"]
    assert len(output["error_summary"]) == audit_module._MAX_ERROR_SUMMARY_LENGTH


def test_record_batch_audit_still_includes_batch_id_when_provided(
    fake_audit: _FakeAuditService,
) -> None:
    record_batch_audit(
        execution_mode="scheduled",
        universe_provider="csv",
        screening_policies=["high_dividend_financial_health"],
        output_values={"actual_added_count": 3},
        now=_NOW,
        batch_id="batch-1",
    )

    assert fake_audit.calls[0]["input_values"]["batch_id"] == "batch-1"
