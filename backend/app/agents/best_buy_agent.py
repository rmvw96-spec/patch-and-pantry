"""A short, plain explanation of the best-buy table (P1-6).

The table is computed by ``compare_best_buy``; the model only explains it. It may quote numbers
exactly as the table shows them and nothing else: an output validator rejects any explanation
with a number the table does not contain (a computed ratio, a rounded figure, a made-up price),
and the model gets the offending numbers back to fix. Tone and wording rules come from the
agent guide (``app.agents.guide``); explanations with dieting words are sent back as well.
"""

from dataclasses import dataclass

from pydantic_ai import Agent, ModelRetry, RunContext, models
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from app.agents.guide import BEST_BUY_TASK, dieting_words, instructions
from app.agents.models import build_model
from app.agents.usage import ModelUsage
from app.core.config import settings
from app.tools.best_buy import (
    BestBuyTable,
    amount_text,
    money,
    percent,
    unsupported_numbers,
)

MAX_MODEL_REQUESTS = 4

INSTRUCTIONS = instructions(BEST_BUY_TASK)


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
    dieting = dieting_words(text)
    if dieting:
        raise ModelRetry(
            f"Do not use these words: {', '.join(dieting)}. This is about buying produce, "
            'not dieting; say "cups", never "servings" or "portions".'
        )
    return text


def format_table(table: BestBuyTable) -> str:
    """The table as the model sees it. The numbers use the same formats the validator accepts."""
    lines = [f"Prices compared on {table.on.isoformat()}."]
    item_notes = {note.item_name: note for note in table.items}
    current = None
    for row in table.rows:
        if row.item_name != current:
            current = row.item_name
            lines.append(
                f"\n{row.item_name} (needs {row.per_week:g} {row.measure}s a week)"
            )
            note = item_notes.get(row.item_name)
            if note is not None:
                said = [t for t in (note.storage, *note.notes) if t]
                if said:
                    lines.append(
                        f"Notes for every {row.item_name.lower()} row: {' '.join(said)}"
                    )
        mark = "MARKED BEST" if row.marked else row.kind.replace("_", " ")
        if row.kind == "no_price":
            lines.append(f"- {mark}: no price yet")
            continue
        price = f"{money(row.price_cents)} for {row.quantity:g} {row.unit}"
        detail = (
            f"{amount_text(row.amount_bought)} {row.measure}s bought, "
            f"{amount_text(row.usable_amount)} usable ({percent(row.spoiled_share)} spoils), "
            f"{money(row.cost_per_usable_cents)} per usable {row.measure}"
            if row.cost_per_usable_cents is not None
            else f"no cost per {row.measure}"
        )
        notes = f" Notes: {' '.join(row.notes)}" if row.notes else ""
        lines.append(f"- {mark}: {row.label}, {price}: {detail}.{notes}")
    if table.national_average is not None:
        lines.append(f"\n{table.national_average.note}")
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
            "The assistant could not explain the table within its rules; "
            "the table is still right.",
        ) from exc
    return Explanation(result.output, ModelUsage.of(result.usage))
