import uuid
from datetime import UTC, date, datetime
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, EmailStr
from pydantic import Field as PydanticField
from sqlalchemy import JSON, CheckConstraint, DateTime, UniqueConstraint
from sqlmodel import AutoString, Field, Relationship, SQLModel


def get_datetime_utc() -> datetime:
    return datetime.now(UTC)


# Shared properties
class UserBase(SQLModel):
    email: EmailStr = Field(unique=True, index=True, max_length=255)
    is_active: bool = True
    is_superuser: bool = False
    full_name: str | None = Field(default=None, max_length=255)


# Properties to receive via API on creation
class UserCreate(UserBase):
    password: str = Field(min_length=8, max_length=128)


class UserRegister(SQLModel):
    email: EmailStr = Field(max_length=255)
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)


# Properties to receive via API on update, all are optional
class UserUpdate(SQLModel):
    email: EmailStr | None = Field(default=None, max_length=255)
    is_active: bool | None = None
    is_superuser: bool | None = None
    full_name: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, min_length=8, max_length=128)


class UserUpdateMe(SQLModel):
    full_name: str | None = Field(default=None, max_length=255)
    email: EmailStr | None = Field(default=None, max_length=255)


class UpdatePassword(SQLModel):
    current_password: str = Field(min_length=8, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


# Database model, database table inferred from class name
class User(UserBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    hashed_password: str
    created_at: datetime | None = Field(default_factory=get_datetime_utc)
    items: list[Item] = Relationship(back_populates="owner", cascade_delete=True)
    studies: list[Study] = Relationship(back_populates="owner", cascade_delete=True)


# Properties to return via API, id is always required
class UserPublic(UserBase):
    id: uuid.UUID
    created_at: datetime | None = None


class UsersPublic(SQLModel):
    data: list[UserPublic]
    count: int


# Shared properties
class ItemBase(SQLModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=255)


# Properties to receive on item creation
class ItemCreate(ItemBase):
    pass


# Properties to receive on item update
class ItemUpdate(SQLModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=255)


# Database model, database table inferred from class name
class Item(ItemBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    created_at: datetime | None = Field(default_factory=get_datetime_utc)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    owner: User | None = Relationship(back_populates="items")


# Properties to return via API, id is always required
class ItemPublic(ItemBase):
    id: uuid.UUID
    owner_id: uuid.UUID
    created_at: datetime | None = None


class ItemsPublic(SQLModel):
    data: list[ItemPublic]
    count: int


# Lifecycle of an asynchronous study job: queued -> running -> succeeded | failed
StudyStatus = Literal["queued", "running", "succeeded", "failed"]


# Properties to receive on study submission
class StudyCreate(SQLModel):
    prompt: str = Field(min_length=1, max_length=2000)


# Structured result of a successful study (mirrors app.agents.study_agent.StudyResult)
class StudyOutcome(SQLModel):
    objective: float
    feasible: bool
    review: str | None = None


# Database model: one row per submitted study job. Postgres is the source of truth for job state.
class Study(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    owner: User | None = Relationship(back_populates="studies")
    prompt: str = Field(max_length=2000)
    status: str = Field(default="queued", max_length=20, index=True)
    result: dict[str, Any] | None = Field(default=None, sa_type=JSON)
    error_reason: str | None = Field(default=None, max_length=50)
    error_message: str | None = Field(default=None, max_length=500)
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc, sa_type=DateTime(timezone=True)
    )
    started_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    finished_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


# Properties to return via API
class StudyPublic(SQLModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    prompt: str
    status: StudyStatus
    result: StudyOutcome | None = None
    error_reason: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


# Generic message
class Message(SQLModel):
    message: str


# JSON payload containing access token
class Token(SQLModel):
    access_token: str
    token_type: str = "bearer"


# Contents of JWT token
class TokenPayload(SQLModel):
    sub: str | None = None


class NewPassword(SQLModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


# ---------------------------------------------------------------------------------------------
# Food planner (Phase 0, P0-3). Field-level spec: docs/specs/phase-0.md.
#
# Table models skip Pydantic validation, so every rule that matters is also a database CHECK
# constraint: closed sets (the Literal types below), calorie levels, percentages, and MM-DD dates.
# Money is integer cents. Quantities of food are grams of the edible part.
# Reference data rows point at the data_snapshot they came from; snapshots are never changed.
# ---------------------------------------------------------------------------------------------

SnapshotSource = Literal[
    "ers_prices",
    "fdc_foundation",
    "fdc_sr_legacy",
    "foodkeeper",
    "planting_calendar",
    "garden_yields",
    "noaa_normals",
    "ams_retail",
    "dga",
    "fda_daily_values",
]
UserSetting = Literal["store_only", "plus_local", "plus_growing"]
CatalogKind = Literal["produce", "supply"]
CatalogScope = Literal["national", "region", "household"]
Season = Literal["spring", "fall"]
VendorType = Literal[
    "store", "farmers_market", "farm_share", "local_grower", "supplier"
]
CapturedVia = Literal["typed", "photo", "receipt", "public_dataset"]
PriceSourceType = Literal[
    "store", "farmers_market", "farm_share", "local_grower", "public_data"
]
Visibility = Literal["private", "shared"]

# Dietary Guidelines calorie levels for the USDA food patterns: 1,000 to 3,200 in steps of 200.
CalorieLevel = Literal[
    1000, 1200, 1400, 1600, 1800, 2000, 2200, 2400, 2600, 2800, 3000, 3200
]
CALORIE_LEVELS: tuple[int, ...] = get_args(CalorieLevel)

# Month and day without a year ("04-03"), for frost dates and planting windows.
MONTH_DAY_PATTERN = r"^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$"


def _one_of(column: str, values: tuple[str | int, ...], name: str) -> CheckConstraint:
    allowed = ", ".join(f"'{v}'" if isinstance(v, str) else str(v) for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


def _literal_check(column: str, literal: Any, name: str) -> CheckConstraint:
    return _one_of(column, get_args(literal), name)


def _month_day_check(column: str, name: str) -> CheckConstraint:
    return CheckConstraint(f"{column} ~ '{MONTH_DAY_PATTERN}'", name=name)


def _percent_check(column: str, name: str) -> CheckConstraint:
    return CheckConstraint(f"{column} BETWEEN 0 AND 100", name=name)


def _tz_now() -> Any:
    return Field(default_factory=get_datetime_utc, sa_type=DateTime(timezone=True))


class DataSnapshot(SQLModel, table=True):
    """One versioned load of an external dataset. Never updated or deleted (a trigger enforces it)."""

    __tablename__ = "data_snapshot"
    __table_args__ = (
        # The same file loaded through a different crosswalk is a different snapshot, because the
        # crosswalk decides which rows of the file reach which catalog items. NULLS NOT DISTINCT:
        # rows loaded before crosswalk fingerprints existed still count as one snapshot each.
        UniqueConstraint(
            "source",
            "sha256",
            "crosswalk_sha256",
            name="uq_data_snapshot_source_sha256_crosswalk",
            postgresql_nulls_not_distinct=True,
        ),
        _literal_check("source", SnapshotSource, "ck_data_snapshot_source"),
        CheckConstraint("row_count >= 0", name="ck_data_snapshot_row_count"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    source: SnapshotSource = Field(sa_type=AutoString(length=40))
    version: str = Field(max_length=100)
    retrieved_at: datetime = Field(sa_type=DateTime(timezone=True))
    url: str = Field(max_length=2000)
    sha256: str = Field(min_length=64, max_length=64)
    row_count: int
    notes: str | None = Field(default=None, max_length=2000)
    # Fingerprint of the crosswalk rows used for the load (app/seed/crosswalk.py).
    crosswalk_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    # When the snapshot was loaded; the latest snapshot of a source is the current one.
    loaded_at: datetime | None = Field(
        default_factory=get_datetime_utc, sa_type=DateTime(timezone=True)
    )


class Region(SQLModel, table=True):
    __table_args__ = (
        _percent_check("frost_risk_pct", "ck_region_frost_risk_pct"),
        _month_day_check("last_spring_frost", "ck_region_last_spring_frost"),
        _month_day_check("first_fall_frost", "ck_region_first_fall_frost"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    code: str = Field(max_length=40, unique=True)
    name: str = Field(max_length=100)
    noaa_station: str = Field(max_length=100)
    ams_region: str = Field(max_length=40)
    # Risk that frost still comes after the spring date (or before the fall date), in percent.
    frost_risk_pct: int = 30
    last_spring_frost: str = Field(max_length=5)
    first_fall_frost: str = Field(max_length=5)
    frost_snapshot_id: uuid.UUID | None = Field(
        default=None, foreign_key="data_snapshot.id"
    )


class Household(SQLModel, table=True):
    """One per user for now. Everything personal hangs off this row and is deleted with the user."""

    __table_args__ = (
        _literal_check("user_setting", UserSetting, "ck_household_user_setting"),
        CheckConstraint(
            "produce_budget_cents_per_week >= 0",
            name="ck_household_produce_budget_cents_per_week",
        ),
        _percent_check("local_premium_pct", "ck_household_local_premium_pct"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", unique=True, nullable=False, ondelete="CASCADE"
    )
    region_id: uuid.UUID = Field(foreign_key="region.id")
    user_setting: UserSetting = Field(
        default="store_only", sa_type=AutoString(length=20)
    )
    produce_budget_cents_per_week: int | None = None
    local_premium_pct: int | None = None
    created_at: datetime | None = _tz_now()


class HouseholdMember(SQLModel, table=True):
    """An anonymous member: a calorie level and nothing else, by design (no names or body data)."""

    __tablename__ = "household_member"
    __table_args__ = (
        _one_of("calorie_level", CALORIE_LEVELS, "ck_household_member_calorie_level"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    household_id: uuid.UUID = Field(
        foreign_key="household.id", nullable=False, ondelete="CASCADE", index=True
    )
    calorie_level: int = 2000
    created_at: datetime | None = _tz_now()


# The most people a household can list, and the largest weekly budget (10,000 dollars). The cap
# keeps a typo from overflowing the integer column; it is not a judgment about anyone's spending.
MAX_HOUSEHOLD_MEMBERS = 20
MAX_PRODUCE_BUDGET_CENTS_PER_WEEK = 1_000_000


class HouseholdMemberPublic(SQLModel):
    id: uuid.UUID
    calorie_level: int


class HouseholdPublic(SQLModel):
    id: uuid.UUID
    region_code: str
    region_name: str
    user_setting: UserSetting
    produce_budget_cents_per_week: int | None
    members: list[HouseholdMemberPublic]


class HouseholdUpdate(BaseModel):
    """Replaces the household's budget and members. Members are calorie levels and nothing else.

    Unknown fields are rejected (extra="forbid") rather than ignored, so a client cannot send
    names, ages, or body data and believe they were stored (design.md, minimal personal data).
    """

    # A plain Pydantic model: SQLModel's config type does not accept extra="forbid".
    model_config = ConfigDict(extra="forbid")

    produce_budget_cents_per_week: int | None = PydanticField(
        default=None, ge=0, le=MAX_PRODUCE_BUDGET_CENTS_PER_WEEK
    )
    members: list[CalorieLevel] = PydanticField(
        min_length=1, max_length=MAX_HOUSEHOLD_MEMBERS
    )


class CatalogItem(SQLModel, table=True):
    """Produce or a garden supply. Lookups resolve household, then region, then national."""

    __tablename__ = "catalog_item"
    __table_args__ = (
        # NULLS NOT DISTINCT: without it, Postgres would allow duplicate national slugs, whose
        # region_id and household_id are both NULL.
        UniqueConstraint(
            "slug",
            "scope",
            "region_id",
            "household_id",
            name="uq_catalog_item_slug_scope",
            postgresql_nulls_not_distinct=True,
        ),
        _literal_check("kind", CatalogKind, "ck_catalog_item_kind"),
        _literal_check("scope", CatalogScope, "ck_catalog_item_scope"),
        # The scope decides which owner column is set.
        CheckConstraint(
            "(scope = 'national' AND region_id IS NULL AND household_id IS NULL)"
            " OR (scope = 'region' AND region_id IS NOT NULL AND household_id IS NULL)"
            " OR (scope = 'household' AND household_id IS NOT NULL)",
            name="ck_catalog_item_scope_owner",
        ),
        # Above 1 is normal for foods that absorb water when cooked: dried beans (about 2.5),
        # collard greens (1.16). ERS's preparation yield is cooked or edible weight per purchased.
        CheckConstraint(
            "preparation_yield_factor > 0",
            name="ck_catalog_item_preparation_yield_factor",
        ),
        CheckConstraint(
            "cup_equivalent_lb > 0", name="ck_catalog_item_cup_equivalent_lb"
        ),
        CheckConstraint(
            "storage_days_min >= 0", name="ck_catalog_item_storage_days_min"
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    slug: str = Field(max_length=100, index=True)
    name: str = Field(max_length=200)
    kind: CatalogKind = Field(sa_type=AutoString(length=20))
    category: str = Field(max_length=100)
    scope: CatalogScope = Field(default="national", sa_type=AutoString(length=20))
    region_id: uuid.UUID | None = Field(default=None, foreign_key="region.id")
    household_id: uuid.UUID | None = Field(
        default=None, foreign_key="household.id", ondelete="CASCADE"
    )
    # Produce facts; empty for supplies.
    # Edible (or cooked) weight per purchased weight; ERS's preparation yield factor.
    preparation_yield_factor: float | None = None
    cup_equivalent_lb: float | None = None  # pounds of edible food per cup equivalent
    storage_days_min: int | None = None
    storage_location: str | None = Field(default=None, max_length=40)
    # Storage facts come from a different dataset (FoodKeeper) than conversions (ERS).
    storage_snapshot_id: uuid.UUID | None = Field(
        default=None, foreign_key="data_snapshot.id"
    )
    # The item whose facts this one borrows, when it has none of its own.
    stand_in_for_id: uuid.UUID | None = Field(
        default=None, foreign_key="catalog_item.id", ondelete="SET NULL"
    )
    # Every catalog fact has a source, including "stand-in" when borrowed.
    conversion_source: str = Field(min_length=1, max_length=500)
    # The snapshot the conversion facts (yield factor, cup weight) came from.
    snapshot_id: uuid.UUID | None = Field(default=None, foreign_key="data_snapshot.id")


class CatalogNutrient(SQLModel, table=True):
    __tablename__ = "catalog_nutrient"
    __table_args__ = (
        CheckConstraint("amount_per_100g >= 0", name="ck_catalog_nutrient_amount"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    catalog_item_id: uuid.UUID = Field(
        foreign_key="catalog_item.id", nullable=False, ondelete="CASCADE", index=True
    )
    nutrient: str = Field(max_length=100)  # FoodData Central nutrient name or number
    amount_per_100g: float
    unit: str = Field(max_length=20)
    snapshot_id: uuid.UUID | None = Field(default=None, foreign_key="data_snapshot.id")


class CropProfile(SQLModel, table=True):
    __tablename__ = "crop_profile"
    __table_args__ = (
        CheckConstraint("yield_lb_per_sq_ft > 0", name="ck_crop_profile_yield"),
        CheckConstraint(
            "days_to_maturity_min > 0 AND days_to_maturity_max >= days_to_maturity_min",
            name="ck_crop_profile_days_to_maturity",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    catalog_item_id: uuid.UUID = Field(
        foreign_key="catalog_item.id", nullable=False, ondelete="CASCADE", index=True
    )
    yield_lb_per_sq_ft: float
    yield_source: str = Field(max_length=500)
    days_to_maturity_min: int | None = None
    days_to_maturity_max: int | None = None
    snapshot_id: uuid.UUID | None = Field(default=None, foreign_key="data_snapshot.id")


class PlantingWindow(SQLModel, table=True):
    __tablename__ = "planting_window"
    __table_args__ = (
        _literal_check("season", Season, "ck_planting_window_season"),
        _month_day_check("start_md", "ck_planting_window_start_md"),
        _month_day_check("end_md", "ck_planting_window_end_md"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    catalog_item_id: uuid.UUID = Field(
        foreign_key="catalog_item.id", nullable=False, ondelete="CASCADE", index=True
    )
    region_id: uuid.UUID = Field(foreign_key="region.id")
    season: Season = Field(sa_type=AutoString(length=10))
    start_md: str = Field(max_length=5)
    end_md: str = Field(max_length=5)
    # Derived North Alabama dates are unverified until checked against Extension's ANR-1165.
    verified: bool = False
    snapshot_id: uuid.UUID | None = Field(default=None, foreign_key="data_snapshot.id")


class Vendor(SQLModel, table=True):
    """A store, market, farm share, or grower, private to one household. Area only, no address."""

    __table_args__ = (_literal_check("vendor_type", VendorType, "ck_vendor_type"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    household_id: uuid.UUID = Field(
        foreign_key="household.id", nullable=False, ondelete="CASCADE", index=True
    )
    name: str = Field(max_length=200)
    vendor_type: VendorType = Field(sa_type=AutoString(length=20))
    region_id: uuid.UUID | None = Field(default=None, foreign_key="region.id")
    area: str | None = Field(
        default=None, max_length=100
    )  # city or area; never a street address


class PriceObservation(SQLModel, table=True):
    """A price as written. Private by default; public datasets have no household."""

    __tablename__ = "price_observation"
    __table_args__ = (
        _literal_check(
            "captured_via", CapturedVia, "ck_price_observation_captured_via"
        ),
        _literal_check(
            "source_type", PriceSourceType, "ck_price_observation_source_type"
        ),
        _literal_check("visibility", Visibility, "ck_price_observation_visibility"),
        CheckConstraint("price_cents >= 0", name="ck_price_observation_price_cents"),
        CheckConstraint("quantity > 0", name="ck_price_observation_quantity"),
        CheckConstraint("grams_edible > 0", name="ck_price_observation_grams_edible"),
        # A household's own capture, or a public dataset row with no household: never both.
        CheckConstraint(
            "(household_id IS NULL) = (captured_via = 'public_dataset')",
            name="ck_price_observation_public_dataset",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    household_id: uuid.UUID | None = Field(
        default=None, foreign_key="household.id", ondelete="CASCADE", index=True
    )
    catalog_item_id: uuid.UUID = Field(
        foreign_key="catalog_item.id", nullable=False, ondelete="CASCADE", index=True
    )
    vendor_id: uuid.UUID | None = Field(
        default=None, foreign_key="vendor.id", ondelete="SET NULL"
    )
    price_cents: int
    quantity: float
    unit: str = Field(max_length=50)  # as written, for example "3 lb bag"
    grams_edible: float | None = None  # filled by normalize_price (Phase 1)
    observed_on: date
    captured_via: CapturedVia = Field(sa_type=AutoString(length=20))
    source_type: PriceSourceType = Field(sa_type=AutoString(length=20))
    region_id: uuid.UUID | None = Field(default=None, foreign_key="region.id")
    visibility: Visibility = Field(default="private", sa_type=AutoString(length=10))
    # AMS report prices are advertised sale prices: never show them as shelf prices.
    is_sale_price: bool = False
    raw_text: str | None = Field(
        default=None, max_length=500
    )  # the typed line; never an image
    snapshot_id: uuid.UUID | None = Field(default=None, foreign_key="data_snapshot.id")
    created_at: datetime | None = _tz_now()
