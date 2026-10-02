"""Issue #668(HF-3): 保有監視日次バッチの銘柄単位technical failureをHANDLED_FAILUREと
してUSER通知することの確認。

対象のcatch boundary(Issue #668設計、fresh確認済み):

    B1  holding analysis failed unexpectedly(1保有の想定外例外)
    B2  holding_evaluation_record_save_failed(評価後の記録保存失敗。既存契約
        「通知・戻り値に影響させない」は維持する)

実装はbuy_candidates_handler.py(#667)と同型: batch_tracker.py側への新規
カウンタ追加はせず、#665のincident_notifier_handler.py側の既存dedup
(claim window)に委ねる(詳細は#667のテストファイルdocstring参照)。

Issue #717 MUST-1是正: batch_idを`failure_stage`へ埋め込みfingerprintを
batch単位に区別する(#667と同型)。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from jstock_advisor.config.loader import load_config
from jstock_advisor.domain.entities.enums import AccountType, ExecutionMode
from jstock_advisor.domain.entities.execution_context import ExecutionContext
from jstock_advisor.domain.entities.holding import Holding
from jstock_advisor.domain.entities.owner import DEFAULT_OWNER, build_holding_id
from jstock_advisor.lambda_handlers import holdings_watchlist_handler as handler_module
from jstock_advisor.services.rule_version_service import RuleVersionService

_NOW = dt.datetime(2026, 10, 2, 0, 0, tzinfo=dt.UTC)


def _holding(stock_code: str = "2914") -> Holding:
    return Holding(
        owner=DEFAULT_OWNER,
        holding_id=build_holding_id(DEFAULT_OWNER, stock_code),
        stock_code=stock_code,
        stock_name=f"銘柄{stock_code}",
        shares=100,
        average_purchase_price=Decimal("1000"),
        total_purchase_amount=Decimal("100000"),
        first_purchase_date=dt.date(2024, 1, 1),
        last_purchase_date=dt.date(2024, 1, 1),
        account_type=AccountType.SPECIFIC,
        created_at=_NOW,
        updated_at=_NOW,
    )


class _RaisingHoldingEvaluationRecordRepo:
    def save(self, record: object) -> None:
        raise RuntimeError("boom")


# --- _notify_handled_failure() / _notify_handled_failure_safely() のcontract -----------


def test_notify_handled_failure_publishes_allowlisted_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module, "publish_incident_envelope", lambda envelope: captured.update(envelope)
    )

    handler_module._notify_handled_failure(
        "HOLDING_ANALYSIS", "HOLDINGS_WATCHLIST_ANALYSIS_FAILED", _NOW, "batch-1"
    )

    assert captured["source"] == "holdings_watchlist"
    assert captured["job_name"] == "holdings-watchlist"
    # Issue #717 MUST-1: batch_idがfailure_stageへ埋め込まれ、fingerprintが
    # batch単位で区別される。
    assert captured["failure_stage"] == "HOLDING_ANALYSIS#batch-1"
    assert captured["reason_code"] == "HOLDINGS_WATCHLIST_ANALYSIS_FAILED"
    assert captured["failure_class"] == "HANDLED_FAILURE"
    assert "stock_code" not in captured
    assert "holding_id" not in captured


def test_notify_handled_failure_safely_swallows_publish_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(envelope: dict[str, object]) -> None:
        raise RuntimeError("SNS unavailable")

    monkeypatch.setattr(handler_module, "publish_incident_envelope", _boom)

    with caplog.at_level("WARNING"):
        handler_module._notify_handled_failure_safely("HOLDING_ANALYSIS", "X", _NOW, "batch-1")

    assert "failed to publish HANDLED_FAILURE envelope" in caplog.text


# --- B2: _persist_holding_evaluation_record()の保存失敗 ------------------------------


def test_b2_evaluation_record_save_failure_notifies_handled_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notified: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        handler_module,
        "_notify_handled_failure_safely",
        lambda stage, reason, now, batch_id: notified.append((stage, reason, batch_id)),
    )

    handler_module._persist_holding_evaluation_record(
        _RaisingHoldingEvaluationRecordRepo(),  # type: ignore[arg-type]
        _holding(),
        _NOW,
        ExecutionContext.normal(),
        "v1-mvp",
        "batch-1",
        execution_plan_mode=None,
        execution_plan_reason=None,
        notification_enabled=True,
        authoritative_engine=None,
        authoritative_outcome_category="HOLD",
        authoritative_recommendation_id=None,
        authoritative_notification_sent=False,
        legacy_sell_ran=False,
        legacy_sell_recommendation_id=None,
        profit_taking_ran=False,
        profit_taking_recommendation_id=None,
        holding_decision_ran=False,
        holding_decision_result_id=None,
        holding_decision_notified=False,
    )

    assert notified == [
        ("EVALUATION_RECORD_SAVE", "HOLDINGS_WATCHLIST_EVALUATION_RECORD_SAVE_FAILED", "batch-1")
    ]


# --- B1: _process_single_holding()本体の想定外例外 -----------------------------------


def test_b1_unexpected_analysis_failure_notifies_handled_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notified: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        handler_module,
        "_notify_handled_failure_safely",
        lambda stage, reason, now, batch_id: notified.append((stage, reason, batch_id)),
    )
    target = _holding("2914")
    monkeypatch.setattr(handler_module.HoldingRepository, "get", lambda self, holding_id: target)

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("analysis boom")

    monkeypatch.setattr(handler_module, "_analyze_one_holding", _boom)

    result = handler_module._process_single_holding(
        build_holding_id(DEFAULT_OWNER, "2914"),
        None,  # batch_id=None: _finish_batch_item()は即return(通知サービス未使用)
        _NOW,
        object(),  # providers
        load_config(),
        object(),  # recommendation_repo
        object(),  # notification_service(batch_id=Noneのため参照されない)
        RuleVersionService(),
        handler_module._DEFAULT_EXECUTION_CONTEXT,
    )

    assert result == {
        "holding_id": build_holding_id(DEFAULT_OWNER, "2914"),
        "recommended": False,
        "notified": False,
        "failed": True,
    }
    assert notified == [("HOLDING_ANALYSIS", "HOLDINGS_WATCHLIST_ANALYSIS_FAILED", None)]


def test_b2_validation_mode_skips_save_and_does_not_notify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既存契約(VALIDATIONでは保存自体をスキップする)が壊れていないことの回帰確認。
    スキップは失敗ではないため通知もされない。"""
    notified: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        handler_module,
        "_notify_handled_failure_safely",
        lambda stage, reason, now, batch_id: notified.append((stage, reason, batch_id)),
    )

    handler_module._persist_holding_evaluation_record(
        _RaisingHoldingEvaluationRecordRepo(),  # type: ignore[arg-type]
        _holding(),
        _NOW,
        ExecutionContext(mode=ExecutionMode.VALIDATION),
        "v1-mvp",
        "batch-1",
        execution_plan_mode=None,
        execution_plan_reason=None,
        notification_enabled=True,
        authoritative_engine=None,
        authoritative_outcome_category="HOLD",
        authoritative_recommendation_id=None,
        authoritative_notification_sent=False,
        legacy_sell_ran=False,
        legacy_sell_recommendation_id=None,
        profit_taking_ran=False,
        profit_taking_recommendation_id=None,
        holding_decision_ran=False,
        holding_decision_result_id=None,
        holding_decision_notified=False,
    )

    assert notified == []


