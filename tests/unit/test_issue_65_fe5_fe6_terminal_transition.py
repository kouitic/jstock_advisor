"""Issue #65 F-E5 / F-E6: dispatcher の中断経路で終端遷移させる。

`try_acquire_dispatch_lease()` が **status=DISPATCHING のレコードを作成済み**
なのに、2 つの中断経路（universe 取得失敗 / 候補 0 件）は監査を書いて return
するだけで**終端遷移しませんでした**（F-E5）。数時間後に毎時 reconciler が
timeout と判定し **偽の DISPATCH_FAILED 警告**を出します。候補 0 件の日は毎回です。

`create_missing_candidate_progress_rows()` は docstring で「RuntimeError を送出し、
呼び出し側が DISPATCH_FAILED へ遷移できるようにする」と約束しているのに、
**受け手が居ませんでした**（F-E6）。

★ 3 つの経路は「同じ終端」ではありません。
    候補 0 件           = **失敗ではない**（正常に走り、やることが無かっただけ）
                          -> COMPLETED（execution_result = NORMAL）
    universe 取得失敗   = **失敗** -> DISPATCH_FAILED
    進捗行の作成失敗    = **失敗** -> DISPATCH_FAILED
  3 つを同じ状態へ潰すと「該当しない / 失敗した」を同じ値にすることになります。

★ 実在の銘柄コード・銘柄名は使用しない（"0000" 等の架空値のみ）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jstock_advisor.infrastructure.aws.batch_tracker import EXECUTION_RESULT_NORMAL
from jstock_advisor.interfaces.candidate_universe import CandidateUniverseError
from jstock_advisor.lambda_handlers import watchlist_dispatcher_handler as handler_module


def _config() -> SimpleNamespace:
    watchlist_screening = SimpleNamespace(
        enabled=True,
        scheduled_run_enabled=True,
        candidate_universe=SimpleNamespace(provider="csv"),
        screening_policy="high_dividend_financial_health",
        staged_rollout=SimpleNamespace(candidate_limit=10, market_segment_filter=None),
        batch_record_ttl_hours=72,
        candidate_progress_ttl_hours=72,
        rotation=SimpleNamespace(enabled=True),
        batch_processing_timeout_hours=24,
    )
    return SimpleNamespace(watchlist_screening=watchlist_screening)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """中断経路で呼ばれる副作用を**順序つきで**記録する。"""
    recorded: list[tuple[str, Any]] = []

    monkeypatch.setattr(handler_module, "load_config", _config)
    monkeypatch.setattr(handler_module, "try_acquire_dispatch_lease", lambda *a, **kw: True)
    monkeypatch.setattr(
        handler_module, "try_acquire_rotation_dispatch_lease", lambda *a, **kw: True
    )
    monkeypatch.setattr(
        handler_module,
        "mark_dispatch_failed",
        lambda *a, **kw: recorded.append(("mark_dispatch_failed", a[0])),
    )
    monkeypatch.setattr(
        handler_module,
        "mark_watchlist_batch_completed",
        lambda *a, **kw: recorded.append(("mark_watchlist_batch_completed", a[1])),
    )
    monkeypatch.setattr(
        handler_module,
        "release_rotation_dispatch_lease",
        lambda *a, **kw: recorded.append(("release_rotation_dispatch_lease", None)),
    )
    monkeypatch.setattr(
        handler_module,
        "record_batch_audit",
        lambda **kw: recorded.append(("record_batch_audit", kw["output_values"])),
    )
    return recorded


def _names(calls: list[tuple[str, Any]]) -> list[str]:
    return [name for name, _ in calls]


# --- A: 候補 0 件 -> COMPLETED ---------------------------------------------------------


def test_no_candidates_terminates_as_completed(
    calls: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 候補 0 件は**失敗ではない**ので COMPLETED で終端すること。

    DISPATCH_FAILED にすると「本物の失敗」と区別がつかなくなり、
    偽の警告を別の形で作り直すことになる。
    """
    monkeypatch.setattr(
        handler_module, "_collect_new_candidate_targets", lambda *a, **kw: ([], {})
    )

    result = handler_module.handler({}, object())

    assert result == {"dispatched": 0}
    assert ("mark_watchlist_batch_completed", EXECUTION_RESULT_NORMAL) in calls
    # ★ 失敗としては終端させない。
    assert "mark_dispatch_failed" not in _names(calls)


