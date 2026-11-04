"""Deterministic stand-in for the LLM, enabled with SIM_MODEL=demo.

Reads the case and inlet pressure from the prompt ("Optimize case B at 70 bar"), runs one
simulation through the real tool, and reports the outcome. It lets you exercise the whole
platform (API, background job, Prefect flow, database) in a browser, an E2E test, or a nightly
run without an LLM API key, and it always gives the same answer for the same prompt.
"""

import re

from pydantic_ai import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

DEFAULT_CASE = "A"
DEFAULT_INLET_BAR = 60.0

_CASE_PATTERN = re.compile(r"\bcase\s+([A-Za-z])\b", re.IGNORECASE)
_BAR_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*bar\b", re.IGNORECASE)


def parse_prompt(prompt: str) -> tuple[str, float]:
    """Extract (case_id, inlet_bar) from a prompt, falling back to case A at 60 bar."""
    case = _CASE_PATTERN.search(prompt)
    bar = _BAR_PATTERN.search(prompt)
    case_id = case.group(1).upper() if case else DEFAULT_CASE
    inlet_bar = float(bar.group(1)) if bar else DEFAULT_INLET_BAR
    return case_id, inlet_bar


def _user_prompt(messages: list[ModelMessage]) -> str:
    for part in messages[0].parts:
        if isinstance(part, UserPromptPart) and isinstance(part.content, str):
            return part.content
    return ""


def _outlet_bar(content: object) -> float:
    """Read outlet_bar from a run_simulation result. Tool results are typed as a broad union."""
    value = content.get("outlet_bar") if isinstance(content, dict) else None
    return float(value) if isinstance(value, int | float) else 0.0


def _demo_study(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    last = messages[-1].parts[-1]
    if isinstance(last, ToolReturnPart):
        output_tool = info.output_tools[0].name
        if last.outcome == "retried":
            # The tool rejected the request (unknown case or pressure over the limit): report it.
            args: dict[str, object] = {"objective": 0.0, "feasible": False}
        else:
            outlet = _outlet_bar(last.content)
            args = {"objective": outlet, "feasible": outlet > 0}
        return ModelResponse(parts=[ToolCallPart(output_tool, args)])
    case_id, inlet_bar = parse_prompt(_user_prompt(messages))
    return ModelResponse(
        parts=[
            ToolCallPart("run_simulation", {"case_id": case_id, "inlet_bar": inlet_bar})
        ]
    )


def _demo_review(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("Demo review: result looks plausible.")])


def demo_study_model() -> FunctionModel:
    return FunctionModel(_demo_study)


def demo_reviewer_model() -> FunctionModel:
    return FunctionModel(_demo_review)
