from __future__ import annotations

import copy
import json
from typing import Any

import httpx
import pytest

from service_desk.config import Credentials, Mapping, SnowPolicy
from service_desk.models import DeskError, Principal
from service_desk.servicenow import SnowClient
from service_desk.state import Item

TENANT = "00000000-0000-0000-0000-000000000001"
USER = "00000000-0000-0000-0000-000000000002"
CALLER = "1" * 32
TICKET = "a" * 32


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-snow-integration",
        action="store_true",
        default=False,
        help="Run read-only live ServiceNow tests using .env and the credentials JSON file.",
    )
    parser.addoption(
        "--snow-credentials-file",
        default=".secrets",
        help="Local username/password JSON for opt-in ServiceNow tests (never uploaded).",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--run-snow-integration"):
        skip = pytest.mark.skip(reason="Use --run-snow-integration for read-only live API checks.")
        for item in items:
            if "snow_integration" in item.keywords:
                item.add_marker(skip)


class MemoryState:
    """Test double only: production never silently substitutes an in-memory store."""

    def __init__(self) -> None:
        self.data: dict[str, Item] = {}
        self.version = 0

    async def get(self, key: str) -> Item | None:
        return copy.deepcopy(self.data.get(key))

    async def create(self, key: str, value: dict[str, Any]) -> None:
        if key in self.data:
            raise DeskError("Already exists")
        self.version += 1
        self.data[key] = Item(copy.deepcopy(value), str(self.version))

    async def replace(self, key: str, value: dict[str, Any], etag: str) -> None:
        if key not in self.data or self.data[key].etag != etag:
            raise DeskError("Concurrent update")
        self.version += 1
        self.data[key] = Item(copy.deepcopy(value), str(self.version))


@pytest.fixture
def principal() -> Principal:
    return Principal(
        tenant_id=TENANT, object_id=USER, caller_id=CALLER, conversation_id="personal-1"
    )


@pytest.fixture
def mapping() -> list[Mapping]:
    return [Mapping(tenant_id=TENANT, object_id=USER, caller_id=CALLER)]


@pytest.fixture
def activity() -> dict[str, Any]:
    return {
        "type": "message",
        "channelId": "msteams",
        "conversation": {"id": "personal-1", "conversationType": "personal", "tenantId": TENANT},
        "from": {"id": "29:not-an-entra-id", "aadObjectId": USER},
        "channelData": {"tenant": {"id": TENANT}},
        "text": "My incidents",
    }


@pytest.fixture
def policy() -> SnowPolicy:
    return SnowPolicy(
        states={"1": "New", "2": "In progress", "6": "Resolved"},
        allow_create=True,
        allow_comments=True,
        resolve_from=["1", "2"],
        resolved_state="6",
        reopen_from=["6"],
        reopened_state="2",
        resolution_codes=["Solved"],
    )


@pytest.fixture
def record() -> dict[str, Any]:
    return {
        "sys_id": TICKET,
        "number": "INC0012345",
        "caller_id": CALLER,
        "short_description": "Printer unavailable",
        "description": "Floor 2 printer is offline",
        "state": {"value": "2", "display_value": "In progress"},
        "priority": {"value": "3", "display_value": "3 - Moderate"},
        "sys_updated_on": "2026-01-01 10:00:00",
        "sys_mod_count": "1",
        "comments": "Investigating",
    }


@pytest.fixture
def memory() -> MemoryState:
    return MemoryState()


@pytest.fixture
def requests() -> list[httpx.Request]:
    return []


@pytest.fixture
async def snow(policy: SnowPolicy, record: dict[str, Any], requests: list[httpx.Request]):
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            if request.url.path.endswith("/sys_choice"):
                return httpx.Response(
                    200,
                    json={
                        "result": [
                            {
                                "name": "incident",
                                "element": "category",
                                "value": "hardware",
                                "label": "Hardware",
                                "dependent_value": "",
                            },
                            {
                                "name": "incident",
                                "element": "category",
                                "value": "software",
                                "label": "Software",
                                "dependent_value": "",
                            },
                            {
                                "name": "incident",
                                "element": "subcategory",
                                "value": "email",
                                "label": "Email",
                                "dependent_value": "software",
                            },
                        ]
                    },
                )
            return httpx.Response(200, json={"result": [record]})
        payload = json.loads(request.content)
        result = {**record, **payload, "sys_mod_count": "2"}
        return httpx.Response(200, json={"result": result})

    client = SnowClient(
        "https://mock.service-now.com",
        Credentials(username="integration", password="test-not-a-real-password"),
        policy,
        transport=httpx.MockTransport(handle),
    )
    yield client
    await client.close()
