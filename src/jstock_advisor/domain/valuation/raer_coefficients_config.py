"""RAER の係数の専用 loader(Issue #602 PR-E。dormant)。

`config/raer_coefficients.yaml`(承認済みの初期値。USER #122 …6114461065【1】A-4)を読み、
B2 の `RaerCoefficients` へ渡す。**式・検証・識別子は B2 の module が持つ**(`raer_composition.py`)。
この module は『YAML → RaerCoefficients』の変換と縮退だけを持ち、式も、独自の hash も持たない
(`coefficient_set_id` は B2 の property をそのまま使う = 識別子の二重化を避ける)。

## 縮退(fail-closed)

ファイルが無い・読めない・壊れている・未知のキー・型の不正・モデル版の不一致・B2 の検証が拒否、の
いずれも `coefficients = None` へ縮退する(例外を送出しない。Shadow の設定の不備が本流を落とさない)。
**部分的に効いた設定を作らない**: 1 項目でも不正なら全体を縮退し、既定値・0 で補完しない。
縮退のときの扱い(RAER を作らず、Shadow の理由として記録する)は PR-F が決める。

## ★ 『有効化の入口』ではない・『未決だから動かない』でもない

この module と YAML は、どこからも import されない(dormant)。係数は A-1〜A-5 ですべて決定済みで、
未決の値は無い。使われない理由は次の 3 つだけで、『値が決まっていないから』ではない:
  ① 配線がない ② 入力の供給が未実装 ③ Shadow の mode が OFF
有効化の判断の前提を取り違えないこと。

## AppConfig(共通部品 S-13)へ載せない理由

`AppConfig` へ field を足すと全領域の lock 検討が要る。`allocation_shadow_config.py` と同じく
専用 loader にし、`config.loader` の `_load_yaml` と `DEFAULT_CONFIG_DIR` だけを使う
(既存の許容された依存)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, StrictFloat, StrictInt, StrictStr

from jstock_advisor.config.loader import DEFAULT_CONFIG_DIR, _load_yaml
from jstock_advisor.domain.entities.enums import ConfidenceLevel
from jstock_advisor.domain.valuation.raer import COMBINED_MODEL_VERSION, AxisCode, RiskFlag
from jstock_advisor.domain.valuation.raer_composition import RaerCoefficients, to_decimal

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CONFIG_FILE_NAME: Final = "raer_coefficients.yaml"
SUPPORTED_FILE_VERSION: Final = 1

# bool・文字列・None を拒否する数値(YAML の `yes` / `"1"` を黙って数にしない)
_Number = StrictInt | StrictFloat


class _QualityFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    deduction_max: _Number
    max_score: _Number


class _CoefficientsFile(BaseModel):
    """YAML の形(キーと型)だけを検査する。値の整合は B2 の `RaerCoefficients` が検証する。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: StrictInt
    model_version: StrictStr
    fv_confidence_factor: dict[StrictStr, _Number]
    quality: _QualityFile
    risk_deductions: dict[StrictStr, _Number]
    risk_deduction_cap: _Number
    trend_deduction_scale: _Number
    min_applied_axes: StrictInt
    required_axes: list[StrictStr]


@dataclass(frozen=True)
class RaerCoefficientsBundle:
    """読み込みの結果。`coefficients = None` は縮退(使えない)。"""

    coefficients: RaerCoefficients | None
    coefficient_set_id: str | None  # B2 の property をそのまま(縮退では None)
    model_version: str | None
    source: Literal["FILE", "FALLBACK"]


def _fallback() -> RaerCoefficientsBundle:
    return RaerCoefficientsBundle(
        coefficients=None, coefficient_set_id=None, model_version=None, source="FALLBACK"
    )


def _to_coefficients(raw: _CoefficientsFile) -> RaerCoefficients:
    if raw.version != SUPPORTED_FILE_VERSION:
        raise ValueError("未対応の version")
    if raw.model_version != COMBINED_MODEL_VERSION:
        # 係数だけが黙って変わらないよう、モデル版が一致しない YAML は読まない(A-4)
        raise ValueError("モデル版が一致しない")
    return RaerCoefficients(
        fv_confidence_factor={
            ConfidenceLevel(key): to_decimal(value, f"fv_confidence_factor.{key}")
            for key, value in raw.fv_confidence_factor.items()
        },
        quality_deduction_max=to_decimal(raw.quality.deduction_max, "quality.deduction_max"),
        quality_max_score=to_decimal(raw.quality.max_score, "quality.max_score"),
        risk_deductions={
            RiskFlag(key): to_decimal(value, f"risk_deductions.{key}")
            for key, value in raw.risk_deductions.items()
        },
        risk_deduction_cap=to_decimal(raw.risk_deduction_cap, "risk_deduction_cap"),
        trend_deduction_scale=to_decimal(raw.trend_deduction_scale, "trend_deduction_scale"),
        min_applied_axes=raw.min_applied_axes,
        required_axes=frozenset(AxisCode(axis) for axis in raw.required_axes),
    )


def load_raer_coefficients(config_dir: Path | None = None) -> RaerCoefficientsBundle:
    """係数を読む。例外を送出しない(不備は縮退。値・path は警告に出さない)。"""
    path = (config_dir or DEFAULT_CONFIG_DIR) / CONFIG_FILE_NAME
    try:
        coefficients = _to_coefficients(_CoefficientsFile.model_validate(_load_yaml(path)))
    except Exception as exc:  # 本流を落とさない。原因の型だけを警告する
        logger.warning(
            "raer coefficients unavailable; falling back to no coefficients (%s)",
            type(exc).__name__,
        )
        return _fallback()
    return RaerCoefficientsBundle(
        coefficients=coefficients,
        coefficient_set_id=coefficients.coefficient_set_id,
        model_version=COMBINED_MODEL_VERSION,
        source="FILE",
    )
