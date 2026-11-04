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

from pydantic_ai import models
from sqlmodel import Session

from app.agents.simulator import PipeSimClient
from app.agents.study_agent import StudyFailed, run_study
from app.core.config import settings
from app.core.db import engine
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
    """Run one study to completion and persist the outcome."""
    prompt = _update_study(study_id, status="running", started_at=get_datetime_utc())
    if prompt is None:
        logger.warning("Study %s was deleted before it could run", study_id)
        return

    sim = PipeSimClient(max_inlet_bar=settings.SIM_MAX_INLET_BAR)
    try:
        result = await run_study(prompt, sim, model=model)
    except StudyFailed as exc:
        _update_study(
            study_id,
            status="failed",
            error_reason=exc.reason,
            error_message=str(exc),
            finished_at=get_datetime_utc(),
        )
    except Exception:
        logger.exception("Study %s failed unexpectedly", study_id)
        _update_study(
            study_id,
            status="failed",
            error_reason="internal_error",
            error_message="The study failed unexpectedly. Please try again.",
            finished_at=get_datetime_utc(),
        )
    else:
        _update_study(
            study_id,
            status="succeeded",
            result=result.model_dump(),
            finished_at=get_datetime_utc(),
        )
