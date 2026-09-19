"""判断の安全条件(shadow計測)の設定モデルと専用loader(Issue #160 PR-0)。

## 位置づけ

shadow = 判定・通知・保存を変えずに、「安全条件を適用していたら何件がどうなったか」を観測する
機構。本モジュールは**設定だけ**を持ち、判定経路のどこにも接続されていない(挙動不変)。

## AppConfig(共通部品S-13)へ載せない理由

`AppConfig`へfieldを足すと全領域のlock検討が要る(development_workflow 2.6.5)。#160のPR-B
(全領域lock・1本)とは別に、全領域lockを増やさないため、専用のモデルと専用loaderにした。
shadowのmodeを読むのは、PR-3で配線するhandler合流点のみである。

## fail-closed

設定が無い・読めない・不正な場合は`mode=OFF`へ縮退する(shadowを実行しない側)。shadowの設定の不備が
本流の起動を落としてはならない(新旧Lambdaの混在・ロールバックでも本流不変)。

## G3の必須入力(USER決定 U1/U3/U4)

UNKNOWNを事実として識別できる2項目に限る。識別できない項目
(`fair_value_rising_with_earnings_growth` / `long_term_holding_benefit_imminent` /
`few_reinvestment_alternatives`)は測定不能であり、Falseを不明と推測しないため、ここへは置けない
(`Literal`で型として拒否する)。
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from jstock_advisor.config.loader import DEFAULT_CONFIG_DIR, _load_yaml

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CONFIG_FILE_NAME = "judgment_safety_shadow.yaml"

#: UNKNOWN(None)を事実として識別できるMitigatingFactorInputsのfield(profit_taking_service.py)。
G3MeasurableInput = Literal["continuous_dividend_increase_years", "is_progressive_or_doe_policy"]

_DEFAULT_G3_INPUTS: tuple[G3MeasurableInput, ...] = (
    "continuous_dividend_increase_years",
    "is_progressive_or_doe_policy",
)


class ShadowMode(StrEnum):
    OFF = "OFF"
    SHADOW = "SHADOW"


class JudgmentSafetyShadowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    mode: ShadowMode = ShadowMode.OFF
    g3_required_inputs: tuple[G3MeasurableInput, ...] = Field(
        default=_DEFAULT_G3_INPUTS, min_length=1
    )

    @property
    def enabled(self) -> bool:
        return self.mode is ShadowMode.SHADOW


def off_config() -> JudgmentSafetyShadowConfig:
    """shadowを実行しない設定(fail-closedの縮退先)。"""
    return JudgmentSafetyShadowConfig()


def load_judgment_safety_shadow_config(
    config_dir: Path | None = None,
) -> JudgmentSafetyShadowConfig:
    """設定を読む。無い・読めない・不正ならmode=OFFへ縮退する(例外を送出しない)。"""
    path = (config_dir or DEFAULT_CONFIG_DIR) / CONFIG_FILE_NAME
    try:
        return JudgmentSafetyShadowConfig.model_validate(_load_yaml(path))
    except Exception as exc:  # 本流を落とさない。原因の型だけを警告する(値・pathは出さない)
        logger.warning(
            "judgment_safety_shadow config unavailable; falling back to mode=OFF (%s)",
            type(exc).__name__,
        )
        return off_config()
