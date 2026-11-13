"""Tool ``compare_best_buy``: the trade-off table for this week's produce (P1-6).

For each item the household needs, one row per price: the household's own prices from the last
30 days, the ERS national average as a labeled reference (or as the priced row when the household
has none), or a "no price" row. Each row gives the price, the amount bought, the usable amount
after spoilage, and the cost per usable amount, with the source of every factor. The lowest cost
per usable amount is marked (the Lowest cost preset).

Amounts are in the item's own measure (spec P1-11, D8): cups for most produce, pieces for items
compared per piece (a lemon, a head of garlic). Rows of one item share its measure.

Spoilage (Phase 1 spec, D4):

    usable = min(amount bought, amount used per day x storage days)

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
    CatalogFacts,
    NormalizeError,
    canonical_unit,
    catalog_facts,
    measure_name,
    normalize_price,
    piece_unit,
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
    bought: float, per_week: float, storage_days: float | None
) -> tuple[float, float]:
    """(usable amount, spoiled share 0-1), in cups or pieces alike. No storage time means no
    spoilage is counted."""
    if bought <= 0:
        return 0.0, 0.0
    if storage_days is None:
        return bought, 0.0
    usable = min(bought, per_week / DAYS_PER_WEEK * storage_days)
    return usable, 1 - usable / bought


@dataclass(frozen=True)
class Need:
    """How much of an item the household uses a week, in the item's measure (cups, pieces)."""

    catalog_item_id: uuid.UUID
    per_week: float


@dataclass(frozen=True)
class Storage:
    """How long the item keeps for this household, and where that number came from."""

    days: float | None
    source: str
    # One sentence for the notes under the table, shown once per item.
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
    problem: str | None = None  # why there is no cost figure (a missing weight, say)
    observation_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    in_shell: bool = False  # nuts weighed in the shell


@dataclass
class BestBuyRow:
    item_name: str
    kind: RowKind
    label: str
    per_week: float
    # What one unit of the amounts is: "cup", or a piece ("lemon", "head").
    measure: str = "cup"
    observed_on: date | None = None
    raw_text: str | None = None
    price_cents: float | None = None
    quantity: float | None = None
    unit: str | None = None
    amount_bought: float | None = None
    usable_amount: float | None = None
    spoiled_share: float | None = None
    storage_days: float | None = None
    cost_per_amount_cents: float | None = None  # before spoilage
    cost_per_usable_cents: float | None = None
    marked: bool = False
    notes: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    observation_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None


@dataclass
class ItemNote:
    """What applies to every row of one item, said once under the table."""

    item_name: str
    storage: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class NationalAverage:
    """Where the national-average rows come from, for the note under the table."""

    version: str
    url: str
    note: str


@dataclass
class BestBuyTable:
    on: date
    rows: list[BestBuyRow] = field(default_factory=list)
    items: list[ItemNote] = field(default_factory=list)
    national_average: NationalAverage | None = None
    data_snapshot_ids: set[uuid.UUID] = field(default_factory=set)

    def marked(self) -> list[BestBuyRow]:
        return [row for row in self.rows if row.marked]


ERS_FALLBACK_NOTE = (
    "No store price in the last 30 days, so the national average is used; "
    "the shopping list shows it under 'no store price yet'."
)


def national_average_label(version: str) -> str:
    return f"National average ({version})"


def national_average_note(version: str) -> str:
    return (
        f"National averages are USDA ERS average retail prices for {version} "
        "(Fruit and Vegetable Prices). They are for comparison, not a price at any store, "
        "and are starred only when there is no store price."
    )


def in_shell_note(shell_refuse_pct: float) -> str:
    return (
        f"Weighed in the shell: {100 - shell_refuse_pct:g}% of the weight is kernels "
        "(USDA SR28)."
    )


def item_note(
    item_name: str, facts: CatalogFacts, storage: Storage, rows: list[BestBuyRow]
) -> ItemNote:
    """The notes that apply to all of an item's rows. Pure."""
    note = ItemNote(item_name, storage.note)
    if facts.stand_in_name:
        note.notes.append(f"Conversions borrowed from {facts.stand_in_name}.")
    if any(row.kind == "ers_fallback" for row in rows):
        note.notes.append(ERS_FALLBACK_NOTE)
    return note


