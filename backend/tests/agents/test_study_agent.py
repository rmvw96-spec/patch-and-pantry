"""Layer 2 of the test pyramid: agent logic with no live LLM.

These tests check internal code around the model (tools, deps, guards, retries, error handling, delegation).
They do not judge whether a real model makes good decisions; that belongs to the eval harness.
Every test runs offline in milliseconds, because conftest.py blocks real model requests.
"""

import pytest
from pydantic import SecretStr
from pydantic_ai import (
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
    capture_run_messages,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from app.agents.study_agent import (
    MAX_MODEL_REQUESTS,
    StudyFailed,
    StudyResult,
    reviewer_agent,
    run_study,
    study_agent,
)
from app.core.config import settings

pytestmark = pytest.mark.anyio


# ---------- helpers for scripting FunctionModel responses ----------


def call_tool(name: str, **args) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(name, args)])


def final_output(info: AgentInfo, **args) -> ModelResponse:
    # Structured output (StudyResult) is returned by calling the agent's output tool.
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])


def last_part(messages):
    return messages[-1].parts[-1]


def was_rejected(part) -> bool:
    """True when a tool raised ModelRetry. Pydantic AI 2.x reports it as a ToolReturnPart with
    outcome == "retried"; older releases used RetryPromptPart. Accept both."""
    return isinstance(part, RetryPromptPart) or (
        isinstance(part, ToolReturnPart) and getattr(part, "outcome", None) == "retried"
    )


# ---------- 1. the safety switch itself ----------


async def test_safety_switch_blocks_live_model_calls(fake_sim, monkeypatch):
    """With a live model configured, conftest's switch makes the request fail loudly instead."""
    # Fake Foundry settings so the model can be built; the switch must be what stops the request.
    monkeypatch.setattr(settings, "SIM_MODEL", "foundry:deepseek-v3.2")
    monkeypatch.setattr(
        settings, "FOUNDRY_ENDPOINT", "https://example.invalid/openai/v1/"
    )
    monkeypatch.setattr(settings, "FOUNDRY_API_KEY", SecretStr("test-key-not-real"))
    with pytest.raises(RuntimeError, match="ALLOW_MODEL_REQUESTS"):
        await run_study("Optimize case A", fake_sim)
    assert fake_sim.calls == []


# ---------- 2. TestModel: wiring checks ----------


async def test_wiring_with_test_model(fake_sim):
    """TestModel calls every tool with schema-valid (but arbitrary) args, then returns valid output.

    It also calls request_review, which runs the reviewer sub-agent, so the reviewer is overridden
    too. Both overrides sit in the test body, where they are visible and can't be silently dropped.
    """
    with (
        study_agent.override(model=TestModel()),
        reviewer_agent.override(
            model=TestModel(custom_output_text="Looks physically plausible.")
        ),
    ):
        result = await run_study("Optimize case A", fake_sim)

    assert len(fake_sim.calls) == 1  # run_simulation executed against the fake
    assert isinstance(result, StudyResult)  # output validated against the schema


async def test_test_model_can_target_one_tool(fake_sim):
    """call_tools narrows TestModel to specific tools, so this test needs no reviewer override."""
    with study_agent.override(model=TestModel(call_tools=["run_simulation"])):
        result = await run_study("Optimize case A", fake_sim)

    assert len(fake_sim.calls) == 1
    assert isinstance(result, StudyResult)


# ---------- 3. FunctionModel: script the branches involved ----------


def unsafe_then_safe(messages, info: AgentInfo) -> ModelResponse:
    """Decide from the last message, not from message counts, so the script survives refactors."""
    last = last_part(messages)
    if was_rejected(last):  # the tool rejected our value: back off
        return call_tool("run_simulation", case_id="A", inlet_bar=70.0)
    if isinstance(last, ToolReturnPart):  # simulation succeeded: report
        return final_output(info, objective=1.0, feasible=True)
    return call_tool(
        "run_simulation", case_id="A", inlet_bar=95.0
    )  # first turn: too high


async def test_unsafe_pressure_never_reaches_simulator(fake_sim):
    with study_agent.override(model=FunctionModel(unsafe_then_safe)):
        result = await run_study("Optimize case A", fake_sim)

    assert [c.inlet_bar for c in fake_sim.calls] == [
        70.0
    ]  # 95 bar was rejected before the simulator
    assert result.feasible


async def test_retry_message_tells_the_model_why(fake_sim):
    """capture_run_messages records the whole exchange to check what the model was told."""
    with capture_run_messages() as messages:
        with study_agent.override(model=FunctionModel(unsafe_then_safe)):
            await run_study("Optimize case A", fake_sim)

    rejections = [p for m in messages for p in m.parts if was_rejected(p)]
    assert len(rejections) == 1
    assert "exceeds the limit of 80.0 bar" in str(rejections[0].content)


# ---------- 4. failure handling through the production entry point ----------


def always_simulate(_messages, _info: AgentInfo) -> ModelResponse:
    return call_tool("run_simulation", case_id="A", inlet_bar=50.0)  # never finishes


async def test_runaway_loop_is_capped(fake_sim):
    """Goes through run_study, so the test fails if someone removes the usage limit from production code."""
    with study_agent.override(model=FunctionModel(always_simulate)):
        with pytest.raises(StudyFailed) as exc_info:
            await run_study("Optimize case A", fake_sim)

    assert exc_info.value.reason == "too_many_steps"
    assert (
        len(fake_sim.calls) <= MAX_MODEL_REQUESTS
    )  # the cap stopped it; it did not run forever


def malformed_output(_messages, info: AgentInfo) -> ModelResponse:
    return final_output(
        info, objective="not a number"
    )  # wrong type, and 'feasible' is missing


async def test_malformed_output_becomes_clear_user_error(fake_sim):
    """After the agent's retries run out, the user gets a stable reason code, not a stack trace."""
    with study_agent.override(model=FunctionModel(malformed_output)):
        with pytest.raises(StudyFailed) as exc_info:
            await run_study("Optimize case A", fake_sim)

    assert exc_info.value.reason == "invalid_output"


# ---------- 5. delegation between agents ----------


async def test_delegation_to_reviewer_agent(fake_sim):
    """Script the orchestrator with FunctionModel and the sub-agent separately. Tests the handoff contract."""
    reviewer_saw: list[str] = []

    def reviewer(messages, _info: AgentInfo) -> ModelResponse:
        reviewer_saw.append(str(last_part(messages).content))
        return ModelResponse(
            parts=[TextPart("Plausible: outlet pressure is below inlet.")]
        )

    def simulate_review_report(messages, info: AgentInfo) -> ModelResponse:
        last = last_part(messages)
        if isinstance(last, UserPromptPart):
            return call_tool("run_simulation", case_id="A", inlet_bar=60.0)
        if isinstance(last, ToolReturnPart) and last.tool_name == "run_simulation":
            outlet = last.content["outlet_bar"]
            return call_tool(
                "request_review", summary=f"Case A at 60 bar gives {outlet} bar outlet"
            )
        return final_output(
            info, objective=2.5, feasible=True, review=str(last.content)
        )

    with (
        study_agent.override(model=FunctionModel(simulate_review_report)),
        reviewer_agent.override(model=FunctionModel(reviewer)),
    ):
        result = await run_study("Optimize case A", fake_sim)

    assert reviewer_saw == [
        "Case A at 60 bar gives 54.0 bar outlet"
    ]  # sub-agent got the right summary
    assert (
        result.review == "Plausible: outlet pressure is below inlet."
    )  # and its answer flowed back
