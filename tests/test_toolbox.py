from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from mcp.types import (
    BlobResourceContents,
    ListResourcesResult,
    ReadResourceResult,
    Resource,
    TextResourceContents,
)
from pydantic import AnyUrl

import service_desk.toolbox as module
from service_desk.toolbox import ResourceSession, SkillCatalog, ToolboxError

ENDPOINT = "https://example.services.ai.azure.com/api/projects/demo/toolboxes/service-desk/mcp"
URI = "skill://printer-troubleshooting/SKILL.md"
BODY = "---\nname: printer-troubleshooting\ndescription: Printer help\n---\nAsk one question."


def resource(
    name: str = "printer-troubleshooting",
    uri: str = URI,
    **kwargs: Any,
) -> Resource:
    return Resource(
        name=name,
        uri=AnyUrl(uri),
        description=kwargs.pop("description", "Printer help"),
        mimeType=kwargs.pop("mimeType", "text/markdown"),
        **kwargs,
    )


def contents(text: str = BODY, uri: str = URI) -> ReadResourceResult:
    return ReadResourceResult(contents=[TextResourceContents(uri=AnyUrl(uri), text=text)])


class FakeSession:
    def __init__(self, pages: list[ListResourcesResult] | None = None) -> None:
        self.pages = pages or [ListResourcesResult(resources=[resource()])]
        self.cursors: list[str | None] = []
        self.reads: list[str] = []
        self.result = contents()
        self.error: Exception | None = None

    async def list_resources(self, cursor: str | None = None) -> ListResourcesResult:
        if self.error:
            raise self.error
        self.cursors.append(cursor)
        return self.pages[len(self.cursors) - 1]

    async def read_resource(self, uri: AnyUrl) -> ReadResourceResult:
        self.reads.append(str(uri))
        return self.result


class FakeCatalog(SkillCatalog):
    def __init__(self, session: FakeSession) -> None:
        super().__init__(ENDPOINT, cast(AsyncTokenCredential, object()))
        self.session = session
        self.calls: list[str] = []

    @asynccontextmanager
    async def _session(self, call_id: str) -> AsyncIterator[ResourceSession]:
        self.calls.append(call_id)
        try:
            yield self.session
        finally:
            self.session.cursors.clear()


@pytest.mark.asyncio
async def test_list_is_metadata_only_and_ignores_supporting_resources() -> None:
    session = FakeSession(
        [
            ListResourcesResult(
                resources=[
                    resource(),
                    resource("manual", "skill://printer-troubleshooting/references/manual.md"),
                    resource("unrelated", "https://unrelated.example/document.md"),
                    resource("index.json", "skill://index.json"),
                ]
            )
        ]
    )
    catalog = FakeCatalog(session)
    assert await catalog.list_skills("opaque / call") == [
        {"name": "printer-troubleshooting", "description": "Printer help", "uri": URI}
    ]
    assert session.reads == []
    assert catalog.calls == ["opaque / call"]


@pytest.mark.asyncio
async def test_load_exact_text_digest_immutable_and_cached() -> None:
    session = FakeSession()
    catalog = FakeCatalog(session)
    skill = await catalog.load_skill("printer-troubleshooting", "call")
    assert skill.content == BODY
    assert skill.uri == URI
    assert skill.digest == "sha256:" + hashlib.sha256(BODY.encode()).hexdigest()
    assert catalog._contents[skill.digest] == BODY
    with pytest.raises(FrozenInstanceError):
        skill.content = "changed"  # type: ignore[misc]
    assert session.reads == [URI]


@pytest.mark.asyncio
async def test_pagination_lists_all_pages_without_reading() -> None:
    session = FakeSession(
        [
            ListResourcesResult(resources=[resource()], nextCursor="opaque-page"),
            ListResourcesResult(
                resources=[resource("network-help", "skill://network-help/SKILL.md")]
            ),
        ]
    )
    # Direct discovery preserves the cursor observations for this assertion.
    entries = await FakeCatalog(session)._discover(session)
    assert list(entries) == ["printer-troubleshooting", "network-help"]
    assert session.cursors == [None, "opaque-page"]
    assert session.reads == []


