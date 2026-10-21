from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from copilot import ToolInvocation
from microsoft_agents.activity import Activity

from service_desk.actions import Actions
from service_desk.main import deliver_knowledge
from service_desk.models import Article, DeskError, KnowledgeSource
from service_desk.tools import ToolContext, build_tools

from .test_main import compose

URL = "https://mock.service-now.com/kb_view.do?sys_kb_id=" + "a" * 32
SOURCE = KnowledgeSource(number="KB0001", title="Email connection checks", url=URL)


@pytest.mark.parametrize("update", [False, True])
async def test_markdown_knowledge_message_has_no_attachment_and_keeps_sources_and_feedback(update):
    turn = SimpleNamespace(
        activity=SimpleNamespace(reply_to_id="kb-message"),
        send_activity=AsyncMock(return_value=SimpleNamespace(id="kb-message")),
        update_activity=AsyncMock(),
    )
    response_id = await deliver_knowledge(
        turn,
        "**Email checks**\n\n- Read the connection indicator.\n<script>bad()</script>",
        [SOURCE],
        update=update,
    )
    assert response_id == "kb-message"
    message = (turn.update_activity if update else turn.send_activity).call_args.args[0]
    assert not message.attachments
    assert message.text.startswith("**Email checks**")
    assert "<script>" not in message.text
    assert f"]({URL})" in message.text
    assert "https://mock.service-now.com/sp" not in message.text
    assert "Open in ServiceNow" not in message.text
    assert message.text_format == "markdown"
    assert message.channel_data["feedbackLoop"] == {"type": "default"}
    assert message.entities
    (turn.send_activity if update else turn.update_activity).assert_not_awaited()


async def test_existing_source_links_are_not_duplicated_and_body_is_bounded():
    turn = SimpleNamespace(
        activity=SimpleNamespace(reply_to_id=None),
        send_activity=AsyncMock(return_value=SimpleNamespace(id="reply")),
    )
    await deliver_knowledge(
        turn,
        f"See [Email checks]({URL}).\n" + "x" * 6000,
        [SOURCE],
    )
    message = turn.send_activity.call_args.args[0]
    assert message.text.count(URL) == 1
    assert "x" * 4001 not in message.text
    assert "**Sources**" not in message.text


@pytest.mark.parametrize("available", [True, False])
async def test_kb_question_sends_one_text_reply_without_result_or_followup_cards(
    monkeypatch, snow, memory, principal, activity, available
):
    context = ToolContext(principal, snow, memory, Actions(snow, memory), Mock(), "call")
    search = AsyncMock(
        return_value=[
            Article(number=SOURCE.number, title=SOURCE.title, text="Safe checks.", url=URL)
        ],
        side_effect=None if available else DeskError("Approved knowledge is unavailable."),
    )
    monkeypatch.setattr(snow, "search_knowledge", search)
    host, runtime = compose(monkeypatch, context)

    async def ask(context, prompt):
        tool = next(
            item for item in build_tools(context) if item.name == "search_approved_knowledge"
        )
        await tool.handler(ToolInvocation(arguments={"query": "email"}))
        yield "final", "**Email connection**\n\nRead the visible connection indicator."

    runtime.agent.ask = ask
    activity["text"] = "What does the email connection KB recommend?"
    turn = SimpleNamespace(
        activity=Activity.model_validate(activity),
        send_activity=AsyncMock(return_value=SimpleNamespace(id="knowledge-answer")),
    )
    await host.agent_app.handlers["message"](turn, None)
    assert turn.send_activity.await_count == 1
    message = turn.send_activity.call_args.args[0]
    assert not message.attachments
    assert "**Email connection**" in message.text
    assert (URL in message.text) is available
    assert "https://mock.service-now.com/sp" not in message.text
    assert ("general guidance" in message.text) is (not available)
    assert context.outbox == []

    monkeypatch.setattr("service_desk.main.hosted_call_id", lambda: "verified")
    activity["text"] = "Feedback: this answer is helpful"
    feedback = SimpleNamespace(
        activity=Activity.model_validate(activity), send_activity=AsyncMock()
    )
    await host.agent_app.handlers["message"](feedback, None)
    assert runtime.feedback.record.call_args.kwargs["response_id"] == "knowledge-answer"
