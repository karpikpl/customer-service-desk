"""Narrow ServiceNow Table API boundary, always constrained to the mapped caller."""

from __future__ import annotations

import re
import time
from typing import Any

import httpx
from opentelemetry import trace
from pydantic import TypeAdapter, ValidationError

from service_desk import telemetry
from service_desk.config import Credentials, SnowPolicy
from service_desk.models import Article, DeskError, Draft, Incident, IncidentNumber, Principal

_FIELDS = (
    "sys_id,number,caller_id,short_description,description,state,priority,"
    "sys_updated_on,sys_mod_count,comments"
)


def _value(record: dict[str, Any], key: str, *, display: bool = False) -> str:
    value = record.get(key, "")
    if isinstance(value, dict):
        value = value.get("display_value" if display else "value", "")
    if not isinstance(value, str):
        raise DeskError("ServiceNow returned an invalid field. Contact service desk support.")
    return value


class SnowClient:
    def __init__(
        self,
        url: str,
        credentials: Credentials,
        policy: SnowPolicy,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.policy = policy
        self._classifications: tuple[float, dict[str, str]] | None = None
        self.http = httpx.AsyncClient(
            base_url=url + "/api/now/table/",
            auth=httpx.BasicAuth(
                credentials.username.get_secret_value(), credentials.password.get_secret_value()
            ),
            timeout=httpx.Timeout(12, connect=5),
            follow_redirects=False,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    async def close(self) -> None:
        await self.http.aclose()

    def _incident(self, record: dict[str, Any], principal: Principal) -> Incident:
        if _value(record, "caller_id") != principal.caller_id:
            raise DeskError("That incident is unavailable to your account.")
        state = _value(record, "state")
        try:
            return Incident(
                sys_id=_value(record, "sys_id"),
                number=_value(record, "number"),
                caller_id=_value(record, "caller_id"),
                short_description=_value(record, "short_description"),
                description=_value(record, "description"),
                state=state,
                state_label=self.policy.states.get(state, _value(record, "state", display=True)),
                priority=_value(record, "priority", display=True),
                updated=_value(record, "sys_updated_on"),
                version=_value(record, "sys_mod_count"),
                comments=_value(record, "comments"),
            )
        except ValidationError as exc:
            raise DeskError("ServiceNow returned an invalid incident record.") from exc

    @telemetry.traced("servicenow.request")
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str],
        payload: dict[str, str] | None = None,
    ) -> Any:
        span = trace.get_current_span()
        span.set_attribute("http.request.method", method)
        resource = (
            "knowledge"
            if path == "kb_knowledge"
            else "incident_choices"
            if path == "sys_choice"
            else "incident"
        )
        span.set_attribute("desk.resource", resource)
        try:
            response = await self.http.request(
                method,
                path,
                params=params,
                json=payload,
                timeout=httpx.Timeout(60, connect=5)
                if method in ("POST", "PATCH")
                else self.http.timeout,
            )
        except httpx.HTTPError as exc:
            raise DeskError("ServiceNow could not be reached. Please try a safe refresh.") from exc
        span.set_attribute("http.response.status_code", response.status_code)
        if not response.is_success:
            if response.status_code in (401, 403):
                raise DeskError("ServiceNow denied this operation. Contact service desk support.")
            if response.status_code == 429:
                raise DeskError("ServiceNow is busy. Please wait before refreshing.")
            raise DeskError("ServiceNow could not complete this operation.")
        try:
            data = response.json()
            return data["result"]
        except (ValueError, KeyError, TypeError) as exc:
            raise DeskError("ServiceNow returned an invalid response.") from exc

    async def list_incidents(
        self, principal: Principal, page: int = 0, *, query: str = ""
    ) -> list[Incident]:
        if page < 0 or page > 1000:
            raise DeskError("This incident page is invalid.")
        if query and not re.fullmatch(r"[\w .-]{2,120}", query):
            raise DeskError("Use a short incident search without query operators.")
        search = f"^short_descriptionLIKE{query}" if query else ""
        result = await self._request(
            "GET",
            "incident",
            params={
                "sysparm_query": (
                    f"caller_id={principal.caller_id}{search}^ORDERBYDESCsys_updated_on"
                ),
                "sysparm_fields": _FIELDS,
                "sysparm_limit": "5",
                "sysparm_offset": str(page * 5),
                "sysparm_display_value": "all",
                "sysparm_exclude_reference_link": "true",
            },
        )
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise DeskError("ServiceNow returned an invalid incident list.")
        return [self._incident(item, principal) for item in result]

    async def get_incident(self, principal: Principal, number: str) -> Incident:
        try:
            TypeAdapter(IncidentNumber).validate_python(number)
        except ValidationError as exc:
            raise DeskError("Please provide an incident number such as INC0012345.") from exc
        result = await self._request(
            "GET",
            "incident",
            params={
                "sysparm_query": f"number={number}^caller_id={principal.caller_id}",
                "sysparm_fields": _FIELDS,
                "sysparm_limit": "1",
                "sysparm_display_value": "all",
                "sysparm_exclude_reference_link": "true",
            },
        )
        if not isinstance(result, list) or not result or not isinstance(result[0], dict):
            raise DeskError("That incident is unavailable to your account.")
        return self._incident(result[0], principal)

    async def search_knowledge(self, query: str) -> list[Article]:
        if not re.fullmatch(r"[\w .-]{2,120}", query):
            raise DeskError("Use a short knowledge search without query operators.")
        result = await self._request(
            "GET",
            "kb_knowledge",
            params={
                "sysparm_query": (
                    f"workflow_state=published^active=true^short_descriptionLIKE{query}"
                ),
                "sysparm_fields": "sys_id,number,short_description,text",
                "sysparm_limit": "3",
                "sysparm_display_value": "all",
            },
        )
        if not isinstance(result, list):
            raise DeskError("ServiceNow returned an invalid knowledge response.")
        articles = []
        for record in result:
            if not isinstance(record, dict):
                raise DeskError("ServiceNow returned an invalid knowledge record.")
            sys_id = _value(record, "sys_id")
            if not re.fullmatch(r"[a-f0-9]{32}", sys_id):
                raise DeskError("ServiceNow returned an invalid knowledge identifier.")
            articles.append(
                Article(
                    number=_value(record, "number"),
                    title=_value(record, "short_description"),
                    text=_value(record, "text")[:12000],
                    url=f"{self.url}/kb_view.do?sys_kb_id={sys_id}",
                )
            )
        return articles

    async def incident_classifications(self) -> dict[str, str]:
        if self._classifications and self._classifications[0] > time.monotonic():
            return dict(self._classifications[1])
        result = await self._request(
            "GET",
            "sys_choice",
            params={
                "sysparm_query": (
                    "nameINincident,task^elementINcategory,subcategory"
                    "^inactive=false^language=en^ORDERBYsequence^ORDERBYlabel"
                ),
                "sysparm_fields": "name,element,value,label,dependent_value",
                "sysparm_limit": "200",
                "sysparm_display_value": "false",
            },
        )
        if not isinstance(result, list) or len(result) >= 200:
            raise DeskError("Incident choices are unavailable or too large. Contact support.")
        if any(not isinstance(item, dict) for item in result):
            raise DeskError("ServiceNow returned invalid incident choices.")
        categories: dict[str, str] = {}
        for table in ("task", "incident"):
            for item in result:
                if _value(item, "name") != table or _value(item, "element") != "category":
                    continue
                value, label = _value(item, "value"), _value(item, "label")
                if not re.fullmatch(r"[\w .-]{1,40}", value) or not label.strip():
                    raise DeskError("ServiceNow returned an invalid incident category.")
                categories[value] = label[:120]
        if not categories:
            raise DeskError("Incident categories could not be loaded. Contact support.")
        choices = dict(categories)
        for item in result:
            if _value(item, "element") != "subcategory":
                continue
            parent, value = _value(item, "dependent_value"), _value(item, "value")
            label = _value(item, "label")
            if parent not in categories:
                continue
            if not re.fullmatch(r"[\w .-]{1,40}", value) or not label.strip():
                raise DeskError("ServiceNow returned an invalid incident subcategory.")
            choices[f"{parent}:{value}"] = f"{categories[parent]} / {label[:120]}"
        self._classifications = (time.monotonic() + 300, choices)
        return dict(choices)

    async def execute(self, principal: Principal, draft: Draft) -> Incident:
        """Only the confirmed-action service calls this method; never registered as a model tool."""
        if draft.scope != principal.scope or draft.caller_id != principal.caller_id:
            raise DeskError("This review is unavailable in this conversation.")
        method = "POST" if draft.request.operation == "create" else "PATCH"
        path = "incident" if method == "POST" else f"incident/{draft.target_id}"
        result = await self._request(
            method,
            path,
            params={
                "sysparm_fields": "sys_id,number,caller_id",
                "sysparm_display_value": "false",
                "sysparm_exclude_reference_link": "true",
            },
            payload=draft.payload,
        )
        if not isinstance(result, dict):
            raise DeskError("ServiceNow returned an invalid write receipt.")
        if _value(result, "caller_id") != principal.caller_id:
            raise DeskError("That incident is unavailable to your account.")
        # Expand display values and journal fields on a read, not inside the write transaction.
        incident = await self.get_incident(principal, _value(result, "number"))
        if incident.sys_id != _value(result, "sys_id"):
            raise DeskError("ServiceNow returned an inconsistent write receipt.")
        return incident
