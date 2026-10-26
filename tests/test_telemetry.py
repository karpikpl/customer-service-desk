from __future__ import annotations

import asyncio
import contextvars
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from copilot import ToolInvocation
from copilot.generated.session_events import ModelCallFinishedOutcome
from microsoft_agents.activity import Activity
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from service_desk import telemetry
from service_desk.actions import Actions
from service_desk.config import Credentials
from service_desk.main import deliver
from service_desk.models import DeskError
from service_desk.servicenow import SnowClient
from service_desk.state import FoundryState
from service_desk.tools import ToolContext, build_tools

from .test_main import compose
from .test_toolbox import FakeCatalog, FakeSession


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)
    yield exporter
    provider.shutdown()


def by_name(spans, name):
    return [span for span in spans.get_finished_spans() if span.name == "service_desk." + name]


@pytest.mark.parametrize("cancelled", [False, True])
def test_errors_record_type_not_payload_and_restore_context(spans, cancelled):
    secret = "private ticket text password=SECRET"
    error = asyncio.CancelledError(secret) if cancelled else DeskError(secret)
    with pytest.raises(type(error)), telemetry.operation("test"):
        raise error
    span = by_name(spans, "test")[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"] == type(error).__name__
    assert span.status.description is None
    assert not span.events
    assert secret not in str(span.attributes)
    assert not trace.get_current_span().get_span_context().is_valid


async def test_sdk_callback_keeps_trace_and_turn_counts_without_host_context(
    spans, snow, memory, principal, record
):
    record["description"] = "SENSITIVE TICKET BODY"
    context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "PRIVATE-CALL-ID")
    with telemetry.turn():
        parent = trace.get_current_span().get_span_context()
        tools = build_tools(context)
        tool = next(item for item in tools if item.name == "get_my_incident")
        for arguments in (
            {"number": "INC0012345", "secret": "PRIVATE-ARGUMENT"},
            {"number": "INC0012345"},
        ):
            task = contextvars.Context().run(
                asyncio.create_task, tool.handler(ToolInvocation(arguments=arguments))
            )
            await task
    turn = by_name(spans, "turn")[0]
    assert turn.attributes["desk.tool_calls"] == 2
    assert turn.attributes["desk.tool_validation_failures"] == 1
    calls = by_name(spans, "tool")
    assert len(calls) == 2
    assert calls[0].attributes["desk.result"] == "invalid_input"
    assert calls[0].status.status_code == StatusCode.ERROR
    assert calls[1].status.status_code == StatusCode.UNSET
    for child in calls + by_name(spans, "servicenow.request"):
        assert child.context.trace_id == parent.trace_id
    assert calls[0].parent.span_id == parent.span_id
    exported = json.dumps(
        [
            (span.name, dict(span.attributes), list(span.events))
            for span in spans.get_finished_spans()
        ]
    )
    for private in (
        "SENSITIVE TICKET BODY",
        "PRIVATE-CALL-ID",
        "PRIVATE-ARGUMENT",
        principal.caller_id,
        principal.object_id,
        "INC0012345",
    ):
        assert private not in exported


async def test_servicenow_http_error_has_safe_operation_metadata(spans, policy, principal):
    client = SnowClient(
        "https://mock.service-now.com",
        Credentials(username="SECRET-USER", password="SECRET-PASSWORD"),
        policy,
        transport=httpx.MockTransport(lambda _: httpx.Response(403, text="SECRET-RESPONSE")),
    )
    try:
        with pytest.raises(DeskError):
            await client.get_incident(principal, "INC0012345")
    finally:
        await client.close()
    span = by_name(spans, "servicenow.request")[0]
    assert span.attributes == {
        "http.request.method": "GET",
        "desk.resource": "incident",
        "http.response.status_code": 403,
        "error.type": "DeskError",
    }
    assert not span.events
    assert span.status.status_code == StatusCode.ERROR


def test_model_events_count_calls_retries_usage_and_close_spans(spans):
    with telemetry.turn(), telemetry.operation("agent") as parent:
        observer = telemetry.ModelEvents(parent)
        for outcome in (ModelCallFinishedOutcome.SUCCESS, ModelCallFinishedOutcome.ERROR):
            observer.observe("model.call_start", SimpleNamespace(turn_id="PRIVATE-TURN"))
            observer.observe(
                "model.call_finished",
                SimpleNamespace(turn_id="PRIVATE-TURN", outcome=outcome),
            )
        observer.observe("assistant.turn_retry", SimpleNamespace(reason="SECRET"))
        observer.observe("model.call_failure", SimpleNamespace(error_message="SECRET"))
        observer.observe(
            "assistant.usage",
            SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                cache_read_tokens=50,
                duration=timedelta(milliseconds=1234),
                model="PRIVATE-MODEL",
            ),
        )
        observer.close()
    model_calls = by_name(spans, "model_call")
    assert [span.status.status_code for span in model_calls] == [
        StatusCode.UNSET,
        StatusCode.ERROR,
    ]
    assert all(span.parent.span_id == parent.get_span_context().span_id for span in model_calls)
    attributes = by_name(spans, "turn")[0].attributes
    assert attributes["desk.model_calls"] == 2
    assert attributes["desk.model_retries"] == 1
    assert attributes["desk.model_failures"] == 1
    assert attributes["desk.input_tokens"] == 100
    assert attributes["desk.output_tokens"] == 20
    assert attributes["desk.cache_read_tokens"] == 50
    assert attributes["desk.model_reported_ms"] == 1234
    assert "SECRET" not in str(attributes)
    assert "PRIVATE" not in str([span.attributes for span in spans.get_finished_spans()])


