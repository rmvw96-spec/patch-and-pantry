"""Tests for the model factory (app/agents/models.py). No test here makes a network request."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel

from app.agents.best_buy_agent import best_buy_agent
from app.agents.capture_agent import capture_agent
from app.agents.models import ModelConfigError, build_model
from app.core.config import settings

ENDPOINT = "https://example-resource.services.ai.azure.com/openai/v1/"
KEY = "settings-key-not-real"


@pytest.fixture
def foundry_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "FOUNDRY_ENDPOINT", ENDPOINT)
    monkeypatch.setattr(settings, "FOUNDRY_API_KEY", SecretStr(KEY))


def _foundry(spec: str) -> OpenAIChatModel:
    model = build_model(spec, role="capture")
    assert isinstance(model, OpenAIChatModel)
    return model


# ---------- Foundry models ----------


@pytest.mark.usefixtures("foundry_settings")
def test_deepseek_uses_default_deployment_and_its_profile() -> None:
    model = _foundry("foundry:deepseek-v3.2")
    assert model.model_name == "DeepSeek-V3.2"
    # The DeepSeek profile: no native JSON-schema output, so structured output uses tool calls.
    assert model.profile.get("supports_json_schema_output") is False


@pytest.mark.usefixtures("foundry_settings")
def test_custom_deployment_name_keeps_the_deepseek_profile() -> None:
    """Without the explicit profile, a custom name gets a generic OpenAI profile (JSON schema on)."""
    default = _foundry("foundry:deepseek-v3.2")
    custom = _foundry("foundry:deepseek-v3.2@my-custom-name")
    assert custom.model_name == "my-custom-name"
    assert custom.profile == default.profile
    assert custom.profile.get("supports_json_schema_output") is False


@pytest.mark.usefixtures("foundry_settings")
def test_settings_win_over_azure_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AzureProvider reads these variables when arguments are missing; the factory never lets it."""
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://wrong.example.com/openai/v1/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "env-key-not-real")
    monkeypatch.setenv("OPENAI_API_VERSION", "2024-10-21")
    model = _foundry("foundry:deepseek-v3.2")
    assert str(model.client.base_url) == ENDPOINT
    assert model.client.api_key == KEY
    # A v1 endpoint must not carry an api-version query parameter.
    assert "api-version" not in str(model.client.base_url)


@pytest.mark.parametrize(
    ("endpoint", "key"),
    [(None, KEY), (ENDPOINT, None), (None, None)],
)
def test_missing_foundry_settings_name_both_variables(
    monkeypatch: pytest.MonkeyPatch, endpoint: str | None, key: str | None
) -> None:
    monkeypatch.setattr(settings, "FOUNDRY_ENDPOINT", endpoint)
    monkeypatch.setattr(settings, "FOUNDRY_API_KEY", SecretStr(key) if key else None)
    with pytest.raises(ModelConfigError, match="FOUNDRY_ENDPOINT and FOUNDRY_API_KEY"):
        build_model("foundry:deepseek-v3.2", role="capture")


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example-resource.services.ai.azure.com/models",
        "https://example-resource.openai.azure.com/",
        "http://example-resource.services.ai.azure.com/openai/v1/",
    ],
)
def test_non_v1_endpoint_is_rejected(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.setattr(settings, "FOUNDRY_ENDPOINT", endpoint)
    monkeypatch.setattr(settings, "FOUNDRY_API_KEY", SecretStr(KEY))
    with pytest.raises(ModelConfigError, match="/openai/v1/"):
        build_model("foundry:deepseek-v3.2", role="capture")


# ---------- other specs ----------


def test_demo_scripts_each_agent() -> None:
    assert isinstance(build_model("demo", role="capture"), FunctionModel)
    assert isinstance(build_model("demo", role="best_buy"), FunctionModel)


def test_test_spec_is_pydantic_ai_test_model() -> None:
    assert build_model("test", role="capture") == "test"


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "azure:gpt-4o",  # the old default, which is no longer a valid spec
        "foundry:claude-sonnet-5-5",  # not deployed
        "foundry:deepseek-v3.2@",
        "foundry:",
    ],
)
def test_unknown_specs_raise_value_error(spec: str) -> None:
    with pytest.raises(ValueError, match="expected 'demo', 'test'"):
        build_model(spec, role="capture")


def test_default_model_is_demo() -> None:
    """Live models run only when SIM_MODEL is set explicitly."""
    assert type(settings).model_fields["SIM_MODEL"].default == "demo"


# ---------- sampling settings and lazy construction ----------


SAMPLING = {"temperature", "top_p", "top_k"}


@pytest.mark.usefixtures("foundry_settings")
def test_no_sampling_settings() -> None:
    """Neither the agents nor the Foundry model carry settings, so sampling stays at defaults."""
    for agent in (capture_agent, best_buy_agent):
        assert agent.model_settings is None
    settings_ = _foundry("foundry:deepseek-v3.2").settings
    assert settings_ is None or not SAMPLING & set(settings_)


def test_importing_the_app_needs_no_foundry_settings() -> None:
    """A Foundry SIM_MODEL with no endpoint or key must not break imports; runs build the model."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("FOUNDRY_", "AZURE_OPENAI_"))
    }
    env["SIM_MODEL"] = "foundry:deepseek-v3.2"
    backend = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.main, app.agents.capture_agent, app.grocery_analysis_jobs",
        ],
        cwd=backend,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_secrets_file_overrides_env_and_is_gitignored() -> None:
    """FOUNDRY_API_KEY belongs in .env.local: .env is committed, and the repository is public."""
    env_files = type(settings).model_config.get("env_file")
    assert env_files == (
        "../.env",
        "../.env.local",
    )  # later files override earlier ones
    repo = Path(__file__).resolve().parents[3]
    if not (repo / ".git").exists():
        pytest.skip("not a git checkout (for example, inside the backend container)")
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", ".env.local"], cwd=repo, check=False
    )
    assert ignored.returncode == 0, ".env.local must be listed in .gitignore"
