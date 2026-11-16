"""Runtime configuration for the audience estimation service."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    database_url: str
    admin_api_key: str
    analyst_api_key: str
    default_schema: str = "audience"
    service_timezone: str = "Asia/Shanghai"

    @property
    def schema(self) -> str:
        # Schema identifiers are quoted and validated to keep DDL safe.
        name = self.default_schema
        if not name.replace("_", "").isalnum():
            raise ValueError("invalid schema name")
        return name


def get_settings() -> Settings:
    return Settings(
        database_url=_env(
            "AUDIENCE_DATABASE_URL",
            "postgresql://ccdev:ccdev@127.0.0.1:5432/ccdev",
        ),
        admin_api_key=_env("AUDIENCE_ADMIN_API_KEY", "dev-admin-key"),
        analyst_api_key=_env("AUDIENCE_ANALYST_API_KEY", "dev-analyst-key"),
        default_schema=_env("AUDIENCE_DB_SCHEMA", "audience"),
        service_timezone=_env("AUDIENCE_SERVICE_TZ", "Asia/Shanghai"),
    )
