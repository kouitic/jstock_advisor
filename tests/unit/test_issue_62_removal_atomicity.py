"""Issue #62 Phase B: 自動削除の非原子性の解消(O-B 順序反転 + O-C 冪等な補完)。

Phase A(#62 issuecomment-5559190234)で確定した欠陥は次の 2 つ。

  1  `delete -> 履歴 -> 監査` の順で書いており、delete の直後に中断すると
     削除履歴が残らない。`is_in_cooldown()` は履歴が無ければ False を返すため、
     翌営業日の自動追加でクールダウン(既定 30 日)が素通りされる。
  2  finalize の `if item is None: continue` が、中断した削除を素通りさせるため
     監査記録が恒久的に欠落する。

本テストは実データ・実銘柄名を使わない(架空の銘柄コードと名称のみ)。
Production への injection は行わない。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.domain.entities.enums import WatchlistRegistrationSource
from jstock_advisor.domain.entities.watchlist import WatchlistItem, WatchlistRemovalHistory
from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.infrastructure.aws.batch_tracker import WatchlistProgressStatus
from jstock_advisor.infrastructure.local_repository.audit_log_repository import (
    AuditLogRepository,
)
from jstock_advisor.infrastructure.local_repository.watchlist_removal_history_repository import (
    WatchlistRemovalHistoryRepository,
)
from jstock_advisor.services import audit_service as audit_service_module
from jstock_advisor.services import watchlist_batch_finalizer as finalizer_module
from jstock_advisor.services.watchlist_batch_finalizer import maybe_finalize_maintenance
from jstock_advisor.services.watchlist_maintenance_service import MaintenanceScreeningSummary
from jstock_advisor.services.watchlist_screening_audit import (
    DECISION_TYPE_REMOVAL,
    REMOVAL_AUDIT_COMPLETION_COMPLETE,
    REMOVAL_AUDIT_COMPLETION_RECONSTRUCTED,
    build_removal_audit_id,
)

_REGION = "ap-northeast-1"
_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)
_BATCH_TABLE = "jstock-batch_runs"
_PROGRESS_TABLE = "jstock-watchlist_candidate_progress"
_CODE = "1111"


@pytest.fixture(autouse=True)
def _stub_display_name_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        finalizer_module,
        "build_stock_display_name_resolver",
        lambda *_a, **_kw: SimpleNamespace(resolve=lambda code, **_k: code),
    )


@pytest.fixture
def audit_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """AuditLogRepository を tmp へ隔離し、テストから直接読めるようにする。"""
    directory = tmp_path / "audit"
    monkeypatch.setattr(
        audit_service_module,
        "AuditLogRepository",
        lambda store_dir=None: AuditLogRepository(store_dir=directory),
    )
    return directory


@pytest.fixture
def dynamo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=_REGION)
        client.create_table(
            TableName=_BATCH_TABLE,
            KeySchema=[{"AttributeName": "batch_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "batch_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName=_PROGRESS_TABLE,
            KeySchema=[
                {"AttributeName": "batch_id", "KeyType": "HASH"},
                {"AttributeName": "stock_code", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "batch_id", "AttributeType": "S"},
                {"AttributeName": "stock_code", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


@pytest.fixture
def history_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> WatchlistRemovalHistoryRepository:
    store_dir = tmp_path / "removal_history"

    def _factory(readd_cooldown_days: int) -> WatchlistRemovalHistoryRepository:
        return WatchlistRemovalHistoryRepository(readd_cooldown_days, store_dir=store_dir)

    monkeypatch.setattr(finalizer_module, "WatchlistRemovalHistoryRepository", _factory)
    return WatchlistRemovalHistoryRepository(30, store_dir=store_dir)


class _RecordingWatchlistRepository:
    """呼び出し順序を観測できる WatchlistRepository のフェイク。"""

    def __init__(self, calls: list[str]) -> None:
        self._items: dict[str, WatchlistItem] = {}
        self.calls = calls

    def get(self, stock_code: str) -> WatchlistItem | None:
        return self._items.get(stock_code)

    def upsert(self, item: WatchlistItem) -> None:
        self._items[item.stock_code] = item

    def delete(self, stock_code: str) -> bool:
        self.calls.append("delete")
        return self._items.pop(stock_code, None) is not None


def _fake_config(*, readd_cooldown_days: int = 30) -> SimpleNamespace:
    return SimpleNamespace(
        watchlist_screening=SimpleNamespace(
            screening_policy="multi_style_monitoring",
            auto_removal=SimpleNamespace(
                enabled=True,
                readd_cooldown_days=readd_cooldown_days,
                minimum_age_days=90,
                consecutive_not_qualified_required=3,
                minimum_not_qualified_span_days=28,
                stale_recheck_days=30,
                maximum_unconfirmed_days=180,
            ),
        )
    )


def _removable_item(created_at: dt.datetime) -> WatchlistItem:
    """即時削除(債務超過)の対象になる AUTO_SCREENING 銘柄。"""
    return WatchlistItem(
        stock_code=_CODE,
        stock_name="架空銘柄A",
        reason="自動追加",
        registration_source=WatchlistRegistrationSource.AUTO_SCREENING,
        registration_policy="multi_style_monitoring",
        created_at=created_at,
        updated_at=created_at,
        consecutive_not_qualified_count=2,
        removal_candidate_since=created_at,
    )


def _drive_maintenance_batch(batch_id: str, now: dt.datetime) -> None:
    """1 銘柄が「削除相当」で終端したメンテナンスバッチを組み立てる。"""
    batch_tracker.try_acquire_dispatch_lease(batch_id, "dispatcher", now, 360, 72)
    batch_tracker.set_watchlist_batch_total(
        batch_id, 1, 72, now, job_type=batch_tracker.WatchlistJobType.WATCHLIST_MAINTENANCE
    )
    batch_tracker.create_missing_candidate_progress_rows(batch_id, [_CODE], now, 72)
    batch_tracker.mark_dispatch_completed(batch_id, now)
    batch_tracker.claim_candidate_lease(batch_id, _CODE, "owner-a", now, 240)
    summary = MaintenanceScreeningSummary(
        passed=False,
        total_score=10.0,
        matched_target_types=[],
        hard_exclusion_reasons=["債務超過のため対象外です"],
        policy_name="multi_style_monitoring",
    )
    batch_tracker.complete_candidate(
        batch_id,
        _CODE,
        "owner-a",
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result="FAILED",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=now,
        screening_summary_json=summary.model_dump_json(),
    )


def _removal_audits(audit_dir: Path) -> list[Any]:
    return [
        e
        for e in AuditLogRepository(store_dir=audit_dir).list_all()
        if e.decision_type == DECISION_TYPE_REMOVAL
    ]


# --- U1: 順序反転の正経路 -------------------------------------------------------


def test_history_is_written_before_delete(
    dynamo, audit_dir: Path, history_repo: WatchlistRemovalHistoryRepository,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """履歴 upsert -> delete -> 監査 の順であること(O-B)。

    旧実装は delete が先だったため、delete 直後の中断で履歴が残らなかった。
    """
    calls: list[str] = []
    repo = _RecordingWatchlistRepository(calls)
    repo.upsert(_removable_item(_NOW - dt.timedelta(days=120)))
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: repo)

    original_upsert = WatchlistRemovalHistoryRepository.upsert

    def _recording_upsert(self, item):  # noqa: ANN001, ANN202
        calls.append("history_upsert")
        return original_upsert(self, item)

    monkeypatch.setattr(WatchlistRemovalHistoryRepository, "upsert", _recording_upsert)

    original_audit = finalizer_module.record_removal_audit

    def _recording_audit(*args: Any, **kwargs: Any) -> None:
        calls.append("audit")
        original_audit(*args, **kwargs)

    monkeypatch.setattr(finalizer_module, "record_removal_audit", _recording_audit)

    _drive_maintenance_batch("maint-order", _NOW)
    assert maybe_finalize_maintenance("maint-order", _NOW, _fake_config()) is True

    assert calls == ["history_upsert", "delete", "audit"]
    assert repo.get(_CODE) is None
    assert history_repo.get(_CODE) is not None
    assert len(_removal_audits(audit_dir)) == 1
    assert _removal_audits(audit_dir)[0].output_values["audit_completion"] == (
        REMOVAL_AUDIT_COMPLETION_COMPLETE
    )


# --- 中断シナリオ: 履歴だけ書けた（delete 前に落ちた） ---------------------------


def test_interrupted_after_history_leaves_item_and_converges_on_retry(
    dynamo, audit_dir: Path, history_repo: WatchlistRemovalHistoryRepository,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """履歴 upsert の直後に落ちた場合、ウォッチリスト項目は残る(安全側)。

    次回の finalize が同じ判定に至れば履歴が upsert し直され、削除まで進む。
    """
    calls: list[str] = []
    repo = _RecordingWatchlistRepository(calls)
    repo.upsert(_removable_item(_NOW - dt.timedelta(days=120)))
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: repo)

    def _boom(self, stock_code: str) -> bool:  # noqa: ANN001
        raise RuntimeError("simulated crash after history upsert")

    monkeypatch.setattr(_RecordingWatchlistRepository, "delete", _boom)

    _drive_maintenance_batch("maint-crash-1", _NOW)
    with pytest.raises(RuntimeError):
        maybe_finalize_maintenance("maint-crash-1", _NOW, _fake_config())

    # 履歴だけが残り、銘柄はまだウォッチリストにある。
    assert history_repo.get(_CODE) is not None
    assert repo.get(_CODE) is not None
    assert _removal_audits(audit_dir) == []

    # 次回の finalize（delete を復旧）で削除まで進み、監査も 1 件だけ残る。
    monkeypatch.setattr(
        _RecordingWatchlistRepository,
        "delete",
        lambda self, stock_code: self._items.pop(stock_code, None) is not None,
    )
    _drive_maintenance_batch("maint-crash-1-retry", _NOW)
    assert maybe_finalize_maintenance("maint-crash-1-retry", _NOW, _fake_config()) is True
    assert repo.get(_CODE) is None
    assert len(_removal_audits(audit_dir)) == 1


# --- U2 / O-C: 削除だけ済んで監査が欠落した場合の補完 ---------------------------


def _preexisting_removal(history_repo: WatchlistRemovalHistoryRepository,
                         removed_at: dt.datetime) -> None:
    history_repo.upsert(
        WatchlistRemovalHistory(
            stock_code=_CODE,
            removed_at=removed_at,
            removal_reason="債務超過のため対象外です",
            removal_category="IMMEDIATE",
            cooldown_until=removed_at + dt.timedelta(days=30),
        )
    )


def test_missing_audit_is_completed_from_history(
    dynamo, audit_dir: Path, history_repo: WatchlistRemovalHistoryRepository,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """削除済み・履歴あり・監査なし の銘柄は、次回 finalize で監査が補完される。"""
    removed_at = _NOW - dt.timedelta(days=1)
    _preexisting_removal(history_repo, removed_at)

    repo = _RecordingWatchlistRepository([])  # 既に削除済みなので空
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: repo)

    _drive_maintenance_batch("maint-complete", _NOW)
    assert maybe_finalize_maintenance("maint-complete", _NOW, _fake_config()) is True

    audits = _removal_audits(audit_dir)
    assert len(audits) == 1
    out = audits[0].output_values
    assert out["audit_completion"] == REMOVAL_AUDIT_COMPLETION_RECONSTRUCTED
    assert out["removed_at"] == removed_at.isoformat()
    assert out["removal_reason"] == "債務超過のため対象外です"
    assert out["removal_category"] == "IMMEDIATE"
    # 復元できなかった項目が「欠測」ではなく「復元不能」として明示されること。
    assert "stock_name" in out["unavailable_fields"]
    assert out["stock_name"] is None
    assert audits[0].audit_id == build_removal_audit_id(_CODE, removed_at)


def test_audit_completion_is_idempotent(
    dynamo, audit_dir: Path, history_repo: WatchlistRemovalHistoryRepository,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """補完は 2 回走っても監査記録が二重にならない(決定的 audit_id)。"""
    removed_at = _NOW - dt.timedelta(days=1)
    _preexisting_removal(history_repo, removed_at)
    repo = _RecordingWatchlistRepository([])
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: repo)

    for batch_id in ("maint-idem-1", "maint-idem-2"):
        _drive_maintenance_batch(batch_id, _NOW)
        assert maybe_finalize_maintenance(batch_id, _NOW, _fake_config()) is True

    assert len(_removal_audits(audit_dir)) == 1


def test_no_history_keeps_previous_skip_behaviour(
    dynamo, audit_dir: Path, history_repo: WatchlistRemovalHistoryRepository,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """履歴が無い(手動削除等)場合は従来どおり skip し、監査を作らない。"""
    repo = _RecordingWatchlistRepository([])
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: repo)

    _drive_maintenance_batch("maint-manual", _NOW)
    assert maybe_finalize_maintenance("maint-manual", _NOW, _fake_config()) is True

    assert _removal_audits(audit_dir) == []
    assert history_repo.get(_CODE) is None


# --- cooldown が履歴欠落で破られないこと ----------------------------------------


@pytest.mark.parametrize("crash_at", ["history_upsert", "delete", "audit"])
def test_deleted_without_history_never_occurs(
    crash_at: str,
    dynamo, audit_dir: Path, history_repo: WatchlistRemovalHistoryRepository,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 本 Issue の中心的な不変条件。

    削除の 3 手順のどこで落ちても
    **「ウォッチリストから消えたのに削除履歴が無い」状態にならない**こと。
    この状態こそが `is_in_cooldown()` を False にし、
    翌営業日の自動追加でクールダウン(既定 30 日)を素通りさせる原因だった。

    旧実装(delete -> 履歴)では crash_at="delete" のときにこの状態が生じる。
    """
    repo = _RecordingWatchlistRepository([])
    repo.upsert(_removable_item(_NOW - dt.timedelta(days=120)))
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: repo)

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"simulated crash at {crash_at}")

    if crash_at == "history_upsert":
        monkeypatch.setattr(WatchlistRemovalHistoryRepository, "upsert", _boom)
    elif crash_at == "delete":
        monkeypatch.setattr(_RecordingWatchlistRepository, "delete", _boom)
    else:
        monkeypatch.setattr(finalizer_module, "record_removal_audit", _boom)

    _drive_maintenance_batch(f"maint-crash-{crash_at}", _NOW)
    with pytest.raises(RuntimeError):
        maybe_finalize_maintenance(f"maint-crash-{crash_at}", _NOW, _fake_config())

    deleted = repo.get(_CODE) is None
    has_history = history_repo.get(_CODE) is not None
    assert not (deleted and not has_history), (
        f"crash_at={crash_at}: 削除済みなのに削除履歴が無い"
        "(クールダウンが素通りされる状態)"
    )
    if deleted:
        # 削除まで進んだ場合はクールダウンが必ず効いていること。
        assert history_repo.is_in_cooldown(_CODE, _NOW) is True
        assert history_repo.is_in_cooldown(_CODE, _NOW + dt.timedelta(days=29)) is True
        assert history_repo.is_in_cooldown(_CODE, _NOW + dt.timedelta(days=31)) is False


