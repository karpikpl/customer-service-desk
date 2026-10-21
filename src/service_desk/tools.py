"""Only these application tools are visible to the model; none executes a write."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from copilot import Tool, ToolInvocation, ToolResult
from pydantic import Field, ValidationError, model_validator

from service_desk import cards
from service_desk.actions import Actions
from service_desk.feedback import FeedbackParams, FeedbackRating
from service_desk.models import (
    CaseOption,
    CaseProgress,
    DeskError,
    DraftRequest,
    IncidentNumber,
    KnowledgeSource,
    Model,
    Principal,
)
from service_desk.servicenow import SnowClient
from service_desk.state import State
from service_desk.toolbox import SkillCatalog, ToolboxError


class ListParams(Model):
    page: int = Field(default=0, ge=0, le=1000)


class GetParams(Model):
    number: IncidentNumber


class SearchParams(Model):
    query: str = Field(
        min_length=2,
        max_length=120,
        description=(
            "A short contiguous article-title fragment, such as 'printer' or 'Outlook'. "
            "This is literal title matching, not full-text or keyword-AND search. "
            "Do not combine alternative paths such as 'remote in place' in one query."
        ),
    )


class SkillParams(Model):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    start_new_case: bool = False


class StepParams(Model):
    title: str = Field(min_length=1, max_length=120)
    step: str = Field(min_length=1, max_length=2000)
    summary: str = Field(min_length=1, max_length=4000)
    interaction: Literal["outcome", "information"] = "outcome"


class OptionsParams(Model):
    title: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=1000)
    summary: str = Field(min_length=1, max_length=4000)
    options: list[CaseOption] = Field(min_length=2, max_length=3)

    @model_validator(mode="after")
    def unique_options(self) -> OptionsParams:
        if len({option.id for option in self.options}) != len(self.options):
            raise ValueError("Option identifiers must be unique.")
        if len({option.label.strip().casefold() for option in self.options}) != len(self.options):
            raise ValueError("Option labels must be distinct.")
        if any(not option.label.strip() for option in self.options):
            raise ValueError("Option labels must not be blank.")
        return self


@dataclass
class ToolContext:
    principal: Principal
    snow: SnowClient
    state: State
    actions: Actions
    toolbox: SkillCatalog
    call_id: str
    outbox: list[cards.Card] = field(default_factory=list)
    portal_path: str = "/sp"
    capture_feedback: Callable[[FeedbackRating], None] | None = None
    feedback_recorded: bool = False
    knowledge_searched: bool = False
    knowledge_sources: list[KnowledgeSource] = field(default_factory=list)

    @property
    def portal_url(self) -> str:
        return self.snow.url + self.portal_path

    @property
    def case_key(self) -> str:
        return self.principal.scope + ":case"

    async def active_case(self) -> CaseProgress | None:
        item = await self.state.get(self.case_key)
        return CaseProgress.model_validate(item.value) if item and item.value else None

    async def save_case(self, progress: CaseProgress) -> None:
        item = await self.state.get(self.case_key)
        if item:
            await self.state.replace(self.case_key, progress.model_dump(), item.etag)
        else:
            await self.state.create(self.case_key, progress.model_dump())


def _tool(
    name: str,
    description: str,
    params: type[Model],
    handler: Callable[[Any], Awaitable[Any]],
) -> Tool:
    async def invoke(invocation: ToolInvocation) -> ToolResult:
        try:
            parsed = params.model_validate(invocation.arguments or {})
            result = await handler(parsed)
            return ToolResult(text_result_for_llm=json.dumps(result), result_type="success")
        except ValidationError:
            return ToolResult(
                text_result_for_llm="Invalid tool input. Use only the advertised fields.",
                result_type="failure",
            )
        except (DeskError, ToolboxError) as exc:
            return ToolResult(text_result_for_llm=str(exc), result_type="failure")

    return Tool(
        name=name,
        description=description,
        handler=invoke,
        parameters=params.model_json_schema(),
        skip_permission=True,
        defer="never",
    )


def build_tools(context: ToolContext) -> list[Tool]:
    async def list_incidents(params: ListParams) -> Any:
        items = await context.snow.list_incidents(context.principal, params.page)
        context.outbox.append(cards.incidents(items, params.page))
        return [item.model_dump(exclude={"sys_id", "caller_id", "version"}) for item in items]

    async def get_incident(params: GetParams) -> Any:
        item = await context.snow.get_incident(context.principal, params.number)
        context.outbox.append(cards.detail(item, context.snow.policy, context.portal_url))
        return item.model_dump(exclude={"sys_id", "caller_id", "version"})

    async def prepare(params: DraftRequest) -> Any:
        draft = await context.actions.prepare(context.principal, params)
        context.outbox.append(cards.review(draft))
        return {
            "status": "review_required",
            "message": "A review card is ready. No write was made.",
        }

    async def search(params: SearchParams) -> Any:
        context.knowledge_searched = True
        context.knowledge_sources = []
        progress = await context.active_case() or CaseProgress()
        try:
            results = await context.snow.search_knowledge(params.query)
        except DeskError:
            await context.save_case(
                progress.model_copy(
                    update={"knowledge_status": "unavailable", "knowledge_sources": []}
                )
            )
            raise
        sources = [
            KnowledgeSource(number=article.number, title=article.title, url=article.url)
            for article in results
        ]
        context.knowledge_sources = sources
        await context.save_case(
            progress.model_copy(
                update={
                    "knowledge_status": "available" if results else "not_found",
                    "knowledge_sources": sources,
                }
            )
        )
        return [article.model_dump() for article in results]

    async def load_skill(params: SkillParams) -> Any:
        existing = await context.active_case()
        if existing and existing.skill_name and not params.start_new_case:
            if existing.skill_name != params.name:
                raise DeskError("Finish or reset the current case before starting another skill.")
            return {
                "name": existing.skill_name,
                "instructions": existing.skill_content,
                "summary": existing.summary,
                "previous_step": existing.step,
                "outcome": existing.outcome,
                "digest": existing.skill_digest,
                "knowledge_status": existing.knowledge_status,
                "options": [option.model_dump() for option in existing.options],
                "selected_option": (
                    existing.selected_option.model_dump() if existing.selected_option else None
                ),
            }
        skill = await context.toolbox.load_skill(params.name, context.call_id)
        progress = existing if existing and not params.start_new_case else CaseProgress()
        updated = progress.model_copy(
            update={
                "skill_name": skill.name,
                "skill_digest": skill.digest,
                "skill_content": skill.content,
                "knowledge_status": "unchecked",
                "knowledge_sources": [],
            }
        )
        await context.save_case(updated)
        return {
            "name": skill.name,
            "instructions": skill.content,
            "digest": skill.digest,
            "summary": updated.summary,
            "selected_option": (
                updated.selected_option.model_dump() if updated.selected_option else None
            ),
        }

    async def show_step(params: StepParams) -> Any:
        progress = await context.active_case() or CaseProgress()
        if params.interaction == "outcome" and not progress.skill_name:
            raise DeskError("Load the relevant Toolbox case skill before offering a managed step.")
        if progress.options:
            raise DeskError("Wait for the user's choice before offering the next step.")
        if params.interaction == "outcome" and progress.knowledge_status == "unchecked":
            raise DeskError(
                "Search approved knowledge for this issue before offering troubleshooting "
                "instructions. If unavailable, explicitly label the step as general guidance."
            )
        updated = progress.model_copy(
            update={
                "summary": params.summary,
                "step": params.step,
                "step_token": uuid.uuid4().hex,
                "outcome": "",
                "interaction": params.interaction,
            }
        )
        await context.save_case(updated)
        if params.interaction == "information":
            context.outbox.append(
                cards.case_question(params.title, params.step, updated.step_token)
            )
        else:
            source = (
                "; ".join(f"{item.number}: {item.title}" for item in progress.knowledge_sources)
                if progress.knowledge_status == "available"
                else "General guidance (no matching approved article)"
                if progress.knowledge_status == "not_found"
                else "General guidance (approved knowledge unavailable)"
            )
            context.outbox.append(
                cards.troubleshooting(
                    params.title,
                    params.step,
                    source,
                    updated.step_token,
                    allow_create=context.snow.policy.allow_create,
                    sources=progress.knowledge_sources,
                )
            )
        return {"status": "step_displayed"}

    async def show_options(params: OptionsParams) -> Any:
        progress = await context.active_case() or CaseProgress()
        updated = progress.model_copy(
            update={
                "summary": params.summary,
                "step": params.question,
                "step_token": uuid.uuid4().hex,
                "outcome": "",
                "options": params.options,
                "selected_option": None,
                "interaction": "choice",
            }
        )
        await context.save_case(updated)
        context.outbox.append(
            cards.case_options(params.title, params.question, params.options, updated.step_token)
        )
        return {"status": "options_displayed", "message": "Wait for the user's selection."}

    async def record_feedback(params: FeedbackParams) -> Any:
        if not context.feedback_recorded:
            if context.capture_feedback is None:
                raise DeskError("Feedback capture is unavailable. Please try again later.")
            context.capture_feedback(params.rating)
            context.feedback_recorded = True
        return {
            "status": "received",
            "message": "Feedback received; no ticket or case was changed.",
        }

    return [
        _tool(
            "record_user_feedback",
            "Record feedback about the assistant, its responses/cards, or support experience. "
            "Not for printer symptoms or troubleshooting outcomes. Classify the rating only; "
            "the host captures the actual CURRENT user message, not model-written text. "
            "Does not post a ServiceNow comment or advance a case.",
            FeedbackParams,
            record_feedback,
        ),
        _tool(
            "list_my_incidents",
            "List my incidents and display the incident list card.",
            ListParams,
            list_incidents,
        ),
        _tool(
            "get_my_incident",
            "Read an owned incident and display its detail card.",
            GetParams,
            get_incident,
        ),
        _tool(
            "prepare_incident_change",
            "Prepare a review card. Does NOT write to ServiceNow.",
            DraftRequest,
            prepare,
        ),
        _tool(
            "search_approved_knowledge",
            "Search approved employee knowledge BEFORE answering support questions or giving "
            "troubleshooting instructions. Display actual source links; cite only returned "
            "articles. An unavailable search does not authorize inventing a KB.",
            SearchParams,
            search,
        ),
        _tool(
            "load_case_skill",
            "Load an advertised Toolbox skill before following its procedure. "
            "For an explicitly different issue, start_new_case=true replaces the old "
            "troubleshooting case, not any incident or pending ticket review.",
            SkillParams,
            load_skill,
        ),
        _tool(
            "show_case_options",
            "Display two or three clickable choices for ANY support decision, including "
            "ticket follow-up versus triage, email clients, and troubleshooting paths. "
            "No skill is required just to offer choices. Use this instead of asking users "
            "to reply 1, 2, or 3. Save accurate context and wait for a selection. "
            "Selection does not authorize or execute a ticket change.",
            OptionsParams,
            show_options,
        ),
        _tool(
            "show_case_step",
            "Save context and display one question or troubleshooting check. "
            "Use interaction=information for missing facts: a text field with Continue / "
            "I don't know. Use interaction=outcome only for a check the user can try: "
            "That worked / Still not working. Outcome checks require a loaded skill "
            "and an approved-knowledge search attempt first.",
            StepParams,
            show_step,
        ),
    ]
