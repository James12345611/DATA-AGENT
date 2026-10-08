"""Optional MySQL backed catalog source (text2sql_V1.md section 6.8 step 6).

The YAML catalog is the V1 source of truth because ``catalog_table`` /
``catalog_column`` in the practice database only carry a subset of the contract
(no ``role``, ``aggregation``, ``allowed`` or join rules).  This module can

* read those tables and merge the extra business names / semantic hints into
  the YAML catalog, and
* assert that storage and YAML agree on the exposed objects.

It never merges objects that are outside the V1 exposure whitelist, and it never
returns raw rows to the LLM.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable, Mapping

from ..catalog.models import CatalogError, ColumnMeta, TableMeta
from ..catalog.service import Catalog, normalize_term
from ..config import Settings


def _connect(dsn: str):
    try:
        from sqlalchemy import create_engine
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise CatalogError("SQLAlchemy is required for CATALOG_SOURCE=mysql") from exc
    return create_engine(dsn, pool_pre_ping=True, pool_recycle=3600)


def fetch_mysql_metadata(dsn: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read ``catalog_table`` / ``catalog_column`` rows."""
    engine = _connect(dsn)
    try:
        with engine.connect() as conn:
            from sqlalchemy import text

            tables = [
                dict(row._mapping)
                for row in conn.execute(
                    text(
                        "SELECT table_name, table_kind, grain, description "
                        "FROM catalog_table ORDER BY table_name"
                    )
                )
            ]
            columns = [
                dict(row._mapping)
                for row in conn.execute(
                    text(
                        "SELECT table_name, column_name, data_type, business_name, "
                        "is_dimension, is_metric, semantic_hint "
                        "FROM catalog_column ORDER BY table_name, column_name"
                    )
                )
            ]
    finally:
        engine.dispose()
    return tables, columns


def enrich_from_mysql(
    catalog: Catalog,
    *,
    table_rows: Iterable[Mapping[str, Any]],
    column_rows: Iterable[Mapping[str, Any]],
) -> Catalog:
    """Merge storage metadata into a YAML catalog without widening its scope."""
    table_rows = list(table_rows)
    column_rows = list(column_rows)
    exposed = set(catalog.exposed_tables)

    tables = {name: catalog.get_table(name) for name in catalog.table_names}
    blocked_rows = [row["table_name"] for row in table_rows if row["table_name"] not in exposed]
    unknown_columns: list[str] = []

    columns: dict[str, dict[str, ColumnMeta]] = {
        name: catalog.columns_of(name) for name in catalog.table_names
    }

    for row in table_rows:
        table_name = str(row["table_name"])
        if table_name not in exposed:
            continue  # internal metadata never enters the V1 surface
        table = tables.get(table_name)
        if table is None:
            raise CatalogError(f"catalog storage lists {table_name} which is absent from YAML")
        description = str(row.get("description") or table.description)
        grain = str(row.get("grain") or table.grain)
        tables[table_name] = replace(table, description=description, grain=grain)

    for row in column_rows:
        table_name = str(row["table_name"])
        column_name = str(row["column_name"])
        if table_name not in exposed:
            continue
        column = columns.get(table_name, {}).get(column_name)
        if column is None:
            unknown_columns.append(f"{table_name}.{column_name}")
            continue
        extra_terms = [
            term.strip()
            for term in str(row.get("semantic_hint") or "").replace("，", ",").split(",")
            if term.strip()
        ]
        business_name = str(row.get("business_name") or column.business_name)
        synonyms = tuple(dict.fromkeys([*column.synonyms, *extra_terms]))
        columns[table_name][column_name] = replace(
            column,
            business_name=business_name,
            synonyms=synonyms,
            data_type=str(row.get("data_type") or column.data_type),
        )

    if unknown_columns:
        raise CatalogError(
            "catalog storage lists columns absent from YAML: " + ", ".join(unknown_columns)
        )

    enriched = Catalog(
        version=catalog.version,
        dialect=catalog.dialect,
        exposed_tables=catalog.exposed_tables,
        blocked_prefixes=catalog.blocked_prefixes,
        tables={name: table for name, table in tables.items() if table is not None},
        columns=columns,
        metrics=[
            metric
            for table_name in catalog.exposed_tables
            for metric in catalog.metrics_for_table(table_name)
        ],
        join_rules=catalog.join_rules,
    )
    enriched.verify_static()
    _ = blocked_rows  # kept for callers that want to log the blocked surface
    return enriched


def storage_whitelist_check(table_rows: Iterable[Mapping[str, Any]], catalog: Catalog) -> list[str]:
    """Report storage tables that are visible but not exposed (informational)."""
    exposed = set(catalog.exposed_tables)
    return sorted(
        {
            str(row["table_name"])
            for row in table_rows
            if str(row["table_name"]) not in exposed
            and not str(row["table_name"]).startswith(catalog.blocked_prefixes)
        }
    )


def search_storage(catalog: Catalog, term: str) -> list[str]:
    """Debug helper: which exposed tables does a term normalize against?"""
    norm = normalize_term(term)
    hits = []
    for table_name in catalog.exposed_tables:
        for column in catalog.columns_of(table_name).values():
            if any(norm in normalize_term(t) for t in column.search_terms()):
                hits.append(column.qualified)
    return hits


def load_catalog_from_settings(settings: Settings) -> Catalog:
    """Load YAML always; merge MySQL metadata when ``CATALOG_SOURCE=mysql``."""
    catalog = Catalog.from_yaml(settings.catalog_path)
    if settings.catalog_source != "mysql":
        return catalog
    dsn = settings.catalog_dsn
    if not dsn:
        raise CatalogError("CATALOG_SOURCE=mysql requires CATALOG_DSN")
    table_rows, column_rows = fetch_mysql_metadata(dsn)
    return enrich_from_mysql(catalog, table_rows=table_rows, column_rows=column_rows)


__all__ = [
    "enrich_from_mysql",
    "fetch_mysql_metadata",
    "load_catalog_from_settings",
    "search_storage",
    "storage_whitelist_check",
]
