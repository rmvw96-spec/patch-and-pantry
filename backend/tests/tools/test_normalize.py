"""Tool normalize_price (Phase 1, P1-4): known answers, units, errors, and the input lookups."""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session

from app.core.db import engine
from app.models import CatalogItem, CatalogPortion, DataSnapshot, PortionSize
from app.seed.loaders import ErsRow, ers_price_per_cup, read_ers_csv
from app.tools.normalize import (
    GRAMS_PER_LB,
    CatalogFacts,
    InvalidAmount,
    NeedsPortion,
    NoConversion,
    NormalizeError,
    UnknownUnit,
    WrongMeasure,
    canonical_unit,
    catalog_facts,
    measure_name,
    normalize_price,
    piece_unit,
    portion_grams,
)

REPO = Path(__file__).resolve().parents[3]
ERS_SOURCES = sorted((REPO / "data" / "sources" / "usda-ers").glob("*.csv"))
# ERS published these pairs swapped (docs/specs/known-answers.md); the inputs are right.
ERS_SWAPPED = {("Artichoke", "Fresh"), ("Artichoke", "Canned"), ("Lima beans", "Frozen"), ("Lima beans", "Dried")}  # fmt: skip


def _facts(
    yield_factor: float | None, cup_lb: float | None, name: str = "Item"
) -> CatalogFacts:
    return CatalogFacts(
        name=name,
        preparation_yield_factor=yield_factor,
        cup_equivalent_lb=cup_lb,
        conversion_source="test",
    )


ONION = _facts(0.9, 0.3527, "Onion")
POTATO = _facts(0.8113, 0.2646, "Potato")
COLLARDS = _facts(1.16, 0.2866, "Collards")
GREEN_PEPPER = _facts(0.82, 0.2646, "Sweet pepper")
SWEET_CORN = _facts(0.54, 0.3638, "Sweet corn")


# ---------- known answers ----------


@pytest.mark.parametrize(
    ("price_cents", "pounds", "facts", "edible", "cups", "dollars_per_cup"),
    [
        (299, 3, ONION, 1224.7, 7.6552, 0.3906),
        (349, 5, POTATO, 1840.0, 15.3307, 0.2276),
        (248, 1, COLLARDS, 526.2, 4.0475, 0.6127),
    ],
)
def test_spec_worked_examples(
    price_cents: int,
    pounds: float,
    facts: CatalogFacts,
    edible: float,
    cups: float,
    dollars_per_cup: float,
) -> None:
    result = normalize_price(price_cents, pounds, "lb", facts)

    assert result.grams_purchased == pytest.approx(pounds * GRAMS_PER_LB)
    assert result.grams_edible == pytest.approx(edible, abs=0.05)
    assert result.cups == pytest.approx(cups, abs=1e-4)
    assert result.cost_per_cup_cents / 100 == pytest.approx(dollars_per_cup, abs=1e-4)


def _ers_rows() -> list[ErsRow]:
    return [
        row
        for path in ERS_SOURCES
        for row in read_ers_csv(path)
        if row.price_unit == "per pound"
    ]


def test_every_ers_pound_price_matches_the_ers_formula() -> None:
    """One pound at ERS's price gives ERS's price per cup: exactly the formula, every row."""
    rows = _ers_rows()
    assert len(rows) > 100
    for row in rows:
        assert row.cup_unit == "pounds"
        facts = _facts(row.preparation_yield_factor, row.cup_size, row.item)
        result = normalize_price(row.price * 100, 1, "lb", facts)
        assert result.cost_per_cup_cents / 100 == pytest.approx(
            ers_price_per_cup(row), abs=1e-4
        ), (row.item, row.form)
        if (row.item, row.form) not in ERS_SWAPPED:
            # Published values round the yield and cup size: 0.001 (known-answers.md).
            assert result.cost_per_cup_cents / 100 == pytest.approx(
                row.published_price_per_cup, abs=1e-3
            ), (row.item, row.form)


