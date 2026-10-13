"""購入側の資金配分の方針値の専用 loader(Issue #603 PR-E)。

## 位置づけ

PR-A の型(`AllocationConstraints` / `AllocationPolicy`)に、`config/allocation_policy.yaml` の
承認済みの値(判断 B。#122 …6114810773【3】)を入れて渡す『設定の読み込み』だけである。計算(PR-C の
`optimize_allocation`)・入力の組み立て(PR-F1)・Shadow への差し込み(PR-F2)は含まない。

**どこからも import されない(dormant)**。Production の判断・通知・保存・環境変数・IAM を変えない。
有効化の入口ではない(mode / enabled の類を持たない)。
Shadow を動かす入口は `allocation_shadow.yaml`。

## 未決定の扱い(fail-closed)

- ファイルが無い・読めない・形式が不正・値が 1 つでも不正 → **全項目を『未決定(None)』へ縮退**
  (= `POLICY_INCOMPLETE` = 配分しない)。例外を送出しない(Shadow の設定の不備が本流を落とさない)。
  部分的に効いた設定を作らないため、不正な値が 1 つでもあれば、決定済みの他の値も使わない。
- 項目が `null` / 欠落 → **その項目だけ未決定**。0・既定値で補完しない
  (出荷の `comparison_key` は #602 PR-G2 で設定済み。null にすると配分しない側へ倒れる)。
- 警告に値・path を出さない(例外の型名だけ。#135)。

## 値の読み方

数値は YAML の int / float だけを受け(bool・文字列・NaN・±inf は不正)、`Decimal(str(x))` へ直す
(float の誤差を持ち込まない。0.15 は 0.15 そのもの)。比率は (0, 1]、金額は有限の正の数。

## AppConfig(共通部品 S-13)へ載せない理由

`AppConfig` へ field を足すと全領域の lock 検討が要る(development_workflow 2.6.5)。
`allocation_shadow_config.py` / `incident_issue_limits.py` と同じ理由で、
専用のモデルと専用 loader にする。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from jstock_advisor.config.loader import DEFAULT_CONFIG_DIR, _load_yaml
from jstock_advisor.domain.signals.portfolio_allocation_types import (
    AllocationConstraints,
    AllocationPolicy,
    PriceTier,
)
from jstock_advisor.domain.valuation.raer import RiskFlag

logger = logging.getLogger(__name__)

CONFIG_FILE_NAME: Final = "allocation_policy.yaml"
SUPPORTED_VERSION: Final = 1

_RISK_FLAG_VALUES: Final = frozenset(flag.value for flag in RiskFlag)
_TIER_NAMES: Final = frozenset(tier.value for tier in PriceTier)


def _decimal(value: object) -> Decimal:
    """YAML の int / float を Decimal へ(bool・文字列・NaN・±inf は不正。float は str 経由)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("must be a number")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("must be finite")
    return number


def _ratio(value: object) -> Decimal:
    number = _decimal(value)
    if not (Decimal(0) < number <= Decimal(1)):
        raise ValueError("must be in (0, 1]")
    return number


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("must be a non-empty str")
    return value


