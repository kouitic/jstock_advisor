"""Issue #506(#132 O-1): domain/notification/incident_signal.py の型検査の確認。

確認すること:

    1 必須フィールド(source/job_name/failure_stage/failure_type/error_type)が
      空文字・非strを拒否する
    2 occurred_atがtimezone-awareでなければならない
    3 failure_count/consecutive_daysはint以外(bool含む)・負数を拒否する
    4 is_ongoingはbool以外を拒否する
    5 ネットワーク・ファイル・AWSに触れない(import検査)
"""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path

import pytest

from jstock_advisor.domain.notification import incident_signal
from jstock_advisor.domain.notification.incident_signal import IncidentSignal

_UTC = dt.UTC


def _signal(**overrides: object) -> IncidentSignal:
    base = {
        "source": "watchlist_reconciler",
        "job_name": "watchlist-dispatcher",
        "failure_stage": "SCHEDULE",
        "failure_type": "MISSED_SCHEDULE",
        "error_type": "WATCHLIST_MISSED_SCHEDULE",
        "error_message": "WATCHLIST_MISSED_SCHEDULE",
        "occurred_at": dt.datetime(2026, 9, 24, 0, 0, tzinfo=_UTC),
    }
    base.update(overrides)
    return IncidentSignal(**base)  # type: ignore[arg-type]


def test_valid_signal_constructs() -> None:
    signal = _signal(failure_count=3, consecutive_days=3, is_ongoing=True)
    assert signal.failure_count == 3
    assert signal.consecutive_days == 3
    assert signal.is_ongoing is True


@pytest.mark.parametrize(
    "field", ["source", "job_name", "failure_stage", "failure_type", "error_type"]
)
def test_empty_required_field_is_rejected(field: str) -> None:
    with pytest.raises(ValueError):
        _signal(**{field: ""})


@pytest.mark.parametrize(
    "field", ["source", "job_name", "failure_stage", "failure_type", "error_type"]
)
def test_non_string_required_field_is_rejected(field: str) -> None:
    with pytest.raises(ValueError):
        _signal(**{field: 123})


def test_error_message_may_be_present_but_must_be_a_string() -> None:
    with pytest.raises(ValueError):
        _signal(error_message=123)


def test_naive_occurred_at_is_rejected() -> None:
    with pytest.raises(ValueError):
        _signal(occurred_at=dt.datetime(2026, 9, 24))  # noqa: DTZ001


def test_non_datetime_occurred_at_is_rejected() -> None:
    with pytest.raises(TypeError):
        _signal(occurred_at="2026-09-24T00:00:00Z")  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["failure_count", "consecutive_days"])
def test_non_int_optional_count_is_rejected(field: str) -> None:
    with pytest.raises(TypeError):
        _signal(**{field: "3"})


@pytest.mark.parametrize("field", ["failure_count", "consecutive_days"])
def test_bool_optional_count_is_rejected(field: str) -> None:
    """★ boolはintのサブクラスのため、isinstance(value, int)だけでは通ってしまう。"""
    with pytest.raises(TypeError):
        _signal(**{field: True})


@pytest.mark.parametrize("field", ["failure_count", "consecutive_days"])
def test_negative_optional_count_is_rejected(field: str) -> None:
    with pytest.raises(ValueError):
        _signal(**{field: -1})


@pytest.mark.parametrize("field", ["failure_count", "consecutive_days"])
def test_none_optional_count_is_accepted(field: str) -> None:
    signal = _signal(**{field: None})
    assert getattr(signal, field) is None


def test_zero_optional_count_is_accepted() -> None:
    signal = _signal(failure_count=0, consecutive_days=0)
    assert signal.failure_count == 0
    assert signal.consecutive_days == 0


def test_non_bool_is_ongoing_is_rejected() -> None:
    with pytest.raises(TypeError):
        _signal(is_ongoing="yes")


def test_is_frozen() -> None:
    signal = _signal()
    with pytest.raises(Exception):  # noqa: B017 - dataclasses.FrozenInstanceError
        signal.source = "other"  # type: ignore[misc]


# --- ネットワーク・ファイル・AWSに触れない -------------------------------------

_ALLOWED_IMPORTS = {
    "__future__",
    "datetime",
    "dataclasses",
    "jstock_advisor.domain.jst",
}


def test_the_module_imports_nothing_that_touches_network_files_or_aws() -> None:
    tree = ast.parse(Path(incident_signal.__file__).read_text(encoding="utf-8"))
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
