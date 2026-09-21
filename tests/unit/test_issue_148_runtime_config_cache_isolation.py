"""Issue #148: 保有判断の RuntimeConfig cache を、テスト間で持ち越さない。

`holding_decision_runtime_config_service` は、取得に成功した設定をモジュールレベルの
`_cached_config` / `_cached_at` へ保持し、後続の取得失敗では安全側の既定値(LEGACY)ではなく
この値を使う。テスト間でリセットされないと、先行テストが書いた mode(SHADOW / ACTIVE)が
別の保存先で動く後続テストへ漏れ、新エンジンが呼ばれて偽の失敗になる(実行順序で結果が変わる)。

`tests/unit/conftest.py` の autouse fixture が、各テストの前後でcacheを破棄する。本ファイルは、
その隔離を固定する(fixtureが外れる・片方の変数だけを消す、といった回帰で赤くなる)。

不変条件を固定する:
  * 各テストの開始時に、cacheは空である(直前のテストが残した値を見ない)。
  * `_cached_config` と `_cached_at` は常に対で消える(片方だけ残らない)。
  * 隔離があっても、cache自体の挙動(取得失敗時に直近の値を使う)は変わらない。
  * 先行テストが SHADOW / ACTIVE を残しても、後続の取得失敗は LEGACY へ倒れる。

★ テストのみ。src・Production・configは変更しない。値はすべて架空。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import RuntimeConfigMode
from jstock_advisor.services import holding_decision_runtime_config_service as mod
from jstock_advisor.services.holding_decision_runtime_config_service import (
    HoldingDecisionRuntimeConfigService,
)
from tests.unit.conftest import reset_holding_decision_runtime_config_cache


def _cache_state() -> tuple[object, object]:
    """cacheの2つのモジュール変数を、そのまま(型の絞り込みなしで)読む。"""
    return (mod._cached_config, mod._cached_at)


def _leak_a_non_legacy_config(store_dir: Path) -> None:
    """先行テストが行う「SHADOWを設定して、取得に成功してcacheへ格納する」を再現する。"""
    service = HoldingDecisionRuntimeConfigService(store_dir=store_dir)
    service.init_config(updated_by="tester", mode=RuntimeConfigMode.SHADOW)
    lookup = service.get_config()
    assert lookup.config.mode == RuntimeConfigMode.SHADOW
    assert mod._cached_config is not None  # 実際にcacheへ格納された(= 持ち越しうる状態)


def test_a_leaves_a_shadow_config_in_the_process_cache(tmp_path: Path) -> None:
    """先行テスト役。SHADOWをcacheへ残したまま終わる(隔離が無ければ、次のテストへ漏れる)。"""
    _leak_a_non_legacy_config(tmp_path)


def test_b_starts_with_an_empty_cache_even_after_a_leaky_test() -> None:
    """★ 直前のテストがSHADOWを残していても、開始時にcacheは空である(定義順で a の直後に走る)。"""
    assert mod._cached_config is None
    assert mod._cached_at is None


def test_c_a_failed_lookup_falls_back_to_legacy_not_to_a_leaked_mode(tmp_path: Path) -> None:
    """★ 別の保存先(レコード無し)の取得失敗は、持ち越した値ではなく LEGACY になる。"""
    lookup = HoldingDecisionRuntimeConfigService(store_dir=tmp_path).get_config()

    assert lookup.is_fallback is True
    assert lookup.config.mode == RuntimeConfigMode.LEGACY


def test_the_two_cache_variables_are_always_cleared_together(tmp_path: Path) -> None:
    _leak_a_non_legacy_config(tmp_path)
    assert mod._cached_config is not None
    assert mod._cached_at is not None

    reset_holding_decision_runtime_config_cache()

    assert _cache_state() == (None, None)


def test_the_cache_itself_still_works_within_one_test(tmp_path: Path) -> None:
    """隔離は、テストの前後だけ。1つのテストの中では、従来どおり直近の値を使う(cacheの挙動は不変)。"""
    _leak_a_non_legacy_config(tmp_path)

    # 同じテストの中で、別の保存先(レコード無し)の取得が失敗すると、直近のcacheを使う(従来の挙動)。
    other = tmp_path / "other"
    other.mkdir()
    lookup = HoldingDecisionRuntimeConfigService(store_dir=other).get_config()

    assert lookup.config.mode == RuntimeConfigMode.SHADOW


def test_the_isolation_is_an_autouse_fixture_applied_without_being_requested(
    request: pytest.FixtureRequest,
) -> None:
    """隔離が opt-in ではなく autouse であることを固定する(要求しなくても各テストに適用される)。"""
    assert "_isolated_holding_decision_runtime_config_cache" in request.fixturenames
