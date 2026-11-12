"""Tool ``compare_best_buy``: the trade-off table for this week's produce (P1-6).

For each item the household needs, one row per price: the household's own prices from the last
30 days, the ERS national average as a labeled reference (or as the priced row when the household
has none), or a "no price" row. Each row gives the price, cups bought, usable cups after
spoilage, and cost per usable cup, with the source of every factor. The lowest cost per usable
cup is marked (the Lowest cost preset).

Spoilage (Phase 1 spec, D4):

    usable cups = min(cups bought, cups used per day x storage days)

Storage days are the midpoint of the item's FoodKeeper range; with "cook and freeze extras" on,
the midpoint of FoodKeeper's cooked-leftovers freezer range. The arithmetic is in pure functions
(``spoilage``, ``build_rows``); ``compare_best_buy`` gathers their inputs from the database.
"""

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal

from sqlmodel import Session, col, select

from app.models import (
    CatalogItem,
    DataSnapshot,
    Household,
    HouseholdItemSetting,
    PriceObservation,
    StorageReference,
    Vendor,
)
from app.tools.normalize import (
    COUNT_UNITS,
    CatalogFacts,
    NormalizeError,
    canonical_unit,
    catalog_facts,
    normalize_price,
    portion_grams,
)

# Prices count for this long (D5).
PRICE_WINDOW_DAYS = 30
DAYS_PER_WEEK = 7

RowKind = Literal["household", "ers_fallback", "ers_reference", "no_price"]


# ---------- pure math ----------


def midpoint(days_min: int | None, days_max: int | None) -> float | None:
    if days_min is None:
        return None
    return (days_min + (days_max if days_max is not None else days_min)) / 2


def spoilage(
    cups_bought: float, cups_per_week: float, storage_days: float | None
) -> tuple[float, float]:
    """(usable cups, spoiled share 0-1). No storage time means no spoilage is counted."""
    if cups_bought <= 0:
        return 0.0, 0.0
    if storage_days is None:
        return cups_bought, 0.0
    usable = min(cups_bought, cups_per_week / DAYS_PER_WEEK * storage_days)
    return usable, 1 - usable / cups_bought


@dataclass(frozen=True)
class Need:
    catalog_item_id: uuid.UUID
    cups_per_week: float


@dataclass(frozen=True)
class Storage:
    """How long the item keeps for this household, and where that number came from."""

    days: float | None
    source: str
    note: str | None = None
    snapshot_id: uuid.UUID | None = None


@dataclass(frozen=True)
class PriceInput:
    """One price, already read from the database, ready for the math."""

    kind: RowKind
    label: str  # the vendor's name, or the ERS label
    observed_on: date
    raw_text: str | None
    price_cents: float
    quantity: float
    unit: str
    piece_grams: float | None = None
    piece_note: str | None = None  # where the piece weight came from
    problem: str | None = None  # why there is no per-cup figure (a missing weight, say)
    observation_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None


@dataclass
class BestBuyRow:
    item_name: str
    kind: RowKind
    label: str
    cups_per_week: float
    observed_on: date | None = None
    raw_text: str | None = None
    price_cents: float | None = None
    quantity: float | None = None
    unit: str | None = None
    cups_bought: float | None = None
    usable_cups: float | None = None
    spoiled_share: float | None = None
    storage_days: float | None = None
    cost_per_cup_cents: float | None = None  # before spoilage
    cost_per_usable_cup_cents: float | None = None
    marked: bool = False
    notes: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    observation_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None


@dataclass
class BestBuyTable:
    on: date
    rows: list[BestBuyRow] = field(default_factory=list)
    data_snapshot_ids: set[uuid.UUID] = field(default_factory=set)

    def marked(self) -> list[BestBuyRow]:
        return [row for row in self.rows if row.marked]


ERS_REFERENCE_NOTE = "A national average for comparison, not a price at any store."
ERS_FALLBACK_NOTE = (
    "No store price in the last 30 days, so this uses the national average; "
    "the shopping list shows it under 'no store price yet'."
)


