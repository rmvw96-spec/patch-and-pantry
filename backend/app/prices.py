"""The household's price book (P1-7): saving confirmed drafts, and reading prices back.

Drafts from typed-list capture are never saved by the agent. The user confirms them on the Prices
page, answering any weight or size question first, and this module saves them as private price
observations. Every number comes from the deterministic tools: a weight answer ("0.6 lb") is
converted with normalize's weight units, and cost per cup with normalize_price.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, col, or_, select

from app.models import (
    CatalogItem,
    Household,
    PortionSize,
    PriceObservation,
    PriceSourceType,
    Vendor,
    VendorType,
    WeightSource,
)
from app.tools.catalog import remember_alias
from app.tools.normalize import (
    WEIGHT_UNITS,
    NormalizeError,
    WrongMeasure,
    canonical_unit,
    catalog_facts,
    measure_name,
    normalize_price,
    piece_unit,
    portion_grams,
)

MAX_PRICE_CENTS = 1_000_000  # 10,000 dollars: a typo guard, like the household budget's
MAX_ENTRIES = 100

# A vendor's type, as the price's source type (suppliers sell like stores).
SOURCE_TYPE: dict[VendorType, PriceSourceType] = {
    "store": "store",
    "farmers_market": "farmers_market",
    "farm_share": "farm_share",
    "local_grower": "local_grower",
    "supplier": "store",
}


class PriceError(ValueError):
    """A price that cannot be saved as given. The message is written for the user."""


# ---------- requests ----------


class PieceWeight(BaseModel):
    """The answer to "How much does one bunch weigh?": an amount in a weight unit."""

    model_config = ConfigDict(extra="forbid")

    amount: float = Field(gt=0, le=100_000)
    unit: Literal["lb", "oz", "kg", "g"]
    source: WeightSource


class PriceEntry(BaseModel):
    """One confirmed line: what the draft read, plus the user's answers."""

    model_config = ConfigDict(extra="forbid")

    raw_text: str = Field(min_length=1, max_length=500)
    catalog_item_id: uuid.UUID
    quantity: float = Field(gt=0, le=10_000)
    unit: str = Field(min_length=1, max_length=20)
    size: PortionSize | None = None
    price_cents: int = Field(ge=0, le=MAX_PRICE_CENTS)
    piece_weight: PieceWeight | None = None
    # Nuts bought in the shell (spec P1-11): ignored for an item with no shell.
    in_shell: bool = False
    # Set when the user confirms a fuzzy match: the typed words become a household alias.
    remember_text: str | None = Field(default=None, max_length=200)


class PricesCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_id: uuid.UUID
    observed_on: date
    entries: list[PriceEntry] = Field(min_length=1, max_length=MAX_ENTRIES)


class PricesDelete(BaseModel):
    """Prices to delete together: all of them, or none if any is not yours."""

    model_config = ConfigDict(extra="forbid")

    ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)


class PriceUpdate(BaseModel):
    """Edit a saved price. The item and the line as typed stay; the rest can change."""

    model_config = ConfigDict(extra="forbid")

    vendor_id: uuid.UUID
    observed_on: date
    quantity: float = Field(gt=0, le=10_000)
    unit: str = Field(min_length=1, max_length=20)
    size: PortionSize | None = None
    price_cents: int = Field(ge=0, le=MAX_PRICE_CENTS)
    piece_weight: PieceWeight | None = None
    in_shell: bool = False


# ---------- responses ----------


class PricePublic(BaseModel):
    id: uuid.UUID
    catalog_item_id: uuid.UUID
    item_name: str
    vendor_id: uuid.UUID | None
    vendor_name: str | None
    observed_on: date
    raw_text: str | None
    quantity: float
    unit: str
    size: PortionSize | None
    price_cents: int
    unit_grams: float | None
    weight_source: WeightSource | None
    cost_per_cup_cents: float | None
    note: str | None  # why there is no cost figure, when there is none
    # What the item is compared by (spec P1-11): "cup", or a piece ("lemon", "head"), and the
    # cost of one piece for an item compared per piece.
    measure: str = "cup"
    cost_per_piece_cents: float | None = None
    in_shell: bool = False
    # Set when saving: the line was already saved for this store and day, and was updated.
    replaced: bool = False


# ---------- checks and conversions ----------


def own_vendor(session: Session, household: Household, vendor_id: uuid.UUID) -> Vendor:
    vendor = session.get(Vendor, vendor_id)
    if vendor is None or vendor.household_id != household.id:
        raise PriceError("Choose one of your own stores.")
    return vendor


