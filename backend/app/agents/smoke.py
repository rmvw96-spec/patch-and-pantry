"""Live smoke check for a Foundry model (Phase 0, P0-1 and P0-2). Run by hand, never in CI.

``scripts/smoke_foundry.py`` calls this. It checks that the model returns real tool calls, then
has the capture agent read a short typed list (no database needed), and reports the trace ID and
token usage so the run can be found in Phoenix and costed.
"""

from dataclasses import dataclass

from pydantic_ai import Agent, ModelResponse, ToolCallPart, models
from pydantic_ai.usage import UsageLimits

from app.agents.capture_agent import (
    MAX_MODEL_REQUESTS,
    Extraction,
    capture_agent,
    numbered_lines,
)
from app.core.tracing import get_tracer

PROBE_PROMPT = "What is the pantry code for Florence, Alabama?"
PROBE_TOOL = "lookup_pantry_code"
SMOKE_LIST = "Kroger\nonions 3 lb bag 2.99\nkale bunch 2.50\ntomatoes 3 for $5"

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
    that before a capture run, where it would surface as a confusing agent failure.
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


@dataclass
class SmokeReport:
    result: Extraction
    trace_id: str
    requests: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int


async def run_smoke_capture(
    model: models.Model | str, text: str = SMOKE_LIST
) -> SmokeReport:
    """Read one typed list and report usage. The trace ID is all zeros when tracing is off."""
    lines = [line for line in text.splitlines() if line.strip()]
    with get_tracer().start_as_current_span("smoke_capture") as span:
        run = await capture_agent.run(
            numbered_lines(lines),
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
