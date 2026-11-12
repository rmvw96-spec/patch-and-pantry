"""Deterministic stand-ins for the LLM, enabled with SIM_MODEL=demo.

Each agent has a script: the capture agent reads typed lists with a small parser, and the
best-buy agent names each item's marked row. They let you exercise the whole platform (API,
background job, database, browser) in an E2E test or locally without an LLM API key, and they
always give the same answer for the same input.
"""

import asyncio
import re

from pydantic_ai import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.core.config import settings


async def _pause() -> None:
    if settings.DEMO_DELAY_SECONDS:
        await asyncio.sleep(settings.DEMO_DELAY_SECONDS)


def _user_prompt(messages: list[ModelMessage]) -> str:
    for part in messages[0].parts:
        if isinstance(part, UserPromptPart) and isinstance(part.content, str):
            return part.content
    return ""


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


async def _demo_capture(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    await _pause()
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


async def _demo_explain(
    messages: list[ModelMessage], _info: AgentInfo
) -> ModelResponse:
    await _pause()
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
            if label.startswith("National average"):
                sentences.append(
                    f"For {item.lower()} there is no store price yet; the national average "
                    f"works out to **{cost} per usable cup**, which is not a store price."
                )
            else:
                sentences.append(
                    f"For {item.lower()}, {label} is the best buy at **{cost} per usable cup**."
                )
    text = " ".join(sentences) or "There is no priced row to recommend yet."
    return ModelResponse(parts=[TextPart(text)])


def demo_best_buy_model() -> FunctionModel:
    return FunctionModel(_demo_explain)
