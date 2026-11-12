"""Food planner core tables (Phase 0, P0-3) against real Postgres.

Each test works inside one transaction that is rolled back at the end, so nothing is left in the
shared development database. Constraint and cascade checks run in Postgres itself when a
statement executes, so flushing is enough to trigger them.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlmodel import Session, SQLModel, func, select

from app.core.db import NORTH_ALABAMA, engine
from app.models import (
    CatalogItem,
    CatalogPortion,
    DataSnapshot,
    Household,
    HouseholdItemSetting,
    HouseholdMember,
    PriceObservation,
    Region,
    StorageReference,
    User,
    Vendor,
)


@pytest.fixture
def session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
        session.rollback()


@pytest.fixture
def region(session: Session) -> Region:
    region = session.exec(select(Region).where(Region.code == "north-al")).one()
    return region


def _user(session: Session) -> User:
    user = User(email=f"food-{uuid.uuid4().hex[:12]}@example.com", hashed_password="x")
    session.add(user)
    session.flush()
    return user


def _household(session: Session, region: Region, **fields: Any) -> Household:
    household = Household(owner_id=_user(session).id, region_id=region.id, **fields)
    session.add(household)
    session.flush()
    return household


def _produce(**fields: Any) -> CatalogItem:
    values: dict[str, Any] = {
        "slug": f"test-onion-{uuid.uuid4().hex[:8]}",
        "name": "Onion",
        "kind": "produce",
        "category": "vegetable",
        "conversion_source": "test",
    }
    return CatalogItem(**(values | fields))


def _price(**fields: Any) -> PriceObservation:
    values: dict[str, Any] = {
        "price_cents": 299,
        "quantity": 3,
        "unit": "3 lb bag",
        "observed_on": date(2026, 11, 11),
        "captured_via": "typed",
        "source_type": "store",
    }
    return PriceObservation(**(values | fields))


def _count(session: Session, model: type[SQLModel], **where: Any) -> int:
    statement = select(func.count()).select_from(model)
    for column, value in where.items():
        statement = statement.where(getattr(model, column) == value)
    return session.exec(statement).one()


def _rejects(session: Session, row: SQLModel, constraint: str) -> None:
    session.add(row)
    with pytest.raises(IntegrityError, match=constraint):
        session.flush()


# ---------- seed data ----------


def test_north_alabama_region_is_seeded(region: Region) -> None:
    """init_db (run by prestart.sh, and by the test session) seeds the region from design.md."""
    assert region.model_dump(include=set(NORTH_ALABAMA)) == NORTH_ALABAMA


# ---------- deleting a user deletes everything personal ----------


def test_deleting_a_user_deletes_their_household_data(
    session: Session, region: Region
) -> None:
    household = _household(session, region, produce_budget_cents_per_week=4000)
    owner = session.get(User, household.owner_id)
    item = _produce(scope="household", household_id=household.id)
    vendor = Vendor(household_id=household.id, name="Corner store", vendor_type="store")
    session.add_all([HouseholdMember(household_id=household.id), item, vendor])
    session.flush()
    session.add(
        _price(household_id=household.id, catalog_item_id=item.id, vendor_id=vendor.id)
    )
    # Shared rows that must survive: a national item with a public-dataset price.
    national = _produce()
    session.add(national)
    session.flush()
    session.add(
        _price(
            catalog_item_id=national.id,
            captured_via="public_dataset",
            source_type="public_data",
        )
    )
    session.flush()

    household_id, national_id = household.id, national.id
    session.delete(owner)
    session.flush()
    session.expunge_all()  # forget loaded rows; read what Postgres now holds

    assert session.get(Household, household_id) is None
    assert _count(session, HouseholdMember, household_id=household_id) == 0
    assert _count(session, Vendor, household_id=household_id) == 0
    assert _count(session, CatalogItem, household_id=household_id) == 0
    assert _count(session, PriceObservation, household_id=household_id) == 0
    assert session.get(CatalogItem, national_id) is not None
    assert _count(session, PriceObservation, catalog_item_id=national_id) == 1


# ---------- invalid values are rejected by the database ----------


@pytest.mark.parametrize("calorie_level", [0, 999, 1100, 2100, 3400])
def test_invalid_calorie_level_is_rejected(
    session: Session, region: Region, calorie_level: int
) -> None:
    household = _household(session, region)
    _rejects(
        session,
        HouseholdMember(household_id=household.id, calorie_level=calorie_level),
        "ck_household_member_calorie_level",
    )


@pytest.mark.parametrize("calorie_level", [1000, 1600, 2000, 3200])
def test_valid_calorie_levels_are_accepted(
    session: Session, region: Region, calorie_level: int
) -> None:
    household = _household(session, region)
    session.add(HouseholdMember(household_id=household.id, calorie_level=calorie_level))
    session.flush()


@pytest.mark.parametrize("pct", [-1, 101])
def test_local_premium_outside_0_to_100_is_rejected(
    session: Session, region: Region, pct: int
) -> None:
    user = _user(session)
    _rejects(
        session,
        Household(owner_id=user.id, region_id=region.id, local_premium_pct=pct),
        "ck_household_local_premium_pct",
    )


@pytest.mark.parametrize("pct", [-1, 101])
def test_frost_risk_outside_0_to_100_is_rejected(session: Session, pct: int) -> None:
    _rejects(
        session,
        Region(**(NORTH_ALABAMA | {"code": "test-region", "frost_risk_pct": pct})),
        "ck_region_frost_risk_pct",
    )


@pytest.mark.parametrize("month_day", ["4-03", "04-3", "13-01", "04-32", "04/03"])
def test_frost_dates_must_be_month_day(session: Session, month_day: str) -> None:
    _rejects(
        session,
        Region(
            **(NORTH_ALABAMA | {"code": "test-region", "last_spring_frost": month_day})
        ),
        "ck_region_last_spring_frost",
    )


def test_closed_sets_are_enforced(session: Session, region: Region) -> None:
    """Table models skip validation, so the Literal type alone would let this value through."""
    household = Household(
        owner_id=_user(session).id,
        region_id=region.id,
        user_setting="everything",  # type: ignore[arg-type]  # invalid on purpose
    )
    _rejects(session, household, "ck_household_user_setting")


def test_one_household_per_user(session: Session, region: Region) -> None:
    household = _household(session, region)
    _rejects(
        session,
        Household(owner_id=household.owner_id, region_id=region.id),
        "household_owner_id_key",
    )


# ---------- catalog and prices ----------


@pytest.mark.parametrize("factor", [0.4586, 1.0, 1.16, 2.4692])  # ERS values
def test_preparation_yield_factor_may_exceed_one(
    session: Session, factor: float
) -> None:
    """Dried beans and cooked greens weigh more prepared than purchased."""
    session.add(_produce(preparation_yield_factor=factor))
    session.flush()


@pytest.mark.parametrize("factor", [0, -0.5])
def test_preparation_yield_factor_must_be_positive(
    session: Session, factor: float
) -> None:
    _rejects(
        session,
        _produce(preparation_yield_factor=factor),
        "ck_catalog_item_preparation_yield_factor",
    )


def test_national_slugs_are_unique(session: Session) -> None:
    """Without NULLS NOT DISTINCT, two national rows (both owner columns NULL) could share a slug."""
    session.add(_produce(slug="test-dup"))
    session.flush()
    _rejects(session, _produce(slug="test-dup"), "uq_catalog_item_slug_scope")


def test_household_item_can_share_a_national_slug(
    session: Session, region: Region
) -> None:
    """A household's correction overrides a national item under the same slug."""
    household = _household(session, region)
    session.add(_produce(slug="test-shared"))
    session.add(
        _produce(slug="test-shared", scope="household", household_id=household.id)
    )
    session.flush()


