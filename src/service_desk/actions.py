"""Reviewed writes: the model prepares drafts; authenticated card clicks commit them."""

from __future__ import annotations

import time
import uuid

from service_desk.models import DeskError, Draft, DraftRequest, Incident, Principal
from service_desk.servicenow import SnowClient
from service_desk.state import Item, State, require_current_conversation


class Actions:
    def __init__(self, snow: SnowClient, state: State, ttl: int = 600) -> None:
        self.snow = snow
        self.state = state
        self.ttl = ttl

    @staticmethod
    def key(principal: Principal, action_id: str) -> str:
        if len(action_id) != 32 or any(c not in "0123456789abcdef" for c in action_id):
            raise DeskError("This review is invalid.")
        return f"{principal.scope}:{action_id}"

    async def _proposal(
        self, principal: Principal, request: DraftRequest
    ) -> tuple[dict[str, str], Incident | None]:
        policy = self.snow.policy
        payload: dict[str, str]
        target = None
        if request.operation == "create":
            if not policy.allow_create:
                raise DeskError("Incident creation is not enabled for this ServiceNow instance.")
            if not request.short_description.strip() or not request.description.strip():
                raise DeskError("Please provide an issue title and description before review.")
            payload = {
                "caller_id": principal.caller_id,
                "short_description": request.short_description.strip(),
                "description": request.description.strip(),
            }
        else:
            if not request.incident_number:
                raise DeskError("Choose an incident before preparing a change.")
            target = await self.snow.get_incident(principal, request.incident_number)
            if not target.version:
                raise DeskError("ServiceNow record version is unavailable; changes are disabled.")
            if request.operation == "comment":
                if not policy.allow_comments or not request.comment.strip():
                    raise DeskError(
                        "A nonempty customer-visible comment is required and must be enabled."
                    )
                payload = {"comments": request.comment.strip()}
            elif request.operation == "resolve":
                if (
                    target.state not in policy.resolve_from
                    or not request.resolution_notes.strip()
                    or request.resolution_code not in policy.resolution_codes
                ):
                    raise DeskError(
                        "Resolution requires an allowed state, code, and resolution notes."
                    )
                payload = {
                    "state": policy.resolved_state,
                    "close_code": request.resolution_code,
                    "close_notes": request.resolution_notes.strip(),
                }
            else:
                if target.state not in policy.reopen_from:
                    raise DeskError("This incident cannot be reopened under the instance policy.")
                payload = {"state": policy.reopened_state}
        return payload, target

    async def prepare(self, principal: Principal, request: DraftRequest) -> Draft:
        await require_current_conversation(self.state, principal)
        payload, target = await self._proposal(principal, request)
        draft = Draft(
            id=uuid.uuid4().hex,
            scope=principal.scope,
            caller_id=principal.caller_id,
            request=request,
            target_id=target.sys_id if target else None,
            target_version=target.version if target else None,
            payload=payload,
            expires_at=time.time() + self.ttl,
        )
        await self.state.create(self.key(principal, draft.id), draft.model_dump(mode="json"))
        return draft

    async def load(self, principal: Principal, action_id: str) -> tuple[Draft, Item]:
        item = await self.state.get(self.key(principal, action_id))
        if not item:
            raise DeskError("This review is unavailable or has expired. Prepare a new review.")
        draft = Draft.model_validate(item.value)
        if draft.scope != principal.scope or draft.caller_id != principal.caller_id:
            raise DeskError("This review is unavailable in this conversation.")
        return draft, item

    async def cancel(self, principal: Principal, action_id: str) -> Draft:
        draft, item = await self.load(principal, action_id)
        if draft.status != "pending":
            raise DeskError("This review has already been handled.")
        cancelled = draft.model_copy(update={"status": "cancelled"})
        await self.state.replace(
            self.key(principal, action_id), cancelled.model_dump(mode="json"), item.etag
        )
        return cancelled

    async def confirm(self, principal: Principal, action_id: str) -> Draft:
        await require_current_conversation(self.state, principal)
        draft, item = await self.load(principal, action_id)
        if draft.status == "succeeded":
            return draft
        if draft.status != "pending":
            raise DeskError(
                "This review has already been handled. If the outcome is unknown, "
                "check the incident before preparing another change."
            )
        if draft.expires_at < time.time():
            expired = draft.model_copy(update={"status": "expired"})
            await self.state.replace(
                self.key(principal, action_id), expired.model_dump(mode="json"), item.etag
            )
            raise DeskError("This review expired. Prepare a new review.")
        payload, current = await self._proposal(principal, draft.request)
        if payload != draft.payload or (
            current
            and (current.sys_id != draft.target_id or current.version != draft.target_version)
        ):
            stale = draft.model_copy(update={"status": "stale"})
            await self.state.replace(
                self.key(principal, action_id), stale.model_dump(mode="json"), item.etag
            )
            raise DeskError("This incident changed. Refresh it and prepare a new review.")
        executing = draft.model_copy(update={"status": "executing"})
        key = self.key(principal, action_id)
        await self.state.replace(key, executing.model_dump(mode="json"), item.etag)
        claimed = await self.state.get(key)
        if not claimed:
            raise DeskError("The action receipt is unavailable; no write was attempted.")
        try:
            result = await self.snow.execute(principal, executing)
        except DeskError:
            unknown = executing.model_copy(update={"status": "outcome_unknown"})
            await self.state.replace(key, unknown.model_dump(mode="json"), claimed.etag)
            raise DeskError(
                "The write outcome could not be confirmed. Do not repeat it yet. "
                "Refresh the incident or ask support to reconcile the change."
            ) from None
        completed = executing.model_copy(update={"status": "succeeded", "result": result})
        await self.state.replace(key, completed.model_dump(mode="json"), claimed.etag)
        return completed
