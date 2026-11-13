"""Tool ``normalize_price``: any price, in any supported unit, to grams and cost per cup (P1-4).

The math is pure (``normalize_price``): no database access, so every number is reproducible from
its inputs. ``catalog_facts`` and ``portion_grams`` read the database and hand the math its inputs.

    grams_purchased = quantity x grams per unit       (a weight unit, or one piece's weight)
    grams_edible    = grams_purchased x preparation_yield_factor
    cups            = grams_edible / (cup_equivalent_lb x 453.59237)
    cost per cup    = price / cups

A piece's weight is its weight as purchased (P1-3), so the yield factor applies the same way to
"3 lb of onions" and to "3 onions". The model never does this arithmetic; it calls this tool.

Juices are measured by volume instead (ERS prices them per pint): an item has a cup size in
pints (``cup_equivalent_pints``) and no cup size in pounds, and

    pints_purchased = quantity x pints per unit       (pint, quart, gallon, fl oz, liter...)
    cups            = pints_purchased x preparation_yield_factor / cup_equivalent_pints

The yield factor is 1 for juice ready to drink and 4 for frozen concentrate (ERS: one pint of
concentrate makes four). On a volume item "oz" means fluid ounces, and "pint" is a volume; on a
weight item "pint" stays a container sold by count (a pint of blueberries).
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import get_args

from sqlmodel import Session, col, select

from app.models import (
    CatalogItem,
    CatalogPortion,
    CountUnit,
    DataSnapshot,
    PortionSize,
)

GRAMS_PER_LB = 453.59237

# Weight units and their spellings. Grams per one unit, exact.
WEIGHT_UNITS: dict[str, float] = {
    "lb": GRAMS_PER_LB,
    "oz": GRAMS_PER_LB / 16,
    "kg": 1000.0,
    "g": 1.0,
}
# Volume units, in pints per one unit (US liquid measure; a pint is 16 fl oz, 2 cups).
LITER_PINTS = 2.113376419
VOLUME_UNITS: dict[str, float] = {
    "fl oz": 1 / 16,
    "cup": 0.5,
    "pint": 1.0,
    "quart": 2.0,
    "half gallon": 4.0,
    "gallon": 8.0,
    "ml": LITER_PINTS / 1000,
    "liter": LITER_PINTS,
}
UNIT_SPELLINGS: dict[str, str] = {
    "lbs": "lb",
    "pound": "lb",
    "pounds": "lb",
    "ounce": "oz",
    "ounces": "oz",
    "kilogram": "kg",
    "kilograms": "kg",
    "kgs": "kg",
    "gram": "g",
    "grams": "g",
    "ea": "each",
    "bunches": "bunch",
    "heads": "head",
    "ears": "ear",
    "pints": "pint",
    "pt": "pint",
    "floz": "fl oz",
    "fl. oz": "fl oz",
    "fl oz.": "fl oz",
    "fluid ounce": "fl oz",
    "fluid ounces": "fl oz",
    "cups": "cup",
    "quarts": "quart",
    "qt": "quart",
    "half gallons": "half gallon",
    "half-gallon": "half gallon",
    "gallons": "gallon",
    "gal": "gallon",
    "liters": "liter",
    "litre": "liter",
    "litres": "liter",
    "l": "liter",
    "milliliter": "ml",
    "milliliters": "ml",
    "millilitre": "ml",
    "millilitres": "ml",
}
COUNT_UNITS: frozenset[str] = frozenset(get_args(CountUnit))


class NormalizeError(ValueError):
    """A price that cannot be turned into cups. The message is written for the user."""


class UnknownUnit(NormalizeError):
    def __init__(self, unit: str) -> None:
        known = ", ".join(
            [
                *WEIGHT_UNITS,
                *sorted(COUNT_UNITS),
                *(u for u in VOLUME_UNITS if u != "pint"),
            ]
        )
        super().__init__(f"Unknown unit {unit!r}. Use one of: {known}.")
        self.unit = unit


class NeedsPortion(NormalizeError):
    """A count unit with no known weight for one piece: the agent asks the user (D3)."""

    def __init__(self, item: str, unit: str, sizes: tuple[str, ...] = ()) -> None:
        # "one sweet pepper", "one bunch of kale"
        piece = item.lower() if unit == "each" else f"{unit} of {item.lower()}"
        if sizes:
            options = (
                ", ".join(sizes[:-1]) + f", or {sizes[-1]}"
                if len(sizes) > 1
                else sizes[0]
            )
            message = f"Which size {piece}: {options}?"
        else:
            message = (
                f"How much does one {piece} weigh? Weigh it if you can; "
                "an estimate is fine and will be labeled as one."
            )
        super().__init__(message)
        self.item = item
        self.unit = unit
        self.sizes = sizes


class NoConversion(NormalizeError):
    """The item has no yield factor or cup size (for example eggplant): no per-cup figure."""

    def __init__(self, item: str) -> None:
        super().__init__(
            f"{item} has no cup conversion yet, so it has no cost per cup. "
            "It is shown by price only."
        )
        self.item = item


class InvalidAmount(NormalizeError):
    pass


class WrongMeasure(NormalizeError):
    """A volume for an item sold by weight or count, or a weight or count for a juice."""

    def __init__(self, item: str, unit: str, by_volume: bool) -> None:
        if by_volume:
            message = (
                f"{item} is measured by volume, not by {unit}. "
                "Give the amount in fl oz, pints, quarts, gallons, or liters."
            )
        else:
            message = (
                f"{item} is sold by weight or count, not by {unit}. "
                "Give the amount in pounds, ounces, or pieces."
            )
        super().__init__(message)
        self.item = item
        self.unit = unit


@dataclass(frozen=True)
class CatalogFacts:
    """The conversion facts for one item, already resolved through any stand-in."""

    name: str
    preparation_yield_factor: float | None
    cup_equivalent_lb: float | None
    conversion_source: str
    # The item whose facts are borrowed, when this item has none of its own.
    stand_in_name: str | None = None
    snapshot_id: uuid.UUID | None = None
    # Juices: pints per cup equivalent, set instead of cup_equivalent_lb.
    cup_equivalent_pints: float | None = None
    # The measure the item is compared by: "cup", or a count unit for a piece (spec P1-11).
    compared_by: str = "cup"
    # Nuts: the shell's share of the weight bought in the shell (SR28 refuse, percent).
    shell_refuse_pct: float | None = None

    @property
    def by_volume(self) -> bool:
        return self.cup_equivalent_pints is not None

    @property
    def by_piece(self) -> bool:
        return self.compared_by != "cup"


@dataclass(frozen=True)
class Normalized:
    # Weights for an item measured by weight or count; pints for a juice.
    grams_purchased: float | None
    grams_edible: float | None
    # Cups for an item compared by the cup; pieces for one compared per piece (spec P1-11).
    cups: float | None
    cost_per_cup_cents: float | None
    unit: str  # the canonical unit used, for example "lb" for "pounds"
    conversion_source: str
    stand_in_name: str | None
    pints_purchased: float | None = None
    pieces: float | None = None
    cost_per_piece_cents: float | None = None

    @property
    def amount(self) -> float | None:
        """How much was bought in the item's own measure: cups, or pieces."""
        return self.pieces if self.pieces is not None else self.cups

    @property
    def cost_per_measure_cents(self) -> float | None:
        """The cost of one cup, or of one piece."""
        if self.cost_per_piece_cents is not None:
            return self.cost_per_piece_cents
        return self.cost_per_cup_cents


