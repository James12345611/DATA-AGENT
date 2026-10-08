"""Deterministic guardrails that do not depend on the LLM.

V1 fails closed: a request that asks to modify data is rejected before any
schema linking or SQL generation happens, and a statement that tries to touch
system schemas or comments is rejected before execution.
"""

from __future__ import annotations

import re

# Destructive / write intent (section 8.2 N01).  Matched against the raw user
# question; the pipeline refuses to continue with recovery_strategy="abort".
UNSAFE_REQUEST_PATTERN = re.compile(
    r"(删除|删掉|清空|清库|抹掉|移除所有|更新|修改|改成|插入|新增一条|写入|"
    r"建表|改表|删表|授权|提权|"
    r"\bdrop\b|\bdelete\b|\bupdate\b|\binsert\b|\btruncate\b|\balter\b|"
    r"\bcreate\b|\bgrant\b|\brevoke\b|\bset\s+global\b)",
    re.IGNORECASE,
)

# System schemas that must never be reachable (section 8.2 N02).
SYSTEM_SCHEMAS = frozenset(
    {
        "information_schema",
        "mysql",
        "performance_schema",
        "sys",
        "catalog_table",
        "catalog_column",
    }
)

# Functions from other dialects that MySQL 8.0 does not provide (N08).
NON_MYSQL_FUNCTIONS = frozenset(
    {
        "DATE_TRUNC",
        "DATEADD",
        "DATEDIFF_BIG",
        "APPROX_COUNT_DISTINCT",
        "APPROX_DISTINCT",
        "APPROX_PERCENTILE",
        "ARRAY_AGG",
        "ARRAY_CONCAT",
        "ARRAY_LENGTH",
        "STRING_AGG",
        "LISTAGG",
        "TO_CHAR",
        "TO_DATE",
        "TO_NUMBER",
        "NVL",
        "NVL2",
        "IIF",
        "LEN",
        "ISNULL2",
        "SQUARE",
        "MEDIAN",
        "PERCENTILE_CONT",
        "PERCENTILE_DISC",
        "MODE",
        "SPLIT_PART",
        "REGEXP_MATCHES",
        "REGEXP_EXTRACT_ALL",
        "TRY_CAST",
        "SAFE_CAST",
        "GENERATE_SERIES",
        "GENERATE_DATE_ARRAY",
        "UNNEST",
        "FLATTEN",
        "STRUCT",
        "SAFE_DIVIDE",
        "IFNULL2",
        "QUALIFY",
        "ILIKE",
        "EXTRACT_EPOCH",
        "DATEPART",
        "DATENAME",
        "CONVERT_TIMEZONE",
        "TIMESTAMPDIFF_SECOND",
        "LOG2",
        "RANDOM",
        "SIGN2",
        "BOOL_AND",
        "BOOL_OR",
        "ANY_VALUE2",
        "GROUP_CONCAT_WS",
    }
)

# Statement node names that must never appear in a V1 statement (section 5.6.3).
FORBIDDEN_STATEMENTS = (
    "Insert",
    "Update",
    "Delete",
    "Drop",
    "Alter",
    "Create",
    "TruncateTable",
    "Truncate",
    "Grant",
    "Revoke",
    "Set",
    "SetItem",
    "Command",
    "Merge",
    "Copy",
    "LoadData",
    "Use",
    "Attach",
    "Detach",
    "Pragma",
    "Kill",
    "Call",
    "Transaction",
    "Commit",
    "Rollback",
    "Lock",
    "Describe",
    "Analyze",
    "Cache",
    "Uncache",
    "Refresh",
    "Vacuum",
    "Export",
    "Import",
    "Explain",
)


def looks_like_unsafe_request(question: str) -> bool:
    """True when the question asks to write, modify or destroy data."""
    return bool(UNSAFE_REQUEST_PATTERN.search(question or ""))


__all__ = [
    "FORBIDDEN_STATEMENTS",
    "NON_MYSQL_FUNCTIONS",
    "SYSTEM_SCHEMAS",
    "UNSAFE_REQUEST_PATTERN",
    "looks_like_unsafe_request",
]
