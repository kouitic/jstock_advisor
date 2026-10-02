"""Issue #667(HF-2): 買い候補日次バッチの銘柄単位technical failureをHANDLED_FAILUREとして
USER通知することの確認。

対象のcatch boundary(Issue #667設計、fresh確認済み行番号):

    B1  _process_single_candidate()本体の想定外例外(1銘柄の分析失敗)
    B3  _save_evaluation_record_safely()の保存失敗
    B4  _update_evaluation_record_outcome_safely()の更新失敗

いずれも「既存のisolation契約(他銘柄・batch全体への非伝播)は変更しない」ことと、
「HANDLED_FAILURE通知自体の失敗が本処理を一切妨げない」ことを確認する
(HF2-AC1〜AC3。#665のHF-0契約に接続する)。

batch単位の集約は、新しい永続カウンタ(infrastructure/aws/batch_tracker.pyの
schema変更)を追加せず、#665のincident_notifier_handler.py側に既にある
fingerprint dedup(claim window)へ委ねる設計とした(各catch境界のfingerprintは
stock_codeを含まない固定値のため、同一batch内の複数銘柄の失敗は実質的に
1件のLINE通知へ収束する)。この設計判断は#667のPhase B設計コメントの原案
(batch_tracker.py側への新規カウンタ追加)からの実装時の変更であり、
PR本文で明示する。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.entities.enums import CandidateSource
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers import buy_candidates_handler as handler_module

_NOW = dt.datetime(2026, 10, 2, 0, 0, tzinfo=dt.UTC)
_CONFIG = object()


class _RaisingEvaluationRecordRepo:
    """upsert/get双方が常に例外を送出するフェイク(保存障害を模す)。

    tests/unit/test_buy_candidate_evaluation_record_repository.pyの同名
    フェイクと同じ契約(本ファイルはHF-2のHANDLED_FAILURE通知の確認に特化し、
    保存障害自体の既存回帰テストは重複させない)。
    """

    def upsert(self, record: object) -> None:
        raise RuntimeError("boom")

    def get(self, evaluation_id: str) -> None:
        raise RuntimeError("boom")

    def list_by_stock(self, stock_code: str) -> list[object]:
        raise RuntimeError("boom")


class _FakeNotificationService:
    def notify_single_candidate(self, *args: object, **kwargs: object) -> bool:
        return False


# --- _notify_handled_failure() / _notify_handled_failure_safely() のcontract -----------


def test_notify_handled_failure_publishes_allowlisted_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        handler_module, "publish_incident_envelope", lambda envelope: captured.update(envelope)
    )

    handler_module._notify_handled_failure(
        "CANDIDATE_ANALYSIS", "BUY_CANDIDATES_ANALYSIS_FAILED", _NOW
    )

    assert captured["source"] == "buy_candidates"
    assert captured["job_name"] == "buy-candidates"
    assert captured["failure_stage"] == "CANDIDATE_ANALYSIS"
    assert captured["reason_code"] == "BUY_CANDIDATES_ANALYSIS_FAILED"
    assert captured["failure_class"] == "HANDLED_FAILURE"
    # HF2-AC3: 個別の投資情報(stock_code等)を一切含まない。
    assert "stock_code" not in captured


def test_notify_handled_failure_safely_swallows_publish_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(envelope: dict[str, Any]) -> None:
        raise RuntimeError("SNS unavailable")

    monkeypatch.setattr(handler_module, "publish_incident_envelope", _boom)

    with caplog.at_level("WARNING"):
        handler_module._notify_handled_failure_safely(
            "CANDIDATE_ANALYSIS", "X", _NOW
        )  # 例外を投げないことが検証

    assert "failed to publish HANDLED_FAILURE envelope" in caplog.text


# --- B1: _process_single_candidate()本体の想定外例外 --------------------------------


def test_b1_unexpected_analysis_failure_notifies_handled_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    notified: list[tuple[str, str]] = []
    monkeypatch.setattr(
        handler_module,
        "_notify_handled_failure_safely",
        lambda stage, reason, now: notified.append((stage, reason)),
    )

    def _boom(self: object, *args: object, **kwargs: object) -> None:
        raise RuntimeError("analysis boom")

    monkeypatch.setattr(handler_module.BuySignalService, "analyze", _boom)
    recommendation_repo = RecommendationRepository(store_dir=tmp_path)

    result = handler_module._process_single_candidate(
        "2914",
        CandidateSource.WATCHLIST,
        None,
        None,
        "batch-1",
        _NOW,
        object(),
        _CONFIG,
        object(),
        recommendation_repo,
        _FakeNotificationService(),
        handler_module._DEFAULT_EXECUTION_CONTEXT,
        None,
    )

    assert result == {
        "stock_code": "2914",
        "recommended": False,
        "notified": False,
        "failed": True,
    }
    assert notified == [("CANDIDATE_ANALYSIS", "BUY_CANDIDATES_ANALYSIS_FAILED")]


def test_b1_normal_analysis_does_not_notify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """HF2-AC2相当: 失敗が無ければ追加通知は発生しない。"""
    notified: list[tuple[str, str]] = []
    monkeypatch.setattr(
        handler_module,
        "_notify_handled_failure_safely",
        lambda stage, reason, now: notified.append((stage, reason)),
    )
    monkeypatch.setattr(handler_module, "build_stock_snapshot", lambda *a, **kw: (object(), None))

    from jstock_advisor.domain.entities.common import BuyPriceLevels, PriceWithRationale
    from jstock_advisor.domain.entities.enums import BuyAction, ConfidenceLevel, RecommendationType
    from jstock_advisor.domain.entities.recommendation import Recommendation
    from jstock_advisor.services.buy_signal_service import BuyAnalysisOutcome

    recommendation = Recommendation(
        recommendation_id="rec-1",
        stock_code="2914",
        stock_name="銘柄2914",
        recommended_at=_NOW,
        recommendation_type=RecommendationType.BUY,
        buy_prices=BuyPriceLevels(entry=PriceWithRationale(price=Decimal("3500"), rationale="x")),
        price_at_recommendation=Decimal("4200"),
        confidence=ConfidenceLevel.HIGH,
        rule_version="v1-mvp",
        buy_action=BuyAction.BUY,
        base_buy_action=BuyAction.BUY,
    )
    outcome = BuyAnalysisOutcome(
        stock_code="2914",
        recommendation=recommendation,
        screening_passed=True,
        exclusion_reasons=[],
        data_error=None,
        buy_action=BuyAction.BUY,
        ranking_group="buy_candidate",
    )
    monkeypatch.setattr(handler_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)
    recommendation_repo = RecommendationRepository(store_dir=tmp_path)

    handler_module._process_single_candidate(
        "2914",
        CandidateSource.WATCHLIST,
        None,
        None,
        None,
        _NOW,
        object(),
        _CONFIG,
        object(),
        recommendation_repo,
        _FakeNotificationService(),
        handler_module._DEFAULT_EXECUTION_CONTEXT,
        None,
    )

    assert notified == []


# --- B3: _save_evaluation_record_safely()の保存失敗 ---------------------------------


def test_b3_evaluation_record_save_failure_notifies_handled_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notified: list[tuple[str, str]] = []
    monkeypatch.setattr(
        handler_module,
        "_notify_handled_failure_safely",
        lambda stage, reason, now: notified.append((stage, reason)),
    )

    from jstock_advisor.domain.entities.enums import BuyAction, PurchaseCategory

    saved = handler_module._save_evaluation_record_safely(
        _RaisingEvaluationRecordRepo(),  # type: ignore[arg-type]
        "batch-1",
        "2914",
        _NOW,
        "v1-mvp",
        CandidateSource.WATCHLIST,
        PurchaseCategory.BUY_CANDIDATE,
        BuyAction.BUY,
        BuyAction.BUY,
        "rec-1",
        ExecutionContext.normal(),
    )

    assert saved is False
    assert notified == [("EVALUATION_RECORD_SAVE", "BUY_CANDIDATES_EVALUATION_RECORD_SAVE_FAILED")]


# --- B4: _update_evaluation_record_outcome_safely()の更新失敗 ------------------------


def test_b4_outcome_update_failure_notifies_handled_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notified: list[tuple[str, str]] = []
    monkeypatch.setattr(
        handler_module,
        "_notify_handled_failure_safely",
        lambda stage, reason, now: notified.append((stage, reason)),
    )

    handler_module._update_evaluation_record_outcome_safely(
        _RaisingEvaluationRecordRepo(),  # type: ignore[arg-type]
        "batch-1",
        "2914",
        1,
        None,
        False,
        "OUTSIDE_TOP_5",
        "OUTSIDE_TOP_5",
        (),
        None,
        ExecutionContext.normal(),
    )

    assert notified == [
        (
            "NOTIFICATION_OUTCOME_RECORD_UPDATE",
            "BUY_CANDIDATES_NOTIFICATION_OUTCOME_RECORD_UPDATE_FAILED",
        )
    ]


def test_b4_record_not_found_does_not_notify_handled_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """finalize時点で対応する行が無い(= B3で既に保存に失敗していた等)ケースは、
    既存どおりWARNINGログのみで、本Issueの新規HANDLED_FAILURE通知の対象にしない
    (二重通知を避ける。B3側で既に通知済みのはず)。
    """
    from jstock_advisor.infrastructure.local_repository.buy_candidate_evaluation_record_repository import (  # noqa: E501
        BuyCandidateEvaluationRecordRepository,
    )

    notified: list[tuple[str, str]] = []
    monkeypatch.setattr(
        handler_module,
        "_notify_handled_failure_safely",
        lambda stage, reason, now: notified.append((stage, reason)),
    )
    eval_repo = BuyCandidateEvaluationRecordRepository(store_dir=tmp_path)

    handler_module._update_evaluation_record_outcome_safely(
        eval_repo,
        "batch-1",
        "9999",
        1,
        None,
        False,
        "OUTSIDE_TOP_5",
        "OUTSIDE_TOP_5",
        (),
        None,
        ExecutionContext.normal(),
    )

    assert notified == []