def build_rows(
    item_name: str,
    need: Need,
    facts: CatalogFacts,
    storage: Storage,
    prices: list[PriceInput],
) -> list[BestBuyRow]:
    """The table rows for one item. Pure: the same inputs always give the same rows."""
    measure = measure_name(item_name, facts.compared_by)
    if not prices:
        return [
            BestBuyRow(
                item_name=item_name,
                kind="no_price",
                label="no price",
                per_week=need.per_week,
                measure=measure,
                notes=["No price yet: add one from a store, market, or farm."],
            )
        ]
    rows = []
    for price in prices:
        row = BestBuyRow(
            item_name=item_name,
            kind=price.kind,
            label=price.label,
            per_week=need.per_week,
            measure=measure,
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
        if price.piece_note:
            row.sources["piece"] = price.piece_note
        if price.problem:
            row.notes.append(price.problem)
            rows.append(row)
            continue
        try:
            normalized = normalize_price(
                price.price_cents,
                price.quantity,
                price.unit,
                facts,
                price.piece_grams,
                in_shell=price.in_shell,
            )
        except NormalizeError as exc:
            row.notes.append(str(exc))
            rows.append(row)
            continue
        bought = normalized.amount
        if bought is None:  # pragma: no cover - normalize_price gives cups or pieces
            raise RuntimeError(f"{item_name}: no amount from normalize_price")
        row.sources["conversion"] = normalized.conversion_source
        if price.in_shell and facts.shell_refuse_pct is not None:
            row.notes.append(in_shell_note(facts.shell_refuse_pct))
        usable, spoiled = spoilage(bought, need.per_week, storage.days)
        row.amount_bought = bought
        row.usable_amount = usable
        row.spoiled_share = spoiled
        row.cost_per_amount_cents = normalized.cost_per_measure_cents
        row.cost_per_usable_cents = price.price_cents / usable if usable > 0 else None
        rows.append(row)

    # Mark the lowest cost per usable amount among the rows that are priced for buying: the
    # household's own, or the ERS fallback. A reference row is never the pick.
    candidates = [
        r
        for r in rows
        if r.kind in ("household", "ers_fallback")
        and r.cost_per_usable_cents is not None
    ]
    if candidates:
        best = min(
            candidates,
            key=lambda r: (
                r.cost_per_usable_cents,
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
            days = midpoint(freezer.days_min, freezer.days_max)
            frozen = _days_text(freezer.days_min, freezer.days_max)
            return Storage(
                days=days,
                source=f"FoodKeeper cooked leftovers, freezer: {frozen}",
                note=(
                    f"Cook and freeze extras: cooked leftovers keep {frozen} frozen "
                    f"(FoodKeeper), counted as {days:g} days. Assumes you freeze what you "
                    f"won't eat in {_days_text(fridge.days_min, fridge.days_max)}."
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
    days = midpoint(item.storage_days_min, item.storage_days_max)
    where = item.storage_location or "storage"
    kept = _days_text(item.storage_days_min, high)
    # Storage borrowed from another food's entry says so ("as walnuts"; owner, 2026-11-13).
    origin = f"FoodKeeper, as {item.storage_as}" if item.storage_as else "FoodKeeper"
    return Storage(
        days=days,
        source=f"{origin}, {where}: {kept}",
        note=f"Keeps {kept} in the {where} ({origin}), counted as {days:g} days.",
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
    source = (
        f"{choice.source} (no size given: medium assumed)"
        if choice.assumed
        else choice.source
    )
    return choice.grams, source, None, choice.snapshot_id


def _price_input(
    session: Session,
    item: CatalogItem,
    facts: CatalogFacts,
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
        counted = piece_unit(unit, facts)
        if counted is not None:
            piece, piece_note, problem, snapshot = _piece(
                session, item, observation, counted
            )
            if snapshot:
                snapshots.add(snapshot)
    # ERS rows keep the full-precision price in their raw text; cents round it.
    cents: float = observation.price_cents
    if kind != "household" and observation.raw_text:
        exact = re.search(r":\s*([0-9.]+) per (?:pound|pint)\b", observation.raw_text)
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
        in_shell=observation.in_shell,
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
        if need.per_week <= 0:
            raise ValueError("the amount a week must be more than zero")
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
                facts,
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
            label = national_average_label(snapshot.version)
            if table.national_average is None:
                table.national_average = NationalAverage(
                    snapshot.version,
                    snapshot.url,
                    national_average_note(snapshot.version),
                )
            prices.append(
                _price_input(
                    session,
                    item,
                    facts,
                    observation,
                    kind,
                    label,
                    table.data_snapshot_ids,
                )
            )
            table.data_snapshot_ids.add(snapshot.id)
        rows = build_rows(item.name, need, facts, storage, prices)
        table.rows.extend(rows)
        table.items.append(item_note(item.name, facts, storage, rows))
    return table


# ---------- the shopping list ----------

NO_STORE_PRICE = "No store price yet"
NOT_COMPARED = "Not compared yet"


@dataclass
class ShoppingLine:
    item_name: str
    purchase: str  # the price as written, or "1 lb" for a national average; "" if none
    price_cents: float | None
    per_week: float
    usable_amount: float | None
    note: str | None = None
    measure: str = "cup"  # what the amounts count: cups, or pieces ("lemon", "head")


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
            first = rows[0]
            reason = (
                "no price yet"
                if first.kind == "no_price"
                else f"no cost per {first.measure}"
            )
            not_compared.lines.append(
                ShoppingLine(
                    name, "", None, first.per_week, None, reason, first.measure
                )
            )
            continue
        purchase = (
            best.raw_text
            if best.kind == "household" and best.raw_text
            else f"{best.quantity:g} {best.unit}"
        )
        line = ShoppingLine(
            name,
            purchase,
            best.price_cents,
            best.per_week,
            best.usable_amount,
            measure=best.measure,
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


def amount_text(amount: float | None) -> str:
    return "n/a" if amount is None else f"{amount:.2f}"


def percent(share: float | None) -> str:
    return "n/a" if share is None else f"{share * 100:.0f}%"


def _row_numbers(row: BestBuyRow) -> Iterable[str]:
    yield money(row.price_cents)
    yield money(row.cost_per_amount_cents)
    yield money(row.cost_per_usable_cents)
    yield amount_text(row.amount_bought)
    yield amount_text(row.usable_amount)
    yield percent(row.spoiled_share)
    yield f"{row.per_week:g}"
    if row.storage_days is not None:
        yield f"{row.storage_days:g}"
    if row.quantity is not None:
        yield f"{row.quantity:g}"
    if row.observed_on is not None:
        yield row.observed_on.isoformat()
    for text in (row.raw_text, row.label, *row.notes, *row.sources.values()):
        if text:
            yield text


def _shown_texts(table: BestBuyTable) -> Iterable[str]:
    for row in table.rows:
        yield from _row_numbers(row)
    for note in table.items:
        yield from (t for t in (note.storage, *note.notes) if t)
    if table.national_average is not None:
        yield table.national_average.note


def table_numbers(table: BestBuyTable) -> set[Decimal]:
    """Every number shown in the table and its notes, as the explanation may quote it."""
    numbers: set[Decimal] = set()
    for text in _shown_texts(table):
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
