"""Grocery analysis runs (P1-1): submit agent work, then poll for the result."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Response
from sqlmodel import Session, col, or_, select

from app.api.deps import CurrentUser, RedisDep, RunModelDep, SessionDep
from app.grocery_analysis_jobs import (
    BestBuyRequest,
    GroceryAnalysisRunCreate,
    GroceryAnalysisRunPublic,
    PriceCaptureRequest,
    execute_run,
    to_public,
)
from app.households import get_or_create_household
from app.models import CatalogItem, GroceryAnalysisRun, Household, Vendor

router = APIRouter(prefix="/grocery-analysis-runs", tags=["grocery-analysis-runs"])

# How long an Idempotency-Key stays reserved. Retries after this window start a new run.
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60


def _idempotency_redis_key(user_id: uuid.UUID, key: str) -> str:
    # Scoped per user, so two users can never collide on the same key.
    return f"idempotency:grocery_analysis_runs:{user_id}:{key}"


def _check_request(
    session: Session,
    household: Household,
    request: PriceCaptureRequest | BestBuyRequest,
) -> None:
    """Reject a request that names someone else's vendor or an unknown item, before it queues."""
    if isinstance(request, PriceCaptureRequest):
        vendor = session.get(Vendor, request.vendor_id)
        if vendor is None or vendor.household_id != household.id:
            raise HTTPException(
                status_code=422, detail="Choose one of your own stores."
            )
        return
    ids = [need.catalog_item_id for need in request.needs]
    # Items the household can see: national, its region's, and its own (never another's).
    visible = or_(
        col(CatalogItem.scope) == "national",
        col(CatalogItem.region_id) == household.region_id,
        col(CatalogItem.household_id) == household.id,
    )
    found = set(
        session.exec(
            select(CatalogItem.id).where(
                col(CatalogItem.id).in_(ids), CatalogItem.kind == "produce", visible
            )
        )
    )
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=422, detail="List each item once.")
    if set(ids) - found:
        raise HTTPException(status_code=422, detail="Unknown produce item.")


@router.post("/", response_model=GroceryAnalysisRunPublic, status_code=202)
def create_grocery_analysis_run(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    run_in: GroceryAnalysisRunCreate,
    background_tasks: BackgroundTasks,
    response: Response,
    redis_client: RedisDep,
    model: RunModelDep,
    idempotency_key: Annotated[str | None, Header(max_length=255)] = None,
) -> Any:
    """
    Start a run. Returns at once with status "queued"; poll GET /grocery-analysis-runs/{id}.

    Send an Idempotency-Key header to make retries safe: repeating a request with the same key
    returns the original run instead of starting a duplicate job.
    """
    household = get_or_create_household(session, current_user.id)
    _check_request(session, household, run_in)
    run_id = uuid.uuid4()
    redis_key = None
    if idempotency_key is not None:
        redis_key = _idempotency_redis_key(current_user.id, idempotency_key)
        # Atomically reserve the key for this new run. Only one request can win the reservation.
        reserved = redis_client.set(
            redis_key, str(run_id), nx=True, ex=IDEMPOTENCY_TTL_SECONDS
        )
        if not reserved:
            stored = redis_client.get(redis_key)
            # Redis's type hints allow bytes or str; the client is configured to decode to str.
            existing_id = stored.decode() if isinstance(stored, bytes) else stored
            existing = (
                session.get(GroceryAnalysisRun, uuid.UUID(existing_id))
                if existing_id
                else None
            )
            if existing is None:
                # Another request holding this key has not finished creating its run yet.
                raise HTTPException(
                    status_code=409,
                    detail="A run with this Idempotency-Key is still being created. Retry shortly.",
                )
            response.headers["Idempotent-Replayed"] = "true"
            return to_public(existing)

    run = GroceryAnalysisRun(
        id=run_id,
        owner_id=current_user.id,
        kind=run_in.kind,
        input=run_in.model_dump(mode="json"),
    )
    try:
        session.add(run)
        session.commit()
        session.refresh(run)
    except Exception:
        # Release the reservation, or retries with this key would get 409 until it expires.
        if redis_key is not None:
            redis_client.delete(redis_key)
        raise

    background_tasks.add_task(execute_run, run.id, model)
    return to_public(run)


@router.get("/{id}", response_model=GroceryAnalysisRunPublic)
def read_grocery_analysis_run(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """
    Get a run's status, and its result or error once it has finished.
    """
    run = session.get(GroceryAnalysisRun, id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if not current_user.is_superuser and (run.owner_id != current_user.id):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return to_public(run)
