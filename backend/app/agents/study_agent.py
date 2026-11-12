"""
Dummy study agent: a stand-in for an agentic "describe a problem -> simulate -> report" flow.

Practice code for a portfolio project. Mirrors the shape of the stack:
Pydantic AI agents on Microsoft Foundry, a simulator behind a dependency.
"""

from dataclasses import dataclass
from typing import TypedDict

from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry, RunContext, models
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from app.agents.models import build_model
from app.core.config import settings

# The agents have no model of their own: each run builds one from settings.SIM_MODEL when it
# starts (app/agents/models.py), so importing this module needs no model settings. Tests replace
# the model with Agent.override, which takes precedence over the model passed to a run.


# Hard cap per study, so a looping agent cannot burn budget forever
MAX_MODEL_REQUESTS = 8


class StudyResult(BaseModel):
    objective: float
    feasible: bool
    review: str | None = None


class SimOutput(TypedDict):
    case_id: str
    inlet_bar: float
    outlet_bar: float


@dataclass
class SimCall:
    case_id: str
    inlet_bar: float


class SimClient:
    """Dependency the agent uses to reach the simulator. In production this would submit a Prefect flow."""

    def __init__(self, max_inlet_bar: float) -> None:
        self.max_inlet_bar = max_inlet_bar

    async def run(self, case_id: str, inlet_bar: float) -> SimOutput:
        raise NotImplementedError("production client submits a simulation job")


class StudyFailed(Exception):
    """User-facing failure with a stable reason code the API layer maps to a message."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


reviewer_agent = Agent(
    output_type=str,
    instructions="Review a simulation summary for physical plausibility in one sentence.",
)

study_agent = Agent(
    deps_type=SimClient,
    output_type=StudyResult,
    retries=2,
    instructions=(
        "Run simulations for the user's case. Keep inlet pressure within the allowed limit. "
        "Optionally ask for a review, then report the result."
    ),
)


@study_agent.tool
async def run_simulation(
    ctx: RunContext[SimClient], case_id: str, inlet_bar: float
) -> SimOutput:
    """Run the simulator for one case at a given inlet pressure in bar."""
    if inlet_bar > ctx.deps.max_inlet_bar:
        # ModelRetry sends this message back to the model so it can choose a safer value.
        raise ModelRetry(
            f"inlet_bar {inlet_bar} exceeds the limit of {ctx.deps.max_inlet_bar} bar; choose a lower value"
        )
    try:
        return await ctx.deps.run(case_id, inlet_bar)
    except ValueError as exc:
        # Bad input (for example an unknown case_id): tell the model so it can correct itself.
        raise ModelRetry(str(exc)) from exc


@study_agent.tool
async def request_review(ctx: RunContext[SimClient], summary: str) -> str:
    """Ask the reviewer agent to sanity-check a result summary."""
    # usage=ctx.usage makes the sub-agent's requests count toward the same usage limits.
    result = await reviewer_agent.run(
        summary, usage=ctx.usage, model=build_model(settings.SIM_MODEL, role="reviewer")
    )
    return result.output


async def run_study(
    prompt: str, sim: SimClient, model: models.Model | str | None = None
) -> StudyResult:
    """Production entry point, called by the API handler or a Prefect task.

    ``model`` replaces the configured model for the study agent (API tests inject one); by
    default it is built from settings.SIM_MODEL.
    """
    if model is None:
        model = build_model(settings.SIM_MODEL, role="study")
    try:
        result = await study_agent.run(
            prompt,
            deps=sim,
            model=model,
            usage_limits=UsageLimits(request_limit=MAX_MODEL_REQUESTS),
        )
    except UsageLimitExceeded as exc:
        raise StudyFailed(
            "too_many_steps",
            "The study took too many steps and was stopped. Try a narrower question.",
        ) from exc
    except UnexpectedModelBehavior as exc:
        raise StudyFailed(
            "invalid_output",
            "The assistant could not produce a valid result. Please try again.",
        ) from exc
    return result.output
