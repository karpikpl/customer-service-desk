"""Fail-closed configuration; deployment secrets never enter model context."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit
from uuid import UUID

from azure.core.credentials_async import AsyncTokenCredential
from azure.keyvault.secrets.aio import SecretClient
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_desk.models import Model, SysId


def https_url(value: str) -> str:
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError("An HTTPS URL without credentials or a fragment is required.")
    return value.rstrip("/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DESK_", env_file=".env", extra="ignore")
    tenant_id: str
    snow_url: str
    snow_portal_path: str = Field(default="/sp", pattern=r"^/(?:[A-Za-z0-9_-]+/?)*$")
    feedback_connection_string: SecretStr = SecretStr("")
    project_endpoint: str = Field(validation_alias="FOUNDRY_PROJECT_ENDPOINT")
    model_deployment: str = Field(validation_alias="AZURE_AI_MODEL_DEPLOYMENT_NAME")
    toolbox_endpoint: str = Field(validation_alias="TOOLBOX_ENDPOINT")
    key_vault_url: str = ""
    credentials_secret: str = "service-desk-snow-credentials"
    identity_secret: str = "service-desk-identity-map"
    policy_secret: str = "service-desk-snow-policy"
    local_config_file: Path | None = None
    confirmation_ttl: int = Field(default=600, ge=60, le=3600)

    @field_validator("tenant_id")
    @classmethod
    def tenant_uuid(cls, value: str) -> str:
        return str(UUID(value))

    @field_validator("snow_url", "project_endpoint", "toolbox_endpoint")
    @classmethod
    def validate_https(cls, value: str) -> str:
        return https_url(value)

    @model_validator(mode="after")
    def secret_source(self) -> Self:
        snow = urlsplit(self.snow_url)
        if snow.path not in ("", "/") or snow.query:
            raise ValueError("DESK_SNOW_URL must be the instance origin only.")
        if self.local_config_file:
            if os.environ.get("FOUNDRY_HOSTING_ENVIRONMENT"):
                raise ValueError("Local secret files are forbidden in a hosted deployment.")
        elif not self.key_vault_url:
            raise ValueError("Configure DESK_KEY_VAULT_URL or a local-only config file.")
        if self.key_vault_url:
            https_url(self.key_vault_url)
        project = self.project_endpoint + "/toolboxes/"
        if not self.toolbox_endpoint.startswith(project):
            raise ValueError("Toolbox must belong to the configured Foundry project.")
        return self


class Credentials(Model):
    username: SecretStr
    password: SecretStr


class Mapping(Model):
    tenant_id: str
    object_id: str
    caller_id: SysId

    @field_validator("tenant_id", "object_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        return str(UUID(value))


class SnowPolicy(Model):
    """Instance choices are supplied by an administrator, never inferred by the model."""

    states: dict[str, str] = Field(default_factory=dict)
    allow_create: bool = False
    allow_comments: bool = False
    resolve_from: list[str] = Field(default_factory=list)
    resolved_state: str = ""
    reopen_from: list[str] = Field(default_factory=list)
    reopened_state: str = ""
    resolution_codes: list[str] = Field(default_factory=list)
    employee_knowledge_bases: list[SysId] = Field(default_factory=list)
    employee_knowledge_articles: list[SysId] = Field(default_factory=list)
    knowledge_audience_verified: bool = False

    @model_validator(mode="after")
    def transitions(self) -> Self:
        if self.resolve_from and not self.resolution_codes:
            raise ValueError("Resolution transitions require configured resolution codes.")
        for source, target in (
            (self.resolve_from, self.resolved_state),
            (self.reopen_from, self.reopened_state),
        ):
            if source and (not target or target not in self.states):
                raise ValueError("Configured transitions require a known target state.")
            if any(state not in self.states for state in source):
                raise ValueError("Transition source states must exist in states.")
        return self


class PrivateConfig(Model):
    credentials: Credentials
    identities: list[Mapping]
    policy: SnowPolicy

    @model_validator(mode="after")
    def unique_identities(self) -> Self:
        keys = [(item.tenant_id, item.object_id) for item in self.identities]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate identity mappings are not allowed.")
        return self


async def load_private(settings: Settings, credential: AsyncTokenCredential) -> PrivateConfig:
    if settings.local_config_file:
        path = settings.local_config_file
        if path.stat().st_mode & 0o077:
            raise ValueError("The local secret configuration must have mode 0600.")
        return PrivateConfig.model_validate_json(path.read_text())
    async with SecretClient(settings.key_vault_url, credential) as client:
        credentials = (await client.get_secret(settings.credentials_secret)).value
        identities = (await client.get_secret(settings.identity_secret)).value
        policy = (await client.get_secret(settings.policy_secret)).value
    if not credentials or not identities or not policy:
        raise ValueError("A required Service Desk configuration secret is empty.")
    return PrivateConfig.model_validate(
        {
            "credentials": json.loads(credentials),
            "identities": json.loads(identities),
            "policy": json.loads(policy),
        }
    )
