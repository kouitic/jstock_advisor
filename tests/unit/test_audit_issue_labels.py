"""audit_issue_labels(Issue #487。#220 Phase C-1)の検出・fail-close・read-only の guard。

## 何を検証するか

`scripts/audit_issue_labels.py` の危険な壊れ方は次のとおりである。

**1 欠落・滞留を見逃す。** label の欠落 3 軸・status 2 個以上・語彙外・滞留の閾値の境界。
**2 「検査できなかった」を「検出 0 件」へ倒す(fail-open)。** 空・壊れた入力は exit 2。
**3 日付の扱いを誤る。** 24 時間で 1 日と数えること、タイムゾーン付きの時刻を UTC へ揃えること。
**4 書き込み・ネットワークを行う。** 入力を読むだけで、何も書き換えない。

## 何を検証しないか

semantic 判断(split の要否・Priority の妥当性・tracking の本文の記載の有無)は検査しない。
本 script がそれを行わないこと自体は、tracking の注記のテストで確認している。
"""

from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_issue_labels.py"
_spec = importlib.util.spec_from_file_location("audit_issue_labels", _SCRIPT)
assert _spec is not None and _spec.loader is not None
ail = importlib.util.module_from_spec(_spec)
sys.modules["audit_issue_labels"] = ail
_spec.loader.exec_module(ail)

NOW = dt.datetime(2026, 9, 20, 0, 0, 0, tzinfo=dt.UTC)


def _issue(number: int, *labels: str, **extra: Any) -> dict[str, Any]:
    return {
        "number": number,
        "labels": [{"name": name} for name in labels],
        "updatedAt": "2026-09-20T00:00:00Z",
        **extra,
    }


_OK = ("enhancement", "priority:P3", "status:設計済")


def _conditions(result: Any) -> list[tuple[int, str]]:
    return [(f.number, f.condition) for f in result.findings]


# --- 正常系 ---------------------------------------------------------------------------------


def test_clean_issues_have_no_finding_and_are_counted() -> None:
    result = ail.audit([_issue(1, *_OK), _issue(2, *_OK)], now=NOW)
    assert result.findings == []
    assert result.checked == 2


def test_closed_issues_are_not_checked() -> None:
    result = ail.audit([_issue(1, *_OK), _issue(2, state="CLOSED")], now=NOW)
    assert result.checked == 1
    assert result.findings == []


# --- label の欠落・語彙 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (("priority:P3", "status:設計済"), "MISSING_TYPE"),
        (("enhancement", "status:設計済"), "MISSING_PRIORITY"),
        (("enhancement", "priority:P3"), "MISSING_STATUS"),
    ],
)
def test_each_missing_axis_is_detected(labels: tuple[str, ...], expected: str) -> None:
    result = ail.audit([_issue(7, *labels)], now=NOW)
    assert _conditions(result) == [(7, expected)]


def test_multiple_status_labels_are_detected() -> None:
    result = ail.audit([_issue(3, "bug", "priority:P1", "status:設計済", "status:開発中")], now=NOW)
    assert _conditions(result) == [(3, "MULTIPLE_STATUS")]


def test_status_label_outside_the_vocabulary_is_detected() -> None:
    result = ail.audit([_issue(4, "bug", "priority:P1", "status:完了")], now=NOW)
    assert _conditions(result) == [(4, "UNKNOWN_STATUS")]


def test_tracking_without_priority_is_reported_with_a_note_but_not_judged() -> None:
    result = ail.audit(
        [_issue(5, "tracking", "status:設計済"), _issue(6, "bug", "status:設計済")], now=NOW
    )
    by_number = {f.number: f for f in result.findings}
    assert by_number[5].condition == "MISSING_PRIORITY"
    assert "N/A の可能性" in by_number[5].detail
    assert "判断しない" in by_number[5].detail
    assert "N/A" not in by_number[6].detail  # tracking 以外には注記しない


# --- 滞留 -----------------------------------------------------------------------------------------


def _deployed(number: int, updated: str, **extra: Any) -> dict[str, Any]:
    return _issue(number, "bug", "priority:P1", "status:デプロイ済", updatedAt=updated, **extra)


def test_stale_deployed_boundary_is_inclusive_and_counts_whole_days() -> None:
    at_threshold = _deployed(1, "2026-09-13T00:00:00Z")  # ちょうど 7 日
    just_below = _deployed(2, "2026-09-13T00:00:01Z")  # 6 日と 23 時間 59 分 59 秒 = 6 日
    result = ail.audit([at_threshold, just_below], now=NOW, deployed_days=7)
    assert _conditions(result) == [(1, "STALE_DEPLOYED")]
    assert {d.number: d.days for d in result.dwell} == {1: 7, 2: 6}


