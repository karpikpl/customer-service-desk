from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from microsoft_agents.activity import Activity

from service_desk import cards
from service_desk.actions import Actions
from service_desk.config import Settings
from service_desk.main import Runtime, create_host
from service_desk.models import CaseOption, CaseProgress, DeskError, DraftRequest
from service_desk.tools import ToolContext

from .test_config import settings_values


class FakeApp:
    def __init__(self):
        self.handlers = {}

    def activity(self, kind):
        def register(function):
            self.handlers[kind] = function
            return function

        return register

    def error(self, function):
        return function


class FakeHost:
    def __init__(self):
        self.agent_app = FakeApp()

    def shutdown_handler(self, function):
        self.shutdown = function
        return function


def compose(monkeypatch, context):
    async def ask(_context, _prompt):
        yield "final", "Please describe your issue."

    @asynccontextmanager
    async def turn(_activity):
        yield context

    runtime = SimpleNamespace(
        turn=turn,
        agent=SimpleNamespace(ask=ask),
        pending=set(),
        close=AsyncMock(),
        feedback=SimpleNamespace(record=Mock()),
    )
    monkeypatch.setattr("service_desk.main.Runtime", lambda _: runtime)
    monkeypatch.setattr("service_desk.main.ActivityAgentServerHost", FakeHost)
    return create_host(Settings(**settings_values())), runtime


async def test_nonstreaming_messages_are_not_dropped(
    monkeypatch, snow, memory, principal, activity
):
    context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "call")
    host, _ = compose(monkeypatch, context)
    turn = SimpleNamespace(
        activity=Activity.model_validate(activity),
        streaming_response=None,
        send_activity=AsyncMock(),
    )
    await host.agent_app.handlers["message"](turn, None)
    assert (
        turn.send_activity.call_args_list[0].args[0].attachments[0].content["body"][2]["text"]
        == "Please describe your issue."
    )
    assert turn.send_activity.await_count == 2


async def test_model_reply_and_cards_do_not_depend_on_teams_streaming(
    monkeypatch, snow, memory, principal, activity
):
    context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "call")
    card = cards.troubleshooting("Printing", "Check the selected printer.", token="a" * 32)
    context.outbox.append(card)
    host, _ = compose(monkeypatch, context)
    turn = SimpleNamespace(
        activity=Activity.model_validate(activity),
        send_activity=AsyncMock(),
    )
    await host.agent_app.handlers["message"](turn, None)
    turn.send_activity.assert_awaited_once()
    message = turn.send_activity.call_args.args[0]
    assert message.text is None
    assert message.attachments[0].content == card


@pytest.mark.parametrize(
    "interaction",
    ["options", "question", "outcome", "intake", "comment", "resolve", "review", "receipt"],
)
@pytest.mark.parametrize("invoke", [False, True])
async def test_interactive_question_delivers_only_the_card_with_its_controls(
    monkeypatch, snow, memory, principal, activity, interaction, invoke
):
    context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "call")
    if interaction == "options":
        card = cards.case_options(
            "Printer troubleshooting",
            "Would you like local or remote checks?",
            [
                CaseOption(id="local", label="Local checks"),
                CaseOption(id="remote", label="Remote checks"),
            ],
            "c" * 32,
        )
        expected = ["Local checks", "Remote checks"]
    elif interaction == "question":
        card = cards.case_question("Printing", "What error does the printer show?", "c" * 32)
        expected = ["Continue", "I don't know"]
    elif interaction == "outcome":
        card = cards.troubleshooting("Printing", "Check the display.", "General guidance", "c" * 32)
        expected = [action["title"] for action in card["actions"]]
    else:
        from .test_cards import gallery

        card = gallery()[interaction]
        expected = [action["title"] for action in card["actions"]]
    context.outbox.append(card)
    host, runtime = compose(monkeypatch, context)

    async def ask(_context, _prompt):
        yield "final", "Choose an option on the card to continue. No ticket changes were made."

    runtime.agent.ask = ask
    if invoke:
        activity.update(
            type="invoke",
            name="adaptiveCard/action",
            value={"action": {"verb": "troubleshoot", "data": {"verb": "troubleshoot"}}},
            replyToId="question-card",
        )
    deferred = SimpleNamespace(
        activity=Activity.model_validate(activity),
        send_activity=AsyncMock(return_value=SimpleNamespace(id="question-card")),
        update_activity=AsyncMock(),
    )

    async def continue_conversation(_identity, _activity, callback):
        await callback(deferred)

    turn = SimpleNamespace(
        activity=Activity.model_validate(activity),
        send_activity=AsyncMock(return_value=SimpleNamespace(id="question-card")),
        identity=Mock(),
        adapter=SimpleNamespace(
            continue_conversation_with_claims=AsyncMock(side_effect=continue_conversation)
        ),
    )
    await host.agent_app.handlers["invoke" if invoke else "message"](turn, None)
    if invoke:
        await asyncio.gather(*runtime.pending)
        deferred.send_activity.assert_not_awaited()
        deferred.update_activity.assert_awaited_once()
        message = deferred.update_activity.call_args.args[0]
        assert message.id == "question-card"
    else:
        message = turn.send_activity.call_args.args[0]
    turn.send_activity.assert_awaited_once()
    rendered = message.attachments[0].content
    assert rendered == card
    assert message.channel_data["feedbackLoop"] == {"type": "default"}
    assert [action["title"] for action in rendered["actions"]] == expected
    assert "service-now-portal" not in str(rendered)
    assert ("mock.service-now.com" in str(rendered)) is (interaction == "receipt")