def test_model_usage_fallback_and_unfinished_call_are_explicit(spans):
    with telemetry.operation("agent") as parent:
        observer = telemetry.ModelEvents(parent)
        observer.observe("assistant.usage", SimpleNamespace(input_tokens=3))
        observer.close()
    assert by_name(spans, "agent")[0].attributes["desk.model_calls"] == 1
    assert by_name(spans, "agent")[0].attributes["desk.model_count_source"] == "usage"
    with telemetry.operation("agent") as parent:
        observer = telemetry.ModelEvents(parent)
        observer.observe("model.call_start", SimpleNamespace(turn_id="private"))
        observer.close()
    assert by_name(spans, "model_call")[0].attributes["desk.completion_observed"] is False
    with telemetry.operation("agent") as parent:
        telemetry.ModelEvents(parent).close()
    assert by_name(spans, "agent")[-1].attributes["desk.model_count_source"] == "unavailable"
    assert "desk.model_calls" not in by_name(spans, "agent")[-1].attributes


async def test_only_successful_content_delivery_sets_first_sent_time(spans):
    from service_desk import cards

    context = SimpleNamespace(
        activity=SimpleNamespace(reply_to_id=None),
        send_activity=AsyncMock(side_effect=[RuntimeError("SECRET"), SimpleNamespace(id="one")]),
    )
    with telemetry.turn():
        with pytest.raises(RuntimeError):
            await deliver(context, cards.notice("Test", "PRIVATE-CARD"))
        assert telemetry._turn.get().first_delivery is False
        await deliver(context, cards.notice("Test", "PRIVATE-CARD"))
        first = telemetry._turn.get().span
        context.send_activity.side_effect = None
        context.send_activity.return_value = SimpleNamespace(id="two")
        await deliver(context, cards.notice("Test", "PRIVATE-CARD"))
    attributes = by_name(spans, "turn")[0].attributes
    assert attributes["desk.deliveries"] == 2
    assert attributes["desk.first_content_sent_ms"] <= attributes["desk.duration_ms"]
    assert first is not None
    assert "PRIVATE-CARD" not in str([span.attributes for span in spans.get_finished_spans()])


async def test_turn_summary_records_deterministic_route(spans, monkeypatch, activity):
    host, _ = compose(monkeypatch, None)
    monkeypatch.setattr("service_desk.main.hosted_call_id", lambda: "verified")
    activity["text"] = "/feedback PRIVATE-ARGUMENT"
    context = SimpleNamespace(
        activity=Activity.model_validate(activity),
        send_activity=AsyncMock(return_value=SimpleNamespace(id="reply")),
    )
    await host.agent_app.handlers["message"](context, None)
    turn = by_name(spans, "turn")[0]
    assert turn.attributes["desk.route"] == "command"
    assert turn.attributes["desk.operation"] == "feedback"
    assert turn.attributes["desk.agent_runs"] == 0
    assert turn.attributes["desk.deliveries"] == 1
    assert "PRIVATE" not in str(turn.attributes)


async def test_state_and_toolbox_spans_keep_correlation_without_payloads(spans):
    store = AsyncMock()
    store.get_item.return_value = SimpleNamespace(value={"secret": "PRIVATE-VALUE"}, etag="etag")
    state = FoundryState(store, "PRIVATE-CALL-ID")
    session = FakeSession()
    catalog = FakeCatalog(session)
    with telemetry.turn():
        parent = trace.get_current_span().get_span_context()
        await state.get("PRIVATE-KEY")
        await state.create("PRIVATE-KEY", {"secret": "PRIVATE-VALUE"})
        await state.replace("PRIVATE-KEY", {"secret": "PRIVATE-VALUE"}, "PRIVATE-ETAG")
        await catalog.list_skills("PRIVATE-CALL-ID")
        await catalog.load_skill("printer-troubleshooting", "PRIVATE-CALL-ID")
    for name in ("state.get", "state.create", "state.replace", "toolbox.discover", "toolbox.load"):
        span = by_name(spans, name)[0]
        assert span.parent.span_id == parent.span_id
        assert span.context.trace_id == parent.trace_id
        assert not span.attributes


async def test_concurrent_turn_counters_are_isolated(spans):
    async def run(size):
        with telemetry.turn():
            for _ in range(size):
                telemetry.count("tool_calls")
                await asyncio.sleep(0)

    await asyncio.gather(run(2), run(3))
    assert sorted(span.attributes["desk.tool_calls"] for span in by_name(spans, "turn")) == [2, 3]


async def test_handled_failure_does_not_look_like_a_successful_turn(
    spans, monkeypatch, snow, memory, principal, activity
):
    tool_context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "call")
    host, runtime = compose(monkeypatch, tool_context)

    async def ask(_context, _prompt):
        raise DeskError("PRIVATE-ERROR")
        yield

    runtime.agent.ask = ask
    context = SimpleNamespace(
        activity=Activity.model_validate(activity),
        send_activity=AsyncMock(return_value=SimpleNamespace(id="reply")),
    )
    await host.agent_app.handlers["message"](context, None)
    span = by_name(spans, "turn")[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"] == "DeskError"
    assert "PRIVATE" not in str(span.attributes)
    assert not span.events