@pytest.mark.parametrize(
    ("price_cents", "quantity", "unit", "piece", "facts", "edible", "cups", "dollars_per_cup"),
    [
        # Two large green peppers for 2.00: SR28 says one is 164 g edible, and 2 x 164 = 328.
        (200, 2, "each", 200.0, GREEN_PEPPER, 328.0, 2.7329, 0.7318),
        # Three medium potatoes for 1.50 (237 g each with the owner's 10% refuse).
        (150, 3, "each", 213 / 0.9, POTATO, 576.0, 4.7994, 0.3125),
        # Four medium ears of corn for 2.00 (kernels 102 g, 64% husk, silk, and cob).
        (200, 4, "ears", 102 / 0.36, SWEET_CORN, 612.0, 3.7087, 0.5393),
    ],
)  # fmt: skip
def test_count_units_use_the_weight_of_one_piece(
    price_cents: int,
    quantity: float,
    unit: str,
    piece: float,
    facts: CatalogFacts,
    edible: float,
    cups: float,
    dollars_per_cup: float,
) -> None:
    result = normalize_price(price_cents, quantity, unit, facts, piece)

    assert result.grams_purchased == pytest.approx(quantity * piece)
    assert result.grams_edible == pytest.approx(edible, abs=0.05)
    assert result.cups == pytest.approx(cups, abs=1e-4)
    assert result.cost_per_cup_cents / 100 == pytest.approx(dollars_per_cup, abs=1e-4)


def test_three_for_five_is_one_price_for_the_lot() -> None:
    lot = normalize_price(500, 3, "each", GREEN_PEPPER, 145.1)
    one = normalize_price(500 / 3, 1, "each", GREEN_PEPPER, 145.1)
    assert lot.cost_per_cup_cents == pytest.approx(one.cost_per_cup_cents)
    assert lot.cups == pytest.approx(3 * one.cups)


def test_a_free_item_costs_nothing_per_cup() -> None:
    assert normalize_price(0, 1, "lb", ONION).cost_per_cup_cents == 0


# ---------- units ----------


@pytest.mark.parametrize(
    ("written", "canonical"),
    [
        ("lb", "lb"), ("LB", "lb"), ("lbs", "lb"), ("Pounds", "lb"), ("lb.", "lb"),
        ("oz", "oz"), ("ounces", "oz"), ("kg", "kg"), ("grams", "g"),
        ("each", "each"), ("ea", "each"), ("Bunches", "bunch"), ("head", "head"),
        ("ears", "ear"), ("pint", "pint"), (" lb ", "lb"),
    ],
)  # fmt: skip
def test_unit_spellings(written: str, canonical: str) -> None:
    assert canonical_unit(written) == canonical


@pytest.mark.parametrize(
    ("quantity", "unit"), [(16, "oz"), (1, "lb"), (0.45359237, "kg"), (453.59237, "g")]
)
def test_weight_units_agree(quantity: float, unit: str) -> None:
    result = normalize_price(299, quantity, unit, ONION)
    assert result.grams_purchased == pytest.approx(GRAMS_PER_LB)
    assert result.cost_per_cup_cents == pytest.approx(
        normalize_price(299, 1, "lb", ONION).cost_per_cup_cents
    )


# ---------- juices: by volume (ERS prices them per pint) ----------


def _juice(yield_factor: float, name: str = "Orange juice") -> CatalogFacts:
    return CatalogFacts(name, yield_factor, None, "test", cup_equivalent_pints=0.5)


ORANGE_JUICE = _juice(1)
OJ_CONCENTRATE = _juice(4, "Orange juice concentrate")


def test_every_ers_pint_price_matches_the_ers_formula() -> None:
    """A pint at ERS's juice price gives ERS's price per cup: the volume formula, every row."""
    rows = [
        r
        for path in ERS_SOURCES
        for r in read_ers_csv(path)
        if r.price_unit.startswith("per pint")
    ]
    assert len(rows) == 11
    for row in rows:
        assert row.cup_unit == "pints"
        facts = CatalogFacts(
            row.item,
            row.preparation_yield_factor,
            None,
            "ERS",
            cup_equivalent_pints=row.cup_size,
        )
        result = normalize_price(row.price * 100, 1, "pint", facts)
        assert result.cost_per_cup_cents / 100 == pytest.approx(
            row.published_price_per_cup, abs=1e-3
        ), row.item
        assert result.grams_purchased is None and result.pints_purchased == 1


