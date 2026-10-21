from __future__ import annotations

import copy
import json
from unittest.mock import Mock

import pytest
from copilot import ToolInvocation
from opentelemetry._logs import get_logger_provider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

from service_desk.actions import Actions
from service_desk.feedback import (
    EVENT_NAME,
    FeedbackRecorder,
    detect_feedback,
    feedback_only,
    parse_native_feedback,
    safe_text,
)
from service_desk.models import DeskError
from service_desk.tools import ToolContext, build_tools

from .conftest import TENANT


@pytest.fixture
def recorder(monkeypatch):
    exporter = InMemoryLogRecordExporter()
    factory = Mock(return_value=exporter)
    monkeypatch.setattr("service_desk.feedback.AzureMonitorLogExporter", factory)
    sink = FeedbackRecorder("synthetic-connection")
    yield sink, exporter, factory
    sink.close()


def test_feedback_uses_only_private_app_insights_provider(
    recorder, activity, caplog, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    sink, exporter, factory = recorder
    global_provider = get_logger_provider()
    activity["id"] = "feedback-activity"
    sink.record(
        activity,
        TENANT,
        source="native",
        rating="negative",
        text="The response was confusing; password=don't-log-me",
        response_id="rated-response",
    )
    assert sink.provider.force_flush()
    record = exporter.get_finished_logs()[0].log_record
    assert record.body == EVENT_NAME
    attributes = record.attributes
    assert attributes["microsoft.custom_event.name"] == EVENT_NAME
    assert attributes["is_feedback"] is True
    assert attributes["feedback_source"] == "native"
    assert attributes["feedback_rating"] == "negative"
    assert attributes["response_id"] == "rated-response"
    assert "password=[redacted]" in attributes["feedback_text"]
    assert "don't-log-me" not in str(attributes)
    assert attributes["feedback_redacted"] is True
    assert len(attributes["conversation_scope"]) == 64
    assert len(attributes["feedback_id"]) == 64
    assert activity["from"]["aadObjectId"] not in str(attributes)
    assert "confusing" not in caplog.text
    assert "don't-log-me" not in caplog.text
    assert get_logger_provider() is global_provider
    assert list(tmp_path.iterdir()) == []
    assert factory.call_args.kwargs["disable_offline_storage"] is True
    assert factory.call_args.kwargs["logging_enable"] is False


def test_unmapped_tenant_member_can_rate_without_a_snow_mapping(recorder, activity):
    sink, exporter, _ = recorder
    activity["from"]["aadObjectId"] = "00000000-0000-0000-0000-000000000099"
    sink.record(
        activity, TENANT, source="conversation", rating="neutral", text="Feedback: simplify"
    )
    assert sink.provider.force_flush()
    assert len(exporter.get_finished_logs()) == 1


@pytest.mark.parametrize("invalid", ["tenant", "group", "missing-user", "mismatch", "channel"])
def test_feedback_rejects_invalid_identity(recorder, activity, invalid):
    sink, exporter, _ = recorder
    if invalid == "tenant":
        activity["channelData"]["tenant"]["id"] = "00000000-0000-0000-0000-000000000099"
    elif invalid == "group":
        activity["conversation"]["conversationType"] = "groupChat"
    elif invalid == "missing-user":
        del activity["from"]["aadObjectId"]
    elif invalid == "mismatch":
        activity["conversation"]["tenantId"] = "00000000-0000-0000-0000-000000000099"
    else:
        activity["channelId"] = "other"
    with pytest.raises(DeskError):
        sink.record(activity, TENANT, source="native", rating="positive", text="Useful")
    assert sink.provider.force_flush()
    assert exporter.get_finished_logs() == ()


def test_missing_telemetry_is_an_explicit_failure(activity):
    with pytest.raises(DeskError, match="not configured"):
        FeedbackRecorder("").record(
            activity, TENANT, source="conversation", rating="negative", text="Feedback: not useful"
        )


def test_retried_feedback_has_stable_id_and_user_scope(recorder, activity):
    sink, exporter, _ = recorder
    activity["id"] = "retryable"
    for _ in range(2):
        sink.record(
            activity, TENANT, source="native", rating="positive", text="Good", response_id="r"
        )
    other = copy.deepcopy(activity)
    other["conversation"]["id"] = "other-personal-chat"
    sink.record(other, TENANT, source="native", rating="positive", text="Good", response_id="r")
    assert sink.provider.force_flush()
    ids = [r.log_record.attributes["feedback_id"] for r in exporter.get_finished_logs()]
    assert ids[0] == ids[1] != ids[2]


@pytest.mark.parametrize(
    ("message", "rating"),
    [
        ("Feedback: simpler cards, please", "neutral"),
        ("Your response was not helpful", "negative"),
        ("That answer was wrong; also list my incidents", "negative"),
        ("This card is helpful", "positive"),
        ("This response was not very helpful", "negative"),
        ("The printer is not working", None),
        ("Still not printing", None),
        ("That worked", None),
        ("I like this printer", None),
        ("Show the card for INC0012345", None),
    ],
)
def test_conservative_feedback_detection(message, rating):
    assert detect_feedback(message) == rating


def test_mixed_feedback_keeps_support_request():
    assert feedback_only("Feedback: this card is confusing")
    assert not feedback_only("Feedback: this is confusing. Also show my incidents")
    assert not feedback_only("Feedback: simplify it. Show my incidents")
    assert not feedback_only("My printer is offline")


@pytest.mark.parametrize(
    "secret",
    [
        "password=hunter2",
        'api_key="do not capture"',
        "Authorization: Bearer abc123",
        "access_token=abc123",
        "https://user:pass@example.com/sp",
        "test@example.com",
        "eyJabcdef.eyJabcdef.signature",
    ],
)
def test_secret_redaction(secret):
    result, redacted, truncated = safe_text(secret)
    assert redacted
    assert not truncated
    assert result != secret


def test_feedback_text_is_utf8_bounded():
    text, redacted, truncated = safe_text("\U0001f44d" * 4000)
    assert truncated
    assert not redacted
    assert len(text.encode()) <= 4000
    assert text.endswith("[truncated]")


@pytest.mark.parametrize("reaction,rating", [("like", "positive"), ("dislike", "negative")])
def test_native_payload(reaction, rating):
    assert parse_native_feedback(
        {
            "name": "message/submitAction",
            "replyToId": "response-1",
            "value": {
                "actionName": "feedback",
                "actionValue": {
                    "reaction": reaction,
                    "feedback": json.dumps({"feedbackText": "A clearer card"}),
                },
            },
        }
    ) == (rating, "A clearer card", "response-1")


@pytest.mark.parametrize(
    "change",
    [
        {"replyToId": None},
        {"value": None},
        {"value": {"actionName": "feedback", "actionValue": {"reaction": "unknown"}}},
        {"value": {"actionName": "feedback", "actionValue": {"reaction": "like", "feedback": "["}}},
        {
            "value": {
                "actionName": "feedback",
                "actionValue": {"reaction": "like", "feedback": '{"feedbackText":123}'},
            }
        },
    ],
)
def test_native_payload_rejects_malformed_input(change):
    activity = {
        "name": "message/submitAction",
        "replyToId": "response",
        "value": {"actionName": "feedback", "actionValue": {"reaction": "like"}},
        **change,
    }
    with pytest.raises(DeskError, match="could not read"):
        parse_native_feedback(activity)


async def test_feedback_tool_only_classifies_current_message_and_deduplicates(
    snow, memory, principal, requests
):
    context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "call")
    context.capture_feedback = Mock()
    tool = next(t for t in build_tools(context) if t.name == "record_user_feedback")
    assert set(tool.parameters["properties"]) == {"rating"}
    for _ in range(2):
        result = await tool.handler(ToolInvocation(arguments={"rating": "negative"}))
        assert result.result_type == "success"
    context.capture_feedback.assert_called_once_with("negative")
    assert memory.data == {}
    assert requests == []
    assert context.outbox == []
    rejected = await tool.handler(
        ToolInvocation(arguments={"rating": "positive", "text": "Fabricated feedback"})
    )
    assert rejected.result_type == "failure"


async def test_feedback_tool_cannot_claim_success_without_capture(snow, memory, principal):
    context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "call")
    tool = next(t for t in build_tools(context) if t.name == "record_user_feedback")
    result = await tool.handler(ToolInvocation(arguments={"rating": "neutral"}))
    assert result.result_type == "failure"
    assert not context.feedback_recorded
