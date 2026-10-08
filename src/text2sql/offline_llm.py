"""Deterministic offline LLM client.

Used when ``LLM_API_KEY`` is empty and by the hermetic test suite.  It is a
*rule based* stand-in for the three LLM nodes, not a language model:

* extraction  - matches business vocabulary from the catalog
* generation  - builds one MySQL 8.0 read-only statement from the intent and the
                selected catalog columns

It obeys exactly the same output contracts as the real client, so the graph,
the validator, the executor and the retry loop can be exercised without any
network access.  It never invents tables or columns: everything it emits comes
from the catalog selections passed in the prompt.
"""

from __future__ import annotations

import json
import re
from typing import Any, Sequence

from .config import Settings
from .prompts import Prompt

UNSAFE_PATTERNS = re.compile(
    r"(删除|删掉|清空|清库|抹掉|更新|修改|改成|插入|新增一条|写入|建表|改表|"
    r"\bdrop\b|\bdelete\b|\bupdate\b|\binsert\b|\btruncate\b|\balter\b|\bgrant\b)",
    re.IGNORECASE,
)

_TOP_N_RE = re.compile(r"(?:前|top\s*|最高的?|最多的?|最大的?|最好的?)\s*(\d{1,3})\s*(?:个|名|条|位|行)?")
_ANALYTIC_RE = re.compile(r"(最高|最多|最大|最好|最低|最少|最小|排名|排行|top)", re.IGNORECASE)
_ASC_RE = re.compile(r"(最低|最少|最小|最差)")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_RECENT_DAYS_RE = re.compile(r"(?:近|最近|过去)\s*(\d{1,3})\s*天")

USER_GRAIN_TABLES = ("dim_user", "mart_user_behavior_summary", "mart_user_order_summary")
_TABLE_ALIAS = {
    "dim_user": "u",
    "mart_user_behavior_summary": "b",
    "mart_user_order_summary": "o",
    "mart_campaign_summary": "c",
    "mart_funnel_summary": "f",
    "mart_retention_cohort": "r",
}


def _normalize(text: Any) -> str:
    from .catalog.service import normalize_term

    return normalize_term(text)


