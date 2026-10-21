"""Administrative, opt-in draft creation. Never loaded or exposed to the agent."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values
from pydantic import TypeAdapter, ValidationError

from service_desk.config import Credentials, https_url
from service_desk.models import SysId

ROOT = Path(__file__).resolve().parents[1]
ARTICLES = (
    ("Printer troubleshooting - remote checks", "printer-remote.html"),
    ("Printer troubleshooting - local checks", "printer-local.html"),
)
FIELDS = "sys_id,number,short_description,workflow_state,kb_knowledge_base"


def article_record(record: Any, title: str, base_id: str) -> dict[str, str]:
    if (
        not isinstance(record, dict)
        or record.get("short_description") != title
        or record.get("kb_knowledge_base") != base_id
        or not all(isinstance(record.get(field), str) for field in FIELDS.split(","))
    ):
        raise ValueError("ServiceNow returned an unexpected article record.")
    TypeAdapter(SysId).validate_python(record["sys_id"])
    return {field: record[field] for field in FIELDS.split(",")}


async def seed_drafts(
    client: httpx.AsyncClient, base_id: str, *, apply: bool = False
) -> list[dict[str, str]]:
    TypeAdapter(SysId).validate_python(base_id)
    results = []
    for title, filename in ARTICLES:
        response = await client.get(
            "kb_knowledge",
            params={
                "sysparm_query": (
                    f"kb_knowledge_base={base_id}^latest=true^short_description={title}"
                ),
                "sysparm_fields": FIELDS,
                "sysparm_limit": "2",
                "sysparm_display_value": "false",
                "sysparm_exclude_reference_link": "true",
            },
        )
        response.raise_for_status()
        records = response.json()["result"]
        if not isinstance(records, list) or len(records) > 1:
            raise ValueError("Article lookup is ambiguous; review it in ServiceNow.")
        if records:
            results.append({**article_record(records[0], title, base_id), "action": "unchanged"})
            continue
        if not apply:
            results.append({"short_description": title, "action": "would_create_draft"})
            continue
        response = await client.post(
            "kb_knowledge",
            params={
                "sysparm_fields": FIELDS,
                "sysparm_display_value": "false",
                "sysparm_exclude_reference_link": "true",
            },
            json={
                "short_description": title,
                "text": (ROOT / "knowledge" / filename).read_text(),
                "kb_knowledge_base": base_id,
                "workflow_state": "draft",
                "active": "true",
                "valid_to": (datetime.now(UTC) + timedelta(days=365)).strftime("%Y-%m-%d"),
            },
        )
        response.raise_for_status()
        record = article_record(response.json()["result"], title, base_id)
        if record["workflow_state"] != "draft":
            raise ValueError("ServiceNow did not retain draft status; inspect the article.")
        results.append({**record, "action": "created_draft"})
    return results


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-base", required=True, help="Existing knowledge base sys_id.")
    parser.add_argument("--credentials-file", type=Path, default=Path(".secrets"))
    parser.add_argument(
        "--apply", action="store_true", help="Create missing drafts; never publish."
    )
    args = parser.parse_args()
    try:
        TypeAdapter(SysId).validate_python(args.knowledge_base)
        if args.credentials_file.stat().st_mode & 0o077:
            parser.error("Credentials file must have mode 0600.")
        raw = json.loads(args.credentials_file.read_text())
        credentials = Credentials.model_validate(raw.get("credentials", raw))
        url = https_url(dotenv_values(ROOT / ".env").get("DESK_SNOW_URL") or "")
        if urlsplit(url).path not in ("", "/") or urlsplit(url).query:
            parser.error("DESK_SNOW_URL must be an HTTPS instance origin.")
    except (OSError, ValueError, AttributeError, ValidationError):
        parser.error(
            "Configure a valid instance URL, base sys_id, and owner-only credentials file."
        )
    async with httpx.AsyncClient(
        base_url=url + "/api/now/table/",
        auth=httpx.BasicAuth(
            credentials.username.get_secret_value(), credentials.password.get_secret_value()
        ),
        timeout=30,
        follow_redirects=False,
    ) as client:
        try:
            results = await seed_drafts(client, args.knowledge_base, apply=args.apply)
        except httpx.HTTPStatusError as error:
            parser.error(f"ServiceNow returned HTTP {error.response.status_code}; inspect ACLs.")
        except (httpx.RequestError, ValueError, KeyError, TypeError):
            parser.error("Draft setup failed; inspect existing articles before retrying.")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
