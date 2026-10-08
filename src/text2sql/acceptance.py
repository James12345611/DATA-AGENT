"""V1 acceptance cases (text2sql_V1.md section 8).

The case table is shared by ``tests/test_acceptance.py`` and
``scripts/run_acceptance.py`` so the automated suite and the generated report
check exactly the same expectations: intent, table selection, key columns,
safety rules and result shape - never literal SQL text equality.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .config import Settings
from .state import SQLOutputState

V1_WHITELIST: frozenset[str] = frozenset(
    {
        "dim_user",
        "mart_user_behavior_summary",
        "mart_user_order_summary",
        "mart_campaign_summary",
        "mart_funnel_summary",
        "mart_retention_cohort",
    }
)

FORBIDDEN_SQL_TOKENS: tuple[str, ...] = (
    "RAW_",
    "FACT_",
    "CATALOG_",
    "INFORMATION_SCHEMA",
    "PERFORMANCE_SCHEMA",
    "MYSQL.",
)

FORBIDDEN_SQL_KEYWORDS: tuple[str, ...] = (
    "INSERT ",
    "UPDATE ",
    "DELETE ",
    "DROP ",
    "ALTER ",
    "TRUNCATE ",
    "CREATE ",
    "GRANT ",
)


@dataclass(frozen=True)
class AcceptanceCase:
    """One positive acceptance case."""

    id: str
    question: str
    expected_tables: frozenset[str]
    required_columns: frozenset[str] = frozenset()
    sql_must_contain: tuple[str, ...] = ()
    forbidden_columns: frozenset[str] = frozenset()
    note: str = ""
    result_shape: str = "rows"
    extra: dict[str, Any] = field(default_factory=dict)


POSITIVE_CASES: tuple[AcceptanceCase, ...] = (
    AcceptanceCase(
        id="Q01",
        question="按渠道统计用户数",
        expected_tables=frozenset({"dim_user"}),
        required_columns=frozenset({"channel", "user_id"}),
        sql_must_contain=("GROUP BY", "CHANNEL"),
        note="GROUP BY channel，用户数按用户粒度统计",
    ),
    AcceptanceCase(
        id="Q02",
        question="各会员等级的用户数量",
        expected_tables=frozenset({"dim_user"}),
        required_columns=frozenset({"vip_level", "user_id"}),
        sql_must_contain=("VIP_LEVEL",),
        note="使用 vip_level 和用户计数",
    ),
    AcceptanceCase(
        id="Q03",
        question="行为次数最多的 10 个用户",
        expected_tables=frozenset({"mart_user_behavior_summary", "dim_user"}),
        required_columns=frozenset({"event_count"}),
        sql_must_contain=("EVENT_COUNT", "ORDER BY", "DESC", "LIMIT 10"),
        note="使用 event_count，降序，LIMIT 10",
    ),
    AcceptanceCase(
        id="Q04",
        question="各渠道的有效订单数和净消费金额",
        expected_tables=frozenset({"dim_user", "mart_user_order_summary"}),
        required_columns=frozenset({"channel", "valid_order_count", "net_amount"}),
        sql_must_contain=("SUM(", "USER_ID", "CHANNEL"),
        note="通过 user_id 关联，使用 SUM 聚合",
    ),
    AcceptanceCase(
        id="Q05",
        question="哪些用户是复购用户",
        expected_tables=frozenset({"mart_user_order_summary"}),
        required_columns=frozenset({"is_repeat_buyer"}),
        sql_must_contain=("IS_REPEAT_BUYER",),
        note="过滤 is_repeat_buyer = 1",
    ),
    AcceptanceCase(
        id="Q06",
        question="各活动的曝光量、点击率和转化率",
        expected_tables=frozenset({"mart_campaign_summary"}),
        required_columns=frozenset(
            {"campaign_id", "exposure_count", "click_rate", "conversion_rate"}
        ),
        sql_must_contain=("CAMPAIGN_ID", "EXPOSURE_COUNT"),
        note="直接使用活动汇总字段，比率按分子分母重算",
    ),
    AcceptanceCase(
        id="Q07",
        question="点击率最高的 5 个活动",
        expected_tables=frozenset({"mart_campaign_summary"}),
        required_columns=frozenset({"click_rate"}),
        sql_must_contain=("ORDER BY", "DESC", "LIMIT 5"),
        note="ORDER BY click_rate DESC LIMIT 5",
    ),
    AcceptanceCase(
        id="Q08",
        question="各行为阶段的去重用户数",
        expected_tables=frozenset({"mart_funnel_summary"}),
        required_columns=frozenset({"event_type", "unique_users"}),
        forbidden_columns=frozenset({"event_count"}),
        sql_must_contain=("UNIQUE_USERS", "EVENT_TYPE"),
        note="使用 unique_users，不能用 event_count 代替",
    ),
    AcceptanceCase(
        id="Q09",
        question="每个 cohort 的 7 日留存率",
        expected_tables=frozenset({"mart_retention_cohort"}),
        required_columns=frozenset({"cohort_date", "d7_rate"}),
        sql_must_contain=("D7_RATE", "COHORT_DATE"),
        note="使用 cohort_date、d7_rate",
    ),
    AcceptanceCase(
        id="Q10",
        question="同时查看用户行为次数和净消费金额",
        expected_tables=frozenset(
            {"dim_user", "mart_user_behavior_summary", "mart_user_order_summary"}
        ),
        required_columns=frozenset({"event_count", "net_amount"}),
        sql_must_contain=("EVENT_COUNT", "NET_AMOUNT"),
        note="三张用户粒度表按 user_id 关联",
    ),
)


def references_column(sql: str, column: str) -> bool:
    """True when the statement *reads* a column with that name.

    Output aliases are not column references, so
    ``... AS click_conversion_rate`` does not count as using
    ``conversion_rate`` (needed for the N04 check).
    """
    try:
        import sqlglot
        from sqlglot import exp

        expression = sqlglot.parse_one(sql, read="mysql")
    except Exception:  # noqa: BLE001 - unparsable SQL is handled elsewhere
        return False
    return any(
        isinstance(node, exp.Column) and node.name.lower() == column.lower()
        for node in expression.find_all(exp.Column)
    )


def assert_safe_sql(sql: str) -> list[str]:
    """Return the list of safety violations of a generated statement."""
    failures: list[str] = []
    text = sql or ""
    upper = text.upper()
    if not upper.lstrip().startswith(("SELECT", "WITH")):
        failures.append("SQL 不是只读 SELECT/WITH 语句")
    if "LIMIT" not in upper:
        failures.append("SQL 缺少 LIMIT")
    if "SELECT*" in upper.replace(" ", ""):
        failures.append("SQL 使用了 SELECT *")
    for token in FORBIDDEN_SQL_TOKENS:
        if token in upper:
            failures.append(f"SQL 引用了未暴露对象：{token}")
    for keyword in FORBIDDEN_SQL_KEYWORDS:
        if keyword in upper and not upper.lstrip().startswith("SELECT"):
            failures.append(f"SQL 包含写操作关键字：{keyword.strip()}")
    stripped = text.strip().rstrip(";")
    if ";" in stripped:
        failures.append("SQL 包含多条语句")
    return failures


def evaluate_case(
    case: AcceptanceCase,
    output: SQLOutputState,
    settings: Settings,
) -> list[str]:
    """Check one graph output against its acceptance case."""
    failures: list[str] = []

    if output.get("status") != "success":
        error = output.get("error") or {}
        return [f"未返回成功状态：{error.get('error_type')} {error.get('message')}"]

    selected = {selection["table"] for selection in output.get("selected_tables", [])}
    missing_tables = set(case.expected_tables) - selected
    if missing_tables:
        failures.append(f"缺少期望表：{sorted(missing_tables)}（实际 {sorted(selected)}）")
    unexpected = selected - V1_WHITELIST
    if unexpected:
        failures.append(f"选择了白名单之外的表：{sorted(unexpected)}")

    columns = {
        column for selection in output.get("selected_tables", []) for column in selection["columns"]
    }
    missing_columns = set(case.required_columns) - columns
    if missing_columns:
        failures.append(f"缺少关键字段：{sorted(missing_columns)}")
    if case.forbidden_columns & columns:
        failures.append(f"使用了禁止字段：{sorted(case.forbidden_columns & columns)}")

    sql_upper = (output.get("sql") or "").upper()
    for token in case.sql_must_contain:
        if token.upper() not in sql_upper:
            failures.append(f"SQL 缺少 {token}")
    failures.extend(assert_safe_sql(output.get("sql") or ""))

    if output["row_count"] != len(output.get("result_rows", [])):
        failures.append("row_count 与 result_rows 长度不一致")
    if output["row_count"] > settings.sql_max_rows:
        failures.append("返回行数超过 SQL_MAX_ROWS")
    if output.get("result_rows") and set(output["result_rows"][0]) != set(
        output.get("result_columns", [])
    ):
        failures.append("result_columns 与实际行字段不一致")
    return failures


def summarize(outputs: Sequence[tuple[AcceptanceCase, SQLOutputState, list[str]]]) -> str:
    lines = [
        "| 编号 | 问题 | 状态 | 选中表 | 行数 | 结果 |",
        "| --- | --- | --- | --- | ---: | --- |",
    ]
    for case, output, failures in outputs:
        tables = (
            "、".join(sorted({s["table"] for s in output.get("selected_tables", [])})) or "-"
        )
        status = "PASS" if not failures else "FAIL"
        lines.append(
            f"| {case.id} | {case.question} | {output.get('status')} | {tables} | "
            f"{output.get('row_count', 0)} | {status} |"
        )
    return "\n".join(lines)


__all__ = [
    "FORBIDDEN_SQL_KEYWORDS",
    "FORBIDDEN_SQL_TOKENS",
    "POSITIVE_CASES",
    "V1_WHITELIST",
    "AcceptanceCase",
    "assert_safe_sql",
    "evaluate_case",
    "references_column",
    "summarize",
]
