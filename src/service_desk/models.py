"""Validated domain records shared by tools, cards, and integrations."""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SysId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
IncidentNumber = Annotated[str, Field(pattern=r"^INC[0-9]{1,20}$")]
Operation = Literal["create", "comment", "resolve", "reopen"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Principal(Model):
    tenant_id: str
    object_id: str
    caller_id: SysId
    conversation_id: str
    display_name: str = Field(default="", max_length=80)
    conversation_generation: str = Field(default="", pattern=r"^(?:[a-f0-9]{32})?$")

    @property
    def identity_scope(self) -> str:
        source = "\0".join((self.tenant_id, self.object_id, self.conversation_id))
        return hashlib.sha256(source.encode()).hexdigest()

    @property
    def scope(self) -> str:
        if not self.conversation_generation:
            return self.identity_scope
        source = "\0".join((self.tenant_id, self.object_id, self.conversation_id))
        source += "\0" + self.conversation_generation
        return hashlib.sha256(source.encode()).hexdigest()


class Incident(Model):
    sys_id: SysId
    number: IncidentNumber
    caller_id: SysId
    short_description: str
    description: str = ""
    state: str
    state_label: str
    priority: str = ""
    updated: str
    version: str
    comments: str = ""


class Article(Model):
    number: str
    title: str
    text: str
    url: str


class KnowledgeSource(Model):
    number: str
    title: str
    url: str


class DraftRequest(Model):
    operation: Operation
    incident_number: IncidentNumber | None = None
    short_description: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=4000)
    category: str = Field(default="", max_length=40)
    subcategory: str = Field(default="", max_length=40)
    impact: Literal["", "1", "2", "3"] = ""
    urgency: Literal["", "1", "2", "3"] = ""
    reported_location: str = Field(default="", max_length=160)
    device: str = Field(default="", max_length=160)
    business_impact: str = Field(default="", max_length=500)
    attempted_steps: str = Field(default="", max_length=1000)
    comment: str = Field(default="", max_length=4000)
    resolution_code: str = Field(default="", max_length=120)
    resolution_notes: str = Field(default="", max_length=4000)


class Draft(Model):
    id: str
    scope: str
    caller_id: SysId
    request: DraftRequest
    target_id: SysId | None = None
    target_version: str | None = None
    payload: dict[str, str]
    expires_at: float
    status: Literal[
        "pending", "executing", "cancelled", "expired", "stale", "succeeded", "outcome_unknown"
    ] = "pending"
    result: Incident | None = None


class CaseOption(Model):
    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=40)
    label: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=240)


class CaseProgress(Model):
    skill_name: str = ""
    skill_digest: str = ""
    skill_content: str = Field(default="", max_length=64000)
    summary: str = Field(default="", max_length=4000)
    step: str = Field(default="", max_length=2000)
    outcome: str = Field(default="", max_length=4000)
    step_token: str = Field(default_factory=lambda: uuid.uuid4().hex)
    options: list[CaseOption] = Field(default_factory=list, max_length=3)
    selected_option: CaseOption | None = None
    interaction: Literal["outcome", "information", "choice"] = "outcome"
    knowledge_status: Literal["unchecked", "available", "not_found", "unavailable"] = "unchecked"
    knowledge_sources: list[KnowledgeSource] = Field(default_factory=list, max_length=3)


class DeskError(Exception):
    """An actionable, safe message suitable for the user."""
