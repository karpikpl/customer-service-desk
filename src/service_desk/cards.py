"""Theme-aware Adaptive Cards, authored deterministically rather than by the model."""

from __future__ import annotations

import re
import uuid
from base64 import b64encode
from importlib.resources import files
from typing import Any
from urllib.parse import urlencode

from service_desk.config import SnowPolicy
from service_desk.models import Article, CaseOption, Draft, DraftRequest, Incident, KnowledgeSource

Card = dict[str, Any]
CUSTOMER_LOGO = "data:image/png;base64," + b64encode(
    files("service_desk").joinpath("assets/customer-logo.png").read_bytes()
).decode("ascii")


def escaped(value: str, limit: int = 4000) -> str:
    text = re.sub(r"<[^>]*>", "", value)[:limit]
    return re.sub(r"([\\`*_\[\]()#!])", r"\\\1", text)


def text(value: str, *, subtle: bool = False, size: str = "Default", limit: int = 4000) -> Card:
    return {
        "type": "TextBlock",
        "text": escaped(value, limit),
        "wrap": True,
        "size": size,
        **({"isSubtle": True} if subtle else {}),
    }


def action(title: str, verb: str, **data: Any) -> Card:
    payload = {"verb": verb, **data}
    inputs = (
        "auto"
        if verb.startswith("prepare_") or verb in ("feedback_submit", "kb_search", "case_answer")
        else "none"
    )
    return {
        "type": "Action.Execute",
        "title": title,
        "verb": verb,
        "data": payload,
        "associatedInputs": inputs,
        "fallback": {
            "type": "Action.Submit",
            "title": title,
            "data": payload,
            "associatedInputs": inputs,
        },
    }


def card(title: str, body: list[Card], actions: list[Card] | None = None) -> Card:
    return {
        "$schema": "https://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.5",
        "fallbackText": f"Customer Service Desk: {title}. Continue by typing your request.",
        "body": [
            {
                "type": "ColumnSet",
                "spacing": "None",
                "columns": [
                    {
                        "type": "Column",
                        "width": "auto",
                        "items": [
                            {
                                "type": "Image",
                                "url": CUSTOMER_LOGO,
                                "altText": "Customer",
                                "width": "168px",
                                "height": "56px",
                                "backgroundColor": "#FFFFFF",
                            }
                        ],
                    },
                    {
                        "type": "Column",
                        "width": "stretch",
                        "verticalContentAlignment": "Center",
                        "items": [
                            {
                                **text("SERVICE DESK", size="Small"),
                                "weight": "Bolder",
                                "horizontalAlignment": "Right",
                            },
                            {
                                **text("Employee support", subtle=True, size="Small"),
                                "spacing": "None",
                                "horizontalAlignment": "Right",
                            },
                        ],
                    },
                ],
            },
            {
                **text(title, size="Large"),
                "weight": "Bolder",
                "spacing": "Medium",
                "separator": True,
            },
            *body,
        ],
        "actions": actions or [],
    }


def notice(title: str, message: str) -> Card:
    return card(title, [text(message)], [action("My incidents", "list")])


def feedback_form(response_id: str = "") -> Card:
    return card(
        "Provide feedback",
        [
            text(
                "Feedback goes to Application Insights, not ServiceNow. "
                "Do not include passwords or unnecessary personal details.",
                subtle=True,
            ),
            input_text("feedback_text", "Your feedback", multiline=True),
            {
                "type": "Input.ChoiceSet",
                "id": "feedback_rating",
                "label": "Rating",
                "value": "neutral",
                "choices": [
                    {"title": "Positive", "value": "positive"},
                    {"title": "Negative", "value": "negative"},
                    {"title": "Suggestion / neutral", "value": "neutral"},
                ],
            },
        ],
        [
            action("Submit feedback", "feedback_submit", feedback_response_id=response_id),
            action("Cancel", "home"),
        ],
    )


def knowledge_search() -> Card:
    return card(
        "Search ServiceNow knowledge",
        [
            text(
                "Search published, active ServiceNow articles across all knowledge bases.",
                subtle=True,
            ),
            {**input_text("kb_query", "Search words"), "maxLength": 120},
        ],
        [action("Search", "kb_search"), action("Cancel", "home")],
    )


