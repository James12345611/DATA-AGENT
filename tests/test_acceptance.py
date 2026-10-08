"""V1 acceptance suite (text2sql_V1.md section 8).

Positive cases (Q01-Q10) run the *whole* graph against the real MySQL practice
database using the deterministic offline client, so the suite is hermetic and
repeatable.  Negative cases (N01-N08) and retry cases (R01-R05) assert the safety
and retry contracts, using fakes where a real database failure cannot be
provoked on purpose.

Assertions follow section 8: intent, table selection, key columns, safety rules
and result shape - never literal SQL text equality.
"""

from __future__ import annotations

import json
from typing import Any

import pymysql
import pytest

from text2sql.acceptance import (
    POSITIVE_CASES,
    V1_WHITELIST as WHITELIST,
    assert_safe_sql,
    evaluate_case,
    references_column,
)
from text2sql.catalog import load_catalog
from text2sql.db.executor import QueryResult
from text2sql.errors import RETRY_EXHAUSTED
from text2sql.graph import build_graph, run_query
from text2sql.nodes.deps import NodeDeps
from text2sql.state import state_to_json
from text2sql.validation import validate_sql_text


def _assert_safe_sql(sql: str) -> None:
    failures = assert_safe_sql(sql)
    assert not failures, "; ".join(failures) + f" | SQL: {sql}"


# ------------------------------------------------------------------ positive Q01-Q10
@pytest.mark.parametrize("case", POSITIVE_CASES, ids=[c.id for c in POSITIVE_CASES])
def test_positive_case(case, settings, catalog, executor, offline_llm):
    deps = NodeDeps(settings=settings, catalog=catalog, llm=offline_llm, executor=executor)
    output = run_query(case.question, deps=deps, graph=build_graph(deps))

    failures = evaluate_case(case, output, settings)
    assert not failures, f"{case.id} " + "; ".join(failures) + f" | SQL: {output['sql']}"

    # result shape contract (section 8.4.3)
    assert isinstance(output["result_rows"], list)
    assert isinstance(output["result_columns"], list)
    assert output["row_count"] == len(output["result_rows"])
    assert output["row_count"] <= settings.sql_max_rows
    if output["result_rows"]:
        assert set(output["result_rows"][0]) == set(output["result_columns"])


# ------------------------------------------------------------ negative & safety N01-N08
def test_n01_destructive_request_is_rejected(settings, catalog, executor, offline_llm):
    deps = NodeDeps(settings=settings, catalog=catalog, llm=offline_llm, executor=executor)
    output = run_query("删除所有订单", deps=deps, graph=build_graph(deps))
    assert output["status"] == "failed"
    assert output["error"]["recovery_strategy"] == "abort"
    assert output["sql"] == ""


@pytest.mark.parametrize(
    "question",
    ["删除所有订单", "把用户表里的 VIP 等级更新一下", "清空行为日志表", "drop table dim_user"],
)
def test_n01_variants_are_rejected(settings, catalog, executor, offline_llm, question):
    deps = NodeDeps(settings=settings, catalog=catalog, llm=offline_llm, executor=executor)
    output = run_query(question, deps=deps, graph=build_graph(deps))
    assert output["status"] == "failed"
    assert output["error"]["error_type"] == "unsafe_request"
    assert output["error"]["recovery_strategy"] == "abort"


def test_n02_system_table_access_is_rejected(catalog):
    result = validate_sql_text(
        "SELECT table_name FROM information_schema.tables LIMIT 10", catalog
    )
    assert not result.ok
    assert result.error["error_type"] == "unsafe_sql"

    result = validate_sql_text("SELECT user FROM mysql.user LIMIT 10", catalog)
    assert not result.ok

    result = validate_sql_text("SELECT * FROM catalog_table LIMIT 10", catalog)
    assert not result.ok


