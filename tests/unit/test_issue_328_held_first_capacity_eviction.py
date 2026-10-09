"""Issue #328(O-4): 総件数上限の淘汰で、保有済みの AUTO_SCREENING を超過分の範囲内で先に外す。

USER 決定 Q-1 = OPTION_A(#328 issuecomment-6069975679):
    EVICTION_SCOPE = OVERFLOW_ONLY / PRIORITIZE_HELD_AUTO = YES / EVICT_WITHOUT_OVERFLOW = NO /
    MANUAL_EVICTION = FORBIDDEN。既存の #324 の容量制御に組み込む。
方式 B1(MANAGER 判断 …6070075262):
    finalize(Worker / TerminalFailureHandler / Reconciler / Dispatcher の4関数から呼ばれる)は保有
    テーブルを読めない。保有を読めるのは Dispatcher だけなので、maintenance の dispatch 時に
    『保有 ∩ 対象(AUTO_SCREENING)』の銘柄コードを batch 行へ記録し、finalize がそれを読む。

守ること(MANAGER の条件):
    1 batch 行へは銘柄コードのみ。ログ・監査へは一覧を出さない(件数のみ。batch-status は件数表示)
    2 淘汰の直前の再確認(MANUAL・登録元・ACTIVE)は維持。保有は dispatch 時点のスナップショット
    3 overflow = 0 なら何も淘汰しない / 属性の無い batch 行は従来の順位と同一
本テストは実データ・実銘柄名を使わない(架空の銘柄コードと名称のみ)。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from jstock_advisor.cli import watchlist_screening as cli_module
from jstock_advisor.domain.entities.watchlist import WatchlistItem
from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.infrastructure.aws.batch_tracker import WatchlistProgressStatus
from jstock_advisor.lambda_handlers import watchlist_dispatcher_handler as dispatcher_module
from jstock_advisor.services import watchlist_batch_finalizer as finalizer_module
from jstock_advisor.services.watchlist_batch_finalizer import (
    _count_held_priority_candidates,
    _evict_over_capacity,
    _held_codes_from_batch_item,
    maybe_finalize_maintenance,
)
from jstock_advisor.services.watchlist_maintenance_service import MaintenanceScreeningSummary
from tests.unit import test_issue_324_total_count_cap as t324
from tests.unit.test_issue_324_total_count_cap import (
    _BATCH_CODE,
    _MANUAL,
    _NOW,
    _batch_item,
    _batch_output,
    _codes,
    _config,
    _FakeWatchlistRepository,
    _item,
    _seed,
)

# fixture は #324 のテストの定義をそのまま再利用する(moto の表・監査の保存先・display name)。
# 別名への代入で登録する(import だけだと、引数名と同名の再定義として lint に警告される)。
dynamo = t324.dynamo
audit_dir = t324.audit_dir
history_repo = t324.history_repo
_stub_display_name_resolver = t324._stub_display_name_resolver

_runner = CliRunner()


# --- 純粋関数 _evict_over_capacity(held_codes)----------------------------------


class TestEvictOverCapacityHeldFirst:
    def test_no_overflow_evicts_nothing_even_when_a_held_item_exists(self) -> None:
        """EVICT_WITHOUT_OVERFLOW = NO: 超過が無ければ、保有済みでも外さない。"""
        items = [_item("1000", 50.0), _item("1001", 10.0)]

        assert _evict_over_capacity(items, 2, frozenset({"1000"})) == []
        assert _evict_over_capacity(items, 5, frozenset({"1000", "1001"})) == []

    def test_one_overflow_takes_the_held_item_instead_of_the_lowest_score(self) -> None:
        items = [_item("1000", 90.0), _item("1001", 10.0), _item("1002", 20.0)]

        # 従来: 最低スコアの 1001。保有済みなら、スコアが高い 1000 を先に外す
        assert _codes(_evict_over_capacity(items, 2)) == ["1001"]
        assert _codes(_evict_over_capacity(items, 2, frozenset({"1000"}))) == ["1000"]

    def test_the_number_evicted_is_still_exactly_the_overflow(self) -> None:
        items = [_item(f"{2000 + i}", float(i)) for i in range(10)]
        held = frozenset({"2005", "2009"})

        victims = _evict_over_capacity(items, 6, held)  # overflow = 4

        assert len(victims) == 4
        assert {"2005", "2009"} <= set(_codes(victims))  # 保有済みが先
        assert _codes(victims)[:2] == ["2005", "2009"]  # 保有済みの中はスコア昇順
        assert _codes(victims)[2:] == ["2000", "2001"]  # 残りは従来の順(スコア昇順)

    def test_more_held_items_than_the_overflow_evicts_only_the_overflow_lowest_held(self) -> None:
        items = [_item("3000", 80.0), _item("3001", 30.0), _item("3002", 60.0), _item("3003", 5.0)]
        held = frozenset({"3000", "3001", "3002"})

        victims = _evict_over_capacity(items, 3, held)  # overflow = 1

        assert _codes(victims) == ["3001"]  # 保有済みの中で最低スコア(3003 は保有でないため後)

    def test_tie_break_among_held_is_score_then_created_at(self) -> None:
        old = _item("4000", 50.0, age_days=1)
        new = _item("4001", 50.0, age_days=2)
        low = _item("4002", 40.0, age_days=9)

        victims = _evict_over_capacity([new, old, low], 0, frozenset({"4000", "4001", "4002"}))

        assert _codes(victims) == ["4002", "4000", "4001"]

    def test_a_held_code_that_is_not_in_the_watchlist_has_no_effect(self) -> None:
        items = [_item("5000", 30.0), _item("5001", 10.0)]

        assert _codes(_evict_over_capacity(items, 1, frozenset({"9999"}))) == ["5001"]

    def test_manual_is_never_evicted_even_when_held(self) -> None:
        """MANUAL_EVICTION = FORBIDDEN。"""
        manual = _item("6000", 0.0, source=_MANUAL)
        auto = _item("6001", 90.0)

        victims = _evict_over_capacity([manual, auto], 1, frozenset({"6000"}))

        assert _codes(victims) == ["6001"]

    def test_legacy_item_without_registration_source_is_not_evicted_even_when_held(self) -> None:
        legacy = _item("6100", 0.0)
        legacy = WatchlistItem.model_validate(
            {
                k: v
                for k, v in legacy.model_dump().items()
                if k not in {"registration_source", "registration_policy"}
            }
        )
        auto = _item("6101", 90.0)

        victims = _evict_over_capacity([legacy, auto], 1, frozenset({"6100"}))

        assert _codes(victims) == ["6101"]

    def test_not_evaluable_is_never_selected_even_when_held(self) -> None:
        not_evaluable = _item("6200", 0.0, result="NOT_EVALUABLE")
        a = _item("6201", 50.0)
        b = _item("6202", 60.0)

        # ACTIVE は 2 件(6201・6202)。cap = 1 → overflow = 1。6200 は数えず・外さない
        victims = _evict_over_capacity([not_evaluable, a, b], 1, frozenset({"6200", "6202"}))

        assert _codes(victims) == ["6202"]

    def test_empty_held_codes_is_identical_to_the_previous_behaviour(self) -> None:
        rng = random.Random(328)
        items = [_item(f"{7000 + i}", float(rng.randint(0, 99)), age_days=i) for i in range(40)]

        for cap in (0, 10, 25, 39, 40):
            assert _evict_over_capacity(items, cap) == _evict_over_capacity(items, cap, frozenset())

    def test_result_does_not_depend_on_the_input_order_and_does_not_mutate_it(self) -> None:
        items = [_item(f"{8000 + i}", float(i % 7), age_days=i) for i in range(12)]
        held = frozenset({"8003", "8008", "8011"})
        snapshot = list(items)

        expected = _codes(_evict_over_capacity(items, 6, held))
        shuffled = list(items)
        random.Random(1).shuffle(shuffled)

        assert _codes(_evict_over_capacity(shuffled, 6, held)) == expected
        assert items == snapshot


class TestHeldCodesFromBatchItem:
    def test_missing_attribute_means_no_priority(self) -> None:
        """従来の batch 行(属性なし)= 従来の淘汰順位。"""
        assert _held_codes_from_batch_item({"batch_id": "b"}) == frozenset()

    def test_null_means_no_priority(self) -> None:
        """dispatcher が保有を読めなかった場合(None)。"""
        assert _held_codes_from_batch_item({"held_stock_codes": None}) == frozenset()

    def test_list_is_returned_as_a_set(self) -> None:
        assert _held_codes_from_batch_item({"held_stock_codes": ["1000", "1001", "1000"]}) == {
            "1000",
            "1001",
        }

    @pytest.mark.parametrize("bad", ["1000", 1000, {"1000": 1}, True])
    def test_a_non_list_value_is_ignored(self, bad: object) -> None:
        assert _held_codes_from_batch_item({"held_stock_codes": bad}) == frozenset()

    def test_non_string_and_empty_elements_are_dropped(self) -> None:
        assert _held_codes_from_batch_item({"held_stock_codes": ["1000", 7, None, ""]}) == {"1000"}


class TestCountHeldPriorityCandidates:
    def test_counts_only_active_auto_items_that_are_held(self) -> None:
        items = [
            _item("1000", 10.0),  # AUTO・ACTIVE・保有 -> 数える
            _item("1001", 10.0),  # AUTO・ACTIVE・保有でない
            _item("1002", 10.0, source=_MANUAL),  # 保有だが MANUAL
            _item("1003", 10.0, result="NOT_EVALUABLE"),  # 保有だが NOT_EVALUABLE
        ]

        assert (
            _count_held_priority_candidates(items, frozenset({"1000", "1002", "1003", "9999"})) == 1
        )

    def test_empty_held_codes_counts_zero(self) -> None:
        assert _count_held_priority_candidates([_item("1000", 10.0)], frozenset()) == 0


# --- 統合: maintenance finalize ----------------------------------------------------

# 保有を表す架空の銘柄コード(監査・ログへ出ないことの確認に使うマーカー)。
_HELD_A = "7771"
_HELD_B = "7772"


def _drive_maintenance_batch_with_held(
    batch_id: str,
    now: dt.datetime,
    held_stock_codes: list[str] | None | object,
) -> None:
    """test_issue_324 の _drive_maintenance_batch と同じ。set_watchlist_batch_total だけ
    held_stock_codes を渡す(`_UNSET` なら引数を渡さない = 従来の呼び出し)。"""
    kwargs: dict[str, Any] = {}
    if held_stock_codes is not _UNSET:
        kwargs["held_stock_codes"] = held_stock_codes
    batch_tracker.try_acquire_dispatch_lease(batch_id, "dispatcher", now, 360, 72)
    batch_tracker.set_watchlist_batch_total(
        batch_id,
        1,
        72,
        now,
        job_type=batch_tracker.WatchlistJobType.WATCHLIST_MAINTENANCE,
        **kwargs,
    )
    batch_tracker.create_missing_candidate_progress_rows(batch_id, [_BATCH_CODE], now, 72)
    batch_tracker.mark_dispatch_completed(batch_id, now)
    batch_tracker.claim_candidate_lease(batch_id, _BATCH_CODE, "owner-a", now, 240)
    summary = MaintenanceScreeningSummary(
        passed=True,
        total_score=95.0,
        matched_target_types=[],
        hard_exclusion_reasons=[],
        policy_name="multi_style_monitoring",
    )
    batch_tracker.complete_candidate(
        batch_id,
        _BATCH_CODE,
        "owner-a",
        terminal_status=WatchlistProgressStatus.COMPLETED,
        evaluation_result="PASSED",
        ranking_entry=None,
        is_provider_failure_suspected=False,
        missing_field_names=[],
        processing_duration_ms=100,
        now=now,
        screening_summary_json=summary.model_dump_json(),
    )


_UNSET = object()


def _run_finalize(
    batch_id: str,
    repo: _FakeWatchlistRepository,
    monkeypatch: pytest.MonkeyPatch,
    cap: int,
    held_stock_codes: list[str] | None | object,
) -> None:
    monkeypatch.setattr(finalizer_module, "WatchlistRepository", lambda: repo)
    _drive_maintenance_batch_with_held(batch_id, _NOW, held_stock_codes)
    assert maybe_finalize_maintenance(batch_id, _NOW, _config(cap)) is True


def _seed_watchlist(repo: _FakeWatchlistRepository) -> None:
    # 評価されるのは _BATCH_CODE(評価後のスコア 95)。他は評価されず、スコアは保存値のまま。
    # ACTIVE は 5 件。cap = 4 なら overflow = 1(従来の淘汰対象 = 最低スコアの 9002)
    _seed(
        repo,
        [
            _batch_item(),
            _item("9001", 30.0),
            _item("9002", 10.0),
            _item("9003", 20.0),
            _item(_HELD_A, 80.0),
        ],
    )


def test_a_held_item_is_evicted_first_within_the_overflow(
    dynamo,
    audit_dir: Path,
    history_repo,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = _FakeWatchlistRepository([])
    _seed_watchlist(repo)

    with caplog.at_level(logging.INFO):
        _run_finalize("held-first", repo, monkeypatch, cap=4, held_stock_codes=[_HELD_A])

    # 従来なら 9002(最低スコア)が外れる。保有済みの _HELD_A(スコア 80)が代わりに外れる
    assert repo.codes == {_BATCH_CODE, "9001", "9002", "9003"}
    output = _batch_output(audit_dir, "held-first")
    assert output["capacity_evicted_count"] == 1
    assert output["capacity_over_count_after_eviction"] == 0
    assert output["capacity_held_priority_candidate_count"] == 1
    assert output["capacity_held_priority_evicted_count"] == 1
    # 銘柄コードの一覧は、バッチの監査にもバッチ単位のログにも出ない
    assert _HELD_A not in json.dumps(output, ensure_ascii=False)
    finalized_lines = [r.getMessage() for r in caplog.records if "finalized" in r.getMessage()]
    assert finalized_lines
    assert all(_HELD_A not in line for line in finalized_lines)
    assert any("capacity_held_priority_evicted=1" in line for line in finalized_lines)


def test_without_overflow_a_held_item_is_not_evicted(
    dynamo, audit_dir: Path, history_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _FakeWatchlistRepository([])
    _seed_watchlist(repo)

    _run_finalize("held-no-overflow", repo, monkeypatch, cap=5, held_stock_codes=[_HELD_A])

    assert repo.codes == {_BATCH_CODE, "9001", "9002", "9003", _HELD_A}
    output = _batch_output(audit_dir, "held-no-overflow")
    assert output["capacity_evicted_count"] == 0
    assert output["capacity_held_priority_candidate_count"] == 1
    assert output["capacity_held_priority_evicted_count"] == 0


@pytest.mark.parametrize(
    "held_stock_codes",
    [pytest.param(_UNSET, id="attribute_not_passed"), pytest.param(None, id="null"), []],
)
def test_a_batch_row_without_held_codes_keeps_the_previous_eviction_order(
    dynamo,
    audit_dir: Path,
    history_repo,
    monkeypatch: pytest.MonkeyPatch,
    held_stock_codes: list[str] | None | object,
) -> None:
    repo = _FakeWatchlistRepository([])
    _seed_watchlist(repo)

    _run_finalize("legacy-row", repo, monkeypatch, cap=4, held_stock_codes=held_stock_codes)

    # 従来どおり最低スコアの 9002 が外れる(保有済みの _HELD_A は残る)
    assert repo.codes == {_BATCH_CODE, "9001", "9003", _HELD_A}
    output = _batch_output(audit_dir, "legacy-row")
    assert output["capacity_held_priority_candidate_count"] == 0
    assert output["capacity_held_priority_evicted_count"] == 0


def test_a_held_item_re_registered_as_manual_after_the_scan_is_not_evicted(
    dynamo, audit_dir: Path, history_repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """条件 2: 淘汰の直前の再確認は維持される(dispatch 時点のスナップショットの限界)。"""
    repo = _FakeWatchlistRepository([])
    _seed_watchlist(repo)
    # 走査時点では AUTO だが、淘汰の直前には MANUAL に変わっていた
    repo.get_override[_HELD_A] = _item(_HELD_A, 80.0, source=_MANUAL)

    _run_finalize("held-recheck", repo, monkeypatch, cap=4, held_stock_codes=[_HELD_A])

    assert _HELD_A in repo.codes  # 外されない
    output = _batch_output(audit_dir, "held-recheck")
    assert output["capacity_evicted_count"] == 0
    assert output["capacity_eviction_skipped_count"] == 1
    assert output["capacity_held_priority_evicted_count"] == 0


def test_held_codes_are_read_from_the_batch_row_by_every_caller_of_the_finalize() -> None:
    """finalize を呼ぶ 4 関数(Dispatcher / Worker / TerminalFailureHandler / Reconciler)は、
    すべて同じ maybe_finalize_maintenance を呼ぶ = batch 行の属性を読む経路が 1 つだけ。
    (保有を読めない 3 関数でも、Dispatcher が batch 行へ記録した値で同じ結果になる)"""
    handlers = (
        "watchlist_dispatcher_handler",
        "watchlist_worker_handler",
        "watchlist_terminal_failure_handler",
        "watchlist_batch_reconciler_handler",
    )
    root = Path(__file__).resolve().parents[2] / "src" / "jstock_advisor" / "lambda_handlers"
    for name in handlers:
        source = (root / f"{name}.py").read_text(encoding="utf-8")
        assert "maybe_finalize_maintenance(" in source, name
    finalizer_source = (root.parent / "services" / "watchlist_batch_finalizer.py").read_text(
        encoding="utf-8"
    )
    assert "_held_codes_from_batch_item(maintenance_batch_item)" in finalizer_source


# --- batch_tracker.set_watchlist_batch_total -------------------------------------


def test_set_watchlist_batch_total_stores_held_codes_as_a_list_of_codes(dynamo) -> None:
    batch_tracker.try_acquire_dispatch_lease("b-held", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total(
        "b-held",
        3,
        72,
        _NOW,
        job_type=batch_tracker.WatchlistJobType.WATCHLIST_MAINTENANCE,
        held_stock_codes=[_HELD_A, _HELD_B],
    )

    stored = batch_tracker.get_watchlist_batch("b-held")

    assert stored is not None
    assert stored["held_stock_codes"] == [_HELD_A, _HELD_B]
    assert stored["total"] == 3  # 既存の引数の挙動は不変


def test_set_watchlist_batch_total_without_held_codes_stores_null(dynamo) -> None:
    batch_tracker.try_acquire_dispatch_lease("b-none", "dispatcher", _NOW, 360, 72)
    batch_tracker.set_watchlist_batch_total("b-none", 2, 72, _NOW)

    stored = batch_tracker.get_watchlist_batch("b-none")

    assert stored is not None
    assert stored["held_stock_codes"] is None
    assert _held_codes_from_batch_item(stored) == frozenset()


# --- dispatcher: _collect_maintenance_targets -------------------------------------


class _FakeHoldingRepository:
    def __init__(self, codes: list[str]) -> None:
        self._codes = codes

    def list_all(self) -> list[Any]:
        return [SimpleNamespace(stock_code=c) for c in self._codes]


class _RaisingHoldingRepository:
    def list_all(self) -> list[Any]:
        raise RuntimeError("holdings unavailable")


def _patch_dispatcher(
    monkeypatch: pytest.MonkeyPatch, watchlist: list[WatchlistItem], holdings: Any
) -> None:
    monkeypatch.setattr(
        dispatcher_module,
        "WatchlistRepository",
        lambda: SimpleNamespace(list_all=lambda: watchlist),
    )
    monkeypatch.setattr(dispatcher_module, "HoldingRepository", lambda: holdings)


def test_dispatcher_records_the_held_codes_among_the_auto_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watchlist = [
        _item("1000", 10.0),
        _item("1001", 20.0),
        _item("1002", 30.0, source=_MANUAL),  # 保有でも MANUAL は対象(codes)に入らない
    ]
    _patch_dispatcher(monkeypatch, watchlist, _FakeHoldingRepository(["1001", "1002", "9999"]))

    codes, extra = dispatcher_module._collect_maintenance_targets({})

    assert codes == ["1000", "1001"]
    # 保有 ∩ 対象(AUTO)のみ。MANUAL の保有(1002)・監視リストに無い保有(9999)は入らない
    assert extra["held_stock_codes"] == ["1001"]


def test_dispatcher_records_an_empty_list_when_nothing_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dispatcher(monkeypatch, [_item("1000", 10.0)], _FakeHoldingRepository([]))

    _codes_out, extra = dispatcher_module._collect_maintenance_targets({})

    assert extra["held_stock_codes"] == []


def test_dispatcher_continues_without_priority_when_holdings_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_dispatcher(monkeypatch, [_item("1000", 10.0)], _RaisingHoldingRepository())

    with caplog.at_level(logging.WARNING):
        codes, extra = dispatcher_module._collect_maintenance_targets({})

    assert codes == ["1000"]  # maintenance 自体は続行する
    assert extra["held_stock_codes"] is None
    warning = " ".join(r.getMessage() for r in caplog.records)
    assert "held stock codes unavailable" in warning
    assert "RuntimeError" in warning
    assert "1000" not in warning  # 銘柄コードをログへ出さない


def test_dispatcher_keeps_passing_the_parent_batch_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_dispatcher(monkeypatch, [_item("1000", 10.0)], _FakeHoldingRepository([]))

    _codes_out, extra = dispatcher_module._collect_maintenance_targets(
        {"triggered_by_batch_id": "parent-1", "trigger_type": "POST_NEW_CANDIDATE_SCREENING"}
    )

    assert extra["triggered_by_batch_id"] == "parent-1"
    assert extra["trigger_type"] == "POST_NEW_CANDIDATE_SCREENING"


def test_held_codes_are_not_put_into_the_audit_output() -> None:
    extra = {"triggered_by_batch_id": "p", "trigger_type": "T", "held_stock_codes": [_HELD_A]}

    safe = dispatcher_module._audit_safe_extra_kwargs(extra)

    assert "held_stock_codes" not in safe
    assert safe == {"triggered_by_batch_id": "p", "trigger_type": "T"}
    assert _HELD_A not in json.dumps(safe)
    assert extra["held_stock_codes"] == [_HELD_A]  # 元の dict は変えない(batch 行へは渡る)


# --- CLI batch-status --------------------------------------------------------------


def test_batch_status_shows_the_held_codes_as_a_count_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "get_watchlist_batch",
        lambda _batch_id: {
            "batch_id": "b",
            "status": "RUNNING",
            "held_stock_codes": [_HELD_A, _HELD_B],
        },
    )

    result = _runner.invoke(cli_module.app, ["batch-status", "b"])

    assert result.exit_code == 0, result.output
    assert "held_stock_codes: 2件" in result.output
    assert _HELD_A not in result.output
    assert _HELD_B not in result.output
    assert "status: RUNNING" in result.output  # 他のキーは従来どおり表示される


def test_batch_status_shows_null_held_codes_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli_module,
        "get_watchlist_batch",
        lambda _batch_id: {"batch_id": "b", "held_stock_codes": None},
    )

    result = _runner.invoke(cli_module.app, ["batch-status", "b"])

    assert result.exit_code == 0, result.output
    assert "held_stock_codes: None" in result.output
