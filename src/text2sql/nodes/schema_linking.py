"""``schema_linking`` node (text2sql_V1.md section 5.4).

Maps the structured intent onto the V1 business tables/columns by calling the
Catalog service.  It never invents fields: everything it returns has passed the
exposure whitelist, and coverage of every required metric/dimension/filter is
verified before ``selected_tables`` is produced.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from ..catalog import Catalog, TableMeta, match_specificity
from ..errors import SCHEMA_LINKING_ERROR, RECOVERY_SURFACE, sql_error
from ..state import SQLGraphState, TableSelection
from .deps import NodeDeps

logger = logging.getLogger(__name__)

USER_GRAIN_TABLES = ("dim_user", "mart_user_behavior_summary", "mart_user_order_summary")
SELF_CONTAINED_TABLES = (
    "mart_campaign_summary",
    "mart_funnel_summary",
    "mart_retention_cohort",
)
MAX_TABLES = 4


def schema_linking(state: SQLGraphState, *, deps: NodeDeps) -> dict[str, Any]:
    """Node entry point."""
    settings = deps.settings
    catalog = deps.catalog

    rewrite_question = state.get("rewrite_question")
    entities = state.get("info_entities")
    # 必要字段检查
    if not isinstance(rewrite_question, str) or not rewrite_question.strip():
        return _fail(settings, "缺少前置状态 rewrite_question，无法进行 schema linking")
    if not isinstance(entities, dict):
        return _fail(settings, "缺少前置状态 info_entities，无法进行 schema linking")

    # 可选字段获取
    metrics = list(entities.get("metrics") or [])
    dimensions = list(entities.get("dimensions") or [])
    keywords = list(entities.get("keywords") or [])
    filters = list(entities.get("filters") or [])
    start_time = entities.get("start_time")
    end_time = entities.get("end_time")

    # ---------------------------------------------------------------- search
    search = catalog.search(
        question=rewrite_question,
        keywords=keywords,
        dimensions=dimensions,
        metrics=metrics,
        max_tables=MAX_TABLES,
    )
    candidates = list(search["candidates"])

    # -------------------------------------------------------------- coverage
    unresolved_metrics: list[str] = []
    unresolved_dimensions: list[str] = []
    required: dict[str, set[str]] = {}  # table -> required columns

    def add_requirement(table_name: str, column_name: str) -> None:
        required.setdefault(table_name, set()).add(column_name)

    for term in metrics:
        resolved = catalog.resolve_metric(term)
        if resolved is None:
            unresolved_metrics.append(term)
            continue
        table, column, _ = resolved
        add_requirement(table.table_name, column.column_name)
        # ratio metrics also need their numerator/denominator to be selectable
        semantics = catalog.metric_for_column(table.table_name, column.column_name)
        if semantics is not None:
            for extra in (semantics.numerator_column, semantics.denominator_column):
                if extra and catalog.get_column(table.table_name, extra) is not None:
                    add_requirement(table.table_name, extra)

    for term in dimensions:
        resolved = catalog.resolve_dimension(term)
        if resolved is None:
            unresolved_dimensions.append(term)
            continue
        table, column, _ = resolved
        add_requirement(table.table_name, column.column_name)

    if unresolved_metrics or unresolved_dimensions:
        parts = []
        if unresolved_metrics:
            parts.append("指标 " + "、".join(unresolved_metrics))
        if unresolved_dimensions:
            parts.append("维度 " + "、".join(unresolved_dimensions))
        return _fail(
            settings,
            "无法在 V1 业务表中找到对应的字段："
            + "；".join(parts)
            + "（V1 不包含商品、类目、品牌等字段，禁止虚构字段）",
        )

    # filters must resolve to a real column as well
    unresolved_filters: list[str] = []
    for condition in filters:
        hint = str(condition.get("field_hint", ""))
        resolved = catalog.resolve_dimension(hint)
        if resolved is None:
            unresolved_filters.append(hint)
            continue
        table, column, _ = resolved
        add_requirement(table.table_name, column.column_name)
    if unresolved_filters:
        return _fail(
            settings,
            "过滤条件无法映射到 V1 字段：" + "、".join(unresolved_filters),
        )

    # A keyword that *exactly* names a catalog field is a real business term
    # (e.g. "复购用户" -> is_repeat_buyer), so it must constrain table selection
    # too; unresolved keywords stay free-form and are not fatal.
    for term in keywords:
        resolved = catalog.resolve_dimension(term) or catalog.resolve_metric(term)
        if resolved is None:
            continue
        table, column, matched_on = resolved
        if match_specificity(term, matched_on) >= 100:
            add_requirement(table.table_name, column.column_name)

    # ------------------------------------------------------ table assembly
    if not candidates and not required:
        attempted = [*metrics, *dimensions, *keywords]
        detail = "、".join(dict.fromkeys(term for term in attempted if term))
        return _fail(
            settings,
            "无法命中任何 V1 业务表：问题中的概念"
            + (f"（{detail}）" if detail else "")
            + "不在 V1 暴露的 6 张业务表范围内",
        )

    selected_table_names = {table for table in required if table in catalog.allowed_tables}
    if not selected_table_names:
        if candidates:
            return _fail(
                settings,
                "无法确定要统计的指标或分组维度："
                "问题只命中了业务表但没有可解析的指标/维度，请补充要统计的口径"
                f"（命中候选：{'、'.join(c['table'] for c in candidates)}）",
            )
        return _fail(settings, "指标和维度没有落在任何允许的 V1 业务表上")

    self_contained = selected_table_names & set(SELF_CONTAINED_TABLES)
    if self_contained and len(selected_table_names) > 1:
        return _fail(
            settings,
            "指标和维度分别落在不允许关联的表组合中："
            + "、".join(sorted(selected_table_names))
            + "（活动、漏斗、留存汇总表在 V1 中不能与用户粒度表关联）",
        )

    # Anchor user-grain queries on dim_user (documented join pattern, section 2.3)
    if selected_table_names & set(USER_GRAIN_TABLES):
        selected_table_names.add("dim_user")

    # every selected table must be a candidate: add the documented anchor so
    # selected_tables stays a subset of candidate_tables (section 5.4)
    candidate_names = {candidate["table"] for candidate in candidates}
    for table_name in sorted(selected_table_names - candidate_names, key=_table_order):
        table = catalog.get_table(table_name)
        if table is None or not table.allowed:
            continue
        candidates.append(
            {
                "table": table_name,
                "matched_columns": sorted(required.get(table_name, set())),
                "matched_terms": [],
                "score": 0.0,
                "reason": f"用户粒度查询的 JOIN 锚点（{table.grain}），按 user_id 关联",
            }
        )
        candidate_names.add(table_name)

    outside = selected_table_names - candidate_names
    if outside:
        return _fail(
            settings,
            "选中的表没有通过 Catalog 候选校验：" + "、".join(sorted(outside)),
        )

    # ------------------------------------------------------------- columns
    selections: list[TableSelection] = []
    for table_name in sorted(selected_table_names, key=_table_order):
        table = catalog.get_table(table_name)
        if table is None:
            continue
        columns = set(required.get(table_name, set()))
        if table.primary_key:
            columns.add(table.primary_key)
        if start_time or end_time:
            for candidate_column in table.default_time_columns:
                columns.add(candidate_column)
        if not columns:
            columns.add(table.primary_key or next(iter(catalog.column_names(table_name))))
        reasoning = next(
            (
                candidate["reason"]
                for candidate in candidates
                if candidate["table"] == table_name
            ),
            f"覆盖 {table.grain} 上的必要字段",
        )
        selections.append(
            TableSelection(
                table=table_name,
                columns=sorted(columns),
                reasoning=reasoning,
            )
        )

    if not selections:
        return _fail(settings, "未能选择任何可用的业务表和字段")

    # time filters need a real time column somewhere in the selection
    if start_time or end_time:
        has_time = False
        for selection in selections:
            for column_name in selection["columns"]:
                column = catalog.get_column(selection["table"], column_name)
                if column is not None and column.role == "time":
                    has_time = True
                    break
            if has_time:
                break
        if not has_time:
            return _fail(
                settings,
                "所选汇总表在 V1 中没有可用时间字段，无法应用时间过滤",
            )

    # ---------------------------------------------------------- join check
    user_tables = [name for name in selected_table_names if name in USER_GRAIN_TABLES]
    for index, left in enumerate(user_tables):
        for right in user_tables[index + 1 :]:
            if not catalog.is_join_allowed(left, right):
                return _fail(
                    settings,
                    f"不允许的 JOIN 组合：{left} ~ {right}（V1 只允许通过 user_id 关联用户粒度表）",
                )

    candidate_selections = [
        TableSelection(
            table=candidate["table"],
            columns=sorted(set(candidate["matched_columns"]) | required.get(candidate["table"], set())),
            reasoning=candidate["reason"],
        )
        for candidate in candidates
    ]

    logger.info(
        "schema_linking 候选表=%s 选中表=%s",
        [candidate["table"] for candidate in candidates],
        [selection["table"] for selection in selections],
    )
    for selection in selections:
        logger.debug(
            "  选中 %s(%s)：%s",
            selection["table"],
            ", ".join(selection["columns"]),
            selection.get("reasoning", ""),
        )

    return {
        "candidate_tables": candidate_selections,
        "selected_tables": selections,
        "status": "pending",
        "error": None,
    }


def _table_order(table_name: str) -> tuple[int, str]:
    if table_name == "dim_user":
        return (0, table_name)
    if table_name in USER_GRAIN_TABLES:
        return (1, table_name)
    return (2, table_name)


def _fail(settings, message: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "error": sql_error(
            SCHEMA_LINKING_ERROR, message, RECOVERY_SURFACE, settings=settings
        ),
    }


def selected_tables_of(state: SQLGraphState) -> list[TableSelection]:
    return list(state.get("selected_tables") or [])


def selection_map(selections: Iterable[TableSelection]) -> dict[str, set[str]]:
    return {selection["table"]: set(selection["columns"]) for selection in selections}


def user_grain_tables(catalog: Catalog, selections: Iterable[TableSelection]) -> list[TableMeta]:
    out = []
    for selection in selections:
        if selection["table"] in USER_GRAIN_TABLES:
            table = catalog.get_table(selection["table"])
            if table is not None:
                out.append(table)
    return out


__all__ = ["schema_linking", "selection_map", "user_grain_tables"]
