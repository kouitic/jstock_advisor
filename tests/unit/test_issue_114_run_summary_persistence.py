"""Issue #114 Phase B1: evaluation_handler境界の回帰テスト。

本モジュールが固定する契約は1つ。

**監査書き込みの失敗が評価run自体を失敗させない。**
評価本体(EvaluationResultの保存)は既に成功しているため、監査書き込みの
失敗でLambdaをFAILさせると、Lambdaのasync retryで評価処理全体
(外部provider呼び出し・DynamoDB read/write)が不要に再実行される。
handlerは例外を外へ出さず、戻り値の`audit_persisted=false`とERRORログで表現する。

IAM(最小権限)の検証はtest_infra_iam_evaluation_run_summary.pyが担当する。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.lambda_handlers import evaluation_handler
from jstock_advisor.services import evaluation_run_audit
from jstock_advisor.services.evaluation_run_audit import PERSIST_FAILED_EVENT
from jstock_advisor.services.recommendation_evaluation_service import (
    EvaluationRunOutcome,
    EvaluationRunSummary,
)

_SUMMARY = EvaluationRunSummary(
    due_count=100,
    already_evaluated_count=40,
    pending_count=60,
    pending_recommendation_count=55,
    evaluated_count=50,
    backlog_remaining=10,
    budget_exhausted=True,
    recommendations_scanned=55,
    provider_call_count=3,
    duration_ms=1234,
)


class _StubService:
    def __init__(self, **_kwargs: Any) -> None:
        self.saved_calls = 0

    def run_due_evaluations_single_pass(self, *_args: Any, **_kwargs: Any) -> EvaluationRunOutcome:
        self.saved_calls += 1
        return EvaluationRunOutcome(summary=_SUMMARY)


@pytest.fixture
def stubbed_handler(monkeypatch: pytest.MonkeyPatch) -> list[_StubService]:
    """外部I/O(provider bundle)とサービス本体を差し替える。"""
    created: list[_StubService] = []

    def _build_service(**kwargs: Any) -> _StubService:
        service = _StubService(**kwargs)
        created.append(service)
        return service

    monkeypatch.setattr(
        evaluation_handler,
        "build_real_provider_bundle",
        lambda *_a, **_k: SimpleNamespace(market_data=object()),
    )
    monkeypatch.setattr(evaluation_handler, "RecommendationEvaluationService", _build_service)
    return created


# --- handler境界の契約 ------------------------------------------------------


def test_handler_reports_audit_persisted_true(
    monkeypatch: pytest.MonkeyPatch, stubbed_handler: list[_StubService]
) -> None:
    monkeypatch.setattr(evaluation_handler, "record_run_summary", lambda *_a, **_k: True)

    result = evaluation_handler.handler({}, None)

    assert result["audit_persisted"] is True
    # #113で追加した既存の可観測性フィールドは維持されている。
    assert result["backlog_remaining"] == 10
    assert result["budget_exhausted"] is True


def test_handler_passes_run_timestamps_to_audit(
    monkeypatch: pytest.MonkeyPatch, stubbed_handler: list[_StubService]
) -> None:
    captured: dict[str, Any] = {}

    def _record(summary: Any, **kwargs: Any) -> bool:
        captured["summary"] = summary
        captured.update(kwargs)
        return True

    monkeypatch.setattr(evaluation_handler, "record_run_summary", _record)

    evaluation_handler.handler({}, None)

    assert captured["summary"] is _SUMMARY
    started = captured["run_started_at"]
    completed = captured["run_completed_at"]
    assert started.tzinfo is not None
    assert completed.tzinfo is not None
    assert completed >= started


def test_handler_does_not_fail_when_audit_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    stubbed_handler: list[_StubService],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """**audit failureでhandler failure(=async retry)を起こさない。**"""

    class _ExplodingAuditService:
        def record_if_absent(self, **_kwargs: Any) -> Any:
            raise RuntimeError("dynamodb unavailable")

    monkeypatch.setattr(
        evaluation_run_audit, "AuditService", lambda *_a, **_k: _ExplodingAuditService()
    )

    with caplog.at_level(logging.ERROR, logger=evaluation_run_audit.__name__):
        result = evaluation_handler.handler({}, None)  # 例外が出ないこと自体が契約

    assert result["audit_persisted"] is False
    # 評価本体の結果は影響を受けない。
    assert result["backlog_remaining"] == 10
    assert result["evaluated"] == _SUMMARY.business_evaluated_count
    assert result["calendar_evaluated"] == _SUMMARY.calendar_evaluated_count
    # 無音にしない。
    assert any(PERSIST_FAILED_EVENT in r.getMessage() for r in caplog.records)


def test_handler_runs_evaluation_exactly_once_on_audit_failure(
    monkeypatch: pytest.MonkeyPatch, stubbed_handler: list[_StubService]
) -> None:
    """audit失敗でも評価処理を再実行しない(retryを誘発しないことの裏返し)。"""
    monkeypatch.setattr(evaluation_handler, "record_run_summary", lambda *_a, **_k: False)

    evaluation_handler.handler({}, None)

    assert len(stubbed_handler) == 1
    assert stubbed_handler[0].saved_calls == 1