def knowledge_results(query: str, articles: list[Article]) -> Card:
    body = [text(f"Search: {query}", subtle=True)]
    if not articles:
        body.append(text("No published articles matched. Try different search words."))
    for article in articles:
        body.extend(
            [
                {**text(f"{article.number} — {article.title}"), "weight": "Bolder"},
                text(re.sub(r"<[^>]*>", " ", article.text)[:400], size="Small"),
                {
                    "type": "ActionSet",
                    "actions": [
                        {
                            "type": "Action.OpenUrl",
                            "title": "Open article in ServiceNow",
                            "url": article.url,
                        }
                    ],
                },
            ]
        )
    return card("Knowledge search results", body, [action("Search again", "kb_form")])


def profile(display_name: str, tenant: str, user: str, caller: str | None) -> Card:
    details = {
        "Teams name": display_name or "Not supplied by Teams",
        "Entra user ID": user,
        "Tenant ID": tenant,
        "ServiceNow caller ID": caller or "Not mapped",
    }
    return card(
        "Your identity",
        [
            {
                "type": "FactSet",
                "facts": [
                    {"title": key, "value": escaped(value)} for key, value in details.items()
                ],
            },
            text(
                "Identity comes from your authenticated Teams account. "
                "The ServiceNow caller is a temporary mock mapping, not browser SSO.",
                subtle=True,
            ),
        ],
    )


def answer(message: str) -> Card:
    result = card(
        "Service Desk",
        [{"type": "TextBlock", "text": re.sub(r"<[^>]*>", "", message)[:4000], "wrap": True}],
    )
    result["fallbackText"] = "Customer Service Desk: " + re.sub(r"<[^>]*>", "", message)[:1500]
    return result


def with_incident_link(value: Card, url: str) -> Card:
    return {
        **value,
        "body": [
            *value["body"],
            {
                "type": "ActionSet",
                "id": "service-now-incident",
                "separator": True,
                "spacing": "Medium",
                "actions": [
                    {
                        "type": "Action.OpenUrl",
                        "title": "View incident in ServiceNow",
                        "url": url,
                    }
                ],
            },
        ],
        "fallbackText": value["fallbackText"] + f" Incident in ServiceNow: {url}",
    }


def incident_url(portal_url: str, item: Incident) -> str:
    return (
        portal_url + "?" + urlencode({"id": "ticket", "table": "incident", "sys_id": item.sys_id})
    )


def welcome(display_name: str = "", *, require_confirmation: bool = True) -> Card:
    name = " ".join(re.sub(r"<[^>]*>", "", display_name).split())[:80]
    title = f"Hello, {name}!" if name else "Hello!"
    result = card(
        title,
        [
            text("I'm Customer Service Desk. How can I help you today?"),
            text("View your incidents and their latest updates."),
            text("Report an issue in one form or update an existing ticket."),
            text("Troubleshoot printer problems step by step."),
            text(
                "ServiceNow changes must be enabled by your service desk. "
                + (
                    "You review and confirm each change before it is sent."
                    if require_confirmation
                    else "Demo mode: submitting a form or requesting an update sends it "
                    "without a separate confirmation."
                ),
                subtle=True,
            ),
            text(
                "Commands: /feedback /incident /kb /agent /new /incidents /whoami /help",
                subtle=True,
                size="Small",
            ),
        ],
        [
            action("My incidents", "list"),
            action("Report an issue", "intake"),
            action("Troubleshoot", "troubleshoot"),
        ],
    )
    result["fallbackText"] = (
        f"{title} I'm Customer Service Desk. How can I help you today? "
        "I can help you view incidents, describe an issue, or troubleshoot printing. "
        + (
            "ServiceNow changes require review and confirmation when enabled."
            if require_confirmation
            else "Demo mode: submissions and requested updates have no separate confirmation."
        )
    )
    return result


