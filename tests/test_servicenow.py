from __future__ import annotations

import httpx
import pytest

from service_desk.config import Credentials, SnowPolicy
from service_desk.models import DeskError
from service_desk.servicenow import SnowClient


async def test_reads_always_filter_caller_and_paginate(snow, principal, requests):
    result = await snow.list_incidents(principal, 2)
    assert result[0].state_label == "In progress"
    params = requests[0].url.params
    assert params["sysparm_query"].startswith("caller_id=" + principal.caller_id + "^")
    assert params["sysparm_limit"] == "5"
    assert params["sysparm_offset"] == "10"
    await snow.get_incident(principal, "INC0012345")
    assert requests[1].url.params["sysparm_query"].endswith("caller_id=" + principal.caller_id)


@pytest.mark.parametrize("number", ["INC1^ORcaller_idISNOTEMPTY", "../sys_user", "1", "INC1\n"])
async def test_query_injection_rejected_before_http(snow, principal, requests, number):
    with pytest.raises(DeskError):
        await snow.get_incident(principal, number)
    assert requests == []


async def test_response_ownership_rechecked(snow, principal, record):
    record["caller_id"] = "2" * 32
    with pytest.raises(DeskError, match="unavailable"):
        await snow.get_incident(principal, "INC0012345")


async def test_unknown_and_unauthorized_are_indistinguishable(policy, principal):
    async with httpx.AsyncClient():
        client = SnowClient(
            "https://mock.service-now.com",
            Credentials(username="x", password="y"),
            policy,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"result": []})),
        )
        try:
            with pytest.raises(DeskError, match="unavailable to your account"):
                await client.get_incident(principal, "INC999")
        finally:
            await client.close()


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
async def test_api_errors_do_not_expose_response_or_retry(policy, principal, status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            status, text="SECRET diagnostic", headers={"Location": "https://evil"}
        )

    client = SnowClient(
        "https://mock.service-now.com",
        Credentials(username="x", password="y"),
        policy,
        transport=httpx.MockTransport(handle),
    )
    try:
        with pytest.raises(DeskError) as caught:
            await client.list_incidents(principal)
        assert "SECRET" not in str(caught.value)
        assert len(calls) == 1
    finally:
        await client.close()


async def test_knowledge_disabled_without_verified_audience(snow, requests):
    with pytest.raises(DeskError, match="general guidance"):
        await snow.search_knowledge("printer")
    assert not requests


async def test_knowledge_allowlist_and_citations(principal):
    records = [
        {
            "sys_id": "b" * 32,
            "number": "KB123",
            "short_description": "Printing",
            "text": "Try a test page.",
        }
    ]
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"result": records})

    policy = SnowPolicy(employee_knowledge_bases=["c" * 32], knowledge_audience_verified=True)
    client = SnowClient(
        "https://mock.service-now.com",
        Credentials(username="x", password="y"),
        policy,
        transport=httpx.MockTransport(handle),
    )
    try:
        articles = await client.search_knowledge("printer")
        assert articles[0].url.startswith("https://mock.service-now.com/kb_view.do?")
        assert "kb_knowledge_baseIN" + "c" * 32 in calls[0].url.params["sysparm_query"]
        with pytest.raises(DeskError):
            await client.search_knowledge("printer^ORactive=true")
    finally:
        await client.close()


@pytest.mark.parametrize("allowed_article", ["b" * 32, "d" * 32])
async def test_article_allowlist_narrows_base_access_and_rechecks_results(allowed_article):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "result": [
                    {
                        "sys_id": "b" * 32,
                        "number": "KB123",
                        "short_description": "Printer troubleshooting - remote checks",
                        "text": "Verify the selected queue.",
                    }
                ]
            },
        )

    client = SnowClient(
        "https://mock.service-now.com",
        Credentials(username="x", password="y"),
        SnowPolicy(
            employee_knowledge_bases=["c" * 32],
            employee_knowledge_articles=[allowed_article],
            knowledge_audience_verified=True,
        ),
        transport=httpx.MockTransport(handle),
    )
    try:
        if allowed_article == "b" * 32:
            assert (
                len(await client.search_knowledge("Printer troubleshooting - remote checks")) == 1
            )
        else:
            with pytest.raises(DeskError, match="outside the approved"):
                await client.search_knowledge("Printer troubleshooting - remote checks")
        query = calls[0].url.params["sysparm_query"]
        assert "^sys_idIN" + allowed_article in query
        assert query.startswith("workflow_state=published^active=true^")
    finally:
        await client.close()