def build_rows(
    item_name: str,
    need: Need,
    facts: CatalogFacts,
    storage: Storage,
    prices: list[PriceInput],
) -> list[BestBuyRow]:
    """The table rows for one item. Pure: the same inputs always give the same rows."""
    if not prices:
        return [
            BestBuyRow(
                item_name=item_name,
                kind="no_price",
                label="no price",
                cups_per_week=need.cups_per_week,
                notes=["No price yet: add one from a store, market, or farm."],
            )
        ]
    rows = []
    for price in prices:
        row = BestBuyRow(
            item_name=item_name,
            kind=price.kind,
            label=price.label,
            cups_per_week=need.cups_per_week,
            observed_on=price.observed_on,
            raw_text=price.raw_text,
            price_cents=price.price_cents,
            quantity=price.quantity,
            unit=price.unit,
            storage_days=storage.days,
            observation_id=price.observation_id,
            vendor_id=price.vendor_id,
        )
        row.sources["price"] = (
            price.label
            if price.kind == "household"
            else (price.raw_text or price.label)
        )
        row.sources["storage"] = storage.source
        if price.kind == "ers_reference":
            row.notes.append(ERS_REFERENCE_NOTE)
        if price.kind == "ers_fallback":
            row.notes.append(ERS_FALLBACK_NOTE)
        if price.piece_note:
            row.sources["piece"] = price.piece_note
        if price.problem:
            row.notes.append(price.problem)
            rows.append(row)
            continue
        try:
            normalized = normalize_price(
                price.price_cents, price.quantity, price.unit, facts, price.piece_grams
            )
        except NormalizeError as exc:
            row.notes.append(str(exc))
            rows.append(row)
            continue
        row.sources["conversion"] = normalized.conversion_source
        if normalized.stand_in_name:
            row.notes.append(f"Conversions borrowed from {normalized.stand_in_name}.")
        usable, spoiled = spoilage(normalized.cups, need.cups_per_week, storage.days)
        row.cups_bought = normalized.cups
        row.usable_cups = usable
        row.spoiled_share = spoiled
        row.cost_per_cup_cents = normalized.cost_per_cup_cents
        row.cost_per_usable_cup_cents = (
            price.price_cents / usable if usable > 0 else None
        )
        if storage.note:
            row.notes.append(storage.note)
        rows.append(row)

    # Mark the lowest cost per usable cup among the rows that are priced for buying: the
    # household's own, or the ERS fallback. A reference row is never the pick.
    candidates = [
        r
        for r in rows
        if r.kind in ("household", "ers_fallback")
        and r.cost_per_usable_cup_cents is not None
    ]
    if candidates:
        best = min(
            candidates,
            key=lambda r: (
                r.cost_per_usable_cup_cents,
                -(r.observed_on or date.min).toordinal(),
                r.label,
            ),
        )
        best.marked = True
    return rows


# ---------- gathering the inputs ----------


def _latest_reference(
    session: Session, location: Literal["refrigerator", "freezer"]
) -> StorageReference | None:
    return session.exec(
        select(StorageReference)
        .join(DataSnapshot, col(DataSnapshot.id) == StorageReference.snapshot_id)
        .where(
            StorageReference.key == "cooked_leftovers",
            StorageReference.location == location,
        )
        .order_by(col(DataSnapshot.loaded_at).desc())
    ).first()


def _days_text(days_min: int, days_max: int) -> str:
    return f"{days_min} days" if days_min == days_max else f"{days_min}-{days_max} days"


def item_storage(session: Session, household: Household, item: CatalogItem) -> Storage:
    setting = session.exec(
        select(HouseholdItemSetting).where(
            HouseholdItemSetting.household_id == household.id,
            HouseholdItemSetting.catalog_item_id == item.id,
        )
    ).first()
    if setting is not None and setting.freeze_extras:
        freezer = _latest_reference(session, "freezer")
        fridge = _latest_reference(session, "refrigerator")
        if freezer is not None and fridge is not None:
            return Storage(
                days=midpoint(freezer.days_min, freezer.days_max),
                source=f"FoodKeeper cooked leftovers, freezer: {_days_text(freezer.days_min, freezer.days_max)}",
                note=(
                    "Cook and freeze extras: assumes you freeze what you won't eat in "
                    f"{_days_text(fridge.days_min, fridge.days_max)}."
                ),
                snapshot_id=freezer.snapshot_id,
            )
    if item.storage_days_min is None:
        return Storage(
            days=None,
            source="FoodKeeper has no storage time for this item",
            note="No storage time, so spoilage is not counted.",
        )
    high = (
        item.storage_days_max
        if item.storage_days_max is not None
        else item.storage_days_min
    )
    return Storage(
        days=midpoint(item.storage_days_min, item.storage_days_max),
        source=f"FoodKeeper, {item.storage_location or 'storage'}: {_days_text(item.storage_days_min, high)}",
        snapshot_id=item.storage_snapshot_id,
    )


