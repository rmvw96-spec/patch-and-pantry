# ruff: noqa: T201  -- a command-line script: printing is its output
"""Score the regex reader and a live model on a typed list with a hand-checked answer key.

Three readings are scored: the rules alone ("regex"), the model on every line ("model"), and
rules first with the model reading only the lines the rules are not sure of ("rules+m", as the
app reads lists). Each gives the same fields (item words, quantity, unit, price), and each
reading then goes through the steps the capture code applies (a missing quantity is 1, a unit
written before the item is found in the line, a per-unit price is multiplied out), so two
readings that lead to the same price count as the same. Item words are compared after lower-casing and
dropping punctuation; the catalog match itself is not part of the score.

The answer key is a CSV next to the list (``list.txt`` -> ``list.csv``): one row per acceptable
reading, several rows for a line when more than one reading is fine. ``size`` blank means none
written; ``*`` means any.

Manual only: unless --regex-only is given it calls a live model, which costs money. From backend/,
with FOUNDRY_ENDPOINT and FOUNDRY_API_KEY in ../.env.local:

    uv run python scripts/compare_readers.py evals/lists/shoals-farm-market.txt --runs 3
    uv run python scripts/compare_readers.py evals/lists/shoals-farm-market.txt --regex-only
"""

import argparse
import asyncio
import csv
import math
import re
import sys
import time
from dataclasses import dataclass, fields
from pathlib import Path

from app.agents.capture_agent import (
    ExtractedLine,
    read_lines,
    split_lines,
    total_cents,
)
from app.agents.models import build_model
from app.core.config import CaptureReader
from app.tools.normalize import UnknownUnit, canonical_unit, count_unit_in
from app.tools.read_line import read_list_line, read_price

DEFAULT_SPEC = "foundry:deepseek-v3.2"
FIELDS = ("item", "quantity", "unit", "price_cents", "size")


@dataclass(frozen=True)
class Reading:
    """What the capture code ends up with for one line, before catalog matching."""

    item: str | None
    quantity: float | None
    unit: str | None
    price_cents: int | None
    size: str | None


def words(text: str | None) -> str | None:
    if not text:
        return None
    text = text.lower().replace("’", "'")
    return " ".join(re.sub(r"[^a-z0-9' ]", " ", text).split()) or None


def unit_of(written: str | None) -> str | None:
    if not written:
        return None
    try:
        return canonical_unit(written)
    except UnknownUnit:
        return " ".join(written.lower().split())


def reading_of(raw: str, line: ExtractedLine | None) -> Reading | None:
    """The same steps as ``capture_agent._process``, without the catalog."""
    if line is None or not line.is_price:
        return None
    quantity = 1.0 if line.quantity is None else line.quantity
    written = read_price(line.price_text)
    cents = written.cents
    per_unit = line.price_is_per_unit or written.per is not None
    if cents is not None and per_unit and quantity > 0 and quantity != 1:
        cents = total_cents(cents, quantity)
    return Reading(
        item=words(line.item_text),
        quantity=quantity,
        unit=unit_of(line.unit or written.per or count_unit_in(raw)),
        price_cents=cents,
        size=line.size,
    )


@dataclass(frozen=True)
class Expected:
    reading: Reading
    any_size: bool