@pytest.mark.asyncio
async def test_metadata_and_new_skills_refresh_without_redeploy() -> None:
    session = FakeSession()
    catalog = FakeCatalog(session)
    old = await catalog.load_skill("printer-troubleshooting", "first")
    session.pages = [
        ListResourcesResult(
            resources=[
                resource(description="Updated printer guide"),
                resource("new-skill", "skills://new-skill/SKILL.md"),
            ]
        )
    ]
    session.result = contents("Updated exact content")
    listed = await catalog.list_skills("second")
    new = await catalog.load_skill("printer-troubleshooting", "third")
    assert listed[1]["name"] == "new-skill"
    assert new.description == "Updated printer guide"
    assert new.content == "Updated exact content"
    assert new.digest != old.digest
    assert old.content == BODY
    assert catalog.calls == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_changed_uri_is_rediscovered_before_reading() -> None:
    session = FakeSession()
    catalog = FakeCatalog(session)
    await catalog.list_skills("first")
    uri = "skill://printer-troubleshooting/v2"
    session.pages = [
        ListResourcesResult(
            resources=[
                resource(
                    uri=uri,
                    _meta={"io.modelcontextprotocol.skills/name": "printer-troubleshooting"},
                )
            ]
        )
    ]
    session.result = contents(uri=uri)
    assert (await catalog.load_skill("printer-troubleshooting", "second")).uri == uri
    assert session.reads == [uri]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "uri"),
    [
        ("printer-troubleshooting/SKILL.md", URI),
        ("SKILL.md", URI),
        ("printer-troubleshooting", "skills://printer-troubleshooting/SKILL.md"),
        ("printer-troubleshooting", "skill://printer-troubleshooting"),
        ("printer-troubleshooting/SKILL.md", "skill://printer-troubleshooting"),
        ("printer-troubleshooting", "skill://acme/printer-troubleshooting/SKILL.md"),
    ],
)
async def test_supported_resource_conventions(name: str, uri: str) -> None:
    catalog = FakeCatalog(FakeSession([ListResourcesResult(resources=[resource(name, uri)])]))
    assert (await catalog.list_skills("call"))[0]["name"] == "printer-troubleshooting"


@pytest.mark.asyncio
async def test_explicit_namespaced_metadata() -> None:
    uri = "https://example.services.ai.azure.com/resources/approved"
    entry = resource(
        "opaque-resource",
        uri,
        _meta={
            "io.modelcontextprotocol.skills/name": "printer-troubleshooting",
            "io.modelcontextprotocol.skills/description": "Explicit description",
        },
    )
    session = FakeSession([ListResourcesResult(resources=[entry])])
    session.result = contents(uri=uri)
    skill = await FakeCatalog(session).load_skill("printer-troubleshooting", "call")
    assert skill.description == "Explicit description"
    assert session.reads == [uri]


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com/mcp",
        "file:///etc/passwd",
        "https://user:secret@example.com/mcp",
        "https://example.com/mcp#fragment",
        "https://example.com:bad/mcp",
        "https://example.com/\r\nx-header",
        "https:///missing",
        "https://example.com\\@elsewhere/mcp",
    ],
)
def test_invalid_endpoints(endpoint: str) -> None:
    with pytest.raises(ToolboxError, match="HTTPS"):
        SkillCatalog(endpoint, cast(AsyncTokenCredential, object()))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uri",
    [
        "https://attacker.example/printer-troubleshooting/SKILL.md",
        "http://example.services.ai.azure.com/printer-troubleshooting/SKILL.md",
        "file:///printer-troubleshooting/SKILL.md",
        "skill://user:password@printer-troubleshooting/SKILL.md",
        "skill://printer-troubleshooting/SKILL.md?target=secret",
        "skill://printer-troubleshooting/SKILL.md#fragment",
        "skill://printer-troubleshooting/%252e%252e/SKILL.md",
        "skill://printer-troubleshooting/a%5cb/SKILL.md",
    ],
)
async def test_unsafe_discovered_uri_is_never_read(uri: str) -> None:
    entry = resource(
        uri=uri, _meta={"io.modelcontextprotocol.skills/name": "printer-troubleshooting"}
    )
    session = FakeSession([ListResourcesResult(resources=[entry])])
    with pytest.raises(ToolboxError, match="unsafe"):
        await FakeCatalog(session).load_skill("printer-troubleshooting", "call")
    assert session.reads == []


