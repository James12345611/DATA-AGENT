"""Normalized error contract (text2sql_V1.md sections 5.2 and 5.7).

Every failure in every node ends up as an :class:`text2sql.state.SQLError` with a
fixed ``error_type``, a human readable ``message`` and a ``recovery_strategy``.
Database exceptions are classified deterministically here (no LLM involved).
"""

from __future__ import annotations

from typing import Any

from .config import Settings
from .state import RecoveryStrategy, SQLError

# ------------------------------------------------------------------ error types

EXTRACTION_ERROR = "extraction_error"
SCHEMA_LINKING_ERROR = "schema_linking_error"
SQL_GENERATION_ERROR = "sql_generation_error"
UNSAFE_SQL = "unsafe_sql"
UNKNOWN_TABLE = "unknown_table"
UNKNOWN_COLUMN = "unknown_column"
SQL_POLICY_ERROR = "sql_policy_error"
SYNTAX_ERROR = "syntax_error"
TIMEOUT = "timeout"
PERMISSION_ERROR = "permission_error"
CONNECTION_ERROR = "connection_error"
RESOURCE_ERROR = "resource_error"
EXECUTION_ERROR = "execution_error"
RETRY_EXHAUSTED = "retry_exhausted"
LLM_ERROR = "llm_error"

RECOVERY_RETRY: RecoveryStrategy = "retry"
RECOVERY_RETRY_NEW_TABLE: RecoveryStrategy = "retry_with_new_table"
RECOVERY_SURFACE: RecoveryStrategy = "surface_to_user"
RECOVERY_ABORT: RecoveryStrategy = "abort"


def sql_error(
    error_type: str,
    message: str,
    recovery_strategy: RecoveryStrategy = RECOVERY_SURFACE,
    *,
    sql: str | None = None,
    attempt: int | None = None,
    settings: Settings | None = None,
) -> SQLError:
    """Build a normalized ``SQLError`` (optional keys are only present when set)."""
    text = str(message)
    if settings is not None:
        text = settings.redact(text)
    error = SQLError(
        error_type=error_type,
        message=text,
        recovery_strategy=recovery_strategy,
    )
    if sql is not None:
        error["sql"] = settings.redact(sql) if settings is not None else sql
    if attempt is not None:
        error["attempt"] = int(attempt)
    return error


# --------------------------------------------------------- MySQL errno mapping

# errno -> (error_type, recovery_strategy)
_MYSQL_ERRNO_MAP: dict[int, tuple[str, RecoveryStrategy]] = {
    1054: (UNKNOWN_COLUMN, RECOVERY_RETRY),          # ER_BAD_FIELD_ERROR
    1052: (UNKNOWN_COLUMN, RECOVERY_RETRY),          # ER_NON_UNIQ_ERROR (ambiguous)
    1064: (SYNTAX_ERROR, RECOVERY_RETRY),            # ER_PARSE_ERROR
    1146: (UNKNOWN_TABLE, RECOVERY_RETRY),           # ER_NO_SUCH_TABLE
    1051: (UNKNOWN_TABLE, RECOVERY_RETRY),           # ER_BAD_TABLE_ERROR
    1109: (UNKNOWN_TABLE, RECOVERY_RETRY),           # ER_UNKNOWN_TABLE
    1066: (SYNTAX_ERROR, RECOVERY_RETRY),            # ER_NONUNIQ_TABLE
    1317: (TIMEOUT, RECOVERY_RETRY),                 # ER_QUERY_INTERRUPTED
    3024: (TIMEOUT, RECOVERY_RETRY),                 # ER_QUERY_TIMEOUT (max_execution_time)
    1205: (TIMEOUT, RECOVERY_RETRY),                 # ER_LOCK_WAIT_TIMEOUT
    1045: (PERMISSION_ERROR, RECOVERY_SURFACE),      # ER_ACCESS_DENIED_ERROR
    1044: (PERMISSION_ERROR, RECOVERY_SURFACE),      # ER_DBACCESS_DENIED_ERROR
    1142: (PERMISSION_ERROR, RECOVERY_SURFACE),      # ER_TABLEACCESS_DENIED_ERROR
    1143: (PERMISSION_ERROR, RECOVERY_SURFACE),      # ER_COLUMNACCESS_DENIED_ERROR
    1227: (PERMISSION_ERROR, RECOVERY_SURFACE),      # ER_SPECIFIC_ACCESS_DENIED_ERROR
    1370: (PERMISSION_ERROR, RECOVERY_SURFACE),      # ER_PROCACCESS_DENIED_ERROR
    1114: (RESOURCE_ERROR, RECOVERY_ABORT),          # ER_RECORD_FILE_FULL
    1038: (RESOURCE_ERROR, RECOVERY_ABORT),          # ER_OUT_OF_SORTMEMORY
    1206: (RESOURCE_ERROR, RECOVERY_ABORT),          # ER_LOCK_TABLE_FULL
    1040: (RESOURCE_ERROR, RECOVERY_ABORT),          # ER_CON_COUNT_ERROR
    1041: (RESOURCE_ERROR, RECOVERY_ABORT),          # ER_OUT_OF_RESOURCES
    1049: (CONNECTION_ERROR, RECOVERY_SURFACE),      # ER_BAD_DB_ERROR
    2002: (CONNECTION_ERROR, RECOVERY_SURFACE),
    2003: (CONNECTION_ERROR, RECOVERY_SURFACE),
    2005: (CONNECTION_ERROR, RECOVERY_SURFACE),
    2006: (CONNECTION_ERROR, RECOVERY_SURFACE),
    2013: (CONNECTION_ERROR, RECOVERY_SURFACE),
    2055: (CONNECTION_ERROR, RECOVERY_SURFACE),
}

