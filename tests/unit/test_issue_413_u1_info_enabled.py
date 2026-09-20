"""Issue #493(#413 U-1): shareholder_benefit_registry_service の INFO 出力。

`check_registry_health()` は登録件数を INFO で常時記録する(CSV の取込漏れの観測)。
module が `logger.setLevel(logging.INFO)` を宣言せず、Lambda では一度も出力されなかった。
ここでは次を確認する。

    1 宣言が実際に効く: Lambda の root logger の既定(WARNING)のもとでも、INFO が有効になる。
    2 INFO が実際に出力され、件数だけを含む。
    3 優待の内容・銘柄コードを出力しない。**架空の値を、実際に読まれるデータ(登録した優待)へ置く**。
      置いていないデータで「出力に現れない」と確認しても、常に真になる(#413 PR-3 の教訓)。
    4 既存の WARNING / exception の行が変わっていない(件数が少ないとき WARNING / 取得失敗で ERROR)。

宣言があること自体は tests/unit/test_issue_413_logger_level_declared.py(#413 の guard)が見る。
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from jstock_advisor.domain.entities.enums import BenefitUtilityCategory
from jstock_advisor.infrastructure.local_repository.shareholder_benefit_registry_repository import (
    ShareholderBenefitRegistryRepository,
)
from jstock_advisor.services import shareholder_benefit_registry_service as registry
from jstock_advisor.services.shareholder_benefit_registry_service import (
    ShareholderBenefitRegistryService,
    check_registry_health,
)

#: 実在しない架空値。出力に現れたら、優待の内容・銘柄を出している。
_FAKE_CODE = "0000"
_FAKE_DESCRIPTION = "架空優待A-説明"
_FAKE_TIER = "架空ティア-1"

_MODULE = registry.__name__


def _service_with_entries(tmp_path: Path, count: int) -> ShareholderBenefitRegistryService:
    service = ShareholderBenefitRegistryService(ShareholderBenefitRegistryRepository(tmp_path))
    for i in range(count):
        service.register(
            stock_code=f"{_FAKE_CODE[:-1]}{i}",
            min_shares_required=100,
            frequency_per_year=2,
            category=next(iter(BenefitUtilityCategory)),
            description=_FAKE_DESCRIPTION,
            min_shares_for_tier=100,
            tier_group=_FAKE_TIER,
        )
    return service


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _MODULE and r.levelno == level]


def _leaked(messages: list[str]) -> list[str]:
    """出力のうち、優待の内容・銘柄を含むもの。"""
    return [
        m for m in messages if any(v in m for v in (_FAKE_DESCRIPTION, _FAKE_TIER, _FAKE_CODE[:-1]))
    ]


def test_info_is_enabled_even_when_the_root_logger_is_at_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lambda の root 既定(WARNING)に左右されず、INFO が有効になる(宣言が効いている)。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)

    assert logging.getLogger().getEffectiveLevel() == logging.WARNING
    assert logging.getLogger(_MODULE).isEnabledFor(logging.INFO)
    assert not logging.getLogger(_MODULE).isEnabledFor(logging.DEBUG)


def test_health_check_emits_info_with_the_count_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    service = _service_with_entries(tmp_path, 3)

    with caplog.at_level(logging.INFO, logger=_MODULE):
        check_registry_health(min_expected_entries=1, service=service)

    infos = _messages(caplog, logging.INFO)
    assert infos == ["ShareholderBenefitRegistry loaded 3 entries."]
    assert _messages(caplog, logging.WARNING) == []


def test_output_never_contains_registered_benefit_content_or_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """架空値は、実際に読まれるデータ(list_all が返す登録済みの優待)に入っている。"""
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    service = _service_with_entries(tmp_path, 2)
    assert any(  # 前提: 架空値が読まれるデータに実際に入っている(無ければ検査は常に真)
        _FAKE_DESCRIPTION in b.description for e in service.list_all() for b in e.benefits
    )

    with caplog.at_level(logging.DEBUG, logger=_MODULE):
        check_registry_health(min_expected_entries=5, service=service)  # WARNING の経路も通す

    every = [r.getMessage() for r in caplog.records if r.name == _MODULE]
    assert every  # 何も出ていなければ、検査が成立していない
    assert _leaked(every) == []


def test_the_leak_check_itself_turns_red_when_content_is_logged() -> None:
    """検査自体の確認: 優待の内容を含む行が出れば、_leaked が検出する(注入した漏れを見逃さない)。"""
    leaking = [f"ShareholderBenefitRegistry loaded entry {_FAKE_CODE[:-1]}1 {_FAKE_DESCRIPTION}"]
    assert _leaked(leaking) == leaking


def test_existing_warning_and_error_paths_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(logging.getLogger(), "level", logging.WARNING)
    service = _service_with_entries(tmp_path, 1)

    with caplog.at_level(logging.INFO, logger=_MODULE):
        check_registry_health(min_expected_entries=5, service=service)
    assert len(_messages(caplog, logging.INFO)) == 1  # 件数は常に INFO
    warnings = _messages(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "loaded 1 entries (expected at least 5)" in warnings[0]

    class _Failing(ShareholderBenefitRegistryService):
        def list_all(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_MODULE):
        check_registry_health(min_expected_entries=5, service=_Failing())
    assert _messages(caplog, logging.INFO) == []  # 取得に失敗したら件数は出さない
    errors = _messages(caplog, logging.ERROR)
    assert len(errors) == 1
    assert "event=shareholder_benefit_registry_health_check_failed" in errors[0]


def test_every_info_call_site_passes_only_a_count() -> None:
    """構造の歯止め: この module の INFO の引数は件数だけ。内容・銘柄を渡す行を足すと赤くなる。"""
    tree = ast.parse(Path(registry.__file__).read_text(encoding="utf-8"))
    infos = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "info"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "logger"
    ]
    assert len(infos) == 1
    (call,) = infos
    args = [a.id if isinstance(a, ast.Name) else type(a).__name__ for a in call.args[1:]]
    assert args == ["count"]
