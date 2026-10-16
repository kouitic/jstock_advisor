"""資金入替の観測 P の mode の設定と専用 loader(Issue #604 P-b4 A1。dormant)。

mode は `config/rotation_observation.yaml` から読む。★ 環境変数・イベントの値では決めない ★
(P 固有の環境変数は 0 件。購入側 Shadow の `allocation_shadow.yaml` とは別の設定・別の入口)。

* mode は `RotationObserverMode`(OFF / SHADOW)だけ。★ ACTIVE は存在しない ★
* 設定が無い・読めない・不正(未知の mode・余計なキー・型違い)なら OFF へ縮退する(fail-closed。
  観測の設定の不備で、観測が意図せず ON になることも、Lambda が落ちることもない)
* 例外を送出せず、原因は型名だけを警告する(値・path は出さない)

## AppConfig(共通部品 S-13)へ載せない理由

`AppConfig` へ field を足すと全領域の lock 検討が要る(development_workflow 2.6.5)。#160 / #582 /
#603 と同じ理由で、専用のモデルと専用 loader にする。
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from jstock_advisor.config.loader import DEFAULT_CONFIG_DIR, _load_yaml
from jstock_advisor.services.rotation_observation_runner import RotationObserverMode

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CONFIG_FILE_NAME = "rotation_observation.yaml"


class RotationObservationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    mode: RotationObserverMode = RotationObserverMode.OFF


def off_config() -> RotationObservationConfig:
    """観測を実行しない設定(fail-closed の縮退先)。"""
    return RotationObservationConfig()


def load_rotation_observation_config(config_dir: Path | None = None) -> RotationObservationConfig:
    """設定を読む。無い・読めない・不正なら mode=OFF へ縮退する(例外を送出しない)。"""
    path = (config_dir or DEFAULT_CONFIG_DIR) / CONFIG_FILE_NAME
    try:
        return RotationObservationConfig.model_validate(_load_yaml(path))
    except Exception as exc:  # noqa: BLE001 - 観測の設定の不備で落ちない。型名だけを警告する
        logger.warning(
            "rotation_observation config unavailable; falling back to mode=OFF (%s)",
            type(exc).__name__,
        )
        return off_config()
