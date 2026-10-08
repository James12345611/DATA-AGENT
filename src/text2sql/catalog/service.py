"""Catalog service layer (text2sql_V1.md section 6).

``schema_linking`` never touches ``catalog_*`` storage tables: it calls the
abstract :class:`Catalog` interface below, which

* only ever returns objects from the V1 exposure whitelist,
* retrieves deterministically (no vectors, no LLM) and explainably,
* returns an empty candidate list instead of a "most similar guess",
* can verify its own consistency against the live MySQL views (section 6.7).
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from .models import (
    CatalogCandidate,
    CatalogConsistencyError,
    CatalogError,
    CatalogSearchResult,
    ColumnMeta,
    ColumnRole,
    JoinRule,
    MetricSemantic,
    TableEvidence,
    TableMeta,
    TermMatch,
)

# Deterministic scoring configuration (section 6.5).
SCORE_THRESHOLD = 0.30
SCORE_SCALE = 2.0
MAX_TABLES_DEFAULT = 4
ROLE_WEIGHT: dict[str, float] = {
    "metric": 1.0,
    "flag": 1.0,
    "dimension": 0.8,
    "time": 0.6,
    "key": 0.6,
}
SOURCE_WEIGHT: dict[str, float] = {
    "metric": 1.0,
    "dimension": 0.85,
    "keyword": 0.75,
    "question": 0.5,
}
# A table-level synonym hit is meaningful on its own (e.g. "留存"), but weaker
# than a real column hit.
TABLE_SYNONYM_POINTS = 0.7
MIN_SUBSTRING_LEN = 2

_PUNCT_RE = re.compile(r"[\s_\-./\\,，。、；;：:!！?？\"'“”‘’()（）\[\]【】<>《》|+*#@$%^&~`]+")
# Chinese structural particles that never change the business meaning of a term
# ("点击后的转化率" == "点击后转化率").
_PARTICLE_RE = re.compile(r"[的了之]")


def normalize_term(text: Any) -> str:
    """Unicode + case normalization used by every deterministic match."""
    if text is None:
        return ""
    value = unicodedata.normalize("NFKC", str(text)).casefold()
    value = _PUNCT_RE.sub("", value)
    return _PARTICLE_RE.sub("", value)


def _plural_variants(norm: str) -> set[str]:
    variants = {norm}
    if norm.endswith("ies") and len(norm) > 4:
        variants.add(norm[:-3] + "y")
    if norm.endswith("s") and len(norm) > 3:
        variants.add(norm[:-1])
    return variants


def _match_specificity(term_norm: str, target_norm: str) -> int:
    """0 = no match, otherwise the specificity (length) of the matched term."""
    if not term_norm or not target_norm:
        return 0
    if term_norm == target_norm:
        return len(target_norm) + 100  # exact matches dominate
    variants = _plural_variants(term_norm)
    if target_norm in variants:
        return len(target_norm) + 100
    if len(term_norm) >= MIN_SUBSTRING_LEN and term_norm in target_norm:
        return len(term_norm)
    if len(target_norm) >= MIN_SUBSTRING_LEN and target_norm in term_norm:
        return len(target_norm)
    return 0


def match_specificity(term: Any, target: Any) -> int:
    """Public, normalized version of the deterministic matcher.

    ``>= 100`` means the term matches a catalog term exactly (ignoring case,
    punctuation and Chinese structural particles); smaller positive values are
    substring matches, and ``0`` means no match at all.
    """
    return _match_specificity(normalize_term(term), normalize_term(target))


class Catalog:
    """The V1 business semantic registry."""

    def __init__(
        self,
        *,
        version: str,
        dialect: str,
        exposed_tables: Sequence[str],
        blocked_prefixes: Sequence[str],
        tables: Mapping[str, TableMeta],
        columns: Mapping[str, Mapping[str, ColumnMeta]],
        metrics: Sequence[MetricSemantic],
        join_rules: Sequence[JoinRule],
    ) -> None:
        self.version = version
        self.dialect = dialect
        self.exposed_tables: tuple[str, ...] = tuple(exposed_tables)
        self.blocked_prefixes: tuple[str, ...] = tuple(blocked_prefixes)
        self._tables = dict(tables)
        self._columns = {t: dict(cols) for t, cols in columns.items()}
        self._metrics = list(metrics)
        self._join_rules = [dict(rule) for rule in join_rules]  # type: ignore[misc]
        self._columns_by_table: dict[str, dict[str, ColumnMeta]] = self._columns

    # ------------------------------------------------------------- loading
    @classmethod
    def from_yaml(cls, path: str | Path) -> "Catalog":
        path = Path(path)
        if not path.is_file():
            raise CatalogError(f"catalog file not found: {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise CatalogError("catalog yaml must be a mapping")

        exposed = list(raw.get("exposed_tables") or [])
        blocked = list(raw.get("blocked_prefixes") or [])

        tables: dict[str, TableMeta] = {}
        for item in raw.get("tables") or []:
            table = TableMeta(
                table_name=str(item["table_name"]),
                table_kind=str(item.get("table_kind", "mart")),  # type: ignore[arg-type]
                grain=str(item.get("grain", "")),
                description=str(item.get("description", "")),
                synonyms=tuple(item.get("synonyms") or ()),
                allowed=bool(item.get("allowed", True)),
                default_time_columns=tuple(item.get("default_time_columns") or ()),
                primary_key=str(item.get("primary_key", "")),
            )
            tables[table.table_name] = table

        columns: dict[str, dict[str, ColumnMeta]] = {name: {} for name in tables}
        for item in raw.get("columns") or []:
            table_name = str(item["table_name"])
            column = ColumnMeta(
                table_name=table_name,
                column_name=str(item["column_name"]),
                data_type=str(item.get("data_type", "")),
                role=str(item.get("role", "dimension")),  # type: ignore[arg-type]
                business_name=str(item.get("business_name", "")),
                synonyms=tuple(item.get("synonyms") or ()),
                description=str(item.get("description", "")),
                nullable=bool(item.get("nullable", True)),
                allowed=bool(item.get("allowed", True)),
                aggregation=str(item.get("aggregation", "none")),  # type: ignore[arg-type]
                unit=item.get("unit"),
                is_ratio=bool(item.get("is_ratio", False)),
                ratio_numerator=item.get("ratio_numerator"),
                ratio_denominator=item.get("ratio_denominator"),
            )
            columns.setdefault(table_name, {})[column.column_name] = column

        metrics = [
            MetricSemantic(
                name=str(item["name"]),
                table=str(item["table"]),
                column=str(item["column"]),
                aggregation=str(item.get("aggregation", "none")),
                business_name=str(item.get("business_name", item["name"])),
                synonyms=tuple(item.get("synonyms") or ()),
                description=str(item.get("description", "")),
                formula=str(item.get("formula", "")),
                numerator_column=item.get("numerator_column"),
                denominator_column=item.get("denominator_column"),
            )
            for item in raw.get("metrics") or []
        ]

        join_rules = [dict(rule) for rule in (raw.get("join_rules") or [])]

        catalog = cls(
            version=str(raw.get("catalog_version", "v1")),
            dialect=str(raw.get("dialect", "mysql")),
            exposed_tables=exposed,
            blocked_prefixes=blocked,
            tables=tables,
            columns=columns,
            metrics=metrics,
            join_rules=join_rules,  # type: ignore[arg-type]
        )
        catalog.verify_static()
        return catalog

    # ------------------------------------------------------- basic accessors
    @property
    def table_names(self) -> tuple[str, ...]:
        return tuple(self._tables)

    @property
    def allowed_tables(self) -> tuple[str, ...]:
        allowed = [t for t in self.exposed_tables if self._tables.get(t, None) and self._tables[t].allowed]
        return tuple(allowed)

    @property
    def join_rules(self) -> list[JoinRule]:
        return [dict(rule) for rule in self._join_rules]  # type: ignore[misc]

    @property
    def metrics(self) -> list[MetricSemantic]:
        return list(self._metrics)

    def allowed_join_rules(self) -> list[JoinRule]:
        return [rule for rule in self.join_rules if rule.get("allowed")]

    def get_table(self, table: str) -> TableMeta | None:
        return self._tables.get(table)

    def columns_of(self, table: str) -> dict[str, ColumnMeta]:
        return dict(self._columns_by_table.get(table, {}))

    def column_names(self, table: str) -> set[str]:
        return set(self._columns_by_table.get(table, {}))

    def get_column(self, table: str, column: str) -> ColumnMeta | None:
        return self._columns_by_table.get(table, {}).get(column)

    def metrics_for_table(self, table: str) -> list[MetricSemantic]:
        return [metric for metric in self._metrics if metric.table == table]

    def metric_for_column(self, table: str, column: str) -> MetricSemantic | None:
        for metric in self._metrics:
            if metric.table == table and metric.column == column:
                return metric
        return None

    def is_join_allowed(self, left: str, right: str) -> bool:
        for rule in self._join_rules:
            if not rule.get("allowed"):
                continue
            pair = {rule["left_table"], rule["right_table"]}
            if pair == {left, right}:
                return True
        return False

    def join_key(self, left: str, right: str) -> tuple[str, str] | None:
        for rule in self._join_rules:
            if not rule.get("allowed"):
                continue
            if {rule["left_table"], rule["right_table"]} == {left, right}:
                left_column = rule.get("left_column")
                right_column = rule.get("right_column")
                if not left_column or not right_column:
                    return None
                if rule["left_table"] == left:
                    return left_column, right_column
                return right_column, left_column
        return None

    # ------------------------------------------------------------- matching
    def resolve_metric(self, term: str) -> tuple[TableMeta, ColumnMeta, str] | None:
        """Resolve a metric term to ``(table, column, matched_on)``."""
        return self._resolve(term, sources=("metric",))

    def resolve_dimension(self, term: str) -> tuple[TableMeta, ColumnMeta, str] | None:
        """Resolve a dimension/filter term to ``(table, column, matched_on)``."""
        return self._resolve(term, sources=("dimension", "filter"))

    def _resolve(
        self, term: str, *, sources: Sequence[str]
    ) -> tuple[TableMeta, ColumnMeta, str] | None:
        term_norm = normalize_term(term)
        if not term_norm:
            return None

        best: tuple[int, int, str, ColumnMeta] | None = None

        def consider(column: ColumnMeta, matched_on: str, rank: int) -> None:
            nonlocal best
            specificity = _match_specificity(term_norm, normalize_term(matched_on))
            if specificity == 0:
                return
            key = (rank, specificity)
            if best is None or key > (best[0], best[1]):
                best = (rank, specificity, matched_on, column)

        if "metric" in sources:
            for metric in self._metrics:
                for candidate_term in metric.search_terms():
                    if _match_specificity(term_norm, normalize_term(candidate_term)):
                        column = self.get_column(metric.table, metric.column)
                        if column is not None and column.allowed and metric.table in self.allowed_tables:
                            consider(column, candidate_term, rank=3)

        allowed_roles: set[str] = set()
        if "metric" in sources:
            allowed_roles |= {"metric", "flag", "key"}
        if "dimension" in sources:
            # flags are filterable business attributes too ("是否复购用户" = 1)
            allowed_roles |= {"dimension", "time", "key", "flag"}

        for table_name in self.allowed_tables:
            for column in self._columns_by_table.get(table_name, {}).values():
                if not column.allowed or column.role not in allowed_roles:
                    continue
                for candidate_term in column.search_terms():
                    consider(column, candidate_term, rank=2 if column.role != "key" else 1)

        if best is None:
            return None
        column = best[3]
        table = self.get_table(column.table_name)
        if table is None:
            return None
        return table, column, best[2]

    # --------------------------------------------------------------- search
    def search(
        self,
        *,
        question: str,
        keywords: list[str],
        dimensions: list[str],
        metrics: list[str],
        max_tables: int = MAX_TABLES_DEFAULT,
    ) -> CatalogSearchResult:
        """Deterministic candidate retrieval (section 6.5)."""
        if max_tables <= 0 or max_tables > MAX_TABLES_DEFAULT:
            max_tables = MAX_TABLES_DEFAULT

        question_norm = normalize_term(question)
        evidence: dict[str, TableEvidence] = {}
        table_hits: dict[str, list[str]] = {}

        def bucket(table_name: str) -> TableEvidence:
            if table_name not in evidence:
                evidence[table_name] = TableEvidence(table=table_name)
            return evidence[table_name]

        def record(match: TermMatch, reason: str) -> None:
            bucket(match.table).add(
                match,
                ROLE_WEIGHT.get(match.role, 0.5) * SOURCE_WEIGHT[match.source],
                reason,
            )

        # 1) metrics first, then dimensions, then table level synonyms
        for term in metrics:
            resolved = self.resolve_metric(term)
            if resolved is None:
                continue
            table, column, matched_on = resolved
            record(
                TermMatch(
                    term=term,
                    table=table.table_name,
                    column=column.column_name,
                    role=column.role,
                    matched_on=matched_on,
                    source="metric",
                    weight=ROLE_WEIGHT.get(column.role, 1.0),
                ),
                f"命中指标 {column.business_name or column.column_name}",
            )

        for term in dimensions:
            resolved = self.resolve_dimension(term)
            if resolved is None:
                continue
            table, column, matched_on = resolved
            record(
                TermMatch(
                    term=term,
                    table=table.table_name,
                    column=column.column_name,
                    role=column.role,
                    matched_on=matched_on,
                    source="dimension",
                    weight=ROLE_WEIGHT.get(column.role, 0.8),
                ),
                f"命中维度 {column.business_name or column.column_name}",
            )

        for term in keywords:
            resolved = self._resolve(term, sources=("metric", "dimension"))
            if resolved is not None:
                table, column, matched_on = resolved
                specificity = _match_specificity(
                    normalize_term(term), normalize_term(matched_on)
                )
                if specificity:
                    # An exact keyword hit keeps full weight; a partial hit is
                    # discounted so that it cannot outrank a declared metric.
                    record(
                        TermMatch(
                            term=term,
                            table=table.table_name,
                            column=column.column_name,
                            role=column.role,
                            matched_on=matched_on,
                            source="keyword",
                            weight=ROLE_WEIGHT.get(column.role, 0.6),
                        ),
                        f"关键词命中字段 {column.business_name or column.column_name}",
                    )
                    if specificity < 100:
                        evidence[table.table_name].points -= (
                            ROLE_WEIGHT.get(column.role, 0.6) * SOURCE_WEIGHT["keyword"] * 0.2
                        )
                    continue
            # table level synonyms
            for table_name in self.allowed_tables:
                table = self._tables[table_name]
                for table_term in table.search_terms():
                    if _match_specificity(normalize_term(term), normalize_term(table_term)):
                        hits = table_hits.setdefault(table_name, [])
                        if term not in hits:
                            hits.append(term)
                        bucket(table_name).points += TABLE_SYNONYM_POINTS
                        reason = f"关键词命中表语义 {table_name}"
                        if reason not in bucket(table_name).reasons:
                            bucket(table_name).reasons.append(reason)
                        break

        # question-substring matching as the weakest evidence
        for table_name in self.allowed_tables:
            for column in self._columns_by_table.get(table_name, {}).values():
                if not column.allowed:
                    continue
                for candidate_term in column.search_terms():
                    target = normalize_term(candidate_term)
                    if len(target) >= MIN_SUBSTRING_LEN and target in question_norm:
                        record(
                            TermMatch(
                                term=candidate_term,
                                table=table_name,
                                column=column.column_name,
                                role=column.role,
                                matched_on=candidate_term,
                                source="question",
                                weight=ROLE_WEIGHT.get(column.role, 0.6),
                            ),
                            f"问题文本命中 {column.business_name or column.column_name}",
                        )
                        break

        candidates: list[CatalogCandidate] = []
        for table_name, item in evidence.items():
            if not item.matched_columns and not table_hits.get(table_name):
                continue
            score = min(1.0, item.points / SCORE_SCALE)
            if score < SCORE_THRESHOLD:
                continue
            table = self._tables[table_name]
            reason_parts = list(item.reasons) or ["命中表语义"]
            candidates.append(
                CatalogCandidate(
                    table=table_name,
                    matched_columns=list(item.matched_columns),
                    matched_terms=list(item.matched_terms),
                    score=round(score, 4),
                    reason=f"{table.grain}；" + "；".join(reason_parts[:3]),
                )
            )

        candidates.sort(key=lambda c: (-c["score"], c["table"]))
        candidates = candidates[:max_tables]

        relevant_rules = [
            rule
            for rule in self._join_rules
            if rule["left_table"] in {c["table"] for c in candidates}
            or rule["right_table"] in {c["table"] for c in candidates}
        ]
        return CatalogSearchResult(
            candidates=candidates,
            join_rules=relevant_rules,  # type: ignore[typeddict-item]
            catalog_version=self.version,
        )

    # ---------------------------------------------------------- consistency
    def verify_static(self) -> None:
        """Section 6.7 checks that do not need a database connection."""
        issues: list[str] = []

        if not self.exposed_tables:
            issues.append("exposed_tables 不能为空")

        for table_name in self.exposed_tables:
            table = self._tables.get(table_name)
            if table is None:
                issues.append(f"白名单表 {table_name} 缺少表级元数据")
                continue
            if not table.allowed:
                issues.append(f"白名单表 {table_name} 未标记 allowed")
            if not table.grain.strip():
                issues.append(f"{table_name} 缺少 grain")
            if not table.description.strip():
                issues.append(f"{table_name} 缺少 description")
            if not table.primary_key:
                issues.append(f"{table_name} 缺少 primary_key")
            elif not self.get_column(table_name, table.primary_key):
                issues.append(f"{table_name}.primary_key={table.primary_key} 不在字段清单中")
            for time_column in table.default_time_columns:
                meta = self.get_column(table_name, time_column)
                if meta is None:
                    issues.append(f"{table_name}.default_time_columns 指向未知字段 {time_column}")
                elif meta.role != "time":
                    issues.append(f"{table_name}.{time_column} 的 role 应为 time")
            if not self._columns_by_table.get(table_name):
                issues.append(f"{table_name} 没有任何字段元数据")

        for table_name, columns in self._columns_by_table.items():
            if table_name not in self.exposed_tables:
                issues.append(f"字段元数据引用了未暴露的表 {table_name}")
            for column in columns.values():
                if column.role in {"metric", "flag"} and column.aggregation == "none":
                    issues.append(
                        f"指标字段 {column.qualified} 缺少默认聚合方式（section 6.7.4）"
                    )
                if column.is_ratio:
                    for related in (column.ratio_numerator, column.ratio_denominator):
                        if not related or related not in columns:
                            issues.append(
                                f"比率字段 {column.qualified} 的分子/分母 {related!r} 不存在"
                            )

        for metric in self._metrics:
            if metric.table not in self.exposed_tables:
                issues.append(f"指标 {metric.name} 引用了未暴露的表 {metric.table}")
            if not self.get_column(metric.table, metric.column):
                issues.append(f"指标 {metric.name} 引用了未知字段 {metric.table}.{metric.column}")
            if metric.aggregation == "none" or not metric.aggregation:
                issues.append(f"指标 {metric.name} 缺少聚合方式")
            if not metric.formula:
                issues.append(f"指标 {metric.name} 缺少业务口径 formula")
            for extra in (metric.numerator_column, metric.denominator_column):
                if extra and not self.get_column(metric.table, extra):
                    issues.append(
                        f"指标 {metric.name} 的分子/分母字段 {metric.table}.{extra} 不存在"
                    )
            if metric.is_ratio and not (metric.numerator_column and metric.denominator_column):
                issues.append(f"比率指标 {metric.name} 必须声明 numerator_column/denominator_column")

        # synonyms must not point at conflicting metrics
        owners: dict[str, set[str]] = {}
        for metric in self._metrics:
            for term in metric.search_terms():
                owners.setdefault(normalize_term(term), set()).add(metric.name)
        for term, names in owners.items():
            if len(names) > 1:
                issues.append(f"同义词 {term} 同时指向指标 {sorted(names)}")

        for rule in self._join_rules:
            for side in ("left", "right"):
                table_name = rule.get(f"{side}_table")
                column_name = rule.get(f"{side}_column")
                if table_name not in self.exposed_tables:
                    issues.append(f"join_rule 引用了未暴露的表 {table_name}")
                    continue
                if column_name and not self.get_column(str(table_name), str(column_name)):
                    issues.append(f"join_rule 引用了未知字段 {table_name}.{column_name}")
            if rule.get("allowed"):
                left_column = rule.get("left_column")
                right_column = rule.get("right_column")
                if not left_column or not right_column:
                    issues.append(
                        f"允许的 join_rule {rule['left_table']}~{rule['right_table']} 缺少关联字段"
                    )
                    continue
                left_type = (self.get_column(rule["left_table"], left_column) or ColumnMeta(
                    table_name="", column_name="", data_type="", role="key", business_name=""
                )).data_type
                right_type = (self.get_column(rule["right_table"], right_column) or ColumnMeta(
                    table_name="", column_name="", data_type="", role="key", business_name=""
                )).data_type
                if left_type and right_type and left_type != right_type:
                    issues.append(
                        f"join_rule {rule['left_table']}.{left_column}({left_type}) 与 "
                        f"{rule['right_table']}.{right_column}({right_type}) 类型不兼容"
                    )

        if issues:
            raise CatalogConsistencyError(issues)

    def verify_live(self, live_columns: Mapping[str, Iterable[str]]) -> None:
        """Section 6.7.2: every catalog column must exist in the MySQL view."""
        issues: list[str] = []
        for table_name in self.exposed_tables:
            actual = {str(name) for name in live_columns.get(table_name, [])}
            if not actual:
                issues.append(f"数据库中不存在视图 {table_name}")
                continue
            for column in self._columns_by_table.get(table_name, {}):
                if column not in actual:
                    issues.append(f"字段 {table_name}.{column} 在数据库中不存在")
        if issues:
            raise CatalogConsistencyError(issues)

    # ------------------------------------------------------------ rendering
    def describe_selection(self, selections: Sequence[Mapping[str, Any]]) -> str:
        """Render selected tables/columns for the SQL generation prompt."""
        blocks: list[str] = []
        for selection in selections:
            table_name = str(selection["table"])
            table = self._tables.get(table_name)
            if table is None:
                continue
            lines = [
                f"表 {table_name}（{table.grain}）：{table.description}",
                f"  主键/粒度字段: {table.primary_key}",
            ]
            if table.default_time_columns:
                lines.append(f"  时间字段: {', '.join(table.default_time_columns)}")
            lines.append("  可用字段:")
            for column_name in selection["columns"]:
                column = self.get_column(table_name, str(column_name))
                if column is None:
                    continue
                bits = [f"    - {column.column_name} ({column.data_type}"]
                if column.unit:
                    bits.append(f", 单位 {column.unit}")
                bits.append(f", 角色 {column.role}, 聚合 {column.aggregation})")
                detail = f"      {column.business_name}：{column.description}"
                if column.is_ratio and column.ratio_numerator and column.ratio_denominator:
                    detail += (
                        f"（比率字段，跨行汇总请使用 "
                        f"{column.ratio_numerator}/{column.ratio_denominator} 重算）"
                    )
                lines.append("".join(bits))
                lines.append(detail)
            blocks.append("\n".join(lines))

        allowed_pairs = [
            f"{rule['left_table']}.{rule['left_column']} = "
            f"{rule['right_table']}.{rule['right_column']}（{rule['cardinality']}）"
            for rule in self.allowed_join_rules()
        ]
        if allowed_pairs:
            blocks.append("允许的 JOIN：\n  " + "\n  ".join(allowed_pairs))
        return "\n".join(blocks)

    def metric_guidance(self) -> str:
        """Metric semantics rendered for the generation prompt (section 6.3.3)."""
        lines: list[str] = []
        for metric in self._metrics:
            lines.append(
                f"- {metric.name} = {metric.formula}（{metric.table}.{metric.column}，"
                f"聚合 {metric.aggregation}）：{metric.description}"
            )
        return "\n".join(lines)
