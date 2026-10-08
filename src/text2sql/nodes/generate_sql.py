"""``generate_sql`` node (text2sql_V1.md section 5.5).

Generates one MySQL read-only statement from the normalized question, the
structured intent and the catalog selections.  It never executes SQL and never
decides safety - that is ``validate_sql``'s job.
"""

from __future__ import annotations

import logging
from typing import Any

from ..errors import (
    LLM_ERROR,
    RECOVERY_SURFACE,
    SQL_GENERATION_ERROR,
    sql_error,
)
from ..llm import LLMError
from ..prompts import entities_json, generation_prompt
from ..state import SQLGraphState
from .deps import NodeDeps

logger = logging.getLogger(__name__)


def generate_sql(state: SQLGraphState, *, deps: NodeDeps) -> dict[str, Any]:
    """Node entry point."""
    settings = deps.settings
    question = state.get("question") or ""
    rewrite_question = state.get("rewrite_question")
    entities = state.get("info_entities")
    selections = state.get("selected_tables")
    previous_errors = list(state.get("previous_sql_errors") or [])
    retry_count = int(state.get("retry_count") or 0)

    missing = [
        name
        for name, value in (
            ("rewrite_question", rewrite_question),
            ("info_entities", entities),
            ("selected_tables", selections),
        )
        if not value
    ]
    if missing:
        return _fail(
            settings,
            "生成 SQL 所需的前置状态缺失：" + "、".join(missing),
        )

    prompt = generation_prompt(
        question=question,
        rewrite_question=str(rewrite_question),
        entities_json=entities_json(dict(entities)),  # type: ignore[arg-type]
        schema_block=deps.catalog.describe_selection(selections),  # type: ignore[arg-type]
        metric_guidance=deps.catalog.metric_guidance(),
        max_rows=settings.sql_max_rows,
        previous_errors=_render_errors(previous_errors),
    )

    try:
        sql = deps.llm.complete_sql(prompt)
    except LLMError as exc:
        return _fail(settings, str(exc), error_type=LLM_ERROR)
    except Exception as exc:  # noqa: BLE001 - provider specific failures
        return _fail(settings, f"SQL 生成失败：{exc}", error_type=LLM_ERROR)

    if not sql or not sql.strip():
        return _fail(settings, "模型未返回可用的 SQL")

    logger.info("generate_sql 生成 SQL（%s 字符）", len(sql.strip()))
    logger.debug("生成的 SQL：\n%s", sql.strip())

    return {
        "sql": sql.strip(),
        "retry_count": retry_count,
        "status": "pending",
        "error": None,
    }


def _render_errors(errors: list[dict[str, Any]]) -> str | None:
    if not errors:
        return None
    lines = []
    for index, error in enumerate(errors, start=1):
        lines.append(
            f"{index}. [{error.get('error_type')}] {error.get('message')}"
            + (f"（第 {error.get('attempt')} 次尝试）" if "attempt" in error else "")
        )
    return "\n".join(lines)


def _fail(settings, message: str, *, error_type: str = SQL_GENERATION_ERROR) -> dict[str, Any]:
    return {
        "status": "failed",
        "error": sql_error(error_type, message, RECOVERY_SURFACE, settings=settings),
    }


__all__ = ["generate_sql"]
