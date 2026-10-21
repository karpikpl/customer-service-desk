from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from microsoft_agents.activity import Activity

from service_desk.actions import Actions
from service_desk.commands import COMMANDS, parse_command
from service_desk.identity import IdentityMapper
from service_desk.models import Article, CaseProgress, DeskError, DraftRequest
from service_desk.state import conversation_principal
from service_desk.toolbox import ToolboxError
from service_desk.tools import ToolContext

from .conftest import CALLER, TENANT, USER
from .test_main import compose


@pytest.fixture
def commands_host(monkeypatch, snow, memory, principal, mapping, activity):
    initial = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "verified-call")
    host, runtime = compose(monkeypatch, initial)
    runtime.agent.ask = Mock(side_effect=AssertionError("Commands must never call the model"))
    runtime.agent.reset = AsyncMock()
    runtime.initialize = AsyncMock()
    runtime.mapper = IdentityMapper(TENANT, mapping)
    runtime.toolbox = SimpleNamespace(
        list_skills=AsyncMock(return_value=[{"name": "printer-troubleshooting"}])
    )
    monkeypatch.setattr("service_desk.main.hosted_call_id", lambda: "verified-call")
    monkeypatch.setenv("FOUNDRY_AGENT_VERSION", "test-version")

    @asynccontextmanager
    async def turn(payload):
        current = await conversation_principal(
            memory, runtime.mapper.resolve(payload), refresh=True
        )
        yield ToolContext(
            current, snow, memory, Actions(snow, memory), runtime.toolbox, "verified-call"
        )

    runtime.turn = turn
    counter = 0

    async def send(text="", *, value=None, invoke=False, change=None):
        nonlocal counter
        counter += 1
        payload = copy.deepcopy(activity)
        payload.update(text=text, id=f"incoming-{counter}")
        if value is not None:
            payload["value"] = value
        if invoke:
            payload.update(type="invoke", name="adaptiveCard/action", replyToId="rated-message")
        if change:
            change(payload)
        context = SimpleNamespace(
            activity=Activity.model_validate(payload),
            send_activity=AsyncMock(return_value=SimpleNamespace(id=f"outgoing-{counter}")),
            update_activity=AsyncMock(),
        )
        await host.agent_app.handlers["invoke" if invoke else "message"](context, None)
        return context

    return SimpleNamespace(send=send, runtime=runtime, initial=initial)


def card_from(context):
    return context.send_activity.call_args.args[0].attachments[0].content


@pytest.mark.parametrize("name", COMMANDS)
def test_command_parsing_is_explicit_and_case_insensitive(name):
    assert parse_command(f" /{name.upper()} ").name == name


@pytest.mark.parametrize(
    "text",
    [
        "/unknown",
        "/whoami someone@example.com",
        "/new delete records",
        "/agent secrets",
        "/incidents 0",
        "/incidents -1",
        "/incidents all-users",
        "/incidents 1002",
        "/new/extra",
        "/incident " + "x" * 161,
        "/kb " + "x" * 121,
    ],
)
def test_invalid_commands_never_become_model_prompts(text):
    with pytest.raises(DeskError):
        parse_command(text)


def test_normal_text_is_not_a_command():
    assert parse_command("Please find /kb printer") is None


async def test_incidents_are_owned_paginated_and_include_closed(commands_host, principal, requests):
    response = await commands_host.send("/incidents 2")
    value = card_from(response)
    assert value["body"][1]["text"] == "Your incidents"
    query = requests[0].url.params
    assert query["sysparm_query"] == f"caller_id={principal.caller_id}^ORDERBYDESCsys_updated_on"
    assert query["sysparm_offset"] == "5"
    assert "active=" not in query["sysparm_query"]
    assert all(r.method == "GET" for r in requests)
    commands_host.runtime.agent.ask.assert_not_called()


async def test_incident_command_opens_reviewed_intake_without_writing(commands_host, requests):
    value = card_from(await commands_host.send("/incident Printer is offline"))
    assert value["body"][1]["text"] == "Report an issue"
    title = next(item for item in value["body"] if item.get("id") == "title")
    assert title["value"] == "Printer is offline"
    assert value["actions"][0]["verb"] == "prepare_create"
    assert requests == []


