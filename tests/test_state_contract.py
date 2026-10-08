"""State contract tests (text2sql_V1.md section 4)."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from text2sql.nodes.deps import history_messages
from text2sql.state import (
    StateContractError,
    dedupe,
    new_state,
    normalize_entities,
    project_output,
    to_jsonable,
    validate_filter_condition,
    validate_input_state,
    validate_table_selection,
)


def test_input_state_requires_non_empty_question():
    with pytest.raises(StateContractError):
        validate_input_state({"question": "   ", "messages": []})
    with pytest.raises(StateContractError):
        validate_input_state({"messages": []})
    assert validate_input_state({"question": " 按渠道统计用户数 ", "messages": []}) == (
        "按渠道统计用户数"
    )


def test_input_state_rejects_unvalidated_sql():
    with pytest.raises(StateContractError):
        validate_input_state({"question": "q", "messages": [], "sql": "SELECT 1"})


def test_input_state_messages_must_be_list():
    with pytest.raises(StateContractError):
        validate_input_state({"question": "q", "messages": "not-a-list"})


@pytest.mark.parametrize(
    "condition",
    [
        {"field_hint": "渠道", "operator": "eq", "value": "ads"},
        {"field_hint": "年龄", "operator": "between", "value": [18, 30]},
        {"field_hint": "渠道", "operator": "in", "value": ["ads", "social"]},
    ],
)
def test_filter_condition_value_shapes(condition):
    assert validate_filter_condition(condition)["value"] == condition["value"]


@pytest.mark.parametrize(
    "condition",
    [
        {"field_hint": "年龄", "operator": "between", "value": [18]},
        {"field_hint": "渠道", "operator": "in", "value": []},
        {"field_hint": "渠道", "operator": "eq", "value": ["ads"]},
        {"field_hint": "", "operator": "eq", "value": "ads"},
        {"field_hint": "渠道", "operator": "like", "value": "ads"},
    ],
)
def test_filter_condition_rejects_bad_shapes(condition):
    with pytest.raises(StateContractError):
        validate_filter_condition(condition)


def test_normalize_entities_dedupes_and_validates():
    entities = normalize_entities(
        {
            "keywords": ["渠道", "渠道", " "],
            "dimensions": "渠道",
            "metrics": ["用户数", "用户数"],
            "filters": [{"field_hint": "是否复购用户", "operator": "eq", "value": 1}],
            "start_time": "2024-01-01",
            "end_time": "",
        }
    )
    assert entities["keywords"] == ["渠道"]
    assert entities["dimensions"] == ["渠道"]
    assert entities["metrics"] == ["用户数"]
    assert entities["start_time"] == "2024-01-01"
    assert entities["end_time"] is None
    assert entities["timezone"] is None


def test_table_selection_requires_columns():
    assert validate_table_selection({"table": "dim_user", "columns": ["channel"]})["table"] == (
        "dim_user"
    )
    with pytest.raises(StateContractError):
        validate_table_selection({"table": "dim_user", "columns": []})


def test_dedupe_preserves_order():
    assert dedupe(["b", "a", "b", "a", "c"]) == ["b", "a", "c"]


def test_new_state_starts_pending_without_sql():
    state = new_state("按渠道统计用户数")
    assert state["status"] == "pending"
    assert state["retry_count"] == 0
    assert state["previous_sql_errors"] == []
    assert "sql" not in state


def test_projection_success_and_failure_contracts():
    success = project_output(
        {
            "question": "q",
            "rewrite_question": "q",
            "selected_tables": [],
            "sql": "SELECT 1 LIMIT 1",
            "result_rows": [{"a": 1}],
            "result_columns": ["a"],
            "row_count": 1,
            "status": "success",
            "error": None,
        }
    )
    assert success["status"] == "success"
    assert success["row_count"] == 1 and success["error"] is None

    failure = project_output(
        {
            "question": "q",
            "status": "failed",
            "error": {
                "error_type": "unsafe_sql",
                "message": "禁止访问系统表",
                "recovery_strategy": "abort",
            },
        }
    )
    assert failure["status"] == "failed"
    assert failure["error"]["recovery_strategy"] == "abort"
    assert failure["result_rows"] == [] and failure["row_count"] == 0

    with pytest.raises(StateContractError):
        project_output({"question": "q", "status": "failed"})


def test_output_projection_requires_result_columns_on_success():
    with pytest.raises(StateContractError):
        project_output({"question": "q", "status": "success"})


def test_history_does_not_duplicate_current_question():
    messages = [
        HumanMessage(content="上一个问题"),
        AIMessage(content="上一个回答"),
        HumanMessage(content="按渠道统计用户数"),
    ]
    history = history_messages("按渠道统计用户数", messages)
    assert len(history) == 2
    assert history[-1].content == "上一个回答"

    # different wording: keep the whole history
    assert len(history_messages("新问题", messages)) == 3


def test_state_to_jsonable_handles_messages():
    payload = to_jsonable({"messages": [HumanMessage(content="你好")], "n": 1})
    assert payload["n"] == 1
    assert payload["messages"][0]["content"] == "你好"
