"""Catalog data models (text2sql_V1.md section 6.3 / 6.5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypedDict

ColumnRole = Literal["key", "dimension", "metric", "time", "flag"]
Aggregation = Literal["none", "count", "sum", "avg", "min", "max", "direct", "weighted_ratio"]
TableKind = Literal["dimension", "mart"]


class JoinRule(TypedDict):
    left_table: str
    left_column: str | None
    right_table: str
    right_column: str | None
    cardinality: Literal["1:1", "1:N", "N:1", "N:N"]
    allowed: bool
    reason: str


class CatalogCandidate(TypedDict):
    table: str
    matched_columns: list[str]
    matched_terms: list[str]
    score: float
    reason: str


class CatalogSearchResult(TypedDict):
    candidates: list[CatalogCandidate]
    join_rules: list[JoinRule]
    catalog_version: str


@dataclass(frozen=True)
class ColumnMeta:
    """Field level metadata (section 6.3.2)."""

    table_name: str
    column_name: str
    data_type: str
    role: ColumnRole
    business_name: str
    synonyms: tuple[str, ...] = ()
    description: str = ""
    nullable: bool = True
    allowed: bool = True
    aggregation: Aggregation = "none"
    unit: str | None = None
    is_ratio: bool = False
    ratio_numerator: str | None = None
    ratio_denominator: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.table_name}.{self.column_name}"

    def search_terms(self) -> tuple[str, ...]:
        """Terms that may match user language, most specific first."""
        terms = [self.business_name, *self.synonyms, self.column_name]
        seen: set[str] = set()
        out: list[str] = []
        for term in terms:
            if term and term not in seen:
                seen.add(term)
                out.append(term)
        return tuple(out)


@dataclass(frozen=True)
class TableMeta:
    """Table level metadata (section 6.3.1)."""

    table_name: str
    table_kind: TableKind
    grain: str
    description: str
    synonyms: tuple[str, ...] = ()
    allowed: bool = True
    default_time_columns: tuple[str, ...] = ()
    primary_key: str = ""

    def search_terms(self) -> tuple[str, ...]:
        return (self.table_name, *self.synonyms)


@dataclass(frozen=True)
class MetricSemantic:
    """Metric semantics (section 6.3.3): what a metric is and how it aggregates."""

    name: str
    table: str
    column: str
    aggregation: str
    business_name: str
    synonyms: tuple[str, ...] = ()
    description: str = ""
    formula: str = ""
    numerator_column: str | None = None
    denominator_column: str | None = None

    def search_terms(self) -> tuple[str, ...]:
        return (self.name, self.business_name, *self.synonyms)

    @property
    def is_ratio(self) -> bool:
        return self.aggregation == "weighted_ratio"

    def required_columns(self) -> tuple[str, ...]:
        """Columns that must be selectable for this metric to be computed."""
        columns = [self.column]
        for extra in (self.numerator_column, self.denominator_column):
            if extra and extra not in columns:
                columns.append(extra)
        return tuple(columns)


@dataclass(frozen=True)
class TermMatch:
    """One deterministic match between an extracted term and a catalog object."""

    term: str
    table: str
    column: str
    role: ColumnRole
    matched_on: str
    source: Literal["metric", "dimension", "keyword", "question", "table_synonym"]
    weight: float
    specificity: int = 0

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass
class TableEvidence:
    """Accumulated evidence for one candidate table during search."""

    table: str
    points: float = 0.0
    matched_columns: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def add(self, match: TermMatch, points: float, reason: str) -> None:
        self.points += points
        if match.column not in self.matched_columns:
            self.matched_columns.append(match.column)
        if match.term not in self.matched_terms:
            self.matched_terms.append(match.term)
        if reason not in self.reasons:
            self.reasons.append(reason)


class CatalogError(RuntimeError):
    """Raised when the catalog itself is missing or malformed."""


class CatalogConsistencyError(CatalogError):
    """Raised when the catalog fails the section 6.7 consistency checks."""

    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("; ".join(issues))


__all__ = [
    "Aggregation",
    "CatalogCandidate",
    "CatalogConsistencyError",
    "CatalogError",
    "CatalogSearchResult",
    "ColumnMeta",
    "ColumnRole",
    "JoinRule",
    "MetricSemantic",
    "TableEvidence",
    "TableKind",
    "TableMeta",
    "TermMatch",
]
