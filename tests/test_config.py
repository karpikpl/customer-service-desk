from __future__ import annotations

import pytest
from pydantic import ValidationError

from service_desk.config import PrivateConfig, Settings, SnowPolicy

from .conftest import TENANT


def settings_values():
    return {
        "tenant_id": TENANT,
        "snow_url": "https://mock.service-now.com",
        "FOUNDRY_PROJECT_ENDPOINT": "https://test.services.ai.azure.com/api/projects/test",
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": "existing-model",
        "TOOLBOX_ENDPOINT": (
            "https://test.services.ai.azure.com/api/projects/test/toolboxes/desk/mcp?api-version=v1"
        ),
        "key_vault_url": "https://test.vault.azure.net",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://mock.service-now.com",
        "https://user:password@mock.service-now.com",
        "https://mock.service-now.com/path",
        "https://mock.service-now.com?redirect=elsewhere",
    ],
)
def test_snow_origin_is_validated(url):
    with pytest.raises(ValidationError):
        Settings(**{**settings_values(), "snow_url": url})


def test_no_cross_project_toolbox():
    with pytest.raises(ValidationError, match="Toolbox"):
        Settings(**{**settings_values(), "TOOLBOX_ENDPOINT": "https://other.example/mcp"})


def test_hosted_secrets_cannot_use_local_files(monkeypatch):
    monkeypatch.setenv("FOUNDRY_HOSTING_ENVIRONMENT", "hosted")
    with pytest.raises(ValidationError, match="Local secret"):
        Settings(**{**settings_values(), "local_config_file": "local-secrets/config.json"})


def test_write_and_knowledge_policy_defaults_are_closed():
    policy = SnowPolicy()
    assert not policy.allow_create
    assert not policy.allow_comments
    assert not policy.resolve_from
    assert not policy.reopen_from
    assert not policy.knowledge_audience_verified


@pytest.mark.parametrize(
    "path",
    ["//evil.example", "https://evil.example", "/sp?redirect=evil", "/../login", "/%2f", "/sp\n"],
)
def test_portal_path_cannot_override_trusted_origin(path):
    with pytest.raises(ValidationError):
        Settings(**{**settings_values(), "snow_portal_path": path})


def test_duplicate_user_mapping_rejected(mapping):
    with pytest.raises(ValidationError, match="Duplicate"):
        PrivateConfig(
            credentials={"username": "x", "password": "y"},
            identities=mapping + mapping,
            policy=SnowPolicy(),
        )
