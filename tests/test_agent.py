from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from azure.core.credentials import AccessToken
from copilot import SessionEventType, ToolInvocation

from service_desk import cards
from service_desk.actions import Actions
from service_desk.agent import DeskAgent, denied_permission
from service_desk.models import CaseOption, CaseProgress, DeskError
from service_desk.toolbox import ToolboxError
from service_desk.tools import ToolContext, build_tools

from .test_config import settings_values


async def test_agent_allows_only_explicit_custom_tools_and_rebinds_each_turn(
    monkeypatch, tmp_path, snow, memory, principal
):
    from service_desk.config import Settings

    monkeypatch.setattr("service_desk.agent.Path.home", lambda: tmp_path)
    credential = Mock()
    credential.get_token = AsyncMock(return_value=AccessToken("synthetic-token", 9999999999))
    client = Mock()
    session = Mock()
    session.disconnect = AsyncMock()
    session.on.side_effect = lambda callback: setattr(session, "callback", callback) or Mock()

    async def send(_prompt):
        for kind, content in (
            (SessionEventType.ASSISTANT_MESSAGE_DELTA, "Your incidents"),
            (SessionEventType.SESSION_IDLE, ""),
        ):
            session.callback(
                SimpleNamespace(type=kind, data=SimpleNamespace(delta_content=content))
            )

    session.send = send
    client.start = AsyncMock()
    client.stop = AsyncMock()
    client.list_sessions = AsyncMock(return_value=[])
    client.create_session = AsyncMock(return_value=session)
    client.resume_session = AsyncMock(return_value=session)
    monkeypatch.setattr("service_desk.agent.CopilotClient", lambda **kwargs: client)
    agent = DeskAgent(Settings(**settings_values()), credential)
    catalog = Mock()
    catalog.list_skills = AsyncMock(
        return_value=[
            {
                "name": "printer-troubleshooting",
                "description": "Printing help",
                "uri": "skill://printer",
            }
        ]
    )
    context = ToolContext(principal, snow, memory, Actions(snow, memory), catalog, "first-call")
    output = [value async for value in agent.ask(context, "My incidents")]
    assert output[-1] == ("final", "Your incidents")
    options = client.create_session.call_args.kwargs
    names = [tool.name for tool in options["tools"]]
    assert options["available_tools"].to_list() == ["custom:" + name for name in names]
    assert options["excluded_tools"].to_list() == ["builtin:*", "mcp:*"]
    assert options["enable_config_discovery"] is False
    assert options["enable_skills"] is False
    assert options["skip_custom_instructions"] is True
    assert options["on_permission_request"] is denied_permission
    assert options["reasoning_summary"] == "none"
    assert options["provider"]["headers"]["x-agent-foundry-call-id"] == "first-call"
    provider = options["provider"]["bearer_token_provider"]
    assert await provider(None) == "synthetic-token"
    credential.get_token.assert_awaited_once_with("https://ai.azure.com/.default")

    client.list_sessions.return_value = [SimpleNamespace(session_id="desk-" + principal.scope[:40])]
    context2 = ToolContext(principal, snow, memory, Actions(snow, memory), catalog, "second-call")
    _ = [value async for value in agent.ask(context2, "Continue")]
    resumed = client.resume_session.call_args.kwargs
    assert resumed["continue_pending_work"] is False
    assert resumed["reasoning_summary"] == "none"
    assert resumed["tools"][0] is not options["tools"][0]
    assert resumed["provider"]["headers"]["x-agent-foundry-call-id"] == "second-call"
    reset_principal = principal.model_copy(update={"conversation_generation": "b" * 32})
    fresh_context = ToolContext(
        reset_principal, snow, memory, Actions(snow, memory), catalog, "after-reset"
    )
    _ = [value async for value in agent.ask(fresh_context, "A fresh conversation")]
    assert client.create_session.call_args.kwargs["session_id"] == (
        "desk-" + reset_principal.scope[:40]
    )
    assert client.create_session.call_args.kwargs["session_id"] != ("desk-" + principal.scope[:40])
    assert client.resume_session.await_count == 1
    assert client.start.await_count == 1
    choices = ToolContext(principal, snow, memory, Actions(snow, memory), catalog, "choices")
    choices.outbox.append(
        cards.case_options(
            "Email support",
            "What would you like to do?",
            [
                CaseOption(id="triage", label="Troubleshoot email"),
                CaseOption(id="view", label="View incident"),
            ],
            "c" * 32,
        )
    )
    reply = [value async for value in agent.ask(choices, "Offer choices")]
    assert reply[-1] == (
        "final",
        "Choose an option on the card to continue. No ticket changes were made.",
    )
    question = ToolContext(principal, snow, memory, Actions(snow, memory), catalog, "question")
    question.outbox.append(cards.case_question("Email", "What error do you see?", "d" * 32))
    reply = [value async for value in agent.ask(question, "Ask for the error")]
    assert reply[-1] == ("final", "Use the question card to continue.")
    await agent.close()
    client.stop.assert_awaited_once()