def load_key(path: Path) -> dict[int, list[Expected]]:
    key: dict[int, list[Expected]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            expected = Expected(
                Reading(
                    item=words(row["item_text"]),
                    quantity=float(row["quantity"]),
                    unit=unit_of(row["unit"]),
                    price_cents=int(row["price_cents"]),
                    size=row["size"] if row["size"] not in ("", "*") else None,
                ),
                any_size=row["size"] == "*",
            )
            key.setdefault(int(row["line"]), []).append(expected)
    return key


def same(field: str, got: Reading, want: Expected) -> bool:
    if field == "size" and want.any_size:
        return True
    a, b = getattr(got, field), getattr(want.reading, field)
    if field == "quantity" and a is not None and b is not None:
        return math.isclose(a, b)
    return bool(a == b)


def score_line(got: Reading | None, wanted: list[Expected]) -> set[str]:
    """The fields that are right, against the closest acceptable reading."""
    if got is None:
        return set()
    return max(({f for f in FIELDS if same(f, got, want)} for want in wanted), key=len)


def show(reading: Reading | None) -> str:
    if reading is None:
        return "(not read as a price)"
    size = f" {reading.size}" if reading.size else ""
    price = "?" if reading.price_cents is None else f"${reading.price_cents / 100:.2f}"
    return f"{reading.item!r} {reading.quantity:g}{size} {reading.unit} = {price}"


@dataclass
class Run:
    name: str
    readings: dict[int, Reading | None]
    raw: dict[int, ExtractedLine | None]
    seconds: float = 0.0
    tokens: str = ""


def regex_run(lines: list[str]) -> Run:
    readings: dict[int, Reading | None] = {}
    read: dict[int, ExtractedLine | None] = {}
    for n, raw in enumerate(lines, start=1):
        read[n] = ExtractedLine.model_validate({"line": n, **read_list_line(raw)})
        readings[n] = reading_of(raw, read[n])
    return Run("regex", readings, read)


async def model_run(
    lines: list[str], spec: str, index: int, reader: CaptureReader
) -> Run:
    """The model reads every line (``model``), or only those the rules are not sure of
    (``rules_first``, as the app reads lists)."""
    model = build_model(spec, role="capture")
    start = time.monotonic()
    extracted, usage, _asked = await read_lines(lines, model, reader)
    readings = {
        n: reading_of(raw, extracted.get(n)) for n, raw in enumerate(lines, start=1)
    }
    tokens = ", ".join(
        f"{f.name.removesuffix('_tokens')} {getattr(usage, f.name)}"
        for f in fields(usage)
        if f.name.endswith("tokens") and getattr(usage, f.name)
    )
    raw = {n: extracted.get(n) for n in readings}
    name = "model" if reader == "model" else "rules+m"
    return Run(f"{name} {index}", readings, raw, time.monotonic() - start, tokens)


def report(
    lines: list[str],
    key: dict[int, list[Expected]],
    runs: list[Run],
    verbose: bool = False,
) -> None:
    print(f"{len(lines)} lines\n")
    print(
        f"{'reader':<9} {'lines right':>12}  " + "  ".join(f"{f:>11}" for f in FIELDS)
    )
    for run in runs:
        right = {n: score_line(run.readings[n], key[n]) for n in key}
        whole = sum(len(r) == len(FIELDS) for r in right.values())
        per_field = "  ".join(
            f"{sum(f in r for r in right.values()):>11}" for f in FIELDS
        )
        extra = f"  ({run.seconds:.1f} s; {run.tokens})" if run.seconds else ""
        print(f"{run.name:<9} {whole:>8}/{len(key):<3}  {per_field}{extra}")

    print("\nLines any reader got wrong (wrong fields in brackets):")
    for n, raw in enumerate(lines, start=1):
        misses = []
        for run in runs:
            fine = score_line(run.readings[n], key[n])
            if len(fine) < len(FIELDS):
                wrong = ", ".join(f for f in FIELDS if f not in fine)
                misses.append(f"    {run.name:<8} [{wrong}] {show(run.readings[n])}")
                if verbose:
                    read = run.raw[n]
                    as_read = read.model_dump(exclude={"line"}) if read else None
                    misses.append(f"    {'':<8} read as {as_read}")
        if misses:
            print(f"\n  {n}: {raw}")
            print(f"    {'expected':<8} {show(key[n][0].reading)}")
            print("\n".join(misses))


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "list", type=Path, help="a typed list; its key is the .csv beside it"
    )
    parser.add_argument("--model", default=DEFAULT_SPEC)
    parser.add_argument("--runs", type=int, default=1, help="model runs (it can vary)")
    parser.add_argument("--regex-only", action="store_true", help="no live model call")
    parser.add_argument(
        "--verbose", action="store_true", help="show the fields read for each miss"
    )
    args = parser.parse_args()

    lines = split_lines(args.list.read_text(encoding="utf-8"))
    key = load_key(args.list.with_suffix(".csv"))
    missing = set(range(1, len(lines) + 1)) - set(key)
    if missing:
        print(
            f"The answer key has no rows for lines {sorted(missing)}", file=sys.stderr
        )
        return 2
    runs = [regex_run(lines)]
    if not args.regex_only:
        if not args.model.startswith("foundry:"):
            print(
                f"Expected a foundry: model spec, got {args.model!r}", file=sys.stderr
            )
            return 2
        for i in range(1, args.runs + 1):
            runs.append(await model_run(lines, args.model, i, "model"))
        for i in range(1, args.runs + 1):
            runs.append(await model_run(lines, args.model, i, "rules_first"))
    report(lines, key, runs, args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
