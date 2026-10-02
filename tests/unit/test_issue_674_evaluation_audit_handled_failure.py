"""Issue #674(HF-9): evaluation run監査永続化の失敗をHANDLED_FAILUREとしてUSER通知する。

対象のcatch境界(TARO設計 #674 issuecomment-5862593493。fresh確認済み):

    evaluation_run_audit.py::record_run_summary() の except Exception
    -> 監査書き込みの失敗で評価runを失敗させない(#114。async retryで評価処理全体が
       再実行されるのを防ぐため)。失敗時は audit_persisted = False を返すだけで、
       ERRORログ以外にUSERへ届く手段が無かった。

本Issueが固定する契約:

    1. audit_persisted が False のとき、HF-0契約(#665)の envelope を publish する
       (failure_class = HANDLED_FAILURE。failure_stage = AUDIT_PERSIST。
        failure_count = 1。run単位の条件であり、#673のような累積値ではない)。
       True のときは publish しない。
    2. record_run_summary() の内部実装・「監査失敗でも評価runは成功として継続する」契約
       (#114)は変えない。通知自体の失敗も評価本体・戻り値を妨げない。
    3. #673(aggregate commit失敗 = 評価結果のrollback)とは**別の条件**であり、混同しない:
       両方が同時に起きれば2件のenvelopeが別々のfailure_stageで出る。

★ CODE IMPLEMENTATION APPROVED ≠ PRODUCTION NOTIFICATION ACTIVATION APPROVED。
  Production での通知有効化(infra配線)は別Issue。詳細は test_issue_673 の docstring。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.lambda_handlers import evaluation_handler
from jstock_advisor.services import evaluation_run_audit
from jstock_advisor.services.incident_envelope_publisher import INCIDENT_ENVELOPE_ALLOWLIST
from jstock_advisor.services.recommendation_evaluation_service import (
    EvaluationRunOutcome,
    EvaluationRunSummary,
)


def _summary(aggregate_commit_failed_count: int = 0) -> EvaluationRunSummary:
    return EvaluationRunSummary(
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
        aggregate_commit_failed_count=aggregate_commit_failed_count,
    )


class _StubService:
    aggregate_commit_failed_count = 0
    created: list[_StubService] = []

    def __init__(self, **_kwargs: Any) -> None:
        self.run_calls = 0
        _StubService.created.append(self)

    def run_due_evaluations_single_pass(self, *_args: Any, **_kwargs: Any) -> EvaluationRunOutcome:
        self.run_calls += 1
        return EvaluationRunOutcome(summary=_summary(_StubService.aggregate_commit_failed_count))


@pytest.fixture
def stubbed_handler(monkeypatch: pytest.MonkeyPatch) -> list[_StubService]:
    """外部I/O(provider bundle)とサービス本体を差し替える(監査記録は各テストが決める)。"""
    _StubService.created = []
    _StubService.aggregate_commit_failed_count = 0
    monkeypatch.setattr(
        evaluation_handler,
        "build_real_provider_bundle",
        lambda *_a, **_k: SimpleNamespace(market_data=object()),
    )
    monkeypatch.setattr(evaluation_handler, "RecommendationEvaluationService", _StubService)
    return _StubService.created


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        evaluation_handler, "publish_incident_envelope", lambda envelope: calls.append(envelope)
    )
    return calls


# --- 契約1: audit_persisted が False のときだけ publish する ------------------------------


def test_audit_persist_failure_publishes_handled_failure_envelope(
    monkeypatch: pytest.MonkeyPatch,
    stubbed_handler: list[_StubService],
    published: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(evaluation_handler, "record_run_summary", lambda *_a, **_k: False)

    result = evaluation_handler.handler({}, None)

    assert result["audit_persisted"] is False
    assert len(published) == 1
    envelope = published[0]
    assert envelope["failure_class"] == "HANDLED_FAILURE"
    assert envelope["failure_stage"] == "AUDIT_PERSIST"
    assert envelope["source"] == "evaluation"
    assert envelope["job_name"] == "evaluation"
    assert envelope["failure_count"] == 1  # run単位の条件。累積値ではない
    assert envelope["reason_code"] == "EVALUATION_AUDIT_PERSIST_FAILED"
    assert set(envelope) <= INCIDENT_ENVELOPE_ALLOWLIST


def test_audit_persisted_true_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    stubbed_handler: list[_StubService],
    published: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(evaluation_handler, "record_run_summary", lambda *_a, **_k: True)

    result = evaluation_handler.handler({}, None)

    assert result["audit_persisted"] is True
    assert published == []


def test_real_audit_write_failure_triggers_the_notification(
    monkeypatch: pytest.MonkeyPatch,
    stubbed_handler: list[_StubService],
    published: list[dict[str, Any]],
) -> None:
    """実際の record_run_summary() が失敗する経路(AuditServiceの書き込み例外)でも通知される。"""

    class _ExplodingAuditService:
        def record_if_absent(self, **_kwargs: Any) -> Any:
            raise RuntimeError("dynamodb unavailable")

    monkeypatch.setattr(
        evaluation_run_audit, "AuditService", lambda *_a, **_k: _ExplodingAuditService()
    )

    result = evaluation_handler.handler({}, None)

    assert result["audit_persisted"] is False
    assert [e["failure_stage"] for e in published] == ["AUDIT_PERSIST"]


# --- 契約2: 監査失敗でも評価runは成功として継続する(#114)。通知の失敗も妨げない -----------


def test_audit_failure_still_does_not_fail_or_rerun_the_evaluation(
    monkeypatch: pytest.MonkeyPatch,
    stubbed_handler: list[_StubService],
    published: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(evaluation_handler, "record_run_summary", lambda *_a, **_k: False)

    result = evaluation_handler.handler({}, None)  # 例外が出ないこと自体が契約

    assert result["evaluated"] == _summary().business_evaluated_count
    assert result["backlog_remaining"] == 10
    assert len(stubbed_handler) == 1
    assert stubbed_handler[0].run_calls == 1  # async retryを誘発しない


def test_publish_failure_does_not_break_the_run_and_logs_only_the_error_type(
    monkeypatch: pytest.MonkeyPatch,
    stubbed_handler: list[_StubService],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(evaluation_handler, "record_run_summary", lambda *_a, **_k: False)

    def _raise(_envelope: dict[str, Any]) -> None:
        raise RuntimeError("sns unavailable: secret-detail-456")

    monkeypatch.setattr(evaluation_handler, "publish_incident_envelope", _raise)

    with caplog.at_level(logging.WARNING, logger=evaluation_handler.__name__):
        result = evaluation_handler.handler({}, None)

    assert result["audit_persisted"] is False
    assert len(stubbed_handler) == 1
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("AUDIT_PERSIST" in m and "RuntimeError" in m for m in messages)
    assert not any("secret-detail-456" in m for m in messages)


# --- 契約3: #673(aggregate commit失敗)と別の条件として独立に通知される ------------------


def test_aggregate_commit_failure_and_audit_failure_are_independent_notifications(
    monkeypatch: pytest.MonkeyPatch,
    stubbed_handler: list[_StubService],
    published: list[dict[str, Any]],
) -> None:
    _StubService.aggregate_commit_failed_count = 3
    monkeypatch.setattr(evaluation_handler, "record_run_summary", lambda *_a, **_k: False)

    evaluation_handler.handler({}, None)

    stages = {e["failure_stage"]: e["failure_count"] for e in published}
    assert stages == {"AGGREGATE_COMMIT": 3, "AUDIT_PERSIST": 1}


def test_audit_failure_alone_does_not_emit_the_aggregate_commit_notification(
    monkeypatch: pytest.MonkeyPatch,
    stubbed_handler: list[_StubService],
    published: list[dict[str, Any]],
) -> None:
    """監査の失敗だけで、aggregate commit失敗(評価結果のrollback)の通知は出ない(混同しない)。"""
    _StubService.aggregate_commit_failed_count = 0
    monkeypatch.setattr(evaluation_handler, "record_run_summary", lambda *_a, **_k: False)

    evaluation_handler.handler({}, None)

    assert [e["failure_stage"] for e in published] == ["AUDIT_PERSIST"]