def test_cooldown_survives_crash_before_audit_and_audit_is_completed_later(
    dynamo, audit_dir: Path, history_repo: WatchlistRemovalHistoryRepository,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """監査の直前で落ちた場合: 削除は済み監査は欠落するが、
    履歴が先に書かれているためクールダウンは効き、次回 finalize が監査を補完する。"""
    repo = _RecordingWatchlistRepository([])
    repo.upsert(_removable_item(_NOW - dt.timedelta(days=120)))
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: repo)

    original_audit = finalizer_module.record_removal_audit

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated crash after delete, before audit")

    monkeypatch.setattr(finalizer_module, "record_removal_audit", _boom)

    _drive_maintenance_batch("maint-cooldown", _NOW)
    with pytest.raises(RuntimeError):
        maybe_finalize_maintenance("maint-cooldown", _NOW, _fake_config())

    assert repo.get(_CODE) is None
    assert _removal_audits(audit_dir) == []
    assert history_repo.is_in_cooldown(_CODE, _NOW) is True

    monkeypatch.setattr(finalizer_module, "record_removal_audit", original_audit)
    _drive_maintenance_batch("maint-cooldown-retry", _NOW)
    assert maybe_finalize_maintenance("maint-cooldown-retry", _NOW, _fake_config()) is True
    audits = _removal_audits(audit_dir)
    assert len(audits) == 1
    assert audits[0].output_values["audit_completion"] == REMOVAL_AUDIT_COMPLETION_RECONSTRUCTED