async def test_typed_option_is_dispatched_before_model_continuation(
    monkeypatch, snow, memory, principal, activity, requests
):
    context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "call")
    await context.save_case(
        CaseProgress(
            skill_name="printer-troubleshooting",
            skill_digest="pinned",
            skill_content="Routing procedure",
            options=[
                CaseOption(id="remote", label="Remote checks"),
                CaseOption(id="local", label="Local checks"),
            ],
        )
    )
    host, runtime = compose(monkeypatch, context)
    prompts = []

    async def ask(_context, prompt):
        prompts.append(prompt)
        yield "final", "Continuing the selected path."

    runtime.agent.ask = ask
    activity["text"] = "Remote checks"
    turn = SimpleNamespace(
        activity=Activity.model_validate(activity),
        send_activity=AsyncMock(),
    )
    await host.agent_app.handlers["message"](turn, None)
    assert (await context.active_case()).selected_option.id == "remote"
    assert "selected a saved support option" in prompts[0]
    assert "remote" in prompts[0]
    assert requests == []


async def test_invoke_acknowledges_before_deferred_write_and_updates_card(
    monkeypatch, snow, memory, principal, activity, requests
):
    actions = Actions(snow, memory)
    context = ToolContext(principal, snow, memory, actions, Mock(), "opaque-call")
    draft = await actions.prepare(
        principal,
        DraftRequest(operation="create", short_description="Printer", description="Offline"),
    )
    host, runtime = compose(monkeypatch, context)
    value = {"action": {"verb": "confirm", "data": {"verb": "confirm", "action_id": draft.id}}}
    activity.update(type="invoke", name="adaptiveCard/action", value=value, replyToId="card-1")
    deferred = SimpleNamespace(
        activity=Activity.model_validate(activity),
        send_activity=AsyncMock(),
        update_activity=AsyncMock(),
    )

    async def continue_conversation(identity, continuation, callback):
        assert identity is turn.identity
        assert continuation is not turn.activity
        assert continuation == turn.activity
        await callback(deferred)

    turn = SimpleNamespace(
        activity=Activity.model_validate(activity),
        identity=Mock(),
        adapter=SimpleNamespace(
            continue_conversation_with_claims=AsyncMock(side_effect=continue_conversation)
        ),
        send_activity=AsyncMock(),
        update_activity=AsyncMock(side_effect=RuntimeError("Original turn is closed")),
    )
    await host.agent_app.handlers["invoke"](turn, None)
    assert not requests
    ack = turn.send_activity.call_args.args[0]
    assert ack.type == "invokeResponse"
    assert ack.value.body["statusCode"] == 200
    await asyncio.gather(*runtime.pending)
    assert [request.method for request in requests] == ["POST", "GET"]
    turn.update_activity.assert_not_awaited()
    turn.adapter.continue_conversation_with_claims.assert_awaited_once()
    updated = deferred.update_activity.call_args.args[0]
    assert updated.id == "card-1"
    assert updated.text is None
    assert len(updated.attachments) == 1
    assert updated.attachments[0].content["body"][1]["text"] == "Change confirmed"


async def test_local_endpoint_cannot_access_real_tickets(monkeypatch, tmp_path, activity):
    monkeypatch.delenv("FOUNDRY_HOSTING_ENVIRONMENT", raising=False)
    monkeypatch.setattr("service_desk.agent.Path.home", lambda: tmp_path)
    runtime = Runtime(Settings(**settings_values()))
    try:
        with pytest.raises(DeskError, match="authenticated"):
            async with runtime.turn(activity):
                pytest.fail("Off-platform access must not be authorized")
        assert runtime.snow is None
    finally:
        await runtime.close()


