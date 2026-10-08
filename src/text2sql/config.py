"""Runtime configuration contract (text2sql_V1.md section 7).

All runtime settings come from environment variables (optionally seeded from a
``.env`` file).  Nothing is hard coded into the nodes, and the database password
is never written to logs, state, prompts or error messages:

>>> settings = Settings.load()
>>> settings.redact("connect failed for " + settings.db_password)
'connect failed for ***'
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote_plus

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
CATALOG_PATH = REPO_ROOT / "catalog" / "catalog_v1.yaml"

MASK = "***"


class ConfigurationError(RuntimeError):
    """Raised when runtime configuration is missing or inconsistent."""


def _as_int(raw: str | None, default: int, name: str) -> int:
    if raw is None or raw == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc


def _as_float(raw: str | None, default: float, name: str) -> float:
    if raw is None or raw == "":
        return default
    try:
        return float(str(raw).strip())
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Immutable runtime configuration."""

    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_name: str = "ecommerce_text2sql"
    db_user: str = "text2sql_readonly"
    db_password: str = ""
    db_charset: str = "utf8mb4"

    sql_dialect: str = "mysql"
    sql_max_rows: int = 100
    sql_timeout_seconds: int = 30
    sql_max_retries: int = 2
    sql_limit_policy: str = "normalize"
    catalog_version: str = "v1"
    catalog_source: str = "yaml"
    catalog_dsn: str | None = None
    catalog_path: Path = field(default=CATALOG_PATH)

    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-chat"
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_temperature: float = 0.0
    llm_timeout_seconds: int = 60
    llm_max_attempts: int = 3

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(
        cls,
        env_file: str | Path | None = DEFAULT_ENV_FILE,
        overrides: Mapping[str, Any] | None = None,
    ) -> "Settings":
        """Build settings from ``env_file`` + process environment + overrides.

        Precedence: explicit ``overrides`` > process environment > ``.env`` file.
        """
        values: dict[str, str] = {}
        if env_file is not None:
            path = Path(env_file)
            if path.is_file():
                values.update(
                    {k: v for k, v in dotenv_values(path).items() if v is not None}
                )
        values.update({k: v for k, v in os.environ.items() if k in _ENV_KEYS})
        if overrides:
            values.update({k: v for k, v in overrides.items() if v is not None})

        settings = cls(
            db_host=values.get("DB_HOST", cls.db_host),
            db_port=_as_int(values.get("DB_PORT"), cls.db_port, "DB_PORT"),
            db_name=values.get("DB_NAME", cls.db_name),
            db_user=values.get("DB_USER", cls.db_user),
            db_password=values.get("DB_PASSWORD", cls.db_password),
            db_charset=values.get("DB_CHARSET", cls.db_charset),
            sql_dialect=values.get("SQL_DIALECT", cls.sql_dialect),
            sql_max_rows=_as_int(values.get("SQL_MAX_ROWS"), cls.sql_max_rows, "SQL_MAX_ROWS"),
            sql_timeout_seconds=_as_int(
                values.get("SQL_TIMEOUT_SECONDS"),
                cls.sql_timeout_seconds,
                "SQL_TIMEOUT_SECONDS",
            ),
            sql_max_retries=_as_int(
                values.get("SQL_MAX_RETRIES"), cls.sql_max_retries, "SQL_MAX_RETRIES"
            ),
            sql_limit_policy=values.get("SQL_LIMIT_POLICY", cls.sql_limit_policy),
            catalog_version=values.get("CATALOG_VERSION", cls.catalog_version),
            catalog_source=values.get("CATALOG_SOURCE", cls.catalog_source),
            catalog_dsn=values.get("CATALOG_DSN") or None,
            catalog_path=Path(values.get("CATALOG_PATH", CATALOG_PATH)),
            llm_provider=values.get("LLM_PROVIDER", cls.llm_provider),
            llm_model=values.get("LLM_MODEL", cls.llm_model),
            llm_base_url=values.get("LLM_BASE_URL", cls.llm_base_url),
            llm_api_key=values.get("LLM_API_KEY", cls.llm_api_key),
            llm_temperature=_as_float(
                values.get("LLM_TEMPERATURE"), cls.llm_temperature, "LLM_TEMPERATURE"
            ),
            llm_timeout_seconds=_as_int(
                values.get("LLM_TIMEOUT_SECONDS"),
                cls.llm_timeout_seconds,
                "LLM_TIMEOUT_SECONDS",
            ),
            llm_max_attempts=_as_int(
                values.get("LLM_MAX_ATTEMPTS"), cls.llm_max_attempts, "LLM_MAX_ATTEMPTS"
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.sql_dialect != "mysql":
            raise ConfigurationError(
                f"V1 only supports SQL_DIALECT=mysql, got {self.sql_dialect!r}"
            )
        if self.sql_max_rows <= 0:
            raise ConfigurationError("SQL_MAX_ROWS must be positive")
        if self.sql_timeout_seconds <= 0:
            raise ConfigurationError("SQL_TIMEOUT_SECONDS must be positive")
        if self.sql_max_retries < 0:
            raise ConfigurationError("SQL_MAX_RETRIES must be >= 0")
        if self.sql_limit_policy not in {"normalize", "reject"}:
            raise ConfigurationError("SQL_LIMIT_POLICY must be 'normalize' or 'reject'")
        if self.catalog_source not in {"yaml", "mysql"}:
            raise ConfigurationError("CATALOG_SOURCE must be 'yaml' or 'mysql'")
        if self.db_charset != "utf8mb4":
            raise ConfigurationError("DB_CHARSET must be utf8mb4 for V1")

    # ------------------------------------------------------------------ urls
    def sqlalchemy_url(self, *, mask_password: bool = False) -> str:
        """``mysql+pymysql://user:password@host:port/db?charset=utf8mb4``."""
        if mask_password:
            credentials = f"{quote_plus(self.db_user)}:{MASK}"
        else:
            credentials = f"{quote_plus(self.db_user)}:{quote_plus(self.db_password)}"
        return (
            f"mysql+pymysql://{credentials}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset={self.db_charset}"
        )

    # ------------------------------------------------------------- redaction
    def secret_values(self) -> tuple[str, ...]:
        secrets = [value for value in (self.db_password, self.llm_api_key) if value]
        if self.catalog_dsn:
            secrets.append(self.catalog_dsn)
        return tuple(secrets)

    def redact(self, text: Any) -> str:
        """Replace every configured secret with ``***``."""
        out = str(text)
        for secret in self.secret_values():
            out = out.replace(secret, MASK)
            out = out.replace(quote_plus(secret), MASK)
        return out

    def describe(self) -> dict[str, Any]:
        """A log-safe summary (no secrets)."""
        return {
            "db": f"{self.db_user}@{self.db_host}:{self.db_port}/{self.db_name}",
            "sql_dialect": self.sql_dialect,
            "sql_max_rows": self.sql_max_rows,
            "sql_timeout_seconds": self.sql_timeout_seconds,
            "sql_max_retries": self.sql_max_retries,
            "catalog_version": self.catalog_version,
            "catalog_source": self.catalog_source,
            "llm": f"{self.llm_provider}:{self.llm_model}",
        }

    def with_overrides(self, **kwargs: Any) -> "Settings":
        return replace(self, **kwargs)


_ENV_KEYS = frozenset(
    {
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "DB_USER",
        "DB_PASSWORD",
        "DB_CHARSET",
        "SQL_DIALECT",
        "SQL_MAX_ROWS",
        "SQL_TIMEOUT_SECONDS",
        "SQL_MAX_RETRIES",
        "SQL_LIMIT_POLICY",
        "CATALOG_VERSION",
        "CATALOG_SOURCE",
        "CATALOG_DSN",
        "CATALOG_PATH",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_TEMPERATURE",
        "LLM_TIMEOUT_SECONDS",
        "LLM_MAX_ATTEMPTS",
    }
)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings."""
    return Settings.load()
