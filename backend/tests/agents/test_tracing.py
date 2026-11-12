"""Tracing: the agents' OpenTelemetry spans, as Phoenix would receive them.

Spans go to an in-memory exporter instead of Phoenix, and models are scripted FunctionModels, so
these tests need neither a collector nor an LLM. The two agents run on their own here (no
database): the capture agent reads a list, and the best-buy agent explains a table, first with a
number the table lacks (sent back) and then correctly.
"""

from collections.abc import Iterator
from datetime import date

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from pydantic_ai import Agent, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agents.best_buy_agent import explain_best_buy
from app.agents.capture_agent import extract_lines
from app.core.tracing import get_tracer, setup_tracing, shutdown_tracing
from app.tools.best_buy import BestBuyTable
from tests.utils.tracing import SnapshotExporter, attribute_text, by_name, parent_of

pytestmark = pytest.mark.anyio

LINE = "onions 3 lb 2.99 (prompt-marker-7f3a)"
EXPLANATION = "There is no priced row yet (summary-marker-kiwi)."


# ---------- helpers ----------


def read_one_line(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    entry = {
        "line": 1,
        "is_price": True,
        "item_text": "onions",
        "quantity": 3,
        "unit": "lb",
        "price_text": "2.99",
    }
    return ModelResponse(
        parts=[ToolCallPart(info.output_tools[0].name, {"lines": [entry]})]
    )


def explain_after_one_retry() -> FunctionModel:
    replies = iter(["Save 42% this week!", EXPLANATION])

    def respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(next(replies))])

    return FunctionModel(respond)


async def run_traced_agents() -> None:
    await extract_lines([LINE], FunctionModel(read_one_line))
    await explain_best_buy(
        BestBuyTable(on=date(2026, 11, 11)), explain_after_one_retry()
    )


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
    await run_traced_agents()
    assert exporter.get_finished_spans() == ()
    # Pydantic AI has no public getter for the global default set by Agent.instrument_all.
    assert Agent._instrument_default is False


# ---------- agent spans ----------


async def test_each_agent_run_is_an_agent_span_with_its_llm_calls(
    exporter: InMemorySpanExporter,
) -> None:
    await run_traced_agents()
    spans = exporter.get_finished_spans()

    for name, llm_calls in (("capture_agent", 1), ("best_buy_agent", 2)):
        root = by_name(spans, f"invoke_agent {name}")
        assert root.parent is None
        # OpenInference span kinds are what Phoenix uses to label and filter spans.
        assert root.attributes["openinference.span.kind"] == "AGENT"
        children = [
            s
            for s in spans
            if s.attributes.get("openinference.span.kind") == "LLM"
            and parent_of(spans, s) is root
        ]
        # The explanation's first answer had a number not in the table, so it was sent back.
        assert len(children) == llm_calls


async def test_an_outer_span_contains_the_agent_runs(
    exporter: InMemorySpanExporter,
) -> None:
    """A grocery analysis run's span becomes the root, so one run is one trace in Phoenix."""
    with get_tracer().start_as_current_span("outer") as outer:
        await run_traced_agents()
    spans = exporter.get_finished_spans()

    root = by_name(spans, "outer")
    for name in ("invoke_agent capture_agent", "invoke_agent best_buy_agent"):
        assert parent_of(spans, by_name(spans, name)) is root
    assert {s.context.trace_id for s in spans} == {outer.get_span_context().trace_id}


async def test_content_is_recorded_by_default(exporter: InMemorySpanExporter) -> None:
    await run_traced_agents()
    text = attribute_text(exporter.get_finished_spans())
    assert "prompt-marker-7f3a" in text
    assert "summary-marker-kiwi" in text


async def test_content_can_be_left_out_of_spans() -> None:
    exporter = SnapshotExporter()
    setup_tracing(endpoint=None, include_content=False, exporter=exporter)
    try:
        await run_traced_agents()
    finally:
        shutdown_tracing()
    spans = exporter.get_finished_spans()

    assert by_name(spans, "invoke_agent capture_agent")  # still traced
    text = attribute_text(spans)
    assert "prompt-marker-7f3a" not in text
    assert "summary-marker-kiwi" not in text
