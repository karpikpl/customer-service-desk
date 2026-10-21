from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from copilot import ToolInvocation
from microsoft_agents.activity import Activity

from service_desk.actions import Actions
from service_desk.tools import ToolContext, build_tools

from .test_main import compose


async def test_all_completed_responses_keep_feedback_without_a_generic_portal(
    monkeypatch, snow, memory, principal, activity
):
    context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "call")
    host, _ = compose(monkeypatch, context)
    turn = SimpleNamespace(activity=Activity.model_validate(activity), send_activity=AsyncMock())
    await host.agent_app.handlers["message"](turn, None)
    for call in turn.send_activity.call_args_list:
        message = call.args[0]
        serialized = message.model_dump(by_alias=True, exclude_none=True)
        assert serialized["channelData"]["feedbackLoop"] == {"type": "default"}
        assert "feedbackLoopEnabled" not in serialized["channelData"]
        assert any(
            "AIGeneratedContent" in e.get("additionalType", []) for e in serialized["entities"]
        )
        if message.attachments:
            body = message.attachments[0].content["body"]
            assert not any(item.get("id") == "service-now-portal" for item in body)
            assert "https://mock.service-now.com/sp" not in str(body)
            feedback = next(item for item in body if item.get("id") == "response-feedback")
            assert [a["verb"] for a in feedback["actions"]] == ["feedback_like", "feedback_dislike"]
        assert message.text is None


async def test_native_feedback_does_not_replace_card_or_require_snow(monkeypatch, activity):
    host, runtime = compose(monkeypatch, None)
    monkeypatch.setattr("service_desk.main.hosted_call_id", lambda: "authenticated-call")
    runtime.turn = Mock(side_effect=AssertionError("Feedback must not load ServiceNow"))
    activity.update(
        type="invoke",
        name="message/submitAction",
        replyToId="incident-card",
        value={
            "actionName": "feedback",
            "actionValue": {
                "reaction": "dislike",
                "feedback": '{"feedbackText":"Please add follow-up actions"}',
            },
        },
    )
    turn = SimpleNamespace(
        activity=Activity.model_validate(activity),
        send_activity=AsyncMock(),
        update_activity=AsyncMock(),
    )
    await host.agent_app.handlers["invoke"](turn, None)
    response = turn.send_activity.call_args.args[0]
    assert response.type == "invokeResponse"
    assert response.value.status == 200
    assert response.value.body == {}
    runtime.feedback.record.assert_called_once()
    assert runtime.feedback.record.call_args.kwargs["response_id"] == "incident-card"
    assert runtime.feedback.record.call_args.kwargs["rating"] == "negative"
    turn.update_activity.assert_not_awaited()
    runtime.turn.assert_not_called()
    assert not runtime.pending


async def test_native_feedback_fails_explicitly_off_platform(monkeypatch, activity):
    host, runtime = compose(monkeypatch, None)
    monkeypatch.delenv("FOUNDRY_HOSTING_ENVIRONMENT", raising=False)
    activity.update(type="invoke", name="message/submitAction")
    turn = SimpleNamespace(activity=Activity.model_validate(activity), send_activity=AsyncMock())
    await host.agent_app.handlers["invoke"](turn, None)
    assert turn.send_activity.call_args.args[0].value.status == 400
    runtime.feedback.record.assert_not_called()


