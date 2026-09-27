import json

import pytest

from jstock_advisor.lambda_handlers import _fanout


class _FakeLambdaClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def invoke(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"StatusCode": 202}


def test_dispatch_async_invokes_event_type_with_json_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeLambdaClient()
    monkeypatch.setattr(_fanout.boto3, "client", lambda service: fake_client)

    _fanout.dispatch_async("my-function", {"task": "holding", "stock_code": "2914"})

    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["FunctionName"] == "my-function"
    assert call["InvocationType"] == "Event"
    assert json.loads(call["Payload"]) == {"task": "holding", "stock_code": "2914"}


def test_resolve_function_name_prefers_context_attribute() -> None:
    class _Context:
        function_name = "from-context"

    assert _fanout.resolve_function_name(_Context(), "fallback") == "from-context"


def test_resolve_function_name_falls_back_when_context_lacks_attribute() -> None:
    assert _fanout.resolve_function_name(object(), "fallback") == "fallback"


def test_resolve_function_name_falls_back_when_context_attribute_is_empty() -> None:
    class _Context:
        function_name = ""

    assert _fanout.resolve_function_name(_Context(), "fallback") == "fallback"


# --- Issue #533(#319 Phase 2): SQS dispatch + env var toggle ---


class _FakeSqsClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send_message(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"MessageId": "fake-id"}


def test_dispatch_sqs_sends_json_payload_to_queue_url(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeSqsClient()
    monkeypatch.setattr(_fanout.boto3, "client", lambda service: fake_client)

    _fanout.dispatch_sqs(
        "https://sqs.example/my-queue", {"task": "buy_candidate", "stock_code": "2914"}
    )

    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["QueueUrl"] == "https://sqs.example/my-queue"
    assert json.loads(call["MessageBody"]) == {"task": "buy_candidate", "stock_code": "2914"}


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        (None, False),
        ("", False),
        ("false", False),
        ("FALSE", False),
        ("true", True),
        ("TRUE", True),
        ("  true  ", True),
        ("yes", False),
    ],
)
def test_buy_candidate_sqs_dispatch_enabled(
    monkeypatch: pytest.MonkeyPatch, env_value: str | None, expected: bool
) -> None:
    if env_value is None:
        monkeypatch.delenv(_fanout.BUY_CANDIDATE_SQS_DISPATCH_ENABLED_ENV, raising=False)
    else:
        monkeypatch.setenv(_fanout.BUY_CANDIDATE_SQS_DISPATCH_ENABLED_ENV, env_value)
    assert _fanout.buy_candidate_sqs_dispatch_enabled() is expected


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        (None, False),
        ("false", False),
        ("true", True),
        ("True", True),
    ],
)
def test_holdings_watchlist_sqs_dispatch_enabled(
    monkeypatch: pytest.MonkeyPatch, env_value: str | None, expected: bool
) -> None:
    if env_value is None:
        monkeypatch.delenv(_fanout.HOLDINGS_WATCHLIST_SQS_DISPATCH_ENABLED_ENV, raising=False)
    else:
        monkeypatch.setenv(_fanout.HOLDINGS_WATCHLIST_SQS_DISPATCH_ENABLED_ENV, env_value)
    assert _fanout.holdings_watchlist_sqs_dispatch_enabled() is expected


def test_buy_and_holdings_sqs_dispatch_toggles_are_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2つのトグルは独立変数であり、片方をtrueにしても他方に影響しない
    (#533本文どおり、buy/holdingsそれぞれ独立に切替可能な設計の固定)。
    """
    monkeypatch.setenv(_fanout.BUY_CANDIDATE_SQS_DISPATCH_ENABLED_ENV, "true")
    monkeypatch.delenv(_fanout.HOLDINGS_WATCHLIST_SQS_DISPATCH_ENABLED_ENV, raising=False)

    assert _fanout.buy_candidate_sqs_dispatch_enabled() is True
    assert _fanout.holdings_watchlist_sqs_dispatch_enabled() is False
