"""資金入替の観測の『対象バッチの特定・起動の窓の上限』の契約テスト(Issue #604 P-b2。dormant)。

設計: #604 P-b 設計 rev6(…6169320796)の F2・F3 + rev8(…6169691749)の MUST-1。

  1  batch_id は実際の `derive_scheduled_batch_id` と一致する(ハイフンなしの日付・JST)
  2  TTL の定数は `batch_tracker._TTL_HOURS` と一致する(TTL が変わったら落ちる)
  3  起動の窓の上限 = 予定時刻 + TTL − 余裕(08:00 JST なら 13:00 JST)
  4  型の検証・純粋性(AST)・dormant
"""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.jst import JST
from jstock_advisor.domain.signals import rotation_observation_schedule as module
from jstock_advisor.domain.signals.rotation_observation_schedule import (
    BATCH_RUN_TTL_HOURS,
    DEFAULT_SCHEDULED_TIME,
    HOLDINGS_BATCH_PREFIX,
    READ_MARGIN_HOURS,
    ScheduleContractError,
    holdings_batch_id,
    latest_safe_run_at,
    scheduled_at,
)
from jstock_advisor.infrastructure.aws import batch_tracker
from jstock_advisor.lambda_handlers._scheduling import derive_scheduled_batch_id
from tests.unit.test_rotation_observation_contract import _importers_of

_REPO = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO / "src" / "jstock_advisor"
_MODULE_PATH = _PACKAGE / "domain" / "signals" / "rotation_observation_schedule.py"
_DAY = dt.date(2026, 10, 15)
_NOW = dt.datetime(2026, 10, 15, 9, 0, tzinfo=JST)


# =================================================================================================
# 1 batch_id
# =================================================================================================


def test_the_batch_id_has_a_date_without_hyphens_and_the_jst_scheduled_time() -> None:
    assert holdings_batch_id(_DAY) == "holdings-watchlist-20261015T080000"


@pytest.mark.parametrize(
    "scheduled_time",
    ["2026-10-15T08:00:00+09:00", "2026-10-14T23:00:00Z", "2026-10-14T23:00:00+00:00"],
)
def test_the_batch_id_equals_what_the_real_scheduler_derivation_makes(
    scheduled_time: str,
) -> None:
    """★ 実際の `derive_scheduled_batch_id` と同じ文字列(形式の取り違えを CI で捕まえる)★。"""
    real = derive_scheduled_batch_id(
        HOLDINGS_BATCH_PREFIX, {"scheduled_time": scheduled_time}, _NOW
    )

    assert holdings_batch_id(_DAY) == real


def test_a_different_scheduled_time_changes_only_the_time_part() -> None:
    real = derive_scheduled_batch_id(
        HOLDINGS_BATCH_PREFIX, {"scheduled_time": "2026-10-15T08:30:15+09:00"}, _NOW
    )

    assert (
        holdings_batch_id(_DAY, dt.time(8, 30, 15))
        == real
        == ("holdings-watchlist-20261015T083015")
    )


def test_the_prefix_is_the_one_the_holdings_handler_derives_with() -> None:
    """保有バッチの handler が batch_id を導出する prefix と同じ(ソースの文字列で確認)。"""
    source = (_PACKAGE / "lambda_handlers" / "holdings_watchlist_handler.py").read_text(
        encoding="utf-8"
    )

    assert f'derive_scheduled_batch_id("{HOLDINGS_BATCH_PREFIX}"' in source


def test_a_manual_run_batch_id_is_not_derivable_so_it_is_never_equal() -> None:
    """手動起動(scheduled_time なし)はランダムの接尾辞つき。P の導出とは一致しない(引かない)。"""
    manual = derive_scheduled_batch_id(HOLDINGS_BATCH_PREFIX, {}, _NOW)

    assert manual != holdings_batch_id(_DAY)
    assert manual.startswith("holdings-watchlist-20261015T090000-")


def test_the_default_scheduled_time_is_eight_oclock() -> None:
    assert dt.time(8, 0, 0) == DEFAULT_SCHEDULED_TIME


# =================================================================================================
# 2 TTL
# =================================================================================================


def test_the_ttl_constant_is_the_batch_trackers_ttl() -> None:
    """★ BatchRuns の TTL が変わったら落ちる(窓の上限の根拠が変わるため)★。"""
    assert BATCH_RUN_TTL_HOURS == batch_tracker._TTL_HOURS == 6


