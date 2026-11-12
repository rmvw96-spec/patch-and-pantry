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


# ---------- typed-list capture (P1-5) ----------

_NUMBERED = re.compile(r"^(\d+): (.*)$")
_PRICE_AT_END = re.compile(
    r"(?P<price>\$?\d*\.?\d+|\d+\s*(?:c|¢))\s*(?P<per>(?:/|per\s+|\s)(?:lb|lbs|ea|each|oz))?\s*$",
    re.IGNORECASE,
)
_FOR = re.compile(r"\b(?P<qty>\d+)\s*(?:for|/)\s*$", re.IGNORECASE)
_QTY_UNIT = re.compile(
    r"\b(?P<qty>\d+(?:\.\d+)?)?\s*(?P<unit>lbs?|pounds?|oz|ounces?|kg|each|ea|bunch(?:es)?|heads?|ears?|pints?)\b",
    re.IGNORECASE,
)
_SIZE = re.compile(r"\b(small|medium|large)\b", re.IGNORECASE)
_PACKAGING = re.compile(r"\b(bag|box|pack|package|bundle)\b", re.IGNORECASE)


def read_list_line(text: str) -> dict[str, object]:
    """A rough, deterministic reading of one typed line, standing in for the model in demo mode.

    It follows the same rules as the capture agent's instructions: copy, never calculate.
    """
    entry: dict[str, object] = {"is_price": False}
    price = _PRICE_AT_END.search(text)
    if not price:
        return entry
    rest = text[: price.start()].rstrip()
    entry |= {
        "is_price": True,
        "price_text": price.group("price").strip(),
        "price_is_per_unit": False,
    }
    if price.group("per"):
        entry |= {
            "price_is_per_unit": True,
            "unit": re.sub(r"[/\s]|per", "", price.group("per").lower()),
        }
    lot = _FOR.search(rest)
    if lot:
        entry |= {"quantity": float(lot.group("qty")), "unit": "each"}
        rest = rest[: lot.start()]
    unit = _QTY_UNIT.search(rest)
    if unit:
        if unit.group("qty"):
            entry["quantity"] = float(unit.group("qty"))
        entry.setdefault("unit", unit.group("unit").lower())
        rest = rest[: unit.start()] + rest[unit.end() :]
    size = _SIZE.search(rest)
    if size:
        entry["size"] = size.group(1).lower()
        rest = rest[: size.start()] + rest[size.end() :]
    item = " ".join(_PACKAGING.sub("", rest).split())
    entry["item_text"] = item or None
    return entry


def _demo_capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    lines = []
    for raw in _user_prompt(messages).splitlines():
        numbered = _NUMBERED.match(raw)
        if numbered:
            lines.append(
                {"line": int(numbered.group(1)), **read_list_line(numbered.group(2))}
            )
    return ModelResponse(
        parts=[ToolCallPart(info.output_tools[0].name, {"lines": lines})]
    )


def demo_capture_model() -> FunctionModel:
    return FunctionModel(_demo_capture)


# ---------- best-buy explanation (P1-6) ----------

_ITEM_HEADER = re.compile(r"^(?P<item>.+) \(needs ")
_MARKED = re.compile(
    r"^- MARKED BEST: (?P<label>.+?), .*?(?P<cost>\$\d+\.\d{2}) per usable cup"
)


def _demo_explain(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    """Names each item's marked row, quoting its cost exactly as the table shows it."""
    sentences = []
    item = None
    for line in _user_prompt(messages).splitlines():
        header = _ITEM_HEADER.match(line)
        if header:
            item = header.group("item")
        marked = _MARKED.match(line)
        if marked and item:
            label, cost = marked.group("label"), marked.group("cost")
            if label.startswith("national average"):
                sentences.append(
                    f"For {item.lower()} there is no store price yet; the national average "
                    f"works out to {cost} per usable cup, which is not a store price."
                )
            else:
                sentences.append(
                    f"For {item.lower()}, {label} is the best buy at {cost} per usable cup."
                )
    text = " ".join(sentences) or "There is no priced row to recommend yet."
    return ModelResponse(parts=[TextPart(text)])


def demo_best_buy_model() -> FunctionModel:
    return FunctionModel(_demo_explain)