@pytest.mark.parametrize(
    ("quantity", "unit", "facts", "cups"),
    [
        (1, "pint", ORANGE_JUICE, 2.0),  # a pint is 2 cups
        (52, "fl oz", ORANGE_JUICE, 6.5),
        (52, "oz", ORANGE_JUICE, 6.5),  # on a juice, ounces are fluid ounces
        (1, "half gallon", ORANGE_JUICE, 8.0),
        (1, "gallon", ORANGE_JUICE, 16.0),
        (2, "quarts", ORANGE_JUICE, 8.0),
        (1, "L", ORANGE_JUICE, 4.226752838),
        (12, "fl oz", OJ_CONCENTRATE, 6.0),  # a 12 oz can of concentrate makes 48 fl oz
    ],
)
def test_juice_cups_by_volume(
    quantity: float, unit: str, facts: CatalogFacts, cups: float
) -> None:
    result = normalize_price(399, quantity, unit, facts)
    assert result.cups == pytest.approx(cups)
    assert result.cost_per_cup_cents == pytest.approx(399 / cups)
    assert result.grams_edible is None


@pytest.mark.parametrize("unit", ["lb", "each", "bunch"])
def test_a_juice_by_weight_or_count_is_the_wrong_measure(unit: str) -> None:
    with pytest.raises(WrongMeasure, match="measured by volume"):
        normalize_price(399, 1, unit, ORANGE_JUICE, portion_grams=100.0)


@pytest.mark.parametrize("unit", ["quart", "gallon", "fl oz", "liter", "cup"])
def test_produce_by_volume_is_the_wrong_measure(unit: str) -> None:
    with pytest.raises(WrongMeasure, match="sold by weight or count"):
        normalize_price(399, 1, unit, ONION)


def test_a_pint_is_a_container_for_produce_and_a_volume_for_juice() -> None:
    assert piece_unit("pint", ONION) == "pint"
    assert piece_unit("pint", ORANGE_JUICE) is None
    assert (
        piece_unit("each", ORANGE_JUICE) is None
    )  # normalize refuses it instead of asking
    assert piece_unit("lb", ONION) is None
    # A pint of blueberries goes by its weight.
    assert (
        normalize_price(399, 1, "pint", ONION, portion_grams=340.0).grams_purchased
        == 340.0
    )


@pytest.mark.parametrize(
    ("written", "canonical"),
    [("Qt", "quart"), ("fl. oz", "fl oz"), ("fluid ounces", "fl oz"), ("half-gallon", "half gallon"), ("Litres", "liter"), ("gal", "gallon"), ("ml", "ml"), ("fl  oz", "fl oz")],
)  # fmt: skip
def test_volume_spellings(written: str, canonical: str) -> None:
    assert canonical_unit(written) == canonical


# ---------- each failure has its own error ----------


@pytest.mark.parametrize("unit", ["dozen", "bag", "3 lb bag", "peck", ""])
def test_unknown_units(unit: str) -> None:
    with pytest.raises(UnknownUnit, match="Unknown unit"):
        normalize_price(299, 1, unit, ONION)


@pytest.mark.parametrize("unit", ["each", "bunch", "head", "ear", "pint"])
def test_count_unit_without_a_piece_weight_needs_a_portion(unit: str) -> None:
    with pytest.raises(NeedsPortion) as raised:
        normalize_price(250, 1, unit, _facts(1.0, 0.3, "Kale"))
    assert raised.value.unit == unit
    assert "estimate is fine" in str(raised.value)


def test_needs_portion_reads_naturally() -> None:
    assert str(NeedsPortion("Sweet pepper", "each")).startswith(
        "How much does one sweet pepper weigh?"
    )
    assert str(NeedsPortion("Kale", "bunch")).startswith(
        "How much does one bunch of kale weigh?"
    )
    assert (
        str(NeedsPortion("Tomato", "each", ("small", "medium", "large")))
        == "Which size tomato: small, medium, or large?"
    )
    assert str(NeedsPortion("Cabbage", "head", ("large",))) == (
        "Which size head of cabbage: large?"
    )


@pytest.mark.parametrize(
    "facts", [_facts(None, None, "Eggplant"), _facts(0.8, None), _facts(None, 0.3)]
)
def test_item_without_a_conversion(facts: CatalogFacts) -> None:
    with pytest.raises(NoConversion, match="no cup conversion"):
        normalize_price(199, 1, "lb", facts)


