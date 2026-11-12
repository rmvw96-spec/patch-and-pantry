"""Typed-list capture (P1-5): a typed shopping list becomes draft prices and questions.

The model only reads: for each numbered line it copies the item words, quantity, unit, size, and
price as written. Everything after that is deterministic code: matching the item
(``find_catalog_item``), finding one piece's weight, and the per-cup math (``normalize_price``).
The model does no arithmetic and chooses nothing (Phase 1 spec, D7).

Nothing is saved here. The result is drafts for the user to confirm, and questions for anything
that would otherwise be a guess: an unclear item, a piece with no known weight or size, a unit or
price that cannot be read.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, models
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.usage import UsageLimits
from sqlmodel import Session

from app.agents.models import build_model
from app.agents.usage import ModelUsage
from app.core.config import settings
from app.models import CatalogItem, Household, PortionSize, Vendor
from app.tools.catalog import Match, find_catalog_item
from app.tools.normalize import (
    COUNT_UNITS,
    InvalidAmount,
    NeedsPortion,
    NoConversion,
    UnknownUnit,
    canonical_unit,
    catalog_facts,
    normalize_price,
    portion_grams,
)

MAX_LINES = 100
MAX_LINE_LENGTH = 200
# One request normally; the rest allow output retries.
MAX_MODEL_REQUESTS = 4


# ---------- what the model returns ----------


class ExtractedLine(BaseModel):
    """One typed line, copied as written. No arithmetic: the code converts everything."""

    line: int = Field(description="The line number given in the prompt")
    is_price: bool = Field(
        description="False for a line that is not an item with a price, such as a store name"
    )
    item_text: str | None = Field(
        default=None,
        description="The item words only, as written, without quantity, unit, size, or price",
    )
    quantity: float | None = Field(
        default=None,
        description="The number of units as written, or null if none is written",
    )
    unit: str | None = Field(
        default=None,
        description="The unit word as written (lb, oz, each, bunch, head, ear, pint...), "
        "or null if none is written",
    )
    size: Literal["small", "medium", "large"] | None = None
    price_text: str | None = Field(
        default=None, description="The price exactly as written, such as '2.99' or '$5'"
    )
    price_is_per_unit: bool = Field(
        default=False,
        description="True when the price is for one unit ('2.49/lb', '1.29 ea'); "
        "false when it is for the whole quantity ('3 lb bag 2.99', '3 for $5')",
    )


class Extraction(BaseModel):
    lines: list[ExtractedLine]


INSTRUCTIONS = """\
You read a typed shopping list. Each line is numbered. Return one entry per line number.

Copy what is written; never calculate, convert, or guess:
- item_text: only the words naming the produce ("yellow onions"), as written, misspellings
  included. Leave out quantities, units, sizes, packaging words such as "bag", and prices.
- quantity: the number of units written. "3 lb bag" is 3. "3 for $5" is 3. Null if none.
- unit: the unit word as written. "3 lb bag" is "lb". "3 for $5" and "2 ea" are "each". A bunch
  is "bunch". Null if no unit is written.
- size: small, medium, or large only if the line says so.
- price_text: the price as written, such as "2.99" or "$5". Null if there is no price.
- price_is_per_unit: true only when the price is for one unit, such as "2.49/lb", "2.49 per lb",
  or "1.29 ea".
