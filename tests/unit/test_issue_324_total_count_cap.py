"""Issue #324: ウォッチリスト総件数の上限(total_count_cap)と淘汰。

設計: #324 issuecomment-5853129857 / 修正設計 …-5854072710。
USER 決定(OD1 = A・OD3 = B): …-5854275274。

- 淘汰の順序 = last_monitoring_score の昇順、同点は created_at の昇順(OD1 = A)
- 再追加の防止 = 既存の時間ベース cooldown(readd_cooldown_days)。floor score の gate なし(OD3 = B)
- MANUAL 登録銘柄は淘汰しない。総件数には数える
- last_screening_result == "NOT_EVALUABLE"(#141)の項目は ACTIVE_CAPACITY_COUNT に数えない

本テストは実データ・実銘柄名を使わない(架空の銘柄コードと名称のみ)。
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from moto import mock_aws
from pydantic import ValidationError

from jstock_advisor.config.models import AutoRemovalConfig
from jstock_advisor.domain.entities.enums import WatchlistRegistrationSource
from jstock_advisor.domain.entities.watchlist import WatchlistItem
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
from jstock_advisor.services.watchlist_batch_finalizer import (
    REMOVAL_CATEGORY_CAPACITY_EVICTION,
    _evict_over_capacity,
    maybe_finalize_maintenance,
)
from jstock_advisor.services.watchlist_maintenance_service import MaintenanceScreeningSummary
from jstock_advisor.services.watchlist_screening_audit import DECISION_TYPE_REMOVAL

_AUTO = WatchlistRegistrationSource.AUTO_SCREENING
_MANUAL = WatchlistRegistrationSource.MANUAL
_T0 = dt.datetime(2026, 1, 1, 0, 0, tzinfo=dt.UTC)
_NOW = dt.datetime(2026, 10, 8, 7, 0, tzinfo=dt.UTC)
_COOLDOWN_DAYS = 30


def _item(
    code: str,
    score: float | None,
    *,
    source: WatchlistRegistrationSource = _AUTO,
    age_days: int = 0,
    result: str | None = "PASSED",
) -> WatchlistItem:
    created = _T0 + dt.timedelta(days=age_days)
    return WatchlistItem(
        stock_code=code,
        stock_name=f"架空銘柄{code}",
        reason="テスト",
        registration_source=source,
        registration_policy="multi_style_monitoring" if source == _AUTO else None,
        created_at=created,
        updated_at=created,
        last_monitoring_score=score,
        last_screening_result=result,
    )


def _codes(items: list[WatchlistItem]) -> list[str]:
    return [i.stock_code for i in items]


# --- 純粋関数 _evict_over_capacity --------------------------------------------


class TestEvictOverCapacity:
    def _auto_items(self, n: int) -> list[WatchlistItem]:
        # スコアは code の数値に比例(小さい code ほど低スコア)
        return [_item(f"{1000 + i}", 50.0 + i) for i in range(n)]

    def test_exactly_at_cap_evicts_nothing(self) -> None:
        assert _evict_over_capacity(self._auto_items(5), 5) == []

    def test_one_over_cap_evicts_the_single_lowest_score(self) -> None:
        items = self._auto_items(6)
        assert _codes(_evict_over_capacity(items, 5)) == ["1000"]

    def test_one_under_cap_evicts_nothing(self) -> None:
        assert _evict_over_capacity(self._auto_items(4), 5) == []

    def test_cap_larger_than_current_evicts_nothing(self) -> None:
        assert _evict_over_capacity(self._auto_items(10), 1300) == []

    def test_evicts_exactly_the_overflow_in_ascending_score_order(self) -> None:
        items = self._auto_items(10)
        random.Random(324).shuffle(items)
        assert _codes(_evict_over_capacity(items, 7)) == ["1000", "1001", "1002"]

    def test_tie_break_is_created_at_ascending_and_deterministic(self) -> None:
        # 同点のスコア。created_at が古いものから淘汰される。入力順に依存しない
        items = [
            _item("2003", 60.0, age_days=30),
            _item("2001", 60.0, age_days=10),
            _item("2002", 60.0, age_days=20),
            _item("2004", 70.0, age_days=0),
        ]
        for seed in range(5):
            shuffled = list(items)
            random.Random(seed).shuffle(shuffled)
            assert _codes(_evict_over_capacity(shuffled, 2)) == ["2001", "2002"]

    def test_none_score_is_treated_as_lowest(self) -> None:
        items = [_item("3001", 10.0), _item("3002", None), _item("3003", 20.0)]
        assert _codes(_evict_over_capacity(items, 2)) == ["3002"]

    def test_manual_is_never_evicted_even_with_the_lowest_score(self) -> None:
        items = [
            _item("4001", 1.0, source=_MANUAL),
            _item("4002", 80.0),
            _item("4003", 90.0),
        ]
        # MANUAL も総件数には数える(3 件 > cap 2)。淘汰は AUTO の最低スコア
        assert _codes(_evict_over_capacity(items, 2)) == ["4002"]

    def test_manual_only_over_cap_evicts_nothing(self) -> None:
        items = [_item(f"{5000 + i}", 10.0 + i, source=_MANUAL) for i in range(6)]
        assert _evict_over_capacity(items, 3) == []

    def test_legacy_item_without_registration_source_is_treated_as_manual(self) -> None:
        # 旧形式(registration_source を持たない)のレコードは entity の既定 = MANUAL で読まれる
        legacy = WatchlistItem.model_validate(
            {
                "stock_code": "6001",
                "created_at": _T0.isoformat(),
                "updated_at": _T0.isoformat(),
                "last_monitoring_score": 0.0,
                "last_screening_result": "PASSED",
            }
        )
        assert legacy.registration_source == _MANUAL
        items = [legacy, _item("6002", 70.0), _item("6003", 80.0)]
        assert _codes(_evict_over_capacity(items, 2)) == ["6002"]

    def test_not_evaluable_is_excluded_from_the_active_count(self) -> None:
        # 物理 5 件(うち NOT_EVALUABLE 2 件)。ACTIVE は 3 件 = cap 3 → 淘汰なし
        items = [
            _item("7001", 50.0),
            _item("7002", 60.0),
            _item("7003", 70.0),
            _item("7004", 1.0, result="NOT_EVALUABLE"),
            _item("7005", 2.0, result="NOT_EVALUABLE"),
        ]
        assert _evict_over_capacity(items, 3) == []

    def test_not_evaluable_is_never_selected_for_eviction(self) -> None:
        # ACTIVE 4 件 > cap 3。最低スコアの NOT_EVALUABLE(0.0)ではなく ACTIVE の最低を選ぶ
        items = [
            _item("7101", 50.0),
            _item("7102", 60.0),
            _item("7103", 70.0),
            _item("7104", 80.0),
            _item("7105", 0.0, result="NOT_EVALUABLE"),
        ]
        assert _codes(_evict_over_capacity(items, 3)) == ["7101"]

    def test_overflow_larger_than_eligible_returns_all_eligible_only(self) -> None:
        items = [
            *[_item(f"{8000 + i}", 10.0, source=_MANUAL) for i in range(4)],
            _item("8100", 50.0),
            _item("8101", 60.0),
        ]
        # ACTIVE 6 件、cap 2 → 超過 4 件。AUTO は 2 件しか無い → その 2 件だけ(MANUAL は外さない)
        assert _codes(_evict_over_capacity(items, 2)) == ["8100", "8101"]

    def test_does_not_mutate_its_input(self) -> None:
        items = self._auto_items(6)
        before = list(items)
        _evict_over_capacity(items, 3)
        assert items == before


# --- config ----------------------------------------------------------------------


_AUTO_REMOVAL_KWARGS: dict[str, Any] = {
    "enabled": True,
    "minimum_age_days": 90,
    "consecutive_not_qualified_required": 3,
    "minimum_not_qualified_span_days": 28,
    "stale_recheck_days": 30,
    "maximum_unconfirmed_days": 180,
    "readd_cooldown_days": _COOLDOWN_DAYS,
}


class TestTotalCountCapConfig:
    def test_field_is_required_without_a_default(self) -> None:
        # 上限が無い状態を黙って許さない(設定漏れは config 読み込みで失敗する)
        with pytest.raises(ValidationError, match="total_count_cap"):
            AutoRemovalConfig(**_AUTO_REMOVAL_KWARGS)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_field_must_be_positive(self, bad: int) -> None:
        with pytest.raises(ValidationError, match="total_count_cap"):
            AutoRemovalConfig(**_AUTO_REMOVAL_KWARGS, total_count_cap=bad)

    def test_valid_value_is_accepted(self) -> None:
        assert AutoRemovalConfig(**_AUTO_REMOVAL_KWARGS, total_count_cap=1).total_count_cap == 1

    def test_shipped_yaml_defines_a_positive_cap(self) -> None:
        from jstock_advisor.config.loader import load_config

        cap = load_config().watchlist_screening.auto_removal.total_count_cap
        assert isinstance(cap, int) and cap > 0


# --- 統合: maintenance finalize ----------------------------------------------------

_REGION = "ap-northeast-1"
_BATCH_TABLE = "jstock-batch_runs"
_PROGRESS_TABLE = "jstock-watchlist_candidate_progress"
_REMOVAL_HISTORY_TABLE = "jstock-watchlist_removal_history"
_AUDIT_LOG_TABLE = "jstock-audit_log"
_BATCH_CODE = "1111"


@pytest.fixture(autouse=True)
def _stub_display_name_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        finalizer_module,
        "build_stock_display_name_resolver",
        lambda *_a, **_kw: SimpleNamespace(resolve=lambda code, **_k: code),
    )


@pytest.fixture
def audit_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    directory = tmp_path / "audit"
    monkeypatch.setattr(
        audit_service_module,
        "AuditLogRepository",
        lambda store_dir=None: AuditLogRepository(store_dir=directory),
    )
    return directory


@pytest.fixture
def dynamo(monkeypatch: pytest.MonkeyPatch, lambda_runtime_env: None):
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
        for table_name, key in (
            (_REMOVAL_HISTORY_TABLE, "stock_code"),
            (_AUDIT_LOG_TABLE, "audit_id"),
        ):
            client.create_table(
                TableName=table_name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
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
    return WatchlistRemovalHistoryRepository(_COOLDOWN_DAYS, store_dir=store_dir)


class _FakeWatchlistRepository:
    """呼び出し順序を観測でき、淘汰の直前の get() を差し替えられるフェイク。"""

    def __init__(self, calls: list[str]) -> None:
        self._items: dict[str, WatchlistItem] = {}
        self.calls = calls
        # code -> get() が返す内容(iter_all の状態と食い違わせ、走査後の変化を再現する)
        self.get_override: dict[str, WatchlistItem | None] = {}

    def get(self, stock_code: str) -> WatchlistItem | None:
        if stock_code in self.get_override:
            return self.get_override[stock_code]
        return self._items.get(stock_code)

    def upsert(self, item: WatchlistItem) -> None:
        self._items[item.stock_code] = item

    def delete(self, stock_code: str) -> bool:
        self.calls.append(f"delete:{stock_code}")
        return self._items.pop(stock_code, None) is not None

    def iter_all(self) -> Iterator[WatchlistItem]:
        return iter(list(self._items.values()))

    @property
    def codes(self) -> set[str]:
        return set(self._items)


def _config(cap: int) -> SimpleNamespace:
    return SimpleNamespace(
        watchlist_screening=SimpleNamespace(
            screening_policy="multi_style_monitoring",
            auto_removal=SimpleNamespace(
                enabled=True,
                readd_cooldown_days=_COOLDOWN_DAYS,
                minimum_age_days=90,
                consecutive_not_qualified_required=3,
                minimum_not_qualified_span_days=28,
                stale_recheck_days=30,
                maximum_unconfirmed_days=180,
                total_count_cap=cap,
            ),
        )
    )


def _drive_maintenance_batch(batch_id: str, now: dt.datetime, *, passed: bool = True) -> None:
    """1 銘柄(_BATCH_CODE)だけを評価するメンテナンスバッチ。passed=False は債務超過(即時削除)。"""
    batch_tracker.try_acquire_dispatch_lease(batch_id, "dispatcher", now, 360, 72)
    batch_tracker.set_watchlist_batch_total(
        batch_id, 1, 72, now, job_type=batch_tracker.WatchlistJobType.WATCHLIST_MAINTENANCE
    )
    batch_tracker.create_missing_candidate_progress_rows(batch_id, [_BATCH_CODE], now, 72)
    batch_tracker.mark_dispatch_completed(batch_id, now)
    batch_tracker.claim_candidate_lease(batch_id, _BATCH_CODE, "owner-a", now, 240)
    summary = MaintenanceScreeningSummary(
        passed=passed,
        total_score=95.0 if passed else 10.0,
        matched_target_types=[],
        hard_exclusion_reasons=[] if passed else ["債務超過のため対象外です"],
        policy_name="multi_style_monitoring",
    )
    batch_tracker.complete_candidate(
        batch_id,
        _BATCH_CODE,
        "owner-a",
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result="PASSED" if passed else "FAILED",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=now,
        screening_summary_json=summary.model_dump_json(),
    )


def _seed(repo: _FakeWatchlistRepository, items: list[WatchlistItem]) -> None:
    for item in items:
        repo.upsert(item)


def _batch_output(audit_dir: Path, batch_id: str) -> dict[str, Any]:
    entries = [
        e
        for e in AuditLogRepository(store_dir=audit_dir).list_all()
        if e.audit_id == f"watchlist_maintenance_batch_audit:{batch_id}"
    ]
    assert len(entries) == 1
    return dict(entries[0].output_values)


def _removal_audits(audit_dir: Path) -> list[Any]:
    return [
        e
        for e in AuditLogRepository(store_dir=audit_dir).list_all()
        if e.decision_type == DECISION_TYPE_REMOVAL
    ]


def _batch_item() -> WatchlistItem:
    # バッチで評価される銘柄。評価後のスコアは 95.0(合格)に更新される
    return _item(_BATCH_CODE, 40.0, age_days=300)


def _run(
    batch_id: str, repo: _FakeWatchlistRepository, monkeypatch: pytest.MonkeyPatch, cap: int,
    *, passed: bool = True,
) -> None:
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: repo)
    _drive_maintenance_batch(batch_id, _NOW, passed=passed)
    assert maybe_finalize_maintenance(batch_id, _NOW, _config(cap)) is True


def test_over_cap_evicts_lowest_scores_with_history_delete_audit_order(
    dynamo, audit_dir: Path, history_repo: WatchlistRemovalHistoryRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    repo = _FakeWatchlistRepository(calls)
    # 7 件(バッチの 1111 を含む AUTO 6 + MANUAL 1)、cap 5 → 超過 2 件 = AUTO の最低スコア 2 件
    _seed(
        repo,
        [
            _batch_item(),
            _item("9001", 30.0, age_days=5),
            _item("9002", 31.0, age_days=5),
            _item("9003", 70.0, age_days=5),
            _item("9004", 80.0, age_days=5),
            _item("9005", 5.0, source=_MANUAL, age_days=5),
            _item("9006", 90.0, age_days=5),
        ],
    )

    original_history_upsert = WatchlistRemovalHistoryRepository.upsert

    def _recording_history_upsert(self, item):  # noqa: ANN001, ANN202
        calls.append(f"history:{item.stock_code}")
        return original_history_upsert(self, item)

    monkeypatch.setattr(WatchlistRemovalHistoryRepository, "upsert", _recording_history_upsert)
    original_audit = finalizer_module.record_removal_audit

    def _recording_audit(*args: Any, **kwargs: Any) -> Any:
        calls.append(f"audit:{args[0]}")
        return original_audit(*args, **kwargs)

    monkeypatch.setattr(finalizer_module, "record_removal_audit", _recording_audit)

    _run("cap-evict", repo, monkeypatch, cap=5)

    # 淘汰されたのは、スコアの低い AUTO 2 件(MANUAL 9005 はスコア 5.0 でも残る)
    assert repo.codes == {_BATCH_CODE, "9003", "9004", "9005", "9006"}
    # 各銘柄で 履歴 -> delete -> 監査 の順(Issue #62 Phase B と同じ)
    assert calls == [
        "history:9001", "delete:9001", "audit:9001",
        "history:9002", "delete:9002", "audit:9002",
    ]
    for code in ("9001", "9002"):
        history = history_repo.get(code)
        assert history is not None
        assert history.removal_category == REMOVAL_CATEGORY_CAPACITY_EVICTION == "CAPACITY_EVICTION"
        # OD3 = B: 既存の時間ベース cooldown(readd_cooldown_days)。floor score の gate は無い
        assert history.cooldown_until == _NOW + dt.timedelta(days=_COOLDOWN_DAYS)
        assert history_repo.is_in_cooldown(code, _NOW)
    assert history_repo.get("9003") is None

    audits = _removal_audits(audit_dir)
    assert sorted(a.output_values["removal_category"] for a in audits) == [
        "CAPACITY_EVICTION", "CAPACITY_EVICTION",
    ]

    out = _batch_output(audit_dir, "cap-evict")
    assert out["outcome_counts"]["CAPACITY_EVICTION"] == 2
    assert out["capacity_total_count_cap"] == 5
    assert out["capacity_active_count"] == 7
    assert out["capacity_over_count_before_eviction"] == 2
    assert out["capacity_evicted_count"] == 2
    assert out["capacity_over_count_after_eviction"] == 0
    assert out["removed_count"] == 2
    # 物理件数(この回の開始時点)は従来どおり別の field
    assert out["watchlist_total_count"] == 7


def test_second_run_after_convergence_evicts_nothing(
    dynamo, audit_dir: Path, history_repo: WatchlistRemovalHistoryRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeWatchlistRepository([])
    _seed(
        repo,
        [_batch_item(), _item("9001", 30.0), _item("9002", 31.0), _item("9003", 70.0)],
    )
    _run("cap-first", repo, monkeypatch, cap=3)
    assert repo.codes == {_BATCH_CODE, "9002", "9003"}

    _run("cap-second", repo, monkeypatch, cap=3)
    out = _batch_output(audit_dir, "cap-second")
    assert out["capacity_evicted_count"] == 0
    assert "CAPACITY_EVICTION" not in out["outcome_counts"]
    assert repo.codes == {_BATCH_CODE, "9002", "9003"}


def test_cap_larger_than_current_evicts_nothing_and_changes_no_existing_behaviour(
    dynamo, audit_dir: Path, history_repo: WatchlistRemovalHistoryRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeWatchlistRepository([])
    _seed(repo, [_batch_item(), _item("9001", 1.0), _item("9002", 2.0)])
    _run("cap-large", repo, monkeypatch, cap=1300)

    assert repo.codes == {_BATCH_CODE, "9001", "9002"}
    assert _removal_audits(audit_dir) == []
    out = _batch_output(audit_dir, "cap-large")
    assert out["capacity_evicted_count"] == 0
    assert out["capacity_over_count_before_eviction"] == 0
    assert "CAPACITY_EVICTION" not in out["outcome_counts"]


def test_no_eviction_when_the_existing_auto_removal_already_brings_the_count_to_the_cap(
    dynamo, audit_dir: Path, history_repo: WatchlistRemovalHistoryRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既存の自動削除(Bルート: 3回連続非該当)が先に働き、それで上限内に収まるなら淘汰しない。"""
    repo = _FakeWatchlistRepository([])
    created = _NOW - dt.timedelta(days=120)
    removable = _item(_BATCH_CODE, 40.0).model_copy(
        update={
            "created_at": created,
            "updated_at": created,
            "consecutive_not_qualified_count": 2,
            "removal_candidate_since": created,
        }
    )
    # 3 回目の非該当で削除されるバッチの銘柄(_BATCH_CODE)を含む 4 件、cap 3
    _seed(
        repo,
        [
            removable,
            _item("9001", 30.0),
            _item("9002", 31.0),
            _item("9003", 70.0),
        ],
    )
    _run("cap-after-removal", repo, monkeypatch, cap=3, passed=False)

    assert repo.codes == {"9001", "9002", "9003"}  # 淘汰されたのは評価で外れた 1 件だけ
    out = _batch_output(audit_dir, "cap-after-removal")
    assert out["capacity_active_count"] == 3  # 既存の削除の後の件数で判定している
    assert out["capacity_evicted_count"] == 0
    assert out["outcome_counts"].get("CONSECUTIVE_NOT_QUALIFIED_REMOVAL") == 1
    assert "CAPACITY_EVICTION" not in out["outcome_counts"]
    assert history_repo.get(_BATCH_CODE).removal_category == "CONSECUTIVE_NOT_QUALIFIED"  # type: ignore[union-attr]


