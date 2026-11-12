"""Grocery analysis runs (P1-1): the request and result schemas, and the background job.

A run is one piece of agent work for the user's household: reading a typed list into draft
prices (``price_capture``, P1-5) or comparing best buys (``best_buy``, P1-6). The API stores the
request and returns at once; the job runs it and records the result, the steps that ran, the
reference-data snapshots read, the model, and its token usage.

Postgres is the source of truth for job state. Every exit path below leaves the run in a terminal
state (succeeded or failed) and sets ``grocery_analysis_run.status`` on the run's span, so a
client polling GET /grocery-analysis-runs/{id} never waits forever on a job that has ended.

Database calls here are synchronous inside an async function, which briefly blocks the event loop.
That is fine at this scale; a production version would use an async session or a worker process.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Annotated, Any, Literal

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_ai import models
from sqlmodel import Session

from app.agents.best_buy_agent import ExplanationFailed, explain_best_buy
from app.agents.capture_agent import (
    CaptureFailed,
    CaptureQuestion,
    DraftPrice,
    capture_typed_list,
)
from app.agents.usage import ModelUsage
from app.core.config import settings
from app.core.db import engine
from app.core.tracing import get_tracer
from app.households import get_or_create_household
from app.models import GroceryAnalysisRun, RunKind, RunStatus, get_datetime_utc
from app.tools.best_buy import (
    BestBuyRow,
    ItemNote,
    NationalAverage,
    Need,
    ShoppingGroup,
    compare_best_buy,
    shopping_list,
)

logger = logging.getLogger(__name__)

MAX_LIST_CHARACTERS = 20_000
MAX_NEEDS = 50


# ---------- requests ----------


class PriceCaptureRequest(BaseModel):
    """Read a typed list into draft prices for one store and date. Nothing is saved yet."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["price_capture"]
    text: str = Field(min_length=1, max_length=MAX_LIST_CHARACTERS)
    vendor_id: uuid.UUID
    observed_on: date


class NeedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_item_id: uuid.UUID
    cups_per_week: float = Field(gt=0, le=1000)


class BestBuyRequest(BaseModel):
    """Compare this week's best buys for the items needed."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["best_buy"]
    needs: list[NeedRequest] = Field(min_length=1, max_length=MAX_NEEDS)
    on: date | None = None  # defaults to the day the run starts


GroceryAnalysisRunCreate = Annotated[
    PriceCaptureRequest | BestBuyRequest, Field(discriminator="kind")
]
_request = TypeAdapter[PriceCaptureRequest | BestBuyRequest](GroceryAnalysisRunCreate)


# ---------- results ----------


class PriceCaptureResult(BaseModel):
    kind: Literal["price_capture"] = "price_capture"
    drafts: list[DraftPrice]
    questions: list[CaptureQuestion]
    ignored: list[str]


class BestBuyResult(BaseModel):
    kind: Literal["best_buy"] = "best_buy"
    on: date
    rows: list[BestBuyRow]
    # Notes said once per item (storage, stand-ins) and the national averages' source.
    items: list[ItemNote] = Field(default_factory=list)
    national_average: NationalAverage | None = None
    shopping_list: list[ShoppingGroup] = Field(default_factory=list)
    # The table stands without an explanation if the model could not write one.
    explanation: str | None = None
    explanation_error: str | None = None


RunResult = Annotated[PriceCaptureResult | BestBuyResult, Field(discriminator="kind")]


class GroceryAnalysisRunPublic(BaseModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    kind: RunKind
    input: PriceCaptureRequest | BestBuyRequest = Field(discriminator="kind")
    status: RunStatus
    result: RunResult | None = None
    tools_used: list[str]
    data_snapshot_ids: list[uuid.UUID]
    model: str | None = None
    input_tokens: int
    output_tokens: int
    requests: int
    error_reason: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


def to_public(run: GroceryAnalysisRun) -> GroceryAnalysisRunPublic:
    return GroceryAnalysisRunPublic.model_validate(run, from_attributes=True)


# ---------- the job ----------


class RunFailed(Exception):
    """User-facing failure with a stable reason code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class Outcome:
    result: PriceCaptureResult | BestBuyResult
    tools_used: list[str]
    data_snapshot_ids: set[uuid.UUID] = field(default_factory=set)
    usage: ModelUsage = field(default_factory=ModelUsage)


def model_name(model: models.Model | str | None) -> str:
    """The model a run used: the SIM_MODEL spec, or the injected model's name."""
    if model is None:
        return settings.SIM_MODEL
    if isinstance(model, str):
        return model
    name = model.model_name
    # Some names already carry the system ("function:..."); others do not ("DeepSeek-V3.2").
    return name if name.startswith(f"{model.system}:") else f"{model.system}:{name}"


