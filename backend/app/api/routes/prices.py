"""The household's price book (P1-7): confirm drafts, list, edit, and delete prices."""

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.households import get_or_create_household
from app.models import Message
from app.prices import (
    PriceError,
    PricePublic,
    PricesCreate,
    PriceUpdate,
    own_price,
    price_book,
    save_prices,
    update_price,
)

router = APIRouter(prefix="/prices", tags=["prices"])


def _by_ids(book: list[PricePublic], ids: set[uuid.UUID]) -> list[PricePublic]:
    return [price for price in book if price.id in ids]


@router.post("/", response_model=list[PricePublic], status_code=201)
def create_prices(
    session: SessionDep, current_user: CurrentUser, prices_in: PricesCreate
) -> Any:
    """
    Save confirmed drafts as your prices, all or none. A confirmed fuzzy match (remember_text)
    is saved as your own name for the item.
    """
    household = get_or_create_household(session, current_user.id)
    try:
        saved = save_prices(session, household, prices_in)
    except PriceError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.commit()
    return _by_ids(price_book(session, household, limit=1000), {p.id for p in saved})


@router.get("/", response_model=list[PricePublic])
def read_prices(session: SessionDep, current_user: CurrentUser) -> Any:
    """Your prices, newest first, each with its cost per cup."""
    return price_book(session, get_or_create_household(session, current_user.id))


@router.put("/{id}", response_model=PricePublic)
def update_one_price(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID, price_in: PriceUpdate
) -> Any:
    """Edit one of your prices."""
    household = get_or_create_household(session, current_user.id)
    try:
        update_price(session, household, id, price_in)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Price not found") from exc
    except PriceError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.commit()
    return _by_ids(price_book(session, household, limit=1000), {id})[0]


@router.delete("/{id}", response_model=Message)
def delete_price(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Any:
    """Delete one of your prices."""
    household = get_or_create_household(session, current_user.id)
    try:
        observation = own_price(session, household, id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Price not found") from exc
    session.delete(observation)
    session.commit()
    return Message(message="Price deleted")
