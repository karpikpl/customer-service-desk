from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from service_desk.actions import Actions
from service_desk.activities import CardAction, dispatch, parse_action
from service_desk.models import DeskError
from service_desk.tools import ToolContext, build_tools


def test_execute_and_submit_share_normalization():
    data = {"verb": "view", "incident": "INC0012345"}
    execute = {
        "type": "invoke",
        "name": "adaptiveCard/action",
        "value": {"action": {"type": "Action.Execute", "verb": "view", "data": data}},
    }
    submit = {"type": "message", "value": data}
    assert parse_action(execute) == parse_action(submit)


@pytest.mark.parametrize(
    "value",
    [
        {"verb": "confirm", "action_id": "../../other-user"},
        {"verb": "confirm", "caller_id": "another"},
        {"verb": "prepare_comment", "display_name": "Someone else"},
        {"verb": "list", "page": -1},
        {"verb": "delete_all"},
    ],
)
def test_forged_action_data_rejected(value):
    with pytest.raises(DeskError):
        parse_action({"type": "message", "value": value})


def test_execute_verb_cannot_disagree_with_data():
    with pytest.raises(DeskError, match="inconsistent"):
        parse_action(
            {
                "type": "invoke",
                "name": "adaptiveCard/action",
                "value": {"action": {"verb": "list", "data": {"verb": "confirm"}}},
            }
        )


def test_typed_yes_is_not_confirmation():
    assert parse_action({"type": "message", "text": "yes"}) is None


async def test_edit_cancels_old_draft_before_new_form(snow, memory, principal, requests):
    actions = Actions(snow, memory)
    context = ToolContext(principal, snow, memory, actions, Mock(), "call")
    from service_desk.models import DraftRequest

    draft = await actions.prepare(
        principal,
        DraftRequest(operation="create", short_description="Printer", description="Offline"),
    )
    result, prompt = await dispatch(context, CardAction(verb="edit", action_id=draft.id))
    assert not prompt
    assert result["body"][1]["text"] == "Report an issue"
    with pytest.raises(DeskError):
        await actions.confirm(principal, draft.id)
    assert all(request.method == "GET" for request in requests)


async def test_edit_comment_does_not_duplicate_attribution(snow, memory, principal):
    from service_desk.models import DraftRequest

    principal = principal.model_copy(update={"display_name": "Adele Vance"})
    actions = Actions(snow, memory)
    context = ToolContext(principal, snow, memory, actions, Mock(), "call")
    draft = await actions.prepare(
        principal,
        DraftRequest(operation="comment", incident_number="INC0012345", comment="Still stuck"),
    )
    form, _ = await dispatch(context, CardAction(verb="edit", action_id=draft.id))
    assert (
        next(item for item in form["body"] if item.get("id") == "comment")["value"] == "Still stuck"
    )
    review, _ = await dispatch(
        context,
        CardAction(verb="prepare_comment", incident="INC0012345", comment="Still stuck"),
    )
    assert json.dumps(review).count("Submitted by") == 1


async def test_model_tool_cannot_confirm_or_change_identity(snow, memory, principal, requests):
    from copilot import ToolInvocation

    context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "call")
    tools = build_tools(context)
    assert {t.name for t in tools}.isdisjoint({"confirm", "execute", "bash", "read_file"})
    prepare = next(t for t in tools if t.name == "prepare_incident_change")
    bad = await prepare.handler(
        ToolInvocation(arguments={"operation": "create", "caller_id": "other"})
    )
    assert bad.result_type == "failure"
    good = await prepare.handler(
        ToolInvocation(
            arguments={
                "operation": "create",
                "short_description": "Printer",
                "description": "Offline",
            }
        )
    )
    assert good.result_type == "success"
    assert context.outbox[0]["body"][1]["text"] == "Report an issue"
    assert all(request.method == "GET" for request in requests)


async def test_old_case_card_cannot_advance_current_step(snow, memory, principal):
    from service_desk.models import CaseProgress

    context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "call")
    await context.save_case(
        CaseProgress(
            skill_name="printer-troubleshooting",
            skill_digest="digest",
            skill_content="Approved procedure",
            step_token="a" * 32,
        )
    )
    with pytest.raises(DeskError, match="outdated"):
        await dispatch(context, CardAction(verb="case_failed", case_token="b" * 32))
    assert (await context.active_case()).outcome == ""


async def test_incident_follow_up_rechecks_ownership_and_carries_context(
    snow, memory, principal, requests
):
    context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "call")
    card, prompt = await dispatch(context, CardAction(verb="troubleshoot", incident="INC0012345"))
    assert card is None
    assert "Printer unavailable" in prompt
    assert principal.caller_id not in prompt
    assert requests[-1].url.params["sysparm_query"].endswith("caller_id=" + principal.caller_id)
