"""候補ユニバース本格対応(15/19節)・運用ハードニング第2弾3/4節の
バッチメトリクス集計のテスト。"""

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
    is_provider_failure_suspected: bool = False,
    missing_field_names: list[str] | None = None,
    total_score: float | None = None,
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
        is_provider_failure_suspected=is_provider_failure_suspected,
        missing_field_names=missing_field_names or [],
        total_score=total_score,
        notification_detail=None,
    )


def test_compute_batch_metrics_ignores_non_terminal_rows() -> None:
    records = [_record("1111", status="PENDING"), _record("2222", status="PROCESSING")]
    metrics = compute_batch_metrics(records)
    assert metrics["processed_count"] == 0
    assert metrics["terminal_count"] == 0
    assert metrics["p50_processing_duration_ms"] is None


def test_compute_batch_metrics_counts_provider_failure_rate() -> None:
    records = [
        _record("1111", is_provider_failure_suspected=True),
        _record("2222", is_provider_failure_suspected=True),
        _record("3333", is_provider_failure_suspected=False),
        _record("4444", is_provider_failure_suspected=False),
    ]
    metrics = compute_batch_metrics(records)
    assert metrics["processed_count"] == 4
    assert metrics["evaluation_attempted_count"] == 4
    assert metrics["provider_failure_count"] == 2
    assert metrics["provider_failure_rate_pct"] == 50.0


def test_compute_batch_metrics_computes_field_coverage_rate() -> None:
    records = [
        _record("1111", missing_field_names=["dividend_yield_pct"]),
        _record("2222", missing_field_names=["dividend_yield_pct"]),
        _record("3333"),
        _record("4444"),
    ]
    metrics = compute_batch_metrics(records)
    assert metrics["field_coverage_rate"]["dividend_yield_pct"] == 0.5
    assert metrics["worst_scoring_field_missing_rate_pct"] == 50.0


def test_compute_batch_metrics_field_coverage_rate_always_includes_all_known_fields() -> None:
    """運用ハードニング第2弾4節: 一度も欠損しなかったフィールドも含め、
    既知の全フィールド(必須2件+スコア5件)が必ず出力されること。"""
    records = [_record("1111")]  # 欠損なし
    metrics = compute_batch_metrics(records)
    expected_fields = {
        "shares_outstanding",
        "operating_cashflow",
        "dividend_yield_pct",
        "equity_ratio_pct",
        "payout_ratio_pct",
        "consecutive_dividend_increase_years",
        "shareholder_benefit_yield_pct",
    }
    assert set(metrics["field_coverage_rate"]) == expected_fields
    assert all(rate == 1.0 for rate in metrics["field_coverage_rate"].values())
    assert metrics["worst_required_field_missing_rate_pct"] == 0.0
    assert metrics["worst_scoring_field_missing_rate_pct"] == 0.0


def test_compute_batch_metrics_separates_required_and_scoring_field_missing_rates() -> None:
    records = [
        _record("1111", missing_field_names=["shares_outstanding"]),
        _record("2222", missing_field_names=["dividend_yield_pct", "equity_ratio_pct"]),
        _record("3333"),
        _record("4444"),
    ]
    metrics = compute_batch_metrics(records)
    # shares_outstanding: 1/4=25%(必須項目)。
    # dividend_yield_pct/equity_ratio_pct: 各1/4=25%(スコア項目)。
    assert metrics["worst_required_field_missing_rate_pct"] == 25.0
    assert metrics["worst_scoring_field_missing_rate_pct"] == 25.0


def test_compute_batch_metrics_counts_data_error_and_not_found_separately() -> None:
    records = [
        _record("1111", evaluation_result="DATA_ERROR"),
        _record("2222", evaluation_result="NOT_FOUND"),
        _record("3333", attempt_count=3),
        _record("4444"),
    ]
    metrics = compute_batch_metrics(records)
    assert metrics["data_error_count"] == 1
    assert metrics["not_found_count"] == 1
    assert metrics["sqs_redelivery_count"] == 1


def test_compute_batch_metrics_screening_input_created_excludes_data_error_and_not_found() -> None:
    """運用ハードニング第2弾3節: DATA_ERROR/NOT_FOUND行はScreeningDataResult.input
    が作られていないため、screening_input_created_count(field_coverage_rateの母数)
    から除外されること。"""
    records = [
        _record("1111", evaluation_result="DATA_ERROR"),
        _record("2222", evaluation_result="NOT_FOUND"),
        _record("3333"),
        _record("4444"),
    ]
    metrics = compute_batch_metrics(records)
    assert metrics["evaluation_attempted_count"] == 4
    assert metrics["screening_input_created_count"] == 2
    assert metrics["screening_completed_count"] == 2


