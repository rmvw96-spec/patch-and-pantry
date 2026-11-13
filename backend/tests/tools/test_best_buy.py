"""Tool compare_best_buy and its explanation (Phase 1, P1-6).

Pure math first (spoilage, rows, the marked row), then the database lookups against the known
test catalog (tests/catalog_fixtures.py), then the explanation's numbers check.
"""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from pydantic_ai import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlmodel import Session, delete

from app.agents.best_buy_agent import (
    ExplanationFailed,
    explain_best_buy,
    format_table,
)
from app.core.config import settings
from app.models import (
    CatalogItem,
    DataSnapshot,
    Household,
    HouseholdItemSetting,
    PriceObservation,
    StorageReference,
    Vendor,
)
from app.tools.best_buy import (
    BestBuyRow,
    BestBuyTable,
    Need,
    PriceInput,
    Storage,
    build_rows,
    compare_best_buy,
    item_note,
    midpoint,
    shopping_list,
    spoilage,
    unsupported_numbers,
)
from app.tools.normalize import CatalogFacts
from tests.catalog_fixtures import make_household

Catalog = dict[str, CatalogItem]
ON = date(2026, 11, 11)
ONION = CatalogFacts("Onion", 0.9, 0.3527, "ERS: Onions (Fresh)")
COLLARDS = CatalogFacts("Collards", 1.16, 0.2866, "ERS: Collard greens (Fresh)")
KALE = CatalogFacts("Kale", 1.05, 0.2866, "ERS: Kale (Fresh)")
EGGPLANT = CatalogFacts("Eggplant", None, None, "none")


def _price(
    cents: float, quantity: float, unit: str = "lb", **fields: Any
) -> PriceInput:
    values: dict[str, Any] = {
        "kind": "household",
        "label": "Kroger",
        "observed_on": ON,
        "raw_text": "typed",
    }
    return PriceInput(
        price_cents=cents, quantity=quantity, unit=unit, **(values | fields)
    )


def _storage(days: float | None, note: str | None = None) -> Storage:
    return Storage(days=days, source="FoodKeeper test", note=note)


# ---------- spoilage ----------


@pytest.mark.parametrize(
    ("low", "high", "expected"), [(1, 4, 2.5), (3, 5, 4.0), (30, 30, 30.0), (30, None, 30.0), (None, None, None)]
)  # fmt: skip
def test_midpoint(low: int | None, high: int | None, expected: float | None) -> None:
    assert midpoint(low, high) == expected


@pytest.mark.parametrize(
    ("bought", "per_week", "days", "usable", "spoiled"),
    [
        (7.6552, 2, 30, 7.6552, 0.0),  # onions keep longer than they last
        (4.0475, 3, 2.5, 1.0714, 0.7353),  # collards: 3/7 cup a day for 2.5 days
        (3.6636, 3, 4.0, 1.7143, 0.5321),  # kale
        (4.0475, 3, 45, 4.0475, 0.0),  # collards cooked and frozen
        (4.0, 3, None, 4.0, 0.0),  # no storage time: spoilage not counted
        (0.0, 3, 2.5, 0.0, 0.0),
    ],
)
def test_spoilage(
    bought: float, per_week: float, days: float | None, usable: float, spoiled: float
) -> None:
    got_usable, got_spoiled = spoilage(bought, per_week, days)
    assert got_usable == pytest.approx(usable, abs=1e-4)
    assert got_spoiled == pytest.approx(spoiled, abs=1e-4)


# ---------- the spec's worked example ----------


