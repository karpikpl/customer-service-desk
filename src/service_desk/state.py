"""Foundry-provided application state with conditional updates and no external store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from azure.ai.agentserver.core import get_request_context
from azure.ai.agentserver.core.storage import (
    FoundryStateStore,
    FoundryStorageConflictError,
    FoundryStoragePreconditionError,
)
from azure.core.credentials_async import AsyncTokenCredential
from pydantic import Field

from service_desk.models import DeskError, Model, Principal


@dataclass(frozen=True)
class Item:
    value: dict[str, Any]
    etag: str


class State(Protocol):
    async def get(self, key: str) -> Item | None: ...
    async def create(self, key: str, value: dict[str, Any]) -> None: ...
    async def replace(self, key: str, value: dict[str, Any], etag: str) -> None: ...


class ConversationGeneration(Model):
    generation: str = Field(pattern=r"^[a-f0-9]{32}$")


async def conversation_principal(
    state: State, principal: Principal, *, refresh: bool = False
) -> Principal:
    key = principal.identity_scope + ":conversation"
    item = await state.get(key)
    if item is None:
        return principal.model_copy(update={"conversation_generation": ""})
    generation = ConversationGeneration.model_validate(item.value).generation
    if refresh:
        # Renew the reset pointer's TTL so active conversations cannot revert to legacy scope.
        await state.replace(key, item.value, item.etag)
    return principal.model_copy(update={"conversation_generation": generation})


async def reset_conversation(state: State, principal: Principal) -> None:
    key = principal.identity_scope + ":conversation"
    item = await state.get(key)
    current = ConversationGeneration.model_validate(item.value).generation if item else ""
    if current != principal.conversation_generation:
        raise DeskError("This conversation was already reset. Please send a new message.")
    value = ConversationGeneration(generation=uuid4().hex).model_dump()
    if item:
        await state.replace(key, value, item.etag)
    else:
        await state.create(key, value)


async def require_current_conversation(state: State, principal: Principal) -> None:
    current = await conversation_principal(state, principal)
    if current.scope != principal.scope:
        raise DeskError(
            "This conversation was reset. Prepare a new review in the current conversation."
        )


class FoundryState:
    def __init__(self, store: FoundryStateStore, call_id: str) -> None:
        self.store = store
        self.call_id = call_id

    @classmethod
    async def open(cls, credential: AsyncTokenCredential) -> FoundryState:
        call_id = get_request_context().call_id
        if not call_id:
            raise DeskError("Foundry caller context is required for durable service desk state.")
        store = await FoundryStateStore.get_or_create(
            "customer-service-desk-v1",
            credential,
            user_isolation=True,
            item_ttl_seconds=30 * 24 * 3600,
        )
        try:
            metadata = await store.get()
        except BaseException:
            await store.aclose()
            raise
        if not metadata.user_isolation:
            await store.aclose()
            raise DeskError("The Foundry state store must have user isolation enabled.")
        return cls(store, call_id)

    async def close(self) -> None:
        await self.store.aclose()

    def _call_id(self) -> str:
        call_id = self.call_id
        if not call_id:
            raise DeskError("The verified Foundry caller context is missing.")
        return str(call_id)

    async def get(self, key: str) -> Item | None:
        item = await self.store.get_item(key, call_id=self._call_id())
        return Item(dict(item.value), item.etag) if item else None

    async def create(self, key: str, value: dict[str, Any]) -> None:
        try:
            await self.store.create_item(key, value, call_id=self._call_id())
        except FoundryStorageConflictError as exc:
            raise DeskError("This action already exists. Refresh its review.") from exc

    async def replace(self, key: str, value: dict[str, Any], etag: str) -> None:
        try:
            await self.store.set_item(key, value, if_match=etag, call_id=self._call_id())
        except (FoundryStorageConflictError, FoundryStoragePreconditionError) as exc:
            raise DeskError("This action changed or is already being handled. Refresh it.") from exc
