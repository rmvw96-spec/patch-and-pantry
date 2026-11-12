"""Each user's household, created with defaults the first time it is needed.

Used by the household API and by agent runs, which work for the user's household.
"""

import uuid

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.db import NORTH_ALABAMA
from app.models import Household, HouseholdMember, Region

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
