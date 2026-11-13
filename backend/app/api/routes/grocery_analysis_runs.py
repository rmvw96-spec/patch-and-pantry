"""Grocery analysis runs (P1-1): submit agent work, then poll for the result."""

import uuid
from typing import Annotated, Any, Self

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlmodel import Session, col, or_, select

from app.agents.capture_agent import CaptureFailed, LineAnswer, answer_line
from app.api.deps import CurrentUser, RedisDep, RunModelDep, SessionDep
from app.grocery_analysis_jobs import (
    BestBuyRequest,
    GroceryAnalysisRunCreate,
    GroceryAnalysisRunPublic,
    PriceCaptureRequest,
    PriceCaptureResult,
    execute_run,
    to_public,
)
from app.households import get_or_create_household
from app.models import (
    CatalogItem,
    GroceryAnalysisRun,
    Household,
    PortionSize,
    Vendor,
)
from app.prices import PieceWeight
from app.tools.catalog import remember_alias

router = APIRouter(prefix="/grocery-analysis-runs", tags=["grocery_analysis_runs"])

# How long an Idempotency-Key stays reserved. Retries after this window start a new run.
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60


def _idempotency_redis_key(user_id: uuid.UUID, key: str) -> str:
    # Scoped per user, so two users can never collide on the same key.
    return f"idempotency:grocery_analysis_runs:{user_id}:{key}"


class QuestionAnswer(BaseModel):
    """The answer to one question on a price list: an item, a unit, a size, or a weight."""

    model_config = ConfigDict(extra="forbid")

    raw_text: str = Field(min_length=1, max_length=500)
    catalog_item_id: uuid.UUID | None = None
    unit: str | None = Field(default=None, max_length=20)
    size: PortionSize | None = None
    piece_weight: PieceWeight | None = None

    @model_validator(mode="after")
    def one_answer(self) -> Self:
        given = [self.catalog_item_id, self.unit, self.size, self.piece_weight]
        if sum(value is not None for value in given) != 1:
            raise ValueError("give one answer: an item, a unit, a size, or a weight")
        return self


def _visible(household: Household) -> Any:
    """Items the household can see: national, its region's, and its own (never another's)."""
    return or_(
        col(CatalogItem.scope) == "national",
        col(CatalogItem.region_id) == household.region_id,
        col(CatalogItem.household_id) == household.id,
    )


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
    visible = _visible(household)
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


@router.post("/{id}/answers", response_model=GroceryAnalysisRunPublic)
def answer_question(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID, body: QuestionAnswer
) -> Any:
    """
    Answer one question on a read price list. The line is completed in code (no model call)
    and the question becomes a draft, or the next question. Choosing an item also remembers
    the typed words as the household's name for it.
    """
    run = session.get(GroceryAnalysisRun, id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    if run.kind != "price_capture" or run.status != "succeeded" or not run.result:
        raise HTTPException(status_code=409, detail="This run has no questions.")
    request = PriceCaptureRequest.model_validate(run.input)
    result = PriceCaptureResult.model_validate(run.result)
    index = next(
        (i for i, q in enumerate(result.questions) if q.raw_text == body.raw_text),
        None,
    )
    if index is None:
        raise HTTPException(status_code=404, detail="No question for that line.")
    question = result.questions[index]
    household = get_or_create_household(session, run.owner_id)
    if body.catalog_item_id is not None:
        item = session.exec(
            select(CatalogItem).where(
                CatalogItem.id == body.catalog_item_id,
                CatalogItem.kind == "produce",
                _visible(household),
            )
        ).first()
        if item is None:
            raise HTTPException(status_code=422, detail="Unknown produce item.")
        reading = question.reading
        words = question.item_text or (reading.item_text if reading else None)
        if words and words.strip():
            remember_alias(session, household, words, item)
    answer = LineAnswer(
        catalog_item_id=body.catalog_item_id,
        unit=body.unit,
        size=body.size,
        piece_weight=body.piece_weight,
    )
    try:
        completed = answer_line(
            session, household, question, answer, request.vendor_id, request.observed_on
        )
    except CaptureFailed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    result.questions[index : index + 1] = completed.questions
    result.drafts.extend(completed.drafts)
    run.result = result.model_dump(mode="json")
    run.data_snapshot_ids = sorted(
        {*run.data_snapshot_ids, *(str(i) for i in completed.data_snapshot_ids)}
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return to_public(run)
