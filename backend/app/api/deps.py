from collections.abc import Generator
from typing import Annotated

import jwt
import redis
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from pydantic import ValidationError
from pydantic_ai import models
from sqlmodel import Session

from app.core import security
from app.core.config import settings
from app.core.db import engine
from app.core.redis import get_redis_client
from app.models import TokenPayload, User

reusable_oauth2 = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/login/access-token"
)


def get_db() -> Generator[Session]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_db)]
TokenDep = Annotated[str, Depends(reusable_oauth2)]


def get_current_user(session: SessionDep, token: TokenDep) -> User:
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[security.ALGORITHM]
        )
        token_data = TokenPayload(**payload)
    except InvalidTokenError, ValidationError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate credentials",
        )
    user = session.get(User, token_data.sub)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_current_active_superuser(current_user: CurrentUser) -> User:
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=403, detail="The user doesn't have enough privileges"
        )
    return current_user


def get_redis() -> redis.Redis:
    return get_redis_client()


RedisDep = Annotated[redis.Redis, Depends(get_redis)]


def get_run_model() -> models.Model | str | None:
    """Model for grocery analysis runs. None means the model built from settings.SIM_MODEL.

    Tests replace this dependency (app.dependency_overrides) to inject Pydantic AI's FunctionModel
    or TestModel, so API tests never depend on a live model or on context variables.
    """
    return None


RunModelDep = Annotated[models.Model | str | None, Depends(get_run_model)]