def test_raw_traversal_uri_is_rejected() -> None:
    # Pydantic normalizes dot segments before the SDK exposes Resource.uri.
    with pytest.raises(ToolboxError, match="unsafe"):
        FakeCatalog(FakeSession())._validate_resource_uri(
            "skill://printer-troubleshooting/%2e%2e/SKILL.md"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["unknown", "../../secret", "https://attacker.example", "BadName"])
async def test_arbitrary_names_never_read(name: str) -> None:
    session = FakeSession()
    with pytest.raises(ToolboxError):
        await FakeCatalog(session).load_skill(name, "call")
    assert session.reads == []


@pytest.mark.asyncio
async def test_removed_skill_is_not_loaded_from_cache() -> None:
    session = FakeSession()
    catalog = FakeCatalog(session)
    await catalog.load_skill("printer-troubleshooting", "first")
    session.pages = [ListResourcesResult(resources=[])]
    with pytest.raises(ToolboxError, match="current"):
        await catalog.load_skill("printer-troubleshooting", "second")
    assert session.reads == [URI]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["list", "load"])
async def test_outage_error_is_sanitized(operation: str) -> None:
    session = FakeSession()
    session.error = RuntimeError("Authorization: Bearer very-secret https://user:password@example")
    catalog = FakeCatalog(session)
    with pytest.raises(ToolboxError) as error:
        if operation == "list":
            await catalog.list_skills("call")
        else:
            await catalog.load_skill("printer-troubleshooting", "call")
    assert "secret" not in str(error.value)
    assert "password" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.asyncio
async def test_repeated_cursor_and_page_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    repeated = ListResourcesResult(resources=[], nextCursor="again")
    session = FakeSession([repeated, repeated])
    with pytest.raises(ToolboxError, match="pagination"):
        await FakeCatalog(session).list_skills("call")
    monkeypatch.setattr(module, "MAX_PAGES", 1)
    with pytest.raises(ToolboxError, match="page count"):
        await FakeCatalog(FakeSession([repeated])).list_skills("call")


@pytest.mark.asyncio
async def test_resource_and_metadata_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "MAX_RESOURCES", 1)
    session = FakeSession([ListResourcesResult(resources=[resource(), resource()])])
    with pytest.raises(ToolboxError, match="catalog exceeds"):
        await FakeCatalog(session).list_skills("call")
    monkeypatch.setattr(module, "MAX_CATALOG_BYTES", 10)
    with pytest.raises(ToolboxError, match="metadata exceeds"):
        await FakeCatalog(FakeSession()).list_skills("call")


@pytest.mark.asyncio
async def test_duplicate_names_rejected() -> None:
    session = FakeSession([ListResourcesResult(resources=[resource(), resource()])])
    with pytest.raises(ToolboxError, match="ambiguous"):
        await FakeCatalog(session).list_skills("call")


@pytest.mark.asyncio
@pytest.mark.parametrize("description", ["", "x" * 1025])
async def test_invalid_skill_metadata(description: str) -> None:
    session = FakeSession([ListResourcesResult(resources=[resource(description=description)])])
    with pytest.raises(ToolboxError, match="metadata"):
        await FakeCatalog(session).list_skills("call")


@pytest.mark.asyncio
async def test_advertised_content_size_bound_prevents_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "MAX_SKILL_BYTES", 10)
    session = FakeSession([ListResourcesResult(resources=[resource(size=11)])])
    with pytest.raises(ToolboxError, match="content size"):
        await FakeCatalog(session).load_skill("printer-troubleshooting", "call")
    assert session.reads == []


@pytest.mark.asyncio
async def test_actual_utf8_content_size_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "MAX_SKILL_BYTES", 10)
    session = FakeSession()
    session.result = contents("é" * 6)
    with pytest.raises(ToolboxError, match="content size"):
        await FakeCatalog(session).load_skill("printer-troubleshooting", "call")


