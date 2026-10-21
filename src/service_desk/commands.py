"""Explicit chat commands: parsing and rendering never invoke the model."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from importlib.metadata import version

from service_desk import cards
from service_desk.config import Settings
from service_desk.models import DeskError
from service_desk.tools import ToolContext

COMMANDS = {
    "feedback": "Provide feedback (optionally add text after the command).",
    "incident": "Open a new incident form; creation must be enabled and confirmed.",
    "kb": "Search approved knowledge: /kb printer, or open the search form.",
    "agent": "Show agent, model, version, and skill details.",
    "new": "Start fresh; forget model context and invalidate old cases/reviews.",
    "incidents": "List your incidents, including closed ones; optional page number.",
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
    limit = {"feedback": 4000, "incident": 160, "kb": 120, "incidents": 4}.get(command.name)
    if limit and len(command.argument) > limit:
        raise DeskError(f"/{command.name} accepts at most {limit} characters.")
    if command.name == "incidents" and command.argument:
        if not re.fullmatch(r"[1-9][0-9]{0,3}", command.argument) or int(command.argument) > 1001:
            raise DeskError("Usage: /incidents or /incidents PAGE (1–1001).")
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
                "Commands bypass the model. ServiceNow changes remain policy-gated "
                "and require review and an explicit Confirm click.",
                subtle=True,
            ),
            cards.text("Use /help for all supported commands.", subtle=True),
        ],
    )


async def run_data_command(command: Command, context: ToolContext) -> cards.Card:
    match command.name:
        case "incident":
            if not context.snow.policy.allow_create:
                return cards.notice(
                    "Incident creation is disabled",
                    "This instance does not allow incident creation from chat. "
                    "Open ServiceNow to report an issue directly. No incident was created.",
                )
            return cards.intake(title=command.argument)
        case "incidents":
            page = int(command.argument) - 1 if command.argument else 0
            return cards.incidents(await context.snow.list_incidents(context.principal, page), page)
        case "kb":
            articles = await context.snow.search_knowledge(command.argument)
            return cards.knowledge_results(command.argument, articles)
    raise DeskError("This command is not available here. Use /help.")
