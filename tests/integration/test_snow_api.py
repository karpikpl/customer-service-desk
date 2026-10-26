"""Explicit, read-only live API checks. Never create or mutate ServiceNow records."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from service_desk.config import Credentials, SnowPolicy, https_url
from service_desk.models import Principal
from service_desk.servicenow import SnowClient

pytestmark = pytest.mark.snow_integration


@dataclass(repr=False)
class LiveConfiguration:
    url: str
    credentials: Credentials


@pytest.fixture
def live_configuration(request: pytest.FixtureRequest) -> LiveConfiguration:
    values = dotenv_values(".env")
    path = Path(request.config.getoption("--snow-credentials-file"))
    try:
        url = https_url(values.get("DESK_SNOW_URL") or "")
        if path.stat().st_mode & 0o077:
            pytest.fail("Credentials file must have owner-only permissions: chmod 600 FILE.")
        raw = json.loads(path.read_text())
        credentials = Credentials.model_validate(raw.get("credentials", raw))
        if (
            not credentials.username.get_secret_value()
            or not credentials.password.get_secret_value()
        ):
            pytest.fail("ServiceNow username and password must be nonempty.")
    except (OSError, ValueError, ValidationError):
        pytest.fail(
            "Configure DESK_SNOW_URL in .env and a valid credentials JSON file.", pytrace=False
        )
    return LiveConfiguration(url, credentials)


@pytest.fixture
async def live_http(live_configuration: LiveConfiguration):
    async with httpx.AsyncClient(
        base_url=live_configuration.url.rstrip("/") + "/api/now/table/",
        auth=httpx.BasicAuth(
            live_configuration.credentials.username.get_secret_value(),
            live_configuration.credentials.password.get_secret_value(),
        ),
        timeout=httpx.Timeout(30, connect=10),
        follow_redirects=False,
    ) as client:
        yield client


async def read_records(
    client: httpx.AsyncClient, table: str, params: dict[str, str]
) -> list[dict[str, Any]]:
    try:
        response = await client.get(table, params=params)
    except httpx.HTTPError as exc:
        pytest.fail(
            f"ServiceNow read failed ({type(exc).__name__}); check VPN/DNS/TLS.",
            pytrace=False,
        )
    if response.status_code == 401:
        pytest.fail(
            "ServiceNow rejected HTTP Basic credentials (401). "
            "Verify the account password, API authentication policy, and account status.",
            pytrace=False,
        )
    if response.status_code == 403:
        pytest.fail(
            "ServiceNow denied table access (403); verify integration account ACLs.", pytrace=False
        )
    if response.status_code != 200:
        pytest.fail(f"ServiceNow read returned HTTP {response.status_code}.", pytrace=False)
    try:
        records = response.json()["result"]
    except (ValueError, KeyError, TypeError):
        pytest.fail("ServiceNow did not return a Table API result.", pytrace=False)
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        pytest.fail("ServiceNow result is not a record list.", pytrace=False)
    return records


async def test_basic_auth_and_incident_table_access(live_http):
    await read_records(
        live_http,
        "incident",
        {
            "sysparm_fields": "sys_id,number",
            "sysparm_limit": "1",
            "sysparm_exclude_reference_link": "true",
        },
    )


async def test_incident_choice_metadata_access(live_http):
    records = await read_records(
        live_http,
        "sys_choice",
        {
            "sysparm_query": "name=incident^elementINstate,close_code^inactive=false",
            "sysparm_fields": "element,value,label",
            "sysparm_limit": "100",
        },
    )
    if not any(item.get("element") == "state" for item in records):
        pytest.fail(
            "No incident state metadata is visible; "
            "confirm instance choices before enabling writes.",
            pytrace=False,
        )


async def test_application_client_filters_and_reads_owned_mock_incident(
    live_http, live_configuration
):
    records = await read_records(
        live_http,
        "incident",
        {
            "sysparm_query": "caller_idISNOTEMPTY",
            "sysparm_fields": "sys_id,number,caller_id",
            "sysparm_limit": "1",
            "sysparm_exclude_reference_link": "true",
            "sysparm_display_value": "false",
        },
    )
    if not records:
        pytest.fail(
            "Provide a mock incident with a caller to exercise application reads.", pytrace=False
        )
    record = records[0]
    try:
        principal = Principal(
            tenant_id="integration-test",
            object_id="synthetic-test-only",
            caller_id=record["caller_id"],
            conversation_id="read-only-integration-test",
        )
        number = record["number"]
    except (KeyError, ValidationError):
        pytest.fail("The mock incident lacks the expected caller/number fields.", pytrace=False)
    client = SnowClient(live_configuration.url, live_configuration.credentials, SnowPolicy())
    try:
        incidents = await client.list_incidents(principal)
        if any(item.caller_id != principal.caller_id for item in incidents):
            pytest.fail("An incident escaped the caller ownership filter.", pytrace=False)
        incident = await client.get_incident(principal, number)
        if incident.caller_id != principal.caller_id or incident.number != number:
            pytest.fail("The direct incident lookup returned a different record.", pytrace=False)
    finally:
        await client.close()


async def test_printer_knowledge_reads_published_demo_articles_without_allowlists(
    live_http, live_configuration
):
    records = await read_records(
        live_http,
        "kb_knowledge",
        {
            "sysparm_query": "short_descriptionLIKEPrinter troubleshooting - ",
            "sysparm_fields": (
                "sys_id,number,short_description,kb_knowledge_base,workflow_state,active"
            ),
            "sysparm_limit": "20",
            "sysparm_display_value": "false",
            "sysparm_exclude_reference_link": "true",
        },
    )
    titles = {
        "Printer troubleshooting - remote checks",
        "Printer troubleshooting - local checks",
    }
    demo = [record for record in records if record.get("short_description") in titles]
    if not demo:
        pytest.skip("Optional printer demo articles have not been seeded.")
    client = SnowClient(live_configuration.url, live_configuration.credentials, SnowPolicy())
    try:
        articles = await client.search_knowledge("Printer troubleshooting - ")
        expected = {
            record["number"]
            for record in demo
            if record["workflow_state"] == "published" and record["active"] == "true"
        }
        assert {article.number for article in articles} == expected
    finally:
        await client.close()