async def test_pinned_skill_survives_new_tool_context_and_toolbox_outage(snow, memory, principal):
    catalog = Mock()
    catalog.load_skill = AsyncMock(side_effect=ToolboxError("Unavailable"))
    context = ToolContext(principal, snow, memory, Actions(snow, memory), catalog, "call")
    await context.save_case(
        CaseProgress(
            skill_name="printer-troubleshooting",
            skill_digest="old-digest",
            skill_content="Approved old procedure",
            summary="Test page failed",
        )
    )
    restarted = ToolContext(principal, snow, memory, Actions(snow, memory), catalog, "new-call")
    load = next(tool for tool in build_tools(restarted) if tool.name == "load_case_skill")
    result = await load.handler(ToolInvocation(arguments={"name": "printer-troubleshooting"}))
    assert result.result_type == "success"
    assert "Approved old procedure" in result.text_result_for_llm
    catalog.load_skill.assert_not_awaited()


@pytest.mark.parametrize("interaction", ["options", "information", "missing", "knowledge"])
async def test_unrendered_question_gets_one_bounded_card_repair(
    monkeypatch, tmp_path, snow, memory, principal, requests, interaction
):
    from service_desk.config import Settings

    monkeypatch.setattr("service_desk.agent.Path.home", lambda: tmp_path)
    credential = Mock()
    session = Mock()
    session.disconnect = AsyncMock()
    session.on.side_effect = lambda callback: setattr(session, "callback", callback) or Mock()
    client = Mock(
        start=AsyncMock(),
        stop=AsyncMock(),
        list_sessions=AsyncMock(return_value=[]),
        create_session=AsyncMock(return_value=session),
    )
    monkeypatch.setattr("service_desk.agent.CopilotClient", lambda **kwargs: client)
    catalog = Mock(list_skills=AsyncMock(return_value=[]))
    context = ToolContext(principal, snow, memory, Actions(snow, memory), catalog, "call")
    context.knowledge_searched = interaction == "knowledge"
    prompts = []
    question = (
        "What error do you see?"
        if interaction == "information"
        else "Would you like local or remote checks?"
    )

    async def send(prompt):
        prompts.append(prompt)
        if len(prompts) == 2 and interaction in ("options", "information"):
            tools = client.create_session.call_args.kwargs["tools"]
            if interaction == "options":
                name = "show_case_options"
                arguments = {
                    "title": "Printing",
                    "question": question,
                    "summary": "Basic printer checks requested",
                    "options": [
                        {"id": "local", "label": "Local checks"},
                        {"id": "remote", "label": "Remote checks"},
                    ],
                }
            else:
                name = "show_case_step"
                arguments = {
                    "title": "Printing",
                    "step": question,
                    "summary": "Printing error",
                    "interaction": "information",
                }
            result = await next(tool for tool in tools if tool.name == name).handler(
                ToolInvocation(arguments=arguments)
            )
            assert result.result_type == "success"
        for kind, content in (
            (
                SessionEventType.ASSISTANT_MESSAGE,
                "Please choose a path."
                if interaction == "missing" and len(prompts) == 2
                else question,
            ),
            (SessionEventType.SESSION_IDLE, ""),
        ):
            session.callback(SimpleNamespace(type=kind, data=SimpleNamespace(content=content)))

    session.send = send
    agent = DeskAgent(Settings(**settings_values()), credential)
    try:
        if interaction == "missing":
            with pytest.raises(DeskError, match="could not display the support question"):
                _ = [item async for item in agent.ask(context, "Help with printing")]
            assert context.outbox == []
        else:
            _ = [item async for item in agent.ask(context, "Help with printing")]
            assert len(context.outbox) == (0 if interaction == "knowledge" else 1)
        assert len(prompts) == (1 if interaction == "knowledge" else 2)
        if len(prompts) == 2:
            assert "show_case_options" in prompts[1]
            assert "interaction=information" in prompts[1]
        assert requests == []
        session.disconnect.assert_awaited_once()
    finally:
        await agent.close()


async def test_case_step_requires_skill_and_persists_progress(snow, memory, principal):
    catalog = Mock()
    context = ToolContext(principal, snow, memory, Actions(snow, memory), catalog, "call")
    step = next(tool for tool in build_tools(context) if tool.name == "show_case_step")
    args = {
        "title": "Printing",
        "step": "Check the selected printer.",
        "summary": "Offline printer",
    }
    denied = await step.handler(ToolInvocation(arguments=args))
    assert denied.result_type == "failure"
    await context.save_case(
        CaseProgress(
            skill_name="printer-troubleshooting",
            skill_digest="digest",
            skill_content="Safe guidance",
            knowledge_status="unavailable",
        )
    )
    result = await step.handler(ToolInvocation(arguments=args))
    assert result.result_type == "success"
    saved = await context.active_case()
    assert saved.step == args["step"]
    assert saved.summary == args["summary"]
    assert context.outbox[-1]["actions"][0]["data"]["case_token"] == saved.step_token


def test_permission_requests_are_denied():
    assert type(denied_permission()).__name__ == "PermissionDecisionReject"
