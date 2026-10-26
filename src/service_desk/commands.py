"""Explicit chat commands: parsing and rendering never invoke the model."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from importlib.metadata import version

from service_desk import cards
from service_desk.config import Settings
from service_desk.models import DeskError, DraftRequest
from service_desk.tools import ToolContext

COMMANDS = {
    "feedback": "Provide feedback (optionally add text after the command).",
    "incident": "Open the issue form, or view an owned incident: /incident INC0012345.",
    "kb": "Search published knowledge: /kb printer, or open the search form.",
    "agent": "Show agent, model, version, and skill details.",
    "new": "Start fresh; forget model context and invalidate old cases/reviews.",
    "incidents": "List your incidents, or search: /incidents printer; optional page number.",
    "whoami": "Show your Teams identity and temporary ServiceNow mapping.",
    "help": "Show these commands.",
}


@dataclass(frozen=True)
class Command:
    name: str
    argument: str = ""


def parse_command(text: str) -> Command | None:
    text = text.strip()
    if not text.startswith("/"):
        return None
    match = re.fullmatch(r"/([a-z]+)(?:\s+(.*))?", text, re.I | re.S)
    if not match or match[1].lower() not in COMMANDS:
        raise DeskError("Unknown command. Use /help to see the supported commands.")
    command = Command(match[1].lower(), (match[2] or "").strip())
    if command.name in ("agent", "new", "whoami", "help") and command.argument:
        raise DeskError(f"Usage: /{command.name} (no arguments).")
    limit = {"feedback": 4000, "incident": 160, "kb": 120, "incidents": 120}.get(command.name)
    if limit and len(command.argument) > limit:
        raise DeskError(f"/{command.name} accepts at most {limit} characters.")
    if command.name == "incidents" and command.argument:
        if re.fullmatch(r"-?[0-9]+", command.argument):
            if not 1 <= int(command.argument) <= 1001:
                raise DeskError("Usage: /incidents PAGE (1–1001), or /incidents SEARCH.")
        elif not re.fullmatch(r"[\w .-]{2,120}", command.argument):
            raise DeskError("Use a short incident search without query operators.")
    return command


def help_card() -> cards.Card:
    return cards.card(
        "Service Desk commands",
        [
            cards.text("These commands run directly in the application, without a model call."),
            *[cards.text(f"/{name} — {description}") for name, description in COMMANDS.items()],
        ],
    )


def agent_card(settings: Settings, skill_names: list[str] | None) -> cards.Card:
    details = {
        "Agent": os.environ.get("FOUNDRY_AGENT_NAME", "customer-service-desk"),
        "Hosted version": os.environ.get("FOUNDRY_AGENT_VERSION", "Not reported by the host"),
        "Application version": version("customer-service-desk"),
        "Model deployment": settings.model_deployment,
        "Harness": "GitHub Copilot SDK " + version("github-copilot-sdk"),
        "Channel": "Activity Protocol / Teams personal chat",
        "Skills": ", ".join(skill_names)
        if skill_names
        else ("Skill catalog unavailable." if skill_names is None else "No skills advertised."),
        "Write confirmation": "Required"
        if settings.require_write_confirmation
        else "Off - explicit requests and form submissions commit directly",
    }
    return cards.card(
        "Agent details",
        [
            {
                "type": "FactSet",
                "facts": [
                    {"title": key, "value": cards.escaped(value)} for key, value in details.items()
                ],
            },
            cards.text(
                "Commands bypass the model. ServiceNow changes remain policy-gated. "
                + (
                    "Review and an explicit Confirm click are required."
                    if settings.require_write_confirmation
                    else "Demo mode skips the separate write review."
                ),
                subtle=True,
            ),
            cards.text("Use /help for all supported commands.", subtle=True),
        ],
    )


async def run_data_command(command: Command, context: ToolContext) -> cards.Card:
    match command.name:
        case "incident":
            if re.fullmatch(r"INC[0-9]{1,20}", command.argument, re.IGNORECASE):
                item = await context.snow.get_incident(context.principal, command.argument.upper())
                return cards.detail(
                    item,
                    context.snow.policy,
                    context.portal_url,
                    require_confirmation=context.actions.require_confirmation,
                    scope_token=context.principal.scope,
                )
            if not context.snow.policy.allow_create:
                return cards.notice(
                    "Incident creation is disabled",
                    "This instance does not allow incident creation from chat. "
                    "Open ServiceNow to report an issue directly. No incident was created.",
                )
            return await context.incident_form(
                DraftRequest(operation="create", short_description=command.argument)
            )
        case "incidents":
            is_page = command.argument.isdigit()
            page = int(command.argument) - 1 if is_page else 0
            query = command.argument if command.argument and not is_page else ""
            items = await context.snow.list_incidents(context.principal, page, query=query)
            return cards.incidents(items, page, query=query)
        case "kb":
            articles = await context.snow.search_knowledge(command.argument)
            return cards.knowledge_results(command.argument, articles)
    raise DeskError("This command is not available here. Use /help.")
