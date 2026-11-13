"""Typed-list capture (P1-5): a typed shopping list becomes draft prices and questions.

Lines are read by rules first (``app.tools.read_line``); only the lines they are not sure of go
to the model (owner decision, 2026-11-13). Either way the reader only copies: for each line the
item words, quantity, unit, size, and price as written. Everything after that is deterministic code: matching the item
(``find_catalog_item``), finding one piece's weight, and the per-cup math (``normalize_price``).
The model does no arithmetic and chooses nothing (Phase 1 spec, D7).

Nothing is saved here. The result is drafts for the user to confirm, and questions for anything
that would otherwise be a guess: an unclear item, a piece with no known weight or size, a unit or
price that cannot be read.
"""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, models
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.usage import UsageLimits
from sqlmodel import Session

from app.agents.models import build_model
from app.agents.usage import ModelUsage
from app.core.config import CaptureReader, settings
from app.models import CatalogItem, Household, PortionSize, Vendor
from app.prices import PieceWeight, piece_grams
from app.tools.catalog import Candidate, Match, find_catalog_item
from app.tools.normalize import (
    InvalidAmount,
    NeedsPortion,
    NoConversion,
    UnknownUnit,
    WrongMeasure,
    canonical_unit,
    catalog_facts,
    count_unit_in,
    measure_name,
    normalize_price,
    piece_unit,
    portion_grams,
)
from app.tools.read_line import (
    needs_model,
    read_list_line,
    read_price,
)

MAX_LINES = 100
MAX_LINE_LENGTH = 200
# One request normally; the rest allow output retries.
MAX_MODEL_REQUESTS = 4


# ---------- what the model returns ----------


logger = logging.getLogger(__name__)


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
        description="The amount bought as written ('3 lb bag' is 3, '3 for $5' is 3, "
        "'$0.29 each (3)' is 3), or null if none is written",
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
- quantity: the amount bought, as written. "3 lb bag" is 3. "3 for $5" is 3. A count written
  apart from the price is the amount bought too: "$0.29 each (3)", "1.29 ea x3", and
  "$.73 ea - 3" are 3. Null if none.
- unit: the unit word as written. "3 lb bag" is "lb". "3 for $5" and "2 ea" are "each". A bunch
  is "bunch". A unit can come before the item: "Head of lettuce $3" is "head", "bunch of kale"
  is "bunch". Null if no unit is written.
- size: small, medium, or large only if the line says so ("large oranges", "med avocado").
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
    # The size whose piece weight was used. A size written on a line that needs no piece weight,
    # or for an item without a weight for it, is ignored (None).
    size: PortionSize | None
    price_cents: int  # for the whole quantity
    vendor_id: uuid.UUID
    observed_on: date
    # The item words read from the line, and how they matched (exact, alias, fuzzy...). A fuzzy
    # match the user confirms is saved as a household alias.
    item_text: str = ""
    match_kind: str = "exact"
    # How a per-unit price became the total: "$0.29 each x 3". Code multiplies, never the model.
    price_note: str | None = None
    # No size was written, so medium was used: the page says so (owner, 2026-11-12).
    size_assumed: bool = False
    # The user's answer to "How much does one ... weigh?", saved with the price.
    piece_weight: PieceWeight | None = None
    piece_grams: float | None = None  # one piece as purchased, for count units
    piece_source: str | None = None
    grams_purchased: float | None = None
    grams_edible: float | None = None
    cups: float | None = None
    cost_per_cup_cents: float | None = None
    conversion_source: str | None = None
    stand_in_name: str | None = None
    note: str | None = None  # for example "no cup conversion yet"
    # What the item is compared by (spec P1-11): "cup", or a piece ("lemon", "head"), and for a
    # piece, how many were bought and the cost of one.
    measure: str = "cup"
    pieces: float | None = None
    cost_per_piece_cents: float | None = None
    # Nuts bought in the shell: the price is converted to kernels with SR28's shell refuse.
    in_shell: bool = False
    shell_note: str | None = None


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