async def test_disabled_incident_creation_is_explicit(commands_host, snow, requests):
    snow.policy = snow.policy.model_copy(update={"allow_create": False})
    value = card_from(await commands_host.send("/incident"))
    assert value["body"][1]["text"] == "Incident creation is disabled"
    assert not any(item["type"].startswith("Input.") for item in value["body"])
    assert not any(item.get("id") == "service-now-portal" for item in value["body"])
    assert "https://mock.service-now.com/sp" not in str(value)
    assert requests == []


async def test_kb_form_and_search_are_deterministic(commands_host, snow, requests, monkeypatch):
    value = card_from(await commands_host.send("/kb"))
    assert value["actions"][0]["verb"] == "kb_search"
    assert value["actions"][0]["associatedInputs"] == "auto"
    assert requests == []
    search = AsyncMock(
        return_value=[
            Article(
                number="KB0001",
                title="Printer guide",
                text="<p>Check the selected queue.</p>",
                url="https://mock.service-now.com/kb_view.do?sys_kb_id=" + "a" * 32,
            )
        ]
    )
    monkeypatch.setattr(snow, "search_knowledge", search)
    value = card_from(await commands_host.send("/kb printer"))
    search.assert_awaited_once_with("printer")
    assert "Printer guide" in str(value)
    assert "kb_view.do" in str(value)
    assert "<p>" not in str(value)
    commands_host.runtime.agent.ask.assert_not_called()


async def test_kb_form_submit_never_uses_model(commands_host, snow, monkeypatch):
    search = AsyncMock(return_value=[])
    monkeypatch.setattr(snow, "search_knowledge", search)
    value = card_from(await commands_host.send(value={"verb": "kb_search", "kb_query": "printer"}))
    assert "No approved articles matched" in str(value)
    search.assert_awaited_once_with("printer")


async def test_kb_command_does_not_expand_knowledge_access(commands_host, requests):
    value = card_from(await commands_host.send("/kb printer"))
    assert "Approved ServiceNow knowledge is unavailable" in str(value)
    assert requests == []


async def test_agent_command_lists_allowlisted_metadata_without_secrets(commands_host):
    value = card_from(await commands_host.send("/agent"))
    assert "test-version" in str(value)
    assert "printer-troubleshooting" in str(value)
    assert "GitHub Copilot SDK" in str(value)
    assert "vault.azure.net" not in str(value)
    assert "connection_string" not in str(value)
    commands_host.runtime.initialize.assert_not_awaited()
    commands_host.runtime.toolbox.list_skills.assert_awaited_once_with("verified-call")


async def test_agent_details_explain_unavailable_catalog(commands_host):
    commands_host.runtime.toolbox.list_skills.side_effect = ToolboxError("Unavailable")
    value = card_from(await commands_host.send("/agent"))
    assert "Skill catalog unavailable" in str(value)
    assert "Agent details" in str(value)


async def test_whoami_shows_only_authenticated_self(commands_host):
    value = card_from(await commands_host.send("/whoami"))
    assert USER in str(value)
    assert TENANT in str(value)
    assert CALLER in str(value)
    commands_host.runtime.agent.ask.assert_not_called()


async def test_unmapped_member_can_identify_themselves(commands_host):
    other = "00000000-0000-0000-0000-000000000099"
    value = card_from(
        await commands_host.send("/whoami", change=lambda a: a["from"].update(aadObjectId=other))
    )
    assert other in str(value)
    assert "Not mapped" in str(value)
    assert CALLER not in str(value)


