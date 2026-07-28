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
