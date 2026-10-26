"""Application-managed progressive disclosure of Foundry Toolbox MCP skills.

Discovery uses resources/list, not tools/list or the evolving skills/list SEP.
Foundry's preview and SEP-2640 resource conventions are documented at
https://learn.microsoft.com/azure/foundry/agents/how-to/tools/skills and
https://learn.microsoft.com/azure/foundry/how-to/develop/langchain-toolbox.
The host must pin returned immutable Skills for an active conversation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import unquote, urlsplit

from azure.core.credentials_async import AsyncTokenCredential
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import ListResourcesResult, ReadResourceResult, Resource, TextResourceContents
from pydantic import AnyUrl

from service_desk import telemetry

MAX_PAGES = 16
MAX_RESOURCES = 256
MAX_CATALOG_BYTES = 256 * 1024
MAX_SKILL_BYTES = 128 * 1024
MAX_CACHE_BYTES = 1024 * 1024
MAX_CACHE_ENTRIES = 32
OPERATION_TIMEOUT = 45
TOKEN_SCOPE = "https://ai.azure.com/.default"
_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_META_PREFIX = "io.modelcontextprotocol.skills/"


class ToolboxError(Exception):
    """Safe, user-facing discovery or loading failure."""


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    uri: str
    content: str
    digest: str


class ResourceSession(Protocol):
    """Small injectable boundary; no Azure connection is needed by fake sessions."""

    async def list_resources(self, cursor: str | None) -> ListResourcesResult: ...

    async def read_resource(self, uri: AnyUrl) -> ReadResourceResult: ...


@dataclass(frozen=True, slots=True)
class _Entry:
    name: str
    description: str
    uri: str
    size: int | None


def _valid_name(name: str) -> bool:
    return _NAME.fullmatch(name) is not None and "--" not in name


def _origin(uri: str) -> tuple[str, str, int]:
    parts = urlsplit(uri)
    return parts.scheme, parts.hostname or "", parts.port or 443


def _validate_endpoint(endpoint: str) -> None:
    try:
        parts = urlsplit(endpoint)
        valid = (
            parts.scheme == "https"
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and not parts.fragment
            and not any(ord(char) <= 32 or ord(char) >= 127 for char in endpoint)
            and "\\" not in endpoint
        )
        _origin(endpoint)
    except ValueError:
        valid = False
    if not valid:
        raise ToolboxError("Toolbox endpoint must be an HTTPS URL without credentials.")


def _validate_call_id(call_id: str) -> None:
    if not 1 <= len(call_id) <= 512 or any(not 32 <= ord(c) <= 126 for c in call_id):
        raise ToolboxError("A valid opaque Foundry call ID is required.")


class SkillCatalog:
    """Refresh metadata every operation and read only a discovered skill URI.

    The supplied credential remains owned by the caller. Resources are never
    opened as filesystem paths or fetched as independent URLs. Only skill(s)
    URIs or HTTPS resource identifiers on the configured endpoint origin are
    allowed, and even those are passed exclusively to MCP resources/read.
    """

    def __init__(self, endpoint: str, credential: AsyncTokenCredential) -> None:
        _validate_endpoint(endpoint)
        self._endpoint = endpoint
        self._credential = credential
        self._contents: OrderedDict[str, str] = OrderedDict()
        self._cache_bytes = 0

    @asynccontextmanager
    async def _session(self, call_id: str) -> AsyncIterator[ResourceSession]:
        """Override this context manager for tests or alternate resource transports."""
        token = await self._credential.get_token(TOKEN_SCOPE)
        headers = {
            "Authorization": f"Bearer {token.token}",
            "x-agent-foundry-call-id": call_id,
            "Foundry-Features": "Skills=V1Preview",
        }
        async with streamablehttp_client(
            self._endpoint, headers=headers, timeout=15, sse_read_timeout=30
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    @telemetry.traced("toolbox.discover")
    async def list_skills(self, call_id: str) -> list[dict[str, str]]:
        """Return bounded name/description/URI metadata, never skill contents."""
        _validate_call_id(call_id)
        try:
            async with asyncio.timeout(OPERATION_TIMEOUT), self._session(call_id) as session:
                entries = await self._discover(session)
            return [
                {"name": entry.name, "description": entry.description, "uri": entry.uri}
                for entry in entries.values()
            ]
        except ToolboxError:
            raise
        except Exception:
            raise ToolboxError(
                "Toolbox skill discovery is unavailable. Please try again."
            ) from None

    @telemetry.traced("toolbox.load")
    async def load_skill(self, name: str, call_id: str) -> Skill:
        """Return exact current text and its SHA-256 digest; never trust a caller URI."""
        _validate_call_id(call_id)
        if not _valid_name(name):
            raise ToolboxError("Choose a valid skill name from the skill catalog.")
        try:
            async with asyncio.timeout(OPERATION_TIMEOUT), self._session(call_id) as session:
                entries = await self._discover(session)
                entry = entries.get(name)
                if entry is None:
                    raise ToolboxError("The requested skill is not in the current Toolbox catalog.")
                if entry.size is not None and not 0 <= entry.size <= MAX_SKILL_BYTES:
                    raise ToolboxError("The requested skill exceeds the allowed content size.")
                result = await session.read_resource(AnyUrl(entry.uri))
                content = self._read_content(result, entry.uri)
            digest = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
            content = self._cache(digest, content)
            return Skill(entry.name, entry.description, entry.uri, content, digest)
        except ToolboxError:
            raise
        except Exception:
            raise ToolboxError("The Toolbox skill could not be loaded. Please try again.") from None

    async def _discover(self, session: ResourceSession) -> dict[str, _Entry]:
        entries: dict[str, _Entry] = {}
        uris: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None
        count = total_bytes = 0
        for _ in range(MAX_PAGES):
            page = await session.list_resources(cursor=cursor)
            count += len(page.resources)
            if count > MAX_RESOURCES:
                raise ToolboxError("The Toolbox resource catalog exceeds its allowed size.")
            for resource in page.resources:
                total_bytes += len(resource.model_dump_json().encode("utf-8"))
                if total_bytes > MAX_CATALOG_BYTES:
                    raise ToolboxError("The Toolbox resource metadata exceeds its allowed size.")
                entry = self._entry(resource)
                if entry is None:
                    continue
                if entry.name in entries or entry.uri in uris:
                    raise ToolboxError("The Toolbox skill catalog contains ambiguous entries.")
                entries[entry.name] = entry
                uris.add(entry.uri)
            cursor = page.nextCursor
            if cursor is None:
                return entries
            if not cursor or len(cursor) > 4096 or cursor in seen_cursors:
                raise ToolboxError("The Toolbox catalog returned invalid pagination.")
            seen_cursors.add(cursor)
        raise ToolboxError("The Toolbox catalog exceeds its allowed page count.")

    def _entry(self, resource: Resource) -> _Entry | None:
        uri = str(resource.uri)
        parts = urlsplit(uri)
        segments = [unquote(s) for s in (parts.netloc + parts.path).split("/") if s]
        meta = resource.meta or {}
        # SEP metadata is namespaced. Support explicit name/description metadata
        # as well as Foundry's skill://name[/SKILL.md] resource convention.
        explicit_name = meta.get(_META_PREFIX + "name")
        explicit_description = meta.get(_META_PREFIX + "description")
        conventional = (
            len(segments) >= 2
            and segments[-1] == "SKILL.md"
            and resource.name in {segments[-2], f"{segments[-2]}/SKILL.md", "SKILL.md"}
        )
        bare_skill = (
            parts.scheme in {"skill", "skills"}
            and len(segments) == 1
            and resource.name in {segments[0], f"{segments[0]}/SKILL.md"}
            and _valid_name(segments[0])
        )
        if isinstance(explicit_name, str):
            name = explicit_name
        elif conventional:
            name = segments[-2]
        elif bare_skill:
            name = segments[0]
        else:
            return None
        description = (
            explicit_description
            if isinstance(explicit_description, str)
            else resource.description or ""
        )
        if (
            not _valid_name(name)
            or not description.strip()
            or len(description) > 1024
            or resource.mimeType not in {None, "text/markdown", "text/plain", "application/json"}
        ):
            raise ToolboxError("The Toolbox returned invalid skill metadata.")
        self._validate_resource_uri(uri)
        return _Entry(name, description, uri, resource.size)

    def _validate_resource_uri(self, uri: str) -> None:
        parts = urlsplit(uri)
        decoded = unquote(uri)
        valid = (
            len(uri) <= 4096
            and parts.scheme in {"skill", "skills", "https"}
            and bool(parts.netloc)
            and parts.username is None
            and parts.password is None
            and not parts.query
            and not parts.fragment
            and "\\" not in decoded
            and not any(ord(c) <= 32 or ord(c) == 127 for c in decoded)
            and not any(s in {".", ".."} for s in decoded.split("/"))
            and not re.search(r"%[0-9a-fA-F]{2}", decoded)
        )
        if parts.scheme == "https":
            valid = valid and _origin(uri) == _origin(self._endpoint)
        if not valid:
            raise ToolboxError("The Toolbox returned an unsafe skill resource identifier.")

    @staticmethod
    def _read_content(result: ReadResourceResult, uri: str) -> str:
        if len(result.contents) != 1:
            raise ToolboxError("The Toolbox skill must contain exactly one text resource.")
        item = result.contents[0]
        if (
            not isinstance(item, TextResourceContents)
            or str(item.uri) != uri
            or item.mimeType not in {None, "text/markdown", "text/plain", "application/json"}
        ):
            raise ToolboxError("The Toolbox returned unexpected skill content.")
        content = item.text
        if len(content.encode("utf-8")) > MAX_SKILL_BYTES:
            raise ToolboxError("The requested skill exceeds the allowed content size.")
        # Foundry can wrap SKILL.md text in {"content": "..."}; never execute it.
        if content.lstrip().startswith("{"):
            try:
                wrapper = json.loads(content)
            except ValueError:
                raise ToolboxError("The Toolbox returned invalid skill content.") from None
            if not isinstance(wrapper, dict) or not isinstance(wrapper.get("content"), str):
                raise ToolboxError("The Toolbox returned invalid skill content.")
            content = wrapper["content"]
        if not content.strip():
            raise ToolboxError("The Toolbox returned empty skill content.")
        return content

    def _cache(self, digest: str, content: str) -> str:
        if digest in self._contents:
            self._contents.move_to_end(digest)
            return self._contents[digest]
        self._contents[digest] = content
        self._cache_bytes += len(content.encode("utf-8"))
        while len(self._contents) > MAX_CACHE_ENTRIES or self._cache_bytes > MAX_CACHE_BYTES:
            _, evicted = self._contents.popitem(last=False)
            self._cache_bytes -= len(evicted.encode("utf-8"))
        return content