class DeterministicLLMClient:
    """Rule based ``BaseLLMClient`` implementation (no network)."""

    name = "offline"

    def __init__(self, settings: Settings | None = None, catalog: Any | None = None) -> None:
        self.settings = settings or Settings()
        self.catalog = catalog
        self._generation_calls = 0
        self._attempts_by_question: dict[str, int] = {}

    # ------------------------------------------------------------- interface
    def complete_text(self, prompt: Prompt, *, history: Sequence[Any] | None = None) -> str:
        if "查询意图解析器" in prompt.system:
            return json.dumps(self._extract(prompt), ensure_ascii=False)
        if "SQL 生成器" in prompt.system:
            return self._generate_sql(prompt)
        raise ValueError("离线客户端无法处理该提示词")

    def complete_json(self, prompt: Prompt, *, validator=None, history=None) -> dict[str, Any]:
        payload = self._extract(prompt)
        if validator is not None:
            validator(payload)
        return payload

    def complete_sql(self, prompt: Prompt, *, history: Sequence[Any] | None = None) -> str:
        return self._generate_sql(prompt)

    def close(self) -> None:
        return None

    # ------------------------------------------------------------ extraction
    def _extract(self, prompt: Prompt) -> dict[str, Any]:
        question = self._question_from_prompt(prompt.user)
        if not question:
            question = prompt.user
        norm = _normalize(question)

        if UNSAFE_PATTERNS.search(question):
            return {
                "rewrite_question": question,
                "keywords": [],
                "dimensions": [],
                "metrics": [],
                "filters": [],
                "start_time": None,
                "end_time": None,
                "timezone": None,
                "unsafe_request": True,
            }

        metrics: list[str] = []
        dimensions: list[str] = []
        keywords: list[str] = []
        filters: list[dict[str, Any]] = []

        if self.catalog is not None:
            metric_terms, metric_spans = self._match_terms(
                norm, roles={"metric", "flag"}, use_semantics=True
            )
            metrics.extend(metric_terms)
            dimension_terms, _ = self._match_terms(
                norm,
                roles={"dimension", "time", "key"},
                use_semantics=False,
                blocked_spans=metric_spans,
            )
            dimensions.extend(dimension_terms)

            for table_name in self.catalog.exposed_tables:
                table = self.catalog.get_table(table_name)
                if table is None:
                    continue
                for term in table.search_terms():
                    term_norm = _normalize(term)
                    if len(term_norm) >= 2 and term_norm in norm and term not in keywords:
                        keywords.append(term)

            if not metrics and not dimensions and not keywords:
                # keep unrecognised business nouns as keywords so schema_linking
                # (not extraction) reports the "no V1 table" error, exactly like
                # a real model would.
                keywords.extend(_fallback_keywords(question))

        # canonical wording for the classic "复购用户" filter
        if "复购" in question:
            filters.append({"field_hint": "是否复购用户", "operator": "eq", "value": 1})
            if "复购用户数" not in metrics and re.search(r"(多少|数量|几个|人数)", question):
                metrics.append("复购用户数")

        start_time = None
        end_time = None
        dates = _ISO_DATE_RE.findall(question)
        if len(dates) >= 2:
            start_time, end_time = dates[0], dates[1]
        elif len(dates) == 1:
            start_time = end_time = dates[0]

        return {
            "rewrite_question": question,
            "keywords": keywords[:8],
            "dimensions": dimensions[:6],
            "metrics": metrics[:6],
            "filters": filters,
            "start_time": start_time,
            "end_time": end_time,
            "timezone": "Asia/Shanghai" if start_time or _RECENT_DAYS_RE.search(question) else None,
        }

    @staticmethod
    def _question_from_prompt(user: str) -> str:
        match = re.search(r"用户问题：(.+)", user)
        return match.group(1).strip() if match else ""

    def _match_terms(
        self,
        norm_question: str,
        *,
        roles: set[str],
        use_semantics: bool,
        blocked_spans: list[tuple[int, int]] | None = None,
    ) -> tuple[list[str], list[tuple[int, int]]]:
        """Match business terms, longest (most specific) match wins.

        Two rules keep the offline extraction honest:

        * a specific term ("行为次数") must not be shadowed by a generic one
          ("行为") - the longest match for each canonical name wins;
        * a span already consumed as a metric cannot be re-used as a dimension,
          so "行为次数最多的 10 个用户" does not drag in the funnel table.
        """
        catalog = self.catalog
        candidates: list[tuple[int, str, str, tuple[int, int]]] = []
        blocked = list(blocked_spans or [])

        def overlaps(span: tuple[int, int]) -> bool:
            """Blocked only when fully contained in a span of the other category.

            A mere partial overlap must stay usable: in "各行为阶段的去重用户数"
            the metric matches the long business name "阶段去重用户数" while the
            dimension legitimately matches "行为阶段" just before it.
            """
            start, end = span
            return any(
                start >= other_start and end <= other_end
                for other_start, other_end in blocked
            )

        def scan(terms: tuple[str, ...] | list[str], canonical: str) -> None:
            best: tuple[int, str, tuple[int, int]] | None = None
            for term in terms:
                term_norm = _normalize(term)
                if len(term_norm) < 2:
                    continue
                index = norm_question.find(term_norm)
                if index < 0:
                    continue
                span = (index, index + len(term_norm))
                if best is None or len(term_norm) > best[0]:
                    best = (len(term_norm), term_norm, span)
            if best is not None:
                candidates.append((best[0], best[1], canonical, best[2]))

        if use_semantics:
            for metric in catalog.metrics:
                scan(list(metric.search_terms()), metric.name)

        for table_name in catalog.exposed_tables:
            for column in catalog.columns_of(table_name).values():
                if column.role not in roles:
                    continue
                scan(list(column.search_terms()), column.business_name)

        candidates.sort(key=lambda item: (-item[0], item[2]))
        accepted_spans: list[tuple[int, int]] = []
        out: list[str] = []
        out_spans: list[tuple[int, int]] = []
        for _, term_norm, canonical, span in candidates:
            if overlaps(span):
                continue
            # drop anything contained in (or identical to) a span already used
            if any(
                span[0] >= other[0] and span[1] <= other[1] for other in accepted_spans
            ):
                continue
            accepted_spans.append(span)
            out_spans.append(span)
            if canonical not in out:
                out.append(canonical)
        return out, out_spans

    # ------------------------------------------------------------ generation
    def _generate_sql(self, prompt: Prompt) -> str:
        blocks = _parse_generation_prompt(prompt.user)
        question = blocks["question"]
        entities = blocks["entities"]
        tables = blocks["tables"]
        columns = blocks["columns"]
        if not tables:
            raise ValueError("离线客户端没有拿到可选表，无法生成 SQL")

        self._generation_calls += 1
        # Alias suffixes keep regenerated SQL textually different from the
        # previous attempt while staying stable for the first attempt.
        key = question.strip()
        attempt = self._attempts_by_question.get(key, 0) + 1
        self._attempts_by_question[key] = attempt
        alias_suffix = "" if attempt == 1 else f"_{attempt}"

        def alias_for(table: str) -> str:
            base = _TABLE_ALIAS.get(table, table[:2])
            return f"{base}{alias_suffix}"

        catalog = self.catalog
        resolved_dims: list[tuple[str, str]] = []
        for term in entities.get("dimensions", []):
            resolved = catalog.resolve_dimension(term) if catalog else None
            if resolved is None:
                continue
            table, column, _ = resolved
            if table.table_name in tables and f"{table.table_name}.{column.column_name}" in columns:
                pair = (table.table_name, column.column_name)
                if pair not in resolved_dims:
                    resolved_dims.append(pair)

        resolved_metrics: list[tuple[str, str]] = []
        for term in entities.get("metrics", []):
            resolved = catalog.resolve_metric(term) if catalog else None
            if resolved is None:
                continue
            table, column, _ = resolved
            if table.table_name in tables and f"{table.table_name}.{column.column_name}" in columns:
                pair = (table.table_name, column.column_name)
                if pair not in resolved_metrics:
                    resolved_metrics.append(pair)
        if not resolved_metrics and not resolved_dims:
            # fall back to every metric column that the selection exposes
            for qualified in columns:
                table_name, column_name = qualified.split(".", 1)
                column = catalog.get_column(table_name, column_name) if catalog else None
                if column is not None and column.role in {"metric", "flag"}:
                    resolved_metrics.append((table_name, column_name))
                    break

        select_parts: list[str] = []
        group_parts: list[str] = []
        for table_name, column_name in resolved_dims:
            expression = f"{alias_for(table_name)}.{column_name}"
            select_parts.append(expression)
            group_parts.append(expression)

        order_candidates: list[str] = []
        for table_name, column_name in resolved_metrics:
            expression = self._metric_expression(table_name, column_name, resolved_dims, alias_for)
            select_parts.append(expression)
            order_candidates.append(expression)

        if not select_parts:  # pragma: no cover - defensive
            raise ValueError("离线客户端无法从意图中解析出任何字段")

        from_parts = self._from_clause(tables, alias_for, resolved_dims + resolved_metrics)
        where_parts = self._where_clause(
            entities, tables, columns, alias_for, resolved_dims + resolved_metrics
        )

        sql = f"SELECT {', '.join(select_parts)}\nFROM {from_parts}"
        if where_parts:
            sql += "\nWHERE " + "\n  AND ".join(where_parts)
        if group_parts and resolved_metrics:
            sql += "\nGROUP BY " + ", ".join(group_parts)
        if order_candidates and _ANALYTIC_RE.search(question):
            direction = "ASC" if _ASC_RE.search(question) else "DESC"
            sql += f"\nORDER BY {order_candidates[0]} {direction}"
        sql += f"\nLIMIT {self._limit_from_question(question)}"
        return sql

    def _metric_expression(
        self,
        table_name: str,
        column_name: str,
        resolved_dims: list[tuple[str, str]],
        alias_for,
    ) -> str:
        catalog = self.catalog
        column = catalog.get_column(table_name, column_name) if catalog else None
        alias = alias_for(table_name)
        if column is None:
            return f"SUM({alias}.{column_name})"

        # Ratio metrics: recompute from numerator/denominator unless the query
        # groups by the grain of that table (then the stored rate is exact).
        semantics = catalog.metric_for_column(table_name, column_name) if catalog else None
        table = catalog.get_table(table_name) if catalog else None
        grouped_by_grain = table is not None and (table_name, table.primary_key) in resolved_dims
        if semantics is not None and semantics.is_ratio and not grouped_by_grain:
            numerator = semantics.numerator_column
            denominator = semantics.denominator_column
            if numerator and denominator:
                return (
                    f"ROUND(SUM({alias}.{numerator}) "
                    f"/ NULLIF(SUM({alias}.{denominator}), 0), 4)"
                )

        if column.is_ratio:
            if grouped_by_grain:
                return f"{alias}.{column_name}"
            numerator = f"SUM({alias}.{column.ratio_numerator})"
            denominator = f"SUM({alias}.{column.ratio_denominator})"
            return f"ROUND({numerator} / NULLIF({denominator}, 0), 4)"
        aggregation = column.aggregation
        if aggregation == "count":
            return f"COUNT({alias}.{column_name})"
        if aggregation in {"sum", "none"}:
            return f"SUM({alias}.{column_name})"
        if aggregation == "avg":
            return f"AVG({alias}.{column_name})"
        if aggregation == "min":
            return f"MIN({alias}.{column_name})"
        if aggregation == "max":
            return f"MAX({alias}.{column_name})"
        return f"SUM({alias}.{column_name})"

    def _from_clause(self, tables: list[str], alias_for, used: list[tuple[str, str]]) -> str:
        if "dim_user" in tables:
            base = f"dim_user AS {alias_for('dim_user')}"
            joins = []
            for table_name in tables:
                if table_name == "dim_user" or table_name not in USER_GRAIN_TABLES:
                    continue
                joins.append(
                    f"LEFT JOIN {table_name} AS {alias_for(table_name)}\n"
                    f"  ON {alias_for(table_name)}.user_id = {alias_for('dim_user')}.user_id"
                )
            return "\n".join([base, *joins])
        table_name = tables[0]
        return f"{table_name} AS {alias_for(table_name)}"

    def _where_clause(
        self,
        entities: dict[str, Any],
        tables: list[str],
        columns: set[str],
        alias_for,
        used: list[tuple[str, str]],
    ) -> list[str]:
        catalog = self.catalog
        clauses: list[str] = []
        for condition in entities.get("filters") or []:
            hint = str(condition.get("field_hint", ""))
            operator = str(condition.get("operator", "eq"))
            value = condition.get("value")
            target: tuple[str, str] | None = None
            best_score = 0
            for table_name, column_name in used:
                column = catalog.get_column(table_name, column_name) if catalog else None
                if column is None:
                    continue
                hint_norm = _normalize(hint)
                score = max(
                    (
                        len(_normalize(term))
                        for term in [column.business_name, column.column_name, *column.synonyms]
                        if _normalize(term) and _normalize(term) in hint_norm
                    ),
                    default=0,
                )
                if score > best_score:
                    best_score, target = score, (table_name, column_name)
            if target is None:
                continue
            table_name, column_name = target
            reference = f"{alias_for(table_name)}.{column_name}"
            if operator == "eq":
                clauses.append(f"{reference} = {_literal(value)}")
            elif operator == "neq":
                clauses.append(f"{reference} <> {_literal(value)}")
            elif operator in {"gt", "gte", "lt", "lte"}:
                symbol = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[operator]
                clauses.append(f"{reference} {symbol} {_literal(value)}")
            elif operator == "in" and isinstance(value, list):
                clauses.append(f"{reference} IN ({', '.join(_literal(v) for v in value)})")
            elif operator == "between" and isinstance(value, list) and len(value) == 2:
                clauses.append(f"{reference} BETWEEN {_literal(value[0])} AND {_literal(value[1])}")

        start_time = entities.get("start_time")
        end_time = entities.get("end_time")
        if start_time or end_time:
            time_column = None
            for table_name, column_name in used:
                column = catalog.get_column(table_name, column_name) if catalog else None
                if column is not None and column.role == "time":
                    time_column = (table_name, column_name)
                    break
            if time_column is None and catalog is not None:
                for table_name in tables:
                    table = catalog.get_table(table_name)
                    if table is None or not table.default_time_columns:
                        continue
                    candidate = table.default_time_columns[0]
                    if f"{table_name}.{candidate}" in columns:
                        time_column = (table_name, candidate)
                        break
            if time_column is not None:
                reference = f"{alias_for(time_column[0])}.{time_column[1]}"
                if start_time:
                    clauses.append(f"{reference} >= {_literal(start_time)}")
                if end_time:
                    clauses.append(f"{reference} < DATE_ADD({_literal(end_time)}, INTERVAL 1 DAY)")
        return clauses

    @staticmethod
    def _limit_from_question(question: str) -> int:
        match = _TOP_N_RE.search(question)
        if match:
            value = int(match.group(1))
            return max(1, min(value, 100))
        return 100


