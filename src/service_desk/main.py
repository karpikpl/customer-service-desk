"""Foundry Activity host and Teams delivery composition."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from azure.ai.agentserver.activity import ActivityAgentServerHost
from azure.ai.agentserver.core import get_request_context
from azure.identity.aio import DefaultAzureCredential
from microsoft_agents.activity import Activity, ActivityTypes, Attachment, InvokeResponse

from service_desk import cards, commands
from service_desk.actions import Actions
from service_desk.activities import CardAction, dispatch, match_case_option, parse_action
from service_desk.agent import DeskAgent
from service_desk.config import Settings, load_private
from service_desk.feedback import (
    FeedbackRating,
    FeedbackRecorder,
    detect_feedback,
    feedback_only,
    parse_native_feedback,
)
from service_desk.identity import IdentityMapper, teams_identity
from service_desk.models import DeskError, KnowledgeSource
from service_desk.servicenow import SnowClient
from service_desk.state import FoundryState, conversation_principal, reset_conversation
from service_desk.toolbox import SkillCatalog, ToolboxError
from service_desk.tools import ToolContext

logger = logging.getLogger("service_desk")
_SALUTATION = r"(?:hi|hello|hey|good (?:morning|afternoon|evening))"
_CAPABILITIES = r"(?:what can you do|how can you help(?: me)?|what can you help me with)"
_GREETING = re.compile(
    rf"(?:{_SALUTATION}(?:[,.!?\s]+{_CAPABILITIES})?|{_CAPABILITIES}|help|start)[!?.\s]*",
    flags=re.IGNORECASE,
)


def hosted_call_id() -> str:
    call_id = get_request_context().call_id
    if not os.environ.get("FOUNDRY_HOSTING_ENVIRONMENT") or not call_id:
        raise DeskError(
            "This action requires the authenticated Foundry-hosted Teams endpoint. "
            "Local testing uses synthetic fixtures only."
        )
    return call_id


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.credential = DefaultAzureCredential()
        self.agent = DeskAgent(settings, self.credential)
        self.toolbox = SkillCatalog(settings.toolbox_endpoint, self.credential)
        self.snow: SnowClient | None = None
        self.mapper: IdentityMapper | None = None
        self.init_lock = asyncio.Lock()
        self.locks: dict[str, asyncio.Lock] = {}
        self.pending: set[asyncio.Task[None]] = set()
        self.feedback = FeedbackRecorder(settings.feedback_connection_string.get_secret_value())

    async def initialize(self) -> None:
        async with self.init_lock:
            if self.snow is None:
                private = await load_private(self.settings, self.credential)
                self.mapper = IdentityMapper(self.settings.tenant_id, private.identities)
                self.snow = SnowClient(self.settings.snow_url, private.credentials, private.policy)

    @asynccontextmanager
    async def turn(self, activity: dict[str, Any]) -> AsyncIterator[ToolContext]:
        call_id = hosted_call_id()
        await self.initialize()
        assert self.mapper is not None and self.snow is not None
        principal = self.mapper.resolve(activity)
        lock = self.locks.setdefault(principal.scope, asyncio.Lock())
        async with lock:
            state = await FoundryState.open(self.credential)
            try:
                principal = await conversation_principal(state, principal, refresh=True)
                yield ToolContext(
                    principal=principal,
                    snow=self.snow,
                    state=state,
                    actions=Actions(self.snow, state, self.settings.confirmation_ttl),
                    toolbox=self.toolbox,
                    call_id=call_id,
                    portal_path=self.settings.snow_portal_path,
                )
            finally:
                await state.close()

    async def close(self) -> None:
        if self.pending:
            await asyncio.gather(*self.pending, return_exceptions=True)
        await self.agent.close()
        if self.snow:
            await self.snow.close()
        await self.credential.close()
        await asyncio.to_thread(self.feedback.close)


def attachment(card: cards.Card) -> Attachment:
    return Attachment(content_type="application/vnd.microsoft.card.adaptive", content=card)


def feedback_message(message: Activity) -> Activity:
    message.add_ai_metadata()
    data = message.channel_data
    message.channel_data = {
        **(data if isinstance(data, dict) else {}),
        "feedbackLoop": {"type": "default"},
    }
    return message


async def deliver(context: Any, card: cards.Card, *, update: bool = False) -> str:
    # Teams splits text plus a card into two messages, which cannot be updated together.
    card = cards.with_feedback(card)
    message = feedback_message(
        Activity(
            type=ActivityTypes.message,
            summary=card.get("fallbackText", "Customer Service Desk"),
            attachments=[attachment(card)],
        )
    )
    if update and context.activity.reply_to_id:
        message.id = context.activity.reply_to_id
        await context.update_activity(message)
        return message.id
    else:
        response = await context.send_activity(message)
        return response.id if isinstance(response.id, str) else ""


async def deliver_knowledge(
    context: Any,
    answer: str,
    sources: list[KnowledgeSource],
    *,
    update: bool = False,
) -> str:
    body = re.sub(r"<[^>]*>", "", answer).strip()[:4000]
    missing = [source for source in sources if source.url not in body]
    if missing:
        body += "\n\n**Sources**\n" + "\n".join(
            f"- [{cards.escaped(source.number + ' - ' + source.title, 240)}]({source.url})"
            for source in missing
        )
    if not sources:
        body += (
            "\n\n_No approved KB source was retrieved. "
            "Any troubleshooting advice is general guidance._"
        )
    body += "\n\nUse the feedback controls or `/feedback` to share feedback."
    message = feedback_message(
        Activity(type=ActivityTypes.message, text=body, text_format="markdown")
    )
    if update and context.activity.reply_to_id:
        message.id = context.activity.reply_to_id
        await context.update_activity(message)
        return message.id
    response = await context.send_activity(message)
    return response.id if isinstance(response.id, str) else ""


def create_host(settings: Settings) -> ActivityAgentServerHost:
    runtime = Runtime(settings)
    host = ActivityAgentServerHost()
    app = host.agent_app
    recent_welcomes: deque[tuple[str, str]] = deque(maxlen=256)
    recent_replies: OrderedDict[tuple[str, str], str] = OrderedDict()

    def response_key(activity: dict[str, Any]) -> tuple[str, str]:
        return (
            (activity.get("conversation") or {}).get("id", ""),
            (activity.get("from") or {}).get("aadObjectId", ""),
        )

    def remember_reply(context: Any, response_id: str) -> None:
        if response_id:
            key = response_key(context.activity.model_dump(by_alias=True, exclude_none=True))
            recent_replies[key] = response_id
            recent_replies.move_to_end(key)
            if len(recent_replies) > 256:
                recent_replies.popitem(last=False)

    async def send_card(context: Any, card: cards.Card, *, update: bool = False) -> None:
        response_id = await deliver(context, card, update=update)
        remember_reply(context, response_id)

    def record_message_feedback(activity: dict[str, Any], rating: FeedbackRating) -> None:
        runtime.feedback.record(
            activity,
            settings.tenant_id,
            source="conversation",
            rating=rating,
            text=activity.get("text") or "",
            response_id=activity.get("replyToId") or recent_replies.get(response_key(activity), ""),
        )

    def record_card_feedback(activity: dict[str, Any], action: CardAction) -> None:
        hosted_call_id()
        is_form = action.verb == "feedback_submit"
        if is_form and not action.feedback_text.strip():
            raise DeskError("Please enter your feedback before submitting.")
        runtime.feedback.record(
            activity,
            settings.tenant_id,
            source="command" if is_form else "card",
            rating=action.feedback_rating
            if is_form
            else ("positive" if action.verb == "feedback_like" else "negative"),
            text=action.feedback_text if is_form else "",
            response_id=action.feedback_response_id
            if is_form
            else (activity.get("replyToId") or recent_replies.get(response_key(activity), "")),
        )

    async def handle_command(
        context: Any, activity: dict[str, Any], command: commands.Command, *, update: bool = False
    ) -> None:
        call_id = hosted_call_id()
        tenant, user, _ = teams_identity(activity, settings.tenant_id)
        sender = activity.get("from") or {}
        if command.name == "help":
            card = commands.help_card()
        elif command.name == "agent":
            try:
                catalog = await runtime.toolbox.list_skills(call_id)
                skills = sorted(item["name"] for item in catalog)
            except ToolboxError:
                skills = None
            card = commands.agent_card(settings, skills)
        elif command.name == "feedback":
            response_id = activity.get("replyToId") or recent_replies.get(
                response_key(activity), ""
            )
            if command.argument:
                runtime.feedback.record(
                    activity,
                    settings.tenant_id,
                    source="command",
                    rating="neutral",
                    text=command.argument,
                    response_id=response_id,
                )
                card = cards.notice(
                    "Feedback received", "Thank you. No incident or case was changed."
                )
            else:
                card = cards.feedback_form(response_id)
        elif command.name == "kb" and not command.argument:
            card = cards.knowledge_search()
        elif command.name == "whoami":
            await runtime.initialize()
            assert runtime.mapper is not None
            principal = runtime.mapper.resolve_optional(activity)
            card = cards.profile(
                sender.get("name") or "", tenant, user, principal.caller_id if principal else None
            )
        else:
            async with runtime.turn(activity) as turn:
                if command.name == "new":
                    await runtime.agent.reset(turn.principal)
                    await reset_conversation(turn.state, turn.principal)
                    recent_replies.pop(response_key(activity), None)
                    card = cards.welcome(sender.get("name") or "")
                    card["body"].insert(
                        2,
                        cards.text(
                            "A fresh agent conversation has started. Previous model context is "
                            "discarded; old troubleshooting and review cards are no longer valid. "
                            "Teams history and ServiceNow records were not deleted. "
                            "Already-confirmed operations are not undone.",
                            subtle=True,
                        ),
                    )
                else:
                    card = await commands.run_data_command(command, turn)
        await send_card(context, card, update=update)

    async def send_welcome(context: Any, *, update: bool = False) -> None:
        activity = context.activity.model_dump(by_alias=True, exclude_none=True)
        conversation = activity.get("conversation") or {}
        tenant = (activity.get("channelData") or {}).get("tenant") or {}
        if activity.get("channelId") != "msteams" or conversation.get("conversationType") != (
            "personal"
        ):
            await send_card(
                context,
                cards.notice(
                    "Personal chat only", "Please open Service Desk in a Teams personal chat."
                ),
                update=update,
            )
            return
        if (
            tenant.get("id") != settings.tenant_id
            or conversation.get("tenantId", settings.tenant_id) != settings.tenant_id
        ):
            await send_card(
                context,
                cards.notice("Access help", "Please use your organization's Teams account."),
                update=update,
            )
            return
        sender = activity.get("from") or {}
        name = sender.get("name") or ""
        if not name:
            for member in activity.get("membersAdded") or []:
                if member.get("id") == sender.get("id"):
                    name = member.get("name") or ""
                    break
        await send_card(context, cards.welcome(name), update=update)

    @host.shutdown_handler
    async def shutdown() -> None:
        await runtime.close()

    async def process(context: Any, *, update: bool = False) -> None:
        activity = context.activity.model_dump(by_alias=True, exclude_none=True)
        try:
            action = parse_action(activity)
            prompt = (activity.get("text") or "").strip()
            command = commands.parse_command(prompt) if not action else None
            if action and action.verb in ("kb_form", "kb_search"):
                if action.verb == "kb_search" and not action.kb_query.strip():
                    raise DeskError("Please enter knowledge search words.")
                command = commands.Command("kb", action.kb_query.strip())
            if command:
                await handle_command(context, activity, command, update=update)
                return
            if action and action.verb in ("feedback_like", "feedback_dislike", "feedback_submit"):
                record_card_feedback(activity, action)
                await send_card(
                    context,
                    cards.notice(
                        "Feedback received",
                        "Thank you. No incident or case "
                        "was changed. You can also type Feedback: with comments.",
                    ),
                )
                return
            if (action and action.verb == "home") or (
                not action and (not prompt or _GREETING.fullmatch(prompt))
            ):
                await send_welcome(context, update=update)
                return
            rating = detect_feedback(prompt) if not action else None
            if rating:
                hosted_call_id()
                record_message_feedback(activity, rating)
                if feedback_only(prompt):
                    await send_card(
                        context,
                        cards.notice(
                            "Thanks for your feedback",
                            "Your feedback was received. No incident or troubleshooting case "
                            "was changed. Please do not include passwords in feedback.",
                        ),
                        update=update,
                    )
                    return
            async with runtime.turn(activity) as turn:
                turn.feedback_recorded = rating is not None
                if not action and prompt:
                    # SDK callbacks lack host context; this turn has already been authenticated.
                    turn.capture_feedback = lambda value: record_message_feedback(activity, value)
                if not action:
                    action = await match_case_option(turn, prompt)
                if action:
                    card, prompt = await dispatch(turn, action)
                    if card:
                        await send_card(context, card, update=update)
                        return
                final = ""
                async for kind, value in runtime.agent.ask(turn, prompt):
                    if kind == "final":
                        final = value
                if turn.knowledge_searched and not turn.outbox and final:
                    response_id = await deliver_knowledge(
                        context,
                        final,
                        turn.knowledge_sources,
                        update=update,
                    )
                    remember_reply(context, response_id)
                    return
                continuation = cards.follow_up(active_case=bool(await turn.active_case()))
                has_question = any(
                    action.get("verb") in ("case_option", "case_answer")
                    for card in turn.outbox
                    for action in card.get("actions", [])
                )
                if final and not has_question:
                    await send_card(context, cards.answer(final))
                for card in turn.outbox or [continuation]:
                    await send_card(context, card, update=update)
                    update = False
        except DeskError as exc:
            await send_card(context, cards.notice("Unable to continue", str(exc)), update=update)
        except Exception as exc:
            # SDK/transport failures are sanitized at the Activity boundary.
            logger.error(
                "Activity failed (%s); inspect correlated platform trace.", type(exc).__name__
            )
            await send_card(
                context,
                cards.notice(
                    "Service temporarily unavailable",
                    "I could not finish this request. If a change was being confirmed, "
                    "check the incident before repeating it.",
                ),
                update=update,
            )

    @app.activity("message")
    async def on_message(context: Any, _state: Any) -> None:
        await process(context)

    @app.activity("invoke")
    async def on_invoke(context: Any, _state: Any) -> None:
        activity = context.activity.model_dump(by_alias=True, exclude_none=True)
        if activity.get("name") == "message/submitAction":
            status, body = 200, {}
            try:
                hosted_call_id()
                rating, text, response_id = parse_native_feedback(activity)
                runtime.feedback.record(
                    activity,
                    settings.tenant_id,
                    source="native",
                    rating=rating,
                    text=text,
                    response_id=response_id,
                )
            except DeskError as exc:
                status, body = 400, {"message": str(exc)}
            except Exception as exc:
                logger.error("Feedback submission failed (%s).", type(exc).__name__)
                status, body = 503, {"message": "Feedback could not be recorded. Please retry."}
            await context.send_activity(
                Activity(
                    type=ActivityTypes.invoke_response,
                    value=InvokeResponse(status=status, body=body),
                )
            )
            return
        accepted = False
        try:
            action = parse_action(activity)
            if action and action.verb in ("feedback_like", "feedback_dislike", "feedback_submit"):
                try:
                    record_card_feedback(activity, action)
                except DeskError as exc:
                    response = {
                        "statusCode": 400,
                        "type": "application/vnd.microsoft.error",
                        "value": {"code": "BadRequest", "message": str(exc)},
                    }
                except Exception as exc:
                    logger.error("Card feedback failed (%s).", type(exc).__name__)
                    response = {
                        "statusCode": 503,
                        "type": "application/vnd.microsoft.error",
                        "value": {
                            "code": "ServiceUnavailable",
                            "message": "Feedback could not be recorded. Please retry.",
                        },
                    }
                else:
                    response = {
                        "statusCode": 200,
                        "type": "application/vnd.microsoft.activity.message",
                        "value": "Feedback received. Thank you.",
                    }
                await context.send_activity(
                    Activity(
                        type=ActivityTypes.invoke_response,
                        value=InvokeResponse(status=200, body=response),
                    )
                )
                return
        except DeskError as exc:
            response_card = cards.notice("Unsupported action", str(exc))
        else:
            accepted = len(runtime.pending) < 16
            response_card = cards.card(
                "Checking your request" if accepted else "Service Desk is busy",
                [
                    cards.text(
                        "Please wait. This card will update when your request completes."
                        if accepted
                        else "Please wait for your current requests to complete, then retry."
                    )
                ],
            )
        await context.send_activity(
            Activity(
                type=ActivityTypes.invoke_response,
                value=InvokeResponse(
                    status=200,
                    body={
                        "statusCode": 200,
                        "type": "application/vnd.microsoft.card.adaptive",
                        "value": cards.with_feedback(response_card),
                    },
                ),
            )
        )
        if not accepted:
            return

        async def continue_action(deferred_context: Any) -> None:
            await process(deferred_context, update=True)

        # The SDK closes the original turn's connector when the invoke returns.
        task = asyncio.create_task(
            context.adapter.continue_conversation_with_claims(
                context.identity,
                context.activity.model_copy(deep=True),
                continue_action,
            )
        )
        runtime.pending.add(task)

        def finished(completed: asyncio.Task[None]) -> None:
            runtime.pending.discard(completed)
            if not completed.cancelled() and (error := completed.exception()) is not None:
                logger.error("Deferred card delivery failed (%s).", type(error).__name__)

        task.add_done_callback(finished)

    @app.activity("conversationUpdate")
    async def welcome(context: Any, _state: Any) -> None:
        activity = context.activity.model_dump(by_alias=True, exclude_none=True)
        if activity.get("membersAdded"):
            await lifecycle_welcome(context)

    async def lifecycle_welcome(context: Any) -> None:
        activity = context.activity.model_dump(by_alias=True, exclude_none=True)
        conversation = activity.get("conversation") or {}
        if activity.get("channelId") != "msteams" or conversation.get("conversationType") != (
            "personal"
        ):
            return
        sender = activity.get("from") or {}
        key = (conversation.get("id", ""), sender.get("aadObjectId") or sender.get("id", ""))
        if key not in recent_welcomes:
            await send_welcome(context)
            recent_welcomes.append(key)

    @app.activity("installationUpdate")
    async def installed(context: Any, _state: Any) -> None:
        if context.activity.action in ("add", "add-upgrade"):
            await lifecycle_welcome(context)

    @app.error
    async def on_error(context: Any, error: Exception) -> None:
        logger.error("Teams delivery failed (%s).", type(error).__name__)
        await send_card(
            context,
            cards.notice(
                "Service temporarily unavailable",
                "Service Desk could not complete this request. "
                "Check existing tickets before retrying a change.",
            ),
        )

    return host


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings()
    except ValueError:
        raise SystemExit(
            "Invalid Service Desk configuration. See .env.example and README; "
            "secrets and invalid values are intentionally not printed."
        ) from None
    create_host(settings).run()


if __name__ == "__main__":
    run()