@dataclass(frozen=True)
class LineAnswer:
    """The user's answer to one question. Each field replaces what was read, or adds to it."""

    catalog_item_id: uuid.UUID | None = None
    unit: str | None = None
    size: PortionSize | None = None
    piece_weight: PieceWeight | None = None


@dataclass
class CaptureQuestion:
    raw_text: str
    kind: QuestionKind
    message: str
    options: list[str] = field(default_factory=list)
    # What was read so far, when the item is known: answering completes this draft.
    draft: DraftPrice | None = None
    # The item words the question is about ("greens"), for saving the user's choice as an alias.
    item_text: str | None = None
    # What the model read on this line, so an answer completes it in code without the model,
    # and the answers given so far (an item chosen, then a unit), kept for the next answer.
    reading: ExtractedLine | None = None
    answered: LineAnswer | None = None


@dataclass
class CaptureResult:
    drafts: list[DraftPrice] = field(default_factory=list)
    questions: list[CaptureQuestion] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)  # lines that are not prices
    data_snapshot_ids: set[uuid.UUID] = field(default_factory=set)
    usage: ModelUsage = field(default_factory=ModelUsage)
    # How the lines were read, and how many the model read (with rules first, only the lines
    # the rules were not sure of; owner decision, 2026-11-13).
    reader: CaptureReader = "rules_first"
    model_lines: int = 0


class CaptureFailed(Exception):
    """User-facing failure with a stable reason code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------- deterministic steps ----------


def total_cents(per_unit_cents: int, quantity: float) -> int:
    """The total for a per-unit price: 29 cents each x 3 -> 87. Rounded half up to the cent."""
    total = Decimal(per_unit_cents) * Decimal(str(quantity))
    return int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _per(unit: str) -> str:
    return "each" if unit == "each" else f"a {unit}"


def split_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def numbered_lines(lines: list[str]) -> str:
    return "\n".join(f"{n}: {line}" for n, line in enumerate(lines, start=1))


# Words that say nuts were bought in the shell ("walnuts with shell"; spec P1-11, D9).
_IN_SHELL = re.compile(
    r"\b(?:(?:with|in)\s+(?:the\s+)?shells?|in-shell|unshelled)\b", re.IGNORECASE
)


def shell_words(item_text: str) -> tuple[str, bool]:
    """The item words without any in-shell words, and whether there were any."""
    found = _IN_SHELL.search(item_text)
    if not found:
        return item_text, False
    return " ".join(_IN_SHELL.sub(" ", item_text).split()), True


def _question_for_match(raw: str, match: Match) -> CaptureQuestion:
    names = [c.name for c in match.candidates]
    if match.kind == "ambiguous":
        message = f"Which item is '{match.text}': {', '.join(names)}?"
        return CaptureQuestion(raw, "which_item", message, names, item_text=match.text)
    if names:
        message = f"Which item is '{match.text}'? Closest: {', '.join(names)}."
    else:
        message = f"Which item is '{match.text}'? It is not in the catalog."
    return CaptureQuestion(raw, "no_match", message, names, item_text=match.text)


def _process_line(
    session: Session,
    household: Household,
    raw: str,
    line: ExtractedLine | None,
    vendor_id: uuid.UUID,
    observed_on: date,
    result: CaptureResult,
    answer: LineAnswer | None = None,
) -> None:
    """Read one line into a draft or a question. ``answer`` holds the user's answers so far."""
    answer = answer or LineAnswer()
    asked = len(result.questions)
    _process(session, household, raw, line, vendor_id, observed_on, result, answer)
    for question in result.questions[asked:]:
        question.reading = line