def test_no_candidates_keeps_the_reason_in_the_audit(
    calls: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ status を COMPLETED にしても「0 件だった」理由は失われないこと。

    status はライフサイクルのみを表し、終了理由は execution_result 属性で
    区別する（20 節）。監査側の "no_candidates" がその役目を担う。
    """
    monkeypatch.setattr(
        handler_module, "_collect_new_candidate_targets", lambda *a, **kw: ([], {})
    )

    handler_module.handler({}, object())

    audits = [payload for name, payload in calls if name == "record_batch_audit"]
    assert len(audits) == 1
    assert audits[0]["execution_result"] == "no_candidates"


def test_no_candidates_terminates_before_releasing_the_rotation_lease(
    calls: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 終端遷移が lease 解放**より前**であること。

    逆にすると、解放後・遷移前に異常終了した場合に
    「lease は空いているのに batch は DISPATCHING」という、
    いま直そうとしている状態が**別の形で残る**。
    """
    monkeypatch.setattr(
        handler_module, "_collect_new_candidate_targets", lambda *a, **kw: ([], {})
    )

    handler_module.handler({}, object())

    names = _names(calls)
    assert names.index("mark_watchlist_batch_completed") < names.index(
        "release_rotation_dispatch_lease"
    )


# --- B: universe 取得失敗 -> DISPATCH_FAILED --------------------------------------------


def _raise_universe_error(*args: Any, **kwargs: Any):
    raise CandidateUniverseError("東証上場銘柄一覧のキャッシュが存在しません")


def test_universe_load_failure_terminates_as_dispatch_failed(
    calls: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 母集団が作れていないので**失敗**として終端すること。"""
    monkeypatch.setattr(
        handler_module, "_collect_new_candidate_targets", _raise_universe_error
    )

    result = handler_module.handler({}, object())

    assert result == {"error": "universe_load_failed"}
    assert "mark_dispatch_failed" in _names(calls)
    # ★ 正常完了としては終端させない。
    assert "mark_watchlist_batch_completed" not in _names(calls)


def test_universe_load_failure_terminates_before_releasing_the_rotation_lease(
    calls: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        handler_module, "_collect_new_candidate_targets", _raise_universe_error
    )

    handler_module.handler({}, object())

    names = _names(calls)
    assert names.index("mark_dispatch_failed") < names.index("release_rotation_dispatch_lease")


# --- C: 進捗行の作成失敗 -> DISPATCH_FAILED（F-E6） -------------------------------------


def _raise_runtime_error(*args: Any, **kwargs: Any):
    raise RuntimeError("progress rows partially created")


def _raise_client_error(*args: Any, **kwargs: Any):
    raise ValueError("throttled")  # RuntimeError 以外の代表として


@pytest.mark.parametrize("boom", [_raise_runtime_error, _raise_client_error])
def test_progress_row_creation_failure_terminates_as_dispatch_failed(
    calls: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch, boom: Any
) -> None:
    """★ F-E6: docstring の約束どおり DISPATCH_FAILED へ遷移すること。

    RuntimeError 以外（DynamoDB のスロットリング等）も同じ扱いにする。
    呼び出し側から見れば「進捗行が作れなかった」ことに変わりはなく、
    DISPATCHING のまま lease を保持し続ける方が有害なため。
    """
    monkeypatch.setattr(
        handler_module, "_collect_new_candidate_targets", lambda *a, **kw: (["0000"], {})
    )
    monkeypatch.setattr(handler_module, "set_watchlist_batch_total", lambda *a, **kw: None)
    monkeypatch.setattr(handler_module, "create_missing_candidate_progress_rows", boom)

    result = handler_module.handler({}, object())

    assert result == {"error": "progress_row_creation_failed"}
    names = _names(calls)
    assert "mark_dispatch_failed" in names
    # ★ lease が解放されること（保持し続けない）と、その前に遷移していること。
    assert "release_rotation_dispatch_lease" in names
    assert names.index("mark_dispatch_failed") < names.index("release_rotation_dispatch_lease")


def test_progress_row_creation_failure_is_recorded_in_the_audit(
    calls: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """失敗の可視性: 何で落ちたのかが監査に残ること。"""
    monkeypatch.setattr(
        handler_module, "_collect_new_candidate_targets", lambda *a, **kw: (["0000"], {})
    )
    monkeypatch.setattr(handler_module, "set_watchlist_batch_total", lambda *a, **kw: None)
    monkeypatch.setattr(
        handler_module, "create_missing_candidate_progress_rows", _raise_runtime_error
    )

    handler_module.handler({}, object())

    audits = [payload for name, payload in calls if name == "record_batch_audit"]
    assert audits[-1]["execution_result"] == "progress_row_creation_failed"


# --- 通常経路が変わっていないこと -------------------------------------------------------


def test_the_normal_path_does_not_terminate_the_batch_early(
    calls: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 候補がある通常経路では、中断側の終端遷移が**1 つも起きない**こと。

    ここが崩れると、正常なバッチが dispatch 前に締められる。
    """
    monkeypatch.setattr(
        handler_module, "_collect_new_candidate_targets", lambda *a, **kw: (["0000"], {})
    )
    monkeypatch.setattr(handler_module, "set_watchlist_batch_total", lambda *a, **kw: None)
    monkeypatch.setattr(
        handler_module, "create_missing_candidate_progress_rows", lambda *a, **kw: None
    )
    # 進捗行の件数照合で早期 return させ、SQS 送信まで進めない
    # （本テストの関心は「中断側の終端遷移が起きないこと」だけ）。
    monkeypatch.setattr(handler_module, "query_all_candidate_progress", lambda *a, **kw: [])

    handler_module.handler({}, object())

    assert "mark_watchlist_batch_completed" not in _names(calls)
    # ★ 件数不一致の既存経路は従来どおり mark_dispatch_failed を呼ぶ（不変）。
    assert _names(calls).count("mark_dispatch_failed") == 1
