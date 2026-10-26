from __future__ import annotations

import asyncio
import json
from unittest.mock import Mock

import pytest
from copilot import ToolInvocation
from pydantic import ValidationError

from service_desk import cards
from service_desk.actions import Actions
from service_desk.activities import CardAction, dispatch
from service_desk.commands import Command, run_data_command
from service_desk.models import DeskError, DraftRequest
from service_desk.tools import ToolContext, build_tools


def create_request(**changes):
    return DraftRequest(
        **{
            "operation": "create",
            "short_description": "Printer is offline",
            "description": "One print failed.",
            "category": "hardware",
            "impact": "2",
            "urgency": "1",
            "reported_location": "Floor 2",
            "device": "Printer A",
            "business_impact": "Team cannot print shipping labels.",
            "attempted_steps": "Checked the selected queue.",
            **changes,
        }
    )


async def test_richer_payload_uses_choices_and_keeps_context_out_of_reference_fields(
    snow, memory, principal, requests
):
    draft = await Actions(snow, memory).prepare(principal, create_request())
    assert draft.payload == {
        "caller_id": principal.caller_id,
        "short_description": "Printer is offline",
        "description": (
            "One print failed.\n\nReported location: Floor 2\n\nDevice / printer: Printer A"
            "\n\nBusiness impact: Team cannot print shipping labels."
            "\n\nAlready tried: Checked the selected queue."
        ),
        "category": "hardware",
        "impact": "2",
        "urgency": "1",
        "contact_type": "virtual_agent",
    }
    assert all(request.method == "GET" for request in requests)
    await Actions(snow, memory).confirm(principal, draft.id)
    assert len([request for request in requests if request.url.path.endswith("/sys_choice")]) == 1
    assert json.loads(
        next(request.content for request in requests if request.method == "POST")
    ) == (draft.payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"category": "invented"},
        {"category": "hardware", "subcategory": "email"},
        {"category": "hardware", "subcategory": "printer"},
        {"category": "", "subcategory": "email"},
        {"impact": "2", "urgency": ""},
        {"description": "x" * 4000},
    ],
)
async def test_invalid_or_overlong_intake_is_not_written(
    snow, memory, principal, requests, changes
):
    with pytest.raises(DeskError):
        await Actions(snow, memory).prepare(principal, create_request(**changes))
    assert all(request.method == "GET" for request in requests)


async def test_valid_dependent_subcategory(snow, memory, principal):
    draft = await Actions(snow, memory).prepare(
        principal, create_request(category="software", subcategory="email")
    )
    assert draft.payload["subcategory"] == "email"


@pytest.mark.parametrize("field,value", [("impact", "0"), ("urgency", "high"), ("priority", "1")])
def test_intake_rejects_arbitrary_priority_and_invalid_levels(field, value):
    with pytest.raises(ValidationError):
        create_request(**{field: value})


@pytest.mark.parametrize("require_confirmation", [True, False])
async def test_form_is_prefilled_and_submit_mode_matches_configuration(
    snow, memory, principal, requests, require_confirmation
):
    actions = Actions(snow, memory, require_confirmation=require_confirmation)
    context = ToolContext(principal, snow, memory, actions, Mock(), "call")
    tool = next(t for t in build_tools(context) if t.name == "prepare_incident_change")
    result = await tool.handler(ToolInvocation(arguments=create_request().model_dump()))
    assert result.result_type == "success"
    assert tool.is_terminal
    form = context.outbox[-1]
    assert form["actions"][0]["title"] == (
        "Review incident" if require_confirmation else "Create incident"
    )
    assert next(item for item in form["body"] if item.get("id") == "title")["value"] == (
        "Printer is offline"
    )
    assert all(request.method == "GET" for request in requests)
    action_data = {
        **form["actions"][0]["data"],
        "title": "Printer is offline",
        "description": "One print failed.",
        "classification": "hardware",
        "impact": "2",
        "urgency": "1",
        "reported_location": "Floor 2",
    }
    card, _ = await dispatch(context, CardAction.model_validate(action_data))
    assert card["body"][1]["text"] == (
        "Review your change" if require_confirmation else "Change confirmed"
    )
    assert len([request for request in requests if request.method == "POST"]) == (
        0 if require_confirmation else 1
    )
    again, _ = await dispatch(context, CardAction.model_validate(action_data))
    assert again == card
    assert len([request for request in requests if request.method == "POST"]) == (
        0 if require_confirmation else 1
    )


