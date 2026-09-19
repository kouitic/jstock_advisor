"""Issue #160 PR-0: shadow設定(専用モデル + 専用loader)。挙動不変・fail-closedを固定する。

時刻・営業日には触れない(TIME_SEMANTICS_IMPACT = NO)。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from jstock_advisor.domain.signals import judgment_safety_shadow_config as shadow_cfg
from jstock_advisor.domain.signals.judgment_safety_shadow_config import (
    CONFIG_FILE_NAME,
    JudgmentSafetyShadowConfig,
    ShadowMode,
    load_judgment_safety_shadow_config,
    off_config,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TWO_INPUTS = ("continuous_dividend_increase_years", "is_progressive_or_doe_policy")


def _write(tmp_path: Path, text: str) -> Path:
    (tmp_path / CONFIG_FILE_NAME).write_text(text, encoding="utf-8")
    return tmp_path


def test_repository_config_is_off_and_targets_only_the_two_measurable_inputs() -> None:
    cfg = load_judgment_safety_shadow_config(_REPO_ROOT / "config")

    assert cfg.mode is ShadowMode.OFF
    assert cfg.enabled is False
    assert cfg.g3_required_inputs == _TWO_INPUTS


def test_default_model_is_off() -> None:
    assert JudgmentSafetyShadowConfig().mode is ShadowMode.OFF
    assert off_config().enabled is False


def test_shadow_mode_is_enabled_only_when_explicitly_set(tmp_path: Path) -> None:
    cfg = load_judgment_safety_shadow_config(_write(tmp_path, "version: 1\nmode: SHADOW\n"))

    assert cfg.mode is ShadowMode.SHADOW
    assert cfg.enabled is True
    assert cfg.g3_required_inputs == _TWO_INPUTS


def test_comment_keys_starting_with_underscore_are_ignored(tmp_path: Path) -> None:
    cfg = load_judgment_safety_shadow_config(_write(tmp_path, "_note: x\nmode: SHADOW\n"))

    assert cfg.enabled is True


@pytest.mark.parametrize(
    "text",
    [
        "mode: ON\n",  # 未知のmode
        "mode: shadow\n",  # 大文字小文字も不正(暗黙に有効化しない)
        "mode: true\n",
        "unknown_key: 1\nmode: SHADOW\n",  # extra禁止 → 不正 → OFF(SHADOWにならない)
        "mode: SHADOW\ng3_required_inputs: []\n",  # 空(min_length=1)
        # 測定不能な項目は型として拒否する(Falseを不明と推測しない)
        "mode: SHADOW\ng3_required_inputs: [fair_value_rising_with_earnings_growth]\n",
        "mode: SHADOW\ng3_required_inputs: [long_term_holding_benefit_imminent]\n",
        "mode: SHADOW\ng3_required_inputs: [few_reinvestment_alternatives]\n",
        "mode: SHADOW\ng3_required_inputs: [is_nisa_account]\n",
        "- a\n- b\n",  # 辞書でない
        "mode: [unclosed\n",  # YAMLとして不正
        "",  # 空
    ],
)
def test_invalid_config_falls_back_to_off_and_never_raises(tmp_path: Path, text: str) -> None:
    cfg = load_judgment_safety_shadow_config(_write(tmp_path, text))

    assert cfg == off_config()
    assert cfg.enabled is False


def test_missing_file_falls_back_to_off(tmp_path: Path) -> None:
    assert load_judgment_safety_shadow_config(tmp_path) == off_config()


def test_missing_directory_falls_back_to_off(tmp_path: Path) -> None:
    assert load_judgment_safety_shadow_config(tmp_path / "nope") == off_config()


def test_fallback_warns_with_error_type_only_not_path_or_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=shadow_cfg.__name__)
    secret_dir = tmp_path / "very-private-dir-name"
    secret_dir.mkdir()

    load_judgment_safety_shadow_config(_write(secret_dir, "mode: PRIVATE_VALUE\n"))

    (record,) = [r for r in caplog.records if r.name == shadow_cfg.__name__]
    assert record.levelno == logging.WARNING
    assert "mode=OFF" in record.getMessage()
    assert "very-private-dir-name" not in record.getMessage()
    assert "PRIVATE_VALUE" not in record.getMessage()


def test_valid_config_logs_nothing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=shadow_cfg.__name__)

    load_judgment_safety_shadow_config(_write(tmp_path, "mode: SHADOW\n"))

    assert [r for r in caplog.records if r.name == shadow_cfg.__name__] == []


def test_the_module_is_not_wired_into_the_production_call_graph() -> None:
    """PR-0は設定だけ。判定経路のどこからも参照されない(挙動不変)。参照が増えるのはPR-1以降。"""
    referrers = [
        str(p.relative_to(_REPO_ROOT))
        for p in (_REPO_ROOT / "src").rglob("*.py")
        if p.name != "judgment_safety_shadow_config.py"
        and "judgment_safety_shadow_config" in p.read_text(encoding="utf-8")
    ]

    assert referrers == []


def test_app_config_is_unchanged_and_does_not_carry_the_shadow_block() -> None:
    """S-13(AppConfig)へは載せない: load_configは従来どおりでshadowを持たない。"""
    from jstock_advisor.config.loader import load_config

    cfg = load_config()

    assert not hasattr(cfg, "judgment_safety_shadow")