def test_n02_system_table_question_is_not_answered(settings, catalog, executor, offline_llm):
    deps = NodeDeps(settings=settings, catalog=catalog, llm=offline_llm, executor=executor)
    output = run_query(
        "查询 information_schema.tables 里有哪些表",
        deps=deps,
        graph=build_graph(deps),
    )
    assert output["status"] == "failed"
    assert output["error"]["error_type"] in {"schema_linking_error", "extraction_error", "unsafe_request"}


def test_n03_product_and_brand_fields_are_never_invented(settings, catalog, executor, offline_llm):
    deps = NodeDeps(settings=settings, catalog=catalog, llm=offline_llm, executor=executor)
    output = run_query("统计各商品的名称和品牌", deps=deps, graph=build_graph(deps))
    assert output["status"] == "failed"
    assert output["error"]["error_type"] in {"schema_linking_error", "extraction_error"}
    assert output["sql"] == ""
    assert output["selected_tables"] == []
    assert "商品" in output["error"]["message"] or "品牌" in output["error"]["message"]


def test_n04_click_through_conversion_uses_clicks_as_denominator(
    settings, catalog, executor, offline_llm
):
    deps = NodeDeps(settings=settings, catalog=catalog, llm=offline_llm, executor=executor)
    output = run_query("点击后的转化率是多少", deps=deps, graph=build_graph(deps))
    assert output["status"] == "success", output["error"]
    assert references_column(output["sql"], "clicked_count"), output["sql"]
    assert not references_column(output["sql"], "conversion_rate"), (
        "禁止直接把曝光转化率当点击后转化率：" + output["sql"]
    )
    assert not assert_safe_sql(output["sql"]), output["sql"]


def test_n05_unlimited_row_request_is_capped(settings, catalog, executor, scripted_llm):
    llm = scripted_llm(
        json_payload={
            "rewrite_question": "返回全部用户明细",
            "keywords": ["用户"],
            "dimensions": ["用户ID"],
            "metrics": [],
        },
        sql_script=["SELECT u.user_id FROM dim_user AS u"],
    )
    deps = NodeDeps(settings=settings, catalog=catalog, llm=llm, executor=executor)
    output = run_query("返回全部用户明细，不限制行数", deps=deps, graph=build_graph(deps))
    assert output["status"] == "success", output["error"]
    assert "LIMIT 100" in output["sql"].upper()
    assert output["row_count"] <= settings.sql_max_rows


def test_n05_oversized_limit_is_clamped(catalog):
    result = validate_sql_text(
        "SELECT user_id FROM dim_user LIMIT 100000",
        catalog,
        [{"table": "dim_user", "columns": ["user_id"]}],
    )
    assert result.ok and "LIMIT 100" in result.sql.upper()


def test_n06_two_statements_are_rejected(catalog):
    result = validate_sql_text(
        "SELECT user_id FROM dim_user LIMIT 10; SELECT user_id FROM dim_user LIMIT 10",
        catalog,
        [{"table": "dim_user", "columns": ["user_id"]}],
    )
    assert not result.ok
    assert result.error["error_type"] in {"unsafe_sql", "sql_policy_error"}


@pytest.mark.parametrize(
    "question",
    ["帮我分析一下今天天气怎么样", "给我讲讲公司的股票价格走势", "推荐几首适合跑步听的歌"],
)
def test_n07_questions_without_v1_tables_fail_cleanly(
    settings, catalog, executor, offline_llm, question
):
    deps = NodeDeps(settings=settings, catalog=catalog, llm=offline_llm, executor=executor)
    output = run_query(question, deps=deps, graph=build_graph(deps))
    assert output["status"] == "failed"
    assert output["error"]["error_type"] in {"schema_linking_error", "extraction_error"}
    assert output["sql"] == ""


def test_n08_non_mysql_dialect_functions_are_rejected(catalog):
    result = validate_sql_text(
        "SELECT DATE_TRUNC('day', register_date) AS d, COUNT(user_id) FROM dim_user GROUP BY d LIMIT 10",
        catalog,
        [{"table": "dim_user", "columns": ["user_id", "register_date"]}],
    )
    assert not result.ok
    assert result.error["error_type"] == "sql_policy_error"
    assert "方言" in result.error["message"]