@pytest.mark.parametrize(
    ("facts", "price", "per_week", "days", "usable", "spoiled_pct", "per_usable_cup"),
    [
        (ONION, _price(299, 3), 2, 30.0, 7.66, 0, 0.39),
        (COLLARDS, _price(248, 1), 3, 2.5, 1.07, 74, 2.31),
        (COLLARDS, _price(248, 1), 3, 45.0, 4.05, 0, 0.61),
        (KALE, _price(357, 1), 3, 4.0, 1.71, 53, 2.08),
        (KALE, _price(357, 1), 3, 45.0, 3.66, 0, 0.97),
    ],
)
def test_spec_worked_example(
    facts: CatalogFacts,
    price: PriceInput,
    per_week: float,
    days: float,
    usable: float,
    spoiled_pct: int,
    per_usable_cup: float,
) -> None:
    (row,) = build_rows(
        facts.name, Need(uuid.uuid4(), per_week), facts, _storage(days), [price]
    )
    assert row.usable_amount == pytest.approx(usable, abs=0.005)
    assert row.spoiled_share is not None
    assert round(row.spoiled_share * 100) == spoiled_pct
    assert row.cost_per_usable_cents is not None
    assert row.cost_per_usable_cents / 100 == pytest.approx(per_usable_cup, abs=0.005)
    assert row.marked


# ---------- rows and the marked row ----------


def _rows(
    prices: list[PriceInput], facts: CatalogFacts = ONION, days: float | None = 30
) -> list[BestBuyRow]:
    return build_rows(facts.name, Need(uuid.uuid4(), 2), facts, _storage(days), prices)


def test_the_lowest_cost_per_usable_cup_is_marked() -> None:
    rows = _rows([_price(149, 1, label="Aldi"), _price(299, 3, label="Kroger")])
    assert [r.label for r in rows if r.marked] == ["Kroger"]


def test_spoilage_can_change_the_best_buy() -> None:
    """A big bag is cheaper per cup on the shelf but spoils; the small bunch wins once it does."""
    big = _price(
        400, 3, label="Big bag"
    )  # 12.14 cups, 2.50 usable in 2.5 days at 7 a week
    small = _price(150, 0.5, label="Small bunch")  # 2.02 cups, all usable
    rows = build_rows(
        "Collards", Need(uuid.uuid4(), 7), COLLARDS, _storage(2.5), [big, small]
    )
    by_label = {r.label: r for r in rows}
    assert (
        by_label["Big bag"].cost_per_amount_cents
        < by_label["Small bunch"].cost_per_amount_cents
    )  # type: ignore[operator]
    assert by_label["Small bunch"].marked and not by_label["Big bag"].marked


def test_a_reference_row_is_never_marked() -> None:
    """The national average is for comparison: a cheaper ERS row does not beat a real store."""
    rows = _rows(
        [_price(299, 3), _price(80, 1, kind="ers_reference", label="national average")]
    )
    reference = next(r for r in rows if r.kind == "ers_reference")
    assert not reference.marked
    assert reference.cost_per_usable_cents < rows[0].cost_per_usable_cents  # type: ignore[operator]
    # What a national average is, is said once under the table, not on every row.
    assert reference.notes == []


def test_with_no_store_price_the_national_average_is_marked() -> None:
    rows = _rows([_price(106, 1, kind="ers_fallback", label="national average")])
    assert rows[0].marked and rows[0].notes == []
    note = item_note("Onion", ONION, _storage(30), rows)
    assert "no store price yet" in " ".join(note.notes)


def test_item_notes_are_said_once_per_item() -> None:
    """Storage and stand-ins apply to every row of an item, so they leave the rows (dogfooding)."""
    squash = CatalogFacts("Yellow squash", 0.95, 0.2756, "ERS: Zucchini", "Zucchini")
    storage = _storage(
        4, "Keeps 3-5 days in the refrigerator (FoodKeeper), counted as 4 days."
    )
    rows = build_rows(
        "Yellow squash",
        Need(uuid.uuid4(), 2),
        squash,
        storage,
        [_price(199, 1), _price(149, 1)],
    )
    assert all(row.notes == [] for row in rows)

    note = item_note("Yellow squash", squash, storage, rows)

    assert note.storage == storage.note
    assert note.notes == [
        "Conversions borrowed from Zucchini."
    ]  # no fallback: store prices exist


def test_ties_go_to_the_newest_price_then_the_name() -> None:
    old = _price(299, 3, label="Aldi", observed_on=ON - timedelta(days=5))
    new = _price(299, 3, label="Kroger", observed_on=ON)
    same_day = _price(299, 3, label="Aldi", observed_on=ON)
    assert [r.label for r in _rows([old, new]) if r.marked] == ["Kroger"]
    assert [r.label for r in _rows([new, same_day]) if r.marked] == ["Aldi"]


