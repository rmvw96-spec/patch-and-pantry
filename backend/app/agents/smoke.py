"""Live smoke check for a Foundry model (Phase 0, P0-1 and P0-2). Run by hand, never in CI.

``scripts/smoke_foundry.py`` calls this. It checks that the model returns real tool calls, then
runs one small study through the study agent and the real simulator equations, and reports the
trace ID and token usage so the run can be found in Phoenix and costed.
"""

from dataclasses import dataclass

from pydantic_ai import Agent, ModelResponse, ToolCallPart, models
from pydantic_ai.usage import UsageLimits

from app.agents.simulator import CASES, UnknownCaseError, solve_outlet_pressure
from app.agents.study_agent import (
    MAX_MODEL_REQUESTS,
    SimClient,
    SimOutput,
    StudyResult,
    study_agent,
)
from app.core.tracing import get_tracer

PROBE_PROMPT = "What is the pantry code for Florence, Alabama?"
PROBE_TOOL = "lookup_pantry_code"
SMOKE_PROMPT = "Optimize case B at 70 bar"

probe_agent = Agent(
    output_type=str,
    instructions=(
        "You do not know any pantry codes. Always call the lookup_pantry_code tool, "
        "then answer with the code it returns."
    ),
)


@probe_agent.tool_plain
def lookup_pantry_code(city: str) -> str:
    """Look up the pantry code for a city."""
    return f"PANTRY-{len(city)}"


class ToolCallCheckFailed(Exception):
    """The model answered in text instead of making a real tool call."""


async def check_tool_calls(model: models.Model | str) -> None:
    """Fail if the model does not make a structured call to the probe tool.

    One report describes DeepSeek on Foundry returning tool calls as message text; this catches
    that before a study run, where it would surface as a confusing agent failure.
    """
    result = await probe_agent.run(PROBE_PROMPT, model=model)
    called = any(
        isinstance(part, ToolCallPart) and part.tool_name == PROBE_TOOL
        for message in result.all_messages()
        if isinstance(message, ModelResponse)
        for part in message.parts
    )
    if not called:
        raise ToolCallCheckFailed(
            "The model answered without a structured tool call. Its reply began: "
            f"{result.output[:200]!r}"
        )


class DirectSimClient(SimClient):
    """The real simulator equations, called directly instead of through a Prefect flow."""

    async def run(self, case_id: str, inlet_bar: float) -> SimOutput:
        if case_id not in CASES:
            raise UnknownCaseError(f"unknown case_id {case_id!r}")
        outlet = solve_outlet_pressure(CASES[case_id], inlet_bar)
        return {"case_id": case_id, "inlet_bar": inlet_bar, "outlet_bar": outlet}


@dataclass
class SmokeReport:
    result: StudyResult
    trace_id: str
    requests: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int


async def run_smoke_study(
    model: models.Model | str, prompt: str = SMOKE_PROMPT
) -> SmokeReport:
    """Run one study and report its usage. The trace ID is all zeros when tracing is off."""
    with get_tracer().start_as_current_span("smoke_study") as span:
        run = await study_agent.run(
            prompt,
            deps=DirectSimClient(max_inlet_bar=80.0),
            model=model,
            usage_limits=UsageLimits(request_limit=MAX_MODEL_REQUESTS),
        )
        trace_id = format(span.get_span_context().trace_id, "032x")
    usage = run.usage
    return SmokeReport(
        result=run.output,
        trace_id=trace_id,
        requests=usage.requests,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
    )
