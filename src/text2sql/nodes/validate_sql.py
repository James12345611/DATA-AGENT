"""``validate_sql`` node (text2sql_V1.md section 5.6).

Thin wrapper around :class:`text2sql.validation.SQLValidator`: a rejection never
routes to ``regenerate_sql`` in V1, it goes straight to the failed exit.
"""

from __future__ import annotations

import logging
from typing import Any

from ..errors import (
    RECOVERY_ABORT,
    SQL_POLICY_ERROR,
    sql_error,
)
from ..state import SQLGraphState
from ..validation import SQLValidator, ValidationConfig
from .deps import NodeDeps

logger = logging.getLogger(__name__)


def validate_sql(state: SQLGraphState, *, deps: NodeDeps) -> dict[str, Any]:
    """Node entry point."""
    settings = deps.settings
    sql = state.get("sql")
    selections = state.get("selected_tables") or []
    attempt = int(state.get("retry_count") or 0)

    if not isinstance(sql, str) or not sql.strip():
        return {
            "status": "failed",
            "error": sql_error(
                SQL_POLICY_ERROR,
                "缺少待校验的 SQL",
                RECOVERY_ABORT,
                attempt=attempt,
                settings=settings,
            ),
        }
    if not selections:
        return {
            "status": "failed",
            "error": sql_error(
                SQL_POLICY_ERROR,
                "缺少 selected_tables，无法校验表字段边界",
                RECOVERY_ABORT,
                sql=sql,
                attempt=attempt,
                settings=settings,
            ),
        }

    validator = SQLValidator(
        deps.catalog,
        ValidationConfig.from_settings(settings),
        settings,
    )
    result = validator.validate(sql, selections, attempt=attempt)

    if not result.ok:
        logger.info(
            "validate_sql 拒绝 SQL：%s",
            result.error.get("error_type") if result.error else "unknown",
        )
        logger.debug("被拒绝的 SQL：\n%s", sql)
        return {"status": "failed", "error": result.error}

    payload: dict[str, Any] = {"status": "pending", "error": None}
    if result.normalized:
        # LIMIT normalization is the only field the validator is allowed to fix.
        payload["sql"] = result.sql
        logger.info("validate_sql 通过并规范化 LIMIT：%s", result.notes)
    else:
        logger.debug("validate_sql 通过：tables=%s", result.tables)
    return payload


__all__ = ["validate_sql"]
