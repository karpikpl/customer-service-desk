from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from service_desk.actions import Actions
from service_desk.config import Credentials
from service_desk.models import DeskError, DraftRequest
from service_desk.servicenow import SnowClient


async def test_create_requires_review_and_has_exact_caller(snow, memory, principal, requests):
    actions = Actions(snow, memory)
    draft = await actions.prepare(
        principal,
        DraftRequest(operation="create", short_description="Printer", description="Offline"),
    )
    assert not requests
    result = await actions.confirm(principal, draft.id)
    assert result.status == "succeeded"
    assert json.loads(requests[0].content) == {
        "caller_id": principal.caller_id,
        "short_description": "Printer",
        "description": "Offline",
        "contact_type": "virtual_agent",
    }
    again = await actions.confirm(principal, draft.id)
    assert again.status == "succeeded"
    assert len(requests) == 2
    assert requests[1].method == "GET"


@pytest.mark.parametrize("display_name", ["Adele Vance", ""])
async def test_comment_requires_review(snow, memory, principal, requests, display_name):
    from service_desk import cards

    principal = principal.model_copy(update={"display_name": display_name})
    actions = Actions(snow, memory)
    draft = await actions.prepare(
        principal,
        DraftRequest(operation="comment", incident_number="INC0012345", comment="Still stuck"),
    )
    author = (
        f"Adele Vance (Teams user: {principal.object_id})"
        if display_name
        else f"Teams user: {principal.object_id}"
    )
    expected = f"Submitted by {author} via Service Desk.\n\nStill stuck"
    assert draft.payload == {"comments": expected}
    assert draft.request.comment == "Still stuck"
    assert any(item.get("text") == cards.escaped(expected) for item in cards.review(draft)["body"])
    assert all(r.method == "GET" for r in requests)
    await actions.confirm(principal, draft.id)
    write = next(request for request in requests if request.method == "PATCH")
    assert json.loads(write.content) == {"comments": expected}
    assert "work_notes" not in write.content.decode()


async def test_changed_attribution_requires_new_review(snow, memory, principal, requests):
    actions = Actions(snow, memory)
    principal = principal.model_copy(update={"display_name": "Adele Vance"})
    draft = await actions.prepare(
        principal,
        DraftRequest(operation="comment", incident_number="INC0012345", comment="Still stuck"),
    )
    changed = principal.model_copy(update={"display_name": "Different Name"})
    with pytest.raises(DeskError, match="changed"):
        await actions.confirm(changed, draft.id)
    assert all(request.method == "GET" for request in requests)


async def test_attributed_comment_review_preserves_full_comment(snow, memory, principal):
    from service_desk import cards

    principal = principal.model_copy(update={"display_name": "A" * 80})
    comment = "!" * 4000
    draft = await Actions(snow, memory).prepare(
        principal,
        DraftRequest(operation="comment", incident_number="INC0012345", comment=comment),
    )
    expected = cards.escaped(draft.payload["comments"], limit=4500)
    assert expected.endswith(r"\!" * 4000)
    review = cards.review(draft)
    assert any(item.get("text") == expected for item in review["body"])
    assert len(json.dumps(review).encode()) < 24000


@pytest.mark.parametrize("operation", ["create", "comment"])
async def test_write_receipts_wait_longer_than_reads(snow, memory, principal, requests, operation):
    actions = Actions(snow, memory)
    request = (
        DraftRequest(operation="create", short_description="Printer", description="Offline")
        if operation == "create"
        else DraftRequest(operation="comment", incident_number="INC0012345", comment="Still stuck")
    )
    draft = await actions.prepare(principal, request)
    await actions.confirm(principal, draft.id)
    await snow.get_incident(principal, "INC0012345")
    for sent in requests:
        timeout = sent.extensions["timeout"]
        assert timeout["connect"] == 5
        assert timeout["read"] == (12 if sent.method == "GET" else 60)
    assert len([sent for sent in requests if sent.method != "GET"]) == 1
    write = next(sent for sent in requests if sent.method != "GET")
    assert write.url.params["sysparm_fields"] == "sys_id,number,caller_id"
    assert write.url.params["sysparm_display_value"] == "false"


