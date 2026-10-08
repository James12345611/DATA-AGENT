"""Node contract tests (text2sql_V1.md section 5)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pymysql
import pytest

from text2sql.catalog import Catalog
from text2sql.config import Settings
from text2sql.db.executor import QueryResult
from text2sql.llm import LLMError
from text2sql.nodes.deps import NodeDeps
from text2sql.nodes.execute_sql import execute_sql
from text2sql.nodes.generate_sql import generate_sql
from text2sql.nodes.information_extraction import information_extraction
from text2sql.nodes.regenerate_sql import regenerate_sql
from text2sql.nodes.schema_linking import schema_linking
from text2sql.nodes.validate_sql import validate_sql
from text2sql.offline_llm import DeterministicLLMClient
from text2sql.state import new_state, normalize_entities


class FakeExecutor:
    """Executor double: returns canned results or raises canned exceptions."""

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


def make_deps(
    settings: Settings,
    catalog: Catalog,
    llm,
    executor,
) -> NodeDeps:
    return NodeDeps(settings=settings, catalog=catalog, llm=llm, executor=executor)  # type: ignore[arg-type]


def entities(**kwargs):
    payload = {
        "keywords": kwargs.get("keywords", []),
        "dimensions": kwargs.get("dimensions", []),
        "metrics": kwargs.get("metrics", []),
        "filters": kwargs.get("filters", []),
        "start_time": kwargs.get("start_time"),
        "end_time": kwargs.get("end_time"),
        "timezone": kwargs.get("timezone"),
    }
    return normalize_entities(payload)


# --------------------------------------------------------- information_extraction
def test_extraction_rejects_empty_question(settings, catalog, executor):
    deps = make_deps(settings, catalog, DeterministicLLMClient(settings, catalog), executor)
    result = information_extraction({"question": "  ", "messages": []}, deps=deps)
    assert result["status"] == "failed"
    assert result["error"]["error_type"] == "extraction_error"
    assert result["error"]["recovery_strategy"] == "surface_to_user"


def test_extraction_rejects_destructive_request_before_the_llm(settings, catalog, executor):
    class ExplodingLLM(DeterministicLLMClient):
        def complete_json(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("破坏性请求不应该调用模型")

    deps = make_deps(settings, catalog, ExplodingLLM(settings, catalog), executor)
    result = information_extraction({"question": "删除所有订单", "messages": []}, deps=deps)
    assert result["status"] == "failed"
    assert result["error"]["error_type"] == "unsafe_request"
    assert result["error"]["recovery_strategy"] == "abort"


def test_extraction_success_writes_only_its_own_fields(settings, catalog, executor, scripted_llm):
    llm = scripted_llm(
        json_payload={
            "rewrite_question": "按渠道统计用户数",
            "keywords": ["渠道"],
            "dimensions": ["渠道"],
            "metrics": ["用户数"],
            "filters": [],
            "start_time": None,
            "end_time": None,
            "timezone": None,
        }
    )
    deps = make_deps(settings, catalog, llm, executor)
    result = information_extraction({"question": "按渠道统计用户数", "messages": []}, deps=deps)
    assert set(result) == {"rewrite_question", "info_entities", "status", "error"}
    assert result["status"] == "pending" and result["error"] is None
    assert result["rewrite_question"] == "按渠道统计用户数"
    assert result["info_entities"]["metrics"] == ["用户数"]


def test_extraction_marks_unsafe_request_from_model(settings, catalog, executor, scripted_llm):
    llm = scripted_llm(json_payload={"unsafe_request": True})
    deps = make_deps(settings, catalog, llm, executor)
    result = information_extraction({"question": "帮我把订单表更新一下", "messages": []}, deps=deps)
    assert result["status"] == "failed"
    assert result["error"]["error_type"] == "unsafe_request"
    assert result["error"]["recovery_strategy"] == "abort"


def test_extraction_fails_when_no_terms_found(settings, catalog, executor, scripted_llm):
    llm = scripted_llm(json_payload={"rewrite_question": "今天天气", "keywords": []})
    deps = make_deps(settings, catalog, llm, executor)
    result = information_extraction({"question": "今天天气怎么样", "messages": []}, deps=deps)
    assert result["status"] == "failed"
    assert result["error"]["error_type"] == "extraction_error"


def test_extraction_maps_llm_failure_to_llm_error(settings, catalog, executor, scripted_llm):
    llm = scripted_llm(json_payload={}, sql_error=LLMError("接口超时"))
    llm.complete_json = lambda *a, **k: (_ for _ in ()).throw(LLMError("接口超时"))  # type: ignore[assignment]
    deps = make_deps(settings, catalog, llm, executor)
    result = information_extraction({"question": "按渠道统计用户数", "messages": []}, deps=deps)
    assert result["status"] == "failed"
    assert result["error"]["error_type"] == "llm_error"


# ------------------------------------------------------------------ schema_linking
def test_schema_linking_requires_prerequisites(settings, catalog, executor):
    deps = make_deps(settings, catalog, DeterministicLLMClient(settings, catalog), executor)
    result = schema_linking({"question": "q"}, deps=deps)
    assert result["status"] == "failed"
    assert result["error"]["error_type"] == "schema_linking_error"


def test_schema_linking_never_invents_product_columns(settings, catalog, executor):
    deps = make_deps(settings, catalog, DeterministicLLMClient(settings, catalog), executor)
    state = {
        "question": "统计各商品的名称和品牌",
        "rewrite_question": "统计各商品的名称和品牌",
        "info_entities": entities(dimensions=["品牌"], metrics=["商品名称"]),
        "status": "pending",
    }
    result = schema_linking(state, deps=deps)
    assert result["status"] == "failed"
    assert result["error"]["error_type"] == "schema_linking_error"
    assert "商品" in result["error"]["message"] or "品牌" in result["error"]["message"]


def test_schema_linking_errors_when_nothing_matches(settings, catalog, executor):
    deps = make_deps(settings, catalog, DeterministicLLMClient(settings, catalog), executor)
    state = {
        "question": "帮我看看今天天气",
        "rewrite_question": "帮我看看今天天气",
        "info_entities": entities(keywords=["天气"]),
        "status": "pending",
    }
    result = schema_linking(state, deps=deps)
    assert result["status"] == "failed"
    assert result["error"]["error_type"] == "schema_linking_error"


def test_schema_linking_selects_three_user_grain_tables(settings, catalog, executor):
    deps = make_deps(settings, catalog, DeterministicLLMClient(settings, catalog), executor)
    state = {
        "question": "同时查看用户行为次数和净消费金额",
        "rewrite_question": "同时查看用户行为次数和净消费金额",
        "info_entities": entities(metrics=["行为次数", "净消费金额"]),
        "status": "pending",
    }
    result = schema_linking(state, deps=deps)
    assert result["status"] == "pending" and result["error"] is None
    tables = {selection["table"] for selection in result["selected_tables"]}
    assert tables == {
        "dim_user",
        "mart_user_behavior_summary",
        "mart_user_order_summary",
    }
    candidate_tables = {selection["table"] for selection in result["candidate_tables"]}
    assert tables <= candidate_tables
    for selection in result["selected_tables"]:
        assert selection["columns"]


def test_schema_linking_rejects_mixed_self_contained_and_user_tables(settings, catalog, executor):
    deps = make_deps(settings, catalog, DeterministicLLMClient(settings, catalog), executor)
    state = {
        "question": "各渠道用户的各行为阶段去重用户数",
        "rewrite_question": "各渠道用户的各行为阶段去重用户数",
        "info_entities": entities(dimensions=["渠道", "行为阶段"], metrics=["去重用户数"]),
        "status": "pending",
    }
    result = schema_linking(state, deps=deps)
    assert result["status"] == "failed"
    assert "不允许关联的表组合" in result["error"]["message"]


def test_schema_linking_rejects_time_filter_on_timeless_summary(settings, catalog, executor):
    deps = make_deps(settings, catalog, DeterministicLLMClient(settings, catalog), executor)
    state = {
        "question": "近 30 天各行为阶段的去重用户数",
        "rewrite_question": "近 30 天各行为阶段的去重用户数",
        "info_entities": entities(
            dimensions=["行为阶段"],
            metrics=["去重用户数"],
            start_time="2025-06-01",
            end_time="2025-06-30",
        ),
        "status": "pending",
    }
    result = schema_linking(state, deps=deps)
    assert result["status"] == "failed"
    assert "时间字段" in result["error"]["message"]


def test_schema_linking_adds_ratio_numerator_and_denominator(settings, catalog, executor):
    deps = make_deps(settings, catalog, DeterministicLLMClient(settings, catalog), executor)
    state = {
        "question": "点击后的转化率",
        "rewrite_question": "点击后的转化率",
        "info_entities": entities(metrics=["点击后转化率"]),
        "status": "pending",
    }
    result = schema_linking(state, deps=deps)
    columns = {c for selection in result["selected_tables"] for c in selection["columns"]}
    assert {"converted_count", "clicked_count"} <= columns


# -------------------------------------------------------------------- generate_sql
def test_generate_sql_requires_prerequisites(settings, catalog, executor):
    deps = make_deps(settings, catalog, DeterministicLLMClient(settings, catalog), executor)
    result = generate_sql({"question": "q", "status": "pending"}, deps=deps)
    assert result["status"] == "failed"
    assert result["error"]["error_type"] == "sql_generation_error"


def test_generate_sql_writes_sql_and_keeps_retry_count(settings, catalog, executor, scripted_llm):
    llm = scripted_llm(sql_script=["SELECT u.channel FROM dim_user AS u LIMIT 10"])
    deps = make_deps(settings, catalog, llm, executor)
    state = {
        "question": "按渠道统计用户数",
        "rewrite_question": "按渠道统计用户数",
        "info_entities": entities(dimensions=["渠道"], metrics=["用户数"]),
        "selected_tables": [{"table": "dim_user", "columns": ["user_id", "channel"]}],
        "retry_count": 0,
        "previous_sql_errors": [],
        "status": "pending",
    }
    result = generate_sql(state, deps=deps)
    assert set(result) == {"sql", "retry_count", "status", "error"}
    assert result["sql"].startswith("SELECT")
    assert result["retry_count"] == 0
    assert result["status"] == "pending"


def test_generate_sql_reports_llm_failure(settings, catalog, executor, scripted_llm):
    llm = scripted_llm(sql_error=LLMError("模型不可用"))
    deps = make_deps(settings, catalog, llm, executor)
    state = {
        "question": "q",
        "rewrite_question": "q",
        "info_entities": entities(metrics=["用户数"]),
        "selected_tables": [{"table": "dim_user", "columns": ["user_id"]}],
        "status": "pending",
    }
    result = generate_sql(state, deps=deps)
    assert result["status"] == "failed"
    assert result["error"]["error_type"] == "llm_error"


# --------------------------------------------------------------------- validate_sql
def test_validate_sql_requires_sql_and_selection(settings, catalog, executor):
    deps = make_deps(settings, catalog, DeterministicLLMClient(settings, catalog), executor)
    assert validate_sql({"status": "pending"}, deps=deps)["status"] == "failed"
    assert (
        validate_sql({"sql": "SELECT 1", "status": "pending"}, deps=deps)["status"] == "failed"
    )


def test_validate_sql_passes_valid_statement(settings, catalog, executor):
    deps = make_deps(settings, catalog, DeterministicLLMClient(settings, catalog), executor)
    state = {
        "sql": "SELECT u.channel, COUNT(u.user_id) FROM dim_user AS u GROUP BY u.channel LIMIT 10",
        "selected_tables": [{"table": "dim_user", "columns": ["user_id", "channel"]}],
        "status": "pending",
        "retry_count": 0,
    }
    result = validate_sql(state, deps=deps)
    assert result == {"status": "pending", "error": None}


def test_validate_sql_normalizes_limit(settings, catalog, executor):
    deps = make_deps(settings, catalog, DeterministicLLMClient(settings, catalog), executor)
    state = {
        "sql": "SELECT u.channel FROM dim_user AS u",
        "selected_tables": [{"table": "dim_user", "columns": ["user_id", "channel"]}],
        "status": "pending",
    }
    result = validate_sql(state, deps=deps)
    assert "LIMIT 100" in result["sql"].upper()
    assert result["status"] == "pending"


def test_validate_sql_aborts_on_dangerous_sql(settings, catalog, executor):
    deps = make_deps(settings, catalog, DeterministicLLMClient(settings, catalog), executor)
    state = {
        "sql": "DROP TABLE dim_user",
        "selected_tables": [{"table": "dim_user", "columns": ["user_id"]}],
        "status": "pending",
        "retry_count": 0,
    }
    result = validate_sql(state, deps=deps)
    assert result["status"] == "failed"
    assert result["error"]["recovery_strategy"] == "abort"
    assert result["error"]["attempt"] == 0


# ---------------------------------------------------------------------- execute_sql
def _result(rows: list[dict[str, Any]], columns: list[str]) -> QueryResult:
    return QueryResult(columns=columns, rows=rows, row_count=len(rows), elapsed_ms=3)


def test_execute_sql_success_and_empty_result(settings, catalog, scripted_llm):
    executor = FakeExecutor([_result([{"channel": "ads", "users": 3}], ["channel", "users"])])
    deps = make_deps(settings, catalog, scripted_llm(json_payload={}), executor)
    result = execute_sql({"sql": "SELECT 1 LIMIT 1", "retry_count": 0}, deps=deps)
    assert result["status"] == "success"
    assert result["row_count"] == 1
    assert result["result_columns"] == ["channel", "users"]
    assert result["error"] is None

    empty = FakeExecutor([_result([], ["channel"])])
    deps_empty = make_deps(settings, catalog, scripted_llm(json_payload={}), empty)
    result_empty = execute_sql({"sql": "SELECT 1 LIMIT 1", "retry_count": 0}, deps=deps_empty)
    assert result_empty["status"] == "success"
    assert result_empty["row_count"] == 0


def test_execute_sql_classifies_retryable_error(settings, catalog, scripted_llm):
    error = pymysql.err.ProgrammingError(1054, "Unknown column 'x' in 'field list'")
    deps = make_deps(settings, catalog, scripted_llm(json_payload={}), FakeExecutor([error]))
    result = execute_sql({"sql": "SELECT x FROM dim_user LIMIT 1", "retry_count": 0}, deps=deps)
    assert result["status"] == "pending"
    assert result["error"]["error_type"] == "unknown_column"
    assert result["error"]["recovery_strategy"] == "retry"
    assert result["previous_sql_errors"][-1]["error_type"] == "unknown_column"


def test_execute_sql_classifies_terminal_errors(settings, catalog, scripted_llm):
    connection = pymysql.err.OperationalError(2003, "Can't connect to MySQL server")
    deps = make_deps(settings, catalog, scripted_llm(json_payload={}), FakeExecutor([connection]))
    result = execute_sql({"sql": "SELECT 1 LIMIT 1", "retry_count": 0}, deps=deps)
    assert result["status"] == "failed"
    assert result["error"]["error_type"] == "connection_error"
    assert result["error"]["recovery_strategy"] == "surface_to_user"


def test_execute_sql_resource_error_aborts(settings, catalog, scripted_llm):
    resource = pymysql.err.InternalError(1038, "Out of sort memory")
    deps = make_deps(settings, catalog, scripted_llm(json_payload={}), FakeExecutor([resource]))
    result = execute_sql({"sql": "SELECT 1 LIMIT 1", "retry_count": 0}, deps=deps)
    assert result["status"] == "failed"
    assert result["error"]["recovery_strategy"] == "abort"


def test_execute_sql_retry_exhaustion(settings, catalog, scripted_llm):
    error = pymysql.err.ProgrammingError(1054, "Unknown column 'x'")
    deps = make_deps(settings, catalog, scripted_llm(json_payload={}), FakeExecutor([error]))
    result = execute_sql(
        {"sql": "SELECT x FROM dim_user LIMIT 1", "retry_count": settings.sql_max_retries},
        deps=deps,
    )
    assert result["status"] == "failed"
    assert result["error"]["error_type"] == "retry_exhausted"
    assert result["error"]["recovery_strategy"] == "abort"


def test_execute_sql_timeout_surfaces_after_one_retry(settings, catalog, scripted_llm):
    timeout = pymysql.err.OperationalError(3024, "Query execution was interrupted")
    deps = make_deps(settings, catalog, scripted_llm(json_payload={}), FakeExecutor([timeout]))
    first = execute_sql({"sql": "SELECT 1 LIMIT 1", "retry_count": 0}, deps=deps)
    assert first["error"]["error_type"] == "timeout"
    assert first["error"]["recovery_strategy"] == "retry"

    second = execute_sql({"sql": "SELECT 1 LIMIT 1", "retry_count": 1}, deps=deps)
    assert second["status"] == "failed"
    assert second["error"]["recovery_strategy"] == "surface_to_user"


def test_execute_sql_never_leaks_password(settings, catalog, scripted_llm):
    settings = settings.with_overrides(db_password="topsecret")
    error = pymysql.err.OperationalError(
        2003, "connect failed for mysql+pymysql://user:topsecret@host/db"
    )
    deps = make_deps(settings, catalog, scripted_llm(json_payload={}), FakeExecutor([error]))
    result = execute_sql({"sql": "SELECT 1 LIMIT 1", "retry_count": 0}, deps=deps)
    assert "topsecret" not in result["error"]["message"]


def test_execute_sql_requires_sql(settings, catalog, executor):
    deps = make_deps(settings, catalog, DeterministicLLMClient(settings, catalog), executor)
    result = execute_sql({"retry_count": 0}, deps=deps)
    assert result["status"] == "failed"


# ------------------------------------------------------------------- regenerate_sql
def _generation_state(**overrides):
    state = {
        "question": "按渠道统计用户数",
        "rewrite_question": "按渠道统计用户数",
        "info_entities": entities(dimensions=["渠道"], metrics=["用户数"]),
        "selected_tables": [{"table": "dim_user", "columns": ["user_id", "channel"]}],
        "sql": "SELECT u.channel FROM dim_user AS u LIMIT 10",
        "retry_count": 0,
        "previous_sql_errors": [
            {
                "error_type": "unknown_column",
                "message": "字段不存在：dim_user.gender",
                "recovery_strategy": "retry",
                "attempt": 0,
            }
        ],
        "status": "pending",
    }
    state.update(overrides)
    return state


def test_regenerate_sql_increments_retry_count(settings, catalog, executor, scripted_llm):
    llm = scripted_llm(sql_script=["SELECT u.channel, COUNT(u.user_id) FROM dim_user AS u GROUP BY u.channel LIMIT 10"])
    deps = make_deps(settings, catalog, llm, executor)
    result = regenerate_sql(_generation_state(), deps=deps)
    assert result["retry_count"] == 1
    assert result["status"] == "pending"
    assert result["error"] is None
    assert result["sql"].startswith("SELECT")


def test_regenerate_sql_reports_exhaustion(settings, catalog, executor, scripted_llm):
    llm = scripted_llm(sql_script=["SELECT 1 LIMIT 1"])
    deps = make_deps(settings, catalog, llm, executor)
    result = regenerate_sql(
        _generation_state(retry_count=settings.sql_max_retries), deps=deps
    )
    assert result["status"] == "failed"
    assert result["error"]["error_type"] == "retry_exhausted"
    assert result["error"]["recovery_strategy"] == "abort"


def test_regenerate_sql_prompt_contains_previous_errors(settings, catalog, executor, scripted_llm):
    llm = scripted_llm(sql_script=["SELECT u.channel FROM dim_user AS u LIMIT 10"])
    deps = make_deps(settings, catalog, llm, executor)
    regenerate_sql(_generation_state(), deps=deps)
    assert llm.prompts, "regenerate_sql 必须调用模型"
    assert "必须修正的错误" in llm.prompts[-1].user
    assert "dim_user.gender" in llm.prompts[-1].user


def test_regenerate_sql_requires_prerequisites(settings, catalog, executor, scripted_llm):
    llm = scripted_llm(sql_script=["SELECT 1 LIMIT 1"])
    deps = make_deps(settings, catalog, llm, executor)
    result = regenerate_sql({"status": "pending", "retry_count": 0}, deps=deps)
    assert result["status"] == "failed"
    assert result["error"]["recovery_strategy"] == "abort"