def _piece(
    session: Session, item: CatalogItem, observation: PriceObservation, unit: str
) -> tuple[float | None, str | None, str | None, uuid.UUID | None]:
    """(grams, source note, problem, snapshot) for one piece of a price by count."""
    if observation.unit_grams is not None:
        how = "estimated" if observation.weight_source == "estimated" else "weighed"
        return (
            observation.unit_grams,
            f"{how} at capture: {observation.unit_grams:g} g",
            None,
            None,
        )
    try:
        choice = portion_grams(session, item, unit, observation.size)
    except NormalizeError as exc:
        return None, None, str(exc), None
    return choice.grams, choice.source, None, choice.snapshot_id


def _price_input(
    session: Session,
    item: CatalogItem,
    observation: PriceObservation,
    kind: RowKind,
    label: str,
    snapshots: set[uuid.UUID],
) -> PriceInput:
    piece = piece_note = problem = None
    try:
        unit = canonical_unit(observation.unit)
    except NormalizeError as exc:
        unit, problem = observation.unit, str(exc)
    else:
        if unit in COUNT_UNITS:
            piece, piece_note, problem, snapshot = _piece(
                session, item, observation, unit
            )
            if snapshot:
                snapshots.add(snapshot)
    # ERS rows keep the full-precision price in their raw text; cents round it.
    cents: float = observation.price_cents
    if kind != "household" and observation.raw_text:
        exact = re.search(r":\s*([0-9.]+) per pound$", observation.raw_text)
        if exact:
            cents = float(exact.group(1)) * 100
    return PriceInput(
        kind=kind,
        label=label,
        observed_on=observation.observed_on,
        raw_text=observation.raw_text,
        price_cents=cents,
        quantity=observation.quantity,
        unit=unit,
        piece_grams=piece,
        piece_note=piece_note,
        problem=problem,
        observation_id=observation.id,
        vendor_id=observation.vendor_id,
    )


def _ers_price(
    session: Session, item: CatalogItem
) -> tuple[PriceObservation, DataSnapshot] | None:
    found = session.exec(
        select(PriceObservation, DataSnapshot)
        .join(DataSnapshot, col(DataSnapshot.id) == PriceObservation.snapshot_id)
        .where(
            PriceObservation.catalog_item_id == item.id,
            col(PriceObservation.household_id).is_(None),
            DataSnapshot.source == "ers_prices",
        )
        .order_by(col(DataSnapshot.loaded_at).desc())
    ).first()
    return (found[0], found[1]) if found else None


def compare_best_buy(
    session: Session, household: Household, needs: list[Need], on: date
) -> BestBuyTable:
    """The trade-off table for the household's needs on a date. Reads only; writes nothing."""
    table = BestBuyTable(on=on)
    since = on - timedelta(days=PRICE_WINDOW_DAYS)
    for need in needs:
        if need.cups_per_week <= 0:
            raise ValueError("cups per week must be more than zero")
        item = session.get(CatalogItem, need.catalog_item_id)
        if item is None or item.kind != "produce":
            raise ValueError(f"unknown produce item {need.catalog_item_id}")
        facts = catalog_facts(session, item)
        if facts.snapshot_id:
            table.data_snapshot_ids.add(facts.snapshot_id)
        storage = item_storage(session, household, item)
        if storage.snapshot_id:
            table.data_snapshot_ids.add(storage.snapshot_id)

        own = session.exec(
            select(PriceObservation, Vendor)
            .outerjoin(Vendor, col(Vendor.id) == PriceObservation.vendor_id)
            .where(
                PriceObservation.household_id == household.id,
                PriceObservation.catalog_item_id == item.id,
                col(PriceObservation.observed_on) >= since,
                col(PriceObservation.observed_on) <= on,
            )
            .order_by(col(PriceObservation.observed_on).desc())
        ).all()
        prices = [
            _price_input(
                session,
                item,
                observation,
                "household",
                vendor.name if vendor else "a store",
                table.data_snapshot_ids,
            )
            for observation, vendor in own
        ]
        ers = _ers_price(session, item)
        if ers is not None:
            observation, snapshot = ers
            kind: RowKind = "ers_reference" if prices else "ers_fallback"
            label = f"national average (ERS {snapshot.version}), not a store price"
            prices.append(
                _price_input(
                    session, item, observation, kind, label, table.data_snapshot_ids
                )
            )
            table.data_snapshot_ids.add(snapshot.id)
        table.rows.extend(build_rows(item.name, need, facts, storage, prices))
    return table


