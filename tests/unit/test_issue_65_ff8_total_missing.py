"""Issue #65 F-F8: batch item の `total` 欠落を 0 とみなさない。

`record_result()` と `_build_batch_progress()` は `int(item["total"])` の
**直接添字**で total を読んでいた（同じ構築式の他 11〜13 項目はすべて
`item.get(...)` で防御されており非対称）。TTL 経過後の遅延 retry 等で
batch item が無い状態で呼ぶと **KeyError** で worker が異常終了する。

★ しかし `.get(x, 0)` で埋めるのは**別の欠陥を作る**。
  `BatchProgress.is_complete` は `completed >= total` / `len(completed_codes) >= total`
  であり、total を 0 にすると **常に真** = **即座に finalize 可能**になる。
  さらに `buy_candidates_handler` の
  `if saved_count == progress.total:` が `saved_count == 0` の回に成立し、
  **不完全なバッチで最新バッチポインタが前進**する。

-> 「欠落 = 不明」を `total_known` で表し、**total と数を比べる判定では
   必ず fail-closed へ倒す**（finalize しない／ポインタを進めない）。

★ 実在の銘柄コード・銘柄名は使用しない（"0000" 等の架空値のみ）。
★ Production への failure injection は行わない。
"""

from __future__ import annotations

import logging

import pytest

from jstock_advisor.infrastructure.aws.batch_tracker import (
    BatchProgress,
    _build_batch_progress,
)


def _progress(**overrides) -> BatchProgress:
    base = {
        "total": 3,
        "completed": 0,
        "category_counts": {},
        "data_insufficient_stock_codes": [],
        "failed_stock_codes": [],
        "ranking_entries": [],
        "sector_entries": [],
        "holding_count": 0,
    }
    base.update(overrides)
    return BatchProgress(**base)


def _item(**overrides) -> dict:
    base = {"batch_id": "b-ff8", "total": 3, "completed": 3}
    base.update(overrides)
    return base


# --- T-1 / T-5: KeyError にならない -----------------------------------------------------


def test_missing_total_does_not_raise(caplog: pytest.LogCaptureFixture) -> None:
    """T-1: `total` が無くても KeyError にならないこと。"""
    item = _item()
    del item["total"]
    with caplog.at_level(logging.WARNING):
        progress = _build_batch_progress(item)
    assert progress.total_known is False
    assert "total is unknown" in caplog.text


def test_missing_completed_does_not_raise() -> None:
    """T-5: `completed` が無くても KeyError にならないこと。"""
    item = _item()
    del item["completed"]
    progress = _build_batch_progress(item)
    assert progress.completed == 0
    # ★ total はあるので既知のまま（片方の欠落で両方を不明にしない）。
    assert progress.total_known is True


# --- T-2 / T-7: finalize は fail-closed -------------------------------------------------


def test_unknown_total_never_reports_complete() -> None:
    """T-2: ★ total 不明なら `is_complete` は **False**（= finalize しない）。

    0 で埋めると `completed >= 0` が常に真になり、**未処理銘柄を残したまま
    即座に finalize** できてしまう。元の欠陥（KeyError）より重い事故になる。
    """
    progress = _progress(total=0, total_known=False, completed=0)
    assert progress.is_complete is False


def test_unknown_total_is_fail_closed_for_the_completion_id_branch() -> None:
    """T-7: `completed_codes` を持つ新形式でも、total 不明なら False。

    `is_complete` は二分岐（件数カウンタ / 完了 ID 集合）を持つため、
    **両方**が守られていることを固定する。
    """
    progress = _progress(
        total=0,
        total_known=False,
        completed=0,
        completed_codes=["0000", "0001"],
    )
    assert progress.has_completion_ids is True
    assert progress.is_complete is False


@pytest.mark.parametrize(
    ("completed", "total", "expected"),
    [(2, 3, False), (3, 3, True), (4, 3, True)],
)
def test_known_total_keeps_the_existing_boundary(
    completed: int, total: int, expected: bool
) -> None:
    """T-6 / DoD 1: total が既知なら従来どおり（`completed == total` ちょうどで真）。"""
    assert _progress(total=total, completed=completed).is_complete is expected


def test_total_known_defaults_to_true_so_existing_construction_is_unchanged() -> None:
    """★ 既定 True。既存の構築箇所を 1 つも書き換えずに済むことを固定する。"""
    assert _progress().total_known is True
    assert BatchProgress.__dataclass_fields__["total_known"].default is True


# --- T-3: ポインタ前進も fail-closed ----------------------------------------------------


def test_the_latest_batch_pointer_is_guarded_by_total_known() -> None:
    """T-3: ★ total 不明のとき、最新バッチポインタを**前進させない**こと。

    `is_complete` だけを fail-closed にしても、この経路は塞がらない
    （`saved_count == 0` と `total == 0` が一致してしまう）。
    ここが繋がっていないと「KeyError で落ちる」から
    「静かに不完全なバッチでポインタが進む」へ**失敗の形が変わるだけ**になる。
    """
    from pathlib import Path

    source = Path("src/jstock_advisor/lambda_handlers/buy_candidates_handler.py").read_text(
        encoding="utf-8"
    )
    assert "if progress.total_known and saved_count == progress.total:" in source
    # ★ 素の比較が残っていないこと（ガードを足したつもりで別経路が残る事故を防ぐ）。
    assert "if saved_count == progress.total:" not in source


def test_unknown_total_would_otherwise_match_a_zero_saved_count() -> None:
    """★ 上のガードが**なぜ必要か**を数値で固定する。

    total を 0 と偽ると `saved_count == 0` の回に一致してしまう、という
    関係そのものをテストに残す（ガードを外した人がここで気づけるように）。
    """
    saved_count = 0
    unknown = _progress(total=0, total_known=False, completed=0)
    assert saved_count == unknown.total  # ★ 数としては一致してしまう
    assert unknown.total_known is False  # ★ だからフラグで守る


# --- T-4: 失敗の可視性（DoD 5） ---------------------------------------------------------


def test_unknown_total_is_warned_not_silently_accepted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T-4: 不明を検出したら WARNING を出すこと（黙って進めない）。"""
    item = _item()
    del item["total"]
    with caplog.at_level(logging.WARNING):
        _build_batch_progress(item)
    assert any("total is unknown" in r.getMessage() for r in caplog.records)


def test_known_total_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    """T-6: 通常時は WARNING を出さないこと（ノイズを増やさない）。"""
    with caplog.at_level(logging.WARNING):
        progress = _build_batch_progress(_item())
    assert progress.total == 3
    assert progress.total_known is True
    assert "total is unknown" not in caplog.text
