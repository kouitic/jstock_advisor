"""候補ユニバース本格対応(15/19節)のバッチメトリクス集計のテスト。"""

from __future__ import annotations

from jstock_advisor.infrastructure.aws.batch_tracker import CandidateProgressRecord
from jstock_advisor.services.watchlist_batch_finalizer import compute_batch_metrics


def _record(
    stock_code: str,
    *,
    status: str = "COMPLETED",
    evaluation_result: str | None = "PASSED",
    duration_ms: int = 1000,
    attempt_count: int = 1,
    is_rate_limit_suspected: bool = False,
) -> CandidateProgressRecord:
    return CandidateProgressRecord(
        batch_id="batch-1",
        stock_code=stock_code,
        status=status,
        dispatched=True,
        evaluation_result=evaluation_result,
        ranking_entry=None,
        lease_owner_id=None,
        attempt_count=attempt_count,
        total_processing_duration_ms=duration_ms,
        is_rate_limit_suspected=is_rate_limit_suspected,
    )


def test_compute_batch_metrics_ignores_non_terminal_rows() -> None:
    records = [_record("1111", status="PENDING"), _record("2222", status="PROCESSING")]
    metrics = compute_batch_metrics(records)
    assert metrics["processed_count"] == 0
    assert metrics["p50_processing_duration_ms"] is None


def test_compute_batch_metrics_counts_rate_limit_suspected_rate() -> None:
    records = [
        _record("1111", is_rate_limit_suspected=True),
        _record("2222", is_rate_limit_suspected=True),
        _record("3333", is_rate_limit_suspected=False),
        _record("4444", is_rate_limit_suspected=False),
    ]
    metrics = compute_batch_metrics(records)
    assert metrics["processed_count"] == 4
    assert metrics["rate_limit_suspected_count"] == 2
    assert metrics["rate_limit_suspected_rate_pct"] == 50.0


def test_compute_batch_metrics_counts_data_error_and_redelivery() -> None:
    records = [
        _record("1111", evaluation_result="DATA_INSUFFICIENT"),
        _record("2222", attempt_count=3),
        _record("3333"),
    ]
    metrics = compute_batch_metrics(records)
    assert metrics["data_error_count"] == 1
    assert metrics["sqs_redelivery_count"] == 1


def test_compute_batch_metrics_counts_terminal_failure_reasons() -> None:
    records = [
        _record("1111", status="FAILED", evaluation_result="DISPATCH_SEND_FAILED"),
        _record("2222", status="FAILED", evaluation_result="SQS_MAX_RECEIVE_EXCEEDED"),
        _record("3333", status="FAILED", evaluation_result="BATCH_TIMED_OUT"),
        _record("4444", status="FAILED", evaluation_result="UNEXPECTED_ERROR"),
        _record("5555"),
    ]
    metrics = compute_batch_metrics(records)
    assert metrics["terminal_failure_count"] == 3
    assert metrics["processed_count"] == 5


def test_compute_batch_metrics_p50_p95_use_total_processing_duration_ms() -> None:
    durations = [100, 200, 300, 400, 500]
    records = [_record(f"{i:04d}", duration_ms=d) for i, d in enumerate(durations)]
    metrics = compute_batch_metrics(records)
    assert metrics["p50_processing_duration_ms"] == 300
    assert metrics["p95_processing_duration_ms"] == 500
    assert metrics["estimated_lambda_total_duration_ms"] == sum(durations)
    assert metrics["estimated_yahoo_finance_requests"] == 5 * 11
