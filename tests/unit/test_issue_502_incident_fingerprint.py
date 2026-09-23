"""Issue #502(#132 X-3): incident の fingerprint 計算と dedup 判定の確認。

確認すること:

    1 fingerprint は決定的(同じ入力 -> 同じ値)。キーワード引数の与え方の順序に依存しない
    2 environment / job_name / failure_stage / failure_type のいずれかが違えば別の fingerprint
      になる(反証: 別 root cause を同一にまとめる欠陥を注入すると赤)
    3 job_name は `IncidentJob` の集約名ではなく、内部の job 識別子の粒度で区別する
      (watchlist の 4 関数が同一にまとまらない)
    4 normalized_error_signature が識別子・タイムスタンプ・件数の揺れを吸収しつつ、
      例外の種類は区別する
    5 fingerprint に、識別子・銘柄・所有者・例外文の生の値が残らない
    6 dedup 判定の時間窓の境界(ちょうど window)の扱いが固定されている
    7 ネットワーク・ファイル・AWS・永続化に触れない(module の import の検査)
"""

from __future__ import annotations

import ast
import datetime as dt
import re
from pathlib import Path

import pytest

from jstock_advisor.domain.notification import incident_fingerprint
from jstock_advisor.domain.notification.incident_fingerprint import (
    IncidentFingerprintInput,
    compute_fingerprint,
    is_duplicate_within_window,
    normalize_error_signature,
)

_UTC = dt.UTC


def _signal(**overrides: object) -> IncidentFingerprintInput:
    base = {
        "environment": "production",
        "job_name": "watchlist-dispatcher",
        "failure_stage": "INVOCATION",
        "failure_type": "TIMEOUT",
        "error_type": "TimeoutError",
        "error_message": "call to provider timed out",
    }
    base.update(overrides)
    return IncidentFingerprintInput(**base)  # type: ignore[arg-type]


# --- 1. 決定性 -----------------------------------------------------------


def test_the_same_input_always_produces_the_same_fingerprint() -> None:
    a = compute_fingerprint(_signal())
    b = compute_fingerprint(_signal())
    assert a == b
    assert re.fullmatch(r"[0-9a-f]{64}", a)


def test_fingerprint_does_not_depend_on_the_keyword_order_given_to_the_input() -> None:
    a = compute_fingerprint(
        IncidentFingerprintInput(
            environment="production",
            job_name="evaluation",
            failure_stage="PROCESSING",
            failure_type="UNEXPECTED_EXCEPTION",
            error_type="ValueError",
            error_message="bad value",
        )
    )
    b = compute_fingerprint(
        IncidentFingerprintInput(
            error_message="bad value",
            error_type="ValueError",
            failure_type="UNEXPECTED_EXCEPTION",
            failure_stage="PROCESSING",
            job_name="evaluation",
            environment="production",
        )
    )
    assert a == b


# --- 2. 異なる root cause は別の fingerprint ------------------------------


@pytest.mark.parametrize(
    "field,other",
    [
        ("environment", "staging"),
        ("job_name", "watchlist-worker"),
        ("failure_stage", "PERSISTENCE"),
        ("failure_type", "THROTTLED"),
        ("error_type", "ConnectionError"),
    ],
)
def test_changing_any_single_component_changes_the_fingerprint(field: str, other: str) -> None:
    baseline = compute_fingerprint(_signal())
    changed = compute_fingerprint(_signal(**{field: other}))
    assert baseline != changed


def test_mutation_that_ignores_job_name_would_wrongly_merge_two_different_jobs() -> None:
    """反証: fingerprint が job_name を見ていない実装なら、この 2 つは同じ値になってしまう。"""
    a = compute_fingerprint(_signal(job_name="watchlist-dispatcher"))
    b = compute_fingerprint(_signal(job_name="watchlist-worker"))
    assert a != b


# --- 3. job_name は内部識別子の粒度(IncidentJob の集約名ではない) -----------