async def test_feedback_command_and_form_use_only_telemetry(commands_host, requests, memory):
    value = card_from(await commands_host.send("/feedback"))
    submit = value["actions"][0]
    assert submit["verb"] == "feedback_submit"
    assert submit["associatedInputs"] == "auto"
    commands_host.runtime.feedback.record.assert_not_called()
    response = await commands_host.send(
        value={
            "action": {
                "verb": "feedback_submit",
                "data": {
                    "verb": "feedback_submit",
                    "feedback_text": "Clearer buttons please",
                    "feedback_rating": "negative",
                    "feedback_response_id": "original-response",
                },
            },
        },
        invoke=True,
    )
    assert response.send_activity.call_args.args[0].value.body["statusCode"] == 200
    response.update_activity.assert_not_awaited()
    args = commands_host.runtime.feedback.record.call_args.kwargs
    assert args["source"] == "command"
    assert args["rating"] == "negative"
    assert args["text"] == "Clearer buttons please"
    assert args["response_id"] == "original-response"
    await commands_host.send("/feedback Please use simpler labels")
    args = commands_host.runtime.feedback.record.call_args.kwargs
    assert args["text"] == "Please use simpler labels"
    assert args["rating"] == "neutral"
    assert requests == []
    assert memory.data == {}


async def test_empty_feedback_form_is_rejected(commands_host):
    response = await commands_host.send(
        value={
            "action": {"verb": "feedback_submit", "data": {"verb": "feedback_submit"}},
        },
        invoke=True,
    )
    assert response.send_activity.call_args.args[0].value.body["statusCode"] == 400
    commands_host.runtime.feedback.record.assert_not_called()


async def test_new_rotates_context_and_invalidates_prior_reviews_and_cases(
    commands_host, principal, memory, snow, requests
):
    await commands_host.initial.save_case(
        CaseProgress(
            skill_name="printer-troubleshooting",
            skill_digest="pinned",
            skill_content="Old case",
            summary="Do not carry this into the fresh conversation.",
        )
    )
    draft = await commands_host.initial.actions.prepare(
        principal,
        DraftRequest(operation="create", short_description="Old draft", description="Old"),
    )
    value = card_from(await commands_host.send("/new"))
    assert "A fresh agent conversation has started" in str(value)
    commands_host.runtime.agent.reset.assert_awaited_once_with(principal)
    current = await conversation_principal(memory, principal)
    assert current.scope != principal.scope
    assert current.caller_id == principal.caller_id
    assert current.conversation_id == principal.conversation_id
    fresh = ToolContext(current, snow, memory, Actions(snow, memory), Mock(), "call")
    assert await fresh.active_case() is None
    with pytest.raises(DeskError):
        await fresh.actions.confirm(current, draft.id)
    with pytest.raises(DeskError, match="reset"):
        await fresh.actions.confirm(principal, draft.id)
    value = card_from(await commands_host.send(value={"verb": "confirm", "action_id": draft.id}))
    assert "review is unavailable" in str(value)
    assert requests == []
    commands_host.runtime.agent.ask.assert_not_called()


async def test_failed_sdk_reset_does_not_claim_a_new_conversation(commands_host, memory):
    commands_host.runtime.agent.reset.side_effect = RuntimeError("SDK unavailable")
    value = card_from(await commands_host.send("/new"))
    assert value["body"][1]["text"] == "Service temporarily unavailable"
    assert not any(key.endswith(":conversation") for key in memory.data)


@pytest.mark.parametrize(
    "command", ["/feedback text", "/incident", "/kb", "/agent", "/new", "/incidents", "/whoami"]
)
@pytest.mark.parametrize("group", [False, True])
async def test_commands_reject_other_tenants_and_group_chats(commands_host, command, group):
    def change(activity):
        if group:
            activity["conversation"]["conversationType"] = "groupChat"
        else:
            activity["channelData"]["tenant"]["id"] = "00000000-0000-0000-0000-000000000099"

    value = card_from(await commands_host.send(command, change=change))
    assert value["body"][1]["text"] == "Unable to continue"
    commands_host.runtime.feedback.record.assert_not_called()
    commands_host.runtime.initialize.assert_not_awaited()
    commands_host.runtime.toolbox.list_skills.assert_not_awaited()


async def test_unknown_command_returns_help_not_model(commands_host):
    value = card_from(await commands_host.send("/make-stuff-up"))
    assert "Use /help" in str(value)
    commands_host.runtime.agent.ask.assert_not_called()