def test_compute_batch_metrics_field_missing_rate_unaffected_by_data_error_volume() -> None:
    """DATA_ERROR行を大量に混ぜても、母数がscreening_input_created_countの
    ままであるため欠損率の数値が変わらないこと(母数分離の直接的な回帰確認)。"""
    records = [_record(f"err{i:03d}", evaluation_result="DATA_ERROR") for i in range(96)]
    records += [
        _record("1111", missing_field_names=["dividend_yield_pct"]),
        _record("2222", missing_field_names=["dividend_yield_pct"]),
        _record("3333"),
        _record("4444"),
    ]
    metrics = compute_batch_metrics(records)
    assert metrics["field_coverage_rate"]["dividend_yield_pct"] == 0.5
    assert metrics["worst_scoring_field_missing_rate_pct"] == 50.0


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
    assert metrics["total_candidate_count"] == 5
    # UNEXPECTED_ERROR(1件)+PASSED(1件)のみがevaluation_attempted_countに含まれる
    # (terminal_failureの3件は除外)。
    assert metrics["evaluation_attempted_count"] == 2


def test_compute_batch_metrics_p50_p95_use_total_processing_duration_ms() -> None:
    durations = [100, 200, 300, 400, 500]
    records = [_record(f"{i:04d}", duration_ms=d) for i, d in enumerate(durations)]
    metrics = compute_batch_metrics(records)
    assert metrics["p50_processing_duration_ms"] == 300
    assert metrics["p95_processing_duration_ms"] == 500
    assert metrics["estimated_lambda_total_duration_ms"] == sum(durations)
    assert metrics["estimated_yahoo_finance_requests"] == 5 * 11


# --- data_unavailable_countの4分類合算(LINE通知品質改善、修正⑦) -----------------


def _data_unavailable_count(metrics: dict[str, object]) -> int:
    return (
        metrics["not_found_count"]
        + metrics["data_error_count"]
        + metrics["unexpected_error_count"]
        + metrics["terminal_failure_count"]
    )


def test_compute_batch_metrics_returns_unexpected_error_count() -> None:
    """第7版まではunexpected_error_countが返り値dictに含まれておらず、
    data_unavailable_countの算出式から漏れるバグがあった(修正⑦の回帰テスト)。"""
    records = [
        _record("1111", evaluation_result="UNEXPECTED_ERROR"),
        _record("2222"),
    ]
    metrics = compute_batch_metrics(records)
    assert metrics["unexpected_error_count"] == 1


def test_data_unavailable_count_invariant_holds_with_real_batch_breakdown() -> None:
    """今回の実績値(対象98・合格2・不合格88[FAILED_SCORE50+FAILED_REQUIRED38]・
    データ未検出8)で、total_target_count == ranked_count + data_unavailable_count
    が成立すること。"""
    records = (
        [_record(f"pass{i:03d}", total_score=70.0) for i in range(2)]
        + [
            _record(f"score{i:03d}", evaluation_result="FAILED_SCORE", total_score=40.0)
            for i in range(50)
        ]
        + [
            _record(f"req{i:03d}", evaluation_result="FAILED_REQUIRED", total_score=20.0)
            for i in range(38)
        ]
        + [_record(f"nf{i:03d}", evaluation_result="NOT_FOUND") for i in range(8)]
    )
    metrics = compute_batch_metrics(records)

    ranked_count = metrics["screening_completed_count"]
    data_unavailable_count = _data_unavailable_count(metrics)

    assert metrics["total_candidate_count"] == 98
    assert ranked_count == 90
    assert data_unavailable_count == 8
    assert metrics["total_candidate_count"] == ranked_count + data_unavailable_count


def test_data_unavailable_count_invariant_holds_with_unexpected_error_included() -> None:
    """UNEXPECTED_ERRORを含む場合でも不変条件が成立すること(修正⑦の主目的)。"""
    records = (
        [_record(f"pass{i:03d}", total_score=70.0) for i in range(2)]
        + [
            _record(f"score{i:03d}", evaluation_result="FAILED_SCORE", total_score=40.0)
            for i in range(49)
        ]
        + [
            _record(f"req{i:03d}", evaluation_result="FAILED_REQUIRED", total_score=20.0)
            for i in range(38)
        ]
        + [_record(f"nf{i:03d}", evaluation_result="NOT_FOUND") for i in range(8)]
        + [_record("unexpected001", evaluation_result="UNEXPECTED_ERROR")]
    )
    metrics = compute_batch_metrics(records)

    ranked_count = metrics["screening_completed_count"]
    data_unavailable_count = _data_unavailable_count(metrics)

    assert metrics["total_candidate_count"] == 98
    assert ranked_count == 89
    assert data_unavailable_count == 9
    assert metrics["total_candidate_count"] == ranked_count + data_unavailable_count


def test_data_unavailable_count_invariant_holds_with_terminal_failure_included() -> None:
    records = [
        _record("1111", total_score=70.0),
        _record("2222", status="FAILED", evaluation_result="DISPATCH_SEND_FAILED"),
    ]
    metrics = compute_batch_metrics(records)

    ranked_count = metrics["screening_completed_count"]
    data_unavailable_count = _data_unavailable_count(metrics)

    assert metrics["total_candidate_count"] == ranked_count + data_unavailable_count
    assert data_unavailable_count == 1
