import json

import pytest
import requests

from pycodex import BaseTool, ContextConfig, ResponsesModelClient, ResponsesProviderConfig, ToolRegistry
from pycodex.model import (
    ContextLengthExceeded,
    ResponsesApiError,
    ResponsesIncompleteError,
    ResponsesRetryableError,
    ResponsesSignatureRejected,
)
from pycodex.protocol import AssistantMessage, Prompt, ReasoningItem, ToolCall, ToolResult, UserMessage
from tests.fakes import make_agent, make_runtime
from tests.test_model_fallback import Response

PROXY_URL = "https://mp.z01.normandyfunc.com/v1"


def make_model(monkeypatch, retries=-1, fallback_models=(), base_url="https://example.invalid/v1"):
    monkeypatch.setattr(ResponsesModelClient, "_retry_delay_seconds", lambda *args: 0)
    return ResponsesModelClient(ResponsesProviderConfig(
        model="primary", provider_name="test", base_url=base_url,
        api_key_env=None, stream_max_retries=retries, fallback_models=fallback_models,
    ))


def answer(text="done"):
    return Response(events=[
        {"type": "response.output_item.done", "item": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }},
        {"type": "response.completed"},
    ])


def sse_error(code, message="boom", wrapped=True):
    if wrapped:
        return Response(events=[{"type": "response.failed", "response": {
            "error": {"code": code, "message": message},
        }}])
    return Response(events=[{"type": "error", "code": code, "message": message}])


def signed_history():
    return [
        UserMessage("hello"),
        ReasoningItem(payload={
            "type": "reasoning", "id": "rs_remote",
            "summary": [{"type": "summary_text", "text": "readable thought"}],
            "encrypted_content": "SIG-OLD",
        }),
        ToolCall(call_id="call-1", name="count", arguments="{}", id="fc_remote"),
        ToolResult(call_id="call-1", name="count", output="counted"),
        AssistantMessage("partial answer", id="msg_remote"),
        UserMessage("continue"),
    ]


def test_negative_retry_budget_means_unlimited():
    config = ResponsesProviderConfig("m", "p", "https://example.com/v1", None, stream_max_retries=-1)
    assert config.effective_stream_max_retries() == -1
    assert ResponsesProviderConfig(
        "m", "p", "https://example.com/v1", None, stream_max_retries=0,
    ).effective_stream_max_retries() == 0


def test_backoff_is_capped():
    client = ResponsesModelClient(ResponsesProviderConfig("m", "p", "https://example.com/v1", None))
    assert client._retry_delay_seconds(1) == pytest.approx(0.2)
    assert client._retry_delay_seconds(50) == 60.0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    408, 429, 500, 502, 503, 504, "transport", "truncated",
    "server_error", "internal_server_error", "rate_limit_exceeded", "unknown_sse", "bare_sse",
])
async def test_unlimited_retries_recover_after_persistent_failure(monkeypatch, failure):
    model = make_model(monkeypatch)
    bodies = []

    def send(self, request, **kwargs):
        bodies.append(json.loads(request.body))
        if len(bodies) <= 12:
            if failure == "transport":
                raise requests.ConnectionError("connection reset by peer")
            if failure == "truncated":
                return Response(events=[{"type": "response.created"}])
            if failure == "bare_sse":
                return sse_error("server_error", wrapped=False)
            if failure == "unknown_sse":
                return sse_error("weird_upstream_error")
            if isinstance(failure, str):
                return sse_error(failure)
            return Response(failure)
        return answer()

    monkeypatch.setattr(requests.Session, "send", send)
    events = []
    result = await model.complete(Prompt(input=[UserMessage("hello")], tools=[]), events.append)

    assert result.items[-1].text == "done"
    assert len(bodies) == 13
    assert all(body == bodies[0] for body in bodies)
    notices = [event.message for event in events if event.kind == "stream_error"]
    assert notices == ["Reconnecting... {0}/∞".format(attempt) for attempt in range(1, 13)]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    '{"error":{"message":"Please try rephrasing your request."}}',
    "UPSTREAM SAYS: Rephrasing Your Request may help",
])
async def test_rephrase_400_retries_without_rewriting_request(monkeypatch, body):
    model = make_model(monkeypatch)
    bodies = []

    def send(self, request, **kwargs):
        bodies.append(request.body)
        return Response(400, text=body) if len(bodies) <= 4 else answer()

    monkeypatch.setattr(requests.Session, "send", send)
    result = await model.complete(Prompt(input=[UserMessage("hello")], tools=[]))
    assert result.items[-1].text == "done"
    assert len(bodies) == 5 and len(set(bodies)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,error", [
    ((400, "bad request"), ResponsesApiError),
    ((401, "unauthorized"), ResponsesApiError),
    ((400, "maximum context length exceeded"), ContextLengthExceeded),
    ("insufficient_quota", ResponsesApiError),
    ("invalid_prompt", ResponsesApiError),
    ("context_length_exceeded", ContextLengthExceeded),
    ("incomplete", ResponsesIncompleteError),
])
async def test_terminal_errors_are_not_retried(monkeypatch, failure, error):
    model = make_model(monkeypatch, fallback_models=("backup",))
    attempts = []

    def send(self, request, **kwargs):
        attempts.append(json.loads(request.body)["model"])
        if failure == "incomplete":
            return Response(events=[{"type": "response.incomplete", "response": {
                "incomplete_details": {"reason": "content_filter"},
            }}])
        if isinstance(failure, tuple):
            return Response(failure[0], text=failure[1])
        return sse_error(failure)

    monkeypatch.setattr(requests.Session, "send", send)
    with pytest.raises(error) as raised:
        await model.complete(Prompt(input=[UserMessage("hello")], tools=[]))
    assert not isinstance(raised.value, ResponsesRetryableError)
    assert attempts == ["primary"]