def _process(
    session: Session,
    household: Household,
    raw: str,
    line: ExtractedLine | None,
    vendor_id: uuid.UUID,
    observed_on: date,
    result: CaptureResult,
    answer: LineAnswer,
) -> None:
    if line is None:
        result.questions.append(
            CaptureQuestion(raw, "unreadable", f"Could not read '{raw}'. What is it?")
        )
        return
    if not line.is_price:
        result.ignored.append(raw)
        return
    if answer.catalog_item_id is None and (
        not line.item_text or not line.item_text.strip()
    ):
        result.questions.append(
            CaptureQuestion(raw, "no_match", f"Which item is '{raw}'?")
        )
        return
    if answer.catalog_item_id is not None:
        chosen = session.get(CatalogItem, answer.catalog_item_id)
        if chosen is None:
            raise ValueError("unknown catalog item")
        match = Match(
            "household",
            line.item_text or raw,
            "",
            Candidate(chosen.id, chosen.slug, chosen.name, 1.0),
        )
    else:
        words, _ = shell_words(line.item_text or "")
        match = find_catalog_item(session, words, household)
    if match.item is None:
        result.questions.append(_question_for_match(raw, match))
        return
    item = session.get(CatalogItem, match.item.item_id)
    if item is None:  # pragma: no cover - the matcher only returns existing items
        raise RuntimeError(f"catalog item {match.item.item_id} disappeared")

    written_price = read_price(line.price_text)
    cents = written_price.cents
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
    # A unit before the item ("Head of lettuce $3") can be missed by the reader: the line's own
    # count word is used when it names exactly one.
    written_unit = answer.unit or line.unit or written_price.per or count_unit_in(raw)
    if written_unit is None:
        options = ["lb", "each", "bunch", "head"]
        result.questions.append(
            CaptureQuestion(
                raw,
                "missing_unit",
                f"What did ${cents / 100:.2f} buy of {item.name.lower()}: "
                "a pound, one each, a bunch, a head, or something else?",
                options,
            )
        )
        return
    try:
        unit = canonical_unit(written_unit)
    except UnknownUnit as exc:
        result.questions.append(CaptureQuestion(raw, "unknown_unit", str(exc)))
        return
    # A unit with no number ("lb 1.29", "2.49/lb") is one unit. Prices are stored as the total
    # paid for the quantity, so a per-unit price ("$0.29 each (3)") is multiplied out here.
    quantity = 1.0 if line.quantity is None else line.quantity
    price_note = None
    is_per_unit = line.price_is_per_unit or written_price.per is not None
    if is_per_unit and quantity > 0 and quantity != 1:
        per_unit = cents
        cents = total_cents(per_unit, quantity)
        price_note = f"${per_unit / 100:.2f} {_per(unit)} x {quantity:g}"

    draft = DraftPrice(
        raw_text=raw,
        catalog_item_id=item.id,
        item_name=item.name,
        match_note=match.note,
        item_text=line.item_text or "",
        match_kind=match.kind,
        quantity=quantity,
        unit=unit,
        size=None,
        price_cents=cents,
        vendor_id=vendor_id,
        observed_on=observed_on,
        price_note=price_note,
    )
    facts = catalog_facts(session, item)
    draft.measure = measure_name(item.name, facts.compared_by)
    # In the shell only means something for nuts (an item with a shell refuse).
    shell = facts.shell_refuse_pct
    if shell is not None and shell_words(line.item_text or "")[1]:
        draft.in_shell = True
        draft.shell_note = (
            f"In the shell: {100 - shell:g}% of the weight is kernels (USDA SR28)."
        )
    counted = piece_unit(unit, facts)
    piece: float | None = None
    if counted is not None and answer.piece_weight is not None:
        piece = piece_grams(answer.piece_weight)
        how = answer.piece_weight.source
        draft.piece_grams, draft.piece_source = piece, f"{how} at capture: {piece:g} g"
        draft.piece_weight = answer.piece_weight
    elif counted is not None:
        try:
            # An answered size, then one on the line, then one an alias implies ("satsuma").
            size = answer.size or line.size or match.size
            choice = portion_grams(session, item, counted, size)
        except NeedsPortion as exc:
            kind: QuestionKind = "needs_size" if exc.sizes else "needs_weight"
            result.questions.append(
                CaptureQuestion(raw, kind, str(exc), list(exc.sizes), draft)
            )
            return
        piece = choice.grams
        draft.piece_grams, draft.piece_source = choice.grams, choice.source
        draft.size, draft.size_assumed = choice.size, choice.assumed
        if choice.snapshot_id:
            result.data_snapshot_ids.add(choice.snapshot_id)

    try:
        normalized = normalize_price(
            cents, quantity, unit, facts, piece, in_shell=draft.in_shell
        )
    except NoConversion as exc:
        draft.note = str(exc)
    except InvalidAmount as exc:
        result.questions.append(CaptureQuestion(raw, "unreadable", str(exc), [], draft))
        return
    except WrongMeasure as exc:
        result.questions.append(
            CaptureQuestion(raw, "unknown_unit", str(exc), [], draft)
        )
        return
    else:
        draft.grams_purchased = normalized.grams_purchased
        draft.grams_edible = normalized.grams_edible
        draft.cups = normalized.cups
        draft.cost_per_cup_cents = normalized.cost_per_cup_cents
        draft.pieces = normalized.pieces
        draft.cost_per_piece_cents = normalized.cost_per_piece_cents
        draft.conversion_source = normalized.conversion_source
        draft.stand_in_name = normalized.stand_in_name
        if facts.snapshot_id:
            result.data_snapshot_ids.add(facts.snapshot_id)
    result.drafts.append(draft)