# ---------- the shopping list ----------

NO_STORE_PRICE = "No store price yet"
NOT_COMPARED = "Not compared yet"


@dataclass
class ShoppingLine:
    item_name: str
    purchase: str  # the price as written, or "1 lb" for a national average; "" if none
    price_cents: float | None
    cups_per_week: float
    usable_cups: float | None
    note: str | None = None


@dataclass
class ShoppingGroup:
    """One stop on the trip: a store, or the items still without a store price."""

    label: str
    vendor_id: uuid.UUID | None
    lines: list[ShoppingLine] = field(default_factory=list)


def shopping_list(table: BestBuyTable) -> list[ShoppingGroup]:
    """Each item's marked row, grouped by store (by name); then the items with no store price
    (bought wherever, priced at the national average), then items the table could not compare.

    A line is one purchase as the store sells it: the amount the price was recorded for.
    """
    stores: dict[str, ShoppingGroup] = {}
    fallback = ShoppingGroup(NO_STORE_PRICE, None)
    not_compared = ShoppingGroup(NOT_COMPARED, None)
    items: dict[str, list[BestBuyRow]] = {}
    for row in table.rows:
        items.setdefault(row.item_name, []).append(row)
    for name, rows in items.items():
        best = next((row for row in rows if row.marked), None)
        if best is None:
            reason = "no price yet" if rows[0].kind == "no_price" else "no cost per cup"
            not_compared.lines.append(
                ShoppingLine(name, "", None, rows[0].cups_per_week, None, reason)
            )
            continue
        purchase = (
            best.raw_text
            if best.kind == "household" and best.raw_text
            else f"{best.quantity:g} {best.unit}"
        )
        line = ShoppingLine(
            name, purchase, best.price_cents, best.cups_per_week, best.usable_cups
        )
        if best.kind == "ers_fallback":
            fallback.lines.append(line)
        else:
            stores.setdefault(
                best.label, ShoppingGroup(best.label, best.vendor_id)
            ).lines.append(line)
    groups = [stores[label] for label in sorted(stores)]
    return groups + [g for g in (fallback, not_compared) if g.lines]


# ---------- the explanation may quote only the table's numbers ----------

_NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:  # pragma: no cover - the pattern admits only numbers
        return None


def money(cents: float | None) -> str:
    return "n/a" if cents is None else f"${cents / 100:.2f}"


def cups_text(cups: float | None) -> str:
    return "n/a" if cups is None else f"{cups:.2f}"


def percent(share: float | None) -> str:
    return "n/a" if share is None else f"{share * 100:.0f}%"


def _row_numbers(row: BestBuyRow) -> Iterable[str]:
    yield money(row.price_cents)
    yield money(row.cost_per_cup_cents)
    yield money(row.cost_per_usable_cup_cents)
    yield cups_text(row.cups_bought)
    yield cups_text(row.usable_cups)
    yield percent(row.spoiled_share)
    yield f"{row.cups_per_week:g}"
    if row.storage_days is not None:
        yield f"{row.storage_days:g}"
    if row.quantity is not None:
        yield f"{row.quantity:g}"
    if row.observed_on is not None:
        yield row.observed_on.isoformat()
    for text in (row.raw_text, row.label, *row.notes, *row.sources.values()):
        if text:
            yield text


def table_numbers(table: BestBuyTable) -> set[Decimal]:
    """Every number shown in the table, as the explanation may quote it."""
    numbers: set[Decimal] = set()
    for row in table.rows:
        for text in _row_numbers(row):
            for found in _NUMBER.findall(text):
                value = _decimal(found)
                if value is not None:
                    numbers.add(value.normalize())
    return numbers


def unsupported_numbers(text: str, table: BestBuyTable) -> list[str]:
    """Numbers in ``text`` that the table does not show: each one is a computed or made-up figure."""
    allowed = table_numbers(table)
    bad = []
    for found in _NUMBER.findall(text):
        value = _decimal(found)
        if value is not None and value.normalize() not in allowed:
            bad.append(found)
    return bad
