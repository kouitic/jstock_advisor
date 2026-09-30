"""Issue #582: shadow設定(専用モデル + 専用loader)。挙動不変・fail-closedを固定する。

時刻・営業日には触れない(TIME_SEMANTICS_IMPACT = NO)。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from jstock_advisor.config.loader import _load_yaml
from jstock_advisor.domain.signals import valuation_confidence_shadow_config as shadow_cfg
from jstock_advisor.domain.signals.valuation_confidence_shadow_config import (
    CONFIG_FILE_NAME,
    ShadowMode,
    ValuationConfidenceShadowConfig,
    load_valuation_confidence_shadow_config,
    off_config,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(tmp_path: Path, text: str) -> Path:
    (tmp_path / CONFIG_FILE_NAME).write_text(text, encoding="utf-8")
    return tmp_path


def test_shipped_config_passes_validation_without_falling_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """出荷ファイルがfallbackを経ずに検証を通ること(fallbackの値と偶然一致しても通してはならない)。

    modeを引用符なしで書くと、YAML 1.1では真偽値Falseと解釈されて検証に失敗し、ファイル全体が
    読まれなくなる。fallbackの返す値(OFF)は出荷ファイルの値と一致するため、結果の値だけでは
    「読めた」と「読めずに落ちた」を区別できない。
    """
    caplog.set_level(logging.DEBUG, logger=shadow_cfg.__name__)
    path = _REPO_ROOT / "config" / CONFIG_FILE_NAME

    parsed = ValuationConfidenceShadowConfig.model_validate(_load_yaml(path))
    loaded = load_valuation_confidence_shadow_config(_REPO_ROOT / "config")

    assert loaded == parsed
    assert [r for r in caplog.records if r.name == shadow_cfg.__name__] == []
    assert isinstance(_load_yaml(path)["mode"], str)


def test_shipped_config_values_are_exactly_what_the_file_says() -> None:
    raw = _load_yaml(_REPO_ROOT / "config" / CONFIG_FILE_NAME)

    cfg = load_valuation_confidence_shadow_config(_REPO_ROOT / "config")

    assert raw["mode"] == "OFF"
    assert cfg.mode is ShadowMode.OFF
    assert cfg.enabled is False


def test_unquoted_off_is_a_boolean_in_yaml_and_is_rejected_not_silently_accepted(
    tmp_path: Path,
) -> None:
    """引用符なしのOFFはYAML 1.1でFalse。検証に失敗する(この事故を明示的に固定する)。"""
    raw = _load_yaml(_write(tmp_path, "mode: OFF\n") / CONFIG_FILE_NAME)

    assert raw["mode"] is False
    with pytest.raises(ValueError, match="mode"):
        ValuationConfidenceShadowConfig.model_validate(raw)


def test_default_model_is_off() -> None:
    assert ValuationConfidenceShadowConfig().mode is ShadowMode.OFF
    assert off_config().enabled is False


def test_shadow_mode_is_enabled_only_when_explicitly_set(tmp_path: Path) -> None:
    cfg = load_valuation_confidence_shadow_config(_write(tmp_path, 'version: 1\nmode: "SHADOW"\n'))

    assert cfg.mode is ShadowMode.SHADOW
    assert cfg.enabled is True


@pytest.mark.parametrize(
    "text",
    [
        'mode: "ON"\n',  # 未知のmode
        'mode: "shadow"\n',  # 大文字小文字も不正(暗黙に有効化しない)
        "mode: true\n",
        'unknown_key: 1\nmode: "SHADOW"\n',  # extra禁止 → 不正 → OFF(SHADOWにならない)
        "- a\n- b\n",  # 辞書でない
        "mode: [unclosed\n",  # YAMLとして不正
        "",  # 空
    ],
)
def test_invalid_config_falls_back_to_off_and_never_raises(tmp_path: Path, text: str) -> None:
    cfg = load_valuation_confidence_shadow_config(_write(tmp_path, text))

    assert cfg == off_config()
    assert cfg.enabled is False


def test_missing_file_falls_back_to_off(tmp_path: Path) -> None:
    assert load_valuation_confidence_shadow_config(tmp_path) == off_config()


def test_missing_directory_falls_back_to_off(tmp_path: Path) -> None:
    assert load_valuation_confidence_shadow_config(tmp_path / "nope") == off_config()


def test_fallback_warns_with_error_type_only_not_path_or_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=shadow_cfg.__name__)
    secret_dir = tmp_path / "very-private-dir-name"
    secret_dir.mkdir()

    load_valuation_confidence_shadow_config(_write(secret_dir, "mode: PRIVATE_VALUE\n"))

    (record,) = [r for r in caplog.records if r.name == shadow_cfg.__name__]
    assert record.levelno == logging.WARNING
    assert "mode=OFF" in record.getMessage()
    assert "very-private-dir-name" not in record.getMessage()
    assert "PRIVATE_VALUE" not in record.getMessage()


def test_valid_config_logs_nothing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=shadow_cfg.__name__)

    load_valuation_confidence_shadow_config(_write(tmp_path, 'mode: "SHADOW"\n'))

    assert [r for r in caplog.records if r.name == shadow_cfg.__name__] == []


def test_the_config_is_read_only_by_the_shadow_service() -> None:
    """設定を読んでよいのはshadowサービスのみ。handlerはサービスを呼ぶだけで設定を直接読まない。"""
    referrers = sorted(
        p.relative_to(_REPO_ROOT).as_posix()
        for p in (_REPO_ROOT / "src").rglob("*.py")
        if p.name != "valuation_confidence_shadow_config.py"
        and "valuation_confidence_shadow_config" in p.read_text(encoding="utf-8")
    )

    assert referrers == ["src/jstock_advisor/services/valuation_confidence_shadow_service.py"]


def test_app_config_is_unchanged_and_does_not_carry_the_shadow_block() -> None:
    """S-13(AppConfig)へは載せない: load_configは従来どおりでshadowを持たない。"""
    from jstock_advisor.config.loader import load_config

    cfg = load_config()

    assert not hasattr(cfg, "valuation_confidence_shadow")