def incidents(items: list[Incident], page: int, *, query: str = "") -> Card:
    rows: list[Card] = [
        {
            "type": "TableRow",
            "style": "emphasis",
            "cells": [
                {
                    "type": "TableCell",
                    "items": [{**text(label, size="Small"), "weight": "Bolder"}],
                }
                for label in ("Incident", "Status", "Priority", "Updated (UTC)")
            ],
        }
    ]
    for item in items:
        summary = re.sub(r"<[^>]*>", "", item.short_description)
        if len(summary) > 160:
            summary = summary[:157] + "..."
        rows.append(
            {
                "type": "TableRow",
                "cells": [
                    {
                        "type": "TableCell",
                        "items": [
                            {
                                "type": "ActionSet",
                                "actions": [action(item.number, "view", incident=item.number)],
                            },
                            text(summary, size="Small"),
                        ],
                    },
                    {"type": "TableCell", "items": [text(item.state_label[:80], size="Small")]},
                    {"type": "TableCell", "items": [text(item.priority[:80], size="Small")]},
                    {"type": "TableCell", "items": [text(item.updated[:40], size="Small")]},
                ],
            }
        )
    widths = [4.4, 1.6, 1.8, 2.2]
    body = (
        [
            text(
                (f"Search: {query} | " if query else "")
                + f"Page {page + 1} | Open an incident for details and next steps.",
                subtle=True,
            ),
            {
                "type": "Table",
                "columns": [{"width": width} for width in widths],
                "rows": rows,
                "showGridLines": True,
                "gridStyle": "default",
                "verticalCellContentAlignment": "Center",
                "fallback": {
                    "type": "Container",
                    "items": [
                        {
                            "type": "ColumnSet",
                            "separator": index > 0,
                            "columns": [
                                {"type": "Column", "width": width, "items": cell["items"]}
                                for width, cell in zip(widths, row["cells"], strict=True)
                            ],
                        }
                        for index, row in enumerate(rows)
                    ],
                },
            },
        ]
        if items
        else [text("No incidents on this page. Report an issue if you need help.")]
    )
    actions = [action("Report an issue", "intake")]
    if page:
        actions.append(
            action(
                "Previous", "list", page=page - 1, **({"incident_query": query} if query else {})
            )
        )
    if len(items) == 5 and page < 1000:
        actions.append(
            action("Next", "list", page=page + 1, **({"incident_query": query} if query else {}))
        )
    result = card("Your incidents", body, actions)
    if items:
        result["msteams"] = {"width": "Full"}
    return result


def detail(
    item: Incident,
    policy: SnowPolicy,
    portal_url: str,
    *,
    require_confirmation: bool = True,
    scope_token: str = "",
) -> Card:
    actions = []
    if policy.allow_comments:
        actions.append(action("Add comment", "comment_form", incident=item.number))
    if item.state in policy.resolve_from:
        actions.append(action("Resolve", "resolve_form", incident=item.number))
    elif item.state in policy.reopen_from:
        actions.append(
            action(
                "Review reopening" if require_confirmation else "Reopen incident",
                "prepare_reopen",
                incident=item.number,
                action_id=uuid.uuid4().hex,
                scope_token=scope_token,
            )
        )
    actions.append(action("Refresh", "view", incident=item.number))
    result = card(
        item.number,
        [
            {**text(item.short_description, size="Medium"), "weight": "Bolder"},
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Status", "value": escaped(item.state_label)},
                    {"title": "Priority", "value": escaped(item.priority)},
                    {"title": "Updated", "value": escaped(item.updated + " UTC")},
                ],
            },
            {
                "type": "Container",
                "style": "emphasis",
                "items": [
                    {**text("LATEST CUSTOMER UPDATE", subtle=True, size="Small")},
                    text(item.comments or "No customer-visible update available."),
                ],
            },
            text("DESCRIPTION", subtle=True, size="Small"),
            text(item.description or item.short_description),
            *(
                []
                if policy.allow_comments
                else [
                    text(
                        "Comments in this chat are currently disabled. Open this incident "
                        "in ServiceNow to follow up directly.",
                        subtle=True,
                        size="Small",
                    )
                ]
            ),
            {
                "type": "ActionSet",
                "spacing": "Medium",
                "actions": [
                    action("Troubleshoot this issue", "troubleshoot", incident=item.number),
                    action("My incidents", "list"),
                ],
            },
        ],
        actions,
    )
    return with_incident_link(result, incident_url(portal_url, item))


def input_text(
    identifier: str,
    label: str,
    *,
    multiline: bool = False,
    value: str = "",
    required: bool = True,
    max_length: int | None = None,
) -> Card:
    return {
        "type": "Input.Text",
        "id": identifier,
        "label": label,
        "isMultiline": multiline,
        "isRequired": required,
        "errorMessage": "Please complete this field.",
        "maxLength": max_length or (4000 if multiline else 160),
        "value": value,
    }


def write_notice(require_confirmation: bool) -> Card:
    return text(
        "Next, review exactly what will be sent. Nothing changes until you confirm."
        if require_confirmation
        else "Demo mode: submitting sends this change immediately. There is no separate review.",
        subtle=True,
        size="Small",
    )