def test_stale_merged_is_detected_independently_of_deployed() -> None:
    merged = _issue(8, "bug", "priority:P1", "status:マージ済", updatedAt="2026-09-10T00:00:00Z")
    result = ail.audit([merged], now=NOW, deployed_days=1, merged_days=7)
    assert _conditions(result) == [(8, "STALE_MERGED")]
    assert ail.audit([merged], now=NOW, deployed_days=1).findings == []  # マージ済の閾値なし


def test_without_thresholds_dwell_is_only_observed_never_reported() -> None:
    result = ail.audit([_deployed(1, "2026-08-01T00:00:00Z")], now=NOW)
    assert result.findings == []  # 閾値が無ければ滞留を検出しない(既定値を置かない)
    assert result.dwell[0].days == 50


def test_status_since_is_preferred_over_updated_at() -> None:
    # updatedAt は今日(コメントで更新された)だが、label を付けたのは 10 日前 = #36 型
    issue = _deployed(9, "2026-09-20T00:00:00Z", status_since="2026-09-10T00:00:00Z")
    result = ail.audit([issue], now=NOW, deployed_days=7)
    assert _conditions(result) == [(9, "STALE_DEPLOYED")]
    assert result.dwell[0].basis == "status_since"
    fallback = ail.audit([_deployed(9, "2026-09-20T00:00:00Z")], now=NOW, deployed_days=7)
    assert fallback.findings == []  # status_since が無ければ updatedAt(今日 = 滞留なし)


def test_timezone_offsets_are_normalized_to_utc() -> None:
    # 2026-09-13T09:00:00+09:00 = 2026-09-13T00:00:00Z = ちょうど 7 日前
    result = ail.audit([_deployed(1, "2026-09-13T09:00:00+09:00")], now=NOW, deployed_days=7)
    assert _conditions(result) == [(1, "STALE_DEPLOYED")]


# --- 検査できなかった(fail-close)-------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        [],
        None,
        {"number": 1},
        [{"labels": []}],
        [{"number": 1, "labels": "bug"}],
        [{"number": 1, "labels": [{"nom": "bug"}]}],
        [{"number": 1, "labels": [], "state": "OPEN"}, "not-a-dict"],
    ],
)
def test_malformed_or_empty_input_is_not_reported_as_zero_findings(bad: object) -> None:
    with pytest.raises(ail.InputError):
        ail.audit(bad, now=NOW)


def test_dwell_without_any_start_time_is_uncheckable() -> None:
    issue = {"number": 1, "labels": [{"name": "status:デプロイ済"}]}
    with pytest.raises(ail.InputError):
        ail.audit([issue], now=NOW, deployed_days=7)


def test_naive_or_unparsable_time_is_uncheckable() -> None:
    for bad in ("2026-09-13T00:00:00", "yesterday", 20260913):
        with pytest.raises(ail.InputError):
            ail.audit([_deployed(1, bad)], now=NOW, deployed_days=7)  # type: ignore[arg-type]


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "issues.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ok = _write(tmp_path, [_issue(1, *_OK)])
    assert ail.main(["--input", str(ok), "--now", "2026-09-20T00:00:00Z"]) == 0
    assert "CHECKED_OPEN_ISSUES = 1" in capsys.readouterr().out

    bad_labels = _write(tmp_path, [_issue(1, "bug")])
    argv = ["--input", str(bad_labels), "--now", "2026-09-20T00:00:00Z"]
    assert ail.main(argv) == 0  # 既定では、検出があっても 0(検査はできた)
    assert ail.main([*argv, "--fail-on-findings"]) == 1
    assert "MISSING_PRIORITY" in capsys.readouterr().out


def test_main_returns_2_for_unreadable_empty_or_broken_input(tmp_path: Path) -> None:
    assert ail.main(["--input", str(tmp_path / "missing.json")]) == 2
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert ail.main(["--input", str(broken)]) == 2
    assert ail.main(["--input", str(_write(tmp_path, []))]) == 2
    assert ail.main(["--input", str(_write(tmp_path, [_issue(1, *_OK)])), "--now", "nope"]) == 2


# --- read-only・ネットワークなし・決定的 ---------------------------------------------


def test_run_does_not_modify_the_input_or_create_files(tmp_path: Path) -> None:
    path = _write(tmp_path, [_issue(1, "bug")])
    before = path.read_bytes()
    ail.main(["--input", str(path), "--now", "2026-09-20T00:00:00Z"])
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["issues.json"]


def test_script_imports_no_network_or_process_modules() -> None:
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported.isdisjoint(
        {"socket", "urllib", "http", "requests", "subprocess", "ssl", "asyncio", "shutil"}
    )


def test_audit_is_deterministic_for_a_fixed_now() -> None:
    issues = [_deployed(1, "2026-09-01T00:00:00Z"), _issue(2, "bug")]
    first = ail.audit(issues, now=NOW, deployed_days=3)
    second = ail.audit(issues, now=NOW, deployed_days=3)
    assert first == second