async def test_direct_submissions_are_claimed_once_and_changed_replay_is_rejected(
    snow, memory, principal, requests
):
    actions = Actions(snow, memory, require_confirmation=False)
    request = create_request()
    await asyncio.gather(
        actions.submit(principal, request, "f" * 32),
        actions.submit(principal, request, "f" * 32),
        return_exceptions=True,
    )
    assert len([request for request in requests if request.method == "POST"]) == 1
    with pytest.raises(DeskError, match="different data"):
        await actions.submit(
            principal, request.model_copy(update={"short_description": "Different"}), "f" * 32
        )


async def test_direct_comment_tool_replay_has_one_attributed_write(
    snow, memory, principal, requests
):
    actions = Actions(snow, memory, require_confirmation=False)
    context = ToolContext(principal, snow, memory, actions, Mock(), "call", request_id="activity-1")
    arguments = {
        "operation": "comment",
        "incident_number": "INC0012345",
        "comment": "The test still fails.",
    }
    for _ in range(2):
        tool = next(t for t in build_tools(context) if t.name == "prepare_incident_change")
        result = await tool.handler(ToolInvocation(arguments=arguments))
        assert result.result_type == "success"
        assert json.loads(result.text_result_for_llm)["status"] == "succeeded"
    writes = [request for request in requests if request.method == "PATCH"]
    assert len(writes) == 1
    assert json.loads(writes[0].content)["comments"].endswith("The test still fails.")
    assert "Submitted by" in json.loads(writes[0].content)["comments"]


@pytest.mark.parametrize("scope_token", ["", "b" * 64])
async def test_direct_mode_rejects_legacy_and_cross_conversation_forms(
    snow, memory, principal, requests, scope_token
):
    context = ToolContext(
        principal, snow, memory, Actions(snow, memory, require_confirmation=False), Mock(), "call"
    )
    with pytest.raises(DeskError):
        await dispatch(
            context,
            CardAction(
                verb="prepare_comment",
                action_id="f" * 32,
                scope_token=scope_token,
                incident="INC0012345",
                comment="Test",
            ),
        )
    assert not requests


async def test_known_incident_command_bypasses_intake_and_creation_policy(
    snow, memory, principal, requests
):
    snow.policy = snow.policy.model_copy(update={"allow_create": False})
    context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "call")
    card = await run_data_command(Command("incident", "inc0012345"), context)
    assert card["body"][1]["text"] == "INC0012345"
    assert len(requests) == 1
    assert requests[0].url.params["sysparm_query"].endswith(f"caller_id={principal.caller_id}")


async def test_incident_search_stays_owned_and_preserves_pagination(
    snow, memory, principal, requests, record
):
    context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "call")
    card = await run_data_command(Command("incidents", "printer"), context)
    assert "Search: printer" in str(card)
    assert requests[-1].url.params["sysparm_query"] == (
        f"caller_id={principal.caller_id}^short_descriptionLIKEprinter^ORDERBYDESCsys_updated_on"
    )
    items = [snow._incident(record, principal)] * 5
    next_page = cards.incidents(items, 0, query="printer")["actions"][-1]
    assert next_page["data"]["incident_query"] == "printer"
    await dispatch(context, CardAction.model_validate(next_page["data"]))
    assert requests[-1].url.params["sysparm_offset"] == "5"
    assert "^short_descriptionLIKEprinter^" in requests[-1].url.params["sysparm_query"]


@pytest.mark.parametrize("query", ["printer^ORcaller_idISNOTEMPTY", "x\n", "../incident/"])
async def test_incident_search_rejects_encoded_queries_before_http(
    snow, principal, requests, query
):
    with pytest.raises(DeskError):
        await snow.list_incidents(principal, query=query)
    assert not requests
