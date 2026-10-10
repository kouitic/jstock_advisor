"""Expected Return / RAER の Shadow の器(設定型・実行計画・排他制御)の契約テスト(#602 PR-1)。

## この PR の範囲(USER 承認: #122 issuecomment-6100082651。設計: #602 rev1 …6100131042)
  ExpectedReturnRuntimeConfig / ExpectedReturnExecutionPlan の型と、実行計画を作る純粋関数。
  ★ 永続化(表・IAM・repository)・RAER の計算・shadow の配線・設定を ACTIVE にする経路は含めない。

## 固定するもの
  (0) 先行(characterization): 本件の型が依存する既存の契約(RuntimeConfigMode の値・Entity /
      ImmutableSnapshot の性質・require_timezone_aware)は変更しない
  (1) 実行モードの排他制御: mode と 3 つの flag の組を厳密に対応づける。allow_allocation_use が
      True になるのは ACTIVE のときだけ(全 mode × 全 flag の網羅)
  (2) 設定が無い(未作成)= LEGACY 相当(計算も記録もしない)
  (3) 構造: 時計・available_cash・取得単価を持たない / 誰からも import されない(dormant)
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from jstock_advisor.domain.entities.base import Entity, ImmutableSnapshot
from jstock_advisor.domain.entities.enums import RuntimeConfigMode
from jstock_advisor.domain.jst import require_timezone_aware

# --- (0) 先行: 本件の型が依存する既存の契約は変更しない ------------------------------------------


def test_runtime_config_mode_values_are_pinned() -> None:
    """plan の対応表(mode -> flag の組)は、この 3 値を前提にする。増減したら対応表も見直す。"""
    assert [(m.name, m.value) for m in RuntimeConfigMode] == [
        ("LEGACY", "legacy"),
        ("SHADOW", "shadow"),
        ("ACTIVE", "active"),
    ]


def test_entity_forbids_unknown_fields_and_validates_assignment() -> None:
    class _Probe(Entity):
        value: int

    probe = _Probe(value=1)
    with pytest.raises(ValidationError):
        _Probe(value=1, extra_field=2)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        probe.value = "not-an-int"  # type: ignore[assignment]


def test_immutable_snapshot_is_frozen_and_forbids_unknown_fields() -> None:
    class _Probe(ImmutableSnapshot):
        value: int

    probe = _Probe(value=1)
    with pytest.raises(ValidationError):
        probe.value = 2
    with pytest.raises(ValidationError):
        _Probe(value=1, extra_field=2)  # type: ignore[call-arg]


def test_require_timezone_aware_rejects_naive_and_accepts_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        require_timezone_aware(dt.datetime(2026, 10, 12, 8, 0))
    require_timezone_aware(dt.datetime(2026, 10, 12, 8, 0, tzinfo=dt.UTC))