_CONNECTION_EXCEPTION_NAMES = {
    "OperationalError": "operational",
}


def mysql_errno(exc: BaseException) -> int | None:
    """Extract the MySQL errno from a DBAPI exception if present."""
    for arg in getattr(exc, "args", ()) or ():
        if isinstance(arg, int):
            return arg
    return None


def classify_db_error(exc: BaseException) -> tuple[str, RecoveryStrategy]:
    """Map a database exception onto ``(error_type, recovery_strategy)``."""
    errno = mysql_errno(exc)
    if errno is not None and errno in _MYSQL_ERRNO_MAP:
        return _MYSQL_ERRNO_MAP[errno]

    name = type(exc).__name__
    module = type(exc).__module__ or ""
    if "pymysql" in module or "sqlalchemy" in module:
        if name in {"InterfaceError", "OperationalError", "DatabaseError"} and errno is None:
            # Connection level problems usually surface without an errno.
            return CONNECTION_ERROR, RECOVERY_SURFACE
        if name == "ProgrammingError":
            return SYNTAX_ERROR, RECOVERY_RETRY
        if name == "InternalError":
            return RESOURCE_ERROR, RECOVERY_ABORT
    if isinstance(exc, (TimeoutError,)):
        return TIMEOUT, RECOVERY_RETRY
    if isinstance(exc, (ConnectionError, OSError)):
        return CONNECTION_ERROR, RECOVERY_SURFACE
    return EXECUTION_ERROR, RECOVERY_RETRY


def enforce_retry_policy(
    error: SQLError,
    *,
    retry_count: int,
    max_retries: int,
) -> SQLError:
    """Apply the V1 retry budget to a retryable execution error.

    Section 8.3 requires:

    * ``R03`` a timeout may be retried once, then surfaces to the user;
    * ``R05`` when the retry budget is exhausted the run fails with
      ``retry_exhausted`` instead of looping forever;
    * ``R04`` validation errors never reach this function (they abort directly).
    """
    if error.get("recovery_strategy") != RECOVERY_RETRY:
        return error

    error_type = error.get("error_type")
    if error_type == TIMEOUT and retry_count >= 1:
        downgraded = dict(error)
        downgraded["recovery_strategy"] = RECOVERY_SURFACE
        downgraded["message"] = (
            f"{error.get('message', '')} (查询超时，已重试 {retry_count} 次，不再重试)"
        ).strip()
        return SQLError(**downgraded)  # type: ignore[arg-type]

    if retry_count >= max_retries:
        exhausted = dict(error)
        exhausted["error_type"] = RETRY_EXHAUSTED
        exhausted["recovery_strategy"] = RECOVERY_ABORT
        exhausted["message"] = (
            f"SQL 重试次数已用尽（{retry_count}/{max_retries}）：{error.get('message', '')}"
        )
        return SQLError(**exhausted)  # type: ignore[arg-type]

    return error


def error_summary(error: SQLError) -> dict[str, Any]:
    """Compact, log-safe view of an error (used in prompts for regeneration)."""
    summary: dict[str, Any] = {
        "error_type": error.get("error_type"),
        "message": error.get("message"),
        "recovery_strategy": error.get("recovery_strategy"),
    }
    if "attempt" in error:
        summary["attempt"] = error["attempt"]
    return summary