@pytest.mark.parametrize(
    "greeting",
    [
        "Hello",
        "Hi!",
        "Good morning",
        "help",
        "",
        "What can you do?",
        "Hi, what can you do?",
        "How can you help me?",
    ],
)
async def test_greeting_addresses_sender_without_backend_dependencies(
    monkeypatch, activity, greeting
):
    host, runtime = compose(monkeypatch, None)
    runtime.turn = Mock(side_effect=AssertionError("Greeting must not load secrets or state"))
    runtime.agent.ask = Mock(side_effect=AssertionError("Greeting must not invoke the model"))
    activity["from"]["name"] = "Adele Vance"
    activity["text"] = greeting
    turn = SimpleNamespace(
        activity=Activity.model_validate(activity),
        send_activity=AsyncMock(),
    )
    await host.agent_app.handlers["message"](turn, None)
    message = turn.send_activity.call_args.args[0]
    assert message.attachments[0].content["body"][1]["text"] == r"Hello, Adele Vance\!"
    assert "How can I help you today?" in message.summary
    assert message.text is None
    assert message.attachments[0].content["fallbackText"] == message.summary
    assert [a["verb"] for a in message.attachments[0].content["actions"]] == [
        "list",
        "intake",
        "troubleshoot",
    ]
    runtime.turn.assert_not_called()
    runtime.agent.ask.assert_not_called()


async def test_home_card_returns_personalized_welcome_without_loading_state(monkeypatch, activity):
    host, runtime = compose(monkeypatch, None)
    runtime.turn = Mock(side_effect=AssertionError("Home must not load backend dependencies"))
    activity["from"]["name"] = "Adele"
    activity["value"] = {"verb": "home"}
    turn = SimpleNamespace(activity=Activity.model_validate(activity), send_activity=AsyncMock())
    await host.agent_app.handlers["message"](turn, None)
    assert (
        turn.send_activity.call_args.args[0].attachments[0].content["body"][1]["text"]
        == r"Hello, Adele\!"
    )
    runtime.turn.assert_not_called()


async def test_install_and_member_added_welcome_once_and_ignore_removal(monkeypatch, activity):
    host, runtime = compose(monkeypatch, None)
    runtime.turn = Mock(side_effect=AssertionError("Install welcome must not load backend"))
    activity["from"]["name"] = "Adele"
    activity.update(type="installationUpdate", action="add")
    turn = SimpleNamespace(activity=Activity.model_validate(activity), send_activity=AsyncMock())
    await host.agent_app.handlers["installationUpdate"](turn, None)
    assert turn.send_activity.await_count == 1
    activity.update(type="conversationUpdate", membersAdded=[{"id": "bot"}])
    turn.activity = Activity.model_validate(activity)
    await host.agent_app.handlers["conversationUpdate"](turn, None)
    assert turn.send_activity.await_count == 1
    activity.update(type="installationUpdate", action="remove")
    turn.activity = Activity.model_validate(activity)
    await host.agent_app.handlers["installationUpdate"](turn, None)
    assert turn.send_activity.await_count == 1


async def test_member_added_welcome_uses_matching_sender_name(monkeypatch, activity):
    host, _ = compose(monkeypatch, None)
    activity.update(
        type="conversationUpdate",
        membersAdded=[
            {"id": activity["from"]["id"], "name": "Adele"},
            {"id": "bot", "name": "Bot"},
        ],
    )
    turn = SimpleNamespace(activity=Activity.model_validate(activity), send_activity=AsyncMock())
    await host.agent_app.handlers["conversationUpdate"](turn, None)
    assert (
        turn.send_activity.call_args.args[0].attachments[0].content["body"][1]["text"]
        == r"Hello, Adele\!"
    )


@pytest.mark.parametrize("group", [False, True])
async def test_greetings_do_not_personalize_other_tenants_or_group_chats(
    monkeypatch, activity, group
):
    host, _ = compose(monkeypatch, None)
    activity["text"] = "Hello"
    activity["from"]["name"] = "Do not greet this name"
    if group:
        activity["conversation"]["conversationType"] = "groupChat"
    else:
        activity["channelData"]["tenant"]["id"] = "00000000-0000-0000-0000-000000000099"
    turn = SimpleNamespace(activity=Activity.model_validate(activity), send_activity=AsyncMock())
    await host.agent_app.handlers["message"](turn, None)
    assert "Do not greet this name" not in str(turn.send_activity.call_args)


async def test_greeting_with_issue_still_reaches_assistant(
    monkeypatch, snow, memory, principal, activity
):
    context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "call")
    host, _ = compose(monkeypatch, context)
    activity["text"] = "Hello, my printer is not working"
    turn = SimpleNamespace(
        activity=Activity.model_validate(activity),
        streaming_response=None,
        send_activity=AsyncMock(),
    )
    await host.agent_app.handlers["message"](turn, None)
    assert (
        turn.send_activity.call_args_list[0].args[0].attachments[0].content["body"][2]["text"]
        == "Please describe your issue."
    )