def test_not_evaluable_items_do_not_trigger_eviction(
    dynamo, audit_dir: Path, history_repo: WatchlistRemovalHistoryRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeWatchlistRepository([])
    # 物理 5 件(NOT_EVALUABLE 2 件)。ACTIVE 3 件 = cap 3
    _seed(
        repo,
        [
            _batch_item(),
            _item("9001", 30.0),
            _item("9002", 31.0),
            _item("9003", 1.0, result="NOT_EVALUABLE"),
            _item("9004", 2.0, result="NOT_EVALUABLE"),
        ],
    )
    _run("cap-ne", repo, monkeypatch, cap=3)

    assert len(repo.codes) == 5
    out = _batch_output(audit_dir, "cap-ne")
    assert out["watchlist_total_count"] == 5
    assert out["capacity_active_count"] == 3
    assert out["capacity_evicted_count"] == 0


def test_manual_heavy_watchlist_reports_the_unreached_cap(
    dynamo, audit_dir: Path, history_repo: WatchlistRemovalHistoryRepository,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """MANUAL が多く AUTO だけでは上限に届かない場合は、MANUAL を外さず WARNING を残す。"""
    repo = _FakeWatchlistRepository([])
    _seed(
        repo,
        [
            _batch_item(),
            *[_item(f"{9100 + i}", 1.0, source=_MANUAL) for i in range(4)],
        ],
    )
    with caplog.at_level("WARNING", logger=finalizer_module.logger.name):
        _run("cap-manual", repo, monkeypatch, cap=2)

    # AUTO は _BATCH_CODE の 1 件だけ → それを外しても 4 件(> cap 2)。MANUAL は残る
    assert repo.codes == {f"{9100 + i}" for i in range(4)}
    out = _batch_output(audit_dir, "cap-manual")
    assert out["capacity_evicted_count"] == 1
    assert out["capacity_over_count_after_eviction"] == 2
    assert any("capacity cap not reached" in r.getMessage() for r in caplog.records)


def test_item_re_registered_as_manual_after_the_scan_is_not_evicted(
    dynamo, audit_dir: Path, history_repo: WatchlistRemovalHistoryRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """走査から淘汰の直前までの間に MANUAL へ変わった項目を、誤って外さない。"""
    repo = _FakeWatchlistRepository([])
    _seed(repo, [_batch_item(), _item("9001", 30.0), _item("9002", 31.0), _item("9003", 70.0)])
    repo.get_override["9001"] = _item("9001", 30.0, source=_MANUAL)

    _run("cap-race", repo, monkeypatch, cap=3)

    assert "9001" in repo.codes  # 外されない
    assert history_repo.get("9001") is None
    out = _batch_output(audit_dir, "cap-race")
    assert out["capacity_evicted_count"] == 0
    assert out["capacity_eviction_skipped_count"] == 1


def test_eviction_is_actually_wired_into_the_maintenance_finalize(
    dynamo, audit_dir: Path, history_repo: WatchlistRemovalHistoryRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 関数を呼ばなくなったら落ちること(純粋関数のテストだけでは配線の欠落を検出できない)。"""
    repo = _FakeWatchlistRepository([])
    _seed(repo, [_batch_item(), _item("9001", 30.0), _item("9002", 31.0)])

    def _never(*_a: Any, **_kw: Any) -> list[WatchlistItem]:
        return []

    monkeypatch.setattr(finalizer_module, "_evict_over_capacity", _never)
    _run("cap-unwired", repo, monkeypatch, cap=1)

    # 呼び出しが無効化されると、上限(1)を超えていても淘汰されない = 本テスト群の他の
    # テストが「配線されている」ことの対偶を確認する
    assert repo.codes == {_BATCH_CODE, "9001", "9002"}
    assert _batch_output(audit_dir, "cap-unwired")["capacity_over_count_after_eviction"] == 2