def test_an_empty_price_book_is_a_no_price_row() -> None:
    (row,) = _rows([])
    assert row.kind == "no_price" and not row.marked
    assert row.cost_per_usable_cents is None


def test_an_item_without_a_conversion_shows_its_price_unmarked() -> None:
    (row,) = _rows([_price(199, 1)], facts=EGGPLANT)
    assert row.price_cents == 199 and row.cost_per_usable_cents is None
    assert not row.marked
    assert "no cup conversion" in " ".join(row.notes)


def test_a_price_with_a_problem_is_shown_with_it() -> None:
    (row,) = _rows(
        [_price(250, 1, unit="bunch", problem="How much does one bunch weigh?")]
    )
    assert row.notes == ["How much does one bunch weigh?"] and not row.marked


def test_every_factor_names_its_source() -> None:
    (row,) = _rows([_price(299, 3)])
    assert row.sources == {
        "price": "Kroger",
        "storage": "FoodKeeper test",
        "conversion": "ERS: Onions (Fresh)",
    }


# ---------- gathering from the database ----------


@pytest.fixture
def ers(db_session: Session, catalog: Catalog) -> DataSnapshot:
    """An ERS snapshot with national averages for onion, collards, and kale."""
    snapshot = _snapshot(
        db_session, "ers_prices", "2023", datetime(2026, 11, 11, tzinfo=UTC)
    )
    for slug, dollars in (
        ("onion", 1.062206305),
        ("collards", 2.483338628),
        ("kale", 3.57112371),
    ):
        _public_price(db_session, catalog[slug], snapshot, dollars)
    return snapshot


