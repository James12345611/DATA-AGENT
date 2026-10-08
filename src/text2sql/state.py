"""State contract (text2sql_V1.md section 4).

These TypedDicts are the *only* state contract of V1.  Field names must not be
renamed; nodes return only the fields they own and must not hide missing state
behind empty strings.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from typing_extensions import NotRequired, TypedDict

RecoveryStrategy = Literal["retry", "retry_with_new_table", "surface_to_user", "abort"]
Status = Literal["pending", "success", "failed"]
FilterOperator = Literal[
    "eq",
    "neq",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "in",
    "between",
]

FILTER_OPERATORS: tuple[str, ...] = (
    "eq",
    "neq",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "in",
    "between",
)


class FilterCondition(TypedDict):
    field_hint: str
    operator: FilterOperator
    value: Any


class ExtractedEntities(TypedDict):
    keywords: list[str]
    dimensions: list[str]
    metrics: list[str]
    filters: list[FilterCondition]
    start_time: str | None
    end_time: str | None
    timezone: str | None


class TableSelection(TypedDict):
    table: str
    columns: list[str]
    reasoning: NotRequired[str]


class SQLError(TypedDict):
    error_type: str
    message: str
    recovery_strategy: RecoveryStrategy
    sql: NotRequired[str]
    attempt: NotRequired[int]


class SQLInputState(TypedDict):
    question: str
    messages: list[Any]


class SQLGraphState(TypedDict, total=False):
    question: str
    messages: list[Any]
    rewrite_question: str
    info_entities: ExtractedEntities
    candidate_tables: list[TableSelection]
    selected_tables: list[TableSelection]
    sql: str
    retry_count: int
    previous_sql_errors: list[SQLError]
    result_rows: list[dict[str, Any]]
    result_columns: list[str]
    row_count: int
    status: Status
    error: SQLError | None


class SQLOutputState(TypedDict):
    question: str
    rewrite_question: str
    selected_tables: list[TableSelection]
    sql: str
    result_rows: list[dict[str, Any]]
    result_columns: list[str]
    row_count: int
    status: Status
    error: SQLError | None


# --------------------------------------------------------------------- helpers


class StateContractError(ValueError):
    """Raised when a state payload violates the contract."""


def dedupe(values: list[str]) -> list[str]:
    """De-duplicate strings while preserving order (section 5.3)."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def validate_input_state(state: SQLGraphState | SQLInputState) -> str:
    """Validate ``SQLInputState`` and return the stripped question.

    ``question`` must be a non-empty string.  ``messages`` may be empty.
    """
    question = state.get("question")
    if not isinstance(question, str) or not question.strip():
        raise StateContractError("question must be a non-empty string")
    messages = state.get("messages", [])
    if messages is None:
        raise StateContractError("messages must be a list (use [] when empty)")
    if not isinstance(messages, list):
        raise StateContractError("messages must be a list")
    if "sql" in state and state["sql"] not in (None, ""):
        raise StateContractError("input state must not carry an unvalidated SQL")
    return question.strip()


def validate_filter_condition(raw: Any) -> FilterCondition:
    """Validate one filter condition (section 4.4 value-shape rules)."""
    if not isinstance(raw, dict):
        raise StateContractError(f"filter must be an object, got {type(raw).__name__}")
    field_hint = raw.get("field_hint")
    operator = raw.get("operator")
    if not isinstance(field_hint, str) or not field_hint.strip():
        raise StateContractError("filter.field_hint must be a non-empty string")
    if operator not in FILTER_OPERATORS:
        raise StateContractError(f"unsupported filter operator: {operator!r}")
    value = raw.get("value")
    if operator == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise StateContractError("filter 'between' requires a 2 element list")
        value = list(value)
    elif operator == "in":
        if not isinstance(value, (list, tuple)) or len(value) == 0:
            raise StateContractError("filter 'in' requires a non-empty list")
        value = list(value)
    else:
        if isinstance(value, (list, tuple, dict)):
            raise StateContractError(
                f"filter operator {operator!r} requires a single scalar value"
            )
    return FilterCondition(field_hint=field_hint.strip(), operator=operator, value=value)


