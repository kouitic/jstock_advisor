"""ルールバージョン管理サービス(要求仕様41・43節)。

判断ロジックのバージョンをDRAFT→PROPOSED→APPROVED→ACTIVEの順に遷移させる。
どの段階でも自動遷移は行わず、承認(approve)・有効化(activate)は必ず
人間の明示的な操作を要する(要求仕様45節: 人間承認必須)。
"""

from __future__ import annotations

import datetime as dt

from jstock_advisor.domain.entities.enums import ApprovalStatus
from jstock_advisor.domain.entities.rule_version import RuleVersion
from jstock_advisor.infrastructure.local_repository.rule_version_repository import (
    RuleVersionRepository,
)


class RuleVersionService:
    def __init__(self, repository: RuleVersionRepository | None = None) -> None:
        self._repo = repository or RuleVersionRepository()

    def list_all(self) -> list[RuleVersion]:
        return self._repo.list_all()

    def get(self, rule_version: str) -> RuleVersion | None:
        return self._repo.get(rule_version)

    def get_active_version(self) -> RuleVersion | None:
        for version in self._repo.list_all():
            if version.is_active:
                return version
        return None

    def _require(self, rule_version: str) -> RuleVersion:
        version = self._repo.get(rule_version)
        if version is None:
            raise ValueError(f"rule_version={rule_version} が見つかりません")
        return version

    def create_draft(
        self,
        rule_version: str,
        change_description: str,
        change_reason: str,
        based_on_review: str | None = None,
        backtest_result_ref: str | None = None,
        previous_version: str | None = None,
        now: dt.datetime | None = None,
    ) -> RuleVersion:
        if self._repo.get(rule_version) is not None:
            raise ValueError(f"rule_version={rule_version} は既に存在します")
        version = RuleVersion(
            rule_version=rule_version,
            created_at=now or dt.datetime.now(dt.UTC),
            change_description=change_description,
            change_reason=change_reason,
            approval_status=ApprovalStatus.DRAFT,
            based_on_review=based_on_review,
            backtest_result_ref=backtest_result_ref,
            previous_version=previous_version,
        )
        self._repo.save(version)
        return version

    def submit_for_approval(self, rule_version: str) -> RuleVersion:
        version = self._require(rule_version)
        if version.approval_status != ApprovalStatus.DRAFT:
            raise ValueError(
                f"DRAFT状態のバージョンのみ申請できます(現在: {version.approval_status.value})"
            )
        updated = version.model_copy(update={"approval_status": ApprovalStatus.PROPOSED})
        self._repo.save(updated)
        return updated

    def approve(self, rule_version: str, approved_by: str) -> RuleVersion:
        version = self._require(rule_version)
        if version.approval_status != ApprovalStatus.PROPOSED:
            raise ValueError(
                f"PROPOSED状態のバージョンのみ承認できます(現在: {version.approval_status.value})"
            )
        updated = version.model_copy(
            update={"approval_status": ApprovalStatus.APPROVED, "approved_by": approved_by}
        )
        self._repo.save(updated)
        return updated

    def reject(self, rule_version: str) -> RuleVersion:
        version = self._require(rule_version)
        if version.approval_status != ApprovalStatus.PROPOSED:
            raise ValueError(
                f"PROPOSED状態のバージョンのみ却下できます(現在: {version.approval_status.value})"
            )
        updated = version.model_copy(update={"approval_status": ApprovalStatus.REJECTED})
        self._repo.save(updated)
        return updated

    def activate(self, rule_version: str, now: dt.datetime | None = None) -> RuleVersion:
        version = self._require(rule_version)
        if version.approval_status != ApprovalStatus.APPROVED:
            raise ValueError(
                f"APPROVED状態のバージョンのみ有効化できます(現在: {version.approval_status.value})"
            )
        activation_time = now or dt.datetime.now(dt.UTC)

        current_active = self.get_active_version()
        if current_active is not None and current_active.rule_version != rule_version:
            self._repo.save(
                current_active.model_copy(
                    update={"is_active": False, "effective_to": activation_time}
                )
            )

        activated = version.model_copy(
            update={
                "approval_status": ApprovalStatus.ACTIVE,
                "is_active": True,
                "effective_from": activation_time,
                "effective_to": None,
            }
        )
        self._repo.save(activated)
        return activated

    def rollback_to(self, target_version: str, now: dt.datetime | None = None) -> RuleVersion:
        target = self._require(target_version)
        rollback_time = now or dt.datetime.now(dt.UTC)

        current_active = self.get_active_version()
        if current_active is None:
            raise ValueError("現在有効なルールバージョンがありません")
        if current_active.rule_version == target_version:
            raise ValueError("現在有効なバージョンへはロールバックできません")

        self._repo.save(
            current_active.model_copy(
                update={
                    "approval_status": ApprovalStatus.ROLLED_BACK,
                    "is_active": False,
                    "effective_to": rollback_time,
                    "rollback_target_version": target_version,
                }
            )
        )

        reactivated = target.model_copy(
            update={
                "approval_status": ApprovalStatus.ACTIVE,
                "is_active": True,
                "effective_from": rollback_time,
                "effective_to": None,
            }
        )
        self._repo.save(reactivated)
        return reactivated
