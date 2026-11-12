"""Model factory: turns a model spec (the SIM_MODEL setting) into a Pydantic AI model.

| spec                                 | model                                                 |
| ------------------------------------ | ----------------------------------------------------- |
| ``demo``                             | deterministic scripted model (no LLM, no key)         |
| ``test``                             | Pydantic AI's ``TestModel``                           |
| ``foundry:<canonical>``              | a Microsoft Foundry deployment, default name          |
| ``foundry:<canonical>@<deployment>`` | the same model under a custom deployment name         |

Foundry models are built lazily, when a run starts, so importing ``app`` never needs Foundry
settings or opens a network client. See docs/design.md, "Model, budget, and integration".
"""

from typing import Literal
from urllib.parse import urlparse

from pydantic_ai import models
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.azure import AzureProvider

from app.core.config import settings

# Canonical model name -> default Foundry deployment name. Adding a model (for example one that
# reads photos) is one line here.
FOUNDRY_MODELS: dict[str, str] = {
    "deepseek-v3.2": "DeepSeek-V3.2",
}

FOUNDRY_PREFIX = "foundry:"

Role = Literal["capture", "best_buy"]


class ModelConfigError(ValueError):
    """A model spec or the Foundry settings it needs are invalid."""


def _allowed_forms() -> str:
    canonical = ", ".join(sorted(FOUNDRY_MODELS))
    return (
        "expected 'demo', 'test', 'foundry:<model>', or 'foundry:<model>@<deployment>', "
        f"where <model> is one of: {canonical}"
    )


def parse_foundry_spec(spec: str) -> tuple[str, str]:
    """Split ``foundry:<canonical>[@<deployment>]`` into (canonical, deployment)."""
    rest = spec.removeprefix(FOUNDRY_PREFIX)
    canonical, _, deployment = rest.partition("@")
    if canonical not in FOUNDRY_MODELS:
        raise ModelConfigError(
            f"unknown model {canonical!r} in {spec!r}; {_allowed_forms()}"
        )
    if "@" in rest and not deployment:
        raise ModelConfigError(f"empty deployment name in {spec!r}; {_allowed_forms()}")
    return canonical, deployment or FOUNDRY_MODELS[canonical]


def _foundry_provider() -> AzureProvider:
    endpoint = settings.FOUNDRY_ENDPOINT
    api_key = settings.FOUNDRY_API_KEY
    if not endpoint or api_key is None:
        raise ModelConfigError(
            "Foundry models need FOUNDRY_ENDPOINT and FOUNDRY_API_KEY in the .env file"
        )
    url = urlparse(endpoint)
    if url.scheme != "https" or not url.path.rstrip("/").endswith("/openai/v1"):
        # Only a v1 endpoint makes AzureProvider use a plain OpenAI client that ignores the
        # OPENAI_API_VERSION environment variable, so nothing outside Settings shapes the request.
        raise ModelConfigError(
            "FOUNDRY_ENDPOINT must be the deployment's https v1 endpoint, ending in /openai/v1/"
        )
    # Endpoint and key are passed explicitly, so AzureProvider never falls back to the
    # AZURE_OPENAI_ENDPOINT or AZURE_OPENAI_API_KEY environment variables.
    return AzureProvider(azure_endpoint=endpoint, api_key=api_key.get_secret_value())


def build_foundry_model(spec: str) -> OpenAIChatModel:
    canonical, deployment = parse_foundry_spec(spec)
    provider = _foundry_provider()
    # Pydantic AI resolves a model's profile from its deployment name. Under a custom name,
    # DeepSeek-V3.2 gets a generic OpenAI profile that claims JSON-schema output. Passing
    # AzureProvider.model_profile(canonical) is not enough: that partial profile omits the
    # JSON-output flags, and a partial profile is merged, so the generic values survive. Instead,
    # resolve the full profile the default deployment name gets, and replace the custom name's
    # profile with it (the callable form replaces rather than merges). Building a model opens no
    # connection.
    profile = OpenAIChatModel(FOUNDRY_MODELS[canonical], provider=provider).profile
    return OpenAIChatModel(
        deployment, provider=provider, profile=lambda _resolved: profile.copy()
    )


def build_model(spec: str, role: Role) -> models.Model | str:
    """Build the model for one agent. ``role`` matters only for ``demo``, which scripts each agent."""
    if spec == "demo":
        from app.agents.demo_model import demo_best_buy_model, demo_capture_model

        return demo_capture_model() if role == "capture" else demo_best_buy_model()
    if spec == "test":
        return "test"
    if spec.startswith(FOUNDRY_PREFIX):
        return build_foundry_model(spec)
    raise ModelConfigError(f"unknown model spec {spec!r}; {_allowed_forms()}")