# --- 決定的 audit_id が複数回の削除を潰さないこと --------------------------------


def test_audit_id_distinguishes_repeated_removals() -> None:
    """同一銘柄が期間をおいて 2 回削除された場合、audit_id は別になる。

    `stock_code` だけを id にすると、2 回目の削除の監査が
    `record_if_absent()` に「既にある」と判定されて失われる。
    """
    first = build_removal_audit_id(_CODE, _NOW)
    second = build_removal_audit_id(_CODE, _NOW + dt.timedelta(days=60))
    assert first != second
    assert build_removal_audit_id(_CODE, _NOW) == first


# --- U3: 観測カウンタ ------------------------------------------------------------


def test_interrupted_removal_counts_are_recorded_in_batch_audit(
    dynamo, audit_dir: Path, history_repo: WatchlistRemovalHistoryRepository,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """補完の発生件数が batch audit の output_values に載ること(U3)。"""
    _preexisting_removal(history_repo, _NOW - dt.timedelta(days=1))
    repo = _RecordingWatchlistRepository([])
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: repo)

    _drive_maintenance_batch("maint-count", _NOW)
    assert maybe_finalize_maintenance("maint-count", _NOW, _fake_config()) is True

    batch_audits = [
        e
        for e in AuditLogRepository(store_dir=audit_dir).list_all()
        if e.audit_id == "watchlist_maintenance_batch_audit:maint-count"
    ]
    assert len(batch_audits) == 1
    out = batch_audits[0].output_values
    assert out["interrupted_removal_detected_count"] == 1
    assert out["interrupted_removal_audit_completed_count"] == 1


def test_normal_run_reports_zero_interrupted_counts(
    dynamo, audit_dir: Path, history_repo: WatchlistRemovalHistoryRepository,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """中断が無い通常実行では観測カウンタが 0 であること(平常時の期待値)。"""
    repo = _RecordingWatchlistRepository([])
    repo.upsert(_removable_item(_NOW - dt.timedelta(days=120)))
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: repo)

    _drive_maintenance_batch("maint-zero", _NOW)
    assert maybe_finalize_maintenance("maint-zero", _NOW, _fake_config()) is True

    batch_audits = [
        e
        for e in AuditLogRepository(store_dir=audit_dir).list_all()
        if e.audit_id == "watchlist_maintenance_batch_audit:maint-zero"
    ]
    assert len(batch_audits) == 1
    assert batch_audits[0].output_values["interrupted_removal_detected_count"] == 0
    assert batch_audits[0].output_values["interrupted_removal_audit_completed_count"] == 0
