"""A small, known catalog for matcher and capture tests, inside one rolled-back transaction.

The national catalog in the shared development database may hold real data (or none, in CI), so
these fixtures empty it inside the test's transaction and load a fixed set instead. Nothing
survives the rollback.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlmodel import Session, col, delete, select

from app.core.db import engine
from app.models import (
    CatalogItem,
    CatalogPortion,
    DataSnapshot,
    Household,
    Region,
    User,
    Vendor,
)

# slug: (name, yield factor, cup lb). None: no conversion of its own.
ITEMS: dict[str, tuple[str, float | None, float | None]] = {
    "onion": ("Onion", 0.9, 0.3527),
    "potato": ("Potato", 0.8113, 0.2646),
    "tomato": ("Tomato", 0.91, 0.3748),
    "collards": ("Collards", 1.16, 0.2866),
    "kale": ("Kale", 1.05, 0.2866),
    "turnip-greens": ("Turnip greens", 0.75, 0.3197),
    "mustard-greens": ("Mustard greens", None, None),
    "swiss-chard": ("Swiss chard", None, None),
    "zucchini": ("Zucchini", 0.7695, 0.3968),
    "yellow-squash": ("Yellow squash", None, None),  # stand-in for zucchini
    "butternut-squash": ("Butternut squash", 0.7136, 0.4519),
    "acorn-squash": ("Acorn squash", 0.4586, 0.4519),
    "sweet-pepper": ("Sweet pepper", 0.82, 0.2646),
    "hot-pepper": ("Hot pepper", None, None),
    "lettuce": ("Lettuce", 0.94, 0.2094),
    "sweet-corn": ("Sweet corn", 0.54, 0.3638),
    "eggplant": ("Eggplant", None, None),
    "broccoli": ("Broccoli", 0.78, 0.3417),
}
# slug: (name, yield factor, cup pints). Juices are measured by volume.
JUICES: dict[str, tuple[str, float, float]] = {
    "orange-juice": ("Orange juice", 1.0, 0.5),
}
# slug: FoodKeeper range in days (refrigerator or pantry). Tomato has none ("When ripe").
STORAGE: dict[str, tuple[int, int]] = {
    "onion": (30, 30),
    "potato": (30, 60),
    "collards": (1, 4),
    "kale": (3, 5),
    "lettuce": (7, 14),
    "sweet-pepper": (4, 5),
}
# slug: [(unit, size, grams as purchased)]
PIECES: dict[str, list[tuple[str, str | None, float]]] = {
    "tomato": [
        ("each", "small", 100.0),
        ("each", "medium", 135.2),
        ("each", "large", 200.0),
    ],
    "sweet-pepper": [("each", "medium", 145.1), ("each", "large", 200.0)],
    "lettuce": [("head", None, 666.0)],
    "sweet-corn": [("ear", "medium", 283.3333)],
}


@pytest.fixture
def db_session() -> Iterator[Session]:
    connection = engine.connect()
    outer = connection.begin()
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        yield session
    outer.rollback()
    connection.close()


@pytest.fixture
def catalog(db_session: Session) -> dict[str, CatalogItem]:
    session = db_session
    session.exec(delete(CatalogItem).where(col(CatalogItem.scope) == "national"))
    snapshot = DataSnapshot(
        source="usda_sr28",
        version="test",
        retrieved_at=datetime(2026, 11, 11, tzinfo=UTC),
        url="https://example.com/test",
        sha256=uuid.uuid4().hex * 2,
        row_count=1,
    )
    session.add(snapshot)
    items: dict[str, CatalogItem] = {}
    for slug, (name, yield_factor, cup_lb) in ITEMS.items():
        items[slug] = CatalogItem(
            slug=slug,
            name=name,
            kind="produce",
            category="vegetable",
            preparation_yield_factor=yield_factor,
            cup_equivalent_lb=cup_lb,
            conversion_source=f"test: {name}",
            storage_days_min=STORAGE.get(slug, (None, None))[0],
            storage_days_max=STORAGE.get(slug, (None, None))[1],
            storage_location="refrigerator" if slug in STORAGE else None,
        )
        session.add(items[slug])
    for slug, (name, yield_factor, cup_pints) in JUICES.items():
        items[slug] = CatalogItem(
            slug=slug,
            name=name,
            kind="produce",
            category="fruit",
            preparation_yield_factor=yield_factor,
            cup_equivalent_pints=cup_pints,
            conversion_source=f"test: {name}",
        )
        session.add(items[slug])
    session.flush()
    items["yellow-squash"].stand_in_for_id = items["zucchini"].id
    for slug, pieces in PIECES.items():
        for unit, size, grams in pieces:
            session.add(
                CatalogPortion(
                    catalog_item_id=items[slug].id,
                    unit=unit,
                    size=size,
                    grams=grams,
                    edible_grams=grams * 0.9,
                    refuse_pct=10,
                    description=f"{size or ''} {unit}".strip(),
                    source=f"test piece: {slug} {size} {unit}",
                    snapshot_id=snapshot.id,
                )
            )
    session.flush()
    return items


def make_household(session: Session, **fields: Any) -> Household:
    user = User(
        email=f"capture-{uuid.uuid4().hex[:12]}@example.com", hashed_password="x"
    )
    session.add(user)
    session.flush()
    region = session.exec(select(Region).where(Region.code == "north-al")).one()
    household = Household(owner_id=user.id, region_id=region.id, **fields)
    session.add(household)
    session.flush()
    return household


@pytest.fixture
def household(db_session: Session) -> Household:
    return make_household(db_session)


@pytest.fixture
def vendor(db_session: Session, household: Household) -> Vendor:
    vendor = Vendor(household_id=household.id, name="Corner store", vendor_type="store")
    db_session.add(vendor)
    db_session.flush()
    return vendor
