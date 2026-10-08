"""Read-only MySQL execution layer (text2sql_V1.md section 5.7).

Guarantees enforced here, independently of the LLM and of the validator:

* the connection uses the dedicated read-only account,
* every session runs with a statement timeout (``max_execution_time``),
* every session defaults to a READ ONLY transaction, so a write can never
  commit even if the validator were bypassed,
* multi-statement SQL is impossible (PyMySQL client flag is not enabled),
* credentials never appear in state, logs or error messages.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
from decimal import Decimal
from typing import Any, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from ..config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass
class QueryResult:
    """Normalized execution result."""

    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    elapsed_ms: int
    truncated: bool = False

    def as_state(self) -> dict[str, Any]:
        return {
            "result_rows": self.rows,
            "result_columns": self.columns,
            "row_count": self.row_count,
        }


def jsonable(value: Any) -> Any:
    """Convert a DBAPI value into a stable, serializable Python value."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, dt_time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


class ReadOnlyExecutor:
    """Executes validated SQL on a read-only MySQL connection."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        connect_retries: int = 2,
        connect_backoff_seconds: float = 0.5,
    ) -> None:
        self.settings = settings or get_settings()
        self._engine: Engine | None = None
        self._connect_retries = max(0, connect_retries)
        self._connect_backoff_seconds = connect_backoff_seconds
        # A rejected write attempt must never be reported with credentials in it.
        self._url = self.settings.sqlalchemy_url()

    # ------------------------------------------------------------- engine
    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(
                self._url,
                pool_pre_ping=True,
                pool_recycle=1800,
                pool_size=5,
                max_overflow=5,
                future=True,
                connect_args={
                    "connect_timeout": min(10, self.settings.sql_timeout_seconds),
                    "read_timeout": self.settings.sql_timeout_seconds + 5,
                    "write_timeout": self.settings.sql_timeout_seconds + 5,
                    "charset": self.settings.db_charset,
                },
            )
        return self._engine

    def _connect(self):
        """Open a connection, retrying transient connection failures."""
        attempt = 0
        while True:
            try:
                return self.engine.connect()
            except Exception as exc:  # noqa: BLE001 - classified by the caller
                if attempt >= self._connect_retries:
                    raise
                attempt += 1
                logger.warning(
                    "database connect failed (attempt %s/%s): %s",
                    attempt,
                    self._connect_retries,
                    self.settings.redact(exc),
                )
                time.sleep(self._connect_backoff_seconds * attempt)

    # ------------------------------------------------------------ metadata
    def live_columns(self, tables: list[str] | None = None) -> dict[str, list[str]]:
        """Read the live column list of the exposed views (section 6.7.2)."""
        tables = tables or list(_EXPOSED_TABLES)
        placeholders = ", ".join(f":t{i}" for i in range(len(tables)))
        params = {f"t{i}": name for i, name in enumerate(tables)}
        sql = (
            "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA = :schema AND TABLE_NAME IN ({placeholders}) "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION"
        )
        out: dict[str, list[str]] = {name: [] for name in tables}
        conn = self._connect()
        try:
            result = conn.execute(
                text(sql),
                {**params, "schema": self.settings.db_name},
            )
            for table_name, column_name in result:
                out.setdefault(str(table_name), []).append(str(column_name))
        finally:
            conn.close()
        return out

    def ping(self) -> bool:
        conn = self._connect()
        try:
            conn.exec_driver_sql("SELECT 1")
            return True
        finally:
            conn.close()

    # ----------------------------------------------------------- execution
    def execute(self, sql: str, *, max_rows: int | None = None) -> QueryResult:
        """Execute one validated read-only statement.

        Raises the original DBAPI/SQLAlchemy exception; classification into an
        ``SQLError`` happens in :mod:`text2sql.errors`.
        """
        limit = max_rows or self.settings.sql_max_rows
        timeout_ms = int(self.settings.sql_timeout_seconds * 1000)
        started = time.perf_counter()
        conn = self._connect()
        try:
            # Session level guards: timeout + READ ONLY transaction.
            conn.exec_driver_sql(f"SET SESSION max_execution_time = {timeout_ms}")
            conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
            result = conn.exec_driver_sql(sql)
            columns = [str(name) for name in result.keys()]
            rows: list[dict[str, Any]] = []
            truncated = False
            for row in result:
                if len(rows) >= limit:
                    truncated = True
                    break
                rows.append({col: jsonable(value) for col, value in zip(columns, row)})
        finally:
            conn.close()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            elapsed_ms=elapsed_ms,
            truncated=truncated,
        )

    def explain(self, sql: str) -> list[dict[str, Any]]:
        """``EXPLAIN`` a statement (diagnostics only, never used by the graph)."""
        conn = self._connect()
        try:
            result = conn.exec_driver_sql(f"EXPLAIN {sql}")
            columns = [str(name) for name in result.keys()]
            return [{col: jsonable(value) for col, value in zip(columns, row)} for row in result]
        finally:
            conn.close()

    # ------------------------------------------------------------ lifecycle
    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def __enter__(self) -> "ReadOnlyExecutor":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


_EXPOSED_TABLES = (
    "dim_user",
    "mart_user_behavior_summary",
    "mart_user_order_summary",
    "mart_campaign_summary",
    "mart_funnel_summary",
    "mart_retention_cohort",
)


__all__ = ["QueryResult", "ReadOnlyExecutor", "jsonable"]