def canonical_unit(unit: str) -> str:
    """Lower-case, trimmed, and with common spellings folded: "Pounds" -> "lb"."""
    text = unit.strip().lower().rstrip(".")
    text = " ".join(text.split())
    text = UNIT_SPELLINGS.get(text, text)
    if (
        text not in WEIGHT_UNITS
        and text not in COUNT_UNITS
        and text not in VOLUME_UNITS
    ):
        raise UnknownUnit(unit)
    return text


def count_unit_in(text: str) -> str | None:
    """The one count unit a line names ("Head of lettuce $3" -> "head"), or None for none or
    several. For a unit written before the item, which the reader can miss (dogfooding)."""
    words = re.findall(r"[a-z]+", text.lower())
    found = {UNIT_SPELLINGS.get(w, w) for w in words} & COUNT_UNITS
    return next(iter(found)) if len(found) == 1 else None


# One "each" of an item weighed by the ear or the head is one ear or head ("corn 5 for $6").
EACH_IS = ("ear", "head")


def piece_unit(unit: str, facts: CatalogFacts) -> str | None:
    """The count unit whose weight a price in this (canonical) unit needs, or None.

    Compared by the cup: a count price needs one piece's weight ("2 each" tomatoes). Compared
    per piece: a count price is pieces already, and a weight price needs one piece's weight to
    count them ("2 lb bag" of lemons). A juice never needs one: "pint" is its volume.
    """
    if facts.by_volume:
        return None
    if facts.by_piece:
        return facts.compared_by if unit in WEIGHT_UNITS else None
    return unit if unit in COUNT_UNITS else None


