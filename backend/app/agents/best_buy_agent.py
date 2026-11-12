"""A short, plain explanation of the best-buy table (P1-6).

The table is computed by ``compare_best_buy``; the model only explains it. It may quote numbers
exactly as the table shows them and nothing else: an output validator rejects any explanation
with a number the table does not contain (a computed ratio, a rounded figure, a made-up price),
and the model gets the offending numbers back to fix. Tone and wording rules are refined in P1-8.
"""

from dataclasses import dataclass

from pydantic_ai import Agent, ModelRetry, RunContext, models
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from app.agents.models import build_model
from app.agents.usage import ModelUsage
from app.core.config import settings
from app.tools.best_buy import (
    BestBuyTable,
    cups_text,
    money,
    percent,
    unsupported_numbers,
)

MAX_MODEL_REQUESTS = 4

INSTRUCTIONS = """\
You explain a produce price comparison to a household, in plain words, in at most four short
sentences. The table is already computed; do not compute anything.

- Name the marked (best) row for each item and why it wins, quoting its cost per usable cup.
- Mention spoilage when it changes the answer.
- Quote numbers exactly as the table shows them, with the same digits. Never add, subtract,
  compare by ratio, round, or invent a number. Say "cheaper", not "3 times cheaper".
- A national average (ERS) is not a store price; say so if you mention one.
- This is about buying produce, not dieting: no talk of calories, weight, or eating less.
"""


@dataclass(frozen=True)
class Deps:
    table: BestBuyTable


best_buy_agent = Agent(
    deps_type=Deps, output_type=str, instructions=INSTRUCTIONS, retries=2
)


@best_buy_agent.output_validator
def _only_table_numbers(ctx: RunContext[Deps], text: str) -> str:
    bad = unsupported_numbers(text, ctx.deps.table)
    if bad:
        raise ModelRetry(
            f"These numbers are not in the table: {', '.join(bad)}. "
            "Quote only numbers the table shows, exactly as shown."
        )
    return text


def format_table(table: BestBuyTable) -> str:
    """The table as the model sees it. The numbers use the same formats the validator accepts."""
    lines = [f"Prices compared on {table.on.isoformat()}."]
    current = None
    for row in table.rows:
        if row.item_name != current:
            current = row.item_name
            lines.append(f"\n{row.item_name} (needs {row.cups_per_week:g} cups a week)")
        mark = "MARKED BEST" if row.marked else row.kind.replace("_", " ")
        if row.kind == "no_price":
            lines.append(f"- {mark}: no price yet")
            continue
        price = f"{money(row.price_cents)} for {row.quantity:g} {row.unit}"
        detail = (
            f"{cups_text(row.cups_bought)} cups bought, {cups_text(row.usable_cups)} usable "
            f"({percent(row.spoiled_share)} spoils), {money(row.cost_per_usable_cup_cents)} "
            "per usable cup"
            if row.cost_per_usable_cup_cents is not None
            else "no cost per cup"
        )
        notes = f" Notes: {' '.join(row.notes)}" if row.notes else ""
        lines.append(f"- {mark}: {row.label}, {price}: {detail}.{notes}")
    return "\n".join(lines)


class ExplanationFailed(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Explanation:
    text: str
    usage: ModelUsage


async def explain_best_buy(
    table: BestBuyTable, model: models.Model | str | None = None
) -> Explanation:
    """A short explanation that quotes only the table's numbers."""
    if model is None:
        model = build_model(settings.SIM_MODEL, role="best_buy")
    try:
        result = await best_buy_agent.run(
            format_table(table),
            deps=Deps(table),
            model=model,
            usage_limits=UsageLimits(request_limit=MAX_MODEL_REQUESTS),
        )
    except UsageLimitExceeded as exc:
        raise ExplanationFailed(
            "too_many_steps",
            "The explanation took too many steps; the table is still right.",
        ) from exc
    except UnexpectedModelBehavior as exc:
        raise ExplanationFailed(
            "invalid_output",
            "The assistant could not explain the table using only its numbers; "
            "the table is still right.",
        ) from exc
    return Explanation(result.output, ModelUsage.of(result.usage))
