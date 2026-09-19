"""Issue #413 PR-2: audit_service / _finalize_recovery / watchlist_batch_finalizer の INFO 出力。

3 module は module 直下で `logger.setLevel(logging.INFO)` を宣言した。ここでは次を確認する。

    1 宣言が実際に効く: Lambda の root logger の既定(WARNING)のもとでも、INFO が有効になる。
    2 INFO が実際に出力される(各 module の代表的な INFO の経路を 1 件以上)。
    3 出力に、生の owner / holding_id を含めない(有効化の時点の PII 確認。#135 / #416)。

宣言があること自体は tests/unit/test_issue_413_logger_level_declared.py(PR #436 の guard)が見る。
"""

from __future__ import annotations

import datetime as dt
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import ExecutionMode
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.infrastructure.aws.batch_tracker import (
    JOB_TYPE_NEW_CANDIDATE_SCREENING,
    BatchFamily,
    WatchlistBatchStatus,
)
from jstock_advisor.lambda_handlers import _finalize_recovery as recovery
from jstock_advisor.services import audit_service, watchlist_batch_finalizer
from jstock_advisor.services.audit_service import AuditService

_MODULES = [audit_service.__name__, recovery.__name__, watchlist_batch_finalizer.__name__]

#: 実在しない架空値。出力に現れたら、生の owner / holding_id を出している。
_OWNER = "owner-a"
_HOLDING_ID = "owner-a:holding-1"


@pytest.mark.parametrize("module_name", _MODULES)
def test_info_is_enabled_even_when_the_root_logger_is_at_warning(
    module_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lambda の root 既定(WARNING)に左右されず、INFO が有効になる(宣言が効いている)。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)

    assert logging.getLogger().getEffectiveLevel() == logging.WARNING
    assert logging.getLogger(module_name).isEnabledFor(logging.INFO)
    assert not logging.getLogger(module_name).isEnabledFor(logging.DEBUG)


class _NeverSavedRepository:
    def save(self, entry: object) -> None:
        raise AssertionError("VALIDATION mode must not persist the audit log")


def test_audit_service_validation_info_is_emitted_without_raw_owner_or_holding_id(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    service = AuditService(
        repository=_NeverSavedRepository(),  # type: ignore[arg-type]
        execution_context=ExecutionContext(mode=ExecutionMode.VALIDATION),
    )

    entry = service.record(
        decision_type="test_decision",
        stock_code="0000",
        input_values={"owner": _OWNER, "holding_id": _HOLDING_ID},
        calculation_formulas={},
        output_values={},
        data_sources=[],
        rule_version="test",
        timestamp=dt.datetime(2026, 9, 19, tzinfo=dt.UTC),
    )

    messages = [r.getMessage() for r in caplog.records if r.name == audit_service.__name__]
    assert len(messages) == 1
    assert "VALIDATION MODE audit suppressed" in messages[0]
    assert "decision_type=test_decision" in messages[0]
    assert f"audit_id={entry.audit_id}" in messages[0]
    assert _OWNER not in messages[0]
    assert _HOLDING_ID not in messages[0]


def _fake_record(mode: ExecutionMode, *, is_finalized: bool) -> Any:
    return SimpleNamespace(
        family=BatchFamily.BUY_CANDIDATES,
        execution_context=ExecutionContext(mode=mode),
        is_finalized=is_finalized,
    )


@pytest.mark.parametrize(
    ("mode", "is_finalized", "expected"),
    [
        (ExecutionMode.VALIDATION, False, "finalize recovery skipped: non-NORMAL batch"),
        (ExecutionMode.NORMAL, True, "finalize recovery no-op: already finalized"),
    ],
)
def test_finalize_recovery_info_is_emitted_and_names_only_the_batch(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mode: ExecutionMode,
    is_finalized: bool,
    expected: str,
) -> None:
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    batch_id = "buy-candidates-20260919T000000-abcd1234"
    monkeypatch.setattr(
        recovery, "get_completion_batch", lambda _id: _fake_record(mode, is_finalized=is_finalized)
    )
    event = {
        recovery.RECOVERY_ACTION_KEY: recovery.FINALIZE_ONLY_ACTION,
        "batch_id": batch_id,
        "batch_family": BatchFamily.BUY_CANDIDATES.value,
    }

    result = recovery.resolve_finalize_only_request(
        event, BatchFamily.BUY_CANDIDATES, ExecutionContext.normal()
    )

    assert result is None
    messages = [r.getMessage() for r in caplog.records if r.name == recovery.__name__]
    assert len(messages) == 1
    assert expected in messages[0]
    assert f"batch_id={batch_id}" in messages[0]


def test_watchlist_batch_finalizer_maintenance_not_applicable_info_is_emitted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    config = load_config()
    auto_removal = config.watchlist_screening.auto_removal.model_copy(update={"enabled": True})
    config = config.model_copy(
        update={
            "watchlist_screening": config.watchlist_screening.model_copy(
                update={"auto_removal": auto_removal}
            )
        }
    )
    batch_id = "watchlist-screening-20260919T000000-abcd1234"

    outcome = watchlist_batch_finalizer.maybe_trigger_maintenance(
        batch_id,
        {"job_type": JOB_TYPE_NEW_CANDIDATE_SCREENING},
        dt.datetime(2026, 9, 19, tzinfo=dt.UTC),
        config,
        WatchlistBatchStatus.ABORTED,
    )

    assert outcome is watchlist_batch_finalizer.MaintenanceTriggerOutcome.NOT_APPLICABLE
    messages = [
        r.getMessage() for r in caplog.records if r.name == watchlist_batch_finalizer.__name__
    ]
    assert len(messages) == 1
    assert "watchlist maintenance trigger not applicable" in messages[0]
    assert f"batch_id={batch_id}" in messages[0]