def normalize_entities(raw: Any) -> ExtractedEntities:
    """Coerce an LLM payload into a validated :class:`ExtractedEntities`."""
    if not isinstance(raw, dict):
        raise StateContractError("info_entities must be an object")

    def _str_list(key: str) -> list[str]:
        value = raw.get(key, [])
        if value in (None, ""):
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise StateContractError(f"info_entities.{key} must be a list of strings")
        return dedupe([str(item) for item in value])

    filters_raw = raw.get("filters") or []
    if isinstance(filters_raw, dict):
        filters_raw = [filters_raw]
    if not isinstance(filters_raw, (list, tuple)):
        raise StateContractError("info_entities.filters must be a list")

    def _time(key: str) -> str | None:
        value = raw.get(key)
        if value in (None, "", "null", "None"):
            return None
        if not isinstance(value, str):
            value = str(value)
        return value.strip() or None

    return ExtractedEntities(
        keywords=_str_list("keywords"),
        dimensions=_str_list("dimensions"),
        metrics=_str_list("metrics"),
        filters=[validate_filter_condition(item) for item in filters_raw],
        start_time=_time("start_time"),
        end_time=_time("end_time"),
        timezone=_time("timezone"),
    )


def validate_table_selection(raw: Any) -> TableSelection:
    """Validate one :class:`TableSelection` (``columns`` may not be empty)."""
    if not isinstance(raw, dict):
        raise StateContractError("table selection must be an object")
    table = raw.get("table")
    if not isinstance(table, str) or not table.strip():
        raise StateContractError("table selection requires a table name")
    columns = raw.get("columns")
    if not isinstance(columns, (list, tuple)) or not list(columns):
        raise StateContractError(f"table selection for {table!r} requires columns")
    selection = TableSelection(
        table=table.strip(), columns=dedupe([str(c) for c in columns])
    )
    reasoning = raw.get("reasoning")
    if reasoning:
        selection["reasoning"] = str(reasoning)
    return selection


def new_state(
    question: str,
    messages: list[Any] | None = None,
    **extra: Any,
) -> SQLGraphState:
    """Build a fresh :class:`SQLGraphState` for one question."""
    state: SQLGraphState = {
        "question": question,
        "messages": list(messages or []),
        "retry_count": 0,
        "previous_sql_errors": [],
        "status": "pending",
        "error": None,
    }
    state.update(extra)  # type: ignore[typeddict-item]
    return state


def project_output(state: SQLGraphState) -> SQLOutputState:
    """Project the internal graph state onto the public output contract.

    A successful run always carries ``result_rows`` / ``result_columns`` /
    ``row_count``; a failed run always carries ``status="failed"`` and ``error``.
    """
    if state.get("status") == "success":
        if "result_rows" not in state or "result_columns" not in state:
            raise StateContractError("successful state requires result_rows/result_columns")
        return SQLOutputState(
            question=state.get("question", ""),
            rewrite_question=state.get("rewrite_question", ""),
            selected_tables=list(state.get("selected_tables", [])),
            sql=state.get("sql", ""),
            result_rows=list(state.get("result_rows", [])),
            result_columns=list(state.get("result_columns", [])),
            row_count=int(state.get("row_count", len(state.get("result_rows", [])))),
            status="success",
            error=None,
        )
    error = state.get("error")
    if not error:
        raise StateContractError("failed state requires a normalized SQLError")
    return SQLOutputState(
        question=state.get("question", ""),
        rewrite_question=state.get("rewrite_question", ""),
        selected_tables=list(state.get("selected_tables", [])),
        sql=state.get("sql", ""),
        result_rows=[],
        result_columns=[],
        row_count=0,
        status="failed",
        error=error,
    )


def to_jsonable(value: Any) -> Any:
    """Make a state payload JSON serializable for logs / CLI ``--json``."""
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    content = getattr(value, "content", None)
    if content is not None:  # LangChain BaseMessage
        return {"type": type(value).__name__, "content": to_jsonable(content)}
    return str(value)


def state_to_json(state: SQLGraphState) -> str:
    return json.dumps(to_jsonable(state), ensure_ascii=False, indent=2, default=str)
