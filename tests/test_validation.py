"""SQL validator tests (text2sql_V1.md section 5.6 check list)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from text2sql.catalog import Catalog
from text2sql.config import Settings
from text2sql.validation import (
    SQLValidator,
    ValidationConfig,
    scan_sql_text,
    validate_sql_text,
)

SELECTED_USER = [
    {"table": "dim_user", "columns": ["user_id", "channel"]},
]
SELECTED_ORDER = [
    {"table": "dim_user", "columns": ["user_id", "channel"]},
    {"table": "mart_user_order_summary", "columns": ["user_id", "net_amount"]},
]
SELECTED_CAMPAIGN = [
    {
        "table": "mart_campaign_summary",
        "columns": [
            "campaign_id",
            "exposure_count",
            "clicked_count",
            "converted_count",
            "click_rate",
            "conversion_rate",
        ],
    }
]


def _validate(catalog: Catalog, sql: str, selected=None, settings: Settings | None = None):
    config = ValidationConfig.from_settings(settings) if settings else ValidationConfig()
    return SQLValidator(catalog, config, settings).validate(sql, selected)


def test_valid_query_passes(catalog: Catalog):
    result = _validate(
        catalog,
        "SELECT u.channel, COUNT(u.user_id) AS users FROM dim_user AS u "
        "GROUP BY u.channel LIMIT 100",
        SELECTED_USER,
    )
    assert result.ok, result.error
    assert result.normalized is False


def test_missing_limit_is_normalized_to_max_rows(catalog: Catalog):
    result = _validate(
        catalog,
        "SELECT channel, COUNT(user_id) FROM dim_user GROUP BY channel",
        SELECTED_USER,
    )
    assert result.ok
    assert result.normalized
    assert "LIMIT 100" in result.sql.upper()


def test_limit_above_max_rows_is_clamped(catalog: Catalog):
    result = _validate(catalog, "SELECT channel FROM dim_user LIMIT 5000", SELECTED_USER)
    assert result.ok
    assert result.normalized
    assert "LIMIT 100" in result.sql.upper()


def test_limit_policy_reject_refuses_missing_and_oversized_limit(catalog: Catalog):
    settings = Settings(sql_limit_policy="reject")
    missing = _validate(catalog, "SELECT channel FROM dim_user", SELECTED_USER, settings)
    assert not missing.ok and missing.error["error_type"] == "sql_policy_error"
    oversized = _validate(catalog, "SELECT channel FROM dim_user LIMIT 500", SELECTED_USER, settings)
    assert not oversized.ok and oversized.error["recovery_strategy"] == "abort"


def test_limit_must_be_literal(catalog: Catalog):
    result = _validate(catalog, "SELECT channel FROM dim_user LIMIT 10 + 5", SELECTED_USER)
    assert not result.ok
    assert result.error["error_type"] == "sql_policy_error"


def test_multiple_statements_are_rejected(catalog: Catalog):
    result = _validate(
        catalog,
        "SELECT channel FROM dim_user LIMIT 10; DROP TABLE dim_user",
        SELECTED_USER,
    )
    assert not result.ok
    assert result.error["error_type"] in {"unsafe_sql", "sql_policy_error"}


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO dim_user (user_id) VALUES ('x')",
        "UPDATE dim_user SET channel = 'x'",
        "DELETE FROM dim_user",
        "DROP TABLE dim_user",
        "ALTER TABLE dim_user ADD COLUMN x INT",
        "TRUNCATE TABLE dim_user",
        "CREATE TABLE t (id INT)",
        "GRANT SELECT ON dim_user TO 'x'@'localhost'",
        "SET GLOBAL local_infile = 1",
    ],
)
def test_write_statements_are_rejected(catalog: Catalog, sql: str):
    result = _validate(catalog, sql, SELECTED_USER)
    assert not result.ok
    assert result.error["recovery_strategy"] == "abort"
    assert result.error["error_type"] in {"unsafe_sql", "sql_policy_error"}


def test_system_tables_are_rejected(catalog: Catalog):
    result = _validate(
        catalog,
        "SELECT table_name FROM information_schema.tables LIMIT 10",
        SELECTED_USER,
    )
    assert not result.ok
    assert result.error["error_type"] == "unsafe_sql"
    assert "系统表" in result.error["message"] or "未暴露" in result.error["message"]


def test_unexposed_business_tables_are_rejected(catalog: Catalog):
    for table in ("raw_orders", "fact_order", "catalog_table", "dim_product"):
        result = _validate(catalog, f"SELECT user_id FROM {table} LIMIT 10", SELECTED_USER)
        assert not result.ok, table
        assert result.error["error_type"] in {"unsafe_sql", "unknown_table"}


def test_unknown_table_is_reported(catalog: Catalog):
    result = _validate(catalog, "SELECT id FROM not_a_real_table LIMIT 10", SELECTED_USER)
    assert not result.ok
    assert result.error["error_type"] == "unknown_table"


def test_unknown_column_is_reported(catalog: Catalog):
    result = _validate(
        catalog,
        "SELECT u.user_name FROM dim_user AS u LIMIT 10",
        SELECTED_USER,
    )
    assert not result.ok
    assert result.error["error_type"] == "unknown_column"
    assert "user_name" in result.error["message"]


def test_column_outside_selected_tables_is_rejected(catalog: Catalog):
    result = _validate(
        catalog,
        "SELECT u.gender FROM dim_user AS u LIMIT 10",
        SELECTED_USER,
    )
    assert not result.ok
    assert result.error["error_type"] == "unknown_column"


def test_table_outside_selection_is_rejected(catalog: Catalog):
    result = _validate(
        catalog,
        "SELECT o.net_amount FROM mart_user_order_summary AS o LIMIT 10",
        SELECTED_USER,
    )
    assert not result.ok
    assert result.error["error_type"] == "sql_policy_error"
    assert "未选择" in result.error["message"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM dim_user LIMIT 10",
        "SELECT u.* FROM dim_user AS u LIMIT 10",
    ],
)
def test_select_star_is_rejected(catalog: Catalog, sql: str):
    result = _validate(catalog, sql, SELECTED_USER)
    assert not result.ok
    assert result.error["error_type"] == "sql_policy_error"


def test_count_star_is_allowed(catalog: Catalog):
    result = _validate(
        catalog,
        "SELECT COUNT(*) AS users FROM dim_user LIMIT 10",
        SELECTED_USER,
    )
    assert result.ok, result.error


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT DATE_TRUNC('day', register_date) FROM dim_user LIMIT 5",
        "SELECT APPROX_COUNT_DISTINCT(user_id) FROM dim_user LIMIT 5",
        "SELECT TO_CHAR(register_date, 'YYYY') FROM dim_user LIMIT 5",
        "SELECT NVL(channel, 'x') FROM dim_user LIMIT 5",
    ],
)
def test_non_mysql_functions_are_rejected(catalog: Catalog, sql: str):
    result = _validate(catalog, sql, SELECTED_USER)
    assert not result.ok
    assert result.error["error_type"] == "sql_policy_error"
    assert "方言" in result.error["message"]


def test_mysql_functions_are_allowed(catalog: Catalog):
    result = _validate(
        catalog,
        "SELECT DATE_FORMAT(register_date, '%Y-%m') AS month, COUNT(user_id) AS users "
        "FROM dim_user GROUP BY DATE_FORMAT(register_date, '%Y-%m') LIMIT 20",
        [{"table": "dim_user", "columns": ["user_id", "channel", "register_date"]}],
    )
    assert result.ok, result.error


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT channel /* comment */ FROM dim_user LIMIT 5",
        "SELECT channel -- comment\nFROM dim_user LIMIT 5",
        "SELECT channel # comment\nFROM dim_user LIMIT 5",
    ],
)
def test_comments_are_rejected(catalog: Catalog, sql: str):
    result = _validate(catalog, sql, SELECTED_USER)
    assert not result.ok
    assert result.error["error_type"] == "unsafe_sql"


def test_unclosed_string_is_rejected(catalog: Catalog):
    result = _validate(
        catalog,
        "SELECT channel FROM dim_user WHERE channel = 'ads LIMIT 5",
        SELECTED_USER,
    )
    assert not result.ok
    assert result.error["error_type"] == "unsafe_sql"


def test_forbidden_join_is_rejected(catalog: Catalog):
    result = _validate(
        catalog,
        "SELECT c.campaign_id, u.channel FROM mart_campaign_summary AS c "
        "JOIN dim_user AS u ON u.user_id = c.campaign_id LIMIT 5",
        SELECTED_CAMPAIGN + SELECTED_USER,
    )
    assert not result.ok
    assert result.error["error_type"] == "sql_policy_error"
    assert "JOIN" in result.error["message"]


def test_user_grain_join_must_use_user_id(catalog: Catalog):
    result = _validate(
        catalog,
        "SELECT u.channel, o.net_amount FROM dim_user AS u "
        "JOIN mart_user_order_summary AS o ON o.user_id = u.channel LIMIT 5",
        SELECTED_ORDER,
    )
    assert not result.ok
    assert result.error["error_type"] == "sql_policy_error"


def test_duplicate_table_reference_is_rejected(catalog: Catalog):
    result = _validate(
        catalog,
        "SELECT a.channel FROM dim_user AS a "
        "JOIN dim_user AS b ON b.user_id = a.user_id LIMIT 5",
        SELECTED_USER,
    )
    assert not result.ok
    assert result.error["error_type"] == "sql_policy_error"


def test_allowed_user_grain_join_passes(catalog: Catalog):
    result = _validate(
        catalog,
        "SELECT u.channel, SUM(o.net_amount) AS amount FROM dim_user AS u "
        "LEFT JOIN mart_user_order_summary AS o ON o.user_id = u.user_id "
        "GROUP BY u.channel LIMIT 100",
        SELECTED_ORDER,
    )
    assert result.ok, result.error


def test_cte_is_allowed_and_validated(catalog: Catalog):
    result = _validate(
        catalog,
        "WITH per_channel AS (SELECT channel, COUNT(user_id) AS users FROM dim_user "
        "GROUP BY channel) SELECT channel, users FROM per_channel LIMIT 100",
        SELECTED_USER,
    )
    assert result.ok, result.error


def test_cte_unknown_column_is_rejected(catalog: Catalog):
    result = _validate(
        catalog,
        "WITH per_channel AS (SELECT channel, COUNT(user_id) AS users FROM dim_user "
        "GROUP BY channel) SELECT channel, missing_column FROM per_channel LIMIT 100",
        SELECTED_USER,
    )
    assert not result.ok
    assert result.error["error_type"] == "unknown_column"


def test_group_by_alias_is_recognized(catalog: Catalog):
    result = _validate(
        catalog,
        "SELECT u.channel AS channel_alias, COUNT(u.user_id) AS users FROM dim_user AS u "
        "GROUP BY channel_alias ORDER BY users DESC LIMIT 10",
        SELECTED_USER,
    )
    assert result.ok, result.error


def test_explain_is_rejected(catalog: Catalog):
    result = _validate(catalog, "EXPLAIN SELECT channel FROM dim_user LIMIT 5", SELECTED_USER)
    assert not result.ok
    assert result.error["error_type"] == "unsafe_sql"


def test_union_query_is_read_only_and_allowed(catalog: Catalog):
    result = _validate(
        catalog,
        "SELECT channel FROM dim_user UNION SELECT gender FROM dim_user LIMIT 10",
        [{"table": "dim_user", "columns": ["user_id", "channel", "gender"]}],
    )
    assert result.ok, result.error


def test_validation_errors_carry_sql_and_attempt(catalog: Catalog):
    result = _validate(catalog, "DROP TABLE dim_user", SELECTED_USER)
    assert result.error["sql"] == "DROP TABLE dim_user"
    assert result.error["attempt"] == 0


def test_validator_config_defaults_match_document():
    settings = Settings()
    config = ValidationConfig.from_settings(settings)
    assert config.dialect == "mysql"
    assert config.max_rows == 100
    assert config.allow_cte is True
    assert config.allow_subquery is True
    assert config.allow_explain is False


def test_empty_sql_is_rejected(catalog: Catalog):
    result = _validate(catalog, "   ", SELECTED_USER)
    assert not result.ok
    assert result.error["error_type"] == "sql_policy_error"


def test_scan_sql_text_flags_comment_and_multi_statement():
    assert scan_sql_text("SELECT 1 -- x") != []
    assert scan_sql_text("SELECT 1; SELECT 2") != []
    assert scan_sql_text("SELECT 'a;b' ") == []
    assert scan_sql_text("SELECT 'unterminated") != []


def test_missing_selected_tables_skips_membership_check(catalog: Catalog):
    result = validate_sql_text("SELECT channel FROM dim_user LIMIT 5", catalog, None)
    assert result.ok, result.error
