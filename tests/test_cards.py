from __future__ import annotations

import json
from base64 import b64decode
from importlib.resources import files
from pathlib import Path

import jsonschema
import pytest

from service_desk import cards, commands
from service_desk.config import SnowPolicy
from service_desk.models import Article, CaseOption, Draft, DraftRequest, Incident

FIXTURES = Path(__file__).parent / "fixtures"


def gallery() -> dict[str, dict]:
    incident = Incident(
        sys_id="a" * 32,
        number="INC0012345",
        caller_id="1" * 32,
        short_description="Printer unavailable on Floor 2",
        description="The shared printer appears offline. A test page did not print.",
        state="2",
        state_label="In progress",
        priority="3 - Moderate",
        updated="2026-01-01 10:00:00",
        version="1",
        comments="The service desk is investigating.",
    )
    policy = SnowPolicy(
        states={"2": "In progress", "6": "Resolved"},
        allow_comments=True,
        resolve_from=["2"],
        resolved_state="6",
        resolution_codes=["Solved"],
    )
    draft = Draft(
        id="b" * 32,
        scope="example",
        caller_id="1" * 32,
        request=DraftRequest(
            operation="comment", incident_number=incident.number, comment="A test page failed."
        ),
        payload={"comments": "A test page failed."},
        expires_at=1800000000,
    )
    result = {
        "commands-help": commands.help_card(),
        "feedback-form": cards.feedback_form("synthetic-response"),
        "knowledge-search": cards.knowledge_search(),
        "knowledge-results": cards.knowledge_results(
            "printer",
            [
                Article(
                    number="KB0001",
                    title="Printer checks",
                    text="Check the selected printer.",
                    url="https://mock.service-now.com/kb_view.do?sys_kb_id=" + "a" * 32,
                )
            ],
        ),
        "profile": cards.profile(
            "Synthetic Test User",
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
            "1" * 32,
        ),
        "assistant-answer": cards.answer(
            "I can help with your incidents and printer troubleshooting."
        ),
        "welcome": cards.welcome(),
        "incident-list": cards.incidents([incident], 0),
        "incident-detail": cards.detail(incident, policy, "https://mock.service-now.com/sp"),
        "empty-list": cards.incidents([], 0),
        "intake": cards.intake(
            classifications={"hardware": "Hardware", "software:email": "Software / Email"},
            action_id="e" * 32,
        ),
        "comment": cards.comment_form(incident.number, action_id="e" * 32),
        "resolve": cards.resolve_form(incident.number, policy, action_id="e" * 32),
        "review": cards.review(draft),
        "receipt": cards.receipt(
            draft.model_copy(update={"status": "succeeded", "result": incident}),
            "https://mock.service-now.com/sp",
        ),
        "troubleshooting": cards.troubleshooting(
            "Printing / Check the selected printer",
            "Confirm that the intended printer is selected. Did that solve the problem?",
            "printer-troubleshooting",
        ),
        "case-options": cards.case_options(
            "Printer troubleshooting",
            "Which troubleshooting path do you want to use?",
            [
                CaseOption(
                    id="remote", label="Remote checks", description="Check from your computer."
                ),
                CaseOption(id="local", label="Local checks", description="Observe the device."),
            ],
            "c" * 32,
        ),
        "case-question": cards.case_question(
            "Email troubleshooting", "What error does Outlook show?", "d" * 32
        ),
        "follow-up": cards.follow_up(),
        "incident-follow-up": cards.incident_follow_up(incident.number),
        "access-error": cards.notice(
            "Access help", "Your account is not mapped. Contact service desk support."
        ),
    }
    return result


@pytest.mark.parametrize("name", list(gallery()))
def test_cards_validate_against_official_schema_and_snapshots(name):
    schema = json.loads((FIXTURES / "adaptive-card.schema.json").read_text())
    card = gallery()[name]
    standard_card = dict(card)
    if "msteams" in standard_card:
        assert standard_card.pop("msteams") == {"width": "Full"}
    jsonschema.Draft6Validator(schema).validate(standard_card)
    assert card == json.loads((FIXTURES / "cards" / f"{name}.json").read_text())
    assert len(card["actions"]) <= 3
    assert "refresh" not in card
    assert len(json.dumps(card).encode()) < 24000
    assert "fallbackText" in card
    for action in card["actions"]:
        assert action["type"] == "Action.Execute"
        assert action["fallback"]["type"] == "Action.Submit"
        assert action["data"] == action["fallback"]["data"]


