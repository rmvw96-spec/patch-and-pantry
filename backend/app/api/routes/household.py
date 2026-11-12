"""The signed-in user's household: weekly produce budget and anonymous members (P1-2).

Each user has one household, created with defaults the first time it is read or updated.
"""

import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, delete, select

from app.api.deps import CurrentUser, SessionDep
from app.core.db import NORTH_ALABAMA
from app.models import (
    Household,
    HouseholdMember,
    HouseholdMemberPublic,
    HouseholdPublic,
    HouseholdUpdate,
    Region,
)

router = APIRouter(prefix="/household", tags=["household"])

# The only region for now (design.md): every household starts here.
DEFAULT_REGION_CODE = NORTH_ALABAMA["code"]


def _find_household(session: Session, owner_id: uuid.UUID) -> Household | None:
    return session.exec(select(Household).where(Household.owner_id == owner_id)).first()


def _create_household(session: Session, owner_id: uuid.UUID) -> Household:
    """Create a household with one member at 2,000 calories, or return the one that exists."""
    region = session.exec(
        select(Region).where(Region.code == DEFAULT_REGION_CODE)
    ).first()
    if region is None:
        raise HTTPException(
            status_code=503,
            detail="Region data is not loaded yet. Run the database setup and try again.",
        )
    household = Household(owner_id=owner_id, region_id=region.id)
    session.add(household)
    session.add(HouseholdMember(household_id=household.id))
    try:
        session.commit()
    except IntegrityError:
        # Two first requests raced and the other created it (owner_id is unique): use that one.
        session.rollback()
        existing = _find_household(session, owner_id)
        if existing is None:
            raise
        return existing
    session.refresh(household)
    return household


def get_or_create_household(session: Session, owner_id: uuid.UUID) -> Household:
    return _find_household(session, owner_id) or _create_household(session, owner_id)


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
