from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scripts.seed_printer_knowledge import ARTICLES, seed_drafts

BASE = "b" * 32


def test_local_article_stays_focused_on_local_observations():
    articles = dict(ARTICLES)
    filename = articles["Printer troubleshooting - local checks"]
    content = (Path(__file__).parents[1] / "knowledge" / filename).read_text()
    assert "<h1>Printer troubleshooting - local checks</h1>" in content
    assert "Remote checks" not in content
    assert "in-place" not in content
    assert "Do not open panels" in content
    assert "driver" not in content


@pytest.mark.parametrize("apply", [False, True])
async def test_setup_is_dry_run_by_default_and_creates_only_drafts(apply):
    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"result": []})
        payload = json.loads(request.content)
        assert payload["workflow_state"] == "draft"
        assert payload["kb_knowledge_base"] == BASE
        assert "<h1>" in payload["text"]
        assert "incident" not in request.url.path
        return httpx.Response(
            201, json={"result": {**payload, "sys_id": "a" * 32, "number": "KB0000001"}}
        )

    async with httpx.AsyncClient(
        base_url="https://mock.service-now.com/api/now/table/",
        transport=httpx.MockTransport(handle),
    ) as client:
        results = await seed_drafts(client, BASE, apply=apply)
    assert len(results) == 2
    assert sum(request.method == "POST" for request in requests) == (2 if apply else 0)


async def test_setup_reuses_existing_articles_without_modifying_or_publishing():
    requests = []

    def handle(request):
        requests.append(request)
        assert "^latest=true^" in request.url.params["sysparm_query"]
        title = request.url.params["sysparm_query"].split("^short_description=")[1]
        return httpx.Response(
            200,
            json={
                "result": [
                    {
                        "sys_id": "a" * 32,
                        "number": "KB0000001",
                        "short_description": title,
                        "kb_knowledge_base": BASE,
                        "workflow_state": "published",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="https://mock.service-now.com/api/now/table/",
        transport=httpx.MockTransport(handle),
    ) as client:
        results = await seed_drafts(client, BASE, apply=True)
    assert all(request.method == "GET" for request in requests)
    assert all(result["action"] == "unchanged" for result in results)
    assert [result["short_description"] for result in results] == [title for title, _ in ARTICLES]


async def test_setup_rejects_invalid_base_before_http():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected HTTP request"))
    ) as client:
        with pytest.raises(ValueError):
            await seed_drafts(client, BASE + "^ORactive=true", apply=True)