@pytest.mark.asyncio
async def test_unavailable_and_first_data_timeout_still_fall_back_with_reason(monkeypatch):
    model = make_model(monkeypatch, fallback_models=("backup",))
    attempts = []

    def send(self, request, **kwargs):
        attempts.append(json.loads(request.body)["model"])
        return Response(404) if attempts[-1] == "primary" else answer()

    monkeypatch.setattr(requests.Session, "send", send)
    await model.complete(Prompt(input=[UserMessage("hello")], tools=[]))
    assert attempts == ["primary", "backup"]
    assert model.model == "primary"
    assert model.active_model == "backup"
    assert model.fallback_reason == "primary failed: HTTP 404"

    attempts.clear()
    await model.complete(Prompt(input=[UserMessage("again")], tools=[]))
    assert attempts == ["backup"]
    assert model.active_model == "backup"
    assert "cooling down" in model.fallback_reason or "HTTP 404" in model.fallback_reason

    model.model = "primary"
    assert model.active_model == "primary" and model.fallback_reason == ""


def reasoning_inputs(body):
    return [item for item in body["input"] if item.get("type") == "reasoning"]


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", [
    (400, '{"error":{"message":"Invalid signature in reasoning item rs_remote"}}'),
    (400, '{"error":{"message":"encrypted_content could not be verified"}}'),
    "sse-signature",
])
async def test_signature_rejection_repairs_once_and_keeps_history(monkeypatch, rejection):
    model = make_model(monkeypatch)
    history = signed_history()
    snapshot = [item.serialize() for item in history]
    bodies = []

    def send(self, request, **kwargs):
        bodies.append(json.loads(request.body))
        if len(bodies) == 1:
            if rejection == "sse-signature":
                return sse_error("invalid_request_error", "thinking block signature is invalid")
            return Response(rejection[0], text=rejection[1])
        return answer()

    monkeypatch.setattr(requests.Session, "send", send)
    events = []
    result = await model.complete(Prompt(input=history, tools=[]), events.append)

    assert result.items[-1].text == "done"
    assert len(bodies) == 2
    first, repaired = bodies
    assert reasoning_inputs(first)[0]["encrypted_content"] == "SIG-OLD"
    cleaned = reasoning_inputs(repaired)[0]
    assert cleaned["encrypted_content"] is None and "id" not in cleaned
    assert cleaned["summary"] == [{"type": "summary_text", "text": "readable thought"}]
    assert not any("id" in item for item in repaired["input"] if item.get("type") in (
        "function_call", "message") and item.get("role") != "developer")
    strip = lambda body: [
        {k: v for k, v in item.items() if k not in ("id", "encrypted_content")}
        for item in body["input"]
    ]
    assert strip(first) == strip(repaired)
    assert [item.serialize() for item in history] == snapshot
    assert any("Repairing rejected reasoning signatures" in e.message
               for e in events if e.kind == "stream_error")


@pytest.mark.asyncio
async def test_signature_repair_is_bounded(monkeypatch):
    model = make_model(monkeypatch)
    bodies = []

    def send(self, request, **kwargs):
        bodies.append(json.loads(request.body))
        return Response(400, text='{"error":{"message":"invalid signature"}}')

    monkeypatch.setattr(requests.Session, "send", send)
    with pytest.raises(ResponsesApiError):
        await model.complete(Prompt(input=signed_history(), tools=[]))
    assert len(bodies) == 2


@pytest.mark.asyncio
async def test_rejected_signature_cache_is_per_model_and_spares_new_signatures(monkeypatch):
    model = make_model(monkeypatch)
    bodies = []
    rejected = []

    def send(self, request, **kwargs):
        bodies.append(json.loads(request.body))
        if not rejected:
            rejected.append(1)
            return Response(400, text="Invalid signature")
        return answer()

    monkeypatch.setattr(requests.Session, "send", send)
    await model.complete(Prompt(input=signed_history(), tools=[]))

    fresh = ReasoningItem(payload={
        "type": "reasoning", "id": "rs_new", "summary": [], "encrypted_content": "SIG-NEW",
    })
    bodies.clear()
    await model.complete(Prompt(input=signed_history() + [fresh, UserMessage("more")], tools=[]))
    assert len(bodies) == 1
    sent = {item.get("encrypted_content") for item in reasoning_inputs(bodies[0])}
    assert sent == {None, "SIG-NEW"}

    other = model.with_overrides(model="other")
    bodies.clear()
    await other.complete(Prompt(input=signed_history(), tools=[]))
    assert reasoning_inputs(bodies[0])[0]["encrypted_content"] == "SIG-OLD"


