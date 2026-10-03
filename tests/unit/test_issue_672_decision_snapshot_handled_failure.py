"""Issue #672(HF-7): DecisionSnapshot保存失敗を、判定成功へ埋没させずUSERへ通知する。

`save_decision_snapshot_safely()`は保存失敗を例外にせずWARNINGログだけに留めるため、
判定・Recommendation保存・LINE通知が成功している限り、保存失敗は利用者に届かなかった。
本Issueでは、戻り値(保存失敗ではなかったか)を呼び出し元が見て、保存失敗のときだけ
HANDLED_FAILURE(HF-0契約。#665)をUSERへ通知する。判定・保存・通知の流れは変えない。

確認すること:

    A 戻り値の契約(経路ごとに1件ずつ、値で固定する)
        新規insert成功 / 同一内容の冪等再実行 / CONFLICT(想定内。失敗ではない) → True
        例外(構築・読み取り・insert) / insert_if_absent=Falseなのに強整合readでも存在しない → False
    B 呼び出し元4箇所(buy 1 + holdings_watchlist 3)
        保存失敗のときだけenvelopeが1件(allowlistのkeyのみ。
        failure_stage = DECISION_SNAPSHOT_SAVE#<batch_id>)
        成功・CONFLICT・VALIDATIONでは0件 / 判定・保存・戻り値は失敗の有無で変わらない
        通知の発行失敗が本処理を止めない(fail-soft)
    C 集約: 同一batchの複数失敗は同一fingerprint、別batchは別fingerprint(stageへのbatch_id埋め込み)

★ このテストは「実際のhandler関数を通し、実際の`save_decision_snapshot_safely()`を使う」。
  保存の成否は、差し替えたRepositoryの挙動で作る(戻り値を直接偽装しない)。
"""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from jstock_advisor.domain.decision_snapshot_builder import build_decision_snapshot
from jstock_advisor.domain.entities.enums import (
    BuyAction,
    CandidateSource,
    DecisionType,
    ExecutionMode,
)
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.infrastructure.local_repository.decision_snapshot_repository import (
    DecisionSnapshotRepository,
)
from jstock_advisor.infrastructure.local_repository.recommendation_repository import (
    RecommendationRepository,
)
from jstock_advisor.lambda_handlers import buy_candidates_handler as buy_module
from jstock_advisor.lambda_handlers import holdings_watchlist_handler as holdings_module
from jstock_advisor.services import decision_snapshot_service as service_module
from jstock_advisor.services.decision_snapshot_service import (
    DECISION_SNAPSHOT_CONFLICT_EVENT,
    DECISION_SNAPSHOT_SAVE_FAILED_EVENT,
    save_decision_snapshot_safely,
)
from tests.unit import test_buy_candidates_handler as buy_tests
from tests.unit import test_decision_snapshot_service as service_tests
from tests.unit import test_issue_528_holdings_duplicate_persistence as holdings_tests

_ENVELOPE_KEYS = {
    "source",
    "job_name",
    "failure_stage",
    "failure_type",
    "reason_code",
    "occurred_at",
    "failure_class",
}
_FORBIDDEN_KEYS = {"stock_code", "holding_id", "owner", "stock_name", "recommendation_id"}


# --- A 戻り値の契約 -------------------------------------------------------------------------


class _InsertRaisingRepository:
    """get_consistent()は未挿入(None)、insert_if_absent()が例外(ストレージ障害)。"""

    def get_consistent(self, decision_id: str) -> None:
        return None

    def insert_if_absent(self, decision: object) -> bool:
        raise RuntimeError("storage unavailable")


class _ReadRaisingRepository:
    def get_consistent(self, decision_id: str) -> None:
        raise RuntimeError("storage unavailable")

    def insert_if_absent(self, decision: object) -> bool:
        raise RuntimeError("storage unavailable")


class _ConflictRepository:
    """既存の記録が、これから保存しようとする内容と異なる(同じdecision_idで内容不一致)。"""

    def __init__(self, existing: Any) -> None:
        self._existing = existing

    def get_consistent(self, decision_id: str) -> Any:
        return self._existing

    def insert_if_absent(self, decision: object) -> bool:
        raise AssertionError("既存があるときはinsertしない")


def _save(repo: Any, logger_name: str) -> bool:
    return save_decision_snapshot_safely(
        repo, service_tests._recommendation(), DecisionType.BUY, logging.getLogger(logger_name)
    )