@pytest.mark.parametrize(
    ("price", "quantity", "piece", "message"),
    [
        (-1, 1, None, "negative"),
        (299, 0, None, "more than zero"),
        (299, -2, None, "more than zero"),
        (299, 1, 0.0, "must weigh"),
    ],
)
def test_invalid_amounts(
    price: int, quantity: float, piece: float | None, message: str
) -> None:
    unit = "each" if piece is not None else "lb"
    with pytest.raises(InvalidAmount, match=message):
        normalize_price(price, quantity, unit, ONION, piece)


def test_errors_are_distinct_and_share_a_base() -> None:
    errors = {UnknownUnit, NeedsPortion, NoConversion, InvalidAmount}
    assert len(errors) == 4
    assert all(issubclass(error, NormalizeError) for error in errors)
    assert not issubclass(NeedsPortion, NoConversion)


# ---------- lookups: stand-ins and piece weights (real Postgres, rolled back) ----------


@pytest.fixture
def session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
        session.rollback()


def _item(session: Session, **fields: Any) -> CatalogItem:
    values: dict[str, Any] = {
        "slug": f"test-{uuid.uuid4().hex[:8]}",
        "name": "Zucchini",
        "kind": "produce",
        "category": "vegetable",
        "conversion_source": "USDA ERS test",
    }
    item = CatalogItem(**(values | fields))
    session.add(item)
    session.flush()
    return item


def test_stand_ins_borrow_their_items_facts_and_say_so(session: Session) -> None:
    zucchini = _item(session, preparation_yield_factor=0.7695, cup_equivalent_lb=0.3968)
    squash = _item(
        session,
        name="Yellow squash",
        stand_in_for_id=zucchini.id,
        conversion_source="stand-in: conversions borrowed from zucchini",
    )

    facts = catalog_facts(session, squash)

    assert facts.name == "Yellow squash"
    assert facts.stand_in_name == "Zucchini"
    assert (facts.preparation_yield_factor, facts.cup_equivalent_lb) == (0.7695, 0.3968)
    assert facts.conversion_source == "USDA ERS test"
    result = normalize_price(199, 1, "lb", facts)
    assert result.stand_in_name == "Zucchini"
    assert catalog_facts(session, zucchini).stand_in_name is None


def test_a_stand_in_loop_ends_without_facts(session: Session) -> None:
    first = _item(session, name="First")
    second = _item(session, name="Second", stand_in_for_id=first.id)
    first.stand_in_for_id = second.id
    session.add(first)
    session.flush()

    facts = catalog_facts(session, first)

    assert facts.preparation_yield_factor is None
    with pytest.raises(NoConversion):
        normalize_price(100, 1, "lb", facts)