def intake(
    title: str = "",
    description: str = "",
    *,
    request: DraftRequest | None = None,
    classifications: dict[str, str] | None = None,
    action_id: str = "",
    require_confirmation: bool = True,
    scope_token: str = "",
) -> Card:
    request = request or DraftRequest(
        operation="create", short_description=title, description=description
    )
    classification = request.category + (":" + request.subcategory if request.subcategory else "")

    def choice(identifier: str, label: str, values: dict[str, str], value: str) -> Card:
        return {
            "type": "Input.ChoiceSet",
            "id": identifier,
            "label": label,
            "isRequired": True,
            "errorMessage": f"Choose {label.lower()}.",
            "value": value,
            "choices": [{"title": title, "value": key} for key, title in values.items()],
        }

    return card(
        "Report an issue",
        [
            text(
                "Capture the issue in one form. Known details are filled in for you.", subtle=True
            ),
            input_text("title", "Issue title", value=request.short_description),
            input_text("description", "What happened?", multiline=True, value=request.description),
            choice(
                "classification", "Category / subcategory", classifications or {}, classification
            ),
            {
                "type": "ColumnSet",
                "columns": [
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            choice(
                                "impact",
                                "Impact",
                                {
                                    "3": "Low - one person",
                                    "2": "Medium - team",
                                    "1": "High - widespread",
                                },
                                request.impact,
                            )
                        ],
                    },
                    {
                        "type": "Column",
                        "width": "stretch",
                        "items": [
                            choice(
                                "urgency",
                                "Urgency",
                                {
                                    "3": "Low - can continue working",
                                    "2": "Medium - workaround available",
                                    "1": "High - blocked, no workaround",
                                },
                                request.urgency,
                            )
                        ],
                    },
                ],
            },
            {
                "type": "ActionSet",
                "actions": [
                    {
                        "type": "Action.ToggleVisibility",
                        "title": "Additional context (optional)",
                        "targetElements": ["incident-context"],
                    }
                ],
            },
            {
                "type": "Container",
                "id": "incident-context",
                "isVisible": any(
                    (
                        request.reported_location,
                        request.device,
                        request.business_impact,
                        request.attempted_steps,
                    )
                ),
                "items": [
                    input_text(
                        "reported_location",
                        "Reported location",
                        value=request.reported_location,
                        required=False,
                    ),
                    input_text("device", "Device / printer", value=request.device, required=False),
                    input_text(
                        "business_impact",
                        "How is work affected?",
                        multiline=True,
                        value=request.business_impact,
                        required=False,
                        max_length=500,
                    ),
                    input_text(
                        "attempted_steps",
                        "What have you already tried?",
                        multiline=True,
                        value=request.attempted_steps,
                        required=False,
                        max_length=1000,
                    ),
                ],
            },
            write_notice(require_confirmation),
        ],
        [
            action(
                "Review incident" if require_confirmation else "Create incident",
                "prepare_create",
                action_id=action_id or uuid.uuid4().hex,
                scope_token=scope_token,
            ),
            action("Cancel", "home"),
        ],
    )


def comment_form(
    number: str,
    value: str = "",
    *,
    action_id: str = "",
    require_confirmation: bool = True,
    scope_token: str = "",
) -> Card:
    return card(
        f"Comment on {number}",
        [
            input_text("comment", "Customer-visible comment", multiline=True, value=value),
            write_notice(require_confirmation),
        ],
        [
            action(
                "Review comment" if require_confirmation else "Add comment",
                "prepare_comment",
                incident=number,
                action_id=action_id or uuid.uuid4().hex,
                scope_token=scope_token,
            ),
            action("Cancel", "view", incident=number),
        ],
    )


def resolve_form(
    number: str,
    policy: SnowPolicy,
    *,
    request: DraftRequest | None = None,
    action_id: str = "",
    require_confirmation: bool = True,
    scope_token: str = "",
) -> Card:
    return card(
        f"Resolve {number}",
        [
            {
                "type": "Input.ChoiceSet",
                "id": "resolution_code",
                "label": "Resolution code",
                "isRequired": True,
                "errorMessage": "Choose an approved resolution code.",
                "choices": [{"title": code, "value": code} for code in policy.resolution_codes],
                "value": request.resolution_code if request else "",
            },
            input_text(
                "resolution_notes",
                "What resolved the issue?",
                multiline=True,
                value=request.resolution_notes if request else "",
            ),
            write_notice(require_confirmation),
        ],
        [
            action(
                "Review resolution" if require_confirmation else "Resolve incident",
                "prepare_resolve",
                incident=number,
                action_id=action_id or uuid.uuid4().hex,
                scope_token=scope_token,
            ),
            action("Cancel", "view", incident=number),
        ],
    )