def _snapshot(
    session: Session, source: str, version: str, loaded_at: datetime
) -> DataSnapshot:
    snapshot = DataSnapshot(
        source=source,
        version=version,
        retrieved_at=loaded_at,
        url="https://example.com",
        sha256=uuid.uuid4().hex * 2,
        row_count=1,
        loaded_at=loaded_at,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _public_price(
    session: Session, item: CatalogItem, snapshot: DataSnapshot, dollars: float
) -> None:
    session.add(
        PriceObservation(
            catalog_item_id=item.id,
            price_cents=round(dollars * 100),
            quantity=1,
            unit="lb",
            observed_on=date(2023, 7, 1),
            captured_via="public_dataset",
            source_type="public_data",
            visibility="shared",
            raw_text=f"{item.name} (Fresh): {dollars} per pound",
            snapshot_id=snapshot.id,
        )
    )
    session.flush()


def _own_price(
    session: Session,
    household: Household,
    vendor: Vendor,
    item: CatalogItem,
    cents: int,
    quantity: float,
    unit: str = "lb",
    observed_on: date = ON,
    **fields: Any,
) -> PriceObservation:
    observation = PriceObservation(
        household_id=household.id,
        catalog_item_id=item.id,
        vendor_id=vendor.id,
        price_cents=cents,
        quantity=quantity,
        unit=unit,
        observed_on=observed_on,
        captured_via="typed",
        source_type="store",
        raw_text=f"{item.name} {quantity:g} {unit} {cents / 100:.2f}",
        **fields,
    )
    session.add(observation)
    session.flush()
    return observation


def _table(
    session: Session, household: Household, *needs: tuple[CatalogItem, float]
) -> BestBuyTable:
    return compare_best_buy(
        session, household, [Need(item.id, cups) for item, cups in needs], ON
    )


@pytest.mark.usefixtures("ers")
def test_the_household_prices_come_first_with_ers_as_reference(
    db_session: Session, catalog: Catalog, household: Household, vendor: Vendor
) -> None:
    _own_price(db_session, household, vendor, catalog["onion"], 299, 3)

    table = _table(db_session, household, (catalog["onion"], 2), (catalog["kale"], 3))

    onion = [r for r in table.rows if r.item_name == "Onion"]
    assert [r.kind for r in onion] == ["household", "ers_reference"]
    assert onion[0].marked and onion[0].label == "Corner store"
    assert onion[1].label == "National average (2023)"
    assert table.national_average is not None
    assert table.national_average.version == "2023"
    assert "not a price at any store" in table.national_average.note
    assert [n.item_name for n in table.items] == ["Onion", "Kale"]
    kale = [r for r in table.rows if r.item_name == "Kale"]
    assert [r.kind for r in kale] == ["ers_fallback"] and kale[0].marked
    # The ERS row uses the full-precision price from its raw text, not the rounded cents.
    assert kale[0].price_cents == pytest.approx(357.112371)
    assert kale[0].cost_per_usable_cents / 100 == pytest.approx(2.08, abs=0.005)  # type: ignore[operator]


@pytest.mark.usefixtures("ers")
def test_only_the_last_30_days_of_the_households_own_prices(
    db_session: Session, catalog: Catalog, household: Household, vendor: Vendor
) -> None:
    onion = catalog["onion"]
    _own_price(
        db_session,
        household,
        vendor,
        onion,
        100,
        1,
        observed_on=ON - timedelta(days=30),
    )
    _own_price(
        db_session,
        household,
        vendor,
        onion,
        101,
        1,
        observed_on=ON - timedelta(days=31),
    )
    _own_price(
        db_session, household, vendor, onion, 102, 1, observed_on=ON + timedelta(days=1)
    )
    neighbor = make_household(db_session)
    their_store = Vendor(household_id=neighbor.id, name="Theirs", vendor_type="store")
    db_session.add(their_store)
    db_session.flush()
    _own_price(db_session, neighbor, their_store, onion, 50, 1)

    rows = _table(db_session, household, (onion, 2)).rows

    assert [r.price_cents for r in rows if r.kind == "household"] == [100]


def test_the_latest_ers_snapshot_is_used(
    db_session: Session, catalog: Catalog, household: Household
) -> None:
    old = _snapshot(db_session, "ers_prices", "2022", datetime(2026, 1, 1, tzinfo=UTC))
    new = _snapshot(
        db_session, "ers_prices", "2023", datetime(2026, 11, 11, tzinfo=UTC)
    )
    _public_price(db_session, catalog["onion"], old, 0.95)
    _public_price(db_session, catalog["onion"], new, 1.062206305)

    table = _table(db_session, household, (catalog["onion"], 2))

    (row,) = table.rows
    assert row.label == "National average (2023)"
    assert new.id in table.data_snapshot_ids and old.id not in table.data_snapshot_ids


def test_no_price_at_all(
    db_session: Session, catalog: Catalog, household: Household
) -> None:
    (row,) = _table(db_session, household, (catalog["mustard-greens"], 2)).rows
    assert row.kind == "no_price"


def _cooked_leftovers(session: Session) -> DataSnapshot:
    # Loaded "now", so it is newer than any real FoodKeeper load in the development database.
    snapshot = _snapshot(session, "foodkeeper", "test", datetime.now(UTC))
    for location, low, high in (("refrigerator", 3, 4), ("freezer", 30, 60)):
        session.add(
            StorageReference(
                key="cooked_leftovers",
                location=location,
                days_min=low,
                days_max=high,
                description="Leftovers, cooked vegetables",
                snapshot_id=snapshot.id,
            )
        )
    session.flush()
    return snapshot


@pytest.mark.usefixtures("ers")
def test_cook_and_freeze_extras(
    db_session: Session, catalog: Catalog, household: Household, vendor: Vendor
) -> None:
    collards = catalog["collards"]
    snapshot = _cooked_leftovers(db_session)
    _own_price(db_session, household, vendor, collards, 248, 1)
    plain = _table(db_session, household, (collards, 3)).rows[0]
    db_session.add(
        HouseholdItemSetting(
            household_id=household.id, catalog_item_id=collards.id, freeze_extras=True
        )
    )
    db_session.flush()

    table = _table(db_session, household, (collards, 3))
    frozen = table.rows[0]

    assert plain.storage_days == 2.5 and round(plain.spoiled_share * 100) == 74  # type: ignore[operator]
    assert frozen.storage_days == 45.0 and frozen.spoiled_share == 0
    assert frozen.cost_per_usable_cents / 100 == pytest.approx(0.61, abs=0.005)  # type: ignore[operator]
    (note,) = table.items
    assert note.storage == (
        "Cook and freeze extras: cooked leftovers keep 30-60 days frozen (FoodKeeper), "
        "counted as 45 days. Assumes you freeze what you won't eat in 3-4 days."
    )
    assert (
        frozen.sources["storage"] == "FoodKeeper cooked leftovers, freezer: 30-60 days"
    )
    assert snapshot.id in table.data_snapshot_ids
    # Another household's setting changes nothing for this one.
    other = make_household(db_session)
    assert _table(db_session, other, (collards, 3)).rows[0].storage_days == 2.5


def test_freeze_extras_without_cooked_leftovers_falls_back_to_the_item(
    db_session: Session, catalog: Catalog, household: Household
) -> None:
    # The development database may hold real cooked-leftovers rows; this transaction rolls back.
    db_session.exec(delete(StorageReference))
    db_session.add(
        HouseholdItemSetting(
            household_id=household.id,
            catalog_item_id=catalog["kale"].id,
            freeze_extras=True,
        )
    )
    db_session.flush()
    _public_price(
        db_session,
        catalog["kale"],
        _snapshot(db_session, "ers_prices", "2023", datetime.now(UTC)),
        3.57,
    )
    (row,) = _table(db_session, household, (catalog["kale"], 3)).rows
    assert row.storage_days == 4.0


def test_an_item_without_storage_time_counts_no_spoilage(
    db_session: Session, catalog: Catalog, household: Household, vendor: Vendor
) -> None:
    _own_price(db_session, household, vendor, catalog["tomato"], 249, 1)
    table = _table(db_session, household, (catalog["tomato"], 2))
    (row,) = table.rows
    assert row.spoiled_share == 0 and row.storage_days is None
    assert "spoilage is not counted" in (table.items[0].storage or "")


def test_prices_by_count_use_the_size_or_the_weight_given(
    db_session: Session, catalog: Catalog, household: Household, vendor: Vendor
) -> None:
    tomato, kale = catalog["tomato"], catalog["kale"]
    _own_price(db_session, household, vendor, tomato, 500, 3, "each", size="large")
    _own_price(
        db_session, household, vendor, tomato, 400, 2, "each"
    )  # no size: medium, and the source says so
    _own_price(
        db_session,
        household,
        vendor,
        kale,
        250,
        1,
        "bunch",
        unit_grams=280.0,
        weight_source="estimated",
    )

    rows = _table(db_session, household, (tomato, 4), (kale, 3)).rows

    tomatoes = {r.price_cents: r for r in rows if r.item_name == "Tomato"}
    large, unsized = tomatoes[500], tomatoes[400]
    assert large.sources["piece"].startswith("test piece: tomato large")
    assert large.amount_bought is not None and large.marked
    assert unsized.sources["piece"] == (
        "test piece: tomato medium each (no size given: medium assumed)"
    )
    assert unsized.amount_bought is not None
    (bunch,) = (r for r in rows if r.item_name == "Kale")
    assert bunch.sources["piece"] == "estimated at capture: 280 g"
    assert bunch.amount_bought is not None


def test_an_unreadable_stored_unit_is_shown_not_guessed(
    db_session: Session, catalog: Catalog, household: Household, vendor: Vendor
) -> None:
    _own_price(db_session, household, vendor, catalog["onion"], 299, 1, "sack")
    (row,) = _table(db_session, household, (catalog["onion"], 2)).rows
    assert row.cost_per_usable_cents is None
    assert "Unknown unit 'sack'" in " ".join(row.notes)


def test_stand_ins_are_noted(
    db_session: Session, catalog: Catalog, household: Household, vendor: Vendor
) -> None:
    _own_price(db_session, household, vendor, catalog["yellow-squash"], 199, 1)
    (note,) = _table(db_session, household, (catalog["yellow-squash"], 2)).items
    assert "Conversions borrowed from Zucchini." in note.notes


def test_a_juice_priced_by_the_pint_needs_no_piece_weight(
    db_session: Session, catalog: Catalog, household: Household, vendor: Vendor
) -> None:
    _own_price(db_session, household, vendor, catalog["orange-juice"], 199, 1, "pint")
    (row,) = _table(db_session, household, (catalog["orange-juice"], 4)).rows
    assert not any("weigh" in note for note in row.notes) and "piece" not in row.sources
    assert row.amount_bought == pytest.approx(2.0)  # a pint is 2 cups
    assert row.cost_per_usable_cents == pytest.approx(99.5)
    assert row.marked


@pytest.mark.parametrize("cups", [0, -1])
def test_needs_must_be_positive(
    db_session: Session, catalog: Catalog, household: Household, cups: float
) -> None:
    with pytest.raises(ValueError, match="more than zero"):
        _table(db_session, household, (catalog["onion"], cups))


def test_needs_must_be_produce(db_session: Session, household: Household) -> None:
    with pytest.raises(ValueError, match="unknown produce item"):
        compare_best_buy(db_session, household, [Need(uuid.uuid4(), 2)], ON)


# ---------- the explanation quotes only the table ----------


def _onion_table() -> BestBuyTable:
    table = BestBuyTable(on=ON)
    table.rows = _rows([_price(299, 3, label="Kroger"), _price(149, 1, label="Aldi")])
    return table


@pytest.mark.parametrize(
    ("text", "bad"),
    [
        ("Kroger is the best buy at $0.39 per usable cup.", []),
        ("Kroger's 3 lb bag for $2.99 beats Aldi.", []),
        ("Kroger is 1.5 times cheaper than Aldi.", ["1.5"]),  # a computed ratio
        ("Kroger is about $0.40 a cup.", ["0.40"]),  # rounded
        ("You save $0.19 a cup at Kroger.", ["0.19"]),  # a computed difference
        ("No numbers here.", []),
    ],
)
def test_unsupported_numbers(text: str, bad: list[str]) -> None:
    assert unsupported_numbers(text, _onion_table()) == bad


def test_format_table_marks_the_best_row() -> None:
    text = format_table(_onion_table())
    assert "Onion (needs 2 cups a week)" in text
    assert (
        "- MARKED BEST: Kroger, $2.99 for 3 lb: 7.66 cups bought, 7.66 usable (0% spoils), $0.39 per usable cup."
        in text
    )


def test_an_explanation_with_a_computed_number_is_sent_back() -> None:
    replies = iter(
        [
            "Kroger is 1.5 times cheaper.",
            "Kroger is the best buy at $0.39 per usable cup.",
        ]
    )
    seen: list[str] = []

    def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        seen.append(str(messages[-1]))
        return ModelResponse(parts=[TextPart(next(replies))])

    text = asyncio.run(explain_best_buy(_onion_table(), FunctionModel(respond))).text

    assert text == "Kroger is the best buy at $0.39 per usable cup."
    assert "not in the table: 1.5" in seen[1]


def test_an_explanation_with_dieting_words_is_sent_back() -> None:
    # Spec P1-8: the agent guide's rules, enforced for a short list of words.
    replies = iter(
        [
            "Kroger is the best buy at $0.39 per usable cup, a low-calorie serving.",
            "Kroger is the best buy at $0.39 per usable cup.",
        ]
    )
    seen: list[str] = []

    def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        seen.append(str(messages[-1]))
        return ModelResponse(parts=[TextPart(next(replies))])

    text = asyncio.run(explain_best_buy(_onion_table(), FunctionModel(respond))).text

    assert text == "Kroger is the best buy at $0.39 per usable cup."
    assert "Do not use these words: calorie, serving." in seen[1]


def test_an_explanation_that_keeps_inventing_numbers_fails() -> None:
    def respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Save 42% at Kroger!")])

    with pytest.raises(ExplanationFailed) as raised:
        asyncio.run(explain_best_buy(_onion_table(), FunctionModel(respond)))
    assert raised.value.reason == "invalid_output"


def test_a_runaway_explanation_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.agents.best_buy_agent as agent_module

    monkeypatch.setattr(agent_module, "MAX_MODEL_REQUESTS", 1)

    def respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Save 42%!")])

    with pytest.raises(ExplanationFailed) as raised:
        asyncio.run(explain_best_buy(_onion_table(), FunctionModel(respond)))
    assert raised.value.reason == "too_many_steps"


@pytest.mark.usefixtures("ers")
def test_demo_mode_explains_the_real_table(
    db_session: Session,
    catalog: Catalog,
    household: Household,
    vendor: Vendor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "SIM_MODEL", "demo")
    _own_price(db_session, household, vendor, catalog["onion"], 299, 3)
    table = _table(db_session, household, (catalog["onion"], 2), (catalog["kale"], 3))

    explanation = asyncio.run(explain_best_buy(table))
    text = explanation.text
    assert explanation.usage.requests == 1

    assert (
        "For onion, Corner store is the best buy at **$0.39 per usable cup**." in text
    )
    assert (
        "For kale there is no store price yet" in text and "not a store price" in text
    )
    assert unsupported_numbers(text, table) == []


# ---------- the shopping list ----------


def _list_table() -> BestBuyTable:
    table = BestBuyTable(on=ON)
    table.rows = [
        *_rows([_price(299, 3, label="Kroger", raw_text="onions 3 lb bag 2.99"), _price(149, 1, label="Aldi")]),
        *build_rows("Collards", Need(uuid.uuid4(), 3), COLLARDS, _storage(2.5), [_price(248, 1, label="Aldi", raw_text="collards 2.48/lb")]),
        *build_rows("Kale", Need(uuid.uuid4(), 3), KALE, _storage(4), [_price(357.11, 1, kind="ers_fallback", label="national average")]),
        *build_rows("Eggplant", Need(uuid.uuid4(), 1), EGGPLANT, _storage(None), [_price(199, 1, label="Kroger")]),
        *build_rows("Mustard greens", Need(uuid.uuid4(), 2), COLLARDS, _storage(2.5), []),
    ]  # fmt: skip
    return table


def test_the_shopping_list_groups_marked_rows_by_store() -> None:
    groups = shopping_list(_list_table())

    assert [g.label for g in groups] == [
        "Aldi",
        "Kroger",
        "No store price yet",
        "Not compared yet",
    ]
    aldi, kroger, fallback, not_compared = groups
    assert [(line.item_name, line.purchase) for line in aldi.lines] == [
        ("Collards", "collards 2.48/lb")
    ]
    assert [(line.item_name, line.purchase) for line in kroger.lines] == [
        ("Onion", "onions 3 lb bag 2.99")
    ]
    # Only marked rows: Aldi's onions lost to Kroger's bag, so they are not on the list.
    assert all(line.item_name != "Onion" for line in aldi.lines)
    assert [(line.item_name, line.purchase) for line in fallback.lines] == [
        ("Kale", "1 lb")
    ]
    assert {(line.item_name, line.note) for line in not_compared.lines} == {
        ("Eggplant", "no cost per cup"),
        ("Mustard greens", "no price yet"),
    }


def test_a_shopping_line_carries_the_need_and_usable_cups() -> None:
    kroger = next(g for g in shopping_list(_list_table()) if g.label == "Kroger")
    (onion,) = kroger.lines
    assert onion.per_week == 2
    assert onion.usable_amount == pytest.approx(7.66, abs=0.01)
    assert onion.price_cents == 299


def test_an_empty_table_has_an_empty_list() -> None:
    assert shopping_list(BestBuyTable(on=ON)) == []


# ---------- items compared per piece, and nuts in the shell (spec P1-11) ----------

LEMON = CatalogFacts("Lemon", None, None, "not needed", compared_by="each")
WALNUT = CatalogFacts("Walnut", 1.0, 0.2579, "SR28 12155", shell_refuse_pct=55)


def test_a_piece_item_is_ranked_per_usable_piece() -> None:
    """Lemons: 4 a week, keep 15.5 days; a 10-lemon bag spoils some, 3 for $1 does not."""
    rows = build_rows(
        "Lemon",
        Need(uuid.uuid4(), 4),
        LEMON,
        _storage(15.5),
        [
            _price(100, 3, "each", label="Market"),
            _price(250, 10, "each", label="Kroger"),
        ],
    )
    market, kroger = rows
    assert (market.measure, market.per_week) == ("lemon", 4)
    assert market.amount_bought == 3 and market.usable_amount == 3
    assert market.cost_per_usable_cents == pytest.approx(33.33, abs=0.01)
    # 4 a week for 15.5 days is 8.86 lemons used of the 10 bought.
    assert kroger.usable_amount == pytest.approx(4 / 7 * 15.5)
    assert kroger.cost_per_amount_cents == 25
    assert kroger.cost_per_usable_cents == pytest.approx(250 / (4 / 7 * 15.5))
    assert kroger.marked and not market.marked


def test_nuts_in_the_shell_are_ranked_by_kernels_and_noted() -> None:
    rows = build_rows(
        "Walnut",
        Need(uuid.uuid4(), 1),
        WALNUT,
        _storage(None),
        [
            _price(800, 1, label="Market", in_shell=True),
            _price(1500, 1, label="Kroger"),
        ],
    )
    market, kroger = rows
    assert market.measure == "cup"
    assert market.cost_per_amount_cents == pytest.approx(459, abs=1)
    assert (
        "Weighed in the shell: 45% of the weight is kernels (USDA SR28)."
        in market.notes
    )
    assert (
        kroger.notes == [] and kroger.marked
    )  # shelled at $15/lb beats $8/lb in the shell


def test_a_piece_item_needs_and_lists_its_measure(
    db_session: Session, catalog: Catalog, household: Household, vendor: Vendor
) -> None:
    lemon, garlic = catalog["lemon"], catalog["garlic"]
    _own_price(db_session, household, vendor, lemon, 300, 2)  # a 2 lb bag
    _own_price(db_session, household, vendor, garlic, 89, 1, "each")
    table = _table(db_session, household, (lemon, 4), (garlic, 1))

    bag, head = table.rows
    assert (bag.measure, head.measure) == ("lemon", "head")
    assert bag.sources["piece"] == (
        "test piece: lemon medium each (no size given: medium assumed)"
    )
    assert bag.amount_bought == pytest.approx(2 * 453.59237 / 109.4)
    assert head.amount_bought == 1 and head.cost_per_amount_cents == 89
    assert "piece" not in head.sources  # a head priced by the head needs no weight
    lines = [line for group in shopping_list(table) for line in group.lines]
    assert [(line.item_name, line.measure) for line in lines] == [
        ("Lemon", "lemon"),
        ("Garlic", "head"),
    ]
    text = format_table(table)
    assert "Lemon (needs 4 lemons a week)" in text
    assert "per usable head" in text


def test_demo_mode_names_the_measure(
    db_session: Session,
    catalog: Catalog,
    household: Household,
    vendor: Vendor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "SIM_MODEL", "demo")
    _own_price(db_session, household, vendor, catalog["lemon"], 100, 3, "each")
    table = _table(db_session, household, (catalog["lemon"], 4))
    text = asyncio.run(explain_best_buy(table)).text
    assert "best buy at **$0.33 per usable lemon**" in text
    assert unsupported_numbers(text, table) == []


def test_borrowed_storage_says_whose_it_is(
    db_session: Session, catalog: Catalog, household: Household, vendor: Vendor
) -> None:
    """Hazelnuts keep as walnuts do (owner, 2026-11-13): the note says so."""
    kale = catalog["kale"]
    kale.storage_as = "walnuts"
    _own_price(db_session, household, vendor, kale, 250, 1)
    (note,) = _table(db_session, household, (kale, 3)).items
    assert note.storage == (
        "Keeps 3-5 days in the refrigerator (FoodKeeper, as walnuts), counted as 4 days."
    )
