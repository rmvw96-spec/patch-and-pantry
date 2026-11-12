"""The signed-in user's household: weekly produce budget and anonymous members (P1-2).

Each user has one household, created with defaults the first time it is read or updated.
"""

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, col, delete, select

from app.api.deps import CurrentUser, SessionDep
from app.households import get_or_create_household
from app.models import (
    Household,
    HouseholdItemSetting,
    HouseholdMember,
    HouseholdMemberPublic,
    HouseholdPublic,
    HouseholdUpdate,
    Region,
)
from app.prices import PriceError, visible_item
from app.tools.catalog import remember_alias

router = APIRouter(prefix="/household", tags=["household"])


def _public(session: Session, household: Household) -> HouseholdPublic:
    region = session.get(Region, household.region_id)
    if region is None:  # the foreign key makes this impossible; narrows the type
        raise RuntimeError(f"household {household.id} has no region")
    members = session.exec(
        select(HouseholdMember)
        .where(HouseholdMember.household_id == household.id)
        # Members are created in the order they were sent, so this keeps the client's order.
        .order_by(col(HouseholdMember.created_at), col(HouseholdMember.id))
    ).all()
    return HouseholdPublic(
        id=household.id,
        region_code=region.code,
        region_name=region.name,
        user_setting=household.user_setting,
        produce_budget_cents_per_week=household.produce_budget_cents_per_week,
        members=[
            HouseholdMemberPublic(id=m.id, calorie_level=m.calorie_level)
            for m in members
        ],
    )


@router.get("/", response_model=HouseholdPublic)
def read_household(session: SessionDep, current_user: CurrentUser) -> HouseholdPublic:
    """
    Get your household, creating it on first access (North Alabama, one member at 2,000 calories).
    """
    return _public(session, get_or_create_household(session, current_user.id))


@router.put("/", response_model=HouseholdPublic)
def update_household(
    session: SessionDep, current_user: CurrentUser, household_in: HouseholdUpdate
) -> HouseholdPublic:
    """
    Replace your weekly produce budget (in cents; null for none) and your members.

    Members are a list of calorie levels, one per person; the list replaces the current members.
    """
    household = get_or_create_household(session, current_user.id)
    household.produce_budget_cents_per_week = household_in.produce_budget_cents_per_week
    session.add(household)
    session.exec(
        delete(HouseholdMember).where(col(HouseholdMember.household_id) == household.id)
    )
    session.add_all(
        HouseholdMember(household_id=household.id, calorie_level=level)
        for level in household_in.members
    )
    session.commit()
    session.refresh(household)
    return _public(session, household)


# ---------- your names for items, and per-item settings (P1-7) ----------


class AliasCreate(BaseModel):
    """Your name for a catalog item, saved when you choose the item a line meant (D7)."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=200)
    catalog_item_id: uuid.UUID


class AliasPublic(BaseModel):
    text: str  # folded: lower case, singular
    catalog_item_id: uuid.UUID


@router.post("/aliases", response_model=AliasPublic, status_code=201)
def create_alias(
    session: SessionDep, current_user: CurrentUser, alias_in: AliasCreate
) -> AliasPublic:
    """Remember that a typed name means an item, so the same text matches exactly next time."""
    household = get_or_create_household(session, current_user.id)
    try:
        item = visible_item(session, household, alias_in.catalog_item_id)
        alias = remember_alias(session, household, alias_in.text, item)
    except (PriceError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.commit()
    return AliasPublic(text=alias.text, catalog_item_id=alias.catalog_item_id)


class ItemSettingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    freeze_extras: bool


class ItemSettingPublic(BaseModel):
    catalog_item_id: uuid.UUID
    freeze_extras: bool


@router.get("/item-settings", response_model=list[ItemSettingPublic])
def read_item_settings(
    session: SessionDep, current_user: CurrentUser
) -> list[ItemSettingPublic]:
    """Your per-item choices, such as cooking and freezing extras (D4)."""
    household = get_or_create_household(session, current_user.id)
    settings = session.exec(
        select(HouseholdItemSetting).where(
            HouseholdItemSetting.household_id == household.id
        )
    ).all()
    return [
        ItemSettingPublic(
            catalog_item_id=s.catalog_item_id, freeze_extras=s.freeze_extras
        )
        for s in settings
    ]


@router.put("/item-settings/{catalog_item_id}", response_model=ItemSettingPublic)
def update_item_setting(
    session: SessionDep,
    current_user: CurrentUser,
    catalog_item_id: uuid.UUID,
    setting_in: ItemSettingUpdate,
) -> ItemSettingPublic:
    """Set whether you cook this item on purchase and freeze what you won't eat in a few days."""
    household = get_or_create_household(session, current_user.id)
    try:
        item = visible_item(session, household, catalog_item_id)
    except PriceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    setting = session.exec(
        select(HouseholdItemSetting).where(
            HouseholdItemSetting.household_id == household.id,
            HouseholdItemSetting.catalog_item_id == item.id,
        )
    ).first() or HouseholdItemSetting(
        household_id=household.id, catalog_item_id=item.id
    )
    setting.freeze_extras = setting_in.freeze_extras
    session.add(setting)
    session.commit()
    return ItemSettingPublic(
        catalog_item_id=item.id, freeze_extras=setting.freeze_extras
    )
