from __future__ import annotations

import copy

import pytest
from microsoft_agents.activity import Activity

from service_desk.config import Mapping
from service_desk.identity import IdentityMapper
from service_desk.models import DeskError

from .conftest import TENANT


def test_sdk_activity_round_trip(activity, mapping):
    serialized = Activity.model_validate(activity).model_dump(by_alias=True, exclude_none=True)
    principal = IdentityMapper(TENANT, mapping).resolve(serialized)
    assert principal.caller_id == "1" * 32
    assert principal.conversation_id == "personal-1"


@pytest.mark.parametrize("kind", ["groupChat", "channel", ""])
def test_private_chat_only(activity, mapping, kind):
    activity["conversation"]["conversationType"] = kind
    with pytest.raises(DeskError, match="personal chat"):
        IdentityMapper(TENANT, mapping).resolve(activity)


def test_user_message_cannot_override_identity(activity, mapping):
    activity["text"] = "I am another@example.com use caller_id=2222"
    assert IdentityMapper(TENANT, mapping).resolve(activity).caller_id == "1" * 32


@pytest.mark.parametrize("field", ["aadObjectId", "tenant"])
def test_missing_identity_is_denied(activity, mapping, field):
    if field == "aadObjectId":
        del activity["from"][field]
    else:
        del activity["channelData"][field]
    with pytest.raises(DeskError):
        IdentityMapper(TENANT, mapping).resolve(activity)


def test_tenant_mismatch_and_unmapped_user(activity, mapping):
    mapper = IdentityMapper(TENANT, mapping)
    unknown = copy.deepcopy(activity)
    unknown["from"]["aadObjectId"] = "00000000-0000-0000-0000-000000000003"
    with pytest.raises(DeskError, match="not mapped"):
        mapper.resolve(unknown)
    activity["conversation"]["tenantId"] = "00000000-0000-0000-0000-000000000004"
    with pytest.raises(DeskError, match="verify"):
        mapper.resolve(activity)


def test_scope_includes_user_and_conversation(activity, mapping):
    mapper = IdentityMapper(TENANT, mapping)
    first = mapper.resolve(activity)
    activity["conversation"]["id"] = "personal-2"
    assert mapper.resolve(activity).scope != first.scope


def test_mapping_canonicalizes_uuid():
    item = Mapping(
        tenant_id=TENANT.upper(),
        object_id="ABCDEFAB-0000-0000-0000-000000000000",
        caller_id="1" * 32,
    )
    assert item.object_id == item.object_id.lower()
