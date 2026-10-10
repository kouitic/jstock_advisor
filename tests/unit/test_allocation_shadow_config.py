"""購入側 Shadow(Q')の設定モデルと専用 loader の契約テスト(Issue #603)。

固定するもの:
  - 出荷値は mode=OFF、時間のパラメータは USER 承認値(120 / 30 / 5 秒)
  - 設定が無い・壊れている・不正なら OFF へ縮退する(例外を送出しない)
  - 引用符なしの `OFF`(YAML 1.1 の真偽値 False)でも OFF へ縮退し、SHADOW とは決して読まれない
  - SHADOW と読まれるのは `mode: "SHADOW"` を明示したときだけ
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from jstock_advisor.domain.signals import allocation_shadow_config as cfg_module
from jstock_advisor.domain.signals.allocation_shadow_config import (
    CONFIG_FILE_NAME,
    AllocationShadowConfig,
    AllocationShadowMode,
    load_allocation_shadow_config,
    off_config,
)

_REPO_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _write(tmp_path: Path, text: str) -> Path:
    (tmp_path / CONFIG_FILE_NAME).write_text(text, encoding="utf-8")
    return tmp_path


def test_shipped_config_is_off_with_the_approved_time_parameters() -> None:
    config = load_allocation_shadow_config(_REPO_CONFIG_DIR)

    assert config.mode is AllocationShadowMode.OFF
    assert config.enabled is False
    assert config.min_remaining_seconds == 120.0
    assert config.time_limit_seconds == 30.0
    assert config.io_timeout_seconds == 5.0


def test_shipped_config_loads_without_falling_back(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=cfg_module.__name__)

    load_allocation_shadow_config(_REPO_CONFIG_DIR)

    assert [r for r in caplog.records if r.name == cfg_module.__name__] == []


def test_default_model_is_off() -> None:
    assert AllocationShadowConfig().enabled is False
    assert off_config().mode is AllocationShadowMode.OFF


def test_only_an_explicit_shadow_enables(tmp_path: Path) -> None:
    config = load_allocation_shadow_config(_write(tmp_path, 'version: 1\nmode: "SHADOW"\n'))

    assert config.enabled is True
    # 時間のパラメータは書かなければ承認値のまま
    assert (config.min_remaining_seconds, config.time_limit_seconds, config.io_timeout_seconds) == (
        120.0,
        30.0,
        5.0,
    )


def test_time_parameters_can_be_overridden(tmp_path: Path) -> None:
    config = load_allocation_shadow_config(
        _write(
            tmp_path,
            'mode: "SHADOW"\nmin_remaining_seconds: 200\n'
            "time_limit_seconds: 10\nio_timeout_seconds: 2\n",
        )
    )

    assert (config.min_remaining_seconds, config.time_limit_seconds, config.io_timeout_seconds) == (
        200.0,
        10.0,
        2.0,
    )


def test_missing_file_falls_back_to_off(tmp_path: Path) -> None:
    assert load_allocation_shadow_config(tmp_path) == off_config()


@pytest.mark.parametrize(
    "text",
    [
        "",  # 空
        "mode: [unterminated",  # YAML として壊れている
        "- a\n- b\n",  # mapping ではない
        'mode: "BOGUS"\n',  # 未知の mode
        'mode: "SHADOW"\nunexpected_key: 1\n',  # extra=forbid
        'mode: "SHADOW"\nmin_remaining_seconds: 0\n',  # gt=0
        'mode: "SHADOW"\ntime_limit_seconds: -1\n',
        'mode: "SHADOW"\nio_timeout_seconds: "x"\n',
        'mode: "shadow"\n',  # 大文字小文字は区別する(曖昧な値を SHADOW と読まない)
    ],
)
def test_broken_or_invalid_config_falls_back_to_off(tmp_path: Path, text: str) -> None:
    config = load_allocation_shadow_config(_write(tmp_path, text))

    assert config.enabled is False
    assert config == off_config()


def test_unquoted_off_is_never_read_as_shadow(tmp_path: Path) -> None:
    """引用符なしの OFF は YAML 1.1 で False になる。検証に失敗して OFF へ縮退する。"""
    config = load_allocation_shadow_config(_write(tmp_path, "mode: OFF\n"))

    assert config.enabled is False


def test_fallback_warns_with_the_error_type_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=cfg_module.__name__)

    load_allocation_shadow_config(_write(tmp_path, 'mode: "BOGUS"\n'))

    messages = [r.getMessage() for r in caplog.records if r.name == cfg_module.__name__]
    assert len(messages) == 1
    assert "ValidationError" in messages[0]
    assert "BOGUS" not in messages[0]  # 値・path は出さない


def test_config_is_frozen() -> None:
    config = AllocationShadowConfig()
    with pytest.raises(Exception):  # noqa: B017, PT011 - pydantic の frozen 違反
        config.mode = AllocationShadowMode.SHADOW  # type: ignore[misc]