def test_n08_dialect_violation_never_reaches_the_database(
    settings, catalog, executor, scripted_llm
):
    llm = scripted_llm(
        json_payload={
            "rewrite_question": "按注册月份统计用户数",
            "keywords": ["注册日期"],
            "dimensions": ["注册日期"],
            "metrics": ["用户数"],
        },
        sql_script=[
            "SELECT DATE_TRUNC('month', u.register_date) AS m, COUNT(u.user_id) "
            "FROM dim_user AS u GROUP BY m LIMIT 10"
        ],
    )
    calls_before = 0
    deps = NodeDeps(settings=settings, catalog=catalog, llm=llm, executor=executor)
    output = run_query("按注册月份统计用户数", deps=deps, graph=build_graph(deps))
    assert output["status"] == "failed"
    assert output["error"]["error_type"] == "sql_policy_error"
    assert llm.sql_calls == 1
    _ = calls_before


# ------------------------------------------------------------------- retry R01-R05
class RecordingExecutor:
    """Executor double that records how many times SQL was really executed."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    def execute(self, sql: str, *, max_rows: int | None = None) -> QueryResult:
        self.calls.append(sql)
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        return None


VALID_SQL = "SELECT u.channel, COUNT(u.user_id) AS users FROM dim_user AS u GROUP BY u.channel LIMIT 100"
PAYLOAD = {
    "rewrite_question": "按渠道统计用户数",
    "keywords": ["渠道"],
    "dimensions": ["渠道"],
    "metrics": ["用户数"],
}


def _rows(count: int = 2) -> QueryResult:
    rows = [{"channel": f"c{i}", "users": i} for i in range(count)]
    return QueryResult(columns=["channel", "users"], rows=rows, row_count=count, elapsed_ms=1)


def test_r01_unknown_column_is_retried_and_capped(settings, catalog, scripted_llm):
    llm = scripted_llm(json_payload=PAYLOAD, sql_script=[VALID_SQL])
    executor = RecordingExecutor(
        [pymysql.err.ProgrammingError(1054, "Unknown column 'u.gender'")] * 5
    )
    graph_state = build_graph(NodeDeps(settings=settings, catalog=catalog, llm=llm, executor=executor))
    state = graph_state.invoke(
        {
            "question": "按渠道统计用户数",
            "messages": [],
            "retry_count": 0,
            "previous_sql_errors": [],
            "status": "pending",
            "error": None,
        }
    )
    assert state["status"] == "failed"
    assert state["error"]["error_type"] == RETRY_EXHAUSTED
    assert len([e for e in state["previous_sql_errors"]]) == settings.sql_max_retries
    assert len(executor.calls) == settings.sql_max_retries + 1
    assert state["retry_count"] == settings.sql_max_retries


def test_r02_connection_failure_is_never_retried(settings, catalog, scripted_llm):
    llm = scripted_llm(json_payload=PAYLOAD, sql_script=[VALID_SQL])
    executor = RecordingExecutor(
        [pymysql.err.OperationalError(2003, "Can't connect to MySQL server on '127.0.0.1'")]
    )
    deps = NodeDeps(settings=settings, catalog=catalog, llm=llm, executor=executor)
    output = run_query("按渠道统计用户数", deps=deps, graph=build_graph(deps))
    assert output["status"] == "failed"
    assert output["error"]["error_type"] == "connection_error"
    assert output["error"]["recovery_strategy"] == "surface_to_user"
    assert len(executor.calls) == 1
    assert llm.sql_calls == 1


def test_r03_timeout_is_retried_once_then_surfaced(settings, catalog, scripted_llm):
    llm = scripted_llm(json_payload=PAYLOAD, sql_script=[VALID_SQL])
    executor = RecordingExecutor(
        [pymysql.err.OperationalError(3024, "Query execution was interrupted, maximum statement execution time exceeded")]
    )
    deps = NodeDeps(settings=settings, catalog=catalog, llm=llm, executor=executor)
    output = run_query("按渠道统计用户数", deps=deps, graph=build_graph(deps))
    assert output["status"] == "failed"
    assert output["error"]["error_type"] == "timeout"
    assert output["error"]["recovery_strategy"] == "surface_to_user"
    assert len(executor.calls) == 2, "超时只允许重试一次"


def test_r04_validation_rejection_never_regenerates(settings, catalog, scripted_llm):
    llm = scripted_llm(json_payload=PAYLOAD, sql_script=["DELETE FROM dim_user"])
    executor = RecordingExecutor([_rows()])
    deps = NodeDeps(settings=settings, catalog=catalog, llm=llm, executor=executor)
    output = run_query("按渠道统计用户数", deps=deps, graph=build_graph(deps))
    assert output["status"] == "failed"
    assert output["error"]["recovery_strategy"] == "abort"
    assert llm.sql_calls == 1
    assert executor.calls == []


def test_r05_third_failure_returns_retry_exhausted(settings, catalog, scripted_llm):
    llm = scripted_llm(json_payload=PAYLOAD, sql_script=[VALID_SQL])
    executor = RecordingExecutor(
        [pymysql.err.ProgrammingError(1146, "Table 'x' doesn't exist")] * 5
    )
    deps = NodeDeps(settings=settings, catalog=catalog, llm=llm, executor=executor)
    output = run_query("按渠道统计用户数", deps=deps, graph=build_graph(deps))
    assert output["status"] == "failed"
    assert output["error"]["error_type"] == RETRY_EXHAUSTED
    assert output["error"]["recovery_strategy"] == "abort"
    assert len(executor.calls) == settings.sql_max_retries + 1


def test_r05_retry_count_is_monotonic(settings, catalog, scripted_llm):
    llm = scripted_llm(json_payload=PAYLOAD, sql_script=[VALID_SQL])
    executor = RecordingExecutor(
        [
            pymysql.err.ProgrammingError(1054, "Unknown column 'a'"),
            pymysql.err.ProgrammingError(1054, "Unknown column 'b'"),
            _rows(3),
        ]
    )
    deps = NodeDeps(settings=settings, catalog=catalog, llm=llm, executor=executor)
    graph_state = build_graph(deps)
    state = graph_state.invoke(
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
    assert state["retry_count"] == 2
    assert [error["attempt"] for error in state["previous_sql_errors"]] == [0, 1]
    assert state["row_count"] == 3


# ------------------------------------------------- end to end acceptance criteria
def test_e2e_criteria_secrets_never_appear_in_state(settings, catalog, executor, offline_llm):
    deps = NodeDeps(settings=settings, catalog=catalog, llm=offline_llm, executor=executor)
    output = run_query("按渠道统计用户数", deps=deps, graph=build_graph(deps))
    serialized = json.dumps(output, ensure_ascii=False, default=str)
    assert settings.db_password not in serialized
    assert settings.llm_api_key not in serialized
    assert "mysql+pymysql" not in serialized


def test_e2e_criteria_catalog_version_change_is_detected(settings, catalog):
    from dataclasses import replace

    from text2sql.catalog import CatalogConsistencyError
    from text2sql.catalog import load_catalog as load

    with pytest.raises(CatalogConsistencyError):
        load(replace(settings, catalog_version="v999"))


def test_e2e_criteria_sample_and_full_share_the_same_contract(settings, catalog, executor):
    """Row counts may differ between sample/full data; schema and Catalog may not."""
    live = executor.live_columns(list(WHITELIST))
    catalog.verify_live(live)
    counts = {}
    for table in WHITELIST:
        result = executor.execute(f"SELECT COUNT(*) AS c FROM {table}")
        counts[table] = result.rows[0]["c"]
    assert set(counts) == WHITELIST
    assert all(isinstance(value, int) for value in counts.values())
