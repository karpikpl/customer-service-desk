from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest
from copilot import ToolInvocation

from service_desk import cards
from service_desk.actions import Actions
from service_desk.activities import CardAction, dispatch, match_case_option, parse_action
from service_desk.models import CaseOption, CaseProgress, DeskError
from service_desk.tools import ToolContext, build_tools

OPTIONS = [
    {"id": "remote", "label": "Remote checks", "description": "From your computer."},
    {"id": "local", "label": "Local checks", "description": "Observe the device."},
]
PARAMS = {
    "title": "Printer troubleshooting",
    "question": "Where would you like to check?",
    "summary": "The user's printer is unavailable. Path not yet chosen.",
    "options": OPTIONS,
}


@pytest.fixture
async def context(snow, memory, principal):
    result = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "call")
    await result.save_case(
        CaseProgress(
            skill_name="printer-troubleshooting",
            skill_digest="sha256:pinned",
            skill_content="Pinned routing procedure",
            knowledge_status="unavailable",
        )
    )
    return result


async def show_options(context, params=None):
    tool = next(tool for tool in build_tools(context) if tool.name == "show_case_options")
    return await tool.handler(ToolInvocation(arguments=params if params is not None else PARAMS))


async def test_general_choices_do_not_require_a_loaded_skill(snow, memory, principal):
    context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "call")
    result = await show_options(context)
    assert result.result_type == "success"
    assert len(context.outbox) == 1
    assert (await context.active_case()).skill_name == ""
    assert (await context.active_case()).options


@pytest.mark.parametrize(
    "options",
    [
        OPTIONS[:1],
        OPTIONS * 2,
        [OPTIONS[0], OPTIONS[0]],
        [OPTIONS[0], {**OPTIONS[1], "label": "REMOTE CHECKS"}],
        [OPTIONS[0], {**OPTIONS[1], "id": "../other"}],
        [OPTIONS[0], {**OPTIONS[1], "label": " "}],
        [OPTIONS[0], {**OPTIONS[1], "url": "https://untrusted.example"}],
    ],
)
async def test_invalid_options_do_not_save_or_display(context, options):
    before = await context.active_case()
    result = await show_options(context, {**PARAMS, "options": options})
    assert result.result_type == "failure"
    assert context.outbox == []
    assert await context.active_case() == before


@pytest.mark.parametrize("choice_id", ["remote", "local"])
async def test_selected_path_is_saved_and_consumed_once(context, requests, choice_id):
    assert (await show_options(context)).result_type == "success"
    progress = await context.active_case()
    action = CardAction(verb="case_option", case_token=progress.step_token, option_id=choice_id)
    card, prompt = await dispatch(context, action)
    assert card is None
    assert choice_id in prompt
    assert "pinned case skill" in prompt
    updated = await context.active_case()
    assert updated.selected_option.id == choice_id
    assert updated.options == []
    assert updated.step_token != progress.step_token
    assert updated.skill_digest == "sha256:pinned"
    assert updated.summary == PARAMS["summary"]
    with pytest.raises(DeskError, match="outdated"):
        await dispatch(context, action)
    assert requests == []


async def test_only_saved_choice_identifiers_are_accepted(context):
    await show_options(context)
    progress = await context.active_case()
    with pytest.raises(DeskError, match="Choose one"):
        await dispatch(
            context,
            CardAction(verb="case_option", case_token=progress.step_token, option_id="publish-kb"),
        )
    assert await context.active_case() == progress


async def test_choice_cannot_cross_user_or_conversation(context, snow, memory, principal):
    await show_options(context)
    progress = await context.active_case()
    for other_principal in [
        principal.model_copy(update={"object_id": "other-user"}),
        principal.model_copy(update={"conversation_id": "other-conversation"}),
    ]:
        other = ToolContext(
            other_principal, snow, memory, Actions(snow, memory), Mock(), "other-call"
        )
        with pytest.raises(DeskError, match="outdated"):
            await dispatch(
                other,
                CardAction(verb="case_option", case_token=progress.step_token, option_id="remote"),
            )
    assert await context.active_case() == progress


async def test_replaced_choices_make_previous_card_stale(context):
    await show_options(context)
    old = await context.active_case()
    await show_options(context)
    with pytest.raises(DeskError, match="outdated"):
        await dispatch(
            context, CardAction(verb="case_option", case_token=old.step_token, option_id="remote")
        )


@pytest.mark.parametrize("reply", ["remote", "Remote checks", "  REMOTE CHECKS  ", "local"])
async def test_exact_typed_choices_use_the_same_registered_dispatch(context, reply):
    await show_options(context)
    action = await match_case_option(context, reply)
    assert action.verb == "case_option"
    assert action.option_id == ("local" if reply == "local" else "remote")
    await dispatch(context, action)
    assert (await context.active_case()).selected_option.id == action.option_id


@pytest.mark.parametrize("reply", ["yes", "confirm", "choose for me", "remote and local"])
async def test_other_typed_replies_are_not_inferred_as_a_choice_or_confirmation(context, reply):
    await show_options(context)
    assert await match_case_option(context, reply) is None


async def test_ambiguous_typed_choice_requires_button(context):
    await show_options(
        context,
        {
            **PARAMS,
            "options": [
                {"id": "remote", "label": "Computer"},
                {"id": "computer", "label": "Other"},
            ],
        },
    )
    with pytest.raises(DeskError, match="multiple"):
        await match_case_option(context, "computer")


async def test_concurrent_choice_clicks_have_one_winner(context):
    await show_options(context)
    progress = await context.active_case()
    results = await asyncio.gather(
        *[
            dispatch(
                context,
                CardAction(verb="case_option", case_token=progress.step_token, option_id=choice),
            )
            for choice in ("remote", "local")
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(result, tuple) for result in results) == 1
    assert sum(isinstance(result, DeskError) for result in results) == 1


async def test_step_cannot_bypass_pending_choice_and_retains_selected_path(context):
    await show_options(context)
    step_tool = next(tool for tool in build_tools(context) if tool.name == "show_case_step")
    invocation = ToolInvocation(
        arguments={"title": "Printing", "step": "Check the queue.", "summary": "Remote path."}
    )
    assert (await step_tool.handler(invocation)).result_type == "failure"
    progress = await context.active_case()
    with pytest.raises(DeskError, match="Choose an option"):
        await dispatch(context, CardAction(verb="case_worked", case_token=progress.step_token))
    await dispatch(
        context, CardAction(verb="case_option", case_token=progress.step_token, option_id="remote")
    )
    assert (await step_tool.handler(invocation)).result_type == "success"
    assert (await context.active_case()).selected_option.id == "remote"


def test_options_actions_share_execute_submit_dispatch_and_escape_text():
    option = CaseOption(
        id="remote", label="<b>Remote</b>", description="[bad](https://bad.example)"
    )
    card = cards.case_options("Choose", "Which path?", [option], "a" * 32)
    execute = card["actions"][0]
    assert set(execute["data"]) == {"verb", "option_id", "case_token"}
    assert parse_action(
        {"type": "invoke", "name": "adaptiveCard/action", "value": {"action": execute}}
    ) == parse_action({"type": "message", "value": execute["fallback"]["data"]})
    assert card["body"][3]["items"][0]["text"] == "Remote"
    assert card["body"][3]["items"][1]["text"] == r"\[bad\]\(https://bad.example\)"


def test_old_saved_cases_remain_readable():
    progress = CaseProgress.model_validate(
        {
            "skill_name": "printer-troubleshooting",
            "skill_digest": "old",
            "skill_content": "Previously pinned procedure",
        }
    )
    assert progress.options == []
    assert progress.selected_option is None