HOUSEHOLD = object()  # placeholder for the test household's id


@pytest.mark.parametrize(
    "fields",
    [
        {"scope": "national", "household_id": HOUSEHOLD},
        {"scope": "region"},  # region scope without a region
        {"scope": "household"},  # household scope without a household
    ],
)
def test_scope_must_match_its_owner_column(
    session: Session, region: Region, fields: dict[str, Any]
) -> None:
    household = _household(session, region)
    values = {k: household.id if v is HOUSEHOLD else v for k, v in fields.items()}
    _rejects(session, _produce(**values), "ck_catalog_item_scope_owner")


@pytest.mark.parametrize(
    ("household", "captured_via"),
    [(False, "typed"), (True, "public_dataset")],
)
def test_public_dataset_prices_have_no_household(
    session: Session, region: Region, household: bool, captured_via: str
) -> None:
    item = _produce()
    session.add(item)
    session.flush()
    household_id = _household(session, region).id if household else None
    _rejects(
        session,
        _price(
            catalog_item_id=item.id,
            household_id=household_id,
            captured_via=captured_via,
        ),
        "ck_price_observation_public_dataset",
    )


def test_prices_are_private_by_default_and_not_sale_prices(
    session: Session, region: Region
) -> None:
    household = _household(session, region)
    item = _produce()
    session.add(item)
    session.flush()
    price = _price(household_id=household.id, catalog_item_id=item.id)
    session.add(price)
    session.flush()
    session.refresh(price)
    assert price.visibility == "private"
    assert price.is_sale_price is False


