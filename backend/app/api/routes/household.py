"""The signed-in user's household: weekly produce budget and anonymous members (P1-2).

Each user has one household, created with defaults the first time it is read or updated.
"""

from fastapi import APIRouter
from sqlmodel import Session, col, delete, select

from app.api.deps import CurrentUser, SessionDep
from app.households import get_or_create_household
from app.models import (
    Household,
    HouseholdMember,
    HouseholdMemberPublic,
    HouseholdPublic,
    HouseholdUpdate,
    Region,
)

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