def _snapshot(session: Session, loaded_at: datetime) -> DataSnapshot:
    snapshot = DataSnapshot(
        source="usda_sr28",
        version="SR28 (2015)",
        retrieved_at=datetime(2026, 11, 11, tzinfo=UTC),
        url="https://example.com/sr28asc.zip",
        sha256=uuid.uuid4().hex * 2,
        row_count=1,
        loaded_at=loaded_at,
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def _piece(
    session: Session,
    item: CatalogItem,
    grams: float,
    size: PortionSize | None = None,
    unit: str = "each",
    snapshot: DataSnapshot | None = None,
    measured_on: date | None = None,
) -> None:
    session.add(
        CatalogPortion(
            catalog_item_id=item.id,
            unit=unit,
            size=size,
            grams=grams,
            edible_grams=grams * 0.9 if snapshot else None,
            refuse_pct=10 if snapshot else None,
            description="test",
            source=f"test {grams}",
            snapshot_id=snapshot.id if snapshot else None,
            measured_on=measured_on,
        )
    )
    session.flush()


def test_no_piece_weight_is_a_question(session: Session) -> None:
    item = _item(session, name="Kale")
    with pytest.raises(NeedsPortion, match="one bunch of kale"):
        portion_grams(session, item, "bunch")


def test_no_size_means_medium_and_says_so(session: Session) -> None:
    """Owner, 2026-11-12: market and store sizes differ, so medium unless the line says."""
    item = _item(session, name="Tomato")
    snapshot = _snapshot(session, datetime(2026, 11, 11, tzinfo=UTC))
    for size, grams in (("small", 100.0), ("medium", 135.2), ("large", 200.0)):
        _piece(session, item, grams, size, snapshot=snapshot)

    assumed = portion_grams(session, item, "each")
    assert (assumed.grams, assumed.size, assumed.assumed) == (135.2, "medium", True)
    given = portion_grams(session, item, "each", "large")
    assert (given.grams, given.size, given.assumed) == (200.0, "large", False)
    medium = portion_grams(session, item, "each", "medium")
    assert (medium.size, medium.assumed) == ("medium", False)


def test_sizes_without_a_medium_are_asked(session: Session) -> None:
    item = _item(session, name="Papaya")
    snapshot = _snapshot(session, datetime.now(UTC))
    _piece(session, item, 253.0, "small", snapshot=snapshot)
    _piece(session, item, 1260.0, "large", snapshot=snapshot)
    with pytest.raises(NeedsPortion) as raised:
        portion_grams(session, item, "each")
    assert raised.value.sizes == ("small", "large")
    with pytest.raises(NeedsPortion):
        portion_grams(session, item, "each", "medium")  # no weight for it: ignored
    assert portion_grams(session, item, "each", "large").grams == 1260.0


def test_an_unsized_piece_needs_no_size(session: Session) -> None:
    item = _item(session, name="Lettuce")
    _piece(
        session,
        item,
        666.0,
        unit="head",
        snapshot=_snapshot(session, datetime.now(UTC)),
    )
    assert portion_grams(session, item, "heads").grams == 666.0
    # A size for an item with one weight is ignored, not a question.
    ignored = portion_grams(session, item, "head", "large")
    assert (ignored.grams, ignored.size, ignored.assumed) == (666.0, None, False)


def test_a_single_size_is_used_without_asking(session: Session) -> None:
    item = _item(session, name="Cucumber")
    _piece(
        session, item, 310.0, "medium", snapshot=_snapshot(session, datetime.now(UTC))
    )
    assert portion_grams(session, item, "each").grams == 310.0


def test_a_size_without_a_weight_is_ignored(session: Session) -> None:
    item = _item(session, name="Onion")
    snapshot = _snapshot(session, datetime.now(UTC))
    _piece(session, item, 122.2, "medium", snapshot=snapshot)
    _piece(session, item, 166.7, "large", snapshot=snapshot)
    choice = portion_grams(session, item, "each", "small")
    assert (choice.grams, choice.size, choice.assumed) == (122.2, "medium", True)


def test_the_latest_snapshot_wins(session: Session) -> None:
    item = _item(session, name="Potato")
    now = datetime.now(UTC)
    _piece(
        session,
        item,
        284.0,
        "medium",
        snapshot=_snapshot(session, now - timedelta(days=1)),
    )
    _piece(session, item, 236.7, "medium", snapshot=_snapshot(session, now))
    assert portion_grams(session, item, "each", "medium").grams == 236.7


def test_a_weighed_piece_beats_a_loaded_one(session: Session) -> None:
    item = _item(session, name="Broccoli")
    _piece(
        session,
        item,
        997.0,
        unit="bunch",
        snapshot=_snapshot(session, datetime.now(UTC)),
    )
    _piece(session, item, 640.0, unit="bunch", measured_on=date(2026, 11, 1))
    _piece(session, item, 610.0, unit="bunch", measured_on=date(2026, 11, 14))

    choice = portion_grams(session, item, "bunch")

    assert choice.grams == 610.0  # the newest weighing
    assert choice.snapshot_id is None


def test_weight_units_need_no_portion(session: Session) -> None:
    with pytest.raises(ValueError, match="weight unit"):
        portion_grams(session, _item(session), "lb")


# ---------- items compared per piece, and nuts in the shell (spec P1-11) ----------

LEMON = CatalogFacts("Lemon", None, None, "not needed", compared_by="each")
GARLIC = CatalogFacts("Garlic", None, None, "not needed", compared_by="head")
# SR28 12155 and 12142: 1 cup chopped kernels (117 g, 109 g); shell refuse 55% and 47%.
WALNUT = CatalogFacts("Walnut", 1.0, 117 / GRAMS_PER_LB, "SR28", shell_refuse_pct=55)
PECAN = CatalogFacts("Pecan", 1.0, 109 / GRAMS_PER_LB, "SR28", shell_refuse_pct=47)


def test_a_count_price_of_a_piece_item_is_pieces() -> None:
    """ "3 for $1" is 3 lemons at 33 cents; no cups (owner, 2026-11-13)."""
    result = normalize_price(100, 3, "each", LEMON)
    assert (result.pieces, result.cups, result.cost_per_cup_cents) == (3, None, None)
    assert result.cost_per_piece_cents == pytest.approx(100 / 3)
    assert (result.amount, result.cost_per_measure_cents) == (
        3,
        result.cost_per_piece_cents,
    )


def test_each_garlic_is_a_head() -> None:
    assert normalize_price(89, 1, "each", GARLIC).pieces == 1
    assert normalize_price(178, 2, "head", GARLIC).cost_per_piece_cents == 89


def test_a_weight_price_of_a_piece_item_counts_pieces_by_their_weight() -> None:
    # A 2 lb bag of medium lemons (109.4 g as bought) for $3: 8.29 lemons, $0.36 each.
    bag = normalize_price(300, 2, "lb", LEMON, portion_grams=109.4)
    assert bag.pieces == pytest.approx(2 * GRAMS_PER_LB / 109.4)
    assert bag.cost_per_piece_cents == pytest.approx(36.18, abs=0.01)
    # Spec worked answer: garlic $4/lb is 9.55 medium heads (47.5 g), $0.42 a head.
    garlic = normalize_price(400, 1, "lb", GARLIC, portion_grams=47.5)
    assert garlic.pieces == pytest.approx(9.55, abs=0.005)
    assert garlic.cost_per_piece_cents == pytest.approx(41.89, abs=0.01)


def test_a_weight_price_of_a_piece_item_needs_one_piece_weight() -> None:
    with pytest.raises(NeedsPortion) as raised:
        normalize_price(300, 2, "lb", LEMON)
    assert raised.value.unit == "each"
    with pytest.raises(NeedsPortion, match="head of garlic"):
        normalize_price(400, 1, "lb", GARLIC)


@pytest.mark.parametrize("unit", ["bunch", "ear", "quart"])
def test_a_piece_item_is_not_sold_by_other_units(unit: str) -> None:
    with pytest.raises(WrongMeasure):
        normalize_price(100, 1, unit, LEMON)


def test_a_piece_item_needs_a_piece_weight_only_for_a_weight_price() -> None:
    assert piece_unit("lb", LEMON) == "each"
    assert piece_unit("oz", GARLIC) == "head"
    assert piece_unit("each", LEMON) is None
    assert piece_unit("head", GARLIC) is None


@pytest.mark.parametrize(
    ("facts", "dollars", "cups", "per_cup"),
    [
        # Spec P1-11 worked answers (owner's market, 2026-11-12), per pound in the shell.
        (WALNUT, 8.00, 1.74, 4.59),
        (PECAN, 5.00, 2.21, 2.27),
    ],
)
def test_nuts_in_the_shell_count_only_the_kernels(
    facts: CatalogFacts, dollars: float, cups: float, per_cup: float
) -> None:
    result = normalize_price(dollars * 100, 1, "lb", facts, in_shell=True)
    assert result.cups == pytest.approx(cups, abs=0.005)
    assert result.cost_per_cup_cents is not None
    assert result.cost_per_cup_cents / 100 == pytest.approx(per_cup, abs=0.005)


def test_shelled_nuts_and_the_same_cost_in_the_shell() -> None:
    """Walnuts at $8/lb in the shell cost a cup what shelled ones do at $17.78/lb."""
    shelled = normalize_price(1778, 1, "lb", WALNUT)
    in_shell = normalize_price(800, 1, "lb", WALNUT, in_shell=True)
    assert shelled.cups == pytest.approx(GRAMS_PER_LB / 117)
    assert shelled.cost_per_cup_cents == pytest.approx(
        in_shell.cost_per_cup_cents or 0, abs=0.5
    )


def test_in_shell_means_nothing_for_an_item_without_a_shell() -> None:
    assert normalize_price(299, 3, "lb", ONION, in_shell=True) == normalize_price(
        299, 3, "lb", ONION
    )


def test_measure_names() -> None:
    assert measure_name("Lemon", "each") == "lemon"
    assert measure_name("Garlic", "head") == "head"
    assert measure_name("Onion", "cup") == "cup"