def test_job_name_uses_internal_granularity_not_the_display_aggregation() -> None:
    """#501 の IncidentJob.WATCHLIST_SCREENING は dispatcher/worker/terminal_failure/reconciler の
    4 関数を集約する表示名だが、fingerprint はこれらを区別しなければならない
    (同時に別々の関数で起きた別の障害を、同一 incident に丸めてはならない)。
    """
    internal_names = [
        "watchlist-dispatcher",
        "watchlist-worker",
        "watchlist-terminal-failure-handler",
        "watchlist-batch-reconciler",
    ]
    fingerprints = {compute_fingerprint(_signal(job_name=name)) for name in internal_names}
    assert len(fingerprints) == len(internal_names)


# --- 4. normalize_error_signature ----------------------------------------


def test_normalization_absorbs_identifiers_timestamps_and_counts() -> None:
    a = normalize_error_signature(
        "ProviderDataError",
        "request 11111111-2222-3333-4444-555555555555 failed at 2026-09-22T10:00:00Z "
        "after 3 attempts (id=deadbeef12345678)",
    )
    b = normalize_error_signature(
        "ProviderDataError",
        "request 99999999-8888-7777-6666-555555555555 failed at 2026-09-23T11:30:05+09:00 "
        "after 7 attempts (id=cafebabe87654321)",
    )
    assert a == b


def test_normalization_still_distinguishes_different_exception_types() -> None:
    a = normalize_error_signature("TimeoutError", "call timed out after 30 seconds")
    b = normalize_error_signature("ConnectionError", "call timed out after 30 seconds")
    assert a != b


def test_normalization_distinguishes_different_messages_of_the_same_exception_type() -> None:
    a = normalize_error_signature("ValueError", "invalid market segment")
    b = normalize_error_signature("ValueError", "invalid trading unit")
    assert a != b


def test_normalization_absorbs_whitespace_differences() -> None:
    a = normalize_error_signature("RuntimeError", "line one\nline   two")
    b = normalize_error_signature("RuntimeError", "line one line two")
    assert a == b


def test_normalization_is_bounded_in_length() -> None:
    huge = "x" * 100_000
    signature = normalize_error_signature("RuntimeError", huge)
    assert len(signature) <= 500 + len("RuntimeError: ")


# --- 5. 生の値が fingerprint / 正規化結果に残らない --------------------------


def test_raw_identifiers_do_not_survive_normalization() -> None:
    owner_like = "owner-a"  # 架空値。実データはテストへ書かない(CLAUDE.md 2節)
    signature = normalize_error_signature(
        "KeyError", f"holding not found for {owner_like} request-id=abcdef1234567890"
    )
    assert "abcdef1234567890" not in signature
    # owner_like 自体は英字のみで数字置換の対象外だが、fingerprint の入力に owner や
    # stock_code を渡さない設計(IncidentFingerprintInput にそのようなフィールドが無い)
    # であることを型の側で担保する。
    assert not hasattr(IncidentFingerprintInput, "owner")
    assert not hasattr(IncidentFingerprintInput, "stock_code")


def test_fingerprint_value_itself_is_an_opaque_hash_not_reconstructible_ids() -> None:
    fp = compute_fingerprint(_signal(error_message="secret detail: account 12345"))
    assert "12345" not in fp
    assert "secret" not in fp


# --- 6. dedup 判定 ---------------------------------------------------------


def test_no_prior_notification_is_never_a_duplicate() -> None:
    assert (
        is_duplicate_within_window(
            now=dt.datetime(2026, 9, 22, 10, 0, tzinfo=_UTC),
            last_notified_at=None,
            window=dt.timedelta(minutes=30),
        )
        is False
    )


def test_within_the_window_is_a_duplicate() -> None:
    assert (
        is_duplicate_within_window(
            now=dt.datetime(2026, 9, 22, 10, 29, tzinfo=_UTC),
            last_notified_at=dt.datetime(2026, 9, 22, 10, 0, tzinfo=_UTC),
            window=dt.timedelta(minutes=30),
        )
        is True
    )


def test_exactly_at_the_window_boundary_is_not_a_duplicate() -> None:
    """★ 境界の連続性: ちょうど window が経過した瞬間は「重複ではない」側に固定する。"""
    assert (
        is_duplicate_within_window(
            now=dt.datetime(2026, 9, 22, 10, 30, 0, tzinfo=_UTC),
            last_notified_at=dt.datetime(2026, 9, 22, 10, 0, 0, tzinfo=_UTC),
            window=dt.timedelta(minutes=30),
        )
        is False
    )