def _volume_cups(
    price_cents: float, quantity: float, canonical: str, item: CatalogFacts
) -> Normalized:
    unit = (
        "fl oz" if canonical == "oz" else canonical
    )  # on a juice, ounces are fluid ounces
    if unit not in VOLUME_UNITS:
        raise WrongMeasure(item.name, canonical, by_volume=True)
    if item.preparation_yield_factor is None or item.cup_equivalent_pints is None:
        raise NoConversion(
            item.name
        )  # pragma: no cover - by_volume means a cup size is set
    pints = quantity * VOLUME_UNITS[unit]
    cups = pints * item.preparation_yield_factor / item.cup_equivalent_pints
    return Normalized(
        grams_purchased=None,
        grams_edible=None,
        cups=cups,
        cost_per_cup_cents=price_cents / cups,
        unit=unit,
        conversion_source=item.conversion_source,
        stand_in_name=item.stand_in_name,
        pints_purchased=pints,
    )


def _pieces(
    price_cents: float,
    quantity: float,
    canonical: str,
    item: CatalogFacts,
    portion_grams: float | None,
) -> Normalized:
    """Pieces and cost per piece, for an item compared per piece (lemons, garlic heads)."""
    if canonical in COUNT_UNITS:
        # "3 for $1" is 3 lemons; "each" garlic is a head.
        if canonical not in (item.compared_by, "each"):
            raise WrongMeasure(item.name, canonical, by_volume=False)
        pieces = quantity
        grams = quantity * portion_grams if portion_grams else None
    elif canonical in WEIGHT_UNITS:
        if portion_grams is None:
            raise NeedsPortion(item.name, item.compared_by)
        if portion_grams <= 0:
            raise InvalidAmount(
                f"A piece must weigh more than zero, not {portion_grams} g."
            )
        grams = quantity * WEIGHT_UNITS[canonical]
        pieces = grams / portion_grams
    else:
        raise WrongMeasure(item.name, canonical, by_volume=False)
    return Normalized(
        grams_purchased=grams,
        grams_edible=None,
        cups=None,
        cost_per_cup_cents=None,
        unit=canonical,
        conversion_source=item.conversion_source,
        stand_in_name=item.stand_in_name,
        pieces=pieces,
        cost_per_piece_cents=price_cents / pieces,
    )


def normalize_price(
    price_cents: float,
    quantity: float,
    unit: str,
    item: CatalogFacts,
    portion_grams: float | None = None,
    in_shell: bool = False,
) -> Normalized:
    """Grams, cups, and cost per cup for one price, as written; or pieces and cost per piece
    for an item compared per piece (spec P1-11).

    ``price_cents`` is the price for the whole ``quantity`` ("3 for $5" is 500 for 3 each). It
    may be fractional for reference averages such as ERS's. ``portion_grams`` is the weight of
    one piece as purchased, required for count units (from ``portion_grams()`` or the user), and
    for weight prices of an item compared per piece. ``in_shell``: nuts weighed in the shell,
    whose shell refuse is taken out (D9); ignored for an item with no shell refuse.
    """
    if price_cents < 0:
        raise InvalidAmount(f"A price cannot be negative ({price_cents} cents).")
    if quantity <= 0:
        raise InvalidAmount(f"A quantity must be more than zero, not {quantity}.")
    canonical = canonical_unit(unit)
    if item.by_piece:
        return _pieces(price_cents, quantity, canonical, item, portion_grams)
    if item.by_volume:
        return _volume_cups(price_cents, quantity, canonical, item)
    if canonical in VOLUME_UNITS and canonical not in COUNT_UNITS:
        raise WrongMeasure(item.name, canonical, by_volume=False)
    if canonical in WEIGHT_UNITS:
        grams_per_unit = WEIGHT_UNITS[canonical]
    elif portion_grams is None:
        raise NeedsPortion(item.name, canonical)
    elif portion_grams <= 0:
        raise InvalidAmount(
            f"A piece must weigh more than zero, not {portion_grams} g."
        )
    else:
        grams_per_unit = portion_grams
    if item.preparation_yield_factor is None or item.cup_equivalent_lb is None:
        raise NoConversion(item.name)

    grams_purchased = quantity * grams_per_unit
    kernels = 1.0
    if in_shell and item.shell_refuse_pct is not None:
        kernels = 1 - item.shell_refuse_pct / 100
    grams_edible = grams_purchased * item.preparation_yield_factor * kernels
    cups = grams_edible / (item.cup_equivalent_lb * GRAMS_PER_LB)
    return Normalized(
        grams_purchased=grams_purchased,
        grams_edible=grams_edible,
        cups=cups,
        cost_per_cup_cents=price_cents / cups,
        unit=canonical,
        conversion_source=item.conversion_source,
        stand_in_name=item.stand_in_name,
    )


