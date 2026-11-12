"""Tool ``normalize_price``: any price, in any supported unit, to grams and cost per cup (P1-4).

The math is pure (``normalize_price``): no database access, so every number is reproducible from
its inputs. ``catalog_facts`` and ``portion_grams`` read the database and hand the math its inputs.

    grams_purchased = quantity x grams per unit       (a weight unit, or one piece's weight)
    grams_edible    = grams_purchased x preparation_yield_factor
    cups            = grams_edible / (cup_equivalent_lb x 453.59237)
    cost per cup    = price / cups

A piece's weight is its weight as purchased (P1-3), so the yield factor applies the same way to
"3 lb of onions" and to "3 onions". The model never does this arithmetic; it calls this tool.
"""

import uuid
from dataclasses import dataclass
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
}
COUNT_UNITS: frozenset[str] = frozenset(get_args(CountUnit))


class NormalizeError(ValueError):
    """A price that cannot be turned into cups. The message is written for the user."""


class UnknownUnit(NormalizeError):
    def __init__(self, unit: str) -> None:
        known = ", ".join([*WEIGHT_UNITS, *sorted(COUNT_UNITS)])
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


@dataclass(frozen=True)
class Normalized:
    grams_purchased: float
    grams_edible: float
    cups: float
    cost_per_cup_cents: float
    unit: str  # the canonical unit used, for example "lb" for "pounds"
    conversion_source: str
    stand_in_name: str | None


def canonical_unit(unit: str) -> str:
    """Lower-case, trimmed, and with common spellings folded: "Pounds" -> "lb"."""
    text = unit.strip().lower().rstrip(".")
    text = UNIT_SPELLINGS.get(text, text)
    if text not in WEIGHT_UNITS and text not in COUNT_UNITS:
        raise UnknownUnit(unit)
    return text


def normalize_price(
    price_cents: float,
    quantity: float,
    unit: str,
    item: CatalogFacts,
    portion_grams: float | None = None,
) -> Normalized:
    """Grams, cups, and cost per cup for one price, as written.

    ``price_cents`` is the price for the whole ``quantity`` ("3 for $5" is 500 for 3 each). It
    may be fractional for reference averages such as ERS's. ``portion_grams`` is the weight of
    one piece as purchased, required for count units (from ``portion_grams()`` or the user).
    """
    if price_cents < 0:
        raise InvalidAmount(f"A price cannot be negative ({price_cents} cents).")
    if quantity <= 0:
        raise InvalidAmount(f"A quantity must be more than zero, not {quantity}.")
    canonical = canonical_unit(unit)
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
    grams_edible = grams_purchased * item.preparation_yield_factor
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
        conversion_source=source.conversion_source,
        stand_in_name=source.name if source is not item else None,
        snapshot_id=source.snapshot_id,
    )


@dataclass(frozen=True)
class PortionChoice:
    grams: float
    source: str
    snapshot_id: uuid.UUID | None


def portion_grams(
    session: Session, item: CatalogItem, unit: str, size: PortionSize | None = None
) -> PortionChoice:
    """The weight of one piece as purchased, or NeedsPortion naming what to ask.

    A size the user did not give is never assumed: an item with several sizes raises
    NeedsPortion listing them. Portions someone weighed beat loaded ones, newest first; loaded
    portions come from the most recently loaded snapshot.
    """
    canonical = canonical_unit(unit)
    if canonical in WEIGHT_UNITS:
        raise ValueError(f"{canonical} is a weight unit; it needs no portion")
    rows = session.exec(
        select(CatalogPortion, DataSnapshot.loaded_at)
        .outerjoin(DataSnapshot, col(DataSnapshot.id) == CatalogPortion.snapshot_id)
        .where(
            CatalogPortion.catalog_item_id == item.id,
            CatalogPortion.unit == canonical,
        )
    ).all()
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
    if size is None:
        unsized = [p for p in current if p.size is None]
        if unsized:
            chosen = unsized[0]
        elif len(sizes) == 1:
            chosen = next(p for p in current if p.size == sizes[0])
        else:
            raise NeedsPortion(item.name, canonical, sizes)
    else:
        matches = [p for p in current if p.size == size]
        if not matches:
            raise NeedsPortion(item.name, canonical, sizes)
        chosen = matches[0]
    return PortionChoice(chosen.grams, chosen.source, chosen.snapshot_id)
