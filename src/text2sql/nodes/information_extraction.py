"""``information_extraction`` node (text2sql_V1.md section 5.3).

Responsibilities: turn the user question (+ optional multi-turn context) into a
structured query intent.  It does not choose physical tables, does not generate
SQL and does not touch the database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Callable

from ..errors import (
    EXTRACTION_ERROR,
    LLM_ERROR,
    RECOVERY_ABORT,
    RECOVERY_SURFACE,
    sql_error,
)
from ..guardrails import looks_like_unsafe_request
from ..llm import LLMError
from ..prompts import Prompt, extraction_prompt
from ..state import (
    SQLGraphState,
    StateContractError,
    normalize_entities,
)
from .deps import NodeDeps, history_messages

logger = logging.getLogger(__name__)

UNSAFE_REQUEST = "unsafe_request"


def call_json_with_retries(
    llm: Any,
    prompt: Prompt,
    *,
    validator: Callable[[dict[str, Any]], Any] | None,
    attempts: int,
) -> dict[str, Any]:
    """Call the model for a JSON payload, feeding schema errors back on retry.
    大模型输出结果校验与重试机制，面试重点：如何让模型稳定输出合法JSON格式
    """
    last_error: Exception | None = None
    current = prompt
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return llm.complete_json(current, validator=validator)
        except (LLMError, StateContractError, ValueError) as exc:
            last_error = exc
            logger.warning("structured extraction attempt %s failed: %s", attempt, exc)
            current = replace(
                prompt,
                user=(
                    f"{prompt.user}\n\n上一次输出不合法：{exc}\n"
                    "请严格按 JSON 结构重新输出，不要包含解释文字。"
                ),
            )
    raise LLMError(f"结构化输出失败（尝试 {attempts} 次）：{last_error}")


def _validate_extraction_payload(payload: dict[str, Any]) -> None:
    """抽取结果校验，重点抽取 unsafe_request 和 rewrite_question"""
    if payload.get("unsafe_request") is True:
        raise StateContractError("unsafe_request")
    rewrite = payload.get("rewrite_question")
    if rewrite is not None and not isinstance(rewrite, str):
        raise StateContractError("rewrite_question 必须是字符串")


def information_extraction(
    state: SQLGraphState,
    *,
    deps: NodeDeps,
) -> dict[str, Any]:
    """Node entry point."""
    settings = deps.settings
    question = state.get("question")
    if not isinstance(question, str) or not question.strip():
        return {
            "status": "failed",
            "error": sql_error(
                EXTRACTION_ERROR,
                "无法提取有效查询意图：question 必须是非空字符串",
                RECOVERY_SURFACE,
                settings=settings,
            ),
        }
    question = question.strip()

    # Deterministic guardrail: never send a write/destroy request to the model.
    if looks_like_unsafe_request(question):
        return {
            "status": "failed",
            "error": sql_error(
                UNSAFE_REQUEST,
                "拒绝执行：V1 只支持只读查询，问题中包含写入、修改或删除数据的意图",
                RECOVERY_ABORT,
                settings=settings,
            ),
        }

    messages = state.get("messages") or []
    if not isinstance(messages, list):
        return {
            "status": "failed",
            "error": sql_error(
                EXTRACTION_ERROR,
                "无法提取有效查询意图：messages 必须是消息列表",
                RECOVERY_SURFACE,
                settings=settings,
            ),
        }
    history = history_messages(question, messages)

    prompt = extraction_prompt(question, history)

    try:
        payload = call_json_with_retries(
            deps.llm,
            prompt,
            validator=_validate_extraction_payload,
            attempts=settings.llm_max_attempts,
        )
    except StateContractError as exc:
        if str(exc) == "unsafe_request":
            return {
                "status": "failed",
                "error": sql_error(
                    UNSAFE_REQUEST,
                    "拒绝执行：V1 只支持只读查询，问题中包含写入、修改或删除数据的意图",
                    RECOVERY_ABORT,
                    settings=settings,
                ),
            }
        return {
            "status": "failed",
            "error": sql_error(EXTRACTION_ERROR, str(exc), RECOVERY_SURFACE, settings=settings),
        }
    except LLMError as exc:
        return {
            "status": "failed",
            "error": sql_error(LLM_ERROR, str(exc), RECOVERY_SURFACE, settings=settings),
        }
    except Exception as exc:  # noqa: BLE001 - provider specific failures
        return {
            "status": "failed",
            "error": sql_error(
                LLM_ERROR,
                f"模型调用失败：{exc}",
                RECOVERY_SURFACE,
                settings=settings,
            ),
        }

    try:
        entities = normalize_entities(payload)
    except StateContractError as exc:
        return {
            "status": "failed",
            "error": sql_error(
                EXTRACTION_ERROR,
                f"查询意图未通过 schema 校验：{exc}",
                RECOVERY_SURFACE,
                settings=settings,
            ),
        }

    if not (entities["keywords"] or entities["dimensions"] or entities["metrics"]):
        return {
            "status": "failed",
            "error": sql_error(
                EXTRACTION_ERROR,
                "无法提取有效查询意图：没有识别到任何业务关键词、维度或指标",
                RECOVERY_SURFACE,
                settings=settings,
            ),
        }

    rewrite_question = payload.get("rewrite_question") or question 
    if not isinstance(rewrite_question, str) or not rewrite_question.strip():
        rewrite_question = question # rewrite_question是优化项不是必须项，这个字段为None不影响，直接降级为 question本身也是没问题的

    logger.info(
        "information_extraction 规范化问题=%r 指标=%s 维度=%s 过滤=%s",
        rewrite_question.strip(),
        entities["metrics"],
        entities["dimensions"],
        [condition["field_hint"] for condition in entities["filters"]],
    )

    return {
        "rewrite_question": rewrite_question.strip(),
        "info_entities": entities,
        "status": "pending",
        "error": None,
    }


__all__ = ["UNSAFE_REQUEST", "call_json_with_retries", "information_extraction"]
