import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Principal(BaseModel):
    subject: str = Field(min_length=1, max_length=160)
    roles: set[Literal["admin", "reviewer", "operator", "display"]]
    expires_at: AwareDatetime
    station_ids: set[str] = Field(default_factory=set)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIGNORA_", env_file=".env", extra="ignore")

    database_url: SecretStr
    storage_root: Path
    # SHA-256(token) -> actor/roles. Raw credentials are never persisted by this app.
    principals: dict[str, Principal] = Field(default_factory=dict)
    import_roots: dict[str, Path] = Field(default_factory=dict)
    deletion_retention_days: int = Field(default=30, ge=1, le=36500)
    asr_model_path: Path | None = None
    asr_timeout_seconds: int = Field(default=90, ge=5, le=120)
    encoder_model_path: Path | None = None
    encoder_timeout_seconds: int = Field(default=60, ge=5, le=120)
    websocket_origins: set[str] = Field(default_factory=set)
    display_lease_seconds: int = Field(default=15, ge=5, le=60)
    display_poll_seconds: float = Field(default=1, ge=0.1, le=5)
    display_connection_limit: int = Field(default=64, ge=1, le=256)
    # Explicit local-development opt-in for partial preview and development live delivery.
    demo_mode_enabled: bool = False

    @field_validator("websocket_origins")
    @classmethod
    def explicit_origins(cls, origins):
        for origin in origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or "*" in origin
            ):
                raise ValueError(
                    "WebSocket origins must be explicit HTTP(S) origins without a path"
                )
        return origins

    @field_validator("import_roots")
    @classmethod
    def valid_source_aliases(cls, value):
        if any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", key) for key in value):
            raise ValueError("Import sources require safe aliases")
        return value

    @field_validator("principals")
    @classmethod
    def valid_digests(cls, value):
        if any(not re.fullmatch(r"[0-9a-f]{64}", key) for key in value):
            raise ValueError("Credential keys must be SHA-256 digests")
        return value

    @field_validator("database_url")
    @classmethod
    def require_postgresql(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("Use a PostgreSQL psycopg connection URL")
        return value