- is_price: false for lines that are not an item with a price, such as a store name or a note.
"""

capture_agent = Agent(output_type=Extraction, instructions=INSTRUCTIONS, retries=2)


# ---------- what the caller gets ----------


@dataclass
class DraftPrice:
    """A price ready for the user to confirm. Never saved here."""

    raw_text: str  # the line exactly as typed
    catalog_item_id: uuid.UUID
    item_name: str
    match_note: str | None  # "matched 'yellow onoins' to Onion"
    quantity: float
    unit: str
    size: PortionSize | None
    price_cents: int  # for the whole quantity
    vendor_id: uuid.UUID
    observed_on: date
    # The item words read from the line, and how they matched (exact, alias, fuzzy...). A fuzzy
    # match the user confirms is saved as a household alias.
    item_text: str = ""
    match_kind: str = "exact"
    piece_grams: float | None = None  # one piece as purchased, for count units
    piece_source: str | None = None
    grams_purchased: float | None = None
    grams_edible: float | None = None
    cups: float | None = None
    cost_per_cup_cents: float | None = None
    conversion_source: str | None = None
    stand_in_name: str | None = None
    note: str | None = None  # for example "no cup conversion yet"


QuestionKind = Literal[
    "unreadable",
    "no_match",
    "which_item",
    "missing_price",
    "missing_unit",
    "unknown_unit",
    "needs_weight",
    "needs_size",
]


@dataclass
class CaptureQuestion:
    raw_text: str
    kind: QuestionKind
    message: str
    options: list[str] = field(default_factory=list)
    # What was read so far, when the item is known: answering completes this draft.
    draft: DraftPrice | None = None


@dataclass
class CaptureResult:
    drafts: list[DraftPrice] = field(default_factory=list)
    questions: list[CaptureQuestion] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)  # lines that are not prices
    data_snapshot_ids: set[uuid.UUID] = field(default_factory=set)
    usage: ModelUsage = field(default_factory=ModelUsage)


class CaptureFailed(Exception):
    """User-facing failure with a stable reason code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------- deterministic steps ----------

_PRICE = re.compile(r"^\$?\s*(\d+(?:\.\d{1,2})?|\.\d{1,2})\s*$")
_CENTS = re.compile(r"^(\d{1,2})\s*(?:c|¢|cents?)$")


def price_cents(text: str | None) -> int | None:
    """'2.99', '$5', '.99', '99c' -> integer cents; None if the text is not a plain price."""
    if text is None:
        return None
    cleaned = text.strip().lower().replace(",", "")
    cents = _CENTS.match(cleaned)
    if cents:
        return int(cents.group(1))
    dollars = _PRICE.match(cleaned)
    if not dollars:
        return None
    try:
        return int((Decimal(dollars.group(1)) * 100).to_integral_value())
    except InvalidOperation:  # pragma: no cover - the pattern admits only valid numbers
        return None


def split_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def numbered_lines(lines: list[str]) -> str:
    return "\n".join(f"{n}: {line}" for n, line in enumerate(lines, start=1))


def _question_for_match(raw: str, match: Match) -> CaptureQuestion:
    names = [c.name for c in match.candidates]
    if match.kind == "ambiguous":
        return CaptureQuestion(
            raw,
            "which_item",
            f"Which item is '{match.text}': {', '.join(names)}?",
            names,
        )
    if names:
        message = f"Which item is '{match.text}'? Closest: {', '.join(names)}."
    else:
        message = f"Which item is '{match.text}'? It is not in the catalog."
    return CaptureQuestion(raw, "no_match", message, names)


