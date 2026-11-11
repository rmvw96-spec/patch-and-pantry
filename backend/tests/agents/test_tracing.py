"""Tracing: the study agent's OpenTelemetry spans, as Phoenix would receive them.

Spans go to an in-memory exporter instead of Phoenix, and models are scripted FunctionModels, so
these tests need neither a collector nor an LLM.
"""

from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from pydantic_ai import Agent, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agents.study_agent import reviewer_agent, run_study, study_agent
from app.core.tracing import get_tracer, setup_tracing, shutdown_tracing
from tests.agents.conftest import FakeSimClient
from tests.utils.tracing import SnapshotExporter, attribute_text, by_name, parent_of

pytestmark = pytest.mark.anyio

PROMPT = "Optimize case A (prompt-marker-7f3a)"
SUMMARY = "Case A delivers 54 bar (summary-marker-91c2)"


# ---------- helpers ----------


def simulate_review_then_report(
    messages: list[ModelMessage], info: AgentInfo
) -> ModelResponse:
    """Study model: run one simulation, ask for a review, then report."""
    step = len(messages)
    if step == 1:
        return ModelResponse(
            parts=[ToolCallPart("run_simulation", {"case_id": "A", "inlet_bar": 60.0})]
        )
    if step == 3:
        return ModelResponse(
            parts=[ToolCallPart("request_review", {"summary": SUMMARY})]
        )
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name, {"objective": 54.0, "feasible": True}
            )
        ]
    )


def approve(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("Plausible.")])


async def run_traced_study() -> None:
    with (
        study_agent.override(model=FunctionModel(simulate_review_then_report)),
        reviewer_agent.override(model=FunctionModel(approve)),
    ):
        await run_study(PROMPT, FakeSimClient())


# ---------- fixtures ----------


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    exporter = SnapshotExporter()
    setup_tracing(endpoint=None, include_content=True, exporter=exporter)
    yield exporter
    # Instrumentation is global to all agents, so it must not leak into other tests.
    shutdown_tracing()


# ---------- setup ----------


def test_tracing_is_off_without_an_endpoint() -> None:
    assert setup_tracing(endpoint=None, include_content=True) is None
    assert isinstance(get_tracer(), trace.NoOpTracer)


def test_endpoint_configures_a_batching_otlp_exporter() -> None:
    provider = setup_tracing(
        endpoint="http://localhost:6006/v1/traces", include_content=True
    )
    try:
        assert provider is not None
        processors = provider._active_span_processor._span_processors
        assert any(isinstance(p, BatchSpanProcessor) for p in processors)
        assert provider.resource.attributes["service.name"] == "patch-and-pantry"
        assert not isinstance(get_tracer(), trace.NoOpTracer)
    finally:
        shutdown_tracing()
    assert isinstance(get_tracer(), trace.NoOpTracer)


async def test_shutdown_stops_tracing_and_uninstruments_agents() -> None:
    exporter = SnapshotExporter()
    setup_tracing(endpoint=None, include_content=True, exporter=exporter)
    shutdown_tracing()
    await run_traced_study()
    assert exporter.get_finished_spans() == ()
    # Pydantic AI has no public getter for the global default set by Agent.instrument_all.
    assert Agent._instrument_default is False


# ---------- agent spans ----------


async def test_agent_run_produces_agent_llm_and_tool_spans(
    exporter: InMemorySpanExporter,
) -> None:
    await run_traced_study()
    spans = exporter.get_finished_spans()

    root = by_name(spans, "invoke_agent study_agent")
    assert root.parent is None
    # OpenInference span kinds are what Phoenix uses to label and filter spans.
    assert root.attributes["openinference.span.kind"] == "AGENT"
    for tool in ("run_simulation", "request_review"):
        span = by_name(spans, f"execute_tool {tool}")
        assert span.attributes["openinference.span.kind"] == "TOOL"
        assert parent_of(spans, span) is root
    llm_spans = [
        s for s in spans if s.attributes.get("openinference.span.kind") == "LLM"
    ]
    assert len(llm_spans) == 4  # three study-agent turns and one reviewer turn


async def test_reviewer_run_nests_under_the_review_tool(
    exporter: InMemorySpanExporter,
) -> None:
    await run_traced_study()
    spans = exporter.get_finished_spans()

    reviewer = by_name(spans, "invoke_agent reviewer_agent")
    assert parent_of(spans, reviewer).name == "execute_tool request_review"


async def test_content_is_recorded_by_default(exporter: InMemorySpanExporter) -> None:
    await run_traced_study()
    text = attribute_text(exporter.get_finished_spans())
    assert "prompt-marker-7f3a" in text
    assert "summary-marker-91c2" in text


async def test_content_can_be_left_out_of_spans() -> None:
    exporter = SnapshotExporter()
    setup_tracing(endpoint=None, include_content=False, exporter=exporter)
    try:
        await run_traced_study()
    finally:
        shutdown_tracing()
    spans = exporter.get_finished_spans()

    assert by_name(spans, "invoke_agent study_agent")  # still traced
    text = attribute_text(spans)
    assert "prompt-marker-7f3a" not in text
    assert "summary-marker-91c2" not in text
