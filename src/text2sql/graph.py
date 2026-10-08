"""Graph orchestration (text2sql_V1.md sections 3, 5.9, 5.10).SQL编排层

    START -> information_extraction -> schema_linking -> generate_sql
          -> validate_sql --allowed--> execute_sql --success--> END
                          --rejected-> failed              --retryable--> regenerate_sql
                                                           --terminal---> failed
          regenerate_sql --retry_available--> validate_sql
                         --retry_exhausted--> failed

The three conditional edge functions below are the ones fixed by section 5.10;
they only read state and return a routing key - no LLM, no database, no writes.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from .catalog import Catalog, load_catalog
from .config import Settings, get_settings
from .db import ReadOnlyExecutor
from .llm import BaseLLMClient, build_llm
from .nodes.deps import NodeDeps
from .nodes.execute_sql import execute_sql
from .nodes.generate_sql import generate_sql
from .nodes.information_extraction import information_extraction
from .nodes.regenerate_sql import regenerate_sql
from .nodes.schema_linking import schema_linking
from .nodes.validate_sql import validate_sql
from .state import SQLGraphState, SQLOutputState, project_output

logger = logging.getLogger(__name__)


# --------------------------------------------------------------- conditional edges
def validate_sql_conditional_edges(
    state: SQLGraphState,
) -> Literal["execute_sql", "failed"]:
    if state.get("status") == "pending" and state.get("error") is None:
        return "execute_sql"
    return "failed"


def execute_sql_conditional_edges(
    state: SQLGraphState,
) -> Literal["success", "regenerate_sql", "failed"]:
    if state.get("status") == "success" and state.get("error") is None:
        return "success"

    error = state.get("error")
    if error is None:
        return "failed"

    if error["recovery_strategy"] == "retry":
        if state.get("retry_count", 0) < _MAX_RETRIES[0]:
            return "regenerate_sql"
    return "failed"


def regenerate_sql_conditional_edges(
    state: SQLGraphState,
) -> Literal["validate_sql", "failed"]:
    if (
        state.get("status") == "pending"
        and state.get("sql")
        and state.get("retry_count", 0) <= _MAX_RETRIES[0]
        and state.get("error") is None
    ):
        return "validate_sql"
    return "failed"


def pending_conditional_edges(state: SQLGraphState, *, next_node: str) -> str:
    """Shared routing for ``information_extraction`` / ``schema_linking``."""
    if state.get("status") == "pending" and state.get("error") is None:
        return next_node
    return "failed"


# ``SQL_MAX_RETRIES`` is configuration, but section 5.10 fixes the comparison
# against 2 for the default deployment.  The graph factory refreshes this value
# from settings so both stay consistent.
_MAX_RETRIES: list[int] = [2]


def failed_node(state: SQLGraphState) -> dict[str, Any]:
    """Terminal failure exit: guarantees ``status="failed"`` with an error."""
    if state.get("error") is None:
        from .errors import EXECUTION_ERROR, RECOVERY_SURFACE, sql_error

        return {
            "status": "failed",
            "error": sql_error(
                EXECUTION_ERROR,
                "查询失败，但没有可用的结构化错误",
                RECOVERY_SURFACE,
            ),
        }
    return {"status": "failed"}


# ------------------------------------------------------------------ graph build
def build_graph(deps: NodeDeps | None = None, *, settings: Settings | None = None):
    """Compile the V1 SQL subgraph."""
    if deps is None:
        deps = NodeDeps.build(settings)
    _MAX_RETRIES[0] = deps.settings.sql_max_retries

    builder = StateGraph(SQLGraphState)
    builder.add_node("information_extraction", partial(information_extraction, deps=deps))
    builder.add_node("schema_linking", partial(schema_linking, deps=deps))
    builder.add_node("generate_sql", partial(generate_sql, deps=deps))
    builder.add_node("validate_sql", partial(validate_sql, deps=deps))
    builder.add_node("execute_sql", partial(execute_sql, deps=deps))
    builder.add_node("regenerate_sql", partial(regenerate_sql, deps=deps))
    builder.add_node("failed", failed_node)

    builder.add_edge(START, "information_extraction")
    builder.add_conditional_edges(
        "information_extraction",
        partial(pending_conditional_edges, next_node="schema_linking"),
        {"schema_linking": "schema_linking", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "schema_linking",
        partial(pending_conditional_edges, next_node="generate_sql"),
        {"generate_sql": "generate_sql", "failed": "failed"},
    )
    builder.add_edge("generate_sql", "validate_sql")
    builder.add_conditional_edges(
        "validate_sql",
        validate_sql_conditional_edges,
        {"execute_sql": "execute_sql", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "execute_sql",
        execute_sql_conditional_edges,
        {"success": END, "regenerate_sql": "regenerate_sql", "failed": "failed"},
    )
    builder.add_conditional_edges(
        "regenerate_sql",
        regenerate_sql_conditional_edges,
        {"validate_sql": "validate_sql", "failed": "failed"},
    )
    builder.add_edge("failed", END)
    return builder.compile()


def make_deps(
    settings: Settings | None = None,
    *,
    llm: BaseLLMClient | None = None,
    executor: ReadOnlyExecutor | None = None,
    catalog: Catalog | None = None,
    force_offline_llm: bool = False,
) -> NodeDeps:
    settings = settings or get_settings()
    executor = executor or ReadOnlyExecutor(settings)
    catalog = catalog or load_catalog(settings)
    llm = llm or build_llm(settings, catalog=catalog, force_offline=force_offline_llm)
    return NodeDeps(settings=settings, catalog=catalog, llm=llm, executor=executor)


def run_query(
    question: str,
    *,
    messages: list[Any] | None = None,
    deps: NodeDeps | None = None,
    graph: Any | None = None,
    settings: Settings | None = None,
    force_offline_llm: bool = False,
) -> SQLOutputState:
    """Run one question through the compiled subgraph and project the output."""
    deps = deps or make_deps(settings, force_offline_llm=force_offline_llm)
    graph = graph or build_graph(deps)
    initial: SQLGraphState = {
        "question": question,
        "messages": list(messages or []),
        "retry_count": 0,
        "previous_sql_errors": [],
        "status": "pending",
        "error": None,
    }
    final_state: SQLGraphState = graph.invoke(initial)
    return project_output(final_state)


__all__ = [
    "build_graph",
    "execute_sql_conditional_edges",
    "failed_node",
    "make_deps",
    "pending_conditional_edges",
    "regenerate_sql_conditional_edges",
    "run_query",
    "validate_sql_conditional_edges",
]