async def test_cancel_and_edit_revoke_original(snow, memory, principal, requests):
    actions = Actions(snow, memory)
    draft = await actions.prepare(
        principal,
        DraftRequest(operation="create", short_description="Printer", description="Offline"),
    )
    await actions.cancel(principal, draft.id)
    with pytest.raises(DeskError, match="already"):
        await actions.confirm(principal, draft.id)
    assert not requests


async def test_cross_user_and_conversation_reviews_denied(snow, memory, principal):
    actions = Actions(snow, memory)
    draft = await actions.prepare(
        principal,
        DraftRequest(operation="create", short_description="Printer", description="Offline"),
    )
    for replacement in (
        {"object_id": "different"},
        {"conversation_id": "different"},
        {"caller_id": "2" * 32},
    ):
        other = principal.model_copy(update=replacement)
        with pytest.raises(DeskError):
            await actions.confirm(other, draft.id)


async def test_expired_review_is_never_sent(snow, memory, principal, requests):
    actions = Actions(snow, memory, ttl=-1)
    draft = await actions.prepare(
        principal,
        DraftRequest(operation="create", short_description="Printer", description="Offline"),
    )
    with pytest.raises(DeskError, match="expired"):
        await actions.confirm(principal, draft.id)
    assert not requests


async def test_changed_record_invalidates_review(snow, memory, principal, record, requests):
    actions = Actions(snow, memory)
    draft = await actions.prepare(
        principal, DraftRequest(operation="comment", incident_number="INC0012345", comment="Update")
    )
    record["sys_mod_count"] = "2"
    with pytest.raises(DeskError, match="changed"):
        await actions.confirm(principal, draft.id)
    assert all(r.method == "GET" for r in requests)


async def test_concurrent_confirmation_sends_at_most_one_write(snow, memory, principal, requests):
    actions = Actions(snow, memory)
    draft = await actions.prepare(
        principal,
        DraftRequest(operation="create", short_description="Printer", description="Offline"),
    )
    await asyncio.gather(
        actions.confirm(principal, draft.id),
        actions.confirm(principal, draft.id),
        return_exceptions=True,
    )
    assert len([r for r in requests if r.method == "POST"]) == 1


async def test_resolve_validates_code_and_reopen_validates_state(snow, memory, principal, record):
    actions = Actions(snow, memory)
    with pytest.raises(DeskError, match="Resolution"):
        await actions.prepare(
            principal,
            DraftRequest(
                operation="resolve",
                incident_number="INC0012345",
                resolution_code="Unknown",
                resolution_notes="Done",
            ),
        )
    with pytest.raises(DeskError, match="reopened"):
        await actions.prepare(
            principal, DraftRequest(operation="reopen", incident_number="INC0012345")
        )
    draft = await actions.prepare(
        principal,
        DraftRequest(
            operation="resolve",
            incident_number="INC0012345",
            resolution_code="Solved",
            resolution_notes="A test page printed successfully",
        ),
    )
    assert draft.payload["state"] == "6"
    record["state"] = {"value": "6", "display_value": "Resolved"}
    reopened = await actions.prepare(
        principal, DraftRequest(operation="reopen", incident_number="INC0012345")
    )
    assert reopened.payload == {"state": "2"}


