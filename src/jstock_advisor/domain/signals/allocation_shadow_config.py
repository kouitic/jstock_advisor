"""購入側 Shadow(Portfolio Allocation。Q')の設定モデルと専用 loader(Issue #603)。

## 位置づけ

#128 の Portfolio Allocation(#603)を、買い候補バッチの finalize が完了した後に、Production の判断へ
一切接続せずに観測する『枠』の設定。既定は OFF で、設定が無い・読めない・不正な場合も OFF へ縮退する
(fail-closed。shadow の設定の不備が本流の起動を落としてはならない)。

## AppConfig(共通部品 S-13)へ載せない理由

`AppConfig` へ field を足すと全領域の lock 検討が要る(development_workflow 2.6.5)。#160 / #582 と
同じ理由で、専用のモデルと専用 loader にする。

## 時間のパラメータ(USER 承認: #122 の Q' の最終設計。#128 …6100461428)

  min_remaining_seconds  実行前に必要な Lambda の残り時間。これ未満ならスキップして記録する(120 秒)
  time_limit_seconds     Shadow 処理そのものの協調的な期限(30 秒)。強制停止ではない
  io_timeout_seconds     外部 I/O 1 回あたりの待ち時間の上限(5 秒)。待ち切れなければ打ち切る
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from jstock_advisor.config.loader import DEFAULT_CONFIG_DIR, _load_yaml

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CONFIG_FILE_NAME = "allocation_shadow.yaml"


class AllocationShadowMode(StrEnum):
    OFF = "OFF"
    SHADOW = "SHADOW"


class AllocationShadowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    mode: AllocationShadowMode = AllocationShadowMode.OFF
    min_remaining_seconds: float = Field(default=120.0, gt=0)
    time_limit_seconds: float = Field(default=30.0, gt=0)
    io_timeout_seconds: float = Field(default=5.0, gt=0)

    @property
    def enabled(self) -> bool:
        return self.mode is AllocationShadowMode.SHADOW


def off_config() -> AllocationShadowConfig:
    """shadow を実行しない設定(fail-closed の縮退先)。"""
    return AllocationShadowConfig()


def load_allocation_shadow_config(config_dir: Path | None = None) -> AllocationShadowConfig:
    """設定を読む。無い・読めない・不正なら mode=OFF へ縮退する(例外を送出しない)。"""
    path = (config_dir or DEFAULT_CONFIG_DIR) / CONFIG_FILE_NAME
    try:
        return AllocationShadowConfig.model_validate(_load_yaml(path))
    except Exception as exc:  # 本流を落とさない。原因の型だけを警告する(値・path は出さない)
        logger.warning(
            "allocation_shadow config unavailable; falling back to mode=OFF (%s)",
            type(exc).__name__,
        )
        return off_config()
