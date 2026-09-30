"""適正価格信頼度(valuation_confidence)shadow計測の設定モデルと専用loader(Issue #582)。

## 位置づけ

`determine_valuation_confidence()`のHIGH tierは、`industry_model_applied`が
恒久的にFalseであることにより(#208)Productionで一度も到達していない。
`require_industry_model=False`を渡した場合に判定・価格・BuyActionがどう
変わるかを、v1の判定・通知・保存を一切変えずに観測するための設定を持つ
(#160の専用configモデル・loaderと同型)。

## AppConfig(共通部品S-13)へ載せない理由

`AppConfig`へfieldを足すと全領域のlock検討が要る(development_workflow 2.6.5)。
#160と同じ理由で、専用のモデルと専用loaderにする。

## fail-closed

設定が無い・読めない・不正な場合は`mode=OFF`へ縮退する(shadowを実行しない側)。
shadowの設定の不備が本流の起動を落としてはならない。
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from jstock_advisor.config.loader import DEFAULT_CONFIG_DIR, _load_yaml

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CONFIG_FILE_NAME = "valuation_confidence_shadow.yaml"


class ShadowMode(StrEnum):
    OFF = "OFF"
    SHADOW = "SHADOW"


class ValuationConfidenceShadowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    mode: ShadowMode = ShadowMode.OFF

    @property
    def enabled(self) -> bool:
        return self.mode is ShadowMode.SHADOW


def off_config() -> ValuationConfidenceShadowConfig:
    """shadowを実行しない設定(fail-closedの縮退先)。"""
    return ValuationConfidenceShadowConfig()


def load_valuation_confidence_shadow_config(
    config_dir: Path | None = None,
) -> ValuationConfidenceShadowConfig:
    """設定を読む。無い・読めない・不正ならmode=OFFへ縮退する(例外を送出しない)。"""
    path = (config_dir or DEFAULT_CONFIG_DIR) / CONFIG_FILE_NAME
    try:
        return ValuationConfidenceShadowConfig.model_validate(_load_yaml(path))
    except Exception as exc:  # 本流を落とさない。原因の型だけを警告する(値・pathは出さない)
        logger.warning(
            "valuation_confidence_shadow config unavailable; falling back to mode=OFF (%s)",
            type(exc).__name__,
        )
        return off_config()