def review(draft: Draft) -> Card:
    body = [
        text(
            f"{draft.request.operation.title()} "
            f"{draft.request.incident_number or 'a new incident'}",
            size="Medium",
        ),
        text("Nothing has been changed. Review exactly what will be sent.", subtle=True),
    ]
    for key, value in draft.payload.items():
        body.extend(
            [
                text(key.replace("_", " ").upper(), subtle=True, size="Small"),
                text(
                    "Your mapped ServiceNow account" if key == "caller_id" else value,
                    limit=4500 if key == "comments" else 4000,
                ),
            ]
        )
    body.append(text("This review expires after a short period. Confirm only once.", subtle=True))
    return card(
        "Review your change",
        body,
        [
            action("Confirm change", "confirm", action_id=draft.id),
            action("Edit", "edit", action_id=draft.id),
            action("Cancel", "cancel", action_id=draft.id),
        ],
    )


def receipt(draft: Draft, portal_url: str) -> Card:
    if draft.status != "succeeded" or draft.result is None:
        return notice(
            "Change not confirmed", "Refresh the incident before attempting another change."
        )
    result = draft.result
    return with_incident_link(
        card(
            "Change confirmed",
            [
                text(result.number, size="Medium"),
                text(result.short_description),
                text(f"ServiceNow status: {result.state_label}", subtle=True),
            ],
            [
                action("View incident", "view", incident=result.number),
                action("My incidents", "list"),
            ],
        ),
        incident_url(portal_url, result),
    )


def follow_up(active_case: bool = False) -> Card:
    return card(
        "What would you like to do next?",
        [text("Keep working on an issue or check a ticket.", subtle=True)],
        [
            action("My incidents", "list"),
            action("Report an issue", "intake"),
            action("Start a new case", "reset_case")
            if active_case
            else action("Troubleshoot", "troubleshoot"),
        ],
    )


def incident_follow_up(number: str) -> Card:
    return card(
        f"Next for {number}",
        [text("Keep troubleshooting this issue or check your other incidents.", subtle=True)],
        [
            action("Troubleshoot this issue", "troubleshoot", incident=number),
            action("My incidents", "list"),
            action("Report another issue", "intake"),
        ],
    )


def case_options(title: str, question: str, options: list[CaseOption], token: str) -> Card:
    return card(
        title,
        [
            text(question),
            *[
                {
                    "type": "Container",
                    "style": "emphasis",
                    "spacing": "Medium",
                    "items": [
                        {**text(option.label), "weight": "Bolder"},
                        text(option.description, size="Small"),
                    ],
                }
                for option in options
            ],
        ],
        [
            action(option.label, "case_option", case_token=token, option_id=option.id)
            for option in options
        ],
    )


def case_question(title: str, question: str, token: str) -> Card:
    return card(
        title,
        [
            text(question),
            {
                "type": "Input.Text",
                "id": "case_answer",
                "label": "Your answer",
                "isRequired": True,
                "errorMessage": "Enter an answer, or choose I don't know.",
                "maxLength": 2000,
                "isMultiline": True,
            },
        ],
        [
            action("Continue", "case_answer", case_token=token),
            action("I don't know", "case_unknown", case_token=token),
        ],
    )


def troubleshooting(
    title: str,
    step: str,
    source: str = "",
    token: str = "",
    *,
    allow_create: bool = True,
    sources: list[KnowledgeSource] | None = None,
) -> Card:
    body = [text(step)]
    if source:
        body.append(text(f"Source: {source}", subtle=True, size="Small"))
    body.extend(
        {
            "type": "ActionSet",
            "actions": [
                {
                    "type": "Action.OpenUrl",
                    "title": f"Open {item.number} in ServiceNow",
                    "url": item.url,
                }
            ],
        }
        for item in sources or []
    )
    return card(
        title,
        body,
        [
            action("That worked", "case_worked", case_token=token),
            action("Still not working", "case_failed", case_token=token),
            *([action("Create incident", "case_intake", case_token=token)] if allow_create else []),
        ],
    )