class AllocationPolicyConfig(BaseModel):
    """`allocation_policy.yaml` の形。値の検証はここ(型・範囲)で、組み立ては `load_*` が行う。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    policy_id: str | None = None
    comparison_key: str | None = None
    min_raer_to_allocate: Decimal | None = None
    max_single_stock_amount: Decimal | None = None
    max_single_stock_ratio: Decimal | None = None
    max_sector_ratio: Decimal | None = None
    max_high_risk_ratio: Decimal | None = None
    high_risk_flags: tuple[str, ...] | None = None
    tier_quantity_caps: dict[PriceTier, Decimal] | None = None
    unknown_price_tier_as: PriceTier | None = None

    @field_validator("version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        # True・1.0 を version 1 と読まない
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("must be an int")
        return value

    @field_validator("policy_id", "comparison_key", mode="before")
    @classmethod
    def _names(cls, value: object) -> object:
        return None if value is None else _text(value)

    @field_validator("min_raer_to_allocate", mode="before")
    @classmethod
    def _any_finite(cls, value: object) -> object:
        return None if value is None else _decimal(value)

    @field_validator("max_single_stock_amount", mode="before")
    @classmethod
    def _positive_amount(cls, value: object) -> object:
        if value is None:
            return None
        number = _decimal(value)
        if number <= 0:  # 0 は『買えない』にも『制約なし』にも読めて危険
            raise ValueError("must be positive")
        return number

    @field_validator(
        "max_single_stock_ratio", "max_sector_ratio", "max_high_risk_ratio", mode="before"
    )
    @classmethod
    def _ratios(cls, value: object) -> object:
        return None if value is None else _ratio(value)

    @field_validator("high_risk_flags", mode="before")
    @classmethod
    def _flags(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, list) or any(not isinstance(flag, str) for flag in value):
            raise ValueError("must be a list of str")
        if len(set(value)) != len(value):
            raise ValueError("must not repeat a flag")
        if set(value) - _RISK_FLAG_VALUES:
            raise ValueError("has a value outside the vocabulary")
        return tuple(value)

    @field_validator("tier_quantity_caps", mode="before")
    @classmethod
    def _caps(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != _TIER_NAMES:
            raise ValueError("must cover exactly every tier")
        return {PriceTier(name): _ratio(cap) for name, cap in value.items()}


@dataclass(frozen=True)
class AllocationPolicyBundle:
    """方針の束。PR-C の `optimize_allocation` へ `constraints` と `policy` をそのまま渡す。"""

    constraints: AllocationConstraints
    policy: AllocationPolicy
    policy_id: str | None
    unknown_price_tier_as: PriceTier | None
    #: 正規化した値の SHA-256(64 桁)。記録に残し『どの閾値で計算したか』を後から検証する
    config_hash: str
    #: FILE = 読めた / FALLBACK = 全項目を未決定へ縮退した
    source: Literal["FILE", "FALLBACK"]

    def missing(self) -> tuple[str, ...]:
        """未決定の項目名(PR-A の `POLICY_ITEMS` の並び)。空でなければ配分しない。"""
        return self.policy.missing(self.constraints)


def _canonical(value: Decimal | None) -> str | None:
    # 0.15 と 0.150 と 15E-2 を同じ文字列にする(指数表記にしない)
    return None if value is None else format(value.normalize(), "f")


def _hash(
    *,
    policy_id: str | None,
    constraints: AllocationConstraints,
    policy: AllocationPolicy,
    unknown_price_tier_as: PriceTier | None,
) -> str:
    caps = policy.tier_quantity_caps
    payload: dict[str, Any] = {
        "version": SUPPORTED_VERSION,
        "policy_id": policy_id,
        "comparison_key": policy.comparison_key,
        "min_raer_to_allocate": _canonical(policy.min_raer_to_allocate),
        "max_single_stock_amount": _canonical(constraints.max_single_stock_amount),
        "max_single_stock_ratio": _canonical(constraints.max_single_stock_ratio),
        "max_sector_ratio": _canonical(constraints.max_sector_ratio),
        "max_high_risk_ratio": _canonical(constraints.max_high_risk_ratio),
        "high_risk_flags": sorted(constraints.high_risk_flags),
        "tier_quantity_caps": (
            None if caps is None else {tier.value: _canonical(caps[tier]) for tier in PriceTier}
        ),
        "unknown_price_tier_as": (
            None if unknown_price_tier_as is None else unknown_price_tier_as.value
        ),
        "minimum_trading_unit": constraints.minimum_trading_unit,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bundle(
    config: AllocationPolicyConfig | None,
    *,
    minimum_trading_unit: int,
    source: Literal["FILE", "FALLBACK"],
) -> AllocationPolicyBundle:
    cfg = config if config is not None else AllocationPolicyConfig(version=SUPPORTED_VERSION)
    constraints = AllocationConstraints(
        max_single_stock_amount=cfg.max_single_stock_amount,
        max_single_stock_ratio=cfg.max_single_stock_ratio,
        max_sector_ratio=cfg.max_sector_ratio,
        max_high_risk_ratio=cfg.max_high_risk_ratio,
        high_risk_flags=frozenset(cfg.high_risk_flags or ()),
        minimum_trading_unit=minimum_trading_unit,
    )
    policy = AllocationPolicy(
        min_raer_to_allocate=cfg.min_raer_to_allocate,
        comparison_key=cfg.comparison_key,
        tier_quantity_caps=(
            None if cfg.tier_quantity_caps is None else MappingProxyType(cfg.tier_quantity_caps)
        ),
    )
    policy_id = cfg.policy_id if config is not None else None
    return AllocationPolicyBundle(
        constraints=constraints,
        policy=policy,
        policy_id=policy_id,
        unknown_price_tier_as=cfg.unknown_price_tier_as,
        config_hash=_hash(
            policy_id=policy_id,
            constraints=constraints,
            policy=policy,
            unknown_price_tier_as=cfg.unknown_price_tier_as,
        ),
        source=source,
    )


def undecided_policy(*, minimum_trading_unit: int) -> AllocationPolicyBundle:
    """全項目が未決定(配分しない。fail-closed の縮退先)。"""
    return _bundle(None, minimum_trading_unit=minimum_trading_unit, source="FALLBACK")


def load_allocation_policy(
    config_dir: Path | None = None, *, minimum_trading_unit: int
) -> AllocationPolicyBundle:
    """方針の束を読む。無い・読めない・不正なら全項目を未決定へ縮退する(例外を送出しない)。

    `minimum_trading_unit` は呼び出し側が渡す(`profit_taking_rules.yaml` の売買単位。重複した値を
    持たない)。これが 1 未満・int 以外なら呼び出し側の誤りとして `AllocationContractError` を
    送出する(設定の不備ではないため、握り潰さない)。
    """
    # 呼び出し側の引数の検証(設定の不備と区別する)。縮退先も同じ検証を通る
    undecided = undecided_policy(minimum_trading_unit=minimum_trading_unit)
    path = (config_dir or DEFAULT_CONFIG_DIR) / CONFIG_FILE_NAME
    try:
        config = AllocationPolicyConfig.model_validate(_load_yaml(path))
        return _bundle(config, minimum_trading_unit=minimum_trading_unit, source="FILE")
    except Exception as exc:  # 本流を落とさない。原因の型だけを警告する(値・path は出さない)
        logger.warning(
            "allocation_policy config unavailable; treating every item as undecided (%s)",
            type(exc).__name__,
        )
        return undecided


__all__ = [
    "CONFIG_FILE_NAME",
    "SUPPORTED_VERSION",
    "AllocationPolicyBundle",
    "AllocationPolicyConfig",
    "load_allocation_policy",
    "undecided_policy",
]
