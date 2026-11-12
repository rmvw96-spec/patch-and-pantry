"""The household's stores, markets, farm shares, and growers (minimal; the price book is P1-7)."""

import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import col, select

from app.api.deps import CurrentUser, SessionDep
from app.households import get_or_create_household
from app.models import Vendor, VendorType

router = APIRouter(prefix="/vendors", tags=["vendors"])


class VendorCreate(BaseModel):
    """A place the household buys produce. Area only, never a street address."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    vendor_type: VendorType = "store"
    area: str | None = Field(default=None, max_length=100)


class VendorPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    vendor_type: VendorType
    area: str | None = None


@router.get("/", response_model=list[VendorPublic])
def read_vendors(session: SessionDep, current_user: CurrentUser) -> Any:
    """Your stores and markets, by name."""
    household = get_or_create_household(session, current_user.id)
    return session.exec(
        select(Vendor)
        .where(Vendor.household_id == household.id)
        .order_by(col(Vendor.name))
    ).all()


@router.post("/", response_model=VendorPublic, status_code=201)
def create_vendor(
    session: SessionDep, current_user: CurrentUser, vendor_in: VendorCreate
) -> Any:
    """Add a store or market."""
    household = get_or_create_household(session, current_user.id)
    vendor = Vendor(household_id=household.id, **vendor_in.model_dump())
    session.add(vendor)
    session.commit()
    session.refresh(vendor)
    return vendor
