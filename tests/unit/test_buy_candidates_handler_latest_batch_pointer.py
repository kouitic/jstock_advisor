"""LINE UI第二弾「対象確認」機能(2026-08)向け、latest completed batch
pointerの更新条件のテスト。

`_finalize_batch()`は以下2条件を両方満たす場合のみpointerを更新する:
  1. execution_context.mode == ExecutionMode.NORMAL(VALIDATION/DRY_RUNでは
     絶対に更新しない)
  2. そのbatchの全対象銘柄についてBuyCandidateEvaluationRecordの保存が
     実際に成功している(evaluation_record_saved_stock_codesの件数が
     progress.totalと一致)

既存の`test_buy_candidates_handler.py`の`_finalize_batch`テスト群と同じ
パターン(独立ファイル化、フィクスチャ自己完結)を踏襲する。ランキング対象が
0件のシンプルなbatch(dispatch対象は存在するが、購入判定ランキングに乗る
銘柄が無いケース)で検証する — pointer更新条件はランキング結果の有無とは
独立した関心事のため。
"""

from __future__ import annotations

import datetime as dt
import logging

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import ExecutionMode, NotificationMode
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.infrastructure.local_repository.latest_buy_candidate_batch_pointer_repository import (  # noqa: E501
    LatestBuyCandidateBatchPointerRepository,
)
from jstock_advisor.lambda_handlers import buy_candidates_handler as handler_module

_NOW = dt.datetime(2026, 8, 24, 7, 0, tzinfo=dt.UTC)
_CONFIG = load_config()


class _NoopAuditService:
    def record(self, *args: object, **kwargs: object) -> None:
        return None


class _FakeNotificationService:
    def notify_buy_candidates_digest(self, winners: list[object], now: object) -> dict[str, str]:
        return {}

    def notify_batch_summary(self, *args: object, **kwargs: object) -> bool:
        return True


def _progress(total: int, evaluation_record_saved_stock_codes: list[str]):
    return handler_module.BatchProgress(
        total=total,
        completed=total,
        category_counts={},
        data_insufficient_stock_codes=[],
        failed_stock_codes=[],
        ranking_entries=[],
        sector_entries=[],
        holding_count=0,
        evaluation_record_saved_stock_codes=evaluation_record_saved_stock_codes,
    )


def _patch_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler_module, "AuditService", lambda *a, **kw: _NoopAuditService())


def test_pointer_updated_when_normal_and_all_evaluation_records_saved(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _patch_audit(monkeypatch)
    pointer_repo = LatestBuyCandidateBatchPointerRepository(store_dir=tmp_path)
    progress = _progress(total=3, evaluation_record_saved_stock_codes=["1111", "2222", "3333"])

    handler_module._finalize_batch(
        progress,
        "batch-normal-full",
        _CONFIG,
        _NOW,
        recommendation_repo=None,  # type: ignore[arg-type]
        notification_service=_FakeNotificationService(),
        execution_context=ExecutionContext(mode=ExecutionMode.NORMAL),
        latest_batch_pointer_repo=pointer_repo,
    )

    pointer = pointer_repo.get()
    assert pointer is not None
    assert pointer.latest_completed_batch_id == "batch-normal-full"
    assert pointer.total_candidates == 3


def test_pointer_not_updated_when_evaluation_records_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_audit(monkeypatch)
    pointer_repo = LatestBuyCandidateBatchPointerRepository(store_dir=tmp_path)
    progress = _progress(total=3, evaluation_record_saved_stock_codes=["1111", "2222"])

    with caplog.at_level(logging.ERROR, logger=handler_module.logger.name):
        handler_module._finalize_batch(
            progress,
            "batch-incomplete",
            _CONFIG,
            _NOW,
            recommendation_repo=None,  # type: ignore[arg-type]
            notification_service=_FakeNotificationService(),
            execution_context=ExecutionContext(mode=ExecutionMode.NORMAL),
            latest_batch_pointer_repo=pointer_repo,
        )

    assert pointer_repo.get() is None
    assert any("latest batch pointer NOT updated" in record.message for record in caplog.records)


def test_pointer_not_updated_when_validation_mode_even_if_all_saved(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _patch_audit(monkeypatch)
    pointer_repo = LatestBuyCandidateBatchPointerRepository(store_dir=tmp_path)
    progress = _progress(total=2, evaluation_record_saved_stock_codes=["1111", "2222"])

    handler_module._finalize_batch(
        progress,
        "batch-validation",
        _CONFIG,
        _NOW,
        recommendation_repo=None,  # type: ignore[arg-type]
        notification_service=_FakeNotificationService(),
        execution_context=ExecutionContext(mode=ExecutionMode.VALIDATION),
        latest_batch_pointer_repo=pointer_repo,
    )

    assert pointer_repo.get() is None


def test_pointer_not_updated_when_dry_run_mode_even_if_all_saved(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """VALIDATION+DRY_RUNはis_validationも真になるが、mode自体の直接比較で
    確実に除外されることを明示的に確認する(is_validation/is_dry_runという
    派生プロパティの意味論に依存しない)。"""
    _patch_audit(monkeypatch)
    pointer_repo = LatestBuyCandidateBatchPointerRepository(store_dir=tmp_path)
    progress = _progress(total=2, evaluation_record_saved_stock_codes=["1111", "2222"])

    handler_module._finalize_batch(
        progress,
        "batch-dry-run",
        _CONFIG,
        _NOW,
        recommendation_repo=None,  # type: ignore[arg-type]
        notification_service=_FakeNotificationService(),
        execution_context=ExecutionContext(
            mode=ExecutionMode.VALIDATION, notification_mode=NotificationMode.DRY_RUN
        ),
        latest_batch_pointer_repo=pointer_repo,
    )

    assert pointer_repo.get() is None


def test_pointer_preserves_previous_value_when_new_batch_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """一部欠損のbatchが来ても、直前の正常完了batchのpointerが上書きされず
    維持されること。"""
    _patch_audit(monkeypatch)
    pointer_repo = LatestBuyCandidateBatchPointerRepository(store_dir=tmp_path)

    handler_module._finalize_batch(
        _progress(total=2, evaluation_record_saved_stock_codes=["1111", "2222"]),
        "batch-good",
        _CONFIG,
        _NOW,
        recommendation_repo=None,  # type: ignore[arg-type]
        notification_service=_FakeNotificationService(),
        execution_context=ExecutionContext(mode=ExecutionMode.NORMAL),
        latest_batch_pointer_repo=pointer_repo,
    )
    handler_module._finalize_batch(
        _progress(total=3, evaluation_record_saved_stock_codes=["1111"]),
        "batch-bad",
        _CONFIG,
        _NOW + dt.timedelta(hours=1),
        recommendation_repo=None,  # type: ignore[arg-type]
        notification_service=_FakeNotificationService(),
        execution_context=ExecutionContext(mode=ExecutionMode.NORMAL),
        latest_batch_pointer_repo=pointer_repo,
    )

    pointer = pointer_repo.get()
    assert pointer is not None
    assert pointer.latest_completed_batch_id == "batch-good"
