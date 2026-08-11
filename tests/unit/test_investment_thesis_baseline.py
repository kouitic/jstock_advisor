"""Baseline活性化・4分岐の判定・冪等性のテスト(実装プラン2節・3節)。"""

import datetime as dt
from pathlib import Path

from jstock_advisor.domain.entities.enums import BaselineOrigin, ExecutionMode
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holding_decision import BaselineValueSnapshot
from jstock_advisor.infrastructure.aws.baseline_pointer import get_pointer
from jstock_advisor.infrastructure.aws.baseline_sequence import allocate_next_baseline_version
from jstock_advisor.services.investment_thesis_service import InvestmentThesisService

_VALUES = BaselineValueSnapshot(total_yield_pct=4.0, equity_ratio_pct=45.0)
_VALIDATION = ExecutionContext(mode=ExecutionMode.VALIDATION)


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


def test_validation_activation_does_not_persist_baseline_pointer_or_sequence(store_dir: Path):
    """通知検証モード コードレビュー対応: VALIDATIONではactivate_baseline()が
    本番と同等のbaselineを返しつつ、baseline repository/pointer/sequence
    いずれへも一切書き込まないことを検証する。
    """
    service = InvestmentThesisService(store_dir=store_dir, execution_context=_VALIDATION)

    created = service.activate_baseline("7203", "7203", BaselineOrigin.SYSTEM_INITIALIZED, _VALUES)
    assert created.version == 1

    # baseline repositoryへは一切保存されない
    assert service._baseline_repo.list_by_holding("7203") == []  # noqa: SLF001
    # baseline pointerも作成されない
    assert get_pointer("7203", store_dir) is None
    # baseline sequenceカウンタも消費されない(消費されていれば次回は2が返るはず)
    assert allocate_next_baseline_version("7203", store_dir) == 1

    # get_active_baselineは(sequenceが未消費のため)引き続き「履歴無し」を返す
    lookup = service.get_active_baseline("7203")
    assert lookup.baseline is None
    assert lookup.integrity_error is False


def test_validation_get_or_create_thesis_does_not_persist(store_dir: Path):
    """通知検証モード コードレビュー対応: VALIDATIONではInvestmentThesis未存在時、
    プロセス内限りのtransientなthesisを返しつつ本番へは一切保存しない。
    """
    service = InvestmentThesisService(store_dir=store_dir, execution_context=_VALIDATION)
    now = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)

    thesis = service.get_or_create_thesis("7203", "7203", now)
    assert thesis.holding_id == "7203"
    assert thesis.stock_code == "7203"

    assert service.get_thesis("7203") is None


def test_normal_mode_explicit_context_still_persists_baseline_and_thesis(store_dir: Path):
    """NORMAL回帰確認: ExecutionContext.normal()を明示的に渡した場合も、
    従来どおりbaseline/pointer/sequence/thesisすべてが本番へ保存される。
    """
    service = InvestmentThesisService(
        store_dir=store_dir, execution_context=ExecutionContext.normal()
    )
    now = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)

    created = service.activate_baseline("7203", "7203", BaselineOrigin.SYSTEM_INITIALIZED, _VALUES)
    assert service._baseline_repo.list_by_holding("7203") != []  # noqa: SLF001
    assert get_pointer("7203", store_dir) is not None
    assert get_pointer("7203", store_dir).active_baseline_id == created.baseline_id

    thesis = service.get_or_create_thesis("7203", "7203", now)
    assert service.get_thesis("7203") is not None
    assert service.get_thesis("7203").investment_thesis_id == thesis.investment_thesis_id