def test_the_margin_is_positive_and_smaller_than_the_ttl() -> None:
    assert 0 < READ_MARGIN_HOURS < BATCH_RUN_TTL_HOURS


# =================================================================================================
# 3 起動の窓の上限
# =================================================================================================


def test_the_scheduled_time_is_jst_aware() -> None:
    assert scheduled_at(_DAY) == dt.datetime(2026, 10, 15, 8, 0, tzinfo=JST)
    assert scheduled_at(_DAY).utcoffset() == dt.timedelta(hours=9)


def test_the_latest_safe_run_is_thirteen_oclock_jst_for_an_eight_oclock_batch() -> None:
    assert latest_safe_run_at(_DAY) == dt.datetime(2026, 10, 15, 13, 0, tzinfo=JST)


def test_the_latest_safe_run_is_before_the_row_expires_with_the_margin() -> None:
    expiry = scheduled_at(_DAY) + dt.timedelta(hours=BATCH_RUN_TTL_HOURS)

    assert latest_safe_run_at(_DAY) < expiry
    assert expiry - latest_safe_run_at(_DAY) == dt.timedelta(hours=READ_MARGIN_HOURS)


def test_the_latest_safe_run_follows_the_scheduled_time() -> None:
    assert latest_safe_run_at(_DAY, dt.time(8, 30)) == dt.datetime(2026, 10, 15, 13, 30, tzinfo=JST)


def test_the_planned_last_run_of_the_window_is_inside_the_limit() -> None:
    """設計の案(08:15・08:30・08:45・09:00)の最後の回は、上限(13:00)の内側。"""
    planned = [dt.datetime(2026, 10, 15, 8, m, tzinfo=JST) for m in (15, 30, 45)]
    planned.append(dt.datetime(2026, 10, 15, 9, 0, tzinfo=JST))

    assert all(run <= latest_safe_run_at(_DAY) for run in planned)


def test_a_run_after_the_limit_would_be_outside() -> None:
    assert dt.datetime(2026, 10, 15, 13, 0, 1, tzinfo=JST) > latest_safe_run_at(_DAY)


# =================================================================================================
# 4 型の検証・純粋性・dormant
# =================================================================================================


@pytest.mark.parametrize("bad", [dt.datetime(2026, 10, 15, 0, 0, tzinfo=JST), "2026-10-15", None])
def test_the_target_day_must_be_a_date(bad: Any) -> None:
    for function in (holdings_batch_id, scheduled_at, latest_safe_run_at):
        with pytest.raises(ScheduleContractError):
            function(bad)


@pytest.mark.parametrize("bad", ["08:00", dt.time(8, 0, tzinfo=JST), None, 8])
def test_the_scheduled_time_must_be_a_naive_time_of_day(bad: Any) -> None:
    for function in (holdings_batch_id, scheduled_at, latest_safe_run_at):
        with pytest.raises(ScheduleContractError):
            function(_DAY, bad)


def _tree() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


def _imports() -> list[str]:
    imported: list[str] = []
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
    return imported


def test_the_module_imports_only_the_standard_library_and_the_jst_helper() -> None:
    for name in _imports():
        assert name.split(".")[0] in {"datetime", "typing", "__future__", "jstock_advisor"}, name
        if name.startswith("jstock_advisor"):
            assert name == "jstock_advisor.domain.jst", name


def test_the_module_reads_no_clock_no_environment_and_does_no_io() -> None:
    called = {
        node.func.attr
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    names = {node.id for node in ast.walk(_tree()) if isinstance(node, ast.Name)}

    assert not called & {"now", "utcnow", "today", "sleep", "open", "getenv"}
    assert "time" not in {name.split(".")[0] for name in _imports()}  # time module なし
    assert not names & {"print", "open", "input", "logger", "logging", "environ"}


def test_nothing_in_src_imports_the_module_yet() -> None:
    """dormant: P-b3 の runner の配線(P-b4)まで、どこからも import されない。"""
    assert _importers_of("rotation_observation_schedule", _MODULE_PATH) == []


def test_the_module_is_importable_by_name() -> None:
    assert module.HOLDINGS_BATCH_PREFIX == "holdings-watchlist"