@pytest.mark.parametrize("operation", ["create", "comment"])
async def test_unknown_write_outcome_never_retried(policy, memory, principal, record, operation):
    writes = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"result": [record]})
        writes.append(request)
        raise httpx.ReadTimeout("SECRET timeout", request=request)

    snow = SnowClient(
        "https://mock.service-now.com",
        Credentials(username="x", password="y"),
        policy,
        transport=httpx.MockTransport(handle),
    )
    try:
        actions = Actions(snow, memory)
        draft = await actions.prepare(
            principal,
            DraftRequest(operation="create", short_description="Printer", description="Offline")
            if operation == "create"
            else DraftRequest(
                operation="comment", incident_number="INC0012345", comment="Still stuck"
            ),
        )
        with pytest.raises(DeskError, match="outcome"):
            await actions.confirm(principal, draft.id)
        saved, _ = await actions.load(principal, draft.id)
        assert saved.status == "outcome_unknown"
        with pytest.raises(DeskError):
            await actions.confirm(principal, draft.id)
        assert len(writes) == 1
    finally:
        await snow.close()


@pytest.mark.parametrize(
    "receipt_case", ["ok", "wrong-caller", "wrong-id", "read-denied", "invalid-number"]
)
async def test_minimal_write_receipt_requires_owned_authoritative_read(
    policy, memory, principal, record, receipt_case
):
    sent = []

    def handle(request):
        sent.append(request)
        if request.method == "GET":
            if receipt_case == "read-denied":
                return httpx.Response(403, text="SECRET diagnostic")
            return httpx.Response(200, json={"result": [record]})
        receipt = {
            "sys_id": "b" * 32 if receipt_case == "wrong-id" else record["sys_id"],
            "number": "INVALID" if receipt_case == "invalid-number" else record["number"],
            "caller_id": "b" * 32 if receipt_case == "wrong-caller" else principal.caller_id,
        }
        return httpx.Response(201, json={"result": receipt})

    snow = SnowClient(
        "https://mock.service-now.com",
        Credentials(username="x", password="y"),
        policy,
        transport=httpx.MockTransport(handle),
    )
    try:
        actions = Actions(snow, memory)
        draft = await actions.prepare(
            principal,
            DraftRequest(operation="create", short_description="Printer", description="Offline"),
        )
        if receipt_case == "ok":
            completed = await actions.confirm(principal, draft.id)
            assert completed.status == "succeeded"
            assert completed.result is not None
            assert completed.result.state_label == "In progress"
            assert completed.result.comments == "Investigating"
            assert sent[-1].method == "GET"
            assert (
                sent[-1].url.params["sysparm_query"].endswith("^caller_id=" + principal.caller_id)
            )
        else:
            with pytest.raises(DeskError, match="outcome"):
                await actions.confirm(principal, draft.id)
            saved, _ = await actions.load(principal, draft.id)
            assert saved.status == "outcome_unknown"
            with pytest.raises(DeskError, match="already"):
                await actions.confirm(principal, draft.id)
        assert len([request for request in sent if request.method == "POST"]) == 1
    finally:
        await snow.close()


async def test_crashed_claim_is_not_replayed(snow, memory, principal, requests):
    actions = Actions(snow, memory)
    draft = await actions.prepare(
        principal,
        DraftRequest(operation="create", short_description="Printer", description="Offline"),
    )
    draft, item = await actions.load(principal, draft.id)
    await memory.replace(
        actions.key(principal, draft.id),
        draft.model_copy(update={"status": "executing"}).model_dump(mode="json"),
        item.etag,
    )
    restarted = Actions(snow, memory)
    with pytest.raises(DeskError, match="already"):
        await restarted.confirm(principal, draft.id)
    assert not requests


async def test_revoked_policy_blocks_existing_review(snow, memory, principal, requests):
    actions = Actions(snow, memory)
    draft = await actions.prepare(
        principal,
        DraftRequest(operation="create", short_description="Printer", description="Offline"),
    )
    snow.policy = snow.policy.model_copy(update={"allow_create": False})
    with pytest.raises(DeskError, match="not enabled"):
        await actions.confirm(principal, draft.id)
    assert not requests


def test_drafts_reject_arbitrary_identity_and_priority():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DraftRequest(operation="create", caller_id="another", priority="1")