# ---------- snapshots are never changed ----------


def _snapshot() -> DataSnapshot:
    return DataSnapshot(
        source="ers_prices",
        version="2023",
        retrieved_at=datetime(2026, 11, 11, tzinfo=UTC),
        url="https://www.ers.usda.gov/data-products/fruit-and-vegetable-prices",
        sha256=uuid.uuid4().hex * 2,
        row_count=1,
    )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE data_snapshot SET version = 'changed' WHERE id = :id",
        "DELETE FROM data_snapshot WHERE id = :id",
    ],
)
def test_snapshots_cannot_be_updated_or_deleted(
    session: Session, statement: str
) -> None:
    snapshot = _snapshot()
    session.add(snapshot)
    session.flush()
    with pytest.raises(DBAPIError, match="never updated or deleted"):
        session.execute(text(statement), {"id": snapshot.id})


def test_same_source_and_hash_is_one_snapshot(session: Session) -> None:
    first = _snapshot()
    session.add(first)
    session.flush()
    duplicate = _snapshot()
    duplicate.sha256 = first.sha256
    _rejects(session, duplicate, "uq_data_snapshot_source_sha256")


# ---------- portions and capture weights (Phase 1, P1-3) ----------


def _portion(item_id: uuid.UUID, **fields: Any) -> CatalogPortion:
    values: dict[str, Any] = {
        "catalog_item_id": item_id,
        "unit": "bunch",
        "grams": 450.0,
        "description": "bunch, rubber-banded",
        "source": "owner measurement",
    }
    return CatalogPortion(**(values | fields))


@pytest.fixture
def item(session: Session) -> CatalogItem:
    item = _produce()
    session.add(item)
    session.flush()
    return item


def test_owner_measured_portion_records_its_date(
    session: Session, item: CatalogItem
) -> None:
    """A portion someone weighed needs no snapshot, but must say when it was weighed."""
    session.add(_portion(item.id, measured_on=date(2026, 11, 14)))
    session.flush()
    _rejects(session, _portion(item.id), "ck_catalog_portion_provenance")


def test_loaded_portion_keeps_edible_weight_and_refuse(
    session: Session, item: CatalogItem
) -> None:
    snapshot = DataSnapshot(
        source="usda_sr28",
        version="SR28 (2015)",
        retrieved_at=datetime(2026, 11, 11, tzinfo=UTC),
        url="https://example.com/sr28asc.zip",
        sha256="a" * 64,
        row_count=1,
    )
    session.add(snapshot)
    session.flush()
    session.add(
        _portion(item.id, snapshot_id=snapshot.id, edible_grams=123.0, refuse_pct=9)
    )
    session.flush()
    _rejects(
        session,
        _portion(item.id, snapshot_id=snapshot.id),
        "ck_catalog_portion_loaded_has_refuse",
    )


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"unit": "dozen"}, "ck_catalog_portion_unit"),
        ({"size": "huge"}, "ck_catalog_portion_size"),
        ({"grams": 0}, "ck_catalog_portion_grams"),
        ({"edible_grams": -1}, "ck_catalog_portion_edible_grams"),
        ({"refuse_pct": 100}, "ck_catalog_portion_refuse_pct"),
    ],
)
def test_portion_values_are_checked(
    session: Session, item: CatalogItem, fields: dict[str, Any], constraint: str
) -> None:
    _rejects(
        session, _portion(item.id, measured_on=date(2026, 11, 14), **fields), constraint
    )


