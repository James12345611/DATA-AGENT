"""``regenerate_sql`` node (text2sql_V1.md section 5.8).

Only triggered by a *recoverable execution error*.  It keeps the original
question, the structured intent and the full error history, never widens the
table set, and always sends the fixed statement back through ``validate_sql``.
"""

from __future__ import annotations

import logging
from typing import Any

from ..errors import (
    LLM_ERROR,
    RECOVERY_ABORT,
    RETRY_EXHAUSTED,
    SQL_GENERATION_ERROR,
    sql_error,
)
from ..llm import LLMError
from ..prompts import entities_json, regeneration_prompt
from ..state import SQLGraphState
from .deps import NodeDeps
from .generate_sql import _render_errors

logger = logging.getLogger(__name__)


def regenerate_sql(state: SQLGraphState, *, deps: NodeDeps) -> dict[str, Any]:
    """Node entry point."""
    settings = deps.settings
    question = state.get("question") or ""
    rewrite_question = state.get("rewrite_question")
    entities = state.get("info_entities")
    selections = state.get("selected_tables") or []
    sql = state.get("sql")
    previous_errors = list(state.get("previous_sql_errors") or [])
    retry_count = int(state.get("retry_count") or 0)

    if retry_count >= settings.sql_max_retries:
        return {
            "status": "failed",
            "error": sql_error(
                RETRY_EXHAUSTED,
                f"SQL 重试次数已用尽（{retry_count}/{settings.sql_max_retries}）",
                RECOVERY_ABORT,
                sql=sql if isinstance(sql, str) else None,
                attempt=retry_count,
                settings=settings,
            ),
        }

    missing = [
        name
        for name, value in (
            ("rewrite_question", rewrite_question),
            ("info_entities", entities),
            ("selected_tables", selections),
            ("sql", sql),
        )
        if not value
    ]
    if missing:
        return {
            "status": "failed",
            "error": sql_error(
                SQL_GENERATION_ERROR,
                "修正 SQL 所需的前置状态缺失：" + "、".join(missing),
                RECOVERY_ABORT,
                attempt=retry_count,
                settings=settings,
            ),
        }

    prompt = regeneration_prompt(
        question=question,
        rewrite_question=str(rewrite_question),
        entities_json=entities_json(dict(entities)),  # type: ignore[arg-type]
        schema_block=deps.catalog.describe_selection(selections),
        metric_guidance=deps.catalog.metric_guidance(),
        max_rows=settings.sql_max_rows,
        previous_sql=str(sql),
        previous_errors=_render_errors(previous_errors) or "（无结构化错误，请按原意图修正）",
    )

    try:
        new_sql = deps.llm.complete_sql(prompt)
    except LLMError as exc:
        return {
            "status": "failed",
            "error": sql_error(
                LLM_ERROR,
                str(exc),
                RECOVERY_ABORT,
                sql=str(sql),
                attempt=retry_count,
                settings=settings,
            ),
        }
    except Exception as exc:  # noqa: BLE001 - provider specific failures
        return {
            "status": "failed",
            "error": sql_error(
                LLM_ERROR,
                f"SQL 修正失败：{exc}",
                RECOVERY_ABORT,
                sql=str(sql),
                attempt=retry_count,
                settings=settings,
            ),
        }

    if not new_sql or not new_sql.strip():
        return {
            "status": "failed",
            "error": sql_error(
                SQL_GENERATION_ERROR,
                "模型未返回修正后的 SQL",
                RECOVERY_ABORT,
                sql=str(sql),
                attempt=retry_count,
                settings=settings,
            ),
        }

    logger.info("第 %s 次修正 SQL", retry_count + 1)
    return {
        "sql": new_sql.strip(),
        "retry_count": retry_count + 1,
        "status": "pending",
        "error": None,
    }


__all__ = ["regenerate_sql"]