def visible_item(
    session: Session, household: Household, item_id: uuid.UUID
) -> CatalogItem:
    """A produce item the household can see: national, its region's, or its own."""
    item = session.exec(
        select(CatalogItem).where(
            CatalogItem.id == item_id,
            CatalogItem.kind == "produce",
            or_(
                col(CatalogItem.scope) == "national",
                col(CatalogItem.region_id) == household.region_id,
                col(CatalogItem.household_id) == household.id,
            ),
        )
    ).first()
    if item is None:
        raise PriceError("Unknown produce item.")
    return item


def piece_grams(weight: PieceWeight) -> float:
    return weight.amount * WEIGHT_UNITS[weight.unit]


@dataclass(frozen=True)
class Checked:
    unit: str
    unit_grams: float | None
    weight_source: WeightSource | None
    grams_edible: float | None
    in_shell: bool = False


def _check(
    session: Session,
    item: CatalogItem,
    quantity: float,
    unit: str,
    size: PortionSize | None,
    price_cents: int,
    weight: PieceWeight | None,
    in_shell: bool = False,
) -> Checked:
    """The unit spelled one way, the piece weight, and edible grams; or why it cannot be saved."""
    try:
        canonical = canonical_unit(unit)
    except NormalizeError as exc:
        raise PriceError(str(exc)) from exc
    facts = catalog_facts(session, item)
    in_shell = in_shell and facts.shell_refuse_pct is not None
    counted = piece_unit(canonical, facts)
    unit_grams = piece_grams(weight) if weight is not None else None
    if counted is None:
        unit_grams = None  # this price needs no piece weight
    piece = unit_grams
    if counted is not None and piece is None:
        try:
            piece = portion_grams(session, item, counted, size).grams
        except NormalizeError as exc:
            raise PriceError(str(exc)) from exc
    try:
        normalized = normalize_price(
            price_cents, quantity, canonical, facts, piece, in_shell=in_shell
        )
        grams_edible: float | None = normalized.grams_edible
    except WrongMeasure as exc:
        raise PriceError(str(exc)) from exc
    except NormalizeError:
        grams_edible = None  # no cup conversion yet: saved, shown by price only
    return Checked(
        unit=canonical,
        unit_grams=unit_grams,
        weight_source=weight.source if unit_grams is not None and weight else None,
        grams_edible=grams_edible,
        in_shell=in_shell,
    )


# ---------- saving and reading ----------


def line_text(raw: str) -> str:
    """A typed line as stored and keyed: trimmed, runs of spaces collapsed."""
    return " ".join(raw.split())


@dataclass(frozen=True)
class Saved:
    observation: PriceObservation
    replaced: bool  # an earlier save of the same line, store, and day was updated


def save_prices(
    session: Session, household: Household, prices: PricesCreate
) -> list[Saved]:
    """Save confirmed drafts, all or none. Flushes; the caller commits.

    One price per line as typed, per store, per day (owner, 2026-11-12): saving a line again
    updates its price, so re-saving a list, or saving a corrected reading, adds no copies.
    """
    vendor = own_vendor(session, household, prices.vendor_id)
    saved = []
    for entry in prices.entries:
        item = visible_item(session, household, entry.catalog_item_id)
        checked = _check(
            session,
            item,
            entry.quantity,
            entry.unit,
            entry.size,
            entry.price_cents,
            entry.piece_weight,
            entry.in_shell,
        )
        raw = line_text(entry.raw_text)
        existing = session.exec(
            select(PriceObservation).where(
                PriceObservation.household_id == household.id,
                PriceObservation.vendor_id == vendor.id,
                PriceObservation.observed_on == prices.observed_on,
                PriceObservation.raw_text == raw,
            )
        ).first()
        observation = existing or PriceObservation(
            household_id=household.id,
            vendor_id=vendor.id,
            observed_on=prices.observed_on,
            raw_text=raw,
            captured_via="typed",
            source_type=SOURCE_TYPE[vendor.vendor_type],
            region_id=household.region_id,
            visibility="private",
            catalog_item_id=item.id,
            price_cents=entry.price_cents,
            quantity=entry.quantity,
            unit=checked.unit,
        )
        observation.catalog_item_id = item.id
        observation.price_cents = entry.price_cents
        observation.quantity = entry.quantity
        observation.unit = checked.unit
        observation.size = entry.size
        observation.unit_grams = checked.unit_grams
        observation.weight_source = checked.weight_source
        observation.grams_edible = checked.grams_edible
        observation.in_shell = checked.in_shell
        session.add(observation)
        if entry.remember_text:
            remember_alias(session, household, entry.remember_text, item)
        saved.append(Saved(observation, replaced=existing is not None))
        session.flush()  # a line listed twice in one save updates the first
    return saved


def own_price(
    session: Session, household: Household, price_id: uuid.UUID
) -> PriceObservation:
    observation = session.get(PriceObservation, price_id)
    if observation is None or observation.household_id != household.id:
        raise LookupError("Price not found")
    return observation