def test_a1_a_new_insert_returns_true(tmp_path: Path) -> None:
    assert _save(DecisionSnapshotRepository(store_dir=tmp_path), "t672.insert") is True


def test_a2_an_identical_rerun_returns_true(tmp_path: Path) -> None:
    repo = DecisionSnapshotRepository(store_dir=tmp_path)
    assert _save(repo, "t672.first") is True
    assert _save(repo, "t672.rerun") is True


def test_a3_a_conflict_returns_true_and_is_logged_as_conflict_not_save_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CONFLICTは既存の記録を正として保持する想定内の動作(保存失敗ではない)。"""
    existing = build_decision_snapshot(
        service_tests._recommendation(price_at_recommendation=Decimal("1300")),
        DecisionType.BUY,
    )
    with caplog.at_level(logging.WARNING, logger="t672.conflict"):
        assert _save(_ConflictRepository(existing), "t672.conflict") is True
    messages = [r.getMessage() for r in caplog.records]
    assert any(DECISION_SNAPSHOT_CONFLICT_EVENT in m for m in messages)
    assert not any(DECISION_SNAPSHOT_SAVE_FAILED_EVENT in m for m in messages)


def test_a4_a_race_lost_to_an_identical_or_conflicting_winner_returns_true() -> None:
    recommendation = service_tests._recommendation()
    identical = build_decision_snapshot(recommendation, DecisionType.BUY)
    conflicting = build_decision_snapshot(
        service_tests._recommendation(price_at_recommendation=Decimal("1300")),
        DecisionType.BUY,
    )
    for winner, name in ((identical, "t672.race_identical"), (conflicting, "t672.race_conflict")):
        repo = service_tests._RaceRepository(winner)
        assert (
            save_decision_snapshot_safely(
                repo, recommendation, DecisionType.BUY, logging.getLogger(name)
            )
            is True
        )


def test_a5_an_exception_on_read_returns_false() -> None:
    assert _save(_ReadRaisingRepository(), "t672.read_raises") is False


def test_a6_an_exception_on_insert_returns_false() -> None:
    assert _save(_InsertRaisingRepository(), "t672.insert_raises") is False


def test_a7_an_exception_while_building_the_snapshot_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _boom(recommendation: object, decision_type: object) -> None:
        raise RuntimeError("build failed")

    monkeypatch.setattr(service_module, "build_decision_snapshot", _boom)
    assert _save(DecisionSnapshotRepository(store_dir=tmp_path), "t672.build_raises") is False


def test_a8_a_missing_record_after_a_lost_insert_returns_false() -> None:
    """例外ではない第2の失敗経路: insert_if_absent=Falseなのに強整合readでも存在しない。"""
    assert _save(service_tests._RaceRepositoryPermanentlyMissing(), "t672.missing") is False


# --- B 呼び出し元4箇所 -----------------------------------------------------------------------


def _ok_repository_factory(tmp_path: Path) -> Any:
    return lambda: DecisionSnapshotRepository(store_dir=tmp_path / "snapshots")


def _failing_repository_factory() -> Any:
    return _ReadRaisingRepository


def _conflicting_repository_factory() -> Any:
    existing = build_decision_snapshot(
        service_tests._recommendation(price_at_recommendation=Decimal("1300")),
        DecisionType.BUY,
    )
    return lambda: _ConflictRepository(existing)


_BUY_REASON = "BUY_CANDIDATES_DECISION_SNAPSHOT_SAVE_FAILED"
_HOLDINGS_REASON = "HOLDINGS_WATCHLIST_DECISION_SNAPSHOT_SAVE_FAILED"


def _run_buy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repository_factory: Any,
    *,
    batch_id: str | None = "batch-672",
    execution_context: ExecutionContext | None = None,
    publish: Any = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    buy_tests._patch_snapshot(monkeypatch)
    buy_tests._patch_audit(monkeypatch)
    recommendation = buy_tests._make_recommendation(
        "2914", company_quality_score=72.5, recommendation_id="rec-672", buy_action=BuyAction.BUY
    )
    outcome = buy_tests._outcome(recommendation, ranking_group="buy_candidate")
    monkeypatch.setattr(buy_module.BuySignalService, "analyze", lambda self, *a, **kw: outcome)
    monkeypatch.setattr(buy_module, "record_result", lambda *a, **kw: None)
    monkeypatch.setattr(buy_module, "DecisionSnapshotRepository", repository_factory)
    envelopes: list[dict[str, Any]] = []
    monkeypatch.setattr(
        buy_module, "publish_incident_envelope", publish if publish else envelopes.append
    )
    repo = RecommendationRepository(store_dir=tmp_path / "recommendations")
    result = buy_module._process_single_candidate(
        "2914",
        CandidateSource.WATCHLIST,
        None,
        None,
        batch_id,
        buy_tests._NOW,
        object(),
        buy_tests._CONFIG,
        object(),
        repo,
        buy_tests._FakeNotificationServiceForRanking(),
        execution_context or ExecutionContext.normal(),
        buy_tests._SpyEvaluationRecordRepository(),
    )
    saved = [r.model_dump(mode="json") for r in repo.list_all()]
    return result, envelopes, saved


def _assert_allowlisted_envelope(envelope: dict[str, Any], *, stage: str, reason: str) -> None:
    assert set(envelope) == _ENVELOPE_KEYS
    assert not (_FORBIDDEN_KEYS & set(envelope))
    assert envelope["failure_stage"] == stage
    assert envelope["reason_code"] == reason
    assert envelope["failure_class"] == "HANDLED_FAILURE"
    assert envelope["failure_type"] == "UNEXPECTED_EXCEPTION"


def test_b1_buy_a_save_failure_publishes_exactly_one_handled_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _result, envelopes, saved = _run_buy(monkeypatch, tmp_path, _failing_repository_factory())
    assert len(envelopes) == 1
    _assert_allowlisted_envelope(
        envelopes[0], stage="DECISION_SNAPSHOT_SAVE#batch-672", reason=_BUY_REASON
    )
    assert envelopes[0]["source"] == "buy_candidates"
    assert envelopes[0]["job_name"] == "buy-candidates"
    assert len(saved) == 1  # Recommendationの保存は失敗に影響されない


def test_b2_buy_success_and_conflict_publish_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _result, ok_envelopes, _saved = _run_buy(
        monkeypatch, tmp_path / "ok", _ok_repository_factory(tmp_path / "ok")
    )
    assert ok_envelopes == []
    _result, conflict_envelopes, _saved = _run_buy(
        monkeypatch, tmp_path / "conflict", _conflicting_repository_factory()
    )
    assert conflict_envelopes == []


def test_b3_buy_the_decision_and_return_value_do_not_depend_on_the_save_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ok_result, _e, ok_saved = _run_buy(
        monkeypatch, tmp_path / "ok", _ok_repository_factory(tmp_path / "ok")
    )
    failed_result, _e, failed_saved = _run_buy(
        monkeypatch, tmp_path / "failed", _failing_repository_factory()
    )
    assert failed_result == ok_result
    assert failed_saved == ok_saved


def test_b4_buy_validation_mode_neither_saves_nor_notifies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """VALIDATIONでは保存自体をスキップする(既存契約)。スキップは失敗ではない。"""
    _result, envelopes, _saved = _run_buy(
        monkeypatch,
        tmp_path,
        _failing_repository_factory(),
        execution_context=ExecutionContext(mode=ExecutionMode.VALIDATION),
    )
    assert envelopes == []


def test_b5_buy_a_publish_failure_does_not_stop_the_processing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(envelope: dict[str, Any]) -> None:
        raise RuntimeError("SNS unavailable")

    ok_result, _e, ok_saved = _run_buy(
        monkeypatch, tmp_path / "ok", _ok_repository_factory(tmp_path / "ok")
    )
    with caplog.at_level(logging.WARNING):
        result, _e, saved = _run_buy(
            monkeypatch, tmp_path / "boom", _failing_repository_factory(), publish=_boom
        )
    assert "failed to publish HANDLED_FAILURE envelope" in caplog.text
    assert result == ok_result
    assert saved == ok_saved


def test_b6_buy_without_a_batch_id_uses_the_plain_failure_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _result, envelopes, _saved = _run_buy(
        monkeypatch, tmp_path, _failing_repository_factory(), batch_id=None
    )
    assert len(envelopes) == 1
    assert envelopes[0]["failure_stage"] == "DECISION_SNAPSHOT_SAVE"


# --- holdings_watchlist(旧SELL / PROFIT_TAKING / HOLDING_DECISION) --------------------------

_HOLDINGS_ENGINES = {
    "legacy_sell": lambda mp: holdings_tests._patch_legacy_sell(
        mp, {holdings_tests._STOCK_CODE: holdings_tests._minimal_recommendation()}
    ),
    "profit_taking": lambda mp: holdings_tests._patch_profit_taking(
        mp, {holdings_tests._STOCK_CODE: holdings_tests._minimal_recommendation()}
    ),
    "holding_decision": lambda mp: holdings_tests._patch_holding_decision(
        mp,
        holdings_tests._HOLDING_DECISION_NOTIFIED_PLAN,
        {holdings_tests._STOCK_CODE: holdings_tests._holding_decision_result()},
    ),
}


def _run_holdings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    engine: str,
    repository_factory: Any,
    *,
    batch_id: str = "batch-672",
    validation: bool = False,
    publish: Any = None,
) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
    holdings_tests._patch_common(monkeypatch, tmp_path)
    _HOLDINGS_ENGINES[engine](monkeypatch)
    monkeypatch.setattr(holdings_module, "DecisionSnapshotRepository", repository_factory)
    envelopes: list[dict[str, Any]] = []
    monkeypatch.setattr(
        holdings_module, "publish_incident_envelope", publish if publish else envelopes.append
    )
    if validation:
        monkeypatch.setattr(
            holdings_module,
            "resolve_execution_context",
            lambda event: ExecutionContext(mode=ExecutionMode.VALIDATION),
        )
    result = holdings_module.handler(holdings_tests._event(batch_id), holdings_tests._FakeContext())
    repo = RecommendationRepository(store_dir=tmp_path)
    saved = [r.model_dump(mode="json") for r in repo.list_all()]
    return result, envelopes, saved


@pytest.mark.parametrize("engine", sorted(_HOLDINGS_ENGINES))
def test_b7_holdings_a_save_failure_publishes_exactly_one_handled_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: str
) -> None:
    result, envelopes, saved = _run_holdings(
        monkeypatch, tmp_path, engine, _failing_repository_factory()
    )
    assert not result.get("failed")
    assert len(envelopes) == 1
    _assert_allowlisted_envelope(
        envelopes[0], stage="DECISION_SNAPSHOT_SAVE#batch-672", reason=_HOLDINGS_REASON
    )
    assert envelopes[0]["source"] == "holdings_watchlist"
    assert envelopes[0]["job_name"] == "holdings-watchlist"
    assert len(saved) == 1


@pytest.mark.parametrize("engine", sorted(_HOLDINGS_ENGINES))
def test_b8_holdings_success_and_conflict_publish_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: str
) -> None:
    _result, ok_envelopes, _saved = _run_holdings(
        monkeypatch, tmp_path / "ok", engine, _ok_repository_factory(tmp_path / "ok")
    )
    assert ok_envelopes == []
    _result, conflict_envelopes, _saved = _run_holdings(
        monkeypatch, tmp_path / "conflict", engine, _conflicting_repository_factory()
    )
    assert conflict_envelopes == []


@pytest.mark.parametrize("engine", sorted(_HOLDINGS_ENGINES))
def test_b9_holdings_the_result_does_not_depend_on_the_save_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: str
) -> None:
    ok_result, _e, ok_saved = _run_holdings(
        monkeypatch, tmp_path / "ok", engine, _ok_repository_factory(tmp_path / "ok")
    )
    failed_result, _e, failed_saved = _run_holdings(
        monkeypatch, tmp_path / "failed", engine, _failing_repository_factory()
    )
    assert failed_result == ok_result
    assert [r["recommendation_type"] for r in failed_saved] == [
        r["recommendation_type"] for r in ok_saved
    ]


@pytest.mark.parametrize("engine", sorted(_HOLDINGS_ENGINES))
def test_b10_holdings_validation_mode_neither_saves_nor_notifies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: str
) -> None:
    _result, envelopes, _saved = _run_holdings(
        monkeypatch, tmp_path, engine, _failing_repository_factory(), validation=True
    )
    assert envelopes == []


@pytest.mark.parametrize("engine", sorted(_HOLDINGS_ENGINES))
def test_b11_holdings_a_publish_failure_does_not_stop_the_processing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    engine: str,
) -> None:
    def _boom(envelope: dict[str, Any]) -> None:
        raise RuntimeError("SNS unavailable")

    ok_result, _e, ok_saved = _run_holdings(
        monkeypatch, tmp_path / "ok", engine, _ok_repository_factory(tmp_path / "ok")
    )
    with caplog.at_level(logging.WARNING):
        result, _e, saved = _run_holdings(
            monkeypatch, tmp_path / "boom", engine, _failing_repository_factory(), publish=_boom
        )
    assert "failed to publish HANDLED_FAILURE envelope" in caplog.text
    assert result == ok_result
    assert [r["recommendation_type"] for r in saved] == [r["recommendation_type"] for r in ok_saved]


def test_b12_legacy_sell_without_a_batch_id_keeps_the_plain_failure_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """旧SELLの関数は`batch_id: str | None = None`を末尾へ持つ。省略したときは素のstage。"""
    envelopes: list[dict[str, Any]] = []
    monkeypatch.setattr(holdings_module, "publish_incident_envelope", envelopes.append)
    monkeypatch.setattr(
        holdings_module, "DecisionSnapshotRepository", _failing_repository_factory()
    )

    def _call(**extra: Any) -> None:
        holdings_module._notify_legacy_sell_and_build_result(
            holdings_tests._holding(),
            holdings_tests._NOW,
            holdings_tests._minimal_recommendation(),
            RecommendationRepository(store_dir=tmp_path),
            holdings_tests._GoldenNotification(),  # type: ignore[arg-type]
            True,
            **extra,
        )

    _call()
    _call(batch_id="batch-x")
    assert [e["failure_stage"] for e in envelopes] == [
        "DECISION_SNAPSHOT_SAVE",
        "DECISION_SNAPSHOT_SAVE#batch-x",
    ]


# --- C 集約(fingerprint) --------------------------------------------------------------------


class _RecordingLineClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture
def incident_notifier_env(monkeypatch: pytest.MonkeyPatch):
    import boto3
    from moto import mock_aws

    from jstock_advisor.lambda_handlers import incident_notifier_handler as notifier_module

    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "jstock")
    monkeypatch.delenv("GITHUB_APP_SECRET_ARN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    fake_line = _RecordingLineClient()
    monkeypatch.setattr(notifier_module, "build_live_line_client_from_env", lambda: fake_line)
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-northeast-1")
        client.create_table(
            TableName="jstock-incident_state",
            KeySchema=[{"AttributeName": "fingerprint", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "fingerprint", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield fake_line


def _deliver(envelope: dict[str, Any]) -> None:
    import json

    from jstock_advisor.lambda_handlers import incident_notifier_handler as notifier_module

    notifier_module.handler({"Records": [{"Sns": {"Message": json.dumps(envelope)}}]}, None)


def _envelope_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, batch_id: str, stock: str
) -> dict[str, Any]:
    _result, envelopes, _saved = _run_buy(
        monkeypatch,
        tmp_path / f"{batch_id}-{stock}",
        _failing_repository_factory(),
        batch_id=batch_id,
    )
    assert len(envelopes) == 1
    return envelopes[0]


def test_c1_same_batch_failures_notify_once_and_different_batches_notify_independently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    incident_notifier_env: _RecordingLineClient,
) -> None:
    a1 = _envelope_for(monkeypatch, tmp_path, "batch-A", "1")
    a2 = _envelope_for(monkeypatch, tmp_path, "batch-A", "2")
    _deliver(a1)
    _deliver(a2)
    assert len(incident_notifier_env.sent) == 1  # 同一batchの複数失敗 → 1件

    b1 = _envelope_for(monkeypatch, tmp_path, "batch-B", "1")
    _deliver(b1)
    assert len(incident_notifier_env.sent) == 2  # 別batch → 独立して届く


def test_c2_a_batch_without_a_failure_notifies_nothing(
    incident_notifier_env: _RecordingLineClient,
) -> None:
    assert incident_notifier_env.sent == []


def test_c3_the_delivered_message_uses_the_provisional_content_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    incident_notifier_env: _RecordingLineClient,
) -> None:
    """★ 暫定の文言(PROVISIONAL。USERの承認なし)。文言の確定は別(deploy前のUSER確認)。
    ここでは「内容」行が対応表の文言で出ること(OTHERへ落ちないこと)だけを固定する。"""
    _deliver(_envelope_for(monkeypatch, tmp_path, "batch-C", "1"))
    assert len(incident_notifier_env.sent) == 1
    message = incident_notifier_env.sent[0]
    assert "買い候補の判定時点のデータの保存に失敗しました" in message
    assert "技術的な問題を検知しました" not in message
