from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from azure.ai.agentserver.core import FoundryAgentRequestContext
from azure.ai.agentserver.core._request_context import reset_request_context, set_request_context
from azure.ai.agentserver.core.storage import FoundryStateStore, FoundryStoragePreconditionError

from service_desk.models import DeskError
from service_desk.state import FoundryState


async def test_explicit_call_id_survives_callback_task_context():
    store = AsyncMock()
    store.get_item.return_value = SimpleNamespace(value={"ok": True}, etag="etag")
    state = FoundryState(store, "opaque-original-call")
    item = await state.get("key")
    assert item.value == {"ok": True}
    store.get_item.assert_awaited_once_with("key", call_id="opaque-original-call")
    await state.replace("key", {"new": True}, "etag")
    store.set_item.assert_awaited_once_with(
        "key", {"new": True}, if_match="etag", call_id="opaque-original-call"
    )


async def test_real_sdk_conditional_replace_preserves_etag_and_rejects_missing_items(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("FOUNDRY_HOSTING_ENVIRONMENT", raising=False)
    monkeypatch.setattr(
        "azure.ai.agentserver.core.storage._state.resolve_state_subdir", lambda _: tmp_path
    )
    store = await FoundryStateStore.get_or_create("synthetic-contract-test")
    state = FoundryState(store, "synthetic-call")
    try:
        await state.create("case", {"step": "first"})
        initial = await state.get("case")
        assert initial is not None
        await state.replace("case", {"step": "second"}, initial.etag)
        updated = await state.get("case")
        assert updated is not None
        assert updated.value == {"step": "second"}
        with pytest.raises(DeskError, match="changed"):
            await state.replace("case", {"step": "stale"}, initial.etag)
        with pytest.raises(DeskError, match="changed"):
            await state.replace("missing", {"step": "new"}, initial.etag)
        assert await state.get("missing") is None
    finally:
        await state.close()


async def test_conditional_conflicts_are_safe():
    store = AsyncMock()
    store.set_item.side_effect = FoundryStoragePreconditionError("RAW server internals")
    with pytest.raises(DeskError, match="changed") as caught:
        await FoundryState(store, "call").replace("key", {}, "stale")
    assert "RAW" not in str(caught.value)


async def test_missing_caller_context_does_not_fall_back_to_local_storage():
    with pytest.raises(DeskError, match="caller context"):
        await FoundryState.open(AsyncMock())


async def test_nonisolated_existing_store_is_rejected(monkeypatch):
    store = AsyncMock()
    store.get.return_value = SimpleNamespace(user_isolation=False)
    factory = AsyncMock(return_value=store)
    monkeypatch.setattr("service_desk.state.FoundryStateStore.get_or_create", factory)
    token = set_request_context(FoundryAgentRequestContext(call_id="verified-call"))
    try:
        with pytest.raises(DeskError, match="isolation"):
            await FoundryState.open(AsyncMock())
        store.aclose.assert_awaited_once()
        assert factory.call_args.kwargs["user_isolation"] is True
    finally:
        reset_request_context(token)