def test_deleting_an_item_deletes_its_portions(
    session: Session, item: CatalogItem
) -> None:
    session.add(_portion(item.id, measured_on=date(2026, 11, 14)))
    session.flush()
    session.delete(item)
    session.flush()
    assert _count(session, CatalogPortion, catalog_item_id=item.id) == 0


@pytest.mark.parametrize("weight_source", ["weighed", "estimated"])
def test_a_capture_weight_says_how_it_was_given(
    session: Session, region: Region, item: CatalogItem, weight_source: str
) -> None:
    household = _household(session, region)
    session.add(
        _price(
            catalog_item_id=item.id,
            household_id=household.id,
            unit="bunch",
            unit_grams=280.0,
            weight_source=weight_source,
        )
    )
    session.flush()


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"unit_grams": 280.0}, "ck_price_observation_unit_grams_source"),
        ({"weight_source": "estimated"}, "ck_price_observation_unit_grams_source"),
        ({"unit_grams": 280.0, "weight_source": "guessed"}, "ck_price_observation_weight_source"),
        ({"unit_grams": 0.0, "weight_source": "weighed"}, "ck_price_observation_unit_grams"),
    ],
)  # fmt: skip
def test_capture_weights_are_checked(
    session: Session,
    region: Region,
    item: CatalogItem,
    fields: dict[str, Any],
    constraint: str,
) -> None:
    household = _household(session, region)
    _rejects(
        session,
        _price(catalog_item_id=item.id, household_id=household.id, **fields),
        constraint,
    )


# ---------- storage ranges, item settings, price sizes (Phase 1, P1-6) ----------


def test_storage_maximum_is_not_below_the_minimum(session: Session) -> None:
    _rejects(
        session,
        _produce(storage_days_min=5, storage_days_max=4),
        "ck_catalog_item_storage_days_max",
    )


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"key": "raw_greens"}, "ck_storage_reference_key"),
        ({"location": "cellar"}, "ck_storage_reference_location"),
        ({"days_min": -1}, "ck_storage_reference_days_min"),
        ({"days_min": 5, "days_max": 4}, "ck_storage_reference_days_max"),
    ],
)
def test_storage_reference_values_are_checked(
    session: Session, fields: dict[str, Any], constraint: str
) -> None:
    snapshot = DataSnapshot(
        source="foodkeeper",
        version="test",
        retrieved_at=datetime(2026, 11, 11, tzinfo=UTC),
        url="https://example.com",
        sha256="b" * 64,
        row_count=1,
    )
    session.add(snapshot)
    session.flush()
    values: dict[str, Any] = {
        "key": "cooked_leftovers",
        "location": "freezer",
        "days_min": 30,
        "days_max": 60,
        "description": "test",
        "snapshot_id": snapshot.id,
    }
    _rejects(session, StorageReference(**(values | fields)), constraint)


def test_one_setting_per_household_and_item(
    session: Session, region: Region, item: CatalogItem
) -> None:
    household = _household(session, region)
    session.add(
        HouseholdItemSetting(
            household_id=household.id, catalog_item_id=item.id, freeze_extras=True
        )
    )
    session.flush()
    _rejects(
        session,
        HouseholdItemSetting(household_id=household.id, catalog_item_id=item.id),
        "uq_household_item_setting_item",
    )


def test_price_sizes_are_checked(
    session: Session, region: Region, item: CatalogItem
) -> None:
    household = _household(session, region)
    _rejects(
        session,
        _price(catalog_item_id=item.id, household_id=household.id, size="jumbo"),
        "ck_price_observation_size",
    )
