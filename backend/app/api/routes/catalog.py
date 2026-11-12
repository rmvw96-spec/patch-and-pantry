"""The produce catalog the household can see, for choosing an item (P1-7)."""

import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlmodel import col, or_, select

from app.api.deps import CurrentUser, SessionDep
from app.households import get_or_create_household
from app.models import CatalogItem

router = APIRouter(prefix="/catalog", tags=["catalog"])


class CatalogItemPublic(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    category: str


@router.get("/", response_model=list[CatalogItemPublic])
def read_catalog(session: SessionDep, current_user: CurrentUser) -> Any:
    """Produce items: national, your region's, and your own, by name."""
    household = get_or_create_household(session, current_user.id)
    items = session.exec(
        select(CatalogItem).where(
            CatalogItem.kind == "produce",
            or_(
                col(CatalogItem.scope) == "national",
                col(CatalogItem.region_id) == household.region_id,
                col(CatalogItem.household_id) == household.id,
            ),
        )
    ).all()
    # Sorted here, not by the database, so the order is the same under any collation:
    # case-insensitive, so "Frozen Brussels sprouts" sits among the other frozen items.
    return [
        CatalogItemPublic(id=i.id, slug=i.slug, name=i.name, category=i.category)
        for i in sorted(items, key=lambda i: (i.name.casefold(), i.name))
    ]
