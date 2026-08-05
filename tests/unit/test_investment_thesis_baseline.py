"""Baseline活性化・4分岐の判定・冪等性のテスト(実装プラン2節・3節)。"""

from pathlib import Path

from jstock_advisor.domain.entities.enums import BaselineOrigin
from jstock_advisor.domain.entities.holding_decision import BaselineValueSnapshot
from jstock_advisor.services.investment_thesis_service import InvestmentThesisService

_VALUES = BaselineValueSnapshot(total_yield_pct=4.0, equity_ratio_pct=45.0)


def test_no_history_returns_none_without_integrity_error(store_dir: Path):
    service = InvestmentThesisService(store_dir=store_dir)
    result = service.get_active_baseline("7203")
    assert result.baseline is None
    assert result.integrity_error is False


def test_activation_creates_and_resolves_baseline(store_dir: Path):
    service = InvestmentThesisService(store_dir=store_dir)
    created = service.activate_baseline("7203", "7203", BaselineOrigin.SYSTEM_INITIALIZED, _VALUES)
    assert created.version == 1

    lookup = service.get_active_baseline("7203")
    assert lookup.baseline is not None
    assert lookup.baseline.baseline_id == created.baseline_id
    assert lookup.integrity_error is False


def test_second_activation_supersedes_first(store_dir: Path):
    service = InvestmentThesisService(store_dir=store_dir)
    first = service.activate_baseline("7203", "7203", BaselineOrigin.SYSTEM_INITIALIZED, _VALUES)
    second = service.activate_baseline(
        "7203", "7203", BaselineOrigin.HUMAN_APPROVED, _VALUES, approved_by="kouichi"
    )
    assert second.version == 2
    assert second.supersedes_baseline_id == first.baseline_id

    lookup = service.get_active_baseline("7203")
    assert lookup.baseline.baseline_id == second.baseline_id

    # 旧versionは削除されず、履歴として残る
    history = service._baseline_repo.list_by_holding("7203")  # noqa: SLF001
    assert {b.baseline_id for b in history} == {first.baseline_id, second.baseline_id}


def test_history_present_but_pointer_missing_is_integrity_error(store_dir: Path, monkeypatch):
    service = InvestmentThesisService(store_dir=store_dir)
    service.activate_baseline("7203", "7203", BaselineOrigin.SYSTEM_INITIALIZED, _VALUES)

    # ポインタだけを人為的に消し、「履歴はあるがポインタ無し」状態を再現する。
    from jstock_advisor.infrastructure.aws import baseline_pointer

    store = baseline_pointer._store(store_dir)  # noqa: SLF001
    store.delete("7203")

    lookup = service.get_active_baseline("7203")
    assert lookup.baseline is None
    assert lookup.integrity_error is True


def test_pointer_resolves_to_missing_baseline_is_integrity_error(store_dir: Path):
    service = InvestmentThesisService(store_dir=store_dir)
    service.activate_baseline("7203", "7203", BaselineOrigin.SYSTEM_INITIALIZED, _VALUES)

    from jstock_advisor.infrastructure.aws import baseline_pointer

    store = baseline_pointer._store(store_dir)  # noqa: SLF001
    pointer = store.get("7203")
    corrupted = pointer.model_copy(update={"active_baseline_version": 999})
    store.upsert(corrupted)

    lookup = service.get_active_baseline("7203")
    assert lookup.baseline is None
    assert lookup.integrity_error is True


def test_different_holding_ids_have_independent_baselines(store_dir: Path):
    service = InvestmentThesisService(store_dir=store_dir)
    a = service.activate_baseline("AAA", "AAA", BaselineOrigin.SYSTEM_INITIALIZED, _VALUES)
    b = service.activate_baseline("BBB", "BBB", BaselineOrigin.SYSTEM_INITIALIZED, _VALUES)
    assert a.version == 1
    assert b.version == 1
    assert a.baseline_id != b.baseline_id