async def _price_capture(
    session: Session,
    owner_id: uuid.UUID,
    request: PriceCaptureRequest,
    model: models.Model | str | None,
) -> Outcome:
    household = get_or_create_household(session, owner_id)
    try:
        captured = await capture_typed_list(
            session,
            household,
            request.text,
            request.vendor_id,
            request.observed_on,
            model,
        )
    except CaptureFailed as exc:
        raise RunFailed(exc.reason, str(exc)) from exc
    return Outcome(
        result=PriceCaptureResult(
            drafts=captured.drafts,
            questions=captured.questions,
            ignored=captured.ignored,
        ),
        tools_used=[
            "capture_agent",
            "find_catalog_item",
            "portion_grams",
            "normalize_price",
        ],
        data_snapshot_ids=captured.data_snapshot_ids,
        usage=captured.usage,
    )


async def _best_buy(
    session: Session,
    owner_id: uuid.UUID,
    request: BestBuyRequest,
    model: models.Model | str | None,
) -> Outcome:
    household = get_or_create_household(session, owner_id)
    on = request.on or get_datetime_utc().date()
    needs = [Need(n.catalog_item_id, n.cups_per_week) for n in request.needs]
    try:
        table = compare_best_buy(session, household, needs, on)
    except ValueError as exc:
        raise RunFailed("invalid_input", str(exc)) from exc
    result = BestBuyResult(
        on=table.on,
        rows=table.rows,
        items=table.items,
        national_average=table.national_average,
        shopping_list=shopping_list(table),
    )
    usage = ModelUsage()
    try:
        explanation = await explain_best_buy(table, model)
    except ExplanationFailed as exc:
        result.explanation_error = str(exc)
    else:
        result.explanation, usage = explanation.text, explanation.usage
    return Outcome(
        result=result,
        tools_used=["compare_best_buy", "best_buy_agent"],
        data_snapshot_ids=table.data_snapshot_ids,
        usage=usage,
    )


def _update_run(run_id: uuid.UUID, **fields: Any) -> GroceryAnalysisRun | None:
    """Apply field updates to a run and return it, or None if the run is gone."""
    with Session(engine) as session:
        run = session.get(GroceryAnalysisRun, run_id)
        if run is None:
            return None
        for name, value in fields.items():
            setattr(run, name, value)
        session.add(run)
        session.commit()
        session.refresh(run)
        session.expunge(run)
        return run


async def execute_run(
    run_id: uuid.UUID, model: models.Model | str | None = None
) -> None:
    """Run one grocery analysis to completion and persist the outcome.

    The whole job is one "grocery_analysis_run" span, so in Phoenix each run is a single trace
    with the agent runs and model calls nested under it.
    """
    with get_tracer().start_as_current_span(
        "grocery_analysis_run",
        attributes={
            # Phoenix labels spans by this OpenInference kind; CHAIN is a step that wraps others.
            "openinference.span.kind": "CHAIN",
            "grocery_analysis_run.id": str(run_id),
        },
    ) as span:
        await _execute_run(run_id, model, span)


async def _execute_run(
    run_id: uuid.UUID, model: models.Model | str | None, span: trace.Span
) -> None:
    run = _update_run(run_id, status="running", started_at=get_datetime_utc())
    if run is None:
        logger.warning(
            "Grocery analysis run %s was deleted before it could run", run_id
        )
        span.set_attribute("grocery_analysis_run.status", "deleted")
        return
    span.set_attribute("grocery_analysis_run.kind", run.kind)

    try:
        request = _request.validate_python(run.input)
        with Session(engine) as session:
            if isinstance(request, PriceCaptureRequest):
                outcome = await _price_capture(session, run.owner_id, request, model)
            else:
                outcome = await _best_buy(session, run.owner_id, request, model)
    except RunFailed as exc:
        _record_failure(span, exc.reason, exc)
        _update_run(
            run_id,
            status="failed",
            error_reason=exc.reason,
            error_message=str(exc)[:500],
            model=model_name(model),
            finished_at=get_datetime_utc(),
        )
    except Exception as exc:
        logger.exception("Grocery analysis run %s failed unexpectedly", run_id)
        _record_failure(span, "internal_error", exc)
        _update_run(
            run_id,
            status="failed",
            error_reason="internal_error",
            error_message="The run failed unexpectedly. Please try again.",
            model=model_name(model),
            finished_at=get_datetime_utc(),
        )
    else:
        span.set_attribute("grocery_analysis_run.status", "succeeded")
        _update_run(
            run_id,
            status="succeeded",
            result=outcome.result.model_dump(mode="json"),
            tools_used=outcome.tools_used,
            data_snapshot_ids=sorted(str(i) for i in outcome.data_snapshot_ids),
            model=model_name(model),
            input_tokens=outcome.usage.input_tokens,
            output_tokens=outcome.usage.output_tokens,
            requests=outcome.usage.requests,
            finished_at=get_datetime_utc(),
        )


def _record_failure(span: trace.Span, reason: str, exc: Exception) -> None:
    span.set_attribute("grocery_analysis_run.status", "failed")
    span.set_attribute("grocery_analysis_run.error_reason", reason)
    span.record_exception(exc)
    span.set_status(trace.StatusCode.ERROR, reason)
