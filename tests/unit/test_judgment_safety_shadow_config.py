"""Issue #160 PR-0: shadow設定(専用モデル + 専用loader)。挙動不変・fail-closedを固定する。

時刻・営業日には触れない(TIME_SEMANTICS_IMPACT = NO)。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from jstock_advisor.config.loader import _load_yaml
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


def test_shipped_config_passes_validation_without_falling_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """出荷ファイルがfallbackを経ずに検証を通ること(fallbackの値と偶然一致しても通してはならない)。

    modeを引用符なしで書くと、YAML 1.1では真偽値Falseと解釈されて検証に失敗し、ファイル全体が
    読まれなくなる(g3_required_inputsの編集も効かなくなる)。fallbackの返す値(OFF・2項目)は
    出荷ファイルの値と一致するため、結果の値だけでは「読めた」と「読めずに落ちた」を区別できない。
    """
    caplog.set_level(logging.DEBUG, logger=shadow_cfg.__name__)
    path = _REPO_ROOT / "config" / CONFIG_FILE_NAME

    # fallbackを経ない直接の検証(不正なら例外で赤になる)
    parsed = JudgmentSafetyShadowConfig.model_validate(_load_yaml(path))
    loaded = load_judgment_safety_shadow_config(_REPO_ROOT / "config")

    assert loaded == parsed
    assert [r for r in caplog.records if r.name == shadow_cfg.__name__] == []  # 警告なし
    assert isinstance(_load_yaml(path)["mode"], str)  # 真偽値として解釈されていない


def test_shipped_config_values_are_exactly_what_the_file_says() -> None:
    """出荷ファイルの値そのものを検証する(fallbackの既定値と区別できる形で、ファイルから導出)。"""
    raw = _load_yaml(_REPO_ROOT / "config" / CONFIG_FILE_NAME)

    cfg = load_judgment_safety_shadow_config(_REPO_ROOT / "config")

    assert raw["mode"] == "OFF"
    assert cfg.mode is ShadowMode.OFF
    assert list(cfg.g3_required_inputs) == raw["g3_required_inputs"]
    assert set(cfg.g3_required_inputs) == set(_TWO_INPUTS)  # U1/U3/U4: この2項目のみ


def test_editing_the_g3_list_in_a_copy_of_the_shipped_file_changes_the_result(
    tmp_path: Path,
) -> None:
    """ファイルが実際に効いていること: 出荷ファイルの写しのg3リストを編集すると結果が変わる。"""
    text = (_REPO_ROOT / "config" / CONFIG_FILE_NAME).read_text(encoding="utf-8")
    edited = text.replace("  - is_progressive_or_doe_policy\n", "")
    assert edited != text
    (tmp_path / CONFIG_FILE_NAME).write_text(edited, encoding="utf-8")

    cfg = load_judgment_safety_shadow_config(tmp_path)

    assert cfg.g3_required_inputs == ("continuous_dividend_increase_years",)


def test_unquoted_off_is_a_boolean_in_yaml_and_is_rejected_not_silently_accepted(
    tmp_path: Path,
) -> None:
    """引用符なしのOFFはYAML 1.1でFalse。検証に失敗する(この事故を明示的に固定する)。"""
    raw = _load_yaml(_write(tmp_path, "mode: OFF\n") / CONFIG_FILE_NAME)

    assert raw["mode"] is False
    with pytest.raises(ValueError, match="mode"):
        JudgmentSafetyShadowConfig.model_validate(raw)


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
    """設定は判定経路のどこからも参照されない(挙動不変)。参照してよいのは、自身も未接続の純関数のみ。

    PR-1で`judgment_safety.py`(純関数。それ自体を参照する本番コードは0件で、
    test_judgment_safety.pyが固定する)が参照元に加わった。接続はPR-3以降。
    """
    referrers = [
        p.relative_to(_REPO_ROOT).as_posix()
        for p in (_REPO_ROOT / "src").rglob("*.py")
        if p.name != "judgment_safety_shadow_config.py"
        and "judgment_safety_shadow_config" in p.read_text(encoding="utf-8")
    ]

    assert referrers == ["src/jstock_advisor/domain/signals/judgment_safety.py"]


def test_app_config_is_unchanged_and_does_not_carry_the_shadow_block() -> None:
    """S-13(AppConfig)へは載せない: load_configは従来どおりでshadowを持たない。"""
    from jstock_advisor.config.loader import load_config

    cfg = load_config()

    assert not hasattr(cfg, "judgment_safety_shadow")


def test_duplicate_g3_inputs_are_rejected_and_fall_back_to_off(tmp_path: Path) -> None:
    text = (
        'mode: "SHADOW"\n'
        "g3_required_inputs:\n"
        "  - continuous_dividend_increase_years\n"
        "  - continuous_dividend_increase_years\n"
    )

    with pytest.raises(ValueError, match="duplicates"):
        JudgmentSafetyShadowConfig.model_validate(
            {"mode": "SHADOW", "g3_required_inputs": ["is_progressive_or_doe_policy"] * 2}
        )
    assert load_judgment_safety_shadow_config(_write(tmp_path, text)) == off_config()