@pytest.mark.parametrize("name", list(gallery()))
def test_all_cards_use_customer_wordmark_and_accessible_service_identity(name):
    header = gallery()[name]["body"][0]
    logo = header["columns"][0]["items"][0]
    assert logo == {
        "type": "Image",
        "url": cards.CUSTOMER_LOGO,
        "altText": "Customer",
        "width": "168px",
        "height": "56px",
        "backgroundColor": "#FFFFFF",
    }
    assert header["columns"][1]["items"][0]["text"] == "SERVICE DESK"
    assert "Customer Service Desk" in gallery()[name]["fallbackText"]


def test_customer_logo_is_a_small_self_contained_packaged_red_png():
    prefix = "data:image/png;base64,"
    assert cards.CUSTOMER_LOGO.startswith(prefix)
    image = b64decode(cards.CUSTOMER_LOGO.removeprefix(prefix), validate=True)
    assert image == files("service_desk").joinpath("assets/customer-logo.png").read_bytes()
    assert len(image) <= 1000
    assert image[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(image[16:20], "big") == 336
    assert int.from_bytes(image[20:24], "big") == 112
    chunks = {}
    position = 8
    while position < len(image):
        length = int.from_bytes(image[position : position + 4], "big")
        kind = image[position + 4 : position + 8]
        chunks[kind] = image[position + 8 : position + 8 + length]
        position += length + 12
    assert chunks[b"PLTE"] == bytes((255, 255, 255, 201, 24, 50))
    assert b"tRNS" not in chunks


@pytest.mark.parametrize("name", list(gallery()))
def test_cards_avoid_duplicate_feedback_and_only_link_to_specific_service_now_records(name):
    value = gallery()[name]
    assert "service-now-portal" not in str(value)
    links = [
        action
        for item in value["body"]
        if item["type"] == "ActionSet"
        for action in item["actions"]
        if action["type"] == "Action.OpenUrl"
    ]
    if name in ("incident-detail", "receipt"):
        assert len(links) == 1
        assert links[0]["title"] == "View incident in ServiceNow"
        assert links[0]["url"].endswith("id=ticket&table=incident&sys_id=" + "a" * 32)
        assert links[0]["url"] in value["fallbackText"]
    elif name == "knowledge-results":
        assert len(links) == 1
        assert links[0]["url"].endswith("kb_view.do?sys_kb_id=" + "a" * 32)
    else:
        assert links == []
    assert not any(item.get("id") == "response-feedback" for item in value["body"])
    assert "feedback_like" not in json.dumps(value)
    assert "feedback_dislike" not in json.dumps(value)
    assert "More to share?" not in json.dumps(value)


def test_read_only_incident_keeps_navigation_and_explains_comment_policy():
    item = Incident(
        sys_id="a" * 32,
        number="INC0012345",
        caller_id="1" * 32,
        short_description="Printer unavailable",
        state="7",
        state_label="Closed",
        updated="2026-01-01",
        version="1",
    )
    value = cards.detail(item, SnowPolicy(), "https://mock.service-now.com/sp")
    actions = [
        action
        for body in value["body"]
        if body["type"] == "ActionSet"
        for action in body["actions"]
    ]
    assert [a.get("verb") for a in actions[:2]] == ["troubleshoot", "list"]
    assert "Comments in this chat are currently disabled" in str(value)
    assert [a["verb"] for a in value["actions"]] == ["view"]


def test_incidents_use_table_with_headers_and_read_only_row_actions():
    card = gallery()["incident-list"]
    table = card["body"][3]
    assert table["type"] == "Table"
    assert len(table["columns"]) == 4
    assert [cell["items"][0]["text"] for cell in table["rows"][0]["cells"]] == [
        "Incident",
        "Status",
        "Priority",
        r"Updated \(UTC\)",
    ]
    cells = table["rows"][1]["cells"]
    open_action = cells[0]["items"][0]["actions"][0]
    assert open_action["title"] == "INC0012345"
    assert open_action["data"] == {"verb": "view", "incident": "INC0012345"}
    assert open_action["fallback"]["data"] == open_action["data"]
    assert open_action["associatedInputs"] == "none"
    assert cells[0]["items"][1]["text"] == "Printer unavailable on Floor 2"
    assert [cell["items"][0]["text"] for cell in cells[1:]] == [
        "In progress",
        "3 - Moderate",
        "2026-01-01 10:00:00",
    ]
    assert card["msteams"] == {"width": "Full"}
    fallback = table["fallback"]
    assert fallback["type"] == "Container"
    for row, fallback_row in zip(table["rows"], fallback["items"], strict=True):
        assert [cell["items"] for cell in row["cells"]] == [
            column["items"] for column in fallback_row["columns"]
        ]


def test_incident_table_bounds_untrusted_values_and_preserves_pagination():
    incident = Incident(
        sys_id="a" * 32,
        number="INC0012345",
        caller_id="1" * 32,
        short_description="<b>" + "[" * 1000 + "</b>",
        state="2",
        state_label="*" * 1000,
        priority="`" * 1000,
        updated="[" * 1000,
        version="1",
    )
    card = cards.incidents([incident] * 5, 1)
    table = card["body"][3]
    assert len(table["rows"]) == 6
    cells = table["rows"][1]["cells"]
    assert cells[0]["items"][1]["text"] == r"\[" * 157 + "..."
    assert cells[1]["items"][0]["text"] == r"\*" * 80
    assert cells[2]["items"][0]["text"] == r"\`" * 80
    assert cells[3]["items"][0]["text"] == r"\[" * 40
    assert "<b>" not in json.dumps(card)
    assert len(json.dumps(card).encode()) < 24000
    assert [entry["data"] for entry in card["actions"]] == [
        {"verb": "intake"},
        {"verb": "list", "page": 0},
        {"verb": "list", "page": 2},
    ]
    standard_card = dict(card)
    standard_card.pop("msteams")
    schema = json.loads((FIXTURES / "adaptive-card.schema.json").read_text())
    jsonschema.Draft6Validator(schema).validate(standard_card)


def test_empty_incident_page_has_no_table_or_next_page():
    card = cards.incidents([], 1)
    assert all(entry["type"] != "Table" for entry in card["body"])
    assert "No incidents" in card["body"][2]["text"]
    assert [entry["title"] for entry in card["actions"]] == ["Report an issue", "Previous"]


def test_untrusted_text_cannot_create_links_or_html():
    value = cards.escaped("<script>bad</script>[click](https://evil.example)")
    assert "<script>" not in value
    assert r"\[click\]\(https://evil.example\)" in value


def test_welcome_addresses_user_safely_and_suggests_supported_topics():
    card = cards.welcome("<b>Adele</b> [Vance](https://evil.example)")
    assert card["body"][1]["text"] == r"Hello, Adele \[Vance\]\(https://evil.example\)\!"
    assert len(card["actions"]) == 3
    content = json.dumps(card)
    assert "incidents" in content
    assert "printer" in content
    assert "<b>" not in content
    assert "Hello, Adele" in card["fallbackText"]
    schema = json.loads((FIXTURES / "adaptive-card.schema.json").read_text())
    jsonschema.Draft6Validator(schema).validate(card)


def test_welcome_bounds_display_name_and_has_no_placeholder_identity():
    assert cards.welcome()["body"][1]["text"] == r"Hello\!"
    assert len(cards.welcome("A" * 1000)["body"][1]["text"]) < 100


def test_cancellation_does_not_validate_required_form_fields():
    form = cards.intake()
    assert form["actions"][1]["associatedInputs"] == "none"
    assert form["actions"][0]["associatedInputs"] == "auto"


def test_review_never_contains_authority_bearing_payload_in_action_data():
    review = gallery()["review"]
    for entry in review["actions"]:
        assert set(entry["data"]) == {"verb", "action_id"}


def test_state_changes_are_not_unconditionally_offered():
    card = gallery()["incident-detail"]
    assert "Resolve" in [a["title"] for a in card["actions"]]
    assert "Review reopening" not in [a["title"] for a in card["actions"]]