@pytest.mark.asyncio
async def test_foundry_json_content_wrapper() -> None:
    session = FakeSession([ListResourcesResult(resources=[resource(mimeType="application/json")])])
    session.result = contents(json.dumps({"content": BODY}))
    skill = await FakeCatalog(session).load_skill("printer-troubleshooting", "call")
    assert skill.content == BODY
    assert skill.digest == "sha256:" + hashlib.sha256(BODY.encode()).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        ReadResourceResult(contents=[]),
        ReadResourceResult(contents=[TextResourceContents(uri=AnyUrl(URI), text="")] * 2),
        contents("wrong URI", "skill://unapproved/SKILL.md"),
        contents("   "),
        contents("{broken"),
        contents('{"content":42}'),
        ReadResourceResult(contents=[BlobResourceContents(uri=AnyUrl(URI), blob="YmluYXJ5")]),
    ],
)
async def test_invalid_read_responses(result: ReadResourceResult) -> None:
    session = FakeSession()
    session.result = result
    with pytest.raises(ToolboxError):
        await FakeCatalog(session).load_skill("printer-troubleshooting", "call")


@pytest.mark.asyncio
@pytest.mark.parametrize("call_id", ["", "x\r\nAuthorization: bad", "é", "x" * 513])
async def test_invalid_call_ids_never_connect(call_id: str) -> None:
    catalog = FakeCatalog(FakeSession())
    with pytest.raises(ToolboxError, match="call ID"):
        await catalog.list_skills(call_id)
    assert catalog.calls == []


@pytest.mark.asyncio
async def test_real_session_helper_refreshes_token_and_preserves_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = AsyncMock()
    credential.get_token.side_effect = [AccessToken("token-1", 999), AccessToken("token-2", 999)]
    observed: list[dict[str, str]] = []
    initialized = AsyncMock()

    @asynccontextmanager
    async def transport(
        endpoint: str, *, headers: dict[str, str], **kwargs: Any
    ) -> AsyncIterator[tuple[None, None, None]]:
        assert endpoint == ENDPOINT
        observed.append(headers)
        yield None, None, None

    class Client:
        def __init__(self, *_: Any) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_: Any) -> None:
            pass

        async def initialize(self) -> None:
            await initialized()

        async def list_resources(self, cursor: str | None = None) -> ListResourcesResult:
            return ListResourcesResult(resources=[resource()])

    monkeypatch.setattr(module, "streamablehttp_client", transport)
    monkeypatch.setattr(module, "ClientSession", Client)
    catalog = SkillCatalog(ENDPOINT, credential)
    await catalog.list_skills("opaque-id / first")
    await catalog.list_skills("opaque-id / second")
    assert observed == [
        {
            "Authorization": "Bearer token-1",
            "x-agent-foundry-call-id": "opaque-id / first",
            "Foundry-Features": "Skills=V1Preview",
        },
        {
            "Authorization": "Bearer token-2",
            "x-agent-foundry-call-id": "opaque-id / second",
            "Foundry-Features": "Skills=V1Preview",
        },
    ]
    assert credential.get_token.await_count == 2
    credential.get_token.assert_called_with("https://ai.azure.com/.default")
    assert initialized.await_count == 2


@pytest.mark.asyncio
async def test_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "MAX_CACHE_ENTRIES", 2)
    monkeypatch.setattr(module, "MAX_CACHE_BYTES", 5)
    catalog = FakeCatalog(FakeSession())
    catalog._cache("one", "abc")
    catalog._cache("two", "de")
    catalog._cache("two", "de")
    catalog._cache("three", "fg")
    assert list(catalog._contents) == ["two", "three"]
    assert catalog._cache_bytes == 4


@pytest.mark.asyncio
async def test_timeout_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "OPERATION_TIMEOUT", 0)
    catalog = FakeCatalog(FakeSession())

    # Yield to the event loop to exercise asyncio's operation deadline.
    async def slow_list(cursor: str | None = None) -> ListResourcesResult:
        import asyncio

        await asyncio.sleep(1)
        return ListResourcesResult(resources=[])

    monkeypatch.setattr(catalog.session, "list_resources", slow_list)
    with pytest.raises(ToolboxError, match="unavailable"):
        await catalog.list_skills("call")
