"""Graph orchestration and routing tests (text2sql_V1.md sections 3, 5.9, 5.10)."""

from __future__ import annotations

from typing import Any

import pymysql
import pytest

from text2sql.db.executor import QueryResult
from text2sql.graph import (
    build_graph,
    execute_sql_conditional_edges,
    pending_conditional_edges,
    regenerate_sql_conditional_edges,
    run_query,
    validate_sql_conditional_edges,
)
from text2sql.nodes.deps import NodeDeps

VALID_SQL = (
    "SELECT u.channel, COUNT(u.user_id) AS users FROM dim_user AS u "
    "GROUP BY u.channel LIMIT 100"
)
UNKNOWN_COLUMN_SQL = (
    "SELECT u.gender FROM dim_user AS u GROUP BY u.gender LIMIT 100"
)
DANGEROUS_SQL = "DROP TABLE dim_user"


class FakeExecutor:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[str] = []
        self.closed = False

    def execute(self, sql: str, *, max_rows: int | None = None) -> QueryResult:
        self.calls.append(sql)
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        self.closed = True


def rows(*values: dict[str, Any]) -> QueryResult:
    columns = list(values[0].keys()) if values else ["channel"]
    return QueryResult(columns=columns, rows=list(values), row_count=len(values), elapsed_ms=1)


def test_validate_sql_routing_contract():
    assert validate_sql_conditional_edges({"status": "pending", "error": None}) == "execute_sql"
    assert validate_sql_conditional_edges({"status": "failed", "error": {"error_type": "x"}}) == (
        "failed"
    )
    assert validate_sql_conditional_edges({"status": "pending", "error": {"error_type": "x"}}) == (
        "failed"
    )


def test_execute_sql_routing_contract():
    assert execute_sql_conditional_edges({"status": "success", "error": None}) == "success"
    assert (
        execute_sql_conditional_edges(
            {
                "status": "pending",
                "error": {"error_type": "unknown_column", "recovery_strategy": "retry"},
                "retry_count": 0,
            }
        )
        == "regenerate_sql"
    )
    assert (
        execute_sql_conditional_edges(
            {
                "status": "pending",
                "error": {"error_type": "retry_exhausted", "recovery_strategy": "abort"},
                "retry_count": 2,
            }
        )
        == "failed"
    )
    assert (
        execute_sql_conditional_edges(
            {
                "status": "pending",
                "error": {"error_type": "connection_error", "recovery_strategy": "surface_to_user"},
                "retry_count": 0,
            }
        )
        == "failed"
    )
    assert execute_sql_conditional_edges({"status": "failed"}) == "failed"


def test_regenerate_sql_routing_contract():
    assert (
        regenerate_sql_conditional_edges(
            {"status": "pending", "sql": VALID_SQL, "retry_count": 1, "error": None}
        )
        == "validate_sql"
    )
    assert (
        regenerate_sql_conditional_edges(
            {"status": "failed", "sql": VALID_SQL, "retry_count": 1, "error": {"error_type": "x"}}
        )
        == "failed"
    )
    assert regenerate_sql_conditional_edges({"status": "pending", "sql": "", "retry_count": 1}) == (
        "failed"
    )


def test_pending_routing_contract():
    assert pending_conditional_edges({"status": "pending", "error": None}, next_node="x") == "x"
    assert pending_conditional_edges({"status": "failed", "error": {}}, next_node="x") == "failed"


def _deps(settings, catalog, llm, executor) -> NodeDeps:
    return NodeDeps(settings=settings, catalog=catalog, llm=llm, executor=executor)  # type: ignore[arg-type]


def test_graph_success_path(settings, catalog, scripted_llm):
    llm = scripted_llm(
        json_payload={
            "rewrite_question": "按渠道统计用户数",
            "keywords": ["渠道"],
            "dimensions": ["渠道"],
            "metrics": ["用户数"],
        },
        sql_script=[VALID_SQL],
    )
    executor = FakeExecutor([rows({"channel": "ads", "users": 3})])
    deps = _deps(settings, catalog, llm, executor)
    output = run_query("按渠道统计用户数", deps=deps, graph=build_graph(deps))

    assert output["status"] == "success"
    assert output["row_count"] == 1
    assert output["result_columns"] == ["channel", "users"]
    assert output["error"] is None
    assert {selection["table"] for selection in output["selected_tables"]} == {"dim_user"}