def update_price(
    session: Session, household: Household, price_id: uuid.UUID, update: PriceUpdate
) -> PriceObservation:
    observation = own_price(session, household, price_id)
    vendor = own_vendor(session, household, update.vendor_id)
    item = visible_item(session, household, observation.catalog_item_id)
    # A weight given at capture stays unless a new one replaces it.
    weight = update.piece_weight
    if weight is None and observation.unit_grams and observation.weight_source:
        weight = PieceWeight(
            amount=observation.unit_grams, unit="g", source=observation.weight_source
        )
    checked = _check(
        session,
        item,
        update.quantity,
        update.unit,
        update.size,
        update.price_cents,
        weight,
        update.in_shell,
    )
    clash = session.exec(
        select(PriceObservation.id).where(
            PriceObservation.household_id == household.id,
            PriceObservation.vendor_id == vendor.id,
            PriceObservation.observed_on == update.observed_on,
            PriceObservation.raw_text == observation.raw_text,
            PriceObservation.id != observation.id,
        )
    ).first()
    if clash is not None:
        raise PriceError(
            "You already have this line for that store and day. Edit or delete that one."
        )
    observation.vendor_id = vendor.id
    observation.source_type = SOURCE_TYPE[vendor.vendor_type]
    observation.observed_on = update.observed_on
    observation.quantity = update.quantity
    observation.unit = checked.unit
    observation.size = update.size
    observation.price_cents = update.price_cents
    observation.unit_grams = checked.unit_grams
    observation.weight_source = checked.weight_source
    observation.grams_edible = checked.grams_edible
    observation.in_shell = checked.in_shell
    session.add(observation)
    session.flush()
    return observation


@dataclass(frozen=True)
class Costs:
    """A saved price's cost per cup, or per piece for an item compared per piece."""

    measure: str
    per_cup_cents: float | None = None
    per_piece_cents: float | None = None
    note: str | None = None  # why there is no cost figure


def _costs(session: Session, item: CatalogItem, observation: PriceObservation) -> Costs:
    facts = catalog_facts(session, item)
    measure = measure_name(item.name, facts.compared_by)
    try:
        unit = canonical_unit(observation.unit)
        counted = piece_unit(unit, facts)
        piece = observation.unit_grams
        if counted is not None and piece is None:
            piece = portion_grams(session, item, counted, observation.size).grams
        normalized = normalize_price(
            observation.price_cents,
            observation.quantity,
            unit,
            facts,
            piece,
            in_shell=observation.in_shell,
        )
    except NormalizeError as exc:
        return Costs(measure, note=str(exc))
    return Costs(
        measure, normalized.cost_per_cup_cents, normalized.cost_per_piece_cents
    )


def delete_prices(session: Session, household: Household, ids: list[uuid.UUID]) -> int:
    """Delete the household's prices with these ids, all or none. Flushes; caller commits."""
    wanted = set(ids)
    found = list(
        session.exec(
            select(PriceObservation).where(
                col(PriceObservation.id).in_(wanted),
                PriceObservation.household_id == household.id,
            )
        )
    )
    if len(found) != len(wanted):
        raise LookupError("Price not found")
    for observation in found:
        session.delete(observation)
    session.flush()
    return len(found)


def price_book(
    session: Session, household: Household, limit: int = 200
) -> list[PricePublic]:
    """The household's prices, newest first, each with its cost per cup."""
    rows = session.exec(
        select(PriceObservation, CatalogItem, Vendor)
        .join(CatalogItem, col(CatalogItem.id) == PriceObservation.catalog_item_id)
        .outerjoin(Vendor, col(Vendor.id) == PriceObservation.vendor_id)
        .where(PriceObservation.household_id == household.id)
        .order_by(
            col(PriceObservation.observed_on).desc(),
            col(PriceObservation.created_at).desc(),
        )
        .limit(limit)
    ).all()
    book = []
    for observation, item, vendor in rows:
        costs = _costs(session, item, observation)
        book.append(
            PricePublic(
                id=observation.id,
                catalog_item_id=item.id,
                item_name=item.name,
                vendor_id=observation.vendor_id,
                vendor_name=vendor.name if vendor else None,
                observed_on=observation.observed_on,
                raw_text=observation.raw_text,
                quantity=observation.quantity,
                unit=observation.unit,
                size=observation.size,
                price_cents=observation.price_cents,
                unit_grams=observation.unit_grams,
                weight_source=observation.weight_source,
                cost_per_cup_cents=costs.per_cup_cents,
                note=costs.note,
                measure=costs.measure,
                cost_per_piece_cents=costs.per_piece_cents,
                in_shell=observation.in_shell,
            )
        )
    return book