def answer_line(
    session: Session,
    household: Household,
    question: CaptureQuestion,
    answer: LineAnswer,
    vendor_id: uuid.UUID,
    observed_on: date,
) -> CaptureResult:
    """Complete one question's line with the user's answer, in code: no model call.

    The line is read again from what the model read the first time, with the answer applied.
    The result is a draft, or the next question (an item chosen, then its weight).
    """
    if question.reading is None:
        raise CaptureFailed(
            "not_read", "This line could not be read. Drop it and type it again."
        )
    prior = question.answered or LineAnswer()
    merged = LineAnswer(
        catalog_item_id=answer.catalog_item_id or prior.catalog_item_id,
        unit=answer.unit or prior.unit,
        size=answer.size or prior.size,
        piece_weight=answer.piece_weight or prior.piece_weight,
    )
    result = CaptureResult()
    _process_line(
        session,
        household,
        question.raw_text,
        question.reading,
        vendor_id,
        observed_on,
        result,
        merged,
    )
    for follow_up in result.questions:
        follow_up.answered = merged
    if merged.catalog_item_id is not None:
        chosen = [*result.drafts, *(q.draft for q in result.questions if q.draft)]
        for draft in chosen:
            draft.match_note = f"you chose {draft.item_name}"
            draft.match_kind = "chosen"
    return result


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


async def read_lines(
    lines: list[str],
    model: models.Model | str | None = None,
    reader: CaptureReader = "rules_first",
) -> tuple[dict[int, ExtractedLine], ModelUsage, int]:
    """Read the lines: by the rules first, sending only the ones they are not sure of to the
    model (``reader="rules_first"``), or every line to the model (``"model"``).

    Returns each line's reading by line number, the model's usage, and how many lines it read.
    A line missing from the readings becomes an "unreadable" question.
    """
    if reader == "model":
        extracted, usage = await extract_lines(lines, model)
        return extracted, usage, len(lines)
    readings: dict[int, ExtractedLine] = {}
    unsure: list[int] = []
    for number, raw in enumerate(lines, start=1):
        entry = read_list_line(raw)
        if needs_model(raw, entry):
            unsure.append(number)
        else:
            readings[number] = ExtractedLine.model_validate({"line": number, **entry})
    if not unsure:
        return readings, ModelUsage(), 0
    try:
        extracted, usage = await extract_lines([lines[n - 1] for n in unsure], model)
    except CaptureFailed as exc:
        # The rules' readings still stand; the model's lines become questions.
        logger.warning("The model could not read %d lines: %s", len(unsure), exc.reason)
        return readings, ModelUsage(), len(unsure)
    for asked, number in enumerate(unsure, start=1):
        if asked in extracted:
            readings[number] = extracted[asked].model_copy(update={"line": number})
    return readings, usage, len(unsure)


async def capture_typed_list(
    session: Session,
    household: Household,
    text: str,
    vendor_id: uuid.UUID,
    observed_on: date,
    model: models.Model | str | None = None,
    reader: CaptureReader | None = None,
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
    result = CaptureResult(reader=reader or settings.CAPTURE_READER)
    if not lines:
        return result
    extracted, result.usage, result.model_lines = await read_lines(
        lines, model, result.reader
    )
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