def test_graph_validation_rejection_never_regenerates(settings, catalog, scripted_llm):
    llm = scripted_llm(
        json_payload={
            "rewrite_question": "删除所有订单",
            "keywords": ["订单"],
            "dimensions": [],
            "metrics": [],
        },
        sql_script=[DANGEROUS_SQL, VALID_SQL],
    )
    executor = FakeExecutor([rows({"channel": "ads"})])
    deps = _deps(settings, catalog, llm, executor)
    output = run_query("删除所有订单", deps=deps, graph=build_graph(deps))

    # the destructive phrase guard fires before any LLM call or execution
    assert output["status"] == "failed"
    assert output["error"]["error_type"] == "unsafe_request"
    assert output["error"]["recovery_strategy"] == "abort"
    assert executor.calls == []
    assert llm.sql_calls == 0


def test_graph_validation_rejection_of_generated_sql_aborts(settings, catalog, scripted_llm):
    llm = scripted_llm(
        json_payload={
            "rewrite_question": "按渠道统计用户数",
            "keywords": ["渠道"],
            "dimensions": ["渠道"],
            "metrics": ["用户数"],
        },
        sql_script=[DANGEROUS_SQL, VALID_SQL],
    )
    executor = FakeExecutor([rows({"channel": "ads"})])
    deps = _deps(settings, catalog, llm, executor)
    output = run_query("按渠道统计用户数", deps=deps, graph=build_graph(deps))

    assert output["status"] == "failed"
    assert output["error"]["error_type"] in {"unsafe_sql", "sql_policy_error"}
    assert output["error"]["recovery_strategy"] == "abort"
    assert executor.calls == []
    assert llm.sql_calls == 1, "安全拒绝不得进入 regenerate_sql"


def test_graph_regenerates_after_recoverable_error(settings, catalog, scripted_llm):
    """A transient execution failure must be retried through regenerate_sql."""
    error = pymysql.err.OperationalError(1205, "Lock wait timeout exceeded")
    llm = scripted_llm(
        json_payload={
            "rewrite_question": "按渠道统计用户数",
            "keywords": ["渠道"],
            "dimensions": ["渠道"],
            "metrics": ["用户数"],
        },
        sql_script=[VALID_SQL],
    )
    executor = FakeExecutor([error, rows({"channel": "ads", "users": 3})])
    deps = _deps(settings, catalog, llm, executor)
    output = run_query("按渠道统计用户数", deps=deps, graph=build_graph(deps))

    assert output["status"] == "success"
    assert output["row_count"] == 1
    assert len(executor.calls) == 2
    assert llm.sql_calls == 2, "可恢复错误必须经过 regenerate_sql"


def test_graph_records_previous_errors_for_regeneration(settings, catalog, scripted_llm):
    error = pymysql.err.ProgrammingError(1054, "Unknown column 'u.gender'")
    llm = scripted_llm(
        json_payload={
            "rewrite_question": "按渠道统计用户数",
            "keywords": ["渠道"],
            "dimensions": ["渠道"],
            "metrics": ["用户数"],
        },
        sql_script=[VALID_SQL],
    )
    executor = FakeExecutor([error, rows({"channel": "ads", "users": 1})])
    deps = _deps(settings, catalog, llm, executor)
    graph = build_graph(deps)
    state = graph.invoke(
        {
            "question": "按渠道统计用户数",
            "messages": [],
            "retry_count": 0,
            "previous_sql_errors": [],
            "status": "pending",
            "error": None,
        }
    )
    assert state["status"] == "success"
    assert state["retry_count"] == 1
    assert len(state["previous_sql_errors"]) == 1
    first_error = state["previous_sql_errors"][0]
    assert first_error["error_type"] == "unknown_column"
    assert first_error["attempt"] == 0
    assert "必须修正的错误" in llm.prompts[-1].user