# --- Issue #717 MUST-1(ChatGPTレビュー指摘): fingerprint不変条件のEnd-to-End確認 -------
#
# SAME_BATCH_MULTIPLE_FAILURES -> max 1 HANDLED_FAILURE notification
# DIFFERENT_BATCH_FAILURES    -> independently observable
# SUCCESS_BATCH               -> 0 additional notification
#
# `_notify_handled_failure()`が実際に構築するenvelopeを捕捉し、
# `incident_notifier_handler.handler()`(実際のfingerprint計算・claim/dedupを
# 行う本物のconsumer)へSNSイベントとして流し込み、LINE送信回数で不変条件を
# 直接確認する(#667と同型)。


class _RecordingLineClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def push_message(self, text: str) -> None:
        self.sent.append(text)


def _captured_envelope(
    monkeypatch: pytest.MonkeyPatch, failure_stage: str, reason_code: str, batch_id: str | None
) -> dict[str, object]:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        handler_module, "publish_incident_envelope", lambda envelope: captured.update(envelope)
    )
    handler_module._notify_handled_failure(failure_stage, reason_code, _NOW, batch_id)
    return captured


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


def _deliver_to_incident_notifier(envelope: dict[str, object]) -> None:
    import json

    from jstock_advisor.lambda_handlers import incident_notifier_handler as notifier_module

    event = {"Records": [{"Sns": {"Message": json.dumps(envelope)}}]}
    notifier_module.handler(event, None)


def test_same_batch_multiple_failures_send_at_most_one_notification(
    monkeypatch: pytest.MonkeyPatch, incident_notifier_env: _RecordingLineClient
) -> None:
    """SAME_BATCH_MULTIPLE_FAILURES -> max 1 HANDLED_FAILURE notification。
    同一batch_id内で異なる銘柄(B1境界)が2回失敗しても、LINE送信は1件のみ。
    """
    envelope_1 = _captured_envelope(
        monkeypatch, "HOLDING_ANALYSIS", "HOLDINGS_WATCHLIST_ANALYSIS_FAILED", "batch-A"
    )
    envelope_2 = _captured_envelope(
        monkeypatch, "HOLDING_ANALYSIS", "HOLDINGS_WATCHLIST_ANALYSIS_FAILED", "batch-A"
    )

    _deliver_to_incident_notifier(envelope_1)
    _deliver_to_incident_notifier(envelope_2)

    assert len(incident_notifier_env.sent) == 1


def test_different_batch_failures_are_independently_observable(
    monkeypatch: pytest.MonkeyPatch, incident_notifier_env: _RecordingLineClient
) -> None:
    """DIFFERENT_BATCH_FAILURES -> independently observable。異なるbatch_idの
    失敗は、時間的に近接していても(=時間窓dedupでは本来区別できない)、それぞれ
    独立してLINE通知が届く(旧版の時間窓onlyのdedupでは本来ここが壊れていた)。
    """
    envelope_a = _captured_envelope(
        monkeypatch, "HOLDING_ANALYSIS", "HOLDINGS_WATCHLIST_ANALYSIS_FAILED", "batch-A"
    )
    envelope_b = _captured_envelope(
        monkeypatch, "HOLDING_ANALYSIS", "HOLDINGS_WATCHLIST_ANALYSIS_FAILED", "batch-B"
    )

    _deliver_to_incident_notifier(envelope_a)
    _deliver_to_incident_notifier(envelope_b)

    assert len(incident_notifier_env.sent) == 2


def test_success_batch_sends_zero_additional_notifications(
    incident_notifier_env: _RecordingLineClient,
) -> None:
    """SUCCESS_BATCH -> 0 additional notification。失敗が無ければ
    `_notify_handled_failure_safely()`自体が呼ばれないため、当然LINE送信も0件。"""
    assert incident_notifier_env.sent == []
