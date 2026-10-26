"""Identity is taken only from a platform-authenticated Teams Activity envelope."""

from __future__ import annotations

import re
from collections.abc import Mapping as MappingABC
from typing import Any
from uuid import UUID

from service_desk.config import Mapping
from service_desk.models import DeskError, Principal


class IdentityMapper:
    def __init__(self, tenant_id: str, identities: list[Mapping]) -> None:
        self.tenant_id = str(UUID(tenant_id))
        self._identities = {(item.tenant_id, item.object_id): item.caller_id for item in identities}

    def resolve(self, activity: MappingABC[str, Any]) -> Principal:
        principal = self.resolve_optional(activity)
        if principal is None:
            raise DeskError(
                "Your account is not mapped for this service. Contact support for access."
            )
        return principal

    def resolve_optional(self, activity: MappingABC[str, Any]) -> Principal | None:
        tenant, object_id, conversation_id = teams_identity(activity, self.tenant_id)
        caller = self._identities.get((tenant, object_id))
        if not caller:
            return None
        name = activity["from"].get("name")
        display_name = (
            " ".join(re.sub(r"<[^>]*>", "", name).split())[:80] if isinstance(name, str) else ""
        )
        return Principal(
            tenant_id=tenant,
            object_id=object_id,
            caller_id=caller,
            conversation_id=conversation_id,
            display_name=display_name,
        )


def teams_identity(activity: MappingABC[str, Any], tenant_id: str) -> tuple[str, str, str]:
    """Validate the authenticated envelope without requiring a mock ServiceNow caller."""
    if activity.get("channelId") != "msteams":
        raise DeskError("Please open Customer Service Desk in a Teams personal chat.")
    conversation = activity.get("conversation") or {}
    if not isinstance(conversation, dict) or conversation.get("conversationType") != "personal":
        raise DeskError("For your privacy, please contact me in a Teams personal chat.")
    try:
        tenant = str(UUID(activity["channelData"]["tenant"]["id"]))
        object_id = str(UUID(activity["from"]["aadObjectId"]))
        alternate = conversation.get("tenantId")
        if tenant != tenant_id or (alternate and str(UUID(alternate)) != tenant):
            raise ValueError("Tenant mismatch")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DeskError(
            "I could not verify your Teams identity. Contact service desk support."
        ) from exc
    conversation_id = conversation.get("id")
    if not isinstance(conversation_id, str) or not conversation_id or len(conversation_id) > 2048:
        raise DeskError("I could not verify this conversation.")
    return tenant, object_id, conversation_id
