"""Offline tests for the live smoke check (app/agents/smoke.py), using scripted models."""

import json
from collections.abc import Iterator

import pytest
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agents.demo_model import demo_study_model
from app.agents.smoke import (
    PROBE_TOOL,
    ToolCallCheckFailed,
    check_tool_calls,
    run_smoke_study,
)
from app.core.tracing import setup_tracing, shutdown_tracing
from tests.utils.tracing import SnapshotExporter, by_name

pytestmark = pytest.mark.anyio


def calls_the_tool(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    if len(messages) == 1:
        return ModelResponse(parts=[ToolCallPart(PROBE_TOOL, {"city": "Florence"})])
    return ModelResponse(parts=[TextPart("The code is PANTRY-8.")])


def writes_the_call_as_text(
    _messages: list[ModelMessage], _info: AgentInfo
) -> ModelResponse:
    """The reported Foundry failure: the tool call arrives as message text."""
    call = json.dumps({"name": PROBE_TOOL, "arguments": {"city": "Florence"}})
    return ModelResponse(parts=[TextPart(call)])


async def test_structured_tool_call_passes() -> None:
    await check_tool_calls(FunctionModel(calls_the_tool))


async def test_tool_call_written_as_text_fails() -> None:
    with pytest.raises(ToolCallCheckFailed, match=PROBE_TOOL):
        await check_tool_calls(FunctionModel(writes_the_call_as_text))


async def test_smoke_study_reports_result_and_usage() -> None:
    report = await run_smoke_study(demo_study_model())
    assert report.result.feasible
    assert report.requests == 2  # one simulation call, then the result
    assert report.input_tokens > 0
    assert report.output_tokens > 0
    assert report.trace_id == "0" * 32  # tracing is off


@pytest.fixture
def exporter() -> Iterator[SnapshotExporter]:
    exporter = SnapshotExporter()
    setup_tracing(endpoint=None, include_content=False, exporter=exporter)
    yield exporter
    shutdown_tracing()


async def test_smoke_study_trace_id_finds_the_run(exporter: SnapshotExporter) -> None:
    report = await run_smoke_study(demo_study_model())
    span = by_name(exporter.get_finished_spans(), "smoke_study")
    assert report.trace_id == format(span.context.trace_id, "032x")
    assert report.trace_id != "0" * 32
