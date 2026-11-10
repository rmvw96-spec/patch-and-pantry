"""Background execution of study jobs: run the study agent and record the outcome in Postgres.

Postgres is the source of truth for job state. Every exit path below leaves the study in a
terminal state (succeeded or failed), so a client polling GET /studies/{id} never waits forever
on a job that has already ended.

Database calls here are synchronous inside an async function, which briefly blocks the event
loop. That is fine at this scale; a production version would use an async session or run jobs
in a separate worker process.
"""

import logging
import uuid
from typing import Any

from opentelemetry import trace
from pydantic_ai import models
from sqlmodel import Session

from app.agents.simulator import PipeSimClient
from app.agents.study_agent import StudyFailed, run_study
from app.core.config import settings
from app.core.db import engine
from app.core.tracing import get_tracer
from app.models import Study, get_datetime_utc

logger = logging.getLogger(__name__)


def _update_study(study_id: uuid.UUID, **fields: Any) -> str | None:
    """Apply field updates to a study and return its prompt, or None if the study is gone."""
    with Session(engine) as session:
        study = session.get(Study, study_id)
        if study is None:
            return None
        for name, value in fields.items():
            setattr(study, name, value)
        session.add(study)
        session.commit()
        return study.prompt


async def execute_study(
    study_id: uuid.UUID, model: models.Model | str | None = None
) -> None:
    """Run one study to completion and persist the outcome.

    The whole job is one "study" span, so in Phoenix each study is a single trace with the agent
    run, model calls, and tool calls nested under it. Tracing only adds attributes here; the
    terminal-state rule above does not depend on it.
    """
    with get_tracer().start_as_current_span(
        "study",
        attributes={
            # Phoenix labels spans by this OpenInference kind; CHAIN is a step that wraps others.
            "openinference.span.kind": "CHAIN",
            "study.id": str(study_id),
        },
    ) as span:
        await _execute_study(study_id, model, span)


async def _execute_study(
    study_id: uuid.UUID, model: models.Model | str | None, span: trace.Span
) -> None:
    prompt = _update_study(study_id, status="running", started_at=get_datetime_utc())
    if prompt is None:
        logger.warning("Study %s was deleted before it could run", study_id)
        span.set_attribute("study.status", "deleted")
        return

    sim = PipeSimClient(max_inlet_bar=settings.SIM_MAX_INLET_BAR)
    try:
        result = await run_study(prompt, sim, model=model)
    except StudyFailed as exc:
        _record_failure(span, exc.reason, exc)
        _update_study(
            study_id,
            status="failed",
            error_reason=exc.reason,
            error_message=str(exc),
            finished_at=get_datetime_utc(),
        )
    except Exception as exc:
        logger.exception("Study %s failed unexpectedly", study_id)
        _record_failure(span, "internal_error", exc)
        _update_study(
            study_id,
            status="failed",
            error_reason="internal_error",
            error_message="The study failed unexpectedly. Please try again.",
            finished_at=get_datetime_utc(),
        )
    else:
        span.set_attribute("study.status", "succeeded")
        _update_study(
            study_id,
            status="succeeded",
            result=result.model_dump(),
            finished_at=get_datetime_utc(),
        )


def _record_failure(span: trace.Span, reason: str, exc: Exception) -> None:
    span.set_attribute("study.status", "failed")
    span.set_attribute("study.error_reason", reason)
    span.record_exception(exc)
    span.set_status(trace.StatusCode.ERROR, reason)
