"""永続ラウンドロビン方式(計画Part A-4)の状態管理モジュールの単体テスト。

`running_on_lambda()`がFalse(既定のテスト実行環境)の場合、`_commit_local`
(JsonCollectionStore経由)が使われる。`store_dir`をtmp_pathへ束縛することで、
実データディレクトリ(data/local_store/)を一切汚染しない。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from jstock_advisor.infrastructure.aws.watchlist_rotation_state import (
    create_rotation_state_if_absent,
    get_rotation_state,
    try_commit_rotation_advance,
)

_NOW = dt.datetime(2026, 8, 1, 7, 0, tzinfo=dt.UTC)


def test_get_rotation_state_returns_none_when_not_created(tmp_path: Path) -> None:
    assert get_rotation_state(store_dir=tmp_path) is None


def test_create_rotation_state_if_absent_starts_from_head(tmp_path: Path) -> None:
    state = create_rotation_state_if_absent(_NOW, store_dir=tmp_path)
    assert state.pointer_version == 1
    assert state.cycle_number == 1
    assert state.last_stock_code is None
    assert state.last_market_segment is None
    assert state.cycle_progress_selected_count == 0


def test_create_rotation_state_if_absent_is_idempotent(tmp_path: Path) -> None:
    first = create_rotation_state_if_absent(_NOW, store_dir=tmp_path)
    later = _NOW + dt.timedelta(days=7)
    second = create_rotation_state_if_absent(later, store_dir=tmp_path)
    assert second.pointer_version == first.pointer_version
    assert second.last_started_at == first.last_started_at  # 2回目の呼び出しでリセットされない


def test_try_commit_rotation_advance_success_without_wrap(tmp_path: Path) -> None:
    state = create_rotation_state_if_absent(_NOW, store_dir=tmp_path)
    committed = try_commit_rotation_advance(
        state.pointer_version,
        "Prime",
        "0300",
        wrapped=False,
        selected_count=300,
        now=_NOW,
        store_dir=tmp_path,
    )
    assert committed is True

    updated = get_rotation_state(store_dir=tmp_path)
    assert updated is not None
    assert updated.pointer_version == state.pointer_version + 1
    assert updated.last_market_segment == "Prime"
    assert updated.last_stock_code == "0300"
    assert updated.cycle_number == 1  # wrapped=Falseのため据え置き
    assert updated.cycle_progress_selected_count == 300


def test_try_commit_rotation_advance_wrap_increments_cycle_and_resets_progress(
    tmp_path: Path,
) -> None:
    state = create_rotation_state_if_absent(_NOW, store_dir=tmp_path)
    try_commit_rotation_advance(
        state.pointer_version,
        "Prime",
        "0300",
        wrapped=False,
        selected_count=300,
        now=_NOW,
        store_dir=tmp_path,
    )
    after_first = get_rotation_state(store_dir=tmp_path)
    assert after_first is not None

    later = _NOW + dt.timedelta(days=7)
    committed = try_commit_rotation_advance(
        after_first.pointer_version,
        "Prime",
        "0050",
        wrapped=True,
        selected_count=120,
        now=later,
        store_dir=tmp_path,
    )
    assert committed is True

    after_wrap = get_rotation_state(store_dir=tmp_path)
    assert after_wrap is not None
    assert after_wrap.cycle_number == 2
    assert after_wrap.cycle_progress_selected_count == 120  # リセットされ今回選択件数のみ
    assert after_wrap.last_started_at == later


def test_try_commit_rotation_advance_conflict_on_stale_version(tmp_path: Path) -> None:
    state = create_rotation_state_if_absent(_NOW, store_dir=tmp_path)
    first = try_commit_rotation_advance(
        state.pointer_version,
        "Prime",
        "0300",
        wrapped=False,
        selected_count=300,
        now=_NOW,
        store_dir=tmp_path,
    )
    assert first is True

    # 2つのDispatcherが同時に古いpointer_versionでcommitを試みたケースを模擬する。
    second = try_commit_rotation_advance(
        state.pointer_version,
        "Prime",
        "9999",
        wrapped=False,
        selected_count=300,
        now=_NOW,
        store_dir=tmp_path,
    )
    assert second is False

    # 負けた側の値では上書きされない(先勝ちの結果が維持される)。
    unchanged = get_rotation_state(store_dir=tmp_path)
    assert unchanged is not None
    assert unchanged.last_stock_code == "0300"


def test_try_commit_rotation_advance_returns_false_when_state_absent(tmp_path: Path) -> None:
    committed = try_commit_rotation_advance(
        1, "Prime", "0300", wrapped=False, selected_count=300, now=_NOW, store_dir=tmp_path
    )
    assert committed is False
