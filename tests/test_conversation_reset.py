from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from service_desk.agent import DeskAgent
from service_desk.config import Settings
from service_desk.models import DeskError
from service_desk.state import conversation_principal, reset_conversation

from .test_config import settings_values


async def test_reset_pointer_is_durable_refreshed_and_scoped(memory, principal):
    assert (await conversation_principal(memory, principal)).scope == principal.scope
    await reset_conversation(memory, principal)
    current = await conversation_principal(memory, principal)
    stored = await memory.get(principal.identity_scope + ":conversation")
    again = await conversation_principal(memory, principal, refresh=True)
    assert again.scope == current.scope
    assert (await memory.get(principal.identity_scope + ":conversation")).etag != stored.etag
    for update in ({"object_id": "another-user"}, {"conversation_id": "another-chat"}):
        other = principal.model_copy(update=update)
        assert (await conversation_principal(memory, other)).scope == other.scope
    with pytest.raises(DeskError, match="already reset"):
        await reset_conversation(memory, principal)
    await reset_conversation(memory, current)
    assert (await conversation_principal(memory, principal)).scope != current.scope


async def test_sdk_reset_deletes_only_own_context_without_model_call(
    monkeypatch, tmp_path, principal
):
    monkeypatch.setattr("service_desk.agent.Path.home", lambda: tmp_path)
    owned = "desk-" + principal.scope[:40]
    client = Mock()
    client.start = AsyncMock()
    client.stop = AsyncMock()
    client.list_sessions = AsyncMock(
        return_value=[
            SimpleNamespace(session_id=owned),
            SimpleNamespace(session_id="someone-else"),
        ]
    )
    client.delete_session = AsyncMock()
    monkeypatch.setattr("service_desk.agent.CopilotClient", lambda **_: client)
    agent = DeskAgent(Settings(**settings_values()), Mock())
    try:
        await agent.reset(principal)
        client.delete_session.assert_awaited_once_with(owned)
        client.create_session.assert_not_called()
        client.resume_session.assert_not_called()
        client.list_sessions.return_value = []
        await agent.reset(principal)
        assert client.delete_session.await_count == 1
    finally:
        await agent.close()