def test_one_microsecond_before_the_boundary_is_still_a_duplicate() -> None:
    assert (
        is_duplicate_within_window(
            now=dt.datetime(2026, 9, 22, 10, 29, 59, 999999, tzinfo=_UTC),
            last_notified_at=dt.datetime(2026, 9, 22, 10, 0, 0, tzinfo=_UTC),
            window=dt.timedelta(minutes=30),
        )
        is True
    )


def test_after_the_window_is_not_a_duplicate() -> None:
    assert (
        is_duplicate_within_window(
            now=dt.datetime(2026, 9, 22, 11, 0, 1, tzinfo=_UTC),
            last_notified_at=dt.datetime(2026, 9, 22, 10, 0, 0, tzinfo=_UTC),
            window=dt.timedelta(minutes=30),
        )
        is False
    )


def test_zero_width_window_never_treats_anything_as_a_duplicate() -> None:
    assert (
        is_duplicate_within_window(
            now=dt.datetime(2026, 9, 22, 10, 0, 0, tzinfo=_UTC),
            last_notified_at=dt.datetime(2026, 9, 22, 10, 0, 0, tzinfo=_UTC),
            window=dt.timedelta(0),
        )
        is False
    )


def test_now_before_last_notified_at_is_treated_as_a_duplicate_fail_safe() -> None:
    """呼び出し元の入力誤り(時計の巻き戻り等)を、誤って「重複ではない」側へ倒さない。"""
    assert (
        is_duplicate_within_window(
            now=dt.datetime(2026, 9, 22, 9, 0, 0, tzinfo=_UTC),
            last_notified_at=dt.datetime(2026, 9, 22, 10, 0, 0, tzinfo=_UTC),
            window=dt.timedelta(minutes=30),
        )
        is True
    )


def test_negative_window_is_rejected() -> None:
    with pytest.raises(ValueError):
        is_duplicate_within_window(
            now=dt.datetime(2026, 9, 22, 10, 0, tzinfo=_UTC),
            last_notified_at=dt.datetime(2026, 9, 22, 9, 0, tzinfo=_UTC),
            window=dt.timedelta(minutes=-1),
        )


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError):
        is_duplicate_within_window(
            now=dt.datetime(2026, 9, 22, 10, 0),  # noqa: DTZ001
            last_notified_at=None,
            window=dt.timedelta(minutes=30),
        )


def test_naive_last_notified_at_is_rejected() -> None:
    with pytest.raises(ValueError):
        is_duplicate_within_window(
            now=dt.datetime(2026, 9, 22, 10, 0, tzinfo=_UTC),
            last_notified_at=dt.datetime(2026, 9, 22, 9, 0),  # noqa: DTZ001
            window=dt.timedelta(minutes=30),
        )


# --- IncidentFingerprintInput の入力検査 ------------------------------------


@pytest.mark.parametrize(
    "field",
    ["environment", "job_name", "failure_stage", "failure_type", "error_type"],
)
def test_empty_required_field_is_rejected(field: str) -> None:
    with pytest.raises(ValueError):
        _signal(**{field: ""})


@pytest.mark.parametrize(
    "field",
    ["environment", "job_name", "failure_stage", "failure_type", "error_type"],
)
def test_non_string_required_field_is_rejected(field: str) -> None:
    with pytest.raises(ValueError):
        _signal(**{field: 123})


def test_input_is_frozen() -> None:
    signal = _signal()
    with pytest.raises(Exception):  # noqa: B017 - dataclasses.FrozenInstanceError
        signal.environment = "staging"  # type: ignore[misc]


# --- 7. ネットワーク・ファイル・AWS・永続化に触れない -------------------------

_ALLOWED_IMPORTS = {
    "__future__",
    "datetime",
    "hashlib",
    "re",
    "dataclasses",
    "jstock_advisor.domain.jst",
}


def test_the_module_imports_nothing_that_touches_network_files_or_aws() -> None:
    tree = ast.parse(Path(incident_fingerprint.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert imported <= _ALLOWED_IMPORTS, imported - _ALLOWED_IMPORTS
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    called = {n.func.id for n in calls if isinstance(n.func, ast.Name)}
    assert not called & {"open", "print", "exec", "eval", "__import__"}
