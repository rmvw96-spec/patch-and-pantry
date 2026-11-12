import warnings
from typing import Literal, Self

from pydantic import (
    EmailStr,
    HttpUrl,
    PostgresDsn,
    SecretStr,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Top-level files, one level above ./backend/. .env is committed (shared, non-secret
        # defaults); .env.local is gitignored and overrides it, for secrets such as FOUNDRY_API_KEY.
        env_file=("../.env", "../.env.local"),
        env_ignore_empty=True,
        extra="ignore",
    )
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str
    # 60 minutes * 24 hours * 8 days = 8 days
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 8
    FRONTEND_HOST: str = "http://localhost:5173"
    FASTAPI_ENV: Literal["development"] | None = None

    PROJECT_NAME: str
    SENTRY_DSN: HttpUrl | None = None
    DATABASE_URL: PostgresDsn

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _use_psycopg_driver(cls, value: str | PostgresDsn) -> str:
        database_url = str(value)
        for scheme in ("postgres://", "postgresql://"):
            if database_url.startswith(scheme):
                return database_url.replace(scheme, "postgresql+psycopg://", 1)
        return database_url

    # Redis holds short-lived job coordination state (idempotency keys for study submissions).
    REDIS_URL: str = "redis://localhost:6379/0"

    # Model for the agents (see app/agents/models.py): "demo" (scripted, no key), "test", or
    # "foundry:deepseek-v3.2[@<deployment>]". Live models run only when set explicitly.
    SIM_MODEL: str = "demo"
    # Microsoft Foundry deployment for "foundry:" models: the v1 endpoint from the deployment's page
    # (ending in /openai/v1/) and its key. Read only when a run uses a Foundry model.
    FOUNDRY_ENDPOINT: str | None = None
    FOUNDRY_API_KEY: SecretStr | None = None

    # OpenTelemetry tracing for the agents (see app/core/tracing.py). Unset means tracing is
    # off. For a local Phoenix from compose.override.yml: http://localhost:6006/v1/traces
    PHOENIX_COLLECTOR_ENDPOINT: str | None = None
    # Record prompts, model responses, and tool arguments in spans. Turn off where those may hold
    # data that should not leave the process.
    TRACE_INCLUDE_CONTENT: bool = True

    SMTP_TLS: bool = True
    SMTP_SSL: bool = False
    SMTP_PORT: int = 587
    SMTP_HOST: str | None = None
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    EMAILS_FROM_EMAIL: EmailStr | None = None
    EMAILS_FROM_NAME: str | None = None

    @model_validator(mode="after")
    def _set_default_emails_from(self) -> Self:
        if not self.EMAILS_FROM_NAME:
            self.EMAILS_FROM_NAME = self.PROJECT_NAME
        return self

    EMAIL_RESET_TOKEN_EXPIRE_HOURS: int = 48

    @computed_field  # type: ignore[prop-decorator]
    @property
    def emails_enabled(self) -> bool:
        return bool(self.SMTP_HOST and self.EMAILS_FROM_EMAIL)

    EMAIL_TEST_USER: EmailStr = "test@example.com"
    FIRST_SUPERUSER: EmailStr
    FIRST_SUPERUSER_PASSWORD: str

    def _check_default_secret(self, var_name: str, value: str | None) -> None:
        if value == "changethis":
            message = (
                f'The value of {var_name} is "changethis", '
                "for security, please change it, at least for deployments."
            )
            if self.FASTAPI_ENV == "development":
                warnings.warn(message, stacklevel=1)
            else:
                raise ValueError(message)

    @model_validator(mode="after")
    def _enforce_non_default_secrets(self) -> Self:
        self._check_default_secret("SECRET_KEY", self.SECRET_KEY)
        for host in self.DATABASE_URL.hosts():
            self._check_default_secret("DATABASE_URL password", host["password"])
        self._check_default_secret(
            "FIRST_SUPERUSER_PASSWORD", self.FIRST_SUPERUSER_PASSWORD
        )

        return self


settings = Settings()  # type: ignore # ty: ignore[unused-ignore-comment]
