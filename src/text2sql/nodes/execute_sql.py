"""``execute_sql`` node (text2sql_V1.md section 5.7).

Runs the validated statement on the read-only connection and normalizes the
driver result into state.  An empty result set is a success, not a failure.
"""

from __future__ import annotations

import logging
from typing import Any

from ..errors import (
    EXECUTION_ERROR,
    RECOVERY_SURFACE,
    classify_db_error,
    enforce_retry_policy,
    sql_error,
)
from ..state import SQLGraphState
from .deps import NodeDeps

logger = logging.getLogger(__name__)


def execute_sql(state: SQLGraphState, *, deps: NodeDeps) -> dict[str, Any]:
    """Node entry point."""
    settings = deps.settings
    sql = state.get("sql")
    retry_count = int(state.get("retry_count") or 0)
    previous_errors = list(state.get("previous_sql_errors") or [])

    if not isinstance(sql, str) or not sql.strip():
        return {
            "status": "failed",
            "error": sql_error(
                EXECUTION_ERROR,
                "缺少可执行的 SQL",
                RECOVERY_SURFACE,
                attempt=retry_count,
                settings=settings,
            ),
        }

    try:
        logger.debug("execute_sql 执行：\n%s", sql)
        result = deps.executor.execute(sql, max_rows=settings.sql_max_rows)
    except Exception as exc:  # noqa: BLE001 - classified below
        error_type, recovery = classify_db_error(exc)
        error = sql_error(
            error_type,
            f"SQL 执行失败：{exc}",
            recovery,
            sql=sql,
            attempt=retry_count,
            settings=settings,
        )
        error = enforce_retry_policy(
            error,
            retry_count=retry_count,
            max_retries=settings.sql_max_retries,
        )
        error["attempt"] = retry_count
        logger.info(
            "SQL 执行失败 error_type=%s recovery=%s attempt=%s",
            error.get("error_type"),
            error.get("recovery_strategy"),
            retry_count,
        )
        if error.get("recovery_strategy") == "retry":
            # Recoverable: keep the history and let the router try regenerate_sql.
            return {
                "status": "pending",
                "error": error,
                "previous_sql_errors": [*previous_errors, error],
            }
        return {"status": "failed", "error": error}

    payload = result.as_state()
    payload.update({"status": "success", "error": None})
    logger.info(
        "SQL 执行成功 rows=%s elapsed_ms=%s truncated=%s",
        result.row_count,
        result.elapsed_ms,
        result.truncated,
    )
    return payload


__all__ = ["execute_sql"]
