"""services/evaluation_run_audit.py のテスト(Issue #114 Phase B1)。

run summary を jstock-audit_log へ永続化する責務と、
**監査書き込みの失敗が評価run自体を失敗させない**という契約を固定する。
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.infrastructure.local_repository.audit_log_repository import AuditLogRepository
from jstock_advisor.services import evaluation_run_audit as module
from jstock_advisor.services.audit_service import AuditService
from jstock_advisor.services.evaluation_run_audit import (
    DECISION_TYPE_EVALUATION_RUN_SUMMARY,
    PERSIST_FAILED_EVENT,
    RUN_STATUS_BUDGET_EXHAUSTED,
    RUN_STATUS_COMPLETED,
    build_audit_id,
    record_run_summary,
)
from jstock_advisor.services.recommendation_evaluation_service import EvaluationRunSummary

_STARTED = dt.datetime(2026, 9, 2, 9, 0, tzinfo=dt.UTC)
_COMPLETED = dt.datetime(2026, 9, 2, 9, 3, 20, tzinfo=dt.UTC)


def _summary(**overrides: Any) -> EvaluationRunSummary:
    base: dict[str, Any] = {
        "due_count": 9663,
        "already_evaluated_count": 2666,
        "pending_count": 6997,
        "pending_recommendation_count": 5943,
        "evaluated_count": 4210,
        "skipped_due_to_data_error_count": 7,
        "business_evaluated_count": 3900,
        "calendar_evaluated_count": 310,
        "business_skipped_count": 5,
        "calendar_skipped_count": 2,
        "backlog_remaining": 2787,
        "budget_exhausted": True,
        "recommendations_scanned": 5943,
        "missing_recommendation_count": 1,
        "provider_call_count": 55,
        "duration_ms": 840123,
    }
    base.update(overrides)
    return EvaluationRunSummary(**base)


@pytest.fixture
def audit_service(tmp_path: Path) -> AuditService:
    return AuditService(repository=AuditLogRepository(store_dir=tmp_path))


def _entries(service: AuditService) -> list[Any]:
    repo: AuditLogRepository = service._repository  # noqa: SLF001 - テスト用の直接検証
    return repo.list_by_decision_type(DECISION_TYPE_EVALUATION_RUN_SUMMARY)


# --- 成功系 -----------------------------------------------------------------


def test_records_completed_run_summary(audit_service: AuditService) -> None:
    persisted = record_run_summary(
        _summary(budget_exhausted=False, backlog_remaining=0),
        run_started_at=_STARTED,
        run_completed_at=_COMPLETED,
        audit_service=audit_service,
    )

    assert persisted is True
    entries = _entries(audit_service)
    assert len(entries) == 1
    assert entries[0].input_values["run_status"] == RUN_STATUS_COMPLETED
    assert entries[0].stock_code is None


def test_records_budget_exhausted_run(audit_service: AuditService) -> None:
    persisted = record_run_summary(
        _summary(),  # budget_exhausted=True
        run_started_at=_STARTED,
        run_completed_at=_COMPLETED,
        audit_service=audit_service,
    )

    assert persisted is True
    entry = _entries(audit_service)[0]
    assert entry.input_values["run_status"] == RUN_STATUS_BUDGET_EXHAUSTED
    assert entry.output_values["budget_exhausted"] is True
    assert entry.output_values["backlog_remaining"] == 2787


def test_persists_every_summary_field(audit_service: AuditService) -> None:
    """EvaluationRunSummaryの項目を1つも欠落させない(#113のsummary定義を維持)。"""
    summary = _summary()
    record_run_summary(
        summary,
        run_started_at=_STARTED,
        run_completed_at=_COMPLETED,
        audit_service=audit_service,
    )

    output = _entries(audit_service)[0].output_values
    for field_name in vars(summary):
        assert field_name in output, f"{field_name} が永続化されていない"
        assert output[field_name] == getattr(summary, field_name)


def test_persists_run_metadata(audit_service: AuditService) -> None:
    record_run_summary(
        _summary(),
        run_started_at=_STARTED,
        run_completed_at=_COMPLETED,
        audit_service=audit_service,
    )

    values = _entries(audit_service)[0].input_values
    assert values["run_started_at"] == _STARTED.isoformat()
    assert values["run_completed_at"] == _COMPLETED.isoformat()
    assert values["run_status"] == RUN_STATUS_BUDGET_EXHAUSTED


def test_execution_mode_is_not_invented(audit_service: AuditService) -> None:
    """自然実行とmanual invokeは現行モデルで識別できないため、推測で埋めない。"""
    record_run_summary(
        _summary(),
        run_started_at=_STARTED,
        run_completed_at=_COMPLETED,
        audit_service=audit_service,
    )

    values = _entries(audit_service)[0].input_values
    assert "execution_mode" not in values
    assert "run_source" not in values


# --- 冪等性・複数run --------------------------------------------------------


def test_same_run_started_at_is_recorded_once(audit_service: AuditService) -> None:
    for _ in range(2):
        assert (
            record_run_summary(
                _summary(),
                run_started_at=_STARTED,
                run_completed_at=_COMPLETED,
                audit_service=audit_service,
            )
            is True
        )

    assert len(_entries(audit_service)) == 1


def test_different_run_started_at_creates_separate_records(
    audit_service: AuditService,
) -> None:
    """Lambdaのasync retryはrun_started_atが異なるため別runとして残す。"""
    second_started = _STARTED + dt.timedelta(minutes=2)
    record_run_summary(
        _summary(),
        run_started_at=_STARTED,
        run_completed_at=_COMPLETED,
        audit_service=audit_service,
    )
    record_run_summary(
        _summary(backlog_remaining=0, budget_exhausted=False),
        run_started_at=second_started,
        run_completed_at=second_started + dt.timedelta(minutes=1),
        audit_service=audit_service,
    )

    entries = _entries(audit_service)
    assert len(entries) == 2
    assert build_audit_id(_STARTED) != build_audit_id(second_started)
    assert {e.input_values["run_status"] for e in entries} == {
        RUN_STATUS_BUDGET_EXHAUSTED,
        RUN_STATUS_COMPLETED,
    }


# --- 監査書き込み失敗 -------------------------------------------------------


class _ExplodingAuditService:
    """record_if_absent()が必ず失敗するAuditService代替。"""

    def record_if_absent(self, **_kwargs: Any) -> Any:
        raise RuntimeError("dynamodb unavailable")


def test_audit_failure_does_not_propagate(caplog: pytest.LogCaptureFixture) -> None:
    """**監査書き込みの失敗を呼び出し側へ伝播させない。**

    評価本体は既に成功しているため、ここで例外を投げるとLambdaがFAILし、
    async retryで評価処理全体が不要に再実行される(Issue #114 Phase B1の決定)。
    """
    with caplog.at_level(logging.ERROR, logger=module.__name__):
        persisted = record_run_summary(
            _summary(),
            run_started_at=_STARTED,
            run_completed_at=_COMPLETED,
            audit_service=_ExplodingAuditService(),  # type: ignore[arg-type]
        )

    assert persisted is False

    # 無音のfail-softにしない: 構造化ERRORログが必ず残る。
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert PERSIST_FAILED_EVENT in message
    assert "error_type=RuntimeError" in message
    assert "dynamodb unavailable" in message
    assert _STARTED.isoformat() in message
    assert "backlog_remaining=2787" in message
    assert "budget_exhausted=True" in message


def test_audit_service_construction_failure_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**AuditServiceの生成が失敗しても伝播させない**(PR #119 レビュー指摘R1)。

    boto3クライアントの初期化や設定解決はコンストラクタ側で失敗しうる。
    生成をfailure boundaryの外へ置くと、その経路だけ契約が破れて
    Lambdaがasync retryされてしまう。
    """

    def _exploding_init(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("audit service init failed")

    monkeypatch.setattr(module, "AuditService", _exploding_init)

    with caplog.at_level(logging.ERROR, logger=module.__name__):
        persisted = record_run_summary(
            _summary(),
            run_started_at=_STARTED,
            run_completed_at=_COMPLETED,
            # audit_service を渡さず、モジュール側の生成経路を通す。
        )

    assert persisted is False
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert PERSIST_FAILED_EVENT in message
    assert "error_type=RuntimeError" in message
    assert "audit service init failed" in message


def test_audit_failure_does_not_write_partial_record(tmp_path: Path) -> None:
    """失敗時に中途半端な監査レコードを残さない。"""
    service = AuditService(repository=AuditLogRepository(store_dir=tmp_path))

    def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("boom")

    service.record_if_absent = _boom  # type: ignore[method-assign]

    assert (
        record_run_summary(
            _summary(),
            run_started_at=_STARTED,
            run_completed_at=_COMPLETED,
            audit_service=service,
        )
        is False
    )
    assert AuditLogRepository(store_dir=tmp_path).list_all() == []