# ---------- database lookups that supply the inputs ----------


def catalog_facts(session: Session, item: CatalogItem) -> CatalogFacts:
    """The item's conversion facts, borrowed from its stand-in when it has none of its own."""
    source = item
    seen = {item.id}
    while (
        source.preparation_yield_factor is None and source.stand_in_for_id is not None
    ):
        stand_in = session.get(CatalogItem, source.stand_in_for_id)
        if stand_in is None or stand_in.id in seen:
            break
        seen.add(stand_in.id)
        source = stand_in
    return CatalogFacts(
        name=item.name,
        preparation_yield_factor=source.preparation_yield_factor,
        cup_equivalent_lb=source.cup_equivalent_lb,
        cup_equivalent_pints=source.cup_equivalent_pints,
        conversion_source=source.conversion_source,
        stand_in_name=source.name if source is not item else None,
        snapshot_id=source.snapshot_id,
        compared_by=item.compared_by,
        shell_refuse_pct=source.shell_refuse_pct,
    )


def measure_name(item_name: str, compared_by: str) -> str:
    """What one unit of the item's measure is called: "cup", "lemon", "head"."""
    if compared_by == "each":
        return item_name.lower()
    return compared_by


# The size used when the user names none and an item has several (owner, 2026-11-12).
DEFAULT_SIZE: PortionSize = "medium"


@dataclass(frozen=True)
class PortionChoice:
    grams: float
    source: str
    snapshot_id: uuid.UUID | None
    size: PortionSize | None = (
        None  # the size whose weight was used; None for a single weight
    )
    assumed: bool = False  # the user named no size, so DEFAULT_SIZE was used


def _portions(
    session: Session, item: CatalogItem, unit: str
) -> list[tuple[CatalogPortion, datetime | None]]:
    return list(
        session.exec(
            select(CatalogPortion, DataSnapshot.loaded_at)
            .outerjoin(DataSnapshot, col(DataSnapshot.id) == CatalogPortion.snapshot_id)
            .where(
                CatalogPortion.catalog_item_id == item.id,
                CatalogPortion.unit == unit,
            )
        ).all()
    )


def portion_grams(
    session: Session, item: CatalogItem, unit: str, size: PortionSize | None = None
) -> PortionChoice:
    """The weight of one piece as purchased, or NeedsPortion naming what to ask.

    A size the user gives is used when the item has a weight for it, and ignored otherwise (a
    bunch, or an item with one weight). With no usable size, an item's single weight is used;
    an item with several sizes uses medium, marked ``assumed`` so the page can say so
    (owner, 2026-11-12: market and store sizes differ, and asking was cumbersome). Only an
    item with sizes but no medium raises NeedsPortion listing them. Portions someone weighed
    beat loaded ones, newest first; loaded portions come from the most recently loaded snapshot.
    """
    canonical = canonical_unit(unit)
    if canonical in WEIGHT_UNITS:
        raise ValueError(f"{canonical} is a weight unit; it needs no portion")
    rows = _portions(session, item, canonical)
    if not rows and canonical == "each":
        # Corn is weighed by the ear and lettuce by the head: "each" means one of those.
        others = [unit for unit in EACH_IS if _portions(session, item, unit)]
        if len(others) == 1:
            rows = _portions(session, item, others[0])
    if not rows:
        raise NeedsPortion(item.name, canonical)
    measured = sorted(
        (p for p, _ in rows if p.measured_on is not None),
        key=lambda p: p.measured_on or 0,
        reverse=True,
    )
    loaded_at = max((at for p, at in rows if p.snapshot_id and at), default=None)
    current = measured or [
        p for p, at in rows if p.measured_on is None and at == loaded_at
    ]
    sizes = tuple(s for s in get_args(PortionSize) if any(p.size == s for p in current))
    named = [p for p in current if size is not None and p.size == size]
    unsized = [p for p in current if p.size is None]
    assumed = False
    if named:
        chosen = named[0]
    elif unsized:
        chosen = unsized[0]
    elif len(sizes) == 1 or DEFAULT_SIZE in sizes:
        default = sizes[0] if len(sizes) == 1 else DEFAULT_SIZE
        chosen = next(p for p in current if p.size == default)
        assumed = True
    else:
        raise NeedsPortion(item.name, canonical, sizes)
    return PortionChoice(
        chosen.grams, chosen.source, chosen.snapshot_id, chosen.size, assumed
    )
