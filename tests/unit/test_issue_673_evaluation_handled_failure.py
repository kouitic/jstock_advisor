"""Issue #673(HF-8): 判定事後評価のaggregate commit失敗をHANDLED_FAILUREとしてUSER通知する。

対象のcatch境界(TARO設計 #673 issuecomment-5862585837。fresh確認済み):

    recommendation_evaluation_service.py の aggregate commit 失敗の except Exception
    -> 評価結果を「保存しない」へ倒し(rollbackの契約)、aggregate_commit_failuresを加算して
       run自体は継続する。翌日再試行される。
    -> 件数は既に EvaluationRunSummary.aggregate_commit_failed_count として
       evaluation_handler へ返却されている(新規カウンタ・戻り値契約は不要)。

本Issueが固定する契約:

    1. aggregate_commit_failed_count > 0 のとき、HF-0契約(#665)の envelope を publish する
       (failure_class = HANDLED_FAILURE。failure_stage = AGGREGATE_COMMIT。
        failure_count は件数そのまま)。0のときは publish しない。
    2. 通知自体の失敗(SNS権限不足・Topic ARN未設定等)が、評価本体・戻り値・監査記録を
       一切妨げない(評価runをFAILさせるとasync retryで評価処理全体が再実行されるため)。
    3. rollback・翌日再試行の既存契約(評価処理は1回だけ・戻り値のキー)は変えない。
    4. envelope は allowlist のキーのみ(stock_code / owner / holding_id /
       stack trace / 生のexception message を含めない)。

★ CODE IMPLEMENTATION APPROVED ≠ PRODUCTION NOTIFICATION ACTIVATION APPROVED。
  EvaluationFunction へ INCIDENT_NOTIFICATION_TOPIC_ARN と sns:Publish を配線する infra は
  本Issueの範囲外(別Issue)。配線されるまで、Productionでは publish が Topic ARN 未設定で
  失敗し、WARNING ログだけが残る(= 契約2。本テストで固定)。
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from moto import mock_aws

from jstock_advisor.lambda_handlers import evaluation_handler
from jstock_advisor.services.incident_envelope_publisher import INCIDENT_ENVELOPE_ALLOWLIST
from jstock_advisor.services.recommendation_evaluation_service import (
    EvaluationRunOutcome,
    EvaluationRunSummary,
)

_REGION = "ap-northeast-1"


def _summary(aggregate_commit_failed_count: int) -> EvaluationRunSummary:
    return EvaluationRunSummary(
        due_count=100,
        already_evaluated_count=40,
        pending_count=60,
        pending_recommendation_count=55,
        evaluated_count=50,
        backlog_remaining=10,
        budget_exhausted=True,
        recommendations_scanned=55,
        provider_call_count=3,
        duration_ms=1234,
        aggregate_commit_failed_count=aggregate_commit_failed_count,
    )


class _StubService:
    """評価本体(外部I/O)の代わり。呼ばれた回数と、返すsummaryを保持する。"""

    count = 0
    created: list[_StubService] = []

    def __init__(self, **_kwargs: Any) -> None:
        self.run_calls = 0
        _StubService.created.append(self)

    def run_due_evaluations_single_pass(self, *_args: Any, **_kwargs: Any) -> EvaluationRunOutcome:
        self.run_calls += 1
        return EvaluationRunOutcome(summary=_summary(_StubService.count))


@pytest.fixture
def stubbed_handler(monkeypatch: pytest.MonkeyPatch) -> list[_StubService]:
    """外部I/O(provider bundle)・サービス本体・監査記録(成功扱い)を差し替える。"""
    _StubService.created = []
    _StubService.count = 0
    monkeypatch.setattr(
        evaluation_handler,
        "build_real_provider_bundle",
        lambda *_a, **_k: SimpleNamespace(market_data=object()),
    )
    monkeypatch.setattr(evaluation_handler, "RecommendationEvaluationService", _StubService)
    monkeypatch.setattr(evaluation_handler, "record_run_summary", lambda *_a, **_k: True)
    return _StubService.created


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """publish_incident_envelope() の呼び出しを記録する(実SNSは呼ばない)。"""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        evaluation_handler, "publish_incident_envelope", lambda envelope: calls.append(envelope)
    )
    return calls


# --- 契約1: aggregate_commit_failed_count > 0 のときだけ publish する --------------------


@pytest.mark.parametrize("count", [1, 3, 25])
def test_aggregate_commit_failure_publishes_handled_failure_envelope(
    count: int, stubbed_handler: list[_StubService], published: list[dict[str, Any]]
) -> None:
    _StubService.count = count

    evaluation_handler.handler({}, None)

    assert len(published) == 1
    envelope = published[0]
    assert envelope["failure_class"] == "HANDLED_FAILURE"
    assert envelope["failure_stage"] == "AGGREGATE_COMMIT"
    assert envelope["source"] == "evaluation"
    assert envelope["job_name"] == "evaluation"
    assert envelope["failure_count"] == count  # 既存summaryの件数をそのまま使う
    assert envelope["reason_code"] == "EVALUATION_AGGREGATE_COMMIT_FAILED"
    assert envelope["failure_type"] == "UNHANDLED_EXCEPTION"
    assert envelope["occurred_at"]


def test_no_aggregate_commit_failure_publishes_nothing(
    stubbed_handler: list[_StubService], published: list[dict[str, Any]]
) -> None:
    """0件のときは追加の通知が発生しない(通常の日にUSERへ何も送らない)。"""
    _StubService.count = 0

    evaluation_handler.handler({}, None)

    assert published == []


def test_envelope_contains_only_allowlisted_keys_and_no_identifiers(
    stubbed_handler: list[_StubService], published: list[dict[str, Any]]
) -> None:
    """契約4: allowlistのキーのみ。銘柄・所有者・保有ID・生の例外文を運ぶキーが無い。"""
    _StubService.count = 2

    evaluation_handler.handler({}, None)

    envelope = published[0]
    assert set(envelope) <= INCIDENT_ENVELOPE_ALLOWLIST
    for forbidden in ("stock_code", "owner", "holding_id", "stack_trace", "error_message"):
        assert forbidden not in envelope
    serialized = json.dumps(envelope)
    assert "Traceback" not in serialized


# --- 契約2: 通知の失敗が評価本体・戻り値を妨げない --------------------------------------


def test_publish_failure_does_not_break_the_evaluation_run(
    monkeypatch: pytest.MonkeyPatch,
    stubbed_handler: list[_StubService],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _StubService.count = 4

    def _raise(_envelope: dict[str, Any]) -> None:
        raise RuntimeError("sns unavailable: secret-detail-123")

    monkeypatch.setattr(evaluation_handler, "publish_incident_envelope", _raise)

    with caplog.at_level(logging.WARNING, logger=evaluation_handler.__name__):
        result = evaluation_handler.handler({}, None)  # 例外が出ないこと自体が契約

    # 評価本体の結果・既存の戻り値キーは影響を受けない。
    assert result["evaluated"] == _summary(4).business_evaluated_count
    assert result["backlog_remaining"] == 10
    assert result["audit_persisted"] is True
    # async retryを誘発しない = 評価処理は1回だけ。
    assert len(stubbed_handler) == 1
    assert stubbed_handler[0].run_calls == 1
    # 無音にしない(WARNING)。ただし例外の内容・識別子はログへ出さない(型のみ。#135)。
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("failed to publish HANDLED_FAILURE envelope" in m for m in messages)
    assert any("AGGREGATE_COMMIT" in m and "RuntimeError" in m for m in messages)
    assert not any("secret-detail-123" in m for m in messages)


def test_missing_topic_arn_is_a_warning_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
    stubbed_handler: list[_StubService],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """★ Production NOTIFICATION ACTIVATION は本Issueの範囲外(infra配線は別Issue)。

    EvaluationFunctionに INCIDENT_NOTIFICATION_TOPIC_ARN が無い現状では、実際の
    publish_incident_envelope() は KeyError で失敗する。それが評価runを壊さず、
    WARNINGだけが残ること(= 配線前にmerge/deployされても安全)を、実publisherで固定する。
    """
    monkeypatch.delenv("INCIDENT_NOTIFICATION_TOPIC_ARN", raising=False)
    _StubService.count = 2

    with caplog.at_level(logging.WARNING, logger=evaluation_handler.__name__):
        result = evaluation_handler.handler({}, None)

    assert result["audit_persisted"] is True
    assert len(stubbed_handler) == 1
    assert any("KeyError" in r.getMessage() for r in caplog.records)


# --- 契約3: 既存の戻り値契約は変わらない -------------------------------------------------


def test_return_value_contract_is_unchanged(
    stubbed_handler: list[_StubService], published: list[dict[str, Any]]
) -> None:
    """通知の有無にかかわらず、戻り値のキーは従来どおり(新しいキーを増やさない)。"""
    _StubService.count = 5
    with_failure = evaluation_handler.handler({}, None)
    _StubService.count = 0
    without_failure = evaluation_handler.handler({}, None)

    assert set(with_failure) == set(without_failure)
    assert with_failure == without_failure  # summaryの他の値は同一。通知は戻り値を変えない


# --- 実publisher(moto)でのend-to-end ---------------------------------------------------


@mock_aws
def test_real_publisher_delivers_the_envelope_to_the_topic(
    monkeypatch: pytest.MonkeyPatch, stubbed_handler: list[_StubService]
) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    sns = boto3.client("sns", region_name=_REGION)
    sqs = boto3.client("sqs", region_name=_REGION)
    topic_arn = sns.create_topic(Name="incident-test")["TopicArn"]
    queue_url = sqs.create_queue(QueueName="incident-test-q")["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])[
        "Attributes"
    ]["QueueArn"]
    sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=queue_arn)
    monkeypatch.setenv("INCIDENT_NOTIFICATION_TOPIC_ARN", topic_arn)
    _StubService.count = 7

    evaluation_handler.handler({}, None)

    messages = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10).get("Messages", [])
    assert len(messages) == 1
    body = json.loads(json.loads(messages[0]["Body"])["Message"])
    assert body["failure_class"] == "HANDLED_FAILURE"
    assert body["failure_stage"] == "AGGREGATE_COMMIT"
    assert body["failure_count"] == 7
    assert set(body) <= INCIDENT_ENVELOPE_ALLOWLIST
