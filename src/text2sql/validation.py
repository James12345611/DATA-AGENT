"""Deterministic SQL validation (text2sql_V1.md section 5.6).

The validator is the security boundary: LLM explanations never replace it.
Table names, column names and the statement type are resolved from the sqlglot
MySQL AST - regular expressions are only used as a supplementary scan for
comments, string termination and multi-statement separators.

Configuration is fixed by the document::

    SQL_DIALECT      = "mysql"
    SQL_MAX_ROWS     = 100
    SQL_ALLOW_CTE    = True
    SQL_ALLOW_SUBQUERY = True
    SQL_ALLOW_EXPLAIN = False
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from .catalog import Catalog
from .config import Settings
from .errors import (
    RECOVERY_ABORT,
    SQL_POLICY_ERROR,
    UNKNOWN_COLUMN,
    UNKNOWN_TABLE,
    UNSAFE_SQL,
    sql_error,
)
from .guardrails import (
    FORBIDDEN_STATEMENTS,
    NON_MYSQL_FUNCTIONS,
    SYSTEM_SCHEMAS,
)
from .state import SQLError, TableSelection

logger = logging.getLogger(__name__)

READ_ONLY_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)
# sqlglot renamed Explain -> Describe over time; keep the check version safe.
_EXPLAIN_NODE = getattr(exp, "Explain", None) or getattr(exp, "Describe", None)
_BLACKLIST_RE = re.compile(
    r"\b(" + "|".join(sorted(NON_MYSQL_FUNCTIONS)) + r")\s*\(",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ValidationConfig:
    """Frozen validation configuration (section 5.6)."""

    dialect: str = "mysql"
    max_rows: int = 100
    allow_cte: bool = True
    allow_subquery: bool = True
    allow_explain: bool = False
    limit_policy: str = "normalize"

    @classmethod
    def from_settings(cls, settings: Settings) -> "ValidationConfig":
        return cls(
            dialect=settings.sql_dialect,
            max_rows=settings.sql_max_rows,
            allow_cte=True,
            allow_subquery=True,
            allow_explain=False,
            limit_policy=settings.sql_limit_policy,
        )


@dataclass
class ValidationResult:
    ok: bool
    sql: str
    error: SQLError | None = None
    normalized: bool = False
    notes: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)


class _Rejected(Exception):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


def _arg_any(node: exp.Expression, *names: str) -> Any:
    """Read the first present argument name.

    sqlglot renamed ``from``/``with`` to ``from_``/``with_`` in newer releases;
    this keeps the validator working across versions without regex parsing.
    """
    for name in names:
        value = node.args.get(name)
        if value is not None:
            return value
    return None


def _from_table(select: exp.Expression) -> exp.Expression | None:
    from_clause = _arg_any(select, "from_", "from")
    if from_clause is None:
        return None
    return from_clause.this


def _with_clause(expression: exp.Expression) -> exp.Expression | None:
    return _arg_any(expression, "with_", "with")


def _select_tables(select: exp.Expression) -> list[exp.Table]:
    """Tables directly referenced by one SELECT scope (FROM + JOINs)."""
    tables: list[exp.Table] = []
    from_table = _from_table(select)
    if isinstance(from_table, exp.Table):
        tables.append(from_table)
    for join in select.args.get("joins") or []:
        if isinstance(join.this, exp.Table):
            tables.append(join.this)
    return tables


def scan_sql_text(sql: str) -> list[str]:
    """Supplementary textual scan: comments, unclosed literals, stray ';'.

    Only used *in addition* to the AST checks, never as the primary parser.
    """
    issues: list[str] = []
    in_single = in_double = in_backtick = False
    index = 0
    length = len(sql)
    last_semicolon = -1
    while index < length:
        char = sql[index]
        nxt = sql[index + 1] if index + 1 < length else ""
        if in_single:
            if char == "\\":
                index += 2
                continue
            if char == "'":
                if nxt == "'":  # escaped quote
                    index += 2
                    continue
                in_single = False
        elif in_double:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                if nxt == '"':
                    index += 2
                    continue
                in_double = False
        elif in_backtick:
            if char == "`":
                in_backtick = False
        else:
            if char == "'":
                in_single = True
            elif char == '"':
                in_double = True
            elif char == "`":
                in_backtick = True
            elif char == "-" and nxt == "-":
                issues.append("SQL 中不允许出现注释（--）")
                index = length
                continue
            elif char == "#":
                issues.append("SQL 中不允许出现注释（#）")
                index = length
                continue
            elif char == "/" and nxt == "*":
                issues.append("SQL 中不允许出现注释（/* */）")
                index = length
                continue
            elif char == ";":
                last_semicolon = index
        index += 1

    if in_single or in_double or in_backtick:
        issues.append("SQL 中存在未闭合的字符串或标识符")
    if last_semicolon != -1 and sql[last_semicolon + 1 :].strip():
        issues.append("SQL 中不允许出现多语句分隔符（;）")
    return issues


class SQLValidator:
    """Validates one SQL statement against the V1 policy and the catalog."""

    def __init__(
        self,
        catalog: Catalog,
        config: ValidationConfig | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.catalog = catalog
        self.config = config or ValidationConfig()
        self.settings = settings

    # ---------------------------------------------------------------- public
    def validate(
        self,
        sql: str,
        selected_tables: Sequence[TableSelection] | None = None,
        *,
        attempt: int = 0,
    ) -> ValidationResult:
        selected = {
            selection["table"]: set(selection["columns"]) for selection in (selected_tables or [])
        }
        raw = (sql or "").strip()
        if not raw:
            return self._rejected(
                SQL_POLICY_ERROR, "SQL 为空，无法校验", raw, attempt
            )

        notes: list[str] = []
        try:
            # Supplementary textual scan runs first so that comment/string/
            # multi-statement attacks are reported as unsafe_sql.
            self._check_text_scan(raw)
            expression = self._parse(raw)
            self._check_statement_type(expression)
            self._check_forbidden_nodes(expression)
            self._check_functions(raw, expression)
            tables, aliases, cte_columns = self._collect_tables(expression, selected)
            self._check_tables(tables, selected)
            self._check_joins(expression, tables)
            self._check_star(expression)
            self._check_columns(expression, aliases, cte_columns)
            expression, normalized = self._check_limit(expression)
        except _Rejected as rejected:
            return self._rejected(
                rejected.error_type, rejected.message, raw, attempt
            )

        final_sql = raw
        if normalized:
            final_sql = expression.sql(dialect=self.config.dialect)
            notes.append(f"已按 SQL_MAX_ROWS={self.config.max_rows} 规范化 LIMIT")

        return ValidationResult(
            ok=True,
            sql=final_sql,
            error=None,
            normalized=normalized,
            notes=notes,
            tables=sorted(tables),
            columns=sorted(
                {f"{table}.{column}" for table, columns in aliases.items() for column in columns}
            ),
        )

    # -------------------------------------------------------------- internals
    def _rejected(self, error_type: str, message: str, sql: str, attempt: int) -> ValidationResult:
        error = sql_error(
            error_type,
            message,
            RECOVERY_ABORT,
            sql=sql,
            attempt=attempt,
            settings=self.settings,
        )
        return ValidationResult(ok=False, sql=sql, error=error)

    def _parse(self, sql: str) -> exp.Expression:
        try:
            statements = sqlglot.parse(sql, read=self.config.dialect)
        except (ParseError, TokenError) as exc:
            raise _Rejected(SQL_POLICY_ERROR, f"SQL 解析失败：{exc}") from exc
        statements = [statement for statement in statements if statement is not None]
        if len(statements) != 1:
            raise _Rejected(
                SQL_POLICY_ERROR,
                f"只能执行单条 SQL，解析得到 {len(statements)} 条语句",
            )
        return statements[0]

    def _check_text_scan(self, sql: str) -> None:
        issues = scan_sql_text(sql)
        if issues:
            raise _Rejected(UNSAFE_SQL, "；".join(issues))

    def _check_statement_type(self, expression: exp.Expression) -> None:
        if (
            _EXPLAIN_NODE is not None
            and isinstance(expression, _EXPLAIN_NODE)
            and not self.config.allow_explain
        ):
            raise _Rejected(UNSAFE_SQL, "V1 不允许 EXPLAIN 语句")
        if not isinstance(expression, READ_ONLY_ROOTS):
            raise _Rejected(
                UNSAFE_SQL,
                f"顶层语句必须是 SELECT 或 WITH ... SELECT，实际是 {type(expression).__name__}",
            )
        if isinstance(expression, exp.Select):
            with_clause = expression.args.get("with")
            if with_clause is not None and not self.config.allow_cte:
                raise _Rejected(SQL_POLICY_ERROR, "V1 配置不允许使用 CTE")

    def _check_forbidden_nodes(self, expression: exp.Expression) -> None:
        for name in FORBIDDEN_STATEMENTS:
            node_type = getattr(exp, name, None)
            if node_type is None or not isinstance(node_type, type):
                continue
            if issubclass(node_type, exp.Expression) and expression.find(node_type) is not None:
                if name == "Explain" and self.config.allow_explain:
                    continue
                raise _Rejected(
                    UNSAFE_SQL,
                    f"SQL 中禁止出现 {name.upper()} 语句或子句",
                )
        if not self.config.allow_subquery:
            for subquery in expression.find_all(exp.Subquery):
                if subquery.find(exp.Select) is not None and not isinstance(
                    subquery.parent, exp.CTE
                ):
                    raise _Rejected(SQL_POLICY_ERROR, "V1 配置不允许使用子查询")

    def _collect_tables(
        self,
        expression: exp.Expression,
        selected: dict[str, set[str]] | None = None,
    ) -> tuple[set[str], dict[str, set[str]], dict[str, set[str]]]:
        """Return ``(real_tables, alias->columns, cte_name->columns)``."""
        selected = selected or {}
        cte_columns: dict[str, set[str]] = {}
        cte_names: set[str] = set()
        with_clause = _with_clause(expression)
        if with_clause is not None:
            for cte in with_clause.expressions:
                name = cte.alias_or_name
                cte_names.add(name)
                inner = cte.this
                columns: set[str] = set()
                if isinstance(inner, exp.Select):
                    for projection in inner.expressions:
                        if isinstance(projection, exp.Alias):
                            columns.add(projection.alias)
                        elif isinstance(projection, exp.Column):
                            columns.add(projection.name)
                cte_columns[name] = columns

        aliases: dict[str, set[str]] = {}
        real_tables: set[str] = set()

        for table in expression.find_all(exp.Table):
            name = table.name
            if not name:
                continue
            if name in cte_names:
                alias = table.alias_or_name
                aliases[alias] = set(cte_columns.get(name, set()))
                continue
            schema = table.db or ""
            if schema and schema.lower() in SYSTEM_SCHEMAS:
                raise _Rejected(UNSAFE_SQL, f"禁止访问系统表或元数据表：{schema}.{name}")
            if "." in name:
                head = name.split(".", 1)[0].lower()
                if head in SYSTEM_SCHEMAS:
                    raise _Rejected(UNSAFE_SQL, f"禁止访问系统表或元数据表：{name}")
            if name not in self.catalog.allowed_tables:
                if name.lower() in SYSTEM_SCHEMAS or name.startswith(self.catalog.blocked_prefixes):
                    raise _Rejected(UNSAFE_SQL, f"禁止访问 V1 未暴露的表：{name}")
                raise _Rejected(
                    UNKNOWN_TABLE,
                    f"未知业务表 {name}（V1 只允许：{'、'.join(self.catalog.allowed_tables)}）",
                )
            real_tables.add(name)
            alias = table.alias_or_name
            # Only fields that schema_linking selected may be referenced.
            aliases[alias] = set(selected.get(name) or self.catalog.column_names(name))

        return real_tables, aliases, cte_columns

    def _check_tables(self, tables: Iterable[str], selected: dict[str, set[str]]) -> None:
        tables = set(tables)
        if selected:
            outside = tables - set(selected)
            if outside:
                raise _Rejected(
                    SQL_POLICY_ERROR,
                    "SQL 引用了 schema_linking 未选择的表：" + "、".join(sorted(outside)),
                )
            for table in tables:
                if table not in self.catalog.allowed_tables:
                    raise _Rejected(UNKNOWN_TABLE, f"未知业务表 {table}")

    def _check_joins(self, expression: exp.Expression, tables: set[str]) -> None:
        for select in expression.find_all(exp.Select):
            # Duplicate references are only a risk inside one query scope: the
            # same table may legitimately appear in two UNION branches.
            scope_tables = [
                table.name
                for table in _select_tables(select)
                if table.name in self.catalog.allowed_tables
            ]
            duplicates = {name for name in scope_tables if scope_tables.count(name) > 1}
            if duplicates:
                raise _Rejected(
                    SQL_POLICY_ERROR,
                    "同一张表被重复引用，存在重复聚合风险：" + "、".join(sorted(duplicates)),
                )

        for select in expression.find_all(exp.Select):
            from_table = _from_table(select)
            left_name = (
                from_table.name
                if isinstance(from_table, exp.Table) and from_table.name in self.catalog.allowed_tables
                else None
            )
            joined_names = [left_name] if left_name else []
            for join in select.args.get("joins") or []:
                right = join.this
                right_name = (
                    right.name
                    if isinstance(right, exp.Table) and right.name in self.catalog.allowed_tables
                    else None
                )
                if right_name:
                    for previous in joined_names:
                        if not previous or previous == right_name:
                            continue
                        if not self.catalog.is_join_allowed(previous, right_name):
                            raise _Rejected(
                                SQL_POLICY_ERROR,
                                f"不允许的 JOIN：{previous} ~ {right_name}",
                            )
                        key = self.catalog.join_key(previous, right_name)
                        if key is None:
                            raise _Rejected(
                                SQL_POLICY_ERROR,
                                f"JOIN {previous} ~ {right_name} 缺少登记在册的关联键",
                            )
                        left_column, right_column = key
                        on_clause = join.args.get("on")
                        if on_clause is None or not self._on_uses_key(
                            on_clause, left_column, right_column
                        ):
                            raise _Rejected(
                                SQL_POLICY_ERROR,
                                f"JOIN {previous} ~ {right_name} 必须使用 "
                                f"{right_column} 作为关联键",
                            )
                    joined_names.append(right_name)

    @staticmethod
    def _on_uses_key(on_clause: exp.Expression, left_key: str, right_key: str) -> bool:
        """The join condition must equate the two registered key columns."""
        for equality in on_clause.find_all(exp.EQ):
            left, right = equality.this, equality.expression
            if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                continue
            names = {left.name, right.name}
            if left_key == right_key:
                if names == {left_key}:
                    return True
            elif (left.name == left_key and right.name == right_key) or (
                left.name == right_key and right.name == left_key
            ):
                return True
        return False

    def _check_columns(
        self,
        expression: exp.Expression,
        aliases: dict[str, set[str]],
        cte_columns: dict[str, set[str]],
    ) -> None:
        output_aliases: set[str] = set()
        for select in expression.find_all(exp.Select):
            for projection in select.expressions:
                if isinstance(projection, exp.Alias):
                    output_aliases.add(projection.alias)

        all_real_columns: set[str] = set()
        for alias, columns in aliases.items():
            if alias in cte_columns:
                continue
            all_real_columns |= set(columns)

        for column in expression.find_all(exp.Column):
            name = column.name
            qualifier = column.table
            if qualifier:
                if qualifier in cte_columns:
                    known = cte_columns[qualifier]
                    if known and name not in known:
                        raise _Rejected(
                            UNKNOWN_COLUMN,
                            f"CTE {qualifier} 中不存在字段 {name}",
                        )
                    continue
                if qualifier not in aliases:
                    raise _Rejected(
                        UNKNOWN_COLUMN,
                        f"未知的表别名或表名：{qualifier}",
                    )
                if name not in aliases[qualifier]:
                    table_hint = self._table_for_alias(qualifier)
                    raise _Rejected(
                        UNKNOWN_COLUMN,
                        f"字段不存在：{table_hint}.{name}",
                    )
                continue
            if name in output_aliases or name in all_real_columns:
                continue
            if any(name in columns for columns in cte_columns.values()):
                continue
            raise _Rejected(UNKNOWN_COLUMN, f"字段不存在或没有限定表：{name}")

    def _table_for_alias(self, alias: str) -> str:
        if alias in self.catalog.allowed_tables:
            return alias
        return alias

    def _check_star(self, expression: exp.Expression) -> None:
        for select in expression.find_all(exp.Select):
            for projection in select.expressions:
                if isinstance(projection, exp.Star):
                    raise _Rejected(SQL_POLICY_ERROR, "禁止 SELECT *，必须显式列出字段")
                if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
                    raise _Rejected(SQL_POLICY_ERROR, "禁止 SELECT *，必须显式列出字段")

    def _check_functions(self, raw_sql: str, expression: exp.Expression) -> None:
        found: set[str] = set()
        for node in expression.walk():
            if isinstance(node, exp.Anonymous):
                found.add(str(node.name).upper())
            elif isinstance(node, (exp.Func, exp.Anonymous)):
                try:
                    found.add(str(node.sql_name()).upper())
                except Exception:  # pragma: no cover - defensive
                    continue
        offending = found & NON_MYSQL_FUNCTIONS
        text_offending = {match.upper() for match in _BLACKLIST_RE.findall(raw_sql)}
        offending |= text_offending
        if offending:
            raise _Rejected(
                SQL_POLICY_ERROR,
                "使用了非 MySQL 8.0 方言函数：" + "、".join(sorted(offending)),
            )

    def _check_limit(self, expression: exp.Expression) -> tuple[exp.Expression, bool]:
        limit = expression.args.get("limit")
        if limit is None:
            if self.config.limit_policy == "reject":
                raise _Rejected(
                    SQL_POLICY_ERROR,
                    f"SQL 必须包含 LIMIT，且不超过 {self.config.max_rows} 行",
                )
            return expression.limit(self.config.max_rows), True

        value = limit.expression
        if not isinstance(value, exp.Literal) or not value.is_int:
            raise _Rejected(SQL_POLICY_ERROR, "LIMIT 必须是整数字面量")
        number = int(value.this)
        if number <= 0:
            raise _Rejected(SQL_POLICY_ERROR, "LIMIT 必须大于 0")
        if number > self.config.max_rows:
            if self.config.limit_policy == "reject":
                raise _Rejected(
                    SQL_POLICY_ERROR,
                    f"LIMIT {number} 超过最大返回行数 {self.config.max_rows}",
                )
            return expression.limit(self.config.max_rows), True
        return expression, False


def validate_sql_text(
    sql: str,
    catalog: Catalog,
    selected_tables: Sequence[TableSelection] | None = None,
    *,
    settings: Settings | None = None,
    attempt: int = 0,
) -> ValidationResult:
    """Convenience wrapper used by tests and the CLI."""
    config = ValidationConfig.from_settings(settings) if settings else ValidationConfig()
    return SQLValidator(catalog, config, settings).validate(
        sql, selected_tables, attempt=attempt
    )


__all__ = [
    "SQLValidator",
    "ValidationConfig",
    "ValidationResult",
    "scan_sql_text",
    "validate_sql_text",
]
