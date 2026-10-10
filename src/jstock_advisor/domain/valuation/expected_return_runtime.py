"""Expected Return / RAER の Shadow の器: 設定型・実行計画・実行モードの排他制御(Issue #602 PR-1)。

#128 の Child 3b(#602。RAER)を、Production の判断へ接続する前に **Shadow で観測できる**ように
するための『器』だけを持つ。**dormant**(どこからも import されない)で、永続化(表・IAM・
repository)・RAER の計算(PR-B)・shadow の配線(PR-C)・設定を ACTIVE にする経路は含めない。

## 実行モード(`RuntimeConfigMode` を再利用)と実行計画

  mode     compute_raer  record_shadow  allow_allocation_use
  LEGACY   False         False          False   計算も記録もしない(既定。設定が無い場合もこれ)
  SHADOW   True          True           False   計算して記録するが、資金配分(#603)は使わない
  ACTIVE   True          True           True    資金配分が RAER を使ってよい

``allow_allocation_use`` が True になるのは **ACTIVE のときだけ**。これは USER の案 Y の必須条件
(『案 Y は Shadow 評価の実施のためで、投資判断への利用の承認ではない』)を**構造で**強制するもの:
不整合な組み合わせの `ExpectedReturnExecutionPlan` は**構築できない**。

★ ACTIVE へ進めるのは #606 の gate(shadow -> backtest -> human review -> 明示的な USER 承認)の
  後であり、この module はその gate を実装しない(運用の gate)。型は構造だけを強制し、
  **設定を ACTIVE にする新しい経路を作らない**(書込・永続化・CLI・handler は持たない)。

## 持たないもの(契約テストで固定)

  時計・営業日(updated_at は呼び出し側が渡す値で、tz-aware の検証のみ)/ available_cash・取得単価
  (#884 の PortfolioContext と同じ規約)/ 通知の許可(`notification_enabled`。RAER は通知を作らない)/
  財務の方針の上書き / services・infrastructure・lambda_handlers・providers・cli・config への依存。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

from pydantic import Field, model_validator

from jstock_advisor.domain.entities.base import Entity, ImmutableSnapshot
from jstock_advisor.domain.entities.enums import RuntimeConfigMode
from jstock_advisor.domain.jst import require_timezone_aware

CONFIG_ID: Final = "expected_return"

# mode -> (compute_raer, record_shadow, allow_allocation_use)。実行計画の唯一の対応表。
_FLAGS_BY_MODE: Final[Mapping[RuntimeConfigMode, tuple[bool, bool, bool]]] = MappingProxyType(
    {
        RuntimeConfigMode.LEGACY: (False, False, False),
        RuntimeConfigMode.SHADOW: (True, True, False),
        RuntimeConfigMode.ACTIVE: (True, True, True),
    }
)


class ExpectedReturnRuntimeConfig(Entity):
    """再デプロイ不要で切り替える運用パラメータ(将来の専用表の 1 行)。この PR は型だけを持つ。

    `HoldingDecisionRuntimeConfig` と違い `notification_enabled`(RAER は通知を作らない)と
    財務の方針の上書きを持たない。
    """

    config_id: Literal["expected_return"] = CONFIG_ID
    config_version: int = Field(ge=1)
    mode: RuntimeConfigMode
    updated_at: dt.datetime
    updated_by: str = Field(min_length=1)
    change_reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_updated_at_is_timezone_aware(self) -> ExpectedReturnRuntimeConfig:
        require_timezone_aware(self.updated_at)
        return self


class ExpectedReturnExecutionPlan(ImmutableSnapshot):
    """実行モードごとの『計算するか・記録するか・資金配分が使ってよいか』(独立した 3 つの flag)。

    不整合な組み合わせは構築できない(構造で強制)。`resolve_expected_return_execution_plan()` が
    唯一の構築経路で、mode から一意に決まる。
    """

    mode: RuntimeConfigMode
    compute_raer: bool
    record_shadow: bool
    allow_allocation_use: bool

    @model_validator(mode="after")
    def _check_invariants(self) -> ExpectedReturnExecutionPlan:
        if self.allow_allocation_use and not self.compute_raer:
            raise ValueError(
                "ExpectedReturnExecutionPlan: allow_allocation_use は compute_raer が False の"
                "ときは True にできない(計算しない値を資金配分は使えない)"
            )
        if self.record_shadow and not self.compute_raer:
            raise ValueError(
                "ExpectedReturnExecutionPlan: record_shadow は compute_raer が False の"
                "ときは True にできない(計算しない値は記録できない)"
            )
        if self.allow_allocation_use and self.mode is not RuntimeConfigMode.ACTIVE:
            raise ValueError(
                "ExpectedReturnExecutionPlan: allow_allocation_use は ACTIVE のときだけ True に"
                "できる(SHADOW は資金配分に使わない。投資判断への利用は別の USER 承認)"
            )
        expected = _FLAGS_BY_MODE[self.mode]
        actual = (self.compute_raer, self.record_shadow, self.allow_allocation_use)
        if actual != expected:
            raise ValueError(
                f"ExpectedReturnExecutionPlan: mode={self.mode.value} の flag の組は {expected}"
                f" でなければならない(実際: {actual})"
            )
        return self


def resolve_expected_return_execution_plan(
    config: ExpectedReturnRuntimeConfig | None,
) -> ExpectedReturnExecutionPlan:
    """設定から実行計画を一意に決める(純粋関数)。

    設定が無い(未作成)は LEGACY 相当 = 計算も記録もしない(fail-closed。設定の不在を SHADOW や
    ACTIVE として扱わない)。
    """
    mode = RuntimeConfigMode.LEGACY if config is None else config.mode
    compute_raer, record_shadow, allow_allocation_use = _FLAGS_BY_MODE[mode]
    return ExpectedReturnExecutionPlan(
        mode=mode,
        compute_raer=compute_raer,
        record_shadow=record_shadow,
        allow_allocation_use=allow_allocation_use,
    )