@pytest.mark.parametrize(
    "verb,rating", [("feedback_like", "positive"), ("feedback_dislike", "negative")]
)
async def test_explicit_card_thumbs_preserve_original_card(monkeypatch, activity, verb, rating):
    host, runtime = compose(monkeypatch, None)
    monkeypatch.setattr("service_desk.main.hosted_call_id", lambda: "authenticated-call")
    runtime.turn = Mock(side_effect=AssertionError("Ratings must not load ServiceNow"))
    activity.update(
        type="invoke",
        name="adaptiveCard/action",
        replyToId="rated-incident",
        value={"action": {"verb": verb, "data": {"verb": verb}}},
    )
    turn = SimpleNamespace(
        activity=Activity.model_validate(activity),
        send_activity=AsyncMock(),
        update_activity=AsyncMock(),
    )
    await host.agent_app.handlers["invoke"](turn, None)
    response = turn.send_activity.call_args.args[0]
    assert response.value.body["statusCode"] == 200
    assert response.value.body["type"] == "application/vnd.microsoft.activity.message"
    assert runtime.feedback.record.call_args.kwargs["rating"] == rating
    assert runtime.feedback.record.call_args.kwargs["source"] == "card"
    assert runtime.feedback.record.call_args.kwargs["response_id"] == "rated-incident"
    turn.update_activity.assert_not_awaited()
    assert not runtime.pending
    runtime.turn.assert_not_called()


async def test_explicit_feedback_needs_no_model_or_ticket_mapping(monkeypatch, activity):
    host, runtime = compose(monkeypatch, None)
    monkeypatch.setattr("service_desk.main.hosted_call_id", lambda: "authenticated-call")
    runtime.turn = Mock(side_effect=AssertionError("Explicit feedback must not load ServiceNow"))
    runtime.agent.ask = Mock(side_effect=AssertionError("Explicit feedback must not need model"))
    activity["text"] = "Feedback: the incident card needs clearer actions"
    turn = SimpleNamespace(activity=Activity.model_validate(activity), send_activity=AsyncMock())
    await host.agent_app.handlers["message"](turn, None)
    runtime.feedback.record.assert_called_once()
    assert runtime.feedback.record.call_args.kwargs["text"] == activity["text"]
    assert turn.send_activity.call_args.args[0].attachments[0].content["body"][1]["text"] == (
        "Thanks for your feedback"
    )
    runtime.turn.assert_not_called()


async def test_model_feedback_reuses_authorized_turn_not_sdk_callback_context(
    monkeypatch, snow, memory, principal, activity
):
    context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "verified-call")
    host, runtime = compose(monkeypatch, context)
    monkeypatch.setattr(
        "service_desk.main.hosted_call_id",
        Mock(side_effect=AssertionError("SDK callback cannot re-read host context")),
    )

    async def ask(turn, _prompt):
        tool = next(t for t in build_tools(turn) if t.name == "record_user_feedback")
        result = await tool.handler(ToolInvocation(arguments={"rating": "negative"}))
        assert result.result_type == "success"
        yield "final", "Feedback received."

    runtime.agent.ask = ask
    activity["text"] = "I had to ask three times before you understood me."
    turn = SimpleNamespace(activity=Activity.model_validate(activity), send_activity=AsyncMock())
    await host.agent_app.handlers["message"](turn, None)
    runtime.feedback.record.assert_called_once()
    assert runtime.feedback.record.call_args.kwargs["text"] == activity["text"]


@pytest.mark.parametrize(
    "text",
    [
        "Your response was confusing. Also show my incidents.",
        "I had to ask three times before you understood. Also show my incidents.",
    ],
)
async def test_mixed_feedback_is_captured_once_then_continues_support(
    monkeypatch, snow, memory, principal, activity, requests, text
):
    context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "call")
    host, runtime = compose(monkeypatch, context)
    monkeypatch.setattr("service_desk.main.hosted_call_id", lambda: "authenticated-call")
    prompts = []

    async def ask(turn, prompt):
        prompts.append(prompt)
        tool = next(t for t in build_tools(turn) if t.name == "record_user_feedback")
        await tool.handler(ToolInvocation(arguments={"rating": "negative"}))
        yield "final", "I can help with your incidents."

    runtime.agent.ask = ask
    activity["text"] = text
    turn = SimpleNamespace(activity=Activity.model_validate(activity), send_activity=AsyncMock())
    await host.agent_app.handlers["message"](turn, None)
    runtime.feedback.record.assert_called_once()
    assert prompts == [activity["text"]]
    assert requests == []