_STOPWORDS = {
    "统计", "查询", "查看", "分析", "计算", "各", "各个", "每个", "所有", "全部", "分别", "按",
    "的", "和", "与", "及", "以及", "是多少", "多少", "哪些", "哪个", "什么", "帮我", "一下",
    "情况", "数据", "请", "给出", "看", "下", "个", "条", "位", "名",
}


def _fallback_keywords(question: str) -> list[str]:
    """Split an unrecognised question into business-looking nouns."""
    tokens = re.split(r"[\s,，。、；;：:!！?？\"'“”‘’()（）\[\]【】]+", question or "")
    out: list[str] = []
    for token in tokens:
        cleaned = re.sub(
            r"^(统计|查询|查看|分析|计算|帮我|看看|请)+|(是多少|有多少|怎么样|是什么|多少|哪些)$",
            "",
            token,
        )
        for piece in re.split(r"(?:的|和|与|及|以及|按|每个|各个|各)", cleaned):
            piece = piece.strip()
            if len(piece) >= 2 and piece not in _STOPWORDS and piece not in out:
                out.append(piece)
    return out


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("'", "''")
    return f"'{text}'"


def _parse_generation_prompt(user: str) -> dict[str, Any]:
    """Rebuild the structured inputs the node embedded in the user prompt."""
    question = ""
    entities: dict[str, Any] = {}
    match = re.search(r"原始问题：(.*)", user)
    if match:
        question = match.group(1).strip()
    match = re.search(r"结构化意图：(\{.*\})", user)
    if match:
        try:
            entities = json.loads(match.group(1))
        except json.JSONDecodeError:
            entities = {}

    tables: list[str] = []
    columns: set[str] = set()
    current_table: str | None = None
    for line in user.splitlines():
        table_match = re.match(r"表\s+([A-Za-z_][A-Za-z0-9_]*)（", line.strip())
        if table_match:
            current_table = table_match.group(1)
            if current_table not in tables:
                tables.append(current_table)
            continue
        column_match = re.match(r"-\s+([A-Za-z_][A-Za-z0-9_]*)\s+\(", line.strip())
        if column_match and current_table:
            columns.add(f"{current_table}.{column_match.group(1)}")
    return {
        "question": question,
        "entities": entities,
        "tables": tables,
        "columns": columns,
    }


__all__ = ["DeterministicLLMClient", "UNSAFE_PATTERNS"]