def test_graph_stops_after_retry_budget_is_exhausted(settings, catalog, scripted_llm):
    error = pymysql.err.ProgrammingError(1054, "Unknown column 'u.gender'")
    llm = scripted_llm(
        json_payload={
            "rewrite_question": "按渠道统计用户数",
            "keywords": ["渠道"],
            "dimensions": ["渠道"],
            "metrics": ["用户数"],
        },
        sql_script=[VALID_SQL],
    )
    executor = FakeExecutor([error])
    deps = _deps(settings, catalog, llm, executor)
    output = run_query("按渠道统计用户数", deps=deps, graph=build_graph(deps))

    assert output["status"] == "failed"
    assert output["error"]["error_type"] == "retry_exhausted"
    assert output["error"]["recovery_strategy"] == "abort"
    # 1 initial attempt + SQL_MAX_RETRIES regenerations, never more
    assert len(executor.calls) == settings.sql_max_retries + 1
    assert llm.sql_calls == settings.sql_max_retries + 1


def test_graph_does_not_retry_connection_errors(settings, catalog, scripted_llm):
    error = pymysql.err.OperationalError(2003, "Can't connect to MySQL server")
    llm = scripted_llm(
        json_payload={
            "rewrite_question": "按渠道统计用户数",
            "keywords": ["渠道"],
            "dimensions": ["渠道"],
            "metrics": ["用户数"],
        },
        sql_script=[VALID_SQL],
    )
    executor = FakeExecutor([error])
    deps = _deps(settings, catalog, llm, executor)
    output = run_query("按渠道统计用户数", deps=deps, graph=build_graph(deps))

    assert output["status"] == "failed"
    assert output["error"]["error_type"] == "connection_error"
    assert output["error"]["recovery_strategy"] == "surface_to_user"
    assert llm.sql_calls == 1


def test_graph_retries_timeout_once_then_surfaces(settings, catalog, scripted_llm):
    error = pymysql.err.OperationalError(3024, "Query execution was interrupted")
    llm = scripted_llm(
        json_payload={
            "rewrite_question": "按渠道统计用户数",
            "keywords": ["渠道"],
            "dimensions": ["渠道"],
            "metrics": ["用户数"],
        },
        sql_script=[VALID_SQL],
    )
    executor = FakeExecutor([error])
    deps = _deps(settings, catalog, llm, executor)
    output = run_query("按渠道统计用户数", deps=deps, graph=build_graph(deps))

    assert output["status"] == "failed"
    assert output["error"]["error_type"] == "timeout"
    assert output["error"]["recovery_strategy"] == "surface_to_user"
    assert llm.sql_calls == 2


def test_graph_fails_fast_on_schema_linking_error(settings, catalog, scripted_llm):
    llm = scripted_llm(
        json_payload={
            "rewrite_question": "统计各商品的名称和品牌",
            "keywords": ["商品名称", "品牌"],
            "dimensions": ["品牌"],
            "metrics": ["商品名称"],
        },
        sql_script=[VALID_SQL],
    )
    executor = FakeExecutor([rows({"channel": "ads"})])
    deps = _deps(settings, catalog, llm, executor)
    output = run_query("统计各商品的名称和品牌", deps=deps, graph=build_graph(deps))

    assert output["status"] == "failed"
    assert output["error"]["error_type"] == "schema_linking_error"
    assert llm.sql_calls == 0
    assert executor.calls == []


def test_graph_output_contract_keys(settings, catalog, scripted_llm):
    llm = scripted_llm(
        json_payload={
            "rewrite_question": "按渠道统计用户数",
            "keywords": ["渠道"],
            "dimensions": ["渠道"],
            "metrics": ["用户数"],
        },
        sql_script=[VALID_SQL],
    )
    executor = FakeExecutor([rows({"channel": "ads", "users": 3})])
    deps = _deps(settings, catalog, llm, executor)
    output = run_query("按渠道统计用户数", deps=deps, graph=build_graph(deps))
    assert set(output) == {
        "question",
        "rewrite_question",
        "selected_tables",
        "sql",
        "result_rows",
        "result_columns",
        "row_count",
        "status",
        "error",
    }
    assert output["question"] == "按渠道统计用户数"
