import uuid
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Response

from app.api.deps import CurrentUser, RedisDep, SessionDep, StudyModelDep
from app.models import Study, StudyCreate, StudyPublic
from app.study_jobs import execute_study

router = APIRouter(prefix="/studies", tags=["studies"])

# How long an Idempotency-Key stays reserved. Retries after this window start a new study.
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60


def _idempotency_redis_key(user_id: uuid.UUID, key: str) -> str:
    # Scoped per user, so two users can never collide on the same key.
    return f"idempotency:studies:{user_id}:{key}"


@router.post("/", response_model=StudyPublic, status_code=202)
def create_study(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    study_in: StudyCreate,
    background_tasks: BackgroundTasks,
    response: Response,
    redis_client: RedisDep,
    model: StudyModelDep,
    idempotency_key: Annotated[str | None, Header(max_length=255)] = None,
) -> Any:
    """
    Submit a study. Returns immediately with status "queued"; poll GET /studies/{id} for the outcome.

    Send an Idempotency-Key header to make retries safe: repeating a request with the same key
    returns the original study instead of starting a duplicate job.
    """
    study_id = uuid.uuid4()
    redis_key = None
    if idempotency_key is not None:
        redis_key = _idempotency_redis_key(current_user.id, idempotency_key)
        # Atomically reserve the key for this new study. Only one request can win the reservation.
        reserved = redis_client.set(
            redis_key, str(study_id), nx=True, ex=IDEMPOTENCY_TTL_SECONDS
        )
        if not reserved:
            stored = redis_client.get(redis_key)
            # Redis's type hints allow bytes or str; the client is configured to decode to str.
            existing_id = stored.decode() if isinstance(stored, bytes) else stored
            existing = (
                session.get(Study, uuid.UUID(existing_id)) if existing_id else None
            )
            if existing is None:
                # Another request holding this key has not finished creating its study yet.
                raise HTTPException(
                    status_code=409,
                    detail="A study with this Idempotency-Key is still being created. Retry shortly.",
                )
            response.headers["Idempotent-Replayed"] = "true"
            return existing

    study = Study(id=study_id, owner_id=current_user.id, prompt=study_in.prompt)
    try:
        session.add(study)
        session.commit()
        session.refresh(study)
    except Exception:
        # Release the reservation, or retries with this key would get 409 until it expires.
        if redis_key is not None:
            redis_client.delete(redis_key)
        raise

    background_tasks.add_task(execute_study, study.id, model)
    return study


@router.get("/{id}", response_model=StudyPublic)
def read_study(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Any:
    """
    Get a study's status, and its result or error once it has finished.
    """
    study = session.get(Study, id)
    if not study:
        raise HTTPException(status_code=404, detail="Study not found")
    if not current_user.is_superuser and (study.owner_id != current_user.id):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return study