def _process_line(
    session: Session,
    household: Household,
    raw: str,
    line: ExtractedLine | None,
    vendor_id: uuid.UUID,
    observed_on: date,
    result: CaptureResult,
) -> None:
    if line is None:
        result.questions.append(
            CaptureQuestion(raw, "unreadable", f"Could not read '{raw}'. What is it?")
        )
        return
    if not line.is_price:
        result.ignored.append(raw)
        return
    if not line.item_text or not line.item_text.strip():
        result.questions.append(
            CaptureQuestion(raw, "no_match", f"Which item is '{raw}'?")
        )
        return
    match = find_catalog_item(session, line.item_text, household)
    if match.item is None:
        result.questions.append(_question_for_match(raw, match))
        return
    item = session.get(CatalogItem, match.item.item_id)
    if item is None:  # pragma: no cover - the matcher only returns existing items
        raise RuntimeError(f"catalog item {match.item.item_id} disappeared")

    cents = price_cents(line.price_text)
    if cents is None:
        written = f" ('{line.price_text}')" if line.price_text else ""
        result.questions.append(
            CaptureQuestion(
                raw,
                "missing_price",
                f"What was the price of {item.name.lower()}{written}?",
            )
        )
        return
    if line.unit is None:
        options = ["lb", "each", "bunch"]
        result.questions.append(
            CaptureQuestion(
                raw,
                "missing_unit",
                f"What did ${cents / 100:.2f} buy of {item.name.lower()}: "
                "a pound, one each, a bunch, or something else?",
                options,
            )
        )
        return
    try:
        unit = canonical_unit(line.unit)
    except UnknownUnit as exc:
        result.questions.append(CaptureQuestion(raw, "unknown_unit", str(exc)))
        return
    # A per-unit price ("2.49/lb") or a unit with no number ("lb 1.29") is the price of one unit.
    quantity = 1.0 if line.price_is_per_unit or line.quantity is None else line.quantity

    draft = DraftPrice(
        raw_text=raw,
        catalog_item_id=item.id,
        item_name=item.name,
        match_note=match.note,
        item_text=line.item_text,
        match_kind=match.kind,
        quantity=quantity,
        unit=unit,
        size=line.size,
        price_cents=cents,
        vendor_id=vendor_id,
        observed_on=observed_on,
    )
    piece: float | None = None
    if unit in COUNT_UNITS:
        try:
            choice = portion_grams(session, item, unit, line.size)
        except NeedsPortion as exc:
            kind: QuestionKind = "needs_size" if exc.sizes else "needs_weight"
            result.questions.append(
                CaptureQuestion(raw, kind, str(exc), list(exc.sizes), draft)
            )
            return
        piece = choice.grams
        draft.piece_grams, draft.piece_source = choice.grams, choice.source
        if choice.snapshot_id:
            result.data_snapshot_ids.add(choice.snapshot_id)

    facts = catalog_facts(session, item)
    try:
        normalized = normalize_price(cents, quantity, unit, facts, piece)
    except NoConversion as exc:
        draft.note = str(exc)
    except InvalidAmount as exc:
        result.questions.append(CaptureQuestion(raw, "unreadable", str(exc), [], draft))
        return
    else:
        draft.grams_purchased = normalized.grams_purchased
        draft.grams_edible = normalized.grams_edible
        draft.cups = normalized.cups
        draft.cost_per_cup_cents = normalized.cost_per_cup_cents
        draft.conversion_source = normalized.conversion_source
        draft.stand_in_name = normalized.stand_in_name
        if facts.snapshot_id:
            result.data_snapshot_ids.add(facts.snapshot_id)
    result.drafts.append(draft)


async def extract_lines(
    lines: list[str], model: models.Model | str | None = None
) -> tuple[dict[int, ExtractedLine], ModelUsage]:
    """Ask the model to read the numbered lines. Returns each line's entry by line number."""
    if model is None:
        model = build_model(settings.SIM_MODEL, role="capture")
    try:
        result = await capture_agent.run(
            numbered_lines(lines),
            model=model,
            usage_limits=UsageLimits(request_limit=MAX_MODEL_REQUESTS),
        )
    except UsageLimitExceeded as exc:
        raise CaptureFailed(
            "too_many_steps",
            "Reading the list took too many steps. Try a shorter list.",
        ) from exc
    except UnexpectedModelBehavior as exc:
        raise CaptureFailed(
            "invalid_output", "The assistant could not read the list. Please try again."
        ) from exc
    by_number: dict[int, ExtractedLine] = {}
    for entry in result.output.lines:
        # A line number outside the list, or one given twice, is ignored rather than trusted:
        # the line then gets an "unreadable" question instead of someone else's data.
        if 1 <= entry.line <= len(lines) and entry.line not in by_number:
            by_number[entry.line] = entry
    return by_number, ModelUsage.of(result.usage)


async def capture_typed_list(
    session: Session,
    household: Household,
    text: str,
    vendor_id: uuid.UUID,
    observed_on: date,
    model: models.Model | str | None = None,
) -> CaptureResult:
    """Turn a typed list into drafts and questions. Reads the database; writes nothing."""
    vendor = session.get(Vendor, vendor_id)
    if vendor is None or vendor.household_id != household.id:
        raise CaptureFailed(
            "unknown_vendor", "Choose one of your own stores or markets."
        )
    lines = split_lines(text)
    if len(lines) > MAX_LINES:
        raise CaptureFailed(
            "too_many_lines",
            f"A list can have up to {MAX_LINES} lines; split it in two.",
        )
    long = [line for line in lines if len(line) > MAX_LINE_LENGTH]
    if long:
        raise CaptureFailed(
            "line_too_long", f"Lines can be up to {MAX_LINE_LENGTH} characters long."
        )
    result = CaptureResult()
    if not lines:
        return result
    extracted, result.usage = await extract_lines(lines, model)
    for number, raw in enumerate(lines, start=1):
        _process_line(
            session,
            household,
            raw,
            extracted.get(number),
            vendor_id,
            observed_on,
            result,
        )
    return result