@pytest.mark.asyncio
async def test_plain_requests_are_never_pre_cleaned(monkeypatch):
    model = make_model(monkeypatch)
    bodies = []

    def send(self, request, **kwargs):
        bodies.append(json.loads(request.body))
        return answer()

    monkeypatch.setattr(requests.Session, "send", send)
    await model.complete(Prompt(input=signed_history(), tools=[]))
    assert reasoning_inputs(bodies[0])[0] == {
        "type": "reasoning", "id": "rs_remote",
        "summary": [{"type": "summary_text", "text": "readable thought"}],
        "encrypted_content": "SIG-OLD", "content": None,
    } or reasoning_inputs(bodies[0])[0]["encrypted_content"] == "SIG-OLD"


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url,repairs", [(PROXY_URL, True), ("https://api.example.com/v1", False)])
async def test_modelproxy_server_error_on_signed_request_repairs_once(monkeypatch, base_url, repairs):
    model = make_model(monkeypatch, base_url=base_url)
    bodies = []

    def send(self, request, **kwargs):
        bodies.append(json.loads(request.body))
        return sse_error("server_error") if len(bodies) <= 3 else answer()

    monkeypatch.setattr(requests.Session, "send", send)
    await model.complete(Prompt(input=signed_history(), tools=[]))
    sigs = [reasoning_inputs(body)[0]["encrypted_content"] for body in bodies]
    if repairs:
        assert sigs == ["SIG-OLD", None, None, None]
    else:
        assert sigs == ["SIG-OLD"] * 4


@pytest.mark.asyncio
async def test_modelproxy_server_error_on_unsigned_request_is_plain_retry(monkeypatch):
    model = make_model(monkeypatch, base_url=PROXY_URL)
    events = []
    calls = []

    def send(self, request, **kwargs):
        calls.append(1)
        return sse_error("server_error") if len(calls) == 1 else answer()

    monkeypatch.setattr(requests.Session, "send", send)
    await model.complete(Prompt(input=[UserMessage("hello")], tools=[]), events.append)
    assert [e.message for e in events if e.kind == "stream_error"] == ["Reconnecting... 1/∞"]


@pytest.mark.asyncio
async def test_retries_and_repair_never_replay_tools(monkeypatch):
    model = make_model(monkeypatch, base_url=PROXY_URL)
    executions = []

    class CountTool(BaseTool):
        name = "count"
        description = "Count one execution."
        input_schema = {"type": "object"}

        async def run(self, context, args):
            executions.append("count")
            return "counted"

    tools = ToolRegistry()
    tools.register(CountTool())
    runtime = make_runtime(make_agent(model, tools, ContextConfig()))
    bodies = []

    def send(self, request, **kwargs):
        bodies.append(json.loads(request.body))
        if len(bodies) == 1:
            return Response(events=[
                {"type": "response.output_item.done", "item": {
                    "type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "SIG-1",
                }},
                {"type": "response.output_item.done", "item": {
                    "type": "function_call", "name": "count", "call_id": "count-once",
                    "arguments": "{}", "id": "fc_1",
                }},
                {"type": "response.completed"},
            ])
        if len(bodies) == 2:
            return Response(400, text="invalid signature")
        if len(bodies) <= 5:
            return Response(503)
        return answer()

    monkeypatch.setattr(requests.Session, "send", send)
    await runtime.start()
    try:
        await runtime.submit_user_turn("hello")
        assert executions == ["count"]
        assert len(bodies) == 6
        assert len([i for i in runtime.agent.history if isinstance(i, ToolCall)]) == 1
        assert len([i for i in runtime.agent.history if isinstance(i, ToolResult)]) == 1
        kept = [i for i in runtime.agent.history if isinstance(i, ReasoningItem)][0]
        assert kept.payload["encrypted_content"] == "SIG-1"
        state = runtime.state_snapshot()["model_route"]
        assert state["selected"] == state["active"] == "primary"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_stop_retry_ends_unlimited_backoff(monkeypatch):
    model = make_model(monkeypatch)
    monkeypatch.setattr(ResponsesModelClient, "_retry_delay_seconds", lambda *args: 3600)
    calls = []

    def send(self, request, **kwargs):
        calls.append(1)
        return Response(503)

    monkeypatch.setattr(requests.Session, "send", send)
    import asyncio
    from pycodex.model import ModelRequestInterrupted

    task = asyncio.ensure_future(model.complete(Prompt(input=[UserMessage("hi")], tools=[])))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if calls:
            break
    model.stop_retry()
    with pytest.raises(ModelRequestInterrupted):
        await asyncio.wait_for(task, 2)
    assert len(calls) == 1
