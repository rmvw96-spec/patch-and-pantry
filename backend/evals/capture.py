"""Run a case's list through typed-list capture and score each line against the case.

Capture runs as the app runs it (``capture_typed_list``), against the database's catalog, for a
household, store, and user made for the run inside a transaction that is always rolled back:
nothing is saved, even on the dev database.

Each line ends as one outcome: a ``price`` (a draft, or a question only about one piece's weight
or size, which carries the same draft; CI loads no piece weights, a seeded dev database does), a
question (``ask``), or a ``note``. Scored fields: the outcome, then the item, amount, unit, total
price, and size for a price, or the item and options for a question, as far as the case gives
them.
"""

import asyncio
import math
import uuid
from dataclasses import dataclass, field
from datetime import date

from pydantic_ai import models
from pydantic_ai.exceptions import ModelRateLimitError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from sqlmodel import Session, select

from app.agents.capture_agent import (
    CaptureQuestion,
    CaptureResult,
    DraftPrice,
    capture_typed_list,
)
from app.agents.usage import ModelUsage
from app.core.config import CaptureReader
from app.core.db import engine
from app.models import Household, Region, User, Vendor
from evals.cases import Case, Expect

DAY = date(2026, 11, 13)
# A question about one piece's weight or size still has the line's price: the same outcome as a
# draft, wherever piece weights are loaded or not.
PIECE_QUESTIONS = frozenset({"needs_weight", "needs_size"})


class RetryOnRateLimit(WrapperModel):
    """Waits and retries when the provider says to slow down (HTTP 429).

    Without it, a rate limit in a rules-first run would look like a model mistake: capture keeps
    the rules' readings and turns the model's lines into "unreadable" questions.
    """

    def __init__(
        self, wrapped: models.Model, waits: tuple[float, ...] = (5, 15, 30, 60)
    ):
        super().__init__(wrapped)
        self.waits = waits

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: models.ModelRequestParameters,
    ) -> ModelResponse:
        for wait in self.waits:
            try:
                return await super().request(
                    messages, model_settings, model_request_parameters
                )
            except ModelRateLimitError:
                await asyncio.sleep(wait)
        return await super().request(messages, model_settings, model_request_parameters)


@dataclass(frozen=True)
class Outcome:
    """What capture made of one line."""

    kind: str  # "price", "note", or the question's kind
    item: str | None = None
    quantity: float | None = None
    unit: str | None = None
    price: int | None = None
    size: str | None = None
    options: tuple[str, ...] = ()

    def show(self) -> str:
        if self.kind == "note":
            return "note"
        if self.kind == "price":
            size = f" {self.size}" if self.size else ""
            price = "?" if self.price is None else f"${self.price / 100:.2f}"
            return f"{self.item}: {self.quantity:g}{size} {self.unit} = {price}"
        about = f" ({self.item})" if self.item else ""
        offered = f" [{', '.join(self.options)}]" if self.options else ""
        return f"asks {self.kind}{about}{offered}"


@dataclass
class CaseRun:
    """One run of a case: each line's outcome, and the model's usage."""

    outcomes: dict[str, Outcome]
    usage: ModelUsage = field(default_factory=ModelUsage)
    model_lines: int = 0


def _priced(draft: DraftPrice) -> Outcome:
    return Outcome(
        "price",
        item=draft.item_name,
        quantity=draft.quantity,
        unit=draft.unit,
        price=draft.price_cents,
        size=draft.size,
    )


def _asked(question: CaptureQuestion) -> Outcome:
    if question.kind in PIECE_QUESTIONS and question.draft is not None:
        return _priced(question.draft)
    item = question.draft.item_name if question.draft else None
    return Outcome(question.kind, item=item, options=tuple(question.options))


def outcomes(result: CaptureResult) -> dict[str, Outcome]:
    """Each line's outcome, by the line as typed."""
    found: dict[str, Outcome] = {}
    for draft in result.drafts:
        found[draft.raw_text] = _priced(draft)
    for question in result.questions:
        found[question.raw_text] = _asked(question)
    for raw in result.ignored:
        found[raw] = Outcome("note")
    return found


async def capture_case(
    session: Session, case: Case, model: models.Model | str, reader: CaptureReader
) -> CaseRun:
    """Capture the case's list for a new user, household, and store in ``session``."""
    user = User(email=f"eval-{uuid.uuid4().hex[:12]}@example.com", hashed_password="x")
    session.add(user)
    session.flush()
    region = session.exec(select(Region).where(Region.code == "north-al")).one()
    household = Household(owner_id=user.id, region_id=region.id)
    session.add(household)
    session.flush()
    vendor = Vendor(household_id=household.id, name="Eval store", vendor_type="store")
    session.add(vendor)
    session.flush()
    result = await capture_typed_list(
        session, household, case.text, vendor.id, DAY, model, reader
    )
    return CaseRun(outcomes(result), result.usage, result.model_lines)


async def run_case(
    case: Case, model: models.Model | str, reader: CaptureReader
) -> CaseRun:
    """Capture the case's list in a transaction that is always rolled back."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Session(connection) as session:
                return await capture_case(session, case, model, reader)
        finally:
            transaction.rollback()


# ---------- scoring ----------


def expected_fields(expect: Expect) -> list[str]:
    """The fields a case gives for a line, which are the ones scored."""
    if expect.note:
        return ["outcome"]
    given = ["outcome"]
    if expect.item is not None:
        given.append("item")
    if expect.ask:
        return given + (["options"] if expect.options else [])
    return given + [
        f
        for f in ("quantity", "unit", "price", "size")
        if getattr(expect, f) is not None
    ]


def _right(name: str, got: Outcome, expect: Expect) -> bool:
    if name == "outcome":
        want = "note" if expect.note else expect.ask or "price"
        return got.kind == want
    if name == "options":
        return set(expect.options) <= set(got.options)
    value, wanted = getattr(got, name), getattr(expect, name)
    if name == "quantity" and value is not None:
        return math.isclose(value, wanted, rel_tol=1e-6)
    return bool(value == wanted)


@dataclass(frozen=True)
class LineScore:
    text: str
    right: int
    scored: int
    got: Outcome | None
    wrong: tuple[str, ...]  # fields wrong against the closest acceptable outcome
    closest: Expect


def score_line(text: str, wanted: tuple[Expect, ...], got: Outcome | None) -> LineScore:
    best: LineScore | None = None
    for expect in wanted:
        names = expected_fields(expect)
        wrong = tuple(
            n
            for n in names
            if got is None
            or (n != "outcome" and not _right("outcome", got, expect))
            or not _right(n, got, expect)
        )
        score = LineScore(text, len(names) - len(wrong), len(names), got, wrong, expect)
        if best is None or score.right / score.scored > best.right / best.scored:
            best = score
    assert best is not None
    return best


def score_case(case: Case, run: CaseRun) -> list[LineScore]:
    return [
        score_line(line.text, line.expect, run.outcomes.get(line.text))
        for line in case.lines
    ]


def show_expect(expect: Expect) -> str:
    if expect.note:
        return "note"
    if expect.ask:
        return Outcome(expect.ask, item=expect.item, options=expect.options).show()
    return Outcome(
        "price",
        item=expect.item,
        quantity=expect.quantity if expect.quantity is not None else math.nan,
        unit=expect.unit,
        price=expect.price,
        size=expect.size,
    ).show()
