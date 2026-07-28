import datetime as dt
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import ApprovalStatus
from jstock_advisor.infrastructure.local_repository.rule_version_repository import (
    RuleVersionRepository,
)
from jstock_advisor.services.rule_version_service import RuleVersionService

_NOW = dt.datetime(2026, 7, 24, tzinfo=dt.UTC)


@pytest.fixture
def service(tmp_path: Path) -> RuleVersionService:
    return RuleVersionService(repository=RuleVersionRepository(store_dir=tmp_path))


def test_create_draft(service: RuleVersionService) -> None:
    version = service.create_draft("v1", "desc", "reason", now=_NOW)
    assert version.approval_status == ApprovalStatus.DRAFT
    assert version.is_active is False


def test_create_draft_rejects_duplicate(service: RuleVersionService) -> None:
    service.create_draft("v1", "desc", "reason", now=_NOW)
    with pytest.raises(ValueError):
        service.create_draft("v1", "desc2", "reason2", now=_NOW)


def test_full_lifecycle_submit_approve_activate(service: RuleVersionService) -> None:
    service.create_draft("v1", "desc", "reason", now=_NOW)
    service.submit_for_approval("v1")
    service.approve("v1", approved_by="alice")
    activated = service.activate("v1", now=_NOW)

    assert activated.approval_status == ApprovalStatus.ACTIVE
    assert activated.is_active is True
    active = service.get_active_version()
    assert active is not None
    assert active.rule_version == "v1"


def test_activate_deactivates_previous_active(service: RuleVersionService) -> None:
    service.create_draft("v1", "d1", "r1", now=_NOW)
    service.submit_for_approval("v1")
    service.approve("v1", "alice")
    service.activate("v1", now=_NOW)

    service.create_draft("v2", "d2", "r2", now=_NOW, previous_version="v1")
    service.submit_for_approval("v2")
    service.approve("v2", "alice")
    later = _NOW + dt.timedelta(days=1)
    service.activate("v2", now=later)

    v1 = service.get("v1")
    assert v1 is not None
    assert v1.is_active is False
    assert v1.effective_to == later
    active = service.get_active_version()
    assert active is not None
    assert active.rule_version == "v2"


def test_get_active_version_or_returns_default_when_none_active(
    service: RuleVersionService,
) -> None:
    assert service.get_active_version_or("v1-mvp") == "v1-mvp"


def test_get_active_version_or_returns_active_version(service: RuleVersionService) -> None:
    service.create_draft("v2-corporate-action-redesign", "desc", "reason", now=_NOW)
    service.submit_for_approval("v2-corporate-action-redesign")
    service.approve("v2-corporate-action-redesign", "alice")
    service.activate("v2-corporate-action-redesign", now=_NOW)

    assert service.get_active_version_or("v1-mvp") == "v2-corporate-action-redesign"


def test_activate_requires_approved_status(service: RuleVersionService) -> None:
    service.create_draft("v1", "d", "r", now=_NOW)
    with pytest.raises(ValueError):
        service.activate("v1")


def test_approve_requires_proposed_status(service: RuleVersionService) -> None:
    service.create_draft("v1", "d", "r", now=_NOW)
    with pytest.raises(ValueError):
        service.approve("v1", "alice")


def test_reject_requires_proposed_status(service: RuleVersionService) -> None:
    service.create_draft("v1", "d", "r", now=_NOW)
    with pytest.raises(ValueError):
        service.reject("v1")


def test_rollback_to_previous_version(service: RuleVersionService) -> None:
    service.create_draft("v1", "d1", "r1", now=_NOW)
    service.submit_for_approval("v1")
    service.approve("v1", "alice")
    service.activate("v1", now=_NOW)

    service.create_draft("v2", "d2", "r2", now=_NOW)
    service.submit_for_approval("v2")
    service.approve("v2", "alice")
    later = _NOW + dt.timedelta(days=1)
    service.activate("v2", now=later)

    rollback_time = later + dt.timedelta(days=1)
    reactivated = service.rollback_to("v1", now=rollback_time)

    assert reactivated.rule_version == "v1"
    assert reactivated.is_active is True
    v2 = service.get("v2")
    assert v2 is not None
    assert v2.is_active is False
    assert v2.approval_status == ApprovalStatus.ROLLED_BACK
    assert v2.rollback_target_version == "v1"


def test_rollback_requires_existing_active_version(service: RuleVersionService) -> None:
    service.create_draft("v1", "d", "r", now=_NOW)
    with pytest.raises(ValueError):
        service.rollback_to("v1")
