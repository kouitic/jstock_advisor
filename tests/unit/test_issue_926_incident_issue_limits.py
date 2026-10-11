"""Issue #926: incident の GitHub Issue 起票の上限値(専用 YAML と専用 loader)の契約テスト。

出荷値は全項目 null = 未決定(USER が決める値)。未決定・読めない・不正な設定は、すべて『未決定』
へ縮退する
(= 新規 Issue を作らない fail-closed)。loader は例外を送出しない(LINE の経路を落とさない)。
S-13(config/models.py・loader の AppConfig)へ field を足さない。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from jstock_advisor.domain.notification import incident_issue_limits as limits_module
from jstock_advisor.domain.notification.incident_issue_limits import (
    CONFIG_FILE_NAME,
    IncidentIssueLimits,
    load_incident_issue_limits,
    undecided_limits,
)

_REPO = Path(__file__).resolve().parents[2]
_SHIPPED = _REPO / "config" / CONFIG_FILE_NAME
_FIELDS = (
    "new_issue_min_interval_minutes",
    "new_issue_limit_per_fingerprint",
    "new_issue_limit_global",
    "auto_stop_denied_threshold",
)


def _write(tmp_path: Path, text: str) -> Path:
    (tmp_path / CONFIG_FILE_NAME).write_text(text, encoding="utf-8")
    return tmp_path


def _decided_yaml(**over: object) -> str:
    values: dict[str, object] = {
        "new_issue_min_interval_minutes": 360,
        "new_issue_limit_per_fingerprint": 2,
        "new_issue_limit_global": 5,
        "auto_stop_denied_threshold": 10,
    }
    values.update(over)
    lines = ["version: 1"] + [f"{k}: {'null' if v is None else v}" for k, v in values.items()]
    return "\n".join(lines) + "\n"


# --- 出荷値 --------------------------------------------------------------------------------


def test_the_shipped_file_exists_and_every_value_is_undecided() -> None:
    assert _SHIPPED.is_file()
    loaded = load_incident_issue_limits()
    assert loaded == undecided_limits()
    assert all(getattr(loaded, name) is None for name in _FIELDS)
    assert loaded.missing() == _FIELDS
    assert not loaded.is_decided()


def test_the_shipped_file_names_exactly_the_four_values() -> None:
    import yaml

    data = yaml.safe_load(_SHIPPED.read_text(encoding="utf-8"))
    assert set(data) == {"version", *_FIELDS}
    assert data["version"] == 1
    assert all(data[name] is None for name in _FIELDS)


# --- 決定済みの値 --------------------------------------------------------------------------


def test_decided_values_are_read(tmp_path: Path) -> None:
    loaded = load_incident_issue_limits(_write(tmp_path, _decided_yaml()))
    assert (
        loaded.new_issue_min_interval_minutes,
        loaded.new_issue_limit_per_fingerprint,
        loaded.new_issue_limit_global,
        loaded.auto_stop_denied_threshold,
    ) == (360, 2, 5, 10)
    assert loaded.is_decided() and loaded.missing() == ()


@pytest.mark.parametrize("name", _FIELDS)
def test_one_undecided_value_alone_makes_the_limits_undecided(tmp_path: Path, name: str) -> None:
    loaded = load_incident_issue_limits(_write(tmp_path, _decided_yaml(**{name: None})))
    assert not loaded.is_decided()
    assert loaded.missing() == (name,)
    # 決定済みの他の値は保たれる(縮退は項目単位ではなく、不正なファイル全体だけ)
    assert all(getattr(loaded, other) is not None for other in _FIELDS if other != name)


def test_the_missing_names_follow_a_fixed_order(tmp_path: Path) -> None:
    text = "version: 1\nnew_issue_limit_global: 5\n"
    loaded = load_incident_issue_limits(_write(tmp_path, text))
    assert loaded.missing() == (
        "new_issue_min_interval_minutes",
        "new_issue_limit_per_fingerprint",
        "auto_stop_denied_threshold",
    )


# --- 縮退(fail-closed。例外を送出しない)-------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",  # 空
        "not: [valid",  # YAML が壊れている
        "version: 1\nunknown_key: 1\n",  # 知らないキー
        "version: 1\nnew_issue_limit_global: 0\n",  # 0 以下
        "version: 1\nnew_issue_limit_global: -3\n",
        "version: 1\nnew_issue_limit_global: 2.5\n",  # 小数
        "version: 1\nnew_issue_limit_global: '5'\n",  # 文字列
        "version: 1\nnew_issue_limit_global: true\n",  # 真偽値(bool は int の部分型)
        "version: 1\nauto_stop_denied_threshold: .nan\n",
        "- 1\n- 2\n",  # 辞書でない
    ],
)
def test_an_unreadable_or_invalid_file_falls_back_to_all_undecided(
    tmp_path: Path, text: str
) -> None:
    assert load_incident_issue_limits(_write(tmp_path, text)) == undecided_limits()


def test_one_invalid_value_makes_the_whole_file_undecided(tmp_path: Path) -> None:
    # 部分的に効いた設定を作らない: 1 つでも不正なら、決定済みの他の値も使わない
    loaded = load_incident_issue_limits(_write(tmp_path, _decided_yaml(new_issue_limit_global=0)))
    assert loaded == undecided_limits()


def test_a_missing_file_falls_back_to_all_undecided(tmp_path: Path) -> None:
    assert load_incident_issue_limits(tmp_path) == undecided_limits()


def test_the_loader_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(path: Path) -> dict[str, object]:
        raise OSError("disk")

    monkeypatch.setattr(limits_module, "_load_yaml", boom)
    assert load_incident_issue_limits(tmp_path) == undecided_limits()


def test_the_fallback_warning_never_contains_a_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        load_incident_issue_limits(
            _write(tmp_path, "version: 1\nnew_issue_limit_global: 424242.5\n")
        )
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "424242" not in text and str(tmp_path) not in text


def test_the_model_is_frozen_and_rejects_extra_keys() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic の ValidationError / TypeError のどちらでもよい
        IncidentIssueLimits(unknown=1)  # type: ignore[call-arg]
    with pytest.raises(Exception):  # noqa: B017
        undecided_limits().new_issue_limit_global = 5  # type: ignore[misc]


# --- S-13 に載せない / 純粋性 --------------------------------------------------------------


def test_the_app_config_does_not_hold_the_limits() -> None:
    from jstock_advisor.config import models

    text = Path(models.__file__).read_text(encoding="utf-8")
    assert "new_issue_limit" not in text and "auto_stop_denied_threshold" not in text


def test_the_module_imports_no_infrastructure_and_reads_no_clock() -> None:
    tree = ast.parse(Path(limits_module.__file__).read_text(encoding="utf-8"))
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert not any(m.startswith(("boto3", "jstock_advisor.infrastructure")) for m in imported)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert name not in {"now", "utcnow", "today"}, name
